"""SentryApiClient — resolve a project's numeric id to its name.

Error webhooks only carry the numeric project id. This looks it up: static
SENTRY_PROJECTS map first, then the Sentry API (cached), else the raw id.
"""
import time
import asyncio

import httpx

from app.config import SENTRY_API_URL, SENTRY_ORG, SENTRY_API_TOKEN, PROJECT_NAMES, log


class SentryApiClient:
    def __init__(self):
        self._cache: dict[str, str] = {}
        self._fetched = 0.0
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

    async def aclose(self):
        if self._api is not None:
            await self._api.aclose()
