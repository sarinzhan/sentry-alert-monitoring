"""GitLabClient — pull the crashing source, git blame, and the diff that last
touched the crash line, so the LLM sees real code instead of just a stack trace.

All lookups are keyed off the Sentry project_id -> GitLab repo mapping
(GITLAB_PROJECTS). Reachable internally, so it bypasses the corporate proxy.
"""
import asyncio
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

    # ------------------------------------------------- generic repo access (agent tools)
    # The agent may only touch repos listed in GITLAB_PROJECTS — the service token
    # must not become a way to roam the whole GitLab instance.

    @staticmethod
    def known_repos():
        """Sentry project id -> GitLab repo path, as configured (GITLAB_PROJECTS)."""
        return dict(GITLAB_PROJECTS)

    @staticmethod
    def _repo_enc(repo):
        """URL-encoded project ref for /api/v4/projects/..., or None if the repo
        isn't in the GITLAB_PROJECTS allowlist."""
        if str(repo) not in {str(v) for v in GITLAB_PROJECTS.values()}:
            return None
        return urllib.parse.quote(str(repo), safe="")

    async def read_file(self, repo, path, ref=None, start_line=None, end_line=None):
        """A file (or line window) from an allowed repo, with line numbers.
        Returns text, or an error string the LLM can act on."""
        if self._client is None:
            return "gitlab is not configured"
        proj_enc = self._repo_enc(repo)
        if proj_enc is None:
            return f"unknown repo '{repo}' — allowed: {sorted(set(GITLAB_PROJECTS.values()))}"
        ref = ref or GITLAB_REF
        enc = urllib.parse.quote(str(path), safe="")
        try:
            r = await self._client.get(
                f"/api/v4/projects/{proj_enc}/repository/files/{enc}/raw",
                params={"ref": ref},
            )
            r.raise_for_status()
        except Exception as e:
            log.warning("gitlab read_file error repo=%s path=%s: %s", repo, path, e)
            return f"could not read {path}@{ref}: {e}"
        lines = r.text.splitlines()
        lo = max(1, int(start_line)) if start_line else 1
        hi = min(len(lines), int(end_line)) if end_line else min(len(lines), lo + 399)
        body = "\n".join(f"{i}: {t}" for i, t in enumerate(lines[lo - 1:hi], start=lo))
        note = "" if hi >= len(lines) else f"\n… truncated at line {hi} of {len(lines)}"
        return f"{path} (ref {ref}), lines {lo}-{hi} of {len(lines)}:\n{body}{note}"

    async def list_tree(self, repo, path="", ref=None):
        """Entries of a directory in an allowed repo: [{path, type}], or error string."""
        if self._client is None:
            return "gitlab is not configured"
        proj_enc = self._repo_enc(repo)
        if proj_enc is None:
            return f"unknown repo '{repo}' — allowed: {sorted(set(GITLAB_PROJECTS.values()))}"
        try:
            r = await self._client.get(
                f"/api/v4/projects/{proj_enc}/repository/tree",
                params={"ref": ref or GITLAB_REF, "path": path or "",
                        "per_page": 100, "recursive": False},
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.warning("gitlab list_tree error repo=%s path=%s: %s", repo, path, e)
            return f"could not list {path or '/'}: {e}"
        return [{"path": t.get("path"), "type": t.get("type")}
                for t in data if isinstance(t, dict)]

    async def find_file(self, repo, filename, module=None):
        """Locate a file in an allowed repo by bare filename — maps a stack-frame
        class to its repo path. Tries Java/Kotlin convention paths first when the
        module (e.g. com.foo.bar.Baz) is given, then falls back to a filename
        search. Returns a list of repo paths, or an error string."""
        if self._client is None:
            return "gitlab is not configured"
        proj_enc = self._repo_enc(repo)
        if proj_enc is None:
            return f"unknown repo '{repo}' — allowed: {sorted(set(GITLAB_PROJECTS.values()))}"
        if module and "." in module:
            pkg = module.rsplit(".", 1)[0].replace(".", "/")
            fname = filename or (module.rsplit(".", 1)[1] + ".java")
            for cand in (f"src/main/java/{pkg}/{fname}",
                         f"src/main/kotlin/{pkg}/{fname}",
                         f"{pkg}/{fname}"):
                enc = urllib.parse.quote(cand, safe="")
                try:
                    r = await self._client.get(
                        f"/api/v4/projects/{proj_enc}/repository/files/{enc}",
                        params={"ref": GITLAB_REF})
                except Exception as e:
                    log.warning("gitlab find_file error repo=%s: %s", repo, e)
                    return f"find failed: {e}"
                if r.status_code == 200:
                    return [cand]
        try:
            r = await self._client.get(
                f"/api/v4/projects/{proj_enc}/search",
                params={"scope": "blobs", "search": f"filename:{filename}",
                        "ref": GITLAB_REF},
            )
            r.raise_for_status()
            hits = r.json()
        except Exception as e:
            log.warning("gitlab find_file error repo=%s file=%s: %s", repo, filename, e)
            return f"find failed: {e}"
        paths = sorted({h.get("path") for h in hits
                        if isinstance(h, dict) and h.get("path")})[:20]
        return paths or f"no file named '{filename}' found in {repo}"

    async def search_code_all(self, query):
        """search_code across every allowed repo, concurrently: {repo: hits}
        (repos with no hits are omitted)."""
        repos = sorted(set(GITLAB_PROJECTS.values()))
        results = await asyncio.gather(*(self.search_code(r, query) for r in repos))
        # keep error strings too — the model should see a failed repo, not miss it
        return {r: res for r, res in zip(repos, results) if res} \
            or f"no matches for '{query}' in any repo"

    async def recent_commits(self, repo, path=None, limit=20):
        """Recent commits of an allowed repo (optionally only those touching one
        path): [{sha, title, author, date}], or error string. Cheaper than blame
        when there is no line number yet."""
        if self._client is None:
            return "gitlab is not configured"
        proj_enc = self._repo_enc(repo)
        if proj_enc is None:
            return f"unknown repo '{repo}' — allowed: {sorted(set(GITLAB_PROJECTS.values()))}"
        params = {"ref_name": GITLAB_REF, "per_page": max(1, min(int(limit), 50))}
        if path:
            params["path"] = path
        try:
            r = await self._client.get(
                f"/api/v4/projects/{proj_enc}/repository/commits", params=params)
            r.raise_for_status()
            commits = r.json()
        except Exception as e:
            log.warning("gitlab recent_commits error repo=%s path=%s: %s", repo, path, e)
            return f"could not list commits: {e}"
        return [{
            "sha": (c.get("id") or "")[:8],
            "sha_full": c.get("id"),
            "title": (c.get("title") or "")[:100],
            "author": c.get("author_name"),
            "date": (c.get("committed_date") or "")[:16].replace("T", " "),
        } for c in commits if isinstance(c, dict)]

    async def search_code(self, repo, query):
        """Blob search in an allowed repo: [{path, startline, snippet}], or error string."""
        if self._client is None:
            return "gitlab is not configured"
        proj_enc = self._repo_enc(repo)
        if proj_enc is None:
            return f"unknown repo '{repo}' — allowed: {sorted(set(GITLAB_PROJECTS.values()))}"
        try:
            r = await self._client.get(
                f"/api/v4/projects/{proj_enc}/search",
                params={"scope": "blobs", "search": query, "ref": GITLAB_REF},
            )
            r.raise_for_status()
            hits = r.json()
        except Exception as e:
            log.warning("gitlab search_code error repo=%s q=%s: %s", repo, query, e)
            return f"search failed: {e}"
        return [{"path": h.get("path"), "startline": h.get("startline"),
                 "snippet": (h.get("data") or "")[:500]}
                for h in hits if isinstance(h, dict)][:20]

    async def blame_line(self, repo, path, line):
        """Who last changed one line: {author, email, sha, subject, date}, or error string."""
        if self._client is None:
            return "gitlab is not configured"
        proj_enc = self._repo_enc(repo)
        if proj_enc is None:
            return f"unknown repo '{repo}' — allowed: {sorted(set(GITLAB_PROJECTS.values()))}"
        blame = await self.fetch_blame((proj_enc, str(path), int(line)))
        return blame or f"no blame data for {path}:{line}"

    async def commit_diff(self, repo, sha, path=None):
        """Diff of one commit in an allowed repo (optionally just one file),
        as unified-diff text, or error string."""
        if self._client is None:
            return "gitlab is not configured"
        proj_enc = self._repo_enc(repo)
        if proj_enc is None:
            return f"unknown repo '{repo}' — allowed: {sorted(set(GITLAB_PROJECTS.values()))}"
        sha_enc = urllib.parse.quote(str(sha), safe="")
        try:
            r = await self._client.get(
                f"/api/v4/projects/{proj_enc}/repository/commits/{sha_enc}/diff")
            r.raise_for_status()
            diffs = r.json()
        except Exception as e:
            log.warning("gitlab commit_diff error commit=%s: %s", str(sha)[:8], e)
            return f"could not fetch diff of {sha}: {e}"
        out = []
        for d in diffs if isinstance(diffs, list) else []:
            if path and path not in (d.get("new_path"), d.get("old_path")):
                continue
            text = (d.get("diff") or "").strip()
            if text:
                out.append(f"--- {d.get('old_path')}\n+++ {d.get('new_path')}\n{text}")
        text = ("\n\n".join(out))[:12000]
        if text:
            return text
        return f"no diff found for {sha}" + (f" touching {path}" if path else "")

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
