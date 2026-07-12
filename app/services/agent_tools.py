"""Custom agent tools — the Sentry and GitLab MCP servers the agent runs on.

Each @tool handler closes over the injected client and returns JSON text.
Results are size-capped so one fat tool result can't blow up the context.
Tool names surface to the model as mcp__sentry__<name> / mcp__gitlab__<name>.
"""
import json

from claude_agent_sdk import tool, create_sdk_mcp_server

RESULT_CAP = 15_000  # chars per tool result

SENTRY_TOOLS = [
    "mcp__sentry__list_projects",
    "mcp__sentry__search_issues",
    "mcp__sentry__search_user_issues",
    "mcp__sentry__search_events",
    "mcp__sentry__get_event",
    "mcp__sentry__issue_details",
    "mcp__sentry__issue_latest_event",
    "mcp__sentry__issue_events",
]

GITLAB_TOOLS = [
    "mcp__gitlab__find_file",
    "mcp__gitlab__read_file",
    "mcp__gitlab__list_tree",
    "mcp__gitlab__search_code",
    "mcp__gitlab__blame_line",
    "mcp__gitlab__commit_diff",
    "mcp__gitlab__recent_commits",
]


def _text(data):
    """Wrap any result as the MCP text content the SDK expects."""
    s = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, default=str)
    if len(s) > RESULT_CAP:
        s = s[:RESULT_CAP] + "\n… (truncated)"
    return {"content": [{"type": "text", "text": s}]}


