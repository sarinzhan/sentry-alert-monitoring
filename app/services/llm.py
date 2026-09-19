"""LlmClient — Claude call through the Claude Agent SDK (call + token cost).

Runs through the SDK's bundled `claude` binary, so auth can be an Anthropic
API key OR a Claude subscription OAuth token (`claude setup-token`). Plain
completion by default; pass mcp_servers/allowed_tools (built by
app.services.gitlab_tools) to run an agentic tool loop instead — built-in
tools (fs/bash/web) stay off either way.

Pure client: no DB, no GitLab, no prompt building. The analysis use-case that
caches results and enriches the prompt with source/blame lives in
app.sentry.analysis.AnalysisService.
"""
import os
import time
import asyncio
from dataclasses import dataclass, field

from claude_agent_sdk import (
    query, ClaudeAgentOptions, ResultMessage, AssistantMessage, UserMessage,
    ToolUseBlock, ToolResultBlock, TextBlock, StreamEvent,
)

from app.config import (
    ENABLE_LLM, ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN, LLM_AUTH_OK,
    ANTHROPIC_MODEL, ANTHROPIC_MAX_TOKENS, USD_KGS_RATE,
    ANTHROPIC_SSL_INSECURE, ANTHROPIC_CA_BUNDLE, AGENT_MAX_CONCURRENCY,
    AGENT_MAX_TURNS, log,
)


def money(usd):
    """LLM cost in USD and Kyrgyz som, e.g. '$0.0087 · 0.78 сом'."""
    return f"${usd:.4f} · {usd * USD_KGS_RATE:.2f} сом"


# tool results are kept as short previews in the audit trail, not in full
RESULT_PREVIEW = 700


@dataclass
class LlmCall:
    """Everything one complete() call saw and produced. Persisted by
    LlmAuditRepo under a short id so the full exchange can be inspected later."""
    text: str = ""
    in_tokens: int = 0
    out_tokens: int = 0
    cost: float = None            # USD; None with subscription auth
    turns: int = 0
    duration_ms: int = 0
    model: str = ""
    auth: str = ""
    agentic: bool = False
    prompt: str = ""
    tools_offered: list = field(default_factory=list)
    tool_calls: list = field(default_factory=list)   # [{tool, args, result}]
    session_id: str = None        # SDK session — pass as resume= to continue it


def _emit(on_event, ev):
    """Fire a progress callback; a broken observer must never kill the call."""
    if on_event is None:
        return
    try:
        on_event(ev)
    except Exception as e:
        log.warning("llm on_event failed: %s", e)


def _result_preview(content):
    """Flatten a ToolResultBlock's content into a short one-line preview."""
    if isinstance(content, list):
        content = " ".join(b.get("text", "") for b in content
                           if isinstance(b, dict) and b.get("type") == "text")
    s = " ".join(str(content or "").split())
    return s[:RESULT_PREVIEW] + ("…" if len(s) > RESULT_PREVIEW else "")


def auth_mode():
    """'api-key' | 'subscription' | None — which credential the SDK subprocess
    uses. API key wins when both are set (matches the SDK's own precedence)."""
    if ANTHROPIC_API_KEY:
        return "api-key"
    if CLAUDE_CODE_OAUTH_TOKEN:
        return "subscription"
    return None


def _token_kind(token):
    """'subscription' for a Claude OAuth token (sk-ant-oat…), else 'api-key'."""
    return "subscription" if "-oat" in (token or "") else "api-key"


