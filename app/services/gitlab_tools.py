"""GitLab tool set for the LLM — an in-process MCP server (Claude Agent SDK).

build_gitlab_server() exposes six read-only tools over the MAPPED repos (the
GITLAB_PROJECTS values), so the model can follow a cause across services —
e.g. billing crashes because payments threw upstream — but can't reach repos
outside the configured set: the 'repo' argument is an enum built from trusted
config, defaulting to the crashing service's repo. Every tool caps its output
so one bad call can't flood the context window.

The tools mirror what an engineer does when triaging a stack trace:
read_file / find_file / search_code for code, blame / commit_diff /
recent_commits for "did a recent change break this".
"""
import posixpath
import urllib.parse

from claude_agent_sdk import tool, create_sdk_mcp_server

from app.config import GITLAB_PROJECTS, GITLAB_REF, log

MAX_LINES = 200        # read_file lines per call
MAX_HITS = 20          # find_file / search_code results
MAX_SNIPPET = 400      # chars of matched fragment per search hit
MAX_BLAME_LINES = 100  # blame range span
MAX_DIFF = 5000        # chars of commit diff
MAX_COMMITS = 20       # recent_commits ceiling

# Source file extensions a find_file query may carry; anything else after a dot
# is treated as a Java-style package prefix (com.foo.Bar -> Bar).
_FILE_EXTS = (".java", ".kt", ".kts", ".scala", ".groovy", ".xml", ".yml",
              ".yaml", ".properties", ".sql", ".json", ".gradle")


def _text(s: str):
    return {"content": [{"type": "text", "text": s}]}


def _subject(commit: dict):
    msg = (commit.get("message") or "").strip()
    return (msg.splitlines()[0] if msg else "")[:80]