def build_sentry_server(sentry):
    """In-process MCP server over a SentryQueryClient."""

    @tool("list_projects",
          "List all Sentry projects in the organization. Use this to resolve a "
          "human project name to the slug that search_issues needs. "
          "Returns [{id, slug, name}].",
          {})
    async def list_projects(args):
        return _text(await sentry.list_projects())

    @tool("search_issues",
          "Search one project's issues. query is a Sentry search string, e.g. "
          "'is:unresolved level:error' or 'user.email:x@y.com'. stats_period "
          "like '24h'/'14d'. Returns compact issue dicts with id/title/counts/permalink.",
          {
              "type": "object",
              "properties": {
                  "project": {"type": "string", "description": "project slug"},
                  "query": {"type": "string"},
                  "environment": {"type": "string"},
                  "stats_period": {"type": "string"},
                  "limit": {"type": "integer"},
              },
              "required": ["project"],
          })
    async def search_issues(args):
        return _text(await sentry.search_issues(
            args["project"],
            query=args.get("query") or None,
            environment=args.get("environment") or None,
            stats_period=args.get("stats_period") or None,
            limit=args.get("limit") or 25,
        ))

    @tool("search_user_issues",
          "Find issues affecting one user across ALL projects (query user.id:<id>). "
          "For an email pass user.email:<email> via search_issues instead, or set "
          "by='email' here. Returns issues with project slug.",
          {
              "type": "object",
              "properties": {
                  "user": {"type": "string", "description": "user id or email"},
                  "by": {"type": "string", "enum": ["id", "email", "username"]},
                  "stats_period": {"type": "string"},
                  "limit": {"type": "integer"},
              },
              "required": ["user"],
          })
    async def search_user_issues(args):
        field = {"id": "user.id", "email": "user.email",
                 "username": "user.username"}[args.get("by") or "id"]
        return _text(await sentry.search_org_issues(
            query=f"{field}:{args['user']}",
            stats_period=args.get("stats_period") or None,
            limit=args.get("limit") or 25,
        ))

    @tool("search_events",
          "Search raw Sentry EVENTS (not grouped issues) across all projects "
          "with a Discover query — the main investigation tool. Compose the "
          "query yourself:\n"
          "- 'msisdn:996555123456 \"Read timed out\"' — find one user's error;\n"
          "- 'trace:<trace_id>' — EVERY event of one request trace across all "
          "services, time-ordered: use it to reconstruct what was called and "
          "where the chain broke;\n"
          "- 'msisdn:<x>' with end=<error ISO time> — the user's earlier "
          "actions leading up to a failure.\n"
          "Returns compact rows with event_id/project/timestamp/title/trace. "
          "Follow up with get_event for full detail of one event.",
          {
              "type": "object",
              "properties": {
                  "query": {"type": "string",
                            "description": "Sentry event search string"},
                  "project": {"type": "string",
                              "description": "project slug; omit for all projects"},
                  "stats_period": {"type": "string",
                                   "description": "e.g. '24h', '14d' (ignored when start/end set)"},
                  "start": {"type": "string", "description": "ISO timestamp"},
                  "end": {"type": "string",
                          "description": "ISO timestamp; alone it implies a 7d window before it"},
                  "sort": {"type": "string",
                           "description": "'-timestamp' (default, newest first) or 'timestamp'"},
                  "extra_fields": {"type": "array", "items": {"type": "string"},
                                   "description": "extra tag names to include as columns"},
                  "limit": {"type": "integer"},
              },
              "required": ["query"],
          })
    async def search_events(args):
        rows = await sentry.search_events(
            args["query"],
            project=args.get("project") or None,
            stats_period=args.get("stats_period") or None,
            start=args.get("start") or None,
            end=args.get("end") or None,
            sort=args.get("sort") or "-timestamp",
            extra_fields=args.get("extra_fields") or None,
            limit=args.get("limit") or 20,
        )
        if rows is None:
            return _text(
                "Event search (Discover) is unavailable on this Sentry version "
                "or the query failed. Fall back: search_issues/search_user_issues "
                "with the same filters, then issue_events with query=... and "
                "full=true. Cross-service trace search is not possible then.")
        return _text(rows)

    @tool("get_event",
          "Full detail of ONE event: exception chain + stack frames, "
          "breadcrumbs (the service's last actions before the crash), tags "
          "(msisdn etc.), request url and trace_id. Fetch this after "
          "search_events, or for the exact event id from an alert.",
          {
              "type": "object",
              "properties": {
                  "project": {"type": "string", "description": "project slug"},
                  "event_id": {"type": "string"},
              },
              "required": ["project", "event_id"],
          })
    async def get_event(args):
        return _text(await sentry.get_event(args["project"], args["event_id"])
                     or f"event {args['event_id']} not found in {args['project']}")

    @tool("issue_details",
          "Metadata of one Sentry issue by numeric id: title, culprit, level, "
          "status, counts, firstSeen/lastSeen, permalink.",
          {
              "type": "object",
              "properties": {"issue_id": {"type": "string"}},
              "required": ["issue_id"],
          })
    async def issue_details(args):
        return _text(await sentry.issue_details(args["issue_id"])
                     or f"issue {args['issue_id']} not found")

    @tool("issue_latest_event",
          "The latest event of one issue with the full exception chain and stack "
          "frames (module/filename/lineno/in_app/context_line) — the starting "
          "point for reading the crashing code in GitLab.",
          {
              "type": "object",
              "properties": {"issue_id": {"type": "string"}},
              "required": ["issue_id"],
          })
    async def issue_latest_event(args):
        return _text(await sentry.issue_latest_event(args["issue_id"])
                     or f"no event data for issue {args['issue_id']}")

    @tool("issue_events",
          "Recent events of one issue: [{dateCreated, message, environment, user, "
          "tags}]. Good for seeing which users/environments are affected. Pass "
          "query (e.g. 'msisdn:996...') to filter to one user's occurrences; "
          "full=true adds each event's trace_id.",
          {
              "type": "object",
              "properties": {
                  "issue_id": {"type": "string"},
                  "limit": {"type": "integer"},
                  "query": {"type": "string",
                            "description": "Sentry search string to filter events"},
                  "full": {"type": "boolean",
                           "description": "include trace_id per event"},
              },
              "required": ["issue_id"],
          })
    async def issue_events(args):
        return _text(await sentry.issue_events(
            args["issue_id"], limit=args.get("limit") or 10,
            query=args.get("query") or None, full=bool(args.get("full"))))

    return create_sdk_mcp_server(name="sentry", version="1.0.0", tools=[
        list_projects, search_issues, search_user_issues, search_events,
        get_event, issue_details, issue_latest_event, issue_events,
    ])


