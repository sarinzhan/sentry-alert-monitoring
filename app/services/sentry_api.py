"""SentryApiClient — project id/name resolution + Discover event queries.

Error webhooks only carry the numeric project id; resolve_project looks the
name up (static SENTRY_PROJECTS map first, then the API, else the raw id).
The discover() family powers the lookup commands: events by trace id,
request id (/req) and msisdn (/why, /activity).
"""
import time
import asyncio

import httpx

from app.config import (
    SENTRY_API_URL, SENTRY_ORG, SENTRY_API_TOKEN, PROJECT_NAMES,
    SENTRY_MSISDN_FIELDS, SENTRY_REQUEST_ID_FIELDS,
    SENTRY_LOGS_DATASET, SENTRY_LOGS_MSISDN_QUERY, log,
)

# columns every Discover query returns; issue.id maps a hit back to our #short
DISCOVER_FIELDS = ("id", "title", "project", "message", "issue", "issue.id",
                   "timestamp", "environment")
# columns of the logs dataset (mirrors the fields confirmed to exist in this org);
# the full attribute list is discoverable at runtime via log_attributes()
LOG_FIELDS = ("timestamp", "message", "resource.service.name",
              "instrumentation.name", "trace", "msisdn", "deviceId",
              "exception.type", "exception.message", "exception.stacktrace")


def discover_error_hint(e):
    """Human-readable reason for a failed Discover query (for chat replies)."""
    s = str(e)
    if "403" in s:
        return ("403 Forbidden — токену SENTRY_API_TOKEN не хватает прав на "
                "Discover. Нужны scopes event:read и org:read (для Internal "
                "Integration: Permissions → Issue & Event: Read, Organization: Read).")
    return s[:300]


