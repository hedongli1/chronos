"""Click CLI entry point."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import click
import httpx
from rich.console import Console

from backend.config import Settings
from backend.models import DEFAULT_MODELS

console = Console()


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("aiodocker").setLevel(logging.WARNING)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)-8s %(message)s", datefmt="%X"))
    logging.basicConfig(level=level, handlers=[handler], force=True)


async def _check_benchmark_vpn(base_url: str, health_check_url: str) -> bool:
    """Verify the VPN-only health endpoint before starting any benchmark run."""
    try:
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            resp = await client.get(health_check_url)
    except Exception as e:
        console.print(f"[bold red]VPN check failed:[/bold red] {e}")
        console.print("请先确认 VPN 已连接，然后重新启动 Agent。")
        return False

    if resp.status_code != 200:
        console.print(f"[bold red]VPN check failed:[/bold red] HTTP {resp.status_code}")
        console.print("请先确认 VPN 已连接，然后重新启动 Agent。")
        return False

    try:
        payload = resp.json()
    except Exception:
        console.print("[bold red]VPN check failed:[/bold red] response is not valid JSON")
        console.print("请先确认 VPN 已连接，然后重新启动 Agent。")
        return False

    if payload.get("status") != "ok":
        console.print(f"[bold red]VPN check failed:[/bold red] {payload}")
        console.print("请先确认 VPN 已连接，然后重新启动 Agent。")
        return False

    console.print(f"[green]VPN check passed[/green] via {base_url}")
    return True


async def _check_benchmark_task(settings: Settings) -> bool:
    """Fail fast when the benchmark task itself is not runnable (finished/invalid token)."""
    from backend.agents.coordinator_loop import _create_platform

    platform = _create_platform(settings)
    try:
        health_check = getattr(platform, "health_check", None)
        if not callable(health_check):
            return True
        total, solved = await health_check()
        console.print(f"[green]Benchmark task OK[/green] — {total} tasks, {solved} already completed")
        return True
    except Exception as e:
        console.print(f"[bold red]Benchmark task check failed:[/bold red] {e}")
        console.print("任务已结束或 token 无效。请到平台创建新的跑分任务并更新 BENCHMARK_TOKEN。")
        return False
    finally:
        try:
            await platform.close()
        except Exception:
            pass


@click.command()
@click.option("--platform", default="tsec", type=click.Choice(["tsec", "local"]), help="Platform mode: tsec (TSecBench) or local (offline challenge directory)")
@click.option("--platform-url", default=None, help="TSecBench base URL (overrides .env)")
@click.option("--platform-token", default=None, help="TSecBench API token (overrides .env)")
@click.option("--benchmark-base-url", default=None, help="Benchmark base URL (overrides .env)")
@click.option("--benchmark-token", default=None, help="Benchmark API token (overrides .env)")
@click.option("--benchmark-name", default=None, help="Benchmark run name (overrides .env)")
@click.option("--image", default="ctf-sandbox", help="Docker sandbox image name")
@click.option(
    "--executor",
    default="auto",
    type=click.Choice(["auto", "docker", "headless"]),
    help="Solver executor: 'headless' runs models without any Docker sandbox (tsec defaults to headless)",
)
@click.option("--no-exec", is_flag=True, help="Headless mode: disable the local run_python tool")
@click.option("--time-limit", default=None, type=int, help="Per-challenge wall-clock seconds (default: 1800)")
@click.option(
    "--total-budget-seconds",
    default=None,
    type=int,
    help="Total wall-clock budget for the whole run (e.g. 21600 for a 360-minute title). "
    "0/unset disables global-budget tracking (per-challenge limits only, old behavior).",
)
@click.option("--models", multiple=True, help="Model specs (default: deepseek/deepseek-chat)")
@click.option("--challenge", default=None, help="Solve a single challenge (directory, name, or unique_code)")
@click.option("--challenges-dir", default="challenges", help="Directory for challenge files")
@click.option("--no-submit", is_flag=True, help="Dry run — don't submit flags")
@click.option("--smoke-test", is_flag=True, help="Run a read-only TSecBench platform smoke test")
@click.option("--model-smoke-test", is_flag=True, help="Run a read-only LLM API connectivity smoke test")
@click.option(
    "--max-challenges",
    default=3,
    type=click.IntRange(min=1, max=3),
    help="Concurrent range-container window (hard cap 3)",
)
@click.option("--dashboard-port", default=8088, type=int, help="Local web dashboard port (0 = disable)")
@click.option("--dashboard-host", default="127.0.0.1", help="Local web dashboard host")
@click.option("-i", "--interactive", is_flag=True, help="Enable interactive REPL (type messages in terminal)")
@click.option("-v", "--verbose", is_flag=True, help="Verbose logging")
def main(
    platform: str,
    platform_url: str | None,
    platform_token: str | None,
    benchmark_base_url: str | None,
    benchmark_token: str | None,
    benchmark_name: str | None,
    image: str,
    executor: str,
    no_exec: bool,
    time_limit: int | None,
    total_budget_seconds: int | None,
    models: tuple[str, ...],
    challenge: str | None,
    challenges_dir: str,
    no_submit: bool,
    smoke_test: bool,
    model_smoke_test: bool,
    max_challenges: int,
    dashboard_port: int,
    dashboard_host: str,
    interactive: bool,
    verbose: bool,
) -> None:
    """Offensive-security agent — TSecBench multi-model solver swarm with interactive REPL support.

    Run without --challenge to start the full coordinator (Ctrl+C to stop).
    Use -i/--interactive to get an interactive REPL in the terminal.
    """
    _setup_logging(verbose)

    settings = Settings(sandbox_image=image)
    settings.executor_mode = executor
    if no_exec:
        settings.allow_local_exec = False
    if platform_url:
        settings.platform_url = platform_url
    if platform_token:
        settings.platform_token = platform_token
    if benchmark_base_url:
        settings.benchmark_url = benchmark_base_url
    if benchmark_token:
        settings.benchmark_token = benchmark_token
    if benchmark_name:
        settings.benchmark_name = benchmark_name
    settings.max_concurrent_challenges = max_challenges
    settings.dashboard_port = dashboard_port
    settings.dashboard_host = dashboard_host
    settings.platform_mode = platform

    from backend.agents.swarm import _resolve_executor_mode

    executor_mode = _resolve_executor_mode(settings)
    if time_limit is not None:
        settings.challenge_time_limit_seconds = time_limit
    if total_budget_seconds is not None:
        settings.total_benchmark_budget_seconds = total_budget_seconds

    if models:
        model_specs = list(models)
    elif settings.deepseek_api_key:
        # Primary setup: single DeepSeek model on the official API.
        model_specs = ["deepseek/deepseek-chat"]
    else:
        model_specs = []
        if settings.qwen_api_key:
            model_specs.append("qwen/qwen-plus")
        if settings.glm_api_key:
            model_specs.append(f"glm/{settings.glm_model_id}")
        if not model_specs:
            model_specs = list(DEFAULT_MODELS)

    console.print("[bold]Offensive-Security Agent — TSecBench[/bold]")
    console.print(f"  Platform mode: {platform}")
    console.print(f"  Platform URL: {settings.platform_url}" if platform != "local" else "  Platform URL: local filesystem")
    console.print(f"  Benchmark: {settings.benchmark_url}")
    console.print(f"  Models: {', '.join(model_specs)}")
    console.print(f"  Executor: {executor_mode}" + (" (no Docker sandbox; http/tcp/run_python tools)" if executor_mode == "headless" else ""))
    if executor_mode == "headless" and not settings.allow_local_exec:
        console.print("  Local exec: disabled (--no-exec)")
    console.print(f"  Image: {settings.sandbox_image}" if executor_mode == "docker" else "  Image: (unused in headless mode)")
    console.print(f"  Max challenges: {max_challenges}")
    console.print(f"  Per-challenge time limit: {settings.challenge_time_limit_seconds}s")
    if settings.total_benchmark_budget_seconds > 0:
        console.print(
            f"  Total benchmark budget: {settings.total_benchmark_budget_seconds}s "
            f"(new challenges stop spawning with <{settings.min_remaining_seconds_to_spawn}s left)"
        )
    else:
        console.print("  Total benchmark budget: untracked (per-challenge limits only)")
    console.print(f"  Dashboard: {dashboard_host}:{dashboard_port}" if dashboard_port > 0 else "  Dashboard: disabled")
    if settings.glm_api_key:
        console.print(f"  GLM API Key: configured ({settings.glm_model_id} @ {settings.glm_base_url})")
    if settings.deepseek_api_key:
        console.print("  DeepSeek API Key: configured")
    if settings.qwen_api_key:
        console.print("  Qwen API Key: configured")
    console.print()

    if model_smoke_test:
        asyncio.run(_run_model_smoke_test(settings, model_specs))
        return

    if platform == "tsec" and not smoke_test and not asyncio.run(
        _check_benchmark_vpn(settings.benchmark_url, settings.vpn_health_check_url)
    ):
        sys.exit(1)

    if platform == "tsec" and not smoke_test and not asyncio.run(_check_benchmark_task(settings)):
        sys.exit(1)

    if smoke_test:
        asyncio.run(_run_tsec_smoke_test(settings))
    elif challenge:
        asyncio.run(_run_single(settings, challenge, model_specs, no_submit, max_challenges, challenges_dir))
    else:
        asyncio.run(
            _run_coordinator(
                settings,
                model_specs,
                challenges_dir,
                no_submit,
                interactive,
            )
        )


async def _run_single(
    settings: Settings,
    challenge_ref: str,
    model_specs: list[str],
    no_submit: bool,
    max_challenges: int,
    challenges_dir: str = "challenges",
) -> None:
    """Run a single challenge with a swarm."""
    from backend.agents.coordinator_loop import _create_platform
    from backend.agents.swarm import ChallengeSwarm
    from backend.cost_tracker import CostTracker
    from backend.prompts import ChallengeMeta

    platform = _create_platform(settings)
    cost_tracker = CostTracker()

    challenge_path = Path(challenge_ref)
    meta_path = challenge_path / "metadata.yml"

    if meta_path.exists():
        meta = ChallengeMeta.from_yaml(meta_path)
        console.print(f"[bold]Challenge:[/bold] {meta.name} ({meta.category}, {meta.value} pts)")
    else:
        # Treat the argument as a challenge name / unique_code on the platform.
        challenges = await platform.fetch_all_challenges()
        ch_data = next(
            (
                c for c in challenges
                if c.get("name") == challenge_ref or c.get("unique_code") == challenge_ref
            ),
            None,
        )
        if not ch_data:
            console.print(f"[red]Challenge not found on platform: {challenge_ref}[/red]")
            await platform.close()
            sys.exit(1)
        pulled_dir = await platform.pull_challenge(ch_data, str(Path.cwd() / challenges_dir))
        challenge_path = Path(pulled_dir)
        meta = ChallengeMeta.from_yaml(challenge_path / "metadata.yml")
        console.print(f"[bold]Challenge:[/bold] {meta.name} ({meta.category}, {meta.value} pts)")

    swarm = ChallengeSwarm(
        challenge_dir=str(challenge_path),
        meta=meta,
        platform=platform,
        cost_tracker=cost_tracker,
        settings=settings,
        model_specs=model_specs,
        no_submit=no_submit,
    )

    try:
        from backend.agents.swarm import _time_limit_for_challenge

        effective_limit = _time_limit_for_challenge(settings, meta)
        # asyncio.wait (unlike wait_for) never cancels the swarm on timeout, so
        # kill() records the cancel reason before solvers write finish events.
        run_task = asyncio.create_task(swarm.run(), name=f"swarm-run-{meta.name}")
        done, _ = await asyncio.wait({run_task}, timeout=effective_limit)
        if run_task in done:
            result = run_task.result()
        else:
            swarm.kill("wall_clock")
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)
            result = None
            console.print(
                f"\n[bold yellow]Time limit reached:[/bold yellow] "
                f"{effective_limit}s; challenge stopped."
            )
        from backend.solver_base import FLAG_FOUND
        if result and result.status == FLAG_FOUND:
            console.print(f"\n[bold green]FLAG FOUND:[/bold green] {result.flag}")
        else:
            console.print("\n[bold red]No flag found.[/bold red]")

        console.print("\n[bold]Cost Summary:[/bold]")
        for agent_name in cost_tracker.by_agent:
            console.print(f"  {agent_name}: {cost_tracker.format_usage(agent_name)}")
        console.print(f"  [bold]Total: ${cost_tracker.total_cost_usd:.2f}[/bold]")
    finally:
        await platform.close()


async def _run_coordinator(
    settings: Settings,
    model_specs: list[str],
    challenges_dir: str,
    no_submit: bool,
    interactive: bool = False,
) -> None:
    """Run the mechanical scheduler (continuous until all solved or Ctrl+C)."""
    label = "mechanical scheduler" + (" + REPL" if interactive else "")
    console.print(f"[bold]Starting {label} (Ctrl+C to stop)...[/bold]\n")

    from backend.agents.scheduler import run_scheduler

    results = await run_scheduler(
        settings=settings,
        model_specs=model_specs,
        challenges_root=challenges_dir,
        no_submit=no_submit,
        interactive=interactive,
    )

    console.print("\n[bold]Final Results:[/bold]")
    for challenge, data in results.get("results", {}).items():
        console.print(f"  {challenge}: {data.get('flag', 'no flag')}")
    console.print(f"\n[bold]Total cost: ${results.get('total_cost_usd', 0):.2f}[/bold]")


async def _run_tsec_smoke_test(settings: Settings) -> None:
    """Run a read-only TSecBench platform smoke test.

    Performs ONLY read operations: list challenges and solved state.
    Does NOT start containers, request hints, or submit flags.
    """
    from backend.agents.coordinator_loop import _create_platform

    console.print("[bold cyan]TSecBench 平台只读自检[/bold cyan]")
    console.print(f"  Benchmark URL: {settings.benchmark_url}")
    console.print(f"  Token        : {'configured' if (settings.benchmark_token or settings.platform_token) else '[yellow]not set[/yellow]'}")
    console.print()

    platform = _create_platform(settings)
    checks_passed = 0
    checks_total = 0

    console.print("[bold]1/2 题目列表（严格模式，错误不吞掉）[/bold]")
    checks_total += 1
    health_check = getattr(platform, "health_check", None)
    try:
        if not callable(health_check):
            raise RuntimeError("platform adapter does not support health_check")
        total, solved_count = await health_check()
        console.print(f"  [green][OK][/green] challenges - {total} tasks, {solved_count} completed")
        checks_passed += 1
    except Exception as e:
        console.print(f"  [red][FAIL][/red] challenges - {e}")

    console.print("[bold]2/2 已解状态[/bold]")
    checks_total += 1
    try:
        solved = await platform.fetch_solved_names()
        console.print(f"  [green][OK][/green] solved - {len(solved)} solved")
        checks_passed += 1
    except Exception as e:
        console.print(f"  [red][FAIL][/red] solved - {e}")

    console.print()
    console.print(f"[bold]自检结果: {checks_passed}/{checks_total} 通过[/bold]")
    if checks_passed == checks_total:
        console.print("[bold green]所有只读检查通过，平台连接正常。[/bold green]")
    else:
        console.print("[bold yellow]部分检查未通过，请检查 Token 配置和网络。[/bold yellow]")

    await platform.close()


async def _run_model_smoke_test(settings: Settings, model_specs: list[str]) -> None:
    """Run a read-only LLM API connectivity smoke test."""
    from backend.models import resolve_model, resolve_model_settings

    model_specs = [s.strip() for s in model_specs if s.strip()]
    console.print("[bold cyan]大模型 API 连通性自检[/bold cyan]")
    console.print(f"  DeepSeek API Key: {'configured' if settings.deepseek_api_key else '[yellow]not set[/yellow]'}")
    console.print(f"  Qwen API Key: {'configured' if settings.qwen_api_key else '[yellow]not set[/yellow]'}")
    console.print()

    results: list[tuple[str, str]] = []
    for spec in model_specs:
        console.print(f"[bold]Testing {spec}[/bold]")
        try:
            model = resolve_model(spec, settings)
            model_settings = resolve_model_settings(spec)
            from pydantic_ai import Agent
            from pydantic_ai.usage import UsageLimits
            agent = Agent(
                model=model,
                system_prompt="You are a connectivity test. Reply with exactly: pong",
                model_settings=model_settings,
                output_type=str,
            )
            result = await agent.run("ping", usage_limits=UsageLimits(request_limit=1))
            output = result.output if hasattr(result, "output") else str(result.data)
            output_line = output[:80]
            results.append((spec, f"ok: {output_line}"))
            console.print(f"  [green][OK][/green] {output_line}")
        except Exception as e:
            results.append((spec, f"error: {e}"))
            console.print(f"  [red][FAIL][/red] {e}")

    ok = sum(1 for _, v in results if v.startswith("ok:"))
    console.print()
    console.print(f"[bold]模型自检结果: {ok}/{len(results)} 通过[/bold]")
    if ok == len(results):
        console.print("[bold green]大模型 API Key 可正常调用。[/bold green]")
    else:
        console.print("[bold yellow]至少有一个模型 API 调用失败，请检查 Key 或模型配置。[/bold yellow]")


if __name__ == "__main__":
    main()
