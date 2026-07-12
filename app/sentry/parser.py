"""Normalize the different Sentry webhook shapes (issue / error / event_alert)
into one flat dict the rest of the pipeline consumes.
"""
from app.config import LLM_STACK_LIB_MAX

# Sentry issue lifecycle actions we treat as "an error is happening".
# None covers alert-rule / error payloads that have no 'action' field.
NOTIFY_ACTIONS = {None, "created", "triggered"}


def _first(*vals):
    for v in vals:
        if v:
            return v
    return None


def parse(resource: str, payload: dict):
    """Flatten a webhook payload. Returns None if there is nothing useful to send."""
    data = payload.get("data", {}) or {}
    action = payload.get("action")

    obj = data.get("issue") or data.get("error") or data.get("event") or {}
    if not obj:
        return None

    # stable issue id used as the debounce key
    issue_id = str(
        _first(
            obj.get("id") if resource == "issue" else None,
            obj.get("issue_id"),
            obj.get("groupID"),
            obj.get("group_id"),
            obj.get("id"),
        )
        or ""
    )
    if not issue_id:
        return None

    metadata = obj.get("metadata") or {}
    exc_type = metadata.get("type")
    exc_value = metadata.get("value")

    # event payloads carry the stacktrace; issue payloads usually don't
    # exc_chain: every exception in the chain (Caused by ...), most recent last
    exc_chain = []
    frames_struct = []                                # richer, for LLM + GitLab
    values = (obj.get("exception") or {}).get("values") or []
    for v in values:
        vt, vv = v.get("type"), v.get("value")
        if vt or vv:
            exc_chain.append((vt, str(vv)[:500] if vv else vv))
    if values:
        last = values[-1]                             # the thrown exception
        exc_type = exc_type or last.get("type")
        exc_value = exc_value or last.get("value")
        st = (last.get("stacktrace") or {}).get("frames") or []
        for f in reversed(st):                        # crash site first
            frames_struct.append({
                "function": f.get("function") or "?",
                "filename": f.get("filename"),
                "module": f.get("module"),
                "abs_path": _first(f.get("absPath"), f.get("abs_path")),
                "lineno": f.get("lineno"),
                "in_app": bool(f.get("inApp") if "inApp" in f else f.get("in_app")),
                "context_line": _first(f.get("context_line"), f.get("contextLine")),
            })

    def _fmt(e):
        where = e["filename"] or e["module"] or "?"
        tag = "" if e["in_app"] else " (lib)"
        base = f"{where}:{e['lineno']}" if e["lineno"] else where
        return f"{base} in {e['function']}{tag}"

    frames = [_fmt(e) for e in frames_struct[:5]]                 # short, for Telegram
    # for the LLM: the full in-app (project) trace, plus only the top few
    # library frames for crash-site context (lib frames are mostly noise)
    frames_full, lib_seen = [], 0
    for e in frames_struct:                                       # crash-site first
        if e["in_app"]:
            frames_full.append(_fmt(e))
        elif lib_seen < LLM_STACK_LIB_MAX:
            frames_full.append(_fmt(e))
            lib_seen += 1
    frames_full = frames_full[:60]                                # hard safety cap

    if exc_value:
        exc_value = str(exc_value)[:1000]

    project = obj.get("project")
    # keep the raw numeric project id for GitLab repo mapping (before name resolution)
    project_id = project.get("id") if isinstance(project, dict) else project
    if isinstance(project, dict):
        project = project.get("slug") or project.get("name")

    # flat tag dict — tags arrive as [key, value] pairs or {key, value} dicts
    tags = {}
    for t in (obj.get("tags") or []):
        if isinstance(t, (list, tuple)) and len(t) == 2 and t[0]:
            k, v = t
        elif isinstance(t, dict) and t.get("key"):
            k, v = t["key"], t.get("value")
        else:
            continue
        tags[str(k)] = str(v)[:200] if v is not None else None

    # affected-user identifier for the "critical by users" rule
    u = obj.get("user") or {}
    usr = _first(u.get("id"), u.get("email"), u.get("username"), u.get("ip_address"),
                 tags.get("user"))
    usr = str(usr)[:200] if usr else None

    return {
        "issue_id": issue_id,
        "usr": usr,
        "tags": tags,
        "msisdn": tags.get("msisdn"),
        "trace_id": (((obj.get("contexts") or {}).get("trace")) or {}).get("trace_id"),
        "timestamp": _first(obj.get("datetime"), obj.get("timestamp")),
        "event_id": _first(obj.get("event_id"), obj.get("eventID")),
        "action": action,
        "title": obj.get("title") or exc_type or "Sentry event",
        "culprit": obj.get("culprit"),
        "level": obj.get("level"),
        "environment": obj.get("environment"),
        "type": exc_type,
        "value": exc_value,
        "count": obj.get("count"),
        "user_count": _first(obj.get("userCount"), obj.get("user_count")),
        "url": _first(obj.get("permalink"), obj.get("web_url"), obj.get("url")),
        "frames": frames,
        "frames_full": frames_full,
        "frames_struct": frames_struct,
        "exc_chain": exc_chain,
        "project": project,
        "project_id": project_id,
    }
