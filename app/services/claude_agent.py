"""Shared Claude Agent SDK runner — the one place every LLM call goes through.

Wraps claude_agent_sdk.query(): builds the options (custom tools only, no
file-system/bash built-ins), plumbs auth + corporate-proxy env into the SDK
subprocess, iterates the message stream, and returns an AgentRunResult with
token/cost accounting. AnalysisService and AgentService both run on this.

Auth: ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN (subscription token from
`claude setup-token`). The SDK spawns a bundled `claude` binary, so TLS trust
for the MITMing corporate proxy is passed via NODE_EXTRA_CA_CERTS/SSL_CERT_FILE
instead of the httpx ssl-context trick the old clients used.
"""
import os
import asyncio
from dataclasses import dataclass

from claude_agent_sdk import (
    query, ClaudeAgentOptions, AssistantMessage, ResultMessage, ToolUseBlock,
)

from app.config import (
    ENABLE_LLM, ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN, ANTHROPIC_MODEL,
    ANTHROPIC_PRICE_IN, ANTHROPIC_PRICE_OUT, USD_KGS_RATE,
    ANTHROPIC_SSL_INSECURE, ANTHROPIC_CA_BUNDLE, AGENT_MAX_CONCURRENCY, log,
)


def money(usd):
    """LLM cost in USD and Kyrgyz som, e.g. '$0.0087 · 0.78 сом'."""
    return f"${usd:.4f} · {usd * USD_KGS_RATE:.2f} сом"


def auth_mode():
    """'api-key' | 'oauth' | None — which credential the SDK subprocess will use.
    API key wins when both are set (matches the SDK's own precedence)."""
    if ANTHROPIC_API_KEY:
        return "api-key"
    if CLAUDE_CODE_OAUTH_TOKEN:
        return "oauth"
    return None


def llm_ready():
    """True when the agent stack can actually run (feature flag + credential)."""
    return ENABLE_LLM and auth_mode() is not None


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


@dataclass
class AgentRunResult:
    """One agent run: final text plus accounting. cost may be an estimate —
    with subscription OAuth the SDK reports no dollar cost."""
    text: str
    in_tokens: int
    out_tokens: int
    cost: float
    cost_estimated: bool
    num_turns: int
    subtype: str          # "success" | "error_max_turns" | "error_max_budget_usd" | ...

    @property
    def ok(self):
        return self.subtype == "success" and bool(self.text)


# one subprocess per run — serialize bursts instead of forking dozens of CLIs
_sem = asyncio.Semaphore(max(1, AGENT_MAX_CONCURRENCY))


async def run_agent(prompt, *, system_prompt, mcp_servers, allowed_tools,
                    max_turns, max_budget_usd, model=ANTHROPIC_MODEL):
    """Run one agentic query restricted to the given custom tools.

    Returns an AgentRunResult, or None on failure (logged) — the same
    "never raise" contract the old LlmClient/AgentService had.
    """
    if not llm_ready():
        return None
    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=model,
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        mcp_servers=mcp_servers,
        tools=[],                          # no built-ins: no fs/bash/web access
        allowed_tools=list(allowed_tools),
        permission_mode="bypassPermissions",  # headless; only our tools exist
        setting_sources=[],                # don't load CLAUDE.md/skills from disk
        env=_sdk_env(),
    )
    text = ""
    in_tok = out_tok = 0
    cost = None
    num_turns = 0
    subtype = "error_during_execution"
    try:
        async with _sem:
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, ToolUseBlock):
                            log.info("agent tool call: %s(%s)", block.name, block.input)
                elif isinstance(message, ResultMessage):
                    subtype = message.subtype
                    num_turns = message.num_turns or 0
                    text = (message.result or "").strip()
                    usage = message.usage or {}
                    in_tok = usage.get("input_tokens", 0) or 0
                    out_tok = usage.get("output_tokens", 0) or 0
                    cost = message.total_cost_usd
    except Exception as e:
        log.warning("agent run failed: %s", e)
        return None

    estimated = not cost
    if estimated:  # subscription OAuth reports no cost — estimate from tokens
        cost = in_tok / 1e6 * ANTHROPIC_PRICE_IN + out_tok / 1e6 * ANTHROPIC_PRICE_OUT
    log.info("agent run done: subtype=%s turns=%d in=%d out=%d cost=$%.4f%s",
             subtype, num_turns, in_tok, out_tok, cost, " (est)" if estimated else "")
    return AgentRunResult(text=text, in_tokens=in_tok, out_tokens=out_tok,
                          cost=cost, cost_estimated=estimated,
                          num_turns=num_turns, subtype=subtype)
