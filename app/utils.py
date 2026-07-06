"""Small helpers shared across modules."""
import html
import hashlib


def esc(s) -> str:
    """HTML-escape a value for Telegram's HTML parse mode; empty for None/''."""
    return html.escape(str(s), quote=False) if s not in (None, "") else ""


def short_id(issue_id) -> str:
    """Stable short id shown in the message and used by /status and /ai."""
    return hashlib.sha1(str(issue_id).encode()).hexdigest()[:6]