def build_gitlab_server(gitlab):
    """In-process MCP server over a GitLabClient (allowlisted repos only)."""
    repos = sorted(set(gitlab.known_repos().values()))

    @tool("find_file",
          f"Locate a file in a repo by bare filename — maps a stack-frame class to "
          f"its repo path (e.g. Baz.java + module com.foo.bar.Baz -> "
          f"src/main/java/com/foo/bar/Baz.java). Use this BEFORE read_file when "
          f"you only have a stacktrace. Allowed repos: {repos}.",
          {
              "type": "object",
              "properties": {
                  "repo": {"type": "string"},
                  "filename": {"type": "string", "description": "bare filename, e.g. Baz.java"},
                  "module": {"type": "string",
                             "description": "full class/module from the stack frame, e.g. com.foo.bar.Baz"},
              },
              "required": ["repo", "filename"],
          })
    async def find_file(args):
        return _text(await gitlab.find_file(
            args["repo"], args["filename"], module=args.get("module")))

    @tool("read_file",
          f"Read a file (or line window) from a GitLab repo. Allowed repos: {repos}. "
          "Returns numbered lines; without start/end returns the first ~400 lines.",
          {
              "type": "object",
              "properties": {
                  "repo": {"type": "string"},
                  "path": {"type": "string"},
                  "ref": {"type": "string", "description": "branch/tag/sha (default main branch)"},
                  "start_line": {"type": "integer"},
                  "end_line": {"type": "integer"},
              },
              "required": ["repo", "path"],
          })
    async def read_file(args):
        return _text(await gitlab.read_file(
            args["repo"], args["path"], ref=args.get("ref"),
            start_line=args.get("start_line"), end_line=args.get("end_line")))

    @tool("list_tree",
          f"List a directory of a GitLab repo. Allowed repos: {repos}.",
          {
              "type": "object",
              "properties": {
                  "repo": {"type": "string"},
                  "path": {"type": "string"},
                  "ref": {"type": "string"},
              },
              "required": ["repo"],
          })
    async def list_tree(args):
        return _text(await gitlab.list_tree(
            args["repo"], path=args.get("path") or "", ref=args.get("ref")))

    @tool("search_code",
          f"Full-text search in GitLab code. Use for finding endpoints, classes, "
          f"or user-facing messages (e.g. an API route path or an error string "
          f"the user saw). OMIT repo to search ALL allowed repos at once: {repos}.",
          {
              "type": "object",
              "properties": {
                  "repo": {"type": "string",
                           "description": "one repo; omit to search all allowed repos"},
                  "query": {"type": "string"},
              },
              "required": ["query"],
          })
    async def search_code(args):
        if args.get("repo"):
            return _text(await gitlab.search_code(args["repo"], args["query"]))
        return _text(await gitlab.search_code_all(args["query"]))

    @tool("blame_line",
          "Who last changed one line of a file: author, commit sha, subject, date.",
          {
              "type": "object",
              "properties": {
                  "repo": {"type": "string"},
                  "path": {"type": "string"},
                  "line": {"type": "integer"},
              },
              "required": ["repo", "path", "line"],
          })
    async def blame_line(args):
        return _text(await gitlab.blame_line(args["repo"], args["path"], args["line"]))

    @tool("commit_diff",
          "The diff of one commit (optionally only the hunks touching one file). "
          "Use with blame_line's sha to see the change that introduced a bug.",
          {
              "type": "object",
              "properties": {
                  "repo": {"type": "string"},
                  "sha": {"type": "string"},
                  "path": {"type": "string"},
              },
              "required": ["repo", "sha"],
          })
    async def commit_diff(args):
        return _text(await gitlab.commit_diff(
            args["repo"], args["sha"], path=args.get("path")))

    @tool("recent_commits",
          "Recent commits of a repo, optionally only those touching one path. "
          "Use to check whether something changed recently around a failure "
          "when you don't have an exact line for blame yet.",
          {
              "type": "object",
              "properties": {
                  "repo": {"type": "string"},
                  "path": {"type": "string", "description": "file or directory to filter by"},
                  "limit": {"type": "integer"},
              },
              "required": ["repo"],
          })
    async def recent_commits(args):
        return _text(await gitlab.recent_commits(
            args["repo"], path=args.get("path"), limit=args.get("limit") or 20))

    return create_sdk_mcp_server(name="gitlab", version="1.0.0", tools=[
        find_file, read_file, list_tree, search_code, blame_line,
        commit_diff, recent_commits,
    ])
