"""Model resolution — DeepSeek (primary), plus Qwen and GLM alternates.

All three are OpenAI-compatible chat/responses endpoints; there is no Bedrock /
Azure / Zen / Google / Claude-SDK / Codex path anymore (Less-Is-More: the agent
runs headless on a single provider, DeepSeek by default).
"""

from __future__ import annotations

import itertools
import logging
from typing import TYPE_CHECKING

import httpx
from openai import AsyncOpenAI
from pydantic_ai.models import Model
from pydantic_ai.models.openai import (
    OpenAIChatModel,
    OpenAIChatModelSettings,
    OpenAIResponsesModel,
)
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

if TYPE_CHECKING:
    from backend.config import Settings

logger = logging.getLogger(__name__)

# Round-robin counter for DeepSeek key rotation (rate-limit relief under
# concurrent swarms). Process-local is fine: each solver/coordinator resolves
# its model once at startup.
_deepseek_rr = itertools.count()


def _deepseek_keys(settings: Settings) -> list[str]:
    keys = [settings.deepseek_api_key] if settings.deepseek_api_key else []
    extra = getattr(settings, "deepseek_extra_api_keys", "") or ""
    keys += [k.strip() for k in extra.split(",") if k.strip()]
    return keys

# Default model spec — single DeepSeek model on the official API.
# (Pass --models explicitly for any other provider or a multi-model swarm.)
DEFAULT_MODELS: list[str] = [
    "deepseek/deepseek-chat",
]

# Context window sizes (tokens)
CONTEXT_WINDOWS: dict[str, int] = {
    "deepseek-chat": 128_000,
    "deepseek-reasoner": 128_000,
    "qwen-plus": 1_000_000,
    "glm-5.3": 200_000,
}

# Models that support vision (none of the supported providers currently do).
VISION_MODELS: set[str] = set()


def resolve_model(spec: str, settings: Settings) -> Model:
    """Resolve a 'provider/model_id' spec to a Pydantic AI Model."""
    provider = provider_from_spec(spec)
    model_id = model_id_from_spec(spec)
    match provider:
        case "glm":
            if not settings.glm_api_key:
                raise ValueError(
                    "GLM requires GLM_API_KEY (ZHIPU_API_KEY / ZHIPUAI_API_KEY / "
                    "BIGMODEL_API_KEY are also accepted)."
                )
            base_url = (settings.glm_base_url or "").strip().rstrip("/")
            if not base_url:
                base_url = "https://open.bigmodel.cn/api/coding/paas/v4"
            return OpenAIChatModel(
                model_id,
                provider=OpenAIProvider(
                    base_url=base_url,
                    api_key=settings.glm_api_key,
                ),
            )
        case "qwen":
            if not settings.qwen_api_key:
                raise ValueError(
                    "Qwen requires QWEN_API_KEY (DASHSCOPE_API_KEY and "
                    "BAILIAN_API_KEY are also accepted)."
                )
            if not settings.qwen_base_url:
                raise ValueError("Qwen requires a non-empty QWEN_BASE_URL.")

            raw_url = settings.qwen_base_url.rstrip("/")
            api_mode = settings.qwen_api_mode.strip().lower()
            if raw_url.endswith("/responses"):
                raw_url = raw_url.removesuffix("/responses")
                api_mode = "responses"
            elif raw_url.endswith("/chat/completions"):
                raw_url = raw_url.removesuffix("/chat/completions")
                api_mode = "chat"

            if api_mode == "responses":
                return OpenAIResponsesModel(
                    model_id,
                    provider=OpenAIProvider(
                        base_url=raw_url,
                        api_key=settings.qwen_api_key,
                    ),
                )
            if api_mode != "chat":
                raise ValueError("QWEN_API_MODE must be either 'responses' or 'chat'.")
            return OpenAIChatModel(
                model_id,
                provider=OpenAIProvider(
                    base_url=raw_url,
                    api_key=settings.qwen_api_key,
                ),
            )
        case "deepseek":
            keys = _deepseek_keys(settings)
            if not keys:
                raise ValueError("DeepSeek requires DEEPSEEK_API_KEY.")
            key_idx = next(_deepseek_rr) % len(keys)
            if len(keys) > 1:
                logger.info("DeepSeek model instance using key %d/%d", key_idx + 1, len(keys))
            # trust_env=False: connect directly, never through a local HTTP
            # proxy — the VPN tunnel can break proxy routing mid-run, and both
            # DeepSeek and the benchmark platform are directly reachable.
            # max_retries raised from the SDK default (2): under swarm
            # concurrency a 429 burst should back off and recover, not fail
            # the solver turn.
            client = AsyncOpenAI(
                api_key=keys[key_idx],
                base_url="https://api.deepseek.com/v1",
                max_retries=6,
                http_client=httpx.AsyncClient(
                    trust_env=False,
                    timeout=httpx.Timeout(connect=30.0, read=300.0, write=120.0, pool=30.0),
                ),
            )
            # DeepSeek's chat-completions API expects the output cap under
            # `max_tokens` (the OpenAI `max_completion_tokens` field would be
            # rejected/ignored). Pin the profile accordingly.
            return OpenAIChatModel(
                model_id,
                provider=OpenAIProvider(openai_client=client),
                profile={"openai_chat_supports_max_completion_tokens": False},
            )
        case _:
            raise ValueError(f"Unknown provider: {provider} (expected deepseek, qwen, or glm)")


def resolve_model_settings(spec: str) -> ModelSettings:
    """Get provider-specific model settings.

    The output cap must not exceed what each provider actually accepts:
    DeepSeek's chat API rejects `max_tokens` above 8192 (V3) / 65536
    (reasoner); GLM and Qwen cap at 32K. Values above the limit are not
    "safer" — some endpoints 400 on them.
    """
    provider = spec.split("/", 1)[0]
    model_id = model_id_from_spec(spec)
    match provider:
        case "deepseek":
            # OpenAI-compatible chat endpoint; server-side prompt caching is
            # automatic. Cap output tokens so a run can never reserve the
            # full context window as output.
            limit = 65536 if "reasoner" in model_id else 8192
            return OpenAIChatModelSettings(max_tokens=limit)
        case "glm" | "qwen":
            # GLM-5.x thinking models and qwen-plus top out at 32K output;
            # keep the limit explicit so long tool conversations are accepted.
            return OpenAIChatModelSettings(max_tokens=32_768)
        case _:
            return ModelSettings(max_tokens=128_000)


def model_id_from_spec(spec: str) -> str:
    """Extract just the model ID from a spec (strips effort suffix)."""
    parts = spec.split("/")
    return parts[1] if len(parts) >= 2 else spec


def provider_from_spec(spec: str) -> str:
    """Extract the provider from a spec."""
    return spec.split("/", 1)[0]


def effort_from_spec(spec: str) -> str | None:
    """Extract effort level from a spec like 'claude-sdk/claude-opus-4-6/max'."""
    parts = spec.split("/")
    if len(parts) >= 3 and parts[2] in ("low", "medium", "high", "max"):
        return parts[2]
    return None


def supports_vision(spec: str) -> bool:
    """Check if a model spec supports vision."""
    return model_id_from_spec(spec) in VISION_MODELS


def context_window(spec: str) -> int:
    """Get context window size for a model spec."""
    return CONTEXT_WINDOWS.get(model_id_from_spec(spec), 200_000)