def build_gitlab_server(gitlab, primary_repo: str):
    """(mcp_servers dict, allowed_tools list); tools cover all mapped repos,
    with primary_repo (the crashing service) as the default target."""
    repos = sorted(set(GITLAB_PROJECTS.values()))
    repo_prop = {
        "type": "string",
        "enum": repos,
        "description": f"Which service's repo. Default: {primary_repo} "
                       "(the crashing service).",
    }

    def _enc(args):
        """Resolve the repo argument to (proj_enc, error_result)."""
        repo = (args.get("repo") or primary_repo or "").strip()
        if repo not in repos:
            return None, _text(f"Unknown repo '{repo}'. "
                               f"Mapped repos: {', '.join(repos)}")
        return urllib.parse.quote(repo, safe=""), None

    @tool(
        "read_file",
        f"Read a file from a mapped service repo (ref {GITLAB_REF}). Returns "
        f"numbered lines; use start_line/end_line for a window (max {MAX_LINES} "
        "lines per call). Call this to see code beyond the provided crash-site "
        "snippet — the full method, the caller, a related class.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "Repo-relative file path (from find_file, "
                                        "search_code, or the stack trace)"},
                "start_line": {"type": "integer", "description": "First line, 1-based"},
                "end_line": {"type": "integer", "description": "Last line, inclusive"},
                "repo": repo_prop,
            },
            "required": ["path"],
        },
    )
    async def read_file(args):
        proj_enc, err = _enc(args)
        if err:
            return err
        path = (args.get("path") or "").lstrip("/")
        try:
            content = await gitlab.read_raw(proj_enc, path)
        except Exception as e:
            return _text(f"Error reading {path}: {e}. "
                         "Use find_file to resolve the correct repo path.")
        lines = content.splitlines()
        start = max(1, int(args.get("start_line") or 1))
        end = int(args.get("end_line") or start + MAX_LINES - 1)
        end = min(end, start + MAX_LINES - 1, len(lines))
        body = "\n".join(f"{i}: {t}" for i, t in
                         enumerate(lines[start - 1:end], start=start))
        log.info("gitlab tool read_file repo=%s path=%s %d-%d",
                 args.get("repo") or primary_repo, path, start, end)
        return _text(f"{path} lines {start}-{end} of {len(lines)}:\n{body}")

    @tool(
        "find_file",
        "Locate a class or file in a mapped repo by name. Call this when a "
        "stack frame gives you a class (e.g. com.foo.BarService or "
        "BarService.java) and you need its repo path before read_file.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "Class name, fully-qualified class, or filename"},
                "repo": repo_prop,
            },
            "required": ["name"],
        },
    )
    async def find_file(args):
        proj_enc, err = _enc(args)
        if err:
            return err
        raw = (args.get("name") or "").strip()
        fname = raw.rsplit("/", 1)[-1]
        if "." in fname and not fname.endswith(_FILE_EXTS):
            fname = fname.rsplit(".", 1)[-1]          # dotted FQN -> class name
        stem = fname.rsplit(".", 1)[0] if fname.endswith(_FILE_EXTS) else fname
        try:
            hits = await gitlab.search_blobs(proj_enc, stem)
        except Exception as e:
            return _text(f"Error searching for {stem}: {e}")
        paths, loose = [], []
        for h in hits if isinstance(hits, list) else []:
            hp = h.get("path", "")
            if not hp:
                continue
            bucket = paths if stem.lower() in posixpath.basename(hp).lower() else loose
            if hp not in bucket:
                bucket.append(hp)
        out = (paths or loose)[:MAX_HITS]
        log.info("gitlab tool find_file repo=%s name=%s hits=%d",
                 args.get("repo") or primary_repo, raw, len(out))
        if not out:
            return _text(f"No file matching '{raw}' found in this repo. "
                         "Try another mapped repo via the 'repo' argument.")
        return _text("\n".join(out))

    @tool(
        "search_code",
        "Search a mapped repo's code for a string (grep). Call this to find "
        "callers of a method, where an exception is thrown or caught, a config "
        f"key, or a constant from the error message. Returns up to {MAX_HITS} "
        "hits as path:line plus the matched fragment.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search string"},
                "repo": repo_prop,
            },
            "required": ["query"],
        },
    )
    async def search_code(args):
        proj_enc, err = _enc(args)
        if err:
            return err
        query = (args.get("query") or "").strip()
        try:
            hits = await gitlab.search_blobs(proj_enc, query)
        except Exception as e:
            return _text(f"Error searching for '{query}': {e}")
        parts = []
        for h in (hits if isinstance(hits, list) else [])[:MAX_HITS]:
            frag = (h.get("data") or "").strip()[:MAX_SNIPPET]
            parts.append(f"{h.get('path')}:{h.get('startline')}\n{frag}")
        log.info("gitlab tool search_code repo=%s query=%s hits=%d",
                 args.get("repo") or primary_repo, query, len(parts))
        if not parts:
            return _text(f"No matches for '{query}'.")
        return _text("\n---\n".join(parts))

    @tool(
        "blame",
        "Who last changed a line range of a file: commit, author, date per run "
        "of lines. Call this to find the change that introduced suspect code, "
        "then commit_diff on the sha to see what it changed.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repo-relative file path"},
                "start_line": {"type": "integer", "description": "First line, 1-based"},
                "end_line": {"type": "integer",
                             "description": f"Last line (max {MAX_BLAME_LINES} lines per call)"},
                "repo": repo_prop,
            },
            "required": ["path", "start_line"],
        },
    )
    async def blame(args):
        proj_enc, err = _enc(args)
        if err:
            return err
        path = (args.get("path") or "").lstrip("/")
        start = max(1, int(args.get("start_line") or 1))
        end = int(args.get("end_line") or start)
        end = min(max(end, start), start + MAX_BLAME_LINES - 1)
        try:
            data = await gitlab.blame_range(proj_enc, path, start, end)
        except Exception as e:
            return _text(f"Error blaming {path}: {e}")
        cur, parts = start, []
        for entry in data if isinstance(data, list) else []:
            commit = entry.get("commit") or {}
            n = len(entry.get("lines") or [])
            parts.append(
                f"lines {cur}-{cur + max(n, 1) - 1}: {(commit.get('id') or '')[:8]} "
                f"{(commit.get('committed_date') or '')[:10]} "
                f"{commit.get('author_name')} — {_subject(commit)}"
            )
            cur += n
        log.info("gitlab tool blame repo=%s path=%s %d-%d",
                 args.get("repo") or primary_repo, path, start, end)
        return _text("\n".join(parts) if parts else f"No blame data for {path}.")

    @tool(
        "commit_diff",
        "Full diff of one commit (all files). Call this after blame or "
        "recent_commits to judge whether a change introduced the bug.",
        {
            "type": "object",
            "properties": {
                "sha": {"type": "string", "description": "Commit sha (short or full)"},
                "repo": repo_prop,
            },
            "required": ["sha"],
        },
    )
    async def commit_diff(args):
        proj_enc, err = _enc(args)
        if err:
            return err
        sha = (args.get("sha") or "").strip()
        try:
            diffs = await gitlab.commit_diffs(proj_enc, sha)
        except Exception as e:
            return _text(f"Error fetching diff for {sha[:8]}: {e}")
        parts = []
        for d in diffs if isinstance(diffs, list) else []:
            body = (d.get("diff") or "").strip()
            if body:
                parts.append(f"--- {d.get('old_path')}\n+++ {d.get('new_path')}\n{body}")
        text = "\n\n".join(parts)
        if len(text) > MAX_DIFF:
            text = text[:MAX_DIFF] + "\n... (diff truncated)"
        log.info("gitlab tool commit_diff repo=%s sha=%s files=%d",
                 args.get("repo") or primary_repo, sha[:8], len(parts))
        return _text(text if text else f"No diff found for commit {sha[:8]}.")

    @tool(
        "recent_commits",
        f"Latest commits on {GITLAB_REF} in a mapped repo, optionally only "
        "those touching one file. Call this to answer 'what changed here "
        "lately' — then commit_diff on a suspicious sha.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "Optional repo-relative path to filter by"},
                "limit": {"type": "integer",
                          "description": f"How many commits (default 10, max {MAX_COMMITS})"},
                "repo": repo_prop,
            },
            "required": [],
        },
    )
    async def recent_commits(args):
        proj_enc, err = _enc(args)
        if err:
            return err
        path = (args.get("path") or "").lstrip("/") or None
        limit = min(int(args.get("limit") or 10), MAX_COMMITS)
        try:
            commits = await gitlab.recent_commits(proj_enc, path=path, limit=limit)
        except Exception as e:
            return _text(f"Error listing commits: {e}")
        parts = [
            f"{(c.get('id') or '')[:8]}  {(c.get('committed_date') or '')[:10]}  "
            f"{c.get('author_name')}  {_subject(c)}"
            for c in (commits if isinstance(commits, list) else [])
        ]
        log.info("gitlab tool recent_commits repo=%s path=%s n=%d",
                 args.get("repo") or primary_repo, path, len(parts))
        return _text("\n".join(parts) if parts else "No commits found.")

    tools = [read_file, find_file, search_code, blame, commit_diff, recent_commits]
    server = create_sdk_mcp_server(name="gitlab", version="1.0.0", tools=tools)
    allowed = [f"mcp__gitlab__{t.name}" for t in tools]
    return {"gitlab": server}, allowed
