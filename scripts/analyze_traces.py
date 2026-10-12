#!/usr/bin/env python3
"""Analyze solver traces in logs/ against challenges/*/metadata.yml.

Produces a Markdown report on stdout and writes logs/analysis-report.md.

Trace event schemas come from backend/tracing.py:
  start            {challenge, model, mode}
  tool_call        {tool, args, step}
  tool_result      {tool, result, step}
  model_response   {text, step, input_tokens, output_tokens}
  usage            {input_tokens, output_tokens, cache_read_tokens, cost_usd}
  bump             {insights}
  error            {error}
  flag_confirmed   {tool, flag, step}
  finish           {status, flag, confirmed, cost_usd}   status in
                   {flag_found, gave_up, cancelled, error, quota_error}
  stop             {step_count}
  hint_requested   {hint}
  target_lock_block {tool, host}
  no_progress_warning {step, gap}
  loop_break       {tool, step}
  findings_injected {step}

One trace file == one solver run. A file can contain several `finish` events
(the swarm bump loop calls run_until_done_or_gave_up repeatedly); the LAST
finish is the run's final status, and ANY `flag_found` finish means solved.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

LOG_GLOB = Path("logs")
META_GLOB = Path("challenges")
REPORT_PATH = "logs/analysis-report.md"

# Wall-clock budgets mirrored from backend/config.py defaults; used only to
# classify cancelled runs that died right at the limit.
TIME_LIMIT_S = 1800
KILLCHAIN_TIME_LIMIT_S = 2700

# Scenario classifier copied from backend/prompts.py::_guess_scenario
# (kept in sync manually; metadata `category` holds difficulty, not scenario).
_SCENARIO_WORDS = (
    ("渗透", "killchain"), ("横向", "killchain"), ("内网", "killchain"),
    ("kill chain", "killchain"), ("穿透", "killchain"), ("多级", "killchain"),
    ("跳板", "killchain"),
    ("免杀", "evasion"), ("绕过", "evasion"), ("规避", "evasion"),
    ("waf", "evasion"), ("edr", "evasion"),
    ("rce", "exploit"), ("poc", "exploit"), ("利用", "exploit"), ("exp_", "exploit"),
    ("k8s", "cloud"), ("kubernetes", "cloud"), ("容器逃逸", "cloud"),
    ("iam", "cloud"), ("云", "cloud"),
    ("pwn", "binary"), ("reverse", "binary"), ("逆向", "binary"),
    ("二进制", "binary"), ("stack", "binary"), ("heap", "binary"),
    ("overflow", "binary"), ("bof", "binary"),
    ("注入", "web"), ("sql", "web"), ("xss", "web"),
)


def guess_scenario(meta: dict) -> str:
    blob = " ".join(str(meta.get(k, "")) for k in ("unique_code", "name", "category", "description")).lower()
    for key in ("killchain", "evasion", "exploit", "cloud", "binary", "web"):
        if key in blob:
            return key
    for word, key in _SCENARIO_WORDS:
        if word in blob:
            return key
    return "web"


# Tool-result error signatures, based on backend/tools/*.py return strings.
_TOOL_ERROR_RES = [
    re.compile(r"^\w+ error:", re.I),                 # "http_request error:", "run_python error:"
    re.compile(r"^HTTP request error:", re.I),
    re.compile(r"^Fetch error:", re.I),
    re.compile(r"^Error (reading|writing) file:", re.I),
    re.compile(r"Traceback \(most recent call last\)"),
    re.compile(r"^BLOCKED \(target lock\)"),
    re.compile(r"^HINT DENIED"),
    re.compile(r"^(INCORRECT|DUPLICATE|COOLDOWN|SUBMIT LOCKED)"),
    re.compile(r"^HTTP 5\d\d"),                       # server errors from target
    re.compile(r"^(Notification failed|No coordinator connected)"),
    re.compile(r"^TOOL ARGUMENT ERROR"),
]


def is_tool_error(tool: str, result: str) -> bool:
    head = result[:400]
    return any(r.search(head) for r in _TOOL_ERROR_RES)


def norm_error(msg: str) -> str:
    """Normalize an error string for grouping: first line, strip volatile bits."""
    s = msg.strip().splitlines()[0] if msg.strip() else "(empty)"
    s = re.sub(r"\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?", "<ip>", s)   # IPs
    s = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<uuid>", s)
    s = re.sub(r"\b\d+\b", "<n>", s)                              # numbers
    s = re.sub(r"\s+", " ", s)
    return s[:160]


class Run:
    def __init__(self, path: str) -> None:
        self.path = path
        self.challenge = "?"
        self.model = "?"
        self.start_ts: float | None = None
        self.end_ts: float | None = None
        self.steps = 0
        self.finishes: list[dict] = []
        self.final_status = "unknown"
        self.solved = False
        self.flags_accepted: set[str] = set()
        self.flag_confirm_events = 0
        self.first_flag_ts: float | None = None
        self.last_flag_ts: float | None = None
        self.first_flag_step: int | None = None
        self.wrong_submits = 0
        self.duplicate_submits = 0
        self.cooldown_blocks = 0
        self.submit_locked = False
        self.hints_used = 0
        self.hints_denied = 0
        self.error_events: list[str] = []
        self.bumps = 0
        self.no_progress_warnings = 0
        self.loop_breaks = 0
        self.target_lock_blocks = 0
        self.tool_calls: Counter[str] = Counter()
        self.tool_errors: Counter[str] = Counter()
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        self.usage_cost = 0.0
        self.has_usage = False
        self.finish_cost = 0.0
        self.parse_errors = 0
        self.failure_class: str | None = None

    @property
    def duration(self) -> float:
        if self.start_ts is None or self.end_ts is None:
            return 0.0
        return max(0.0, self.end_ts - self.start_ts)

    @property
    def cost(self) -> float:
        return self.usage_cost if self.has_usage else self.finish_cost

    @property
    def time_to_solve(self) -> float | None:
        ts = self.last_flag_ts or self.first_flag_ts
        if self.solved and ts is not None and self.start_ts is not None:
            return ts - self.start_ts
        return None


def parse_run(path: str) -> Run:
    run = Run(path)
    pending_submit: dict[int, str] = {}  # step -> flag arg, for CORRECT matching
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                run.parse_errors += 1
                continue
            ts = e.get("ts")
            if isinstance(ts, (int, float)):
                run.end_ts = ts
                if run.start_ts is None:
                    run.start_ts = ts
            t = e.get("type")
            if t == "start":
                run.challenge = e.get("challenge", "?")
                run.model = e.get("model", "?")
            elif t == "tool_call":
                tool = e.get("tool", "?")
                run.tool_calls[tool] += 1
                step = e.get("step") or 0
                run.steps = max(run.steps, step)
                if tool == "submit_flag":
                    m = re.search(r'"flag":\s*"((?:[^"\\]|\\.)*)"', str(e.get("args", "")))
                    pending_submit[step] = m.group(1) if m else ""
            elif t == "tool_result":
                tool = e.get("tool", "?")
                result = str(e.get("result", ""))
                if is_tool_error(tool, result):
                    run.tool_errors[tool] += 1
                if tool == "submit_flag":
                    head = result[:60]
                    if head.startswith("CORRECT"):
                        flag = pending_submit.pop(e.get("step") or 0, "")
                        if flag:
                            run.flags_accepted.add(flag)
                    elif head.startswith("INCORRECT"):
                        run.wrong_submits += 1
                    elif head.startswith("DUPLICATE"):
                        run.duplicate_submits += 1
                    elif head.startswith("COOLDOWN"):
                        run.cooldown_blocks += 1
                    if "SUBMIT LOCKED" in result:
                        run.submit_locked = True
                elif tool == "request_hint" and result.startswith("HINT DENIED"):
                    run.hints_denied += 1
            elif t == "finish":
                run.finishes.append(e)
                run.finish_cost = max(run.finish_cost, float(e.get("cost_usd") or 0.0))
                if e.get("status") == "flag_found":
                    run.solved = True
            elif t == "flag_confirmed":
                run.flag_confirm_events += 1
                if run.first_flag_ts is None:
                    run.first_flag_ts = ts
                    run.first_flag_step = e.get("step")
                run.last_flag_ts = ts
                flag = e.get("flag")
                if flag:
                    run.flags_accepted.add(flag)
            elif t == "usage":
                run.has_usage = True
                run.input_tokens += int(e.get("input_tokens") or 0)
                run.output_tokens += int(e.get("output_tokens") or 0)
                run.cache_read_tokens += int(e.get("cache_read_tokens") or 0)
                run.usage_cost += float(e.get("cost_usd") or 0.0)
            elif t == "error":
                run.error_events.append(str(e.get("error", "")))
            elif t == "bump":
                run.bumps += 1
            elif t == "no_progress_warning":
                run.no_progress_warnings += 1
            elif t == "loop_break":
                run.loop_breaks += 1
            elif t == "target_lock_block":
                run.target_lock_blocks += 1
            elif t == "hint_requested":
                run.hints_used += 1
            elif t == "stop":
                sc = e.get("step_count")
                if isinstance(sc, int):
                    run.steps = max(run.steps, sc)
    if run.finishes:
        run.final_status = run.finishes[-1].get("status", "unknown")
    return run


def load_metadata() -> dict[str, dict]:
    metas: dict[str, dict] = {}
    for path in META_GLOB.glob("*/metadata.yml"):
        try:
            with open(path, encoding="utf-8") as fh:
                if yaml:
                    data = yaml.safe_load(fh) or {}
                else:
                    data = {}
                    for ln in fh:
                        m = re.match(r"^(\w+):\s*(.*)$", ln)
                        if m:
                            data[m.group(1)] = m.group(2)
        except Exception:
            continue
        name = str(data.get("name") or path.parent.name)
        data["flag_count"] = max(1, int(data.get("flag_count") or 1))
        data["scenario"] = guess_scenario(data)
        metas[name] = data
    return metas


def classify_failures(runs: list[Run], metas: dict[str, dict]) -> None:
    """Assign failure_class to every run that did not fully solve.

    Rules (first match wins), in priority order:
      1. circuit_breaker      — a submit_flag result contains "SUBMIT LOCKED"
                                (swarm tripped after 4 wrong submissions).
      2. error_crash          — final finish status is "error" (exception in
                                the solver loop; see `error` events).
      3. partial_stall_withdraw — multi-flag task, >=1 flag accepted, run was
                                cancelled afterwards (partial-stall watchdog
                                frees the slot after partial_stall_seconds).
      4. gave_up_stalled      — final status "gave_up" and no-progress
                                warnings were fired (>=40 steps without any
                                submission): the agent ground away and quit.
      5. gave_up              — final status "gave_up": the model ended its
                                turn without a confirmed flag and the bump
                                budget was exhausted.
      6. cancelled_sibling_win — cancelled while another run on the same
                                challenge confirmed a flag within +/-180s of
                                this run's end (normal swarm racing, not a
                                real failure).
      7. cancelled_time_limit — cancelled with duration >= 90% of the
                                wall-clock budget (1800s; 2700s for killchain).
      8. cancelled_external   — any other cancellation (orchestrator slot
                                reallocation, manual stop, shutdown).
    """
    win_times: dict[str, list[float]] = defaultdict(list)
    for r in runs:
        if r.solved and r.first_flag_ts is not None:
            win_times[r.challenge].append(r.first_flag_ts)
    for r in runs:
        if r.solved:
            continue
        if r.submit_locked:
            r.failure_class = "circuit_breaker"
        elif r.final_status == "error":
            r.failure_class = "error_crash"
        elif metas.get(r.challenge, {}).get("flag_count", 1) > 1 and r.flags_accepted:
            r.failure_class = "partial_stall_withdraw"
        elif r.final_status == "gave_up" and r.no_progress_warnings:
            r.failure_class = "gave_up_stalled"
        elif r.final_status == "gave_up":
            r.failure_class = "gave_up"
        elif r.final_status == "cancelled":
            if any(abs(wt - (r.end_ts or 0)) <= 180 for wt in win_times.get(r.challenge, [])):
                r.failure_class = "cancelled_sibling_win"
            else:
                budget = (KILLCHAIN_TIME_LIMIT_S
                          if metas.get(r.challenge, {}).get("scenario") == "killchain"
                          else TIME_LIMIT_S)
                if r.duration >= 0.9 * budget:
                    r.failure_class = "cancelled_time_limit"
                else:
                    r.failure_class = "cancelled_external"
        else:
            r.failure_class = f"other_{r.final_status}"


def fmt_dur(s: float | None) -> str:
    if s is None:
        return "-"
    s = int(s)
    return f"{s // 60}m{s % 60:02d}s"


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    metas = load_metadata()
    files = sorted(LOG_GLOB.glob("*.jsonl"))
    runs = [parse_run(p) for p in files]

    # A run can be cancelled between the platform's flag confirmation and the
    # solver's finish event (finish says cancelled, flag=None, but a
    # flag_confirmed / CORRECT submit result exists). Treat platform-side
    # confirmation of ALL expected flags as a solve.
    recovered = 0
    for r in runs:
        expected = metas.get(r.challenge, {}).get("flag_count", 1)
        if not r.solved and len(r.flags_accepted) >= expected:
            r.solved = True
            recovered += 1
    classify_failures(runs, metas)

    out: list[str] = []

    def emit(s: str = "") -> None:
        out.append(s)
        print(s)

    # ---- sanity ----
    n_trace_challenges = len({r.challenge for r in runs})
    emit("# Trace Analysis Report")
    emit()
    emit(f"- Trace files: **{len(files)}** | parsed runs: **{len(runs)}** "
         f"(JSON parse errors: {sum(r.parse_errors for r in runs)})")
    emit(f"- Finish events: **{sum(len(r.finishes) for r in runs)}** "
         f"(>1 per file = bump-loop restarts)")
    emit(f"- Challenges with traces: **{n_trace_challenges}** / {len(metas)} metadata dirs")
    emit(f"- Runs with `usage` events (token/cost data): "
         f"**{sum(1 for r in runs if r.has_usage)}** / {len(runs)}")
    emit(f"- Runs cancelled *after* the platform confirmed all flags "
         f"(finish=`cancelled` but counted as solves): **{recovered}**")
    missing_meta = sorted({r.challenge for r in runs} - set(metas))
    if missing_meta:
        emit(f"- WARNING: traces without metadata: {', '.join(missing_meta)}")
    untried = sorted(set(metas) - {r.challenge for r in runs})
    if untried:
        emit(f"- Challenges never attempted: {', '.join(untried)}")
    emit()

    # ---- overall ----
    solved_runs = [r for r in runs if r.solved]
    by_ch: dict[str, list[Run]] = defaultdict(list)
    for r in runs:
        by_ch[r.challenge].append(r)
    ch_solved = {c for c, rs in by_ch.items() if any(r.solved for r in rs)}
    ch_partial = {c for c, rs in by_ch.items()
                  if c not in ch_solved
                  and any(len(r.flags_accepted) >= 1 for r in rs)}
    emit("## Overall")
    emit()
    emit(f"- Run-level full solves: **{len(solved_runs)} / {len(runs)}** "
         f"({100 * len(solved_runs) / max(1, len(runs)):.1f}%)")
    emit(f"- Challenge-level: **{len(ch_solved)} solved**, {len(ch_partial)} partial, "
         f"**{n_trace_challenges - len(ch_solved) - len(ch_partial)} unsolved** "
         f"of {n_trace_challenges} attempted "
         f"(solve rate {100 * len(ch_solved) / max(1, n_trace_challenges):.1f}%)")
    emit(f"- Total cost (finish.cost_usd, all runs): "
         f"**${sum(r.cost for r in runs):.2f}**")
    emit()

    # ---- per-challenge table ----
    emit("## Per-Challenge Results")
    emit()
    emit("| challenge | difficulty | scenario | best flags | status | solved by | "
         "min steps | min time | runs | wrong subs | hints |")
    emit("|---|---|---|---|---|---|---|---|---|---|---|")
    for ch in sorted(by_ch):
        rs = by_ch[ch]
        meta = metas.get(ch, {})
        expected = meta.get("flag_count", 1)
        best = max((len(r.flags_accepted) for r in rs), default=0)
        wins = [r for r in rs if r.solved]
        status = "SOLVED" if wins else ("partial" if best >= 1 else "—")
        solvers = sorted({r.model for r in wins})
        min_steps = min((r.first_flag_step for r in wins if r.first_flag_step), default=None)
        min_time = min((r.time_to_solve for r in wins if r.time_to_solve is not None),
                       default=None)
        emit(f"| {ch} | {meta.get('category', '?')} | {meta.get('scenario', '?')} "
             f"| {best}/{expected} | {status} | {', '.join(solvers) or '-'} "
             f"| {min_steps if min_steps is not None else '-'} | {fmt_dur(min_time)} "
             f"| {len(rs)} | {sum(r.wrong_submits + r.duplicate_submits for r in rs)} "
             f"| {sum(r.hints_used for r in rs)} |")
    emit()

    # ---- per-category rates ----
    def rate_table(key: str, title: str) -> None:
        emit(f"## Solve Rate by {title}")
        emit()
        groups: dict[str, list[str]] = defaultdict(list)
        for ch in by_ch:
            groups[str(metas.get(ch, {}).get(key, "?"))].append(ch)
        emit(f"| {title} | challenges | solved | partial | unsolved | solve rate |")
        emit("|---|---|---|---|---|---|")
        for g in sorted(groups):
            chs = groups[g]
            s = sum(1 for c in chs if c in ch_solved)
            p = sum(1 for c in chs if c in ch_partial)
            u = len(chs) - s - p
            emit(f"| {g} | {len(chs)} | {s} | {p} | {u} | "
                 f"{100 * s / max(1, len(chs)):.0f}% |")
        emit()

    rate_table("category", "Difficulty (metadata category)")
    rate_table("scenario", "Scenario (inferred, prompts.py rules)")

    # ---- per-model ----
    emit("## Per-Model Stats")
    emit()
    emit("| model | runs | full solves | partial solves | solve rate | avg steps | "
         "avg tokens (in/out) | avg cost | avg duration |")
    emit("|---|---|---|---|---|---|---|---|---|")
    by_model: dict[str, list[Run]] = defaultdict(list)
    for r in runs:
        by_model[r.model].append(r)
    for m in sorted(by_model):
        rs = by_model[m]
        full = sum(1 for r in rs if r.solved)
        partial = sum(1 for r in rs if not r.solved and r.flags_accepted)
        with_usage = [r for r in rs if r.has_usage]
        avg_in = sum(r.input_tokens for r in with_usage) / max(1, len(with_usage))
        avg_out = sum(r.output_tokens for r in with_usage) / max(1, len(with_usage))
        avg_cost = sum(r.cost for r in rs) / max(1, len(rs))
        avg_dur = sum(r.duration for r in rs) / max(1, len(rs))
        avg_steps = sum(r.steps for r in rs) / max(1, len(rs))
        emit(f"| {m} | {len(rs)} | {full} | {partial} | "
             f"{100 * full / max(1, len(rs)):.1f}% | {avg_steps:.0f} | "
             f"{avg_in:,.0f} / {avg_out:,.0f} (n={len(with_usage)}) | "
             f"${avg_cost:.3f} | {fmt_dur(avg_dur)} |")
    emit()

    # ---- failure classification ----
    emit("## Failure Classification (runs that did NOT fully solve)")
    emit()
    emit("Rules (first match wins): `circuit_breaker` = SUBMIT LOCKED seen "
         "(4 wrong submissions); `error_crash` = final status error; "
         "`partial_stall_withdraw` = multi-flag task with >=1 accepted flag, then "
         "cancelled (stall watchdog); `gave_up_stalled` = gave_up after "
         "no-progress warnings (40+ steps without a submission); `gave_up` = "
         "model ended its turn flagless, bump budget exhausted; "
         "`cancelled_sibling_win` = cancelled within +/-180s of a sibling run's "
         "confirmed flag on the same challenge (normal racing, not a real loss); "
         "`cancelled_time_limit` = cancelled at >=90% of the 1800s/2700s "
         "(killchain) wall-clock budget; `cancelled_external` = orchestrator "
         "slot move / manual stop / shutdown.")
    emit()
    fails = [r for r in runs if not r.solved]
    cls = Counter(r.failure_class for r in fails)
    emit("| failure class | runs | % of failures |")
    emit("|---|---|---|")
    for k, v in cls.most_common():
        emit(f"| {k} | {v} | {100 * v / max(1, len(fails)):.1f}% |")
    emit()
    long_ext = [r for r in fails
                if r.failure_class == "cancelled_external" and 1080 <= r.duration < 1620]
    if long_ext:
        emit(f"Note: {len(long_ext)} `cancelled_external` runs lasted 18–27 min "
             f"({', '.join(sorted({r.challenge for r in long_ext}))}) — they likely hit a "
             "wall-clock limit configured below the 1800s config default (the "
             "coordinator kills the swarm via asyncio.wait_for at "
             "CHALLENGE_TIME_LIMIT_SECONDS, which the trace itself does not record).")
        emit()
    emit("Failure classes by challenge (unsolved/partial only):")
    emit()
    emit("| challenge | class counts |")
    emit("|---|---|")
    for ch in sorted(by_ch):
        if ch in ch_solved:
            continue
        c = Counter(r.failure_class for r in by_ch[ch] if not r.solved)
        emit(f"| {ch} | {', '.join(f'{k}x{n}' for k, n in c.most_common())} |")
    emit()

    # ---- tool stats ----
    emit("## Tool Usage")
    emit()
    total_calls: Counter[str] = Counter()
    total_errs: Counter[str] = Counter()
    for r in runs:
        total_calls.update(r.tool_calls)
        total_errs.update(r.tool_errors)
    emit("Error = result matching tool error signatures (see script: "
         "`X error:`, Traceback, BLOCKED, HINT DENIED, INCORRECT/DUPLICATE/"
         "COOLDOWN/SUBMIT LOCKED, HTTP 5xx, ...).")
    emit()
    emit("| tool | calls | errors | error rate |")
    emit("|---|---|---|---|")
    for tool, n in total_calls.most_common():
        errs = total_errs.get(tool, 0)
        emit(f"| {tool} | {n} | {errs} | {100 * errs / max(1, n):.1f}% |")
    emit()
    # dominant tool in failing runs
    emit("Dominant tool (most calls) within failing runs:")
    emit()
    dom: Counter[str] = Counter()
    for r in fails:
        if r.tool_calls:
            dom[r.tool_calls.most_common(1)[0][0]] += 1
    emit("| tool | failing runs dominated |")
    emit("|---|---|")
    for tool, n in dom.most_common():
        emit(f"| {tool} | {n} |")
    emit()

    # ---- hint analysis ----
    emit("## Hint Analysis")
    emit()
    hint_runs = [r for r in runs if r.hints_used > 0]
    denied = sum(r.hints_denied for r in runs)
    hs = sum(1 for r in hint_runs if r.solved)
    denied_runs = sum(1 for r in runs if r.hints_denied > 0)
    emit(f"- Runs that received a hint (`hint_requested` events): **{len(hint_runs)}**, "
         f"of which solved: **{hs}** ({100 * hs / max(1, len(hint_runs)):.0f}%)")
    emit(f"- Solve rate without any hint: "
         f"{100 * (len(solved_runs) - hs) / max(1, len(runs) - len(hint_runs)):.1f}% "
         f"({len(solved_runs) - hs}/{len(runs) - len(hint_runs)})")
    emit(f"- Hint requests denied (gate: >=20 steps AND >=10 min): "
         f"**{denied}** denials across {denied_runs} runs")
    emit()
    if hint_runs:
        emit("| run | challenge | solved | flags | steps |")
        emit("|---|---|---|---|---|")
        for r in hint_runs:
            emit(f"| {Path(r.path).name} | {r.challenge} | "
                 f"{'yes' if r.solved else 'no'} | {len(r.flags_accepted)} | {r.steps} |")
        emit()

    # ---- top errors ----
    emit("## Top 10 Error Messages / Exception Types")
    emit()
    emit("Sources: `error` events plus first lines of tool results matching "
         "error signatures; normalized (IPs/UUIDs/numbers stripped).")
    emit()
    err_counter: Counter[str] = Counter()
    for r in runs:
        for e in r.error_events:
            err_counter[norm_error(e)] += 1
    for path in files:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"tool_result"' not in line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("type") != "tool_result":
                    continue
                res = str(e.get("result", ""))
                if is_tool_error(str(e.get("tool", "")), res):
                    err_counter[f"[{e.get('tool')}] {norm_error(res)}"] += 1
    emit("| count | error |")
    emit("|---|---|")
    for msg, n in err_counter.most_common(10):
        emit(f"| {n} | `{msg}` |")
    emit()

    with open(REPORT_PATH, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    print(f"\nReport written to {REPORT_PATH}")


if __name__ == "__main__":
    sys.exit(main())
