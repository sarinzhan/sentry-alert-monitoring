"""SentryQueryClient — read-only queries against the Sentry REST API.

Companion to SentryApiClient (which only resolves project id -> name). This one
answers questions: list projects, search issues by attribute, fetch an issue's
events. It's the data layer the AgentService's tools call; it does no LLM work.

Same httpx posture as SentryApiClient: trust_env=False so it bypasses the
corporate proxy and reaches Sentry directly on the docker network. Set
SENTRY_API_TOKEN (with project:read + event:read) to enable it.
"""
from datetime import datetime, timedelta, timezone

import httpx

from app.config import SENTRY_API_URL, SENTRY_ORG, SENTRY_API_TOKEN, log


def _trim_exception(entries, lib_frames_max=10):
    """(exception_chain, frames) from an event's entries. Keeps every in-app
    frame but at most lib_frames_max library frames; context_line is the crash
    line itself."""
    chain, frames = [], []
    lib_kept = 0
    for entry in entries or []:
        if entry.get("type") != "exception":
            continue
        for exc in (entry.get("data") or {}).get("values") or []:
            chain.append({"type": exc.get("type"), "value": exc.get("value")})
            for f in ((exc.get("stacktrace") or {}).get("frames") or []):
                in_app = bool(f.get("inApp"))
                if not in_app:
                    if lib_kept >= lib_frames_max:
                        continue
                    lib_kept += 1
                # context is [[lineno, source], ...]; pick the crash line itself
                ctx_line = next(
                    (src for ln, src in (f.get("context") or [])
                     if ln == f.get("lineNo")), None)
                frames.append({
                    "module": f.get("module"),
                    "filename": f.get("filename"),
                    "function": f.get("function"),
                    "lineno": f.get("lineNo"),
                    "in_app": in_app,
                    "context_line": ctx_line,
                })
    return chain, frames


def _trim_crumbs(entries, crumbs_max=30):
    """The LAST crumbs_max breadcrumbs of an event — what happened inside the
    service right before the error. Drops the bulky per-crumb data payloads."""
    crumbs = []
    for entry in entries or []:
        if entry.get("type") != "breadcrumbs":
            continue
        for c in (entry.get("data") or {}).get("values") or []:
            msg = c.get("message") or ""
            crumbs.append({
                "timestamp": c.get("timestamp"),
                "category": c.get("category"),
                "level": c.get("level"),
                "message": msg[:200] if msg else None,
            })
    return crumbs[-crumbs_max:]


def _trace_id(event):
    return (((event.get("contexts") or {}).get("trace")) or {}).get("trace_id")


def _to_dt(v):
    """ISO string (with or without Z) or unix timestamp -> datetime, else None."""
    if v is None:
        return None
    s = str(v)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        pass
    try:
        return datetime.fromtimestamp(float(s), tz=timezone.utc)
    except (ValueError, OverflowError):
        return None


def _iso_norm(v, minus_days=0):
    """Normalize a timestamp to the 'YYYY-MM-DDTHH:MM:SS' form the Discover
    endpoint accepts, optionally shifted back minus_days. None if unparseable."""
    dt = _to_dt(v)
    if dt is None:
        return None
    return (dt - timedelta(days=minus_days)).strftime("%Y-%m-%dT%H:%M:%S")


