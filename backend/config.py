"""Pydantic Settings — runtime configuration from .env file + environment variables."""

from __future__ import annotations

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Platform (TSecBench).
    platform_mode: str = Field(default="tsec", validation_alias=AliasChoices("PLATFORM_MODE", "TSECBENCH_PLATFORM_MODE"))
    platform_url: str = Field(
        default="https://tsecbench.zc.tencent.com",
        validation_alias=AliasChoices("PLATFORM_URL", "TSECBENCH_URL", "BENCHMARK_BASE_URL"),
    )
    platform_token: str = Field(
        default="",
        validation_alias=AliasChoices("PLATFORM_TOKEN", "TSECBENCH_TOKEN", "BENCHMARK_TOKEN"),
    )

    # Model API keys — DeepSeek is primary; Qwen and GLM are optional alternates.
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
            "QWEN_API_KEY", "DASHSCOPE_API_KEY", "BAILIAN_API_KEY", "TSECBENCH_QWEN_API_KEY",
        ),
    )
    qwen_base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        validation_alias=AliasChoices("QWEN_BASE_URL", "DASHSCOPE_BASE_URL", "BAILIAN_BASE_URL"),
    )
    qwen_api_mode: str = Field(
        default="responses",
        validation_alias=AliasChoices("QWEN_API_MODE", "DASHSCOPE_API_MODE"),
    )
    glm_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("GLM_API_KEY", "ZHIPU_API_KEY", "ZHIPUAI_API_KEY", "BIGMODEL_API_KEY"),
    )
    glm_base_url: str = Field(
        # Coding-plan endpoint by default; switch to
        # https://open.bigmodel.cn/api/paas/v4 for pay-as-you-go keys.
        default="https://open.bigmodel.cn/api/coding/paas/v4",
        validation_alias=AliasChoices("GLM_BASE_URL", "ZHIPU_BASE_URL", "BIGMODEL_BASE_URL"),
    )
    glm_model_id: str = Field(default="glm-5.3", validation_alias=AliasChoices("GLM_MODEL_ID"))

    # Benchmark integration (TSecBench) — base URL and token for the scoring run.
    benchmark_name: str = Field(default="", validation_alias=AliasChoices("TSECBENCH_NAME", "BENCHMARK_NAME"))
    benchmark_url: str = Field(
        default="https://tsecbench.zc.tencent.com",
        validation_alias=AliasChoices("TSECBENCH_BENCHMARK_URL", "BENCHMARK_URL", "BENCHMARK_BASE_URL"),
    )
    benchmark_token: str = Field(default="", validation_alias=AliasChoices("TSECBENCH_BENCHMARK_TOKEN", "BENCHMARK_TOKEN"))

    # Runtime.
    allow_local_exec: bool = Field(
        default=True,
        validation_alias=AliasChoices("ALLOW_LOCAL_EXEC", "TSECBENCH_ALLOW_LOCAL_EXEC"),
    )
    max_concurrent_challenges: int = Field(
        default=3, validation_alias=AliasChoices("MAX_CONCURRENT_CHALLENGES", "TSECBENCH_MAX_CONCURRENT_CHALLENGES"),
    )
    challenge_time_limit_seconds: int = Field(
        default=1800,
        validation_alias=AliasChoices("CHALLENGE_TIME_LIMIT_SECONDS", "TSECBENCH_CHALLENGE_TIME_LIMIT_SECONDS"),
    )
    # Total wall-clock budget for the whole benchmark run (e.g. Tsecbench v1's
    # published 360-minute window = 21600). 0 disables tracking entirely; when
    # set, the scheduler clamps each new challenge's time limit to what is left
    # of the run and stops opening new challenges once too little remains.
    total_benchmark_budget_seconds: int = Field(
        default=0, validation_alias=AliasChoices("TOTAL_BENCHMARK_BUDGET_SECONDS"),
    )
    # Below this many remaining seconds in the total budget, stop opening new
    # challenges (finish what is running instead).
    min_remaining_seconds_to_spawn: int = Field(
        default=300, validation_alias=AliasChoices("MIN_REMAINING_SECONDS_TO_SPAWN"),
    )
    # Delay between a solver's continuation turns (bump retry).
    solver_retry_delay_seconds: float = Field(
        default=2.0, validation_alias=AliasChoices("SOLVER_RETRY_DELAY_SECONDS"),
    )
    # When every unsolved task is deferred and nothing is running, auto-clear
    # the deferred list after this many idle seconds (unattended runs must not stall).
    auto_resume_idle_seconds: int = Field(
        default=240, validation_alias=AliasChoices("AUTO_RESUME_IDLE_SECONDS"),
    )
    # Multi-stage penetration (killchain) tasks get a longer wall-clock budget.
    killchain_time_limit_seconds: int = Field(
        default=2700, validation_alias=AliasChoices("KILLCHAIN_TIME_LIMIT_SECONDS"),
    )
    # Multi-flag tasks: after a partial accept, if no new flag is accepted
    # within this window, withdraw with the partial score instead of grinding.
    partial_stall_seconds: int = Field(
        default=600, validation_alias=AliasChoices("PARTIAL_STALL_SECONDS"),
    )
    # Per solver-run scratch dir root (run_python workspaces, trace logs live under logs/).
    state_dir: str = Field(
        default=".ctf-state", validation_alias=AliasChoices("CTF_STATE_DIR", "TSECBENCH_STATE_DIR"),
    )
    # VPN-only health endpoint probed before a run starts. Override only if the
    # platform changes the address.
    vpn_health_check_url: str = Field(
        default="http://10.0.100.58", validation_alias=AliasChoices("VPN_HEALTH_CHECK_URL"),
    )

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}