def _sdk_env(auth_token=None):
    """Env for the SDK's `claude` subprocess: auth, proxy, and CA trust.

    Passed explicitly rather than relying on inheritance, so what the subprocess
    sees is exactly what we decided here. auth_token (a user's personal Claude
    token — OAuth or API key) replaces the shared credential for this call.
    """
    env = {}
    if auth_token:
        if _token_kind(auth_token) == "subscription":
            env["CLAUDE_CODE_OAUTH_TOKEN"] = auth_token
        else:
            env["ANTHROPIC_API_KEY"] = auth_token
    elif ANTHROPIC_API_KEY:
        env["ANTHROPIC_API_KEY"] = ANTHROPIC_API_KEY
    elif CLAUDE_CODE_OAUTH_TOKEN:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = CLAUDE_CODE_OAUTH_TOKEN
    env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(ANTHROPIC_MAX_TOKENS)
    # api.anthropic.com is only reachable through the corporate proxy
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
              "http_proxy", "https_proxy", "no_proxy"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    # the proxy MITMs TLS: trust the corporate CA, or (last resort) skip verify
    if ANTHROPIC_CA_BUNDLE:
        env["NODE_EXTRA_CA_CERTS"] = ANTHROPIC_CA_BUNDLE
        env["SSL_CERT_FILE"] = ANTHROPIC_CA_BUNDLE
    elif ANTHROPIC_SSL_INSECURE:
        env["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"
    return env


class LlmClient:
    def __init__(self):
        self._enabled = ENABLE_LLM and LLM_AUTH_OK
        # each call is one `claude` subprocess — serialize bursts instead of
        # forking dozens of CLIs at once
        self._sem = asyncio.Semaphore(max(1, AGENT_MAX_CONCURRENCY))
        if self._enabled:
            log.info("llm ready: auth=%s model=%s", auth_mode(), ANTHROPIC_MODEL)

    @property
    def enabled(self):
        return self._enabled

    async def complete(self, prompt: str, mcp_servers=None, allowed_tools=None,
                       on_event=None, auth_token=None, model=None, resume=None):
        """Send one prompt. Returns an LlmCall record, or None on failure.
        With mcp_servers set, runs an agentic loop (up to AGENT_MAX_TURNS turns)
        where the model may call those tools; otherwise a single completion.
        Every tool invocation the model makes is captured into the record
        (name, args, result preview) for the audit trail. cost is None with
        subscription auth: the flat-rate plan has no real per-call cost (the
        CLI still reports a hypothetical figure — dropped), so callers show
        only the token counts.

        on_event, if given, receives live progress dicts as the run unfolds —
        {'type': 'text'|'tool'|'tool_result', ...} — so a UI can show the
        model's reasoning like a chat (the web /api/explain/stream SSE).

        auth_token, if given, is the user's PERSONAL Claude token (OAuth or
        API key) — this call runs on it instead of the shared credential
        (the web daily-quota overflow path). model overrides ANTHROPIC_MODEL
        for this call (the web UI model selector).

        resume continues an earlier conversation: pass the session_id from a
        previous LlmCall and the model sees that whole exchange — its own
        prior tool calls and results included (the web chat). Session
        transcripts live under $HOME/.claude (the persistent volume)."""
        if not self._enabled:
            return None
        agentic = bool(mcp_servers)
        mode = _token_kind(auth_token) if auth_token else auth_mode()
        model = model or ANTHROPIC_MODEL
        options = ClaudeAgentOptions(
            model=model,
            max_turns=AGENT_MAX_TURNS if agentic else 1,
            tools=[],                             # no built-ins: no fs/bash/web access
            mcp_servers=mcp_servers or {},        # in-process MCP tools (gitlab)
            allowed_tools=list(allowed_tools or []),
            permission_mode="bypassPermissions",  # headless; nothing to permit anyway
            setting_sources=[],                   # don't load CLAUDE.md/skills from disk
            env=_sdk_env(auth_token),
            resume=resume,
            # raw stream deltas so a UI can type the text out live; only when
            # someone is actually listening — the events are per-token
            include_partial_messages=on_event is not None,
        )
        rec = LlmCall(model=model,
                      auth=(mode or "") + ("/personal" if auth_token else ""),
                      agentic=agentic, prompt=prompt,
                      tools_offered=list(allowed_tools or []))
        started = time.monotonic()
        calls_by_id = {}                          # tool_use id -> its trace entry
        run_in = run_out = 0                      # cumulative usage for live events
        try:
            async with self._sem:
                async for message in query(prompt=prompt, options=options):
                    if isinstance(message, StreamEvent):
                        # live text chunk of the block being generated; the
                        # complete block still follows as an AssistantMessage
                        # TextBlock, so consumers treat deltas as display-only
                        if message.parent_tool_use_id is None:
                            ev = message.event or {}
                            delta = ev.get("delta") or {}
                            if (ev.get("type") == "content_block_delta"
                                    and delta.get("type") == "text_delta"
                                    and delta.get("text")):
                                _emit(on_event, {"type": "delta",
                                                 "text": delta["text"]})
                    elif isinstance(message, AssistantMessage):
                        for block in message.content or []:
                            if isinstance(block, ToolUseBlock):
                                call = {"tool": block.name, "args": block.input}
                                rec.tool_calls.append(call)
                                calls_by_id[block.id] = call
                                _emit(on_event, {"type": "tool",
                                                 "tool": block.name,
                                                 "args": block.input})
                            elif isinstance(block, TextBlock) and (block.text or "").strip():
                                _emit(on_event, {"type": "text",
                                                 "text": block.text.strip()})
                        # per-message usage (when the SDK exposes it) -> live
                        # cumulative token counter for streaming UIs
                        usage = getattr(message, "usage", None)
                        if isinstance(usage, dict) and on_event is not None:
                            run_in += usage.get("input_tokens", 0) or 0
                            run_out += usage.get("output_tokens", 0) or 0
                            _emit(on_event, {"type": "usage",
                                             "in_tokens": run_in,
                                             "out_tokens": run_out})
                    elif isinstance(message, UserMessage):
                        content = message.content
                        for block in content if isinstance(content, list) else []:
                            if isinstance(block, ToolResultBlock):
                                call = calls_by_id.get(block.tool_use_id)
                                if call is not None:
                                    call["result"] = _result_preview(block.content)
                                    _emit(on_event, {"type": "tool_result",
                                                     "tool": call["tool"],
                                                     "result": call["result"]})
                    elif isinstance(message, ResultMessage):
                        rec.text = (message.result or "").strip()
                        usage = message.usage or {}
                        rec.in_tokens = usage.get("input_tokens", 0) or 0
                        rec.out_tokens = usage.get("output_tokens", 0) or 0
                        rec.cost = message.total_cost_usd
                        rec.turns = getattr(message, "num_turns", 0) or 0
                        rec.session_id = getattr(message, "session_id", None)
        except Exception as e:
            log.warning("LLM call failed: %s", e)
            return None
        rec.duration_ms = int((time.monotonic() - started) * 1000)
        if mode == "subscription":
            rec.cost = None
        log.info("llm done auth=%s agentic=%s turns=%d tools=%d in=%d out=%d cost=%s",
                 rec.auth, agentic, rec.turns, len(rec.tool_calls),
                 rec.in_tokens, rec.out_tokens,
                 f"${rec.cost:.4f}" if rec.cost is not None else "-")
        return rec

    async def aclose(self):
        pass                                      # no persistent client to close
