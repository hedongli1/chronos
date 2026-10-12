"""Pydantic Settings — runtime configuration from .env file + environment variables."""

from __future__ import annotations

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Platform: tsec (TSecBench) or local (offline challenge directory replay)
    platform_mode: str = Field(default="tsec", validation_alias=AliasChoices("PLATFORM_MODE", "TSECBENCH_PLATFORM_MODE"))
    platform_url: str = Field(
        default="https://tsecbench.zc.tencent.com",
        validation_alias=AliasChoices("PLATFORM_URL", "TSECBENCH_URL", "BENCHMARK_BASE_URL"),
    )
    platform_token: str = Field(
        default="",
        validation_alias=AliasChoices("PLATFORM_TOKEN", "TSECBENCH_TOKEN", "BENCHMARK_TOKEN"),
    )

    # API Keys
    anthropic_api_key: str = Field(default="", validation_alias=AliasChoices("ANTHROPIC_API_KEY", "TSECBENCH_ANTHROPIC_API_KEY"))
    openai_api_key: str = Field(default="", validation_alias=AliasChoices("OPENAI_API_KEY", "TSECBENCH_OPENAI_API_KEY"))
    deepseek_api_key: str = Field(default="", validation_alias=AliasChoices("DEEPSEEK_API_KEY", "TSECBENCH_DEEPSEEK_API_KEY"))
    # Extra DeepSeek keys (comma-separated). Model instances rotate over all
    # configured keys round-robin, multiplying the rate-limit quota under
    # concurrent swarms.
    deepseek_extra_api_keys: str = Field(
        default="",
        validation_alias=AliasChoices("DEEPSEEK_EXTRA_API_KEYS", "DEEPSEEK_API_KEYS"),
    )
    qwen_api_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "QWEN_API_KEY",
            "DASHSCOPE_API_KEY",
            "BAILIAN_API_KEY",
            "TSECBENCH_QWEN_API_KEY",
        ),
    )
    qwen_base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        validation_alias=AliasChoices(
            "QWEN_BASE_URL",
            "DASHSCOPE_BASE_URL",
            "BAILIAN_BASE_URL",
        ),
    )
    qwen_api_mode: str = Field(
        default="responses",
        validation_alias=AliasChoices("QWEN_API_MODE", "DASHSCOPE_API_MODE"),
    )
    gemini_api_key: str = Field(default="", validation_alias=AliasChoices("GEMINI_API_KEY", "TSECBENCH_GEMINI_API_KEY"))

    # Zhipu BigModel (GLM) — personal Coding Plan friendly.
    glm_api_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "GLM_API_KEY",
            "ZHIPU_API_KEY",
            "ZHIPUAI_API_KEY",
            "BIGMODEL_API_KEY",
        ),
    )
    glm_base_url: str = Field(
        # Coding-plan endpoint by default; switch to
        # https://open.bigmodel.cn/api/paas/v4 for pay-as-you-go keys.
        default="https://open.bigmodel.cn/api/coding/paas/v4",
        validation_alias=AliasChoices("GLM_BASE_URL", "ZHIPU_BASE_URL", "BIGMODEL_BASE_URL"),
    )
    glm_model_id: str = Field(default="glm-5.3", validation_alias=AliasChoices("GLM_MODEL_ID"))

    # Provider-specific (optional, for Bedrock/Azure/Zen fallback)
    aws_region: str = Field(default="us-east-1", validation_alias=AliasChoices("AWS_REGION", "TSECBENCH_AWS_REGION"))
    aws_bearer_token: str = Field(default="", validation_alias=AliasChoices("AWS_BEARER_TOKEN", "TSECBENCH_AWS_BEARER_TOKEN"))
    azure_openai_endpoint: str = Field(default="", validation_alias=AliasChoices("AZURE_OPENAI_ENDPOINT", "TSECBENCH_AZURE_OPENAI_ENDPOINT"))
    azure_openai_api_key: str = Field(default="", validation_alias=AliasChoices("AZURE_OPENAI_API_KEY", "TSECBENCH_AZURE_OPENAI_API_KEY"))
    opencode_zen_api_key: str = Field(default="", validation_alias=AliasChoices("OPENCODE_ZEN_API_KEY", "TSECBENCH_OPENCODE_ZEN_API_KEY"))

    # Benchmark integration (TSecBench)
    benchmark_name: str = Field(default="", validation_alias=AliasChoices("TSECBENCH_NAME", "BENCHMARK_NAME"))
    benchmark_url: str = Field(
        default="https://tsecbench.zc.tencent.com",
        validation_alias=AliasChoices("TSECBENCH_BENCHMARK_URL", "BENCHMARK_URL", "BENCHMARK_BASE_URL"),
    )
    benchmark_token: str = Field(default="", validation_alias=AliasChoices("TSECBENCH_BENCHMARK_TOKEN", "BENCHMARK_TOKEN"))

    # Infra
    executor_mode: str = Field(
        default="auto",
        validation_alias=AliasChoices("EXECUTOR_MODE", "TSECBENCH_EXECUTOR_MODE"),
    )
    allow_local_exec: bool = Field(
        default=True,
        validation_alias=AliasChoices("ALLOW_LOCAL_EXEC", "TSECBENCH_ALLOW_LOCAL_EXEC"),
    )
    sandbox_image: str = Field(default="ctf-sandbox", validation_alias=AliasChoices("SANDBOX_IMAGE", "TSECBENCH_SANDBOX_IMAGE"))
    max_concurrent_challenges: int = Field(default=3, validation_alias=AliasChoices("MAX_CONCURRENT_CHALLENGES", "TSECBENCH_MAX_CONCURRENT_CHALLENGES"))
    max_attempts_per_challenge: int = Field(default=3, validation_alias=AliasChoices("MAX_ATTEMPTS_PER_CHALLENGE", "TSECBENCH_MAX_ATTEMPTS_PER_CHALLENGE"))
    challenge_time_limit_seconds: int = Field(
        default=1800,
        validation_alias=AliasChoices(
            "CHALLENGE_TIME_LIMIT_SECONDS",
            "TSECBENCH_CHALLENGE_TIME_LIMIT_SECONDS",
        ),
    )
    # Total wall-clock budget for the whole benchmark run (e.g. Tsecbench v1's
    # published 360-minute window = 21600). 0 disables tracking entirely
    # (back-compat default — per-challenge limits behave exactly as before).
    # When set, the scheduler clamps each new challenge's time limit to what
    # is actually left of the run, and stops opening new challenges once the
    # remaining budget can no longer realistically finish one — otherwise the
    # system has no way to notice it is scheduled to run past the platform's
    # own cutoff and gets its containers force-closed mid-attempt for free.
    total_benchmark_budget_seconds: int = Field(
        default=0,
        validation_alias=AliasChoices("TOTAL_BENCHMARK_BUDGET_SECONDS"),
    )
    # Below this many remaining seconds in the total budget, stop opening new
    # challenges (finish what is running, but do not start something that has
    # no realistic chance to complete before the platform cuts the run off).
    min_remaining_seconds_to_spawn: int = Field(
        default=300,
        validation_alias=AliasChoices("MIN_REMAINING_SECONDS_TO_SPAWN"),
    )
    solver_retry_delay_seconds: float = Field(
        default=2.0,
        validation_alias=AliasChoices("SOLVER_RETRY_DELAY_SECONDS"),
    )
    # When every unsolved task is deferred and nothing is running, auto-clear
    # the deferred list after this many idle seconds (unattended benchmark runs
    # must not stall waiting for a manual /resume all).
    auto_resume_idle_seconds: int = Field(
        default=240,
        validation_alias=AliasChoices("AUTO_RESUME_IDLE_SECONDS"),
    )
    # Multi-stage penetration (killchain) tasks get a longer wall-clock budget.
    killchain_time_limit_seconds: int = Field(
        default=2700,
        validation_alias=AliasChoices("KILLCHAIN_TIME_LIMIT_SECONDS"),
    )
    # Multi-flag tasks: after a partial accept, if no new flag is accepted
    # within this window, withdraw with the partial score instead of grinding.
    partial_stall_seconds: int = Field(
        default=600,
        validation_alias=AliasChoices("PARTIAL_STALL_SECONDS"),
    )
    # Hint economics (headless solver): the platform hint deducts score, so it
    # stays gated. Normal gate: hint_min_steps AND hint_min_seconds of genuine
    # work. Stall-triggered early unlock: once hint_stall_min_steps AND
    # hint_stall_min_seconds have passed, allow the hint when doom signals show —
    # hint_stall_wrong_submissions platform rejections, or no new confirmed flag
    # and no new finding for hint_stall_steps steps.
    hint_min_steps: int = Field(default=20, validation_alias=AliasChoices("HINT_MIN_STEPS", "TSECBENCH_HINT_MIN_STEPS"))
    hint_min_seconds: int = Field(default=600, validation_alias=AliasChoices("HINT_MIN_SECONDS", "TSECBENCH_HINT_MIN_SECONDS"))
    hint_stall_steps: int = Field(default=30, validation_alias=AliasChoices("HINT_STALL_STEPS", "TSECBENCH_HINT_STALL_STEPS"))
    hint_stall_min_steps: int = Field(default=10, validation_alias=AliasChoices("HINT_STALL_MIN_STEPS", "TSECBENCH_HINT_STALL_MIN_STEPS"))
    hint_stall_min_seconds: int = Field(default=300, validation_alias=AliasChoices("HINT_STALL_MIN_SECONDS", "TSECBENCH_HINT_STALL_MIN_SECONDS"))
    hint_stall_wrong_submissions: int = Field(default=2, validation_alias=AliasChoices("HINT_STALL_WRONG_SUBMISSIONS", "TSECBENCH_HINT_STALL_WRONG_SUBMISSIONS"))
    container_memory_limit: str = Field(default="16g", validation_alias=AliasChoices("CONTAINER_MEMORY_LIMIT", "TSECBENCH_CONTAINER_MEMORY_LIMIT"))
    state_dir: str = Field(
        default=".ctf-state",
        validation_alias=AliasChoices("CTF_STATE_DIR", "TSECBENCH_STATE_DIR"),
    )
    dashboard_host: str = Field(default="127.0.0.1", validation_alias=AliasChoices("DASHBOARD_HOST", "TSECBENCH_DASHBOARD_HOST"))
    dashboard_port: int = Field(default=0, validation_alias=AliasChoices("DASHBOARD_PORT", "TSECBENCH_DASHBOARD_PORT"))

    # VPN-only health endpoint probed before any benchmark run starts. Kept
    # configurable (rather than hardcoded in cli.py) because the platform can
    # change which address it uses for this check without needing a code change.
    vpn_health_check_url: str = Field(
        default="http://10.0.100.58",
        validation_alias=AliasChoices("VPN_HEALTH_CHECK_URL"),
    )

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}
