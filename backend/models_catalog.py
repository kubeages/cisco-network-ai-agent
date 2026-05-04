"""Model catalog - dynamic discovery and static pricing for LLM providers."""

from typing import Any, Dict, List, Optional
import httpx
import structlog

logger = structlog.get_logger()

# Default base URLs per provider
DEFAULT_BASE_URLS: Dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "ollama": "http://localhost:11434/v1",
}

# Known context windows (tokens)
KNOWN_CONTEXT: Dict[str, int] = {
    # OpenAI
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4.1": 1_047_576,
    "gpt-4.1-mini": 1_047_576,
    "o1": 200_000,
    "o3-mini": 200_000,
    # Anthropic
    "claude-opus-4-20250514": 200_000,
    "claude-opus-4": 200_000,
    "claude-sonnet-4-20250514": 200_000,
    "claude-sonnet-4": 200_000,
    "claude-haiku-4-20250514": 200_000,
    "claude-haiku-4": 200_000,
}

# Pricing per 1M tokens (input / output) in USD
KNOWN_PRICING: Dict[str, Dict[str, float]] = {
    # OpenAI
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4-turbo": {"input": 10.00, "output": 30.00},
    "gpt-4.1": {"input": 2.00, "output": 8.00},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
    "o1": {"input": 15.00, "output": 60.00},
    "o3-mini": {"input": 1.10, "output": 4.40},
    # Anthropic
    "claude-opus-4-20250514": {"input": 15.00, "output": 75.00},
    "claude-sonnet-4-20250514": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-20250514": {"input": 0.80, "output": 4.00},
    "claude-opus-4": {"input": 15.00, "output": 75.00},
    "claude-sonnet-4": {"input": 3.00, "output": 15.00},
    "claude-haiku-4": {"input": 0.80, "output": 4.00},
}

# Well-known Anthropic models (no listing API available)
ANTHROPIC_MODELS = [
    {"id": "claude-opus-4-20250514", "name": "Claude Opus 4", "context": 200000},
    {"id": "claude-sonnet-4-20250514", "name": "Claude Sonnet 4", "context": 200000},
    {"id": "claude-haiku-4-20250514", "name": "Claude Haiku 4", "context": 200000},
]


def get_context_window(model_id: str) -> Optional[int]:
    """Look up context window for a model ID."""
    if model_id in KNOWN_CONTEXT:
        return KNOWN_CONTEXT[model_id]
    for known_id, ctx in KNOWN_CONTEXT.items():
        if model_id.startswith(known_id):
            return ctx
    return None


def enrich_with_pricing(model_id: str) -> Optional[Dict[str, float]]:
    """Look up pricing for a model ID."""
    if model_id in KNOWN_PRICING:
        return KNOWN_PRICING[model_id]
    for known_id, pricing in KNOWN_PRICING.items():
        if model_id.startswith(known_id):
            return pricing
    return None


async def discover_models(
    provider: str,
    base_url: Optional[str],
    api_key: Optional[str],
) -> List[Dict[str, Any]]:
    """Discover available models from a provider.

    For OpenAI/vLLM: queries GET /v1/models (or /models).
    For Anthropic: returns a static catalog (no listing API).
    """
    models: List[Dict[str, Any]] = []

    if provider == "anthropic":
        for m in ANTHROPIC_MODELS:
            entry = {**m}
            pricing = enrich_with_pricing(m["id"])
            if pricing:
                entry["pricing"] = pricing
            models.append(entry)
        return models

    # OpenAI-compatible providers (openai, vllm, ollama)
    if not base_url:
        base_url = DEFAULT_BASE_URLS.get(provider)
    if not base_url:
        return []

    headers: Dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        async with httpx.AsyncClient(timeout=15.0, verify=False) as client:
            resp = await client.get(f"{base_url}/models", headers=headers)
            if resp.status_code != 200:
                logger.warning(
                    f"model_discovery_failed provider={provider} status={resp.status_code}"
                )
                return []

            data = resp.json()
            for m in data.get("data", []):
                model_id = m.get("id", "")
                entry: Dict[str, Any] = {
                    "id": model_id,
                    "name": model_id,
                    "owned_by": m.get("owned_by", ""),
                }
                if ctx := m.get("max_model_len"):
                    entry["context"] = ctx
                elif ctx := get_context_window(model_id):
                    entry["context"] = ctx

                pricing = enrich_with_pricing(model_id)
                if pricing:
                    entry["pricing"] = pricing
                models.append(entry)

    except Exception as exc:
        logger.warning(f"model_discovery_error provider={provider} error={str(exc)[:200]}")

    return models
