"""Small helpers shared across modules."""
import html


def esc(s) -> str:
    """HTML-escape a value for Telegram's HTML parse mode; empty for None/''."""
    return html.escape(str(s), quote=False) if s not in (None, "") else ""