class SentryApiClient:
    def __init__(self):
        self._cache: dict[str, str] = {}
        self._fetched = 0.0
        self._log_attrs: list[str] = []
        self._attrs_fetched = 0.0
        self._lock = asyncio.Lock()
        # dedicated client: trust_env=False so it ignores HTTP(S)_PROXY and talks
        # to Sentry directly on the docker network.
        self._api = None
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

    async def resolve_project(self, project):
        """Map a project id to a name. Order: static map -> API -> raw id fallback."""
        if project is None:
            return None
        s = str(project)
        if not s.isdigit():
            return s                       # already a slug/name (issue payloads)
        if s in PROJECT_NAMES:             # static override, no network needed
            return PROJECT_NAMES[s]
        if self._api is not None:
            if s not in self._cache:
                await self._refresh()
            if s in self._cache:
                return self._cache[s]
        return s                           # fallback: show the numeric id (mappable)

    async def _refresh(self):
        async with self._lock:
            now = time.time()
            if now - self._fetched < 60:      # throttle repeated misses
                return
            self._fetched = now
            try:
                r = await self._api.get(
                    f"/api/0/organizations/{SENTRY_ORG}/projects/",
                    params={"per_page": 100},
                )
                r.raise_for_status()
                for proj in r.json():
                    self._cache[str(proj["id"])] = proj.get("slug") or proj.get("name")
                log.info("project cache refreshed: %d projects", len(self._cache))
            except httpx.HTTPStatusError as e:
                # surface the real reason (e.g. 403 = token missing project:read)
                log.warning("project name fetch failed: %s %s",
                            e.response.status_code, e.response.text[:200])
            except Exception as e:
                log.warning("project name fetch failed: %s", e)

    async def project_id_for_slug(self, slug):
        """Reverse lookup: project slug/name -> numeric id (for the repo map)."""
        if not slug:
            return None
        s = str(slug)
        for pid, name in PROJECT_NAMES.items():
            if name == s:
                return pid
        if self._api is not None:
            if s not in self._cache.values():
                await self._refresh()
            for pid, name in self._cache.items():
                if name == s:
                    return pid
        return None

    async def discover(self, query: str, stats_period=None, start=None, end=None,
                       limit: int = 20, fields=DISCOVER_FIELDS, dataset=None):
        """Events across ALL projects matching a Discover search query
        (self-hosted Sentry ships Discover). Time range: start+end (ISO 8601,
        UTC) or stats_period like '24h'/'7d'. dataset=None queries errors;
        'logs' queries application log lines. Raises on HTTP errors."""
        params = [("field", f) for f in fields]
        params += [("query", query), ("sort", "-timestamp"), ("per_page", str(limit))]
        if dataset:
            params.append(("dataset", dataset))
        if start and end:
            params += [("start", start), ("end", end)]
        else:
            params.append(("statsPeriod", stats_period or "24h"))
        r = await self._api.get(f"/api/0/organizations/{SENTRY_ORG}/events/",
                                params=params)
        r.raise_for_status()
        return ((r.json() or {}).get("data")) or []

    async def search_logs(self, query: str, stats_period=None, start=None,
                          end=None, limit: int = 100, fields=None):
        """Application log lines (Sentry logs dataset) matching a search query.
        fields extends/replaces the default column set (timestamp, message and
        the service name are always included). [] when the dataset is disabled."""
        if not SENTRY_LOGS_DATASET:
            return []
        cols = list(fields or LOG_FIELDS)
        for required in ("resource.service.name", "message", "timestamp"):
            if required not in cols:
                cols.insert(0, required)
        try:
            return await self.discover(query, stats_period=stats_period,
                                       start=start, end=end, limit=limit,
                                       fields=cols, dataset=SENTRY_LOGS_DATASET)
        except httpx.HTTPStatusError as e:
            # a 400 usually means one of the optional columns doesn't exist in
            # this org — retry with the minimal set instead of failing the lookup
            if e.response.status_code != 400:
                raise
            log.warning("logs query 400 with fields=%s — retrying minimal: %s",
                        cols, e.response.text[:200])
            minimal = ("timestamp", "message", "resource.service.name", "trace")
            return await self.discover(query, stats_period=stats_period,
                                       start=start, end=end, limit=limit,
                                       fields=minimal, dataset=SENTRY_LOGS_DATASET)

    async def log_attributes(self):
        """All log attribute keys that exist in this org, straight from the API
        (trace-items attributes endpoint; string + number types). Cached 10 min;
        [] when the Sentry version doesn't expose the endpoint."""
        now = time.time()
        if self._log_attrs and now - self._attrs_fetched < 600:
            return self._log_attrs
        self._attrs_fetched = now
        attrs = set()
        for attr_type in ("string", "number"):
            try:
                r = await self._api.get(
                    f"/api/0/organizations/{SENTRY_ORG}/trace-items/attributes/",
                    params={"itemType": "logs", "attributeType": attr_type,
                            "statsPeriod": "14d"},
                )
                r.raise_for_status()
                for a in r.json() or []:
                    if isinstance(a, dict) and (a.get("key") or a.get("name")):
                        attrs.add(a.get("key") or a.get("name"))
            except Exception as e:
                log.info("log attribute listing (%s) unavailable: %s", attr_type, e)
        self._log_attrs = sorted(attrs)
        if self._log_attrs:
            log.info("log attributes discovered: %d keys", len(self._log_attrs))
        return self._log_attrs

    async def logs_for_user(self, msisdn, stats_period=None, start=None,
                            end=None, limit: int = 100):
        """One user's log lines — full-text search, since the msisdn appears
        inside the message text (SENTRY_LOGS_MSISDN_QUERY template)."""
        q = SENTRY_LOGS_MSISDN_QUERY.format(value=str(msisdn).strip())
        return await self.search_logs(q, stats_period=stats_period, start=start,
                                      end=end, limit=limit)

    async def find_events(self, keys, value, **kw):
        """Try each configured search key (e.g. user.id, then a tag) until one
        returns hits. Returns (matched_key, events) — (None, []) if nothing.
        Raises when EVERY key fails (an API problem, e.g. a 403 on missing
        token scopes — not the same as "no data", which callers show as
        'not found')."""
        value = str(value).strip().strip('"')
        errors = []
        for key in keys:
            try:
                events = await self.discover(f'{key}:"{value}"', **kw)
            except Exception as e:
                log.warning("discover %s:%s failed: %s", key, value, e)
                errors.append(e)
                continue
            if events:
                log.info("discover hit key=%s value=%s n=%d", key, value, len(events))
                return key, events
        if errors and len(errors) == len(keys):
            raise errors[-1]
        return None, []

    async def events_for_request(self, request_id, stats_period="24h", limit=50):
        """(matched_key, events) for one request id, across all services."""
        return await self.find_events(SENTRY_REQUEST_ID_FIELDS, request_id,
                                      stats_period=stats_period, limit=limit)

    async def events_for_user(self, msisdn, stats_period=None, start=None,
                              end=None, limit=100):
        """(matched_key, events) for one user (msisdn), across all services."""
        return await self.find_events(SENTRY_MSISDN_FIELDS, msisdn,
                                      stats_period=stats_period, start=start,
                                      end=end, limit=limit)

    async def event_details(self, project_slug, event_id):
        """One full event (stack trace, breadcrumbs, tags) — the Discover rows
        above only carry summary columns."""
        r = await self._api.get(
            f"/api/0/projects/{SENTRY_ORG}/{project_slug}/events/{event_id}/")
        r.raise_for_status()
        return r.json() or {}

    async def events_for_trace(self, trace_id: str, limit: int = 20):
        """Error events across ALL projects sharing one trace id."""
        return await self.discover(f"trace:{trace_id}", stats_period="24h",
                                   limit=limit)

    async def aclose(self):
        if self._api is not None:
            await self._api.aclose()
