"""Which request options each Claude model takes (2026-10-22).

Model ids come from env vars per feature (SSC_ASSISTANT_MODEL, SSC_CLAUDE_MODEL, DOC_SCAN_MODEL, ...), so a
cheaper model can be tried without a deploy. Some options are model-specific and a wrong one is a 400:

  * fallback_kwargs(model)  server-side refusal fallback; only the Opus / Sonnet 5.5 / Fable line offers it
  * thinking_off(model)     the lowest thinking setting for one-shot extraction calls: Sonnet 5.5 takes
                            'between_tools', Opus 5.5 and Fable only run adaptive (send nothing), the rest
                            take 'disabled' at effort high or below
  * agent_kwargs(model)     the Claude window's conversation calls: refusal fallback, plus "drop_block" on the
                            thinking-binding check (2026-10-22). The current models tie each replayed thinking
                            block to the exact tools + system prompt + history it was made with; adding a tool
                            for everyone (update_job) would otherwise make every older chat a 400 on accounts the
                            check enforces. With drop_block the API drops the stale blocks and the chat goes on.
"""
from __future__ import annotations

FALLBACK_MODELS = ("claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-fable-5")
ADAPTIVE_ONLY = ("claude-opus-5-5", "claude-fable-5-1", "claude-fable-5")
BINDING_MODELS = ("claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1", "claude-fable-5")
BINDING_BETA = "thinking-binding-controls-2026-08-01"


def fallback_kwargs(model: str) -> dict:
    if model in FALLBACK_MODELS:
        return {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    return {}


def thinking_off(model: str) -> dict:
    if model == "claude-sonnet-5-5":
        return {"thinking": {"type": "between_tools"}}
    if model in ADAPTIVE_ONLY:
        return {}
    return {"thinking": {"type": "disabled"}}


def agent_kwargs(model: str) -> dict:
    out = fallback_kwargs(model)
    if model in BINDING_MODELS:
        out["betas"] = out.get("betas", []) + [BINDING_BETA]
        out["thinking"] = {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
    return out
