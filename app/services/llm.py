"""LlmClient — a thin Anthropic Messages API wrapper (call + token cost).

Pure client: no DB, no GitLab, no prompt building. The analysis use-case that
caches results and enriches the prompt with source/blame lives in
app.sentry.analysis.AnalysisService.
"""
import ssl

import httpx

from app.config import (
    ENABLE_LLM, ANTHROPIC_API_KEY, ANTHROPIC_MODEL, ANTHROPIC_MAX_TOKENS,
    ANTHROPIC_PRICE_IN, ANTHROPIC_PRICE_OUT, USD_KGS_RATE,
    ANTHROPIC_SSL_INSECURE, ANTHROPIC_CA_BUNDLE, log,
)


def money(usd):
    """LLM cost in USD and Kyrgyz som, e.g. '$0.0087 · 0.78 сом'."""
    return f"${usd:.4f} · {usd * USD_KGS_RATE:.2f} сом"


class LlmClient:
    def __init__(self):
        self._client = None
        if ENABLE_LLM and ANTHROPIC_API_KEY:
            # external call — keeps trust_env (proxy) but needs MITM-tolerant TLS
            ctx = ssl.create_default_context()
            if ANTHROPIC_CA_BUNDLE:
                ctx.load_verify_locations(ANTHROPIC_CA_BUNDLE)
            ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
            if ANTHROPIC_SSL_INSECURE:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            self._client = httpx.AsyncClient(timeout=30, verify=ctx)

    @property
    def enabled(self):
        return self._client is not None

    async def complete(self, prompt: str):
        """Send one prompt. Returns (text, in_tokens, out_tokens, cost_usd), or None."""
        try:
            r = await self._client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": ANTHROPIC_MODEL,
                    "max_tokens": ANTHROPIC_MAX_TOKENS,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            usage = data.get("usage") or {}
            in_tok = usage.get("input_tokens", 0)
            out_tok = usage.get("output_tokens", 0)
            cost = in_tok / 1e6 * ANTHROPIC_PRICE_IN + out_tok / 1e6 * ANTHROPIC_PRICE_OUT
            text = "".join(
                b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
            ).strip()
            return text, in_tok, out_tok, cost
        except Exception as e:
            log.warning("LLM call failed: %s", e)
            return None

    async def aclose(self):
        if self._client is not None:
            await self._client.aclose()
