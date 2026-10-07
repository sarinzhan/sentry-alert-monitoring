"""EventPipeline — orchestrates a Sentry webhook end to end.

parse → record → find subscribers → per chat decide → enrich (blame/LLM) → send.
Holds the asyncio.Lock that serializes the event insert and each per-chat
read-modify-write of the send state.
"""
import json
import time
import hashlib
import asyncio

from app.config import LOG_RAW_PAYLOAD, LOG_LLM_PROMPT, GROUP_ENABLED, log

# Sentry delivers webhooks at-least-once; a redelivery of the SAME event within
# this window is dropped (idempotency), so one error can't alert twice.
_DEDUP_WINDOW_SEC = 120
from app.summaries import fmt_duration
from app.sentry.parser import parse, NOTIFY_ACTIONS
from app.sentry.message import build_message


def _as_chat_id(chat_id):
    """Telegram accepts int ids for groups/users and str for @channels."""
    s = str(chat_id)
    try:
        return int(s)
    except ValueError:
        return s


class EventPipeline:
    def __init__(self, *, issues, subscriptions, rules, keywords, usermap, context,
                 decider, sentry_api, gitlab, analysis, sender, incidents=None):
        self.issues = issues
        self.subscriptions = subscriptions
        self.rules = rules
        self.keywords = keywords
        self.usermap = usermap
        self.context = context
        self.decider = decider
        self.sentry_api = sentry_api
        self.gitlab = gitlab
        self.analysis = analysis
        self.incidents = incidents      # IncidentService (grouping subsystem), may be None
        self._send = sender
        self._lock = asyncio.Lock()
        self._recent = {}          # delivery key -> ts, for at-least-once dedup

    @staticmethod
    def _delivery_key(p, payload):
        """Identify one webhook delivery: the Sentry event id when present,
        otherwise a hash of the whole payload (catches re-sent issue payloads
        that carry no event id)."""
        if p.get("event_id"):
            return f"e:{p['event_id']}"
        blob = json.dumps(payload, sort_keys=True, default=str)
        return "h:" + hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()

    def _seen_recently(self, key, now):
        """True if this exact delivery was already processed within the dedup
        window; records it otherwise. Caller must hold self._lock."""
        cutoff = now - _DEDUP_WINDOW_SEC
        for k in [k for k, t in self._recent.items() if t < cutoff]:
            del self._recent[k]
        if key in self._recent:
            return True
        self._recent[key] = now
        return False

    async def process(self, resource: str, payload: dict):
        try:
            if LOG_RAW_PAYLOAD:
                self._log_raw(resource, payload)
            p = parse(resource, payload)
            if not p:
                log.info("ignored: nothing to parse")
                return
            if p.get("action") not in NOTIFY_ACTIONS:
                log.info("ignored action=%s issue=%s", p.get("action"), p["issue_id"])
                return
            now = time.time()
            # drop re-deliveries of the SAME webhook (Sentry is at-least-once) so
            # one error can't produce two identical alerts
            dedup_key = self._delivery_key(p, payload)
            async with self._lock:
                if self._seen_recently(dedup_key, now):
                    log.info("ignored duplicate delivery issue=%s key=%s",
                             p["issue_id"], dedup_key[:18])
                    return
                # record the occurrence globally + keep issue metadata (short id, title)
                self.issues.record_event(p["issue_id"], p.get("usr"), now)
            p["short"] = self.issues.ensure_issue(p["issue_id"], p.get("title") or "")

            # keyword force-send bypasses the per-chat min gap + status filter
            forced = self.keywords.match(f"{p.get('title') or ''} {p.get('value') or ''}", p) is not None

            # who wants this project? (subscription by numeric id, slug, or '*'). No
            # subscribers -> nothing to do (alerts are opt-in per chat).
            raw_project = p.get("project")
            p["project"] = await self.sentry_api.resolve_project(raw_project)   # id -> name for header
            targets = self.subscriptions.chats_for(p.get("project_id"), raw_project, p.get("project"))
            if not targets:
                log.info("no subscribers issue=%s project=%s", p["issue_id"], p.get("project"))
                if LOG_LLM_PROMPT:          # debug: still build+log the prompt if asked
                    p["_loc"] = await self.gitlab.locate_source(p)
                    if p["_loc"]:
                        p["blame"] = await self.gitlab.fetch_blame(p["_loc"])
                    await self.analysis.analyze(p)
                return

            # locate the crash file once -> reused for blame (author, shown always) + LLM
            p["_loc"] = await self.gitlab.locate_source(p)
            if p["_loc"]:
                p["blame"] = await self.gitlab.fetch_blame(p["_loc"])
                if p.get("blame"):                            # map vcs author -> @telegram
                    p["blame"]["tg"] = self.usermap.lookup(p["blame"].get("author"))
            # save context so /ai can re-run the LLM on demand for this issue
            self.context.store_ctx(p)

            # LLM cause/fix is computed at most once per event, lazily, and SHARED
            # by the incident description and every classic alert (cached per
            # issue+commit inside analyze(), so repeats are cheap).
            analysis_state = {"done": False, "text": None}

            async def get_analysis():
                if not analysis_state["done"]:
                    analysis_state["text"] = await self.analysis.analyze(p, use_tools=True)
                    analysis_state["done"] = True
                return analysis_state["text"]

            # incident grouping subsystem (org-scoped, opt-in per chat): group this
            # issue and, for incident-enabled subscribers, notify/edit the single
            # incident message. Independent of the per-issue error path below.
            if GROUP_ENABLED and self.incidents is not None:
                try:
                    await self.incidents.handle(p, targets, get_analysis, now)
                except Exception as e:
                    log.warning("incident handling failed issue=%s: %s", p["issue_id"], e)

            # stable key for the per-project window (id preferred over slug/name)
            project_key = p.get("project_id") or raw_project or p.get("project")
            analysis = None
            sent = 0
            for chat_id, thread_id in targets:
                rules = self.rules.effective(chat_id)
                async with self._lock:
                    send, status = self.decider.decide(
                        chat_id, p["issue_id"], rules, now, forced, project=project_key)
                if not send:
                    continue
                # agentic cause/fix on EVERY alert (shared getter above, cached)
                analysis = await get_analysis()
                # per-chat view: counts over this chat's stat windows + matching labels
                pc = dict(p)
                pc["status"] = status
                pc["counts"] = self.issues.counts_for(p["issue_id"], rules["stat_windows"], now)
                pc["stat_labels"] = "/".join(fmt_duration(w) for w in rules["stat_windows"])
                pc["llm_meta"] = p.get("llm_meta")
                msg = await self._send(
                    build_message(pc, analysis),
                    chat_id=_as_chat_id(chat_id),
                    message_thread_id=int(thread_id) if thread_id else None)
                if msg is not None:
                    self.context.store_sent(msg.message_id, msg.chat_id, p["issue_id"], p["short"])
                    self.issues.cache_url_project(p["issue_id"], p.get("url"), p.get("project"))
                    self.issues.mark_sent(p["issue_id"], now, critical=(status == "escalating"))
                    sent += 1
                    log.info("sent issue=%s chat=%s status=%s counts=%s%s",
                             p["issue_id"], chat_id, status, pc["counts"],
                             " FORCED" if forced else "")
            if not sent:
                log.info("skip issue=%s no chat triggered (targets=%d)",
                         p["issue_id"], len(targets))
        except Exception as e:
            log.exception("process failed: %s", e)

    @staticmethod
    def _log_raw(resource: str, payload: dict):
        """Dump the raw webhook so we can see the payload shape (esp. `project`)."""
        data = payload.get("data", {}) or {}
        obj = data.get("issue") or data.get("error") or data.get("event") or {}
        proj_fields = {k: obj.get(k) for k in obj if "project" in k.lower()}
        log.info("raw webhook resource=%s action=%s top_keys=%s obj_keys=%s",
                 resource, payload.get("action"), sorted(payload.keys()), sorted(obj.keys()))
        log.info("raw project* fields: %s", proj_fields)
        log.info("raw payload json (truncated 20k): %s",
                 json.dumps(payload, default=str)[:20000])
