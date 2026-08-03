"""LlmClient — one-shot Claude call through the Claude Agent SDK (call + token cost).

First iteration of the SDK migration: same complete() contract as the old
Messages-API client, but the call runs through the SDK's bundled `claude`
binary, so auth can be an Anthropic API key OR a Claude subscription OAuth
token (`claude setup-token`). No tools yet — the MCP tool set comes next.

Pure client: no DB, no GitLab, no prompt building. The analysis use-case that
caches results and enriches the prompt with source/blame lives in
app.sentry.analysis.AnalysisService.
"""
import os
import asyncio

from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

from app.config import (
    ENABLE_LLM, ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN, LLM_AUTH_OK,
    ANTHROPIC_MODEL, ANTHROPIC_MAX_TOKENS, USD_KGS_RATE,
    ANTHROPIC_SSL_INSECURE, ANTHROPIC_CA_BUNDLE, AGENT_MAX_CONCURRENCY, log,
)


def money(usd):
    """LLM cost in USD and Kyrgyz som, e.g. '$0.0087 · 0.78 сом'."""
    return f"${usd:.4f} · {usd * USD_KGS_RATE:.2f} сом"


def _sdk_env():
    """Env for the SDK's `claude` subprocess: auth, proxy, and CA trust.

    Passed explicitly rather than relying on inheritance, so what the subprocess
    sees is exactly what we decided here.
    """
    env = {}
    if ANTHROPIC_API_KEY:
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

    @property
    def enabled(self):
        return self._enabled

    async def complete(self, prompt: str):
        """Send one prompt. Returns (text, in_tokens, out_tokens, cost_usd), or None.
        cost_usd is None with subscription OAuth (the SDK reports no dollar cost) —
        callers then show only the token counts."""
        if not self._enabled:
            return None
        options = ClaudeAgentOptions(
            model=ANTHROPIC_MODEL,
            max_turns=1,                          # plain completion — no tool loop yet
            tools=[],                             # no built-ins: no fs/bash/web access
            allowed_tools=[],
            permission_mode="bypassPermissions",  # headless; nothing to permit anyway
            setting_sources=[],                   # don't load CLAUDE.md/skills from disk
            env=_sdk_env(),
        )
        try:
            async with self._sem:
                text, in_tok, out_tok, cost = "", 0, 0, None
                async for message in query(prompt=prompt, options=options):
                    if isinstance(message, ResultMessage):
                        text = (message.result or "").strip()
                        usage = message.usage or {}
                        in_tok = usage.get("input_tokens", 0) or 0
                        out_tok = usage.get("output_tokens", 0) or 0
                        cost = message.total_cost_usd
        except Exception as e:
            log.warning("LLM call failed: %s", e)
            return None
        return text, in_tok, out_tok, cost

    async def aclose(self):
        pass                                      # no persistent client to close
