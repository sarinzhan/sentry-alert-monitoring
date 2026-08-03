"""GitLabClient — pull the crashing source, git blame, and the diff that last
touched the crash line, so the LLM sees real code instead of just a stack trace.

All lookups are keyed off the Sentry project_id -> GitLab repo mapping
(GITLAB_PROJECTS). Reachable internally, so it bypasses the corporate proxy.
"""
import urllib.parse

import httpx

from app.config import (
    GITLAB_URL, GITLAB_TOKEN, GITLAB_REF, GITLAB_CONTEXT_LINES, GITLAB_PROJECTS, log,
)


class GitLabClient:
    def __init__(self):
        self._path_cache: dict[str, str] = {}   # (repo, module) -> resolved repo path
        self._client = None
        if GITLAB_URL and GITLAB_TOKEN and GITLAB_PROJECTS:
            self._client = httpx.AsyncClient(
                base_url=GITLAB_URL,
                trust_env=False,
                timeout=10,
                headers={"PRIVATE-TOKEN": GITLAB_TOKEN},
            )

    @property
    def enabled(self):
        return self._client is not None

    async def aclose(self):
        if self._client is not None:
            await self._client.aclose()

    # ------------------------------------------------------------ frame -> path
    @staticmethod
    def _pick_frame(p: dict):
        """Frame to fetch source for: topmost in-app frame with a line number.
        Only in-app frames — library files (Spring, etc.) aren't in the repo."""
        frames = p.get("frames_struct") or []
        return next((f for f in frames if f["in_app"] and f.get("lineno")), None)

    @staticmethod
    def _candidate_paths(frame: dict):
        """Guess repo-relative paths for a (Java) frame, most specific first."""
        module = frame.get("module") or ""
        filename = frame.get("filename") or ""
        paths = []
        if frame.get("abs_path") and "/" in frame["abs_path"]:
            paths.append(frame["abs_path"].lstrip("/"))
        if module and "." in module:
            pkg = module.rsplit(".", 1)[0].replace(".", "/")
            fname = filename or (module.rsplit(".", 1)[1] + ".java")
            paths.append(f"src/main/java/{pkg}/{fname}")
            paths.append(f"src/main/kotlin/{pkg}/{fname}")
            paths.append(f"{pkg}/{fname}")
        elif filename:
            paths.append(filename)
        # de-dup, preserve order
        seen, out = set(), []
        for pth in paths:
            if pth not in seen:
                seen.add(pth); out.append(pth)
        return out

    async def locate_source(self, p: dict):
        """Resolve (proj_enc, path, lineno) for the crash frame, or None. Cached per file."""
        if self._client is None:
            return None
        repo = GITLAB_PROJECTS.get(str(p.get("project_id")))
        if not repo:
            return None
        frame = self._pick_frame(p)
        if not frame:
            return None
        proj_enc = urllib.parse.quote(str(repo), safe="")

        ckey = f"{repo}::{frame.get('module')}"
        path = self._path_cache.get(ckey)
        if path is None:
            # verify the project resolves first, so we can tell "wrong project" from
            # "wrong path". GITLAB_PROJECTS must be a numeric id or full namespace path.
            try:
                pr = await self._client.get(f"/api/v4/projects/{proj_enc}")
            except Exception as e:
                log.warning("gitlab project error repo=%s: %s", repo, e)
                return None
            if pr.status_code != 200:
                log.warning("gitlab project NOT FOUND repo=%s (%s) — GITLAB_PROJECTS needs a "
                            "numeric project id or the full namespace path (e.g. mobile/billing)",
                            repo, pr.status_code)
                return None
            path = await self._resolve_path(proj_enc, frame)
            if not path:
                log.warning("gitlab: file not found repo=%s file=%s tried=%s",
                            repo, frame.get("filename"), self._candidate_paths(frame))
                return None
            self._path_cache[ckey] = path
        return proj_enc, path, int(frame["lineno"])

    async def fetch_source(self, loc):
        """Source window around the crash line (loc from locate_source)."""
        proj_enc, path, lineno = loc
        return await self._read_file(proj_enc, path, lineno)

    async def fetch_blame(self, loc):
        """Who last changed the crash line — GitLab blame for that single line."""
        proj_enc, path, lineno = loc
        enc = urllib.parse.quote(path, safe="")
        try:
            r = await self._client.get(
                f"/api/v4/projects/{proj_enc}/repository/files/{enc}/blame",
                params={"ref": GITLAB_REF, "range[start]": lineno, "range[end]": lineno},
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.warning("gitlab blame error path=%s: %s", path, e)
            return None
        commit = ((data[0] if data else {}) or {}).get("commit") or {}
        if not commit:
            return None
        sha = (commit.get("id") or "")[:8]
        msg = (commit.get("message") or "").strip()
        log.info("gitlab blame path=%s line=%d author=%s commit=%s",
                 path, lineno, commit.get("author_name"), sha)
        return {
            "author": commit.get("author_name"),
            "email": commit.get("author_email"),
            "sha": sha,
            "sha_full": commit.get("id"),
            "subject": (msg.splitlines()[0] if msg else "")[:80],
            # "2025-07-18T15:52:33+06:00" -> "2025-07-18 15:52"
            "date": (commit.get("committed_date") or "")[:16].replace("T", " "),
            "line": lineno,
        }

    async def fetch_change(self, loc, sha_full):
        """The diff of the file in the commit that last touched the crash line
        (previous state -> current state). Returns unified-diff text or None."""
        if not sha_full:
            return None
        proj_enc, path, _ = loc
        sha_enc = urllib.parse.quote(str(sha_full), safe="")
        try:
            r = await self._client.get(
                f"/api/v4/projects/{proj_enc}/repository/commits/{sha_enc}/diff"
            )
            r.raise_for_status()
            diffs = r.json()
        except Exception as e:
            log.warning("gitlab diff error commit=%s: %s", str(sha_full)[:8], e)
            return None
        for d in diffs if isinstance(diffs, list) else []:
            if path in (d.get("new_path"), d.get("old_path")):
                text = (d.get("diff") or "").strip()
                if text:
                    log.info("gitlab change ok commit=%s path=%s", str(sha_full)[:8], path)
                    return text[:3000]
        return None

    async def _resolve_path(self, proj_enc: str, frame: dict):
        """Convention paths first; fall back to a repo filename search (multi-module)."""
        for path in self._candidate_paths(frame):
            enc = urllib.parse.quote(path, safe="")
            r = await self._client.get(
                f"/api/v4/projects/{proj_enc}/repository/files/{enc}",
                params={"ref": GITLAB_REF},
            )
            if r.status_code == 200:
                return path
        # fallback: search the repo for the class, match by filename + package path
        module = frame.get("module") or ""
        filename = frame.get("filename") or ""
        classname = module.rsplit(".", 1)[-1] if module else filename.rsplit(".", 1)[0]
        pkgpath = module.rsplit(".", 1)[0].replace(".", "/") if "." in module else ""
        try:
            r = await self._client.get(
                f"/api/v4/projects/{proj_enc}/search",
                params={"scope": "blobs", "search": classname, "ref": GITLAB_REF},
            )
            r.raise_for_status()
            hits = r.json()
        except Exception as e:
            log.warning("gitlab search error: %s", e)
            return None
        best = None
        for h in hits if isinstance(hits, list) else []:
            hp = h.get("path", "")
            if filename and not (hp.endswith("/" + filename) or hp == filename):
                continue
            if pkgpath and hp.endswith(f"{pkgpath}/{filename}"):
                log.info("gitlab: located via search -> %s", hp)
                return hp
            best = best or hp
        if best:
            log.info("gitlab: located via search (loose) -> %s", best)
        return best

    # ------------------------------------------------------- LLM tool backends
    # Thin raw accessors for the MCP tool set (app.services.gitlab_tools).
    # Unlike the fetch_* methods above they raise on HTTP errors — the tool
    # layer turns the exception into a message the model can react to.
    def repo_for(self, p: dict):
        """Mapped GitLab repo path for a parsed event, or None if unmapped."""
        if self._client is None:
            return None
        return GITLAB_PROJECTS.get(str(p.get("project_id")))

    async def read_raw(self, proj_enc: str, path: str):
        """Full raw file content at GITLAB_REF."""
        enc = urllib.parse.quote(path, safe="")
        r = await self._client.get(
            f"/api/v4/projects/{proj_enc}/repository/files/{enc}/raw",
            params={"ref": GITLAB_REF},
        )
        r.raise_for_status()
        return r.text

    async def search_blobs(self, proj_enc: str, query: str, per_page: int = 20):
        """Repo blob search: list of {path, startline, data} hits."""
        r = await self._client.get(
            f"/api/v4/projects/{proj_enc}/search",
            params={"scope": "blobs", "search": query, "ref": GITLAB_REF,
                    "per_page": per_page},
        )
        r.raise_for_status()
        return r.json() or []

    async def blame_range(self, proj_enc: str, path: str, start: int, end: int):
        """Blame entries [{commit, lines}] for a line range."""
        enc = urllib.parse.quote(path, safe="")
        r = await self._client.get(
            f"/api/v4/projects/{proj_enc}/repository/files/{enc}/blame",
            params={"ref": GITLAB_REF, "range[start]": start, "range[end]": end},
        )
        r.raise_for_status()
        return r.json() or []

    async def commit_diffs(self, proj_enc: str, sha: str):
        """All file diffs of one commit (fetch_change returns just one file's)."""
        sha_enc = urllib.parse.quote(str(sha), safe="")
        r = await self._client.get(
            f"/api/v4/projects/{proj_enc}/repository/commits/{sha_enc}/diff"
        )
        r.raise_for_status()
        return r.json() or []

    async def recent_commits(self, proj_enc: str, path: str = None, limit: int = 10):
        """Latest commits on GITLAB_REF, optionally only those touching a path."""
        params = {"ref_name": GITLAB_REF, "per_page": limit}
        if path:
            params["path"] = path
        r = await self._client.get(
            f"/api/v4/projects/{proj_enc}/repository/commits", params=params
        )
        r.raise_for_status()
        return r.json() or []

    async def _read_file(self, proj_enc: str, path: str, lineno: int):
        lo, hi = max(1, lineno - GITLAB_CONTEXT_LINES), lineno + GITLAB_CONTEXT_LINES
        enc = urllib.parse.quote(path, safe="")
        try:
            r = await self._client.get(
                f"/api/v4/projects/{proj_enc}/repository/files/{enc}/raw",
                params={"ref": GITLAB_REF},
            )
            r.raise_for_status()
        except Exception as e:
            log.warning("gitlab read error path=%s: %s", path, e)
            return None
        lines = r.text.splitlines()
        window = lines[lo - 1:hi]
        body = "\n".join(f"{i}{'>' if i == lineno else ':'} {t}"
                         for i, t in enumerate(window, start=lo))
        log.info("gitlab source ok path=%s line=%d", path, lineno)
        return f"{path} (ref {GITLAB_REF}), lines {lo}-{min(hi, len(lines))}:\n{body}"