class SentryQueryClient:
    def __init__(self):
        # built lazily, only when a token is present (empty/"-" -> disabled).
        self._api = None
        self._discover_ok = None    # None = unknown, False = endpoint missing
        self._project_ids = None    # slug -> numeric id, cached from list_projects
        if SENTRY_API_TOKEN:
            self._api = httpx.AsyncClient(
                base_url=SENTRY_API_URL,
                trust_env=False,
                timeout=10,
                headers={"Authorization": f"Bearer {SENTRY_API_TOKEN}"},
            )

    @property
    def enabled(self):
        return self._api is not None

    async def _get(self, path, params=None):
        """GET a Sentry endpoint. Returns parsed JSON, or None on error (logged).

        Mirrors SentryApiClient._refresh error handling: surface the real reason
        (e.g. 403 = token missing project:read/event:read) but never raise —
        the tools layer turns a None into a clean 'no data' for the LLM.
        """
        if self._api is None:
            return None
        try:
            r = await self._api.get(path, params=params or {})
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            log.warning("sentry query failed: %s %s -> %s %s",
                        "GET", path, e.response.status_code, e.response.text[:200])
        except Exception as e:
            log.warning("sentry query failed: GET %s -> %s", path, e)
        return None

    async def _get_status(self, path, params=None):
        """Like _get but returns (status_code, json|None) so the caller can tell
        'endpoint missing' (404) apart from 'no data'. (None, None) on transport
        errors."""
        if self._api is None:
            return None, None
        try:
            r = await self._api.get(path, params=params or {})
            if r.status_code >= 400:
                log.warning("sentry query failed: GET %s -> %s %s",
                            path, r.status_code, r.text[:200])
                return r.status_code, None
            return r.status_code, r.json()
        except Exception as e:
            log.warning("sentry query failed: GET %s -> %s", path, e)
            return None, None

    async def list_projects(self):
        """All projects in the org: [{id, slug, name}, ...] (or [] on error)."""
        data = await self._get(
            f"/api/0/organizations/{SENTRY_ORG}/projects/",
            {"per_page": 100},
        )
        if not data:
            return []
        return [
            {"id": str(p.get("id")), "slug": p.get("slug"), "name": p.get("name")}
            for p in data
        ]

    async def search_issues(self, project_slug, query=None, environment=None,
                            limit=25, stats_period=None):
        """Search a project's issues by attribute.

        query: Sentry search string, e.g. "is:unresolved level:error".
        environment: e.g. "prod". stats_period: e.g. "24h", "14d".
        Returns a list of compact issue dicts (or [] on error).
        """
        params = {"limit": max(1, min(int(limit), 100))}
        if query:
            params["query"] = query
        if environment:
            params["environment"] = environment
        if stats_period:
            params["statsPeriod"] = stats_period
        data = await self._get(
            f"/api/0/projects/{SENTRY_ORG}/{project_slug}/issues/",
            params,
        )
        if not data:
            return []
        return [
            {
                "id": str(i.get("id")),
                "title": i.get("title"),
                "culprit": i.get("culprit"),
                "level": i.get("level"),
                "status": i.get("status"),
                "count": i.get("count"),
                "userCount": i.get("userCount"),
                "firstSeen": i.get("firstSeen"),
                "lastSeen": i.get("lastSeen"),
                "permalink": i.get("permalink"),
            }
            for i in data
        ]

    async def search_org_issues(self, query=None, environment=None,
                                limit=25, stats_period=None):
        """Search issues across ALL projects in the org (e.g. query="user.id:123").
        Same compact shape as search_issues, plus the project slug."""
        params = {"limit": max(1, min(int(limit), 100))}
        if query:
            params["query"] = query
        if environment:
            params["environment"] = environment
        if stats_period:
            params["statsPeriod"] = stats_period
        data = await self._get(
            f"/api/0/organizations/{SENTRY_ORG}/issues/",
            params,
        )
        if not data:
            return []
        return [
            {
                "id": str(i.get("id")),
                "project": (i.get("project") or {}).get("slug"),
                "title": i.get("title"),
                "culprit": i.get("culprit"),
                "level": i.get("level"),
                "status": i.get("status"),
                "count": i.get("count"),
                "userCount": i.get("userCount"),
                "firstSeen": i.get("firstSeen"),
                "lastSeen": i.get("lastSeen"),
                "permalink": i.get("permalink"),
            }
            for i in data
        ]

    async def _project_id_list(self, slug=None):
        """Numeric project ids for the Discover endpoint (cached from
        list_projects). One id when a slug is given, all known ids otherwise."""
        if self._project_ids is None:
            self._project_ids = {p["slug"]: p["id"]
                                 for p in await self.list_projects() if p.get("slug")}
        if slug:
            pid = self._project_ids.get(slug)
            return [pid] if pid else []
        return list(self._project_ids.values())

    async def search_events(self, query, project=None, stats_period=None,
                            start=None, end=None, sort="-timestamp",
                            extra_fields=None, limit=20):
        """Search raw EVENTS org-wide via the Discover endpoint.

        query: Sentry event search string — 'msisdn:996555123456 "Read timed out"',
        'trace:<trace_id>' (whole call chain of one request), etc.
        start/end: ISO timestamps; when only end is given the window defaults to
        the 7 days before it (the "user's actions before the error" case).
        Returns compact rows, [] when nothing matched, or None when this Sentry
        version has no Discover endpoint (the tools layer degrades on None).
        """
        if self._discover_ok is False:
            return None
        fields = ["id", "title", "message", "project", "timestamp", "trace",
                  "level", "environment", "msisdn"]
        for f in extra_fields or []:
            if f and f not in fields:
                fields.append(f)
        params = [("field", f) for f in fields]
        params += [("query", query or ""),
                   ("sort", sort or "-timestamp"),
                   ("per_page", str(max(1, min(int(limit), 50))))]
        # the endpoint wants start+end together; default a 7d window before end
        s, e = _iso_norm(start), _iso_norm(end)
        if e and not s:
            s = _iso_norm(end, minus_days=7)
        if s and e:
            params += [("start", s), ("end", e)]
        elif stats_period:
            params.append(("statsPeriod", stats_period))
        for pid in await self._project_id_list(project):
            params.append(("project", str(pid)))
        status, data = await self._get_status(
            f"/api/0/organizations/{SENTRY_ORG}/events/", params)
        if status == 404:
            if self._discover_ok is not False:
                log.warning("sentry Discover events endpoint unavailable "
                            "(404) — event search disabled for this process")
            self._discover_ok = False
            return None
        if data is None:
            return None  # transport error or bad query — tool layer explains
        self._discover_ok = True
        out = []
        for row in data.get("data") or []:
            item = {"event_id": row.get("id")}
            for f in fields[1:]:
                if row.get(f) not in (None, ""):
                    item[f] = row[f]
            out.append(item)
        return out

    async def get_event(self, project_slug, event_id,
                        lib_frames_max=10, crumbs_max=30):
        """Full detail of ONE event: exception chain + frames, breadcrumbs (the
        service's last actions before the error), tags, request url and
        trace_id. None on error."""
        e = await self._get(
            f"/api/0/projects/{SENTRY_ORG}/{project_slug}/events/{event_id}/")
        if not e:
            return None
        tags = {t.get("key"): t.get("value") for t in (e.get("tags") or [])
                if isinstance(t, dict)}
        chain, frames = _trim_exception(e.get("entries"), lib_frames_max)
        request = None
        for entry in e.get("entries") or []:
            if entry.get("type") == "request":
                d = entry.get("data") or {}
                request = {"method": d.get("method"), "url": d.get("url")}
                break
        return {
            "id": e.get("eventID") or e.get("id"),
            "dateCreated": e.get("dateCreated"),
            "message": e.get("message") or e.get("title"),
            "environment": tags.get("environment"),
            "user": e.get("user"),
            "tags": tags,
            "trace_id": _trace_id(e),
            "request": request,
            "exception_chain": chain,
            "frames": frames,
            "breadcrumbs": _trim_crumbs(e.get("entries"), crumbs_max),
        }

    async def issue_details(self, issue_id):
        """One issue's metadata: title, culprit, level, status, counts, project,
        firstSeen/lastSeen, permalink. None on error."""
        i = await self._get(f"/api/0/organizations/{SENTRY_ORG}/issues/{issue_id}/")
        if not i:
            return None
        return {
            "id": str(i.get("id")),
            "project": (i.get("project") or {}).get("slug"),
            "title": i.get("title"),
            "culprit": i.get("culprit"),
            "level": i.get("level"),
            "status": i.get("status"),
            "count": i.get("count"),
            "userCount": i.get("userCount"),
            "firstSeen": i.get("firstSeen"),
            "lastSeen": i.get("lastSeen"),
            "permalink": i.get("permalink"),
            "metadata": i.get("metadata"),
        }

    async def issue_latest_event(self, issue_id, lib_frames_max=10):
        """The issue's latest event with the full exception chain + stack frames.
        Keeps every in-app frame but at most lib_frames_max library frames.
        None on error."""
        e = await self._get(
            f"/api/0/organizations/{SENTRY_ORG}/issues/{issue_id}/events/latest/")
        if not e:
            return None
        tags = {t.get("key"): t.get("value") for t in (e.get("tags") or [])
                if isinstance(t, dict)}
        chain, frames = _trim_exception(e.get("entries"), lib_frames_max)
        return {
            "id": e.get("eventID") or e.get("id"),
            "dateCreated": e.get("dateCreated"),
            "message": e.get("message") or e.get("title"),
            "environment": tags.get("environment"),
            "user": e.get("user"),
            "tags": tags,
            "trace_id": _trace_id(e),
            "exception_chain": chain,
            "frames": frames,
        }

    async def issue_events(self, issue_id, limit=10, query=None, full=False):
        """Recent events for one issue: [{dateCreated, message, environment,
        user, tags}, ...] (or [] on error).

        query filters events (e.g. 'msisdn:996555123456'); full=True asks the
        API for complete event bodies so trace_id can be included. This is the
        degraded search path for Sentry versions without the Discover endpoint.
        """
        params = {"limit": max(1, min(int(limit), 100))}
        if query:
            params["query"] = query
        if full:
            params["full"] = "true"
        data = await self._get(
            f"/api/0/organizations/{SENTRY_ORG}/issues/{issue_id}/events/",
            params,
        )
        if not data:
            return []
        out = []
        for e in data:
            tags = {t.get("key"): t.get("value") for t in (e.get("tags") or [])
                    if isinstance(t, dict)}
            row = {
                "id": e.get("eventID") or e.get("id"),
                "dateCreated": e.get("dateCreated"),
                "message": e.get("message") or e.get("title"),
                "environment": e.get("environment") or tags.get("environment"),
                "user": (e.get("user") or {}).get("email")
                or (e.get("user") or {}).get("username")
                or (e.get("user") or {}).get("id"),
                "tags": tags,
            }
            if full:
                row["trace_id"] = _trace_id(e)
            out.append(row)
        return out

    async def aclose(self):
        if self._api is not None:
            await self._api.aclose()
