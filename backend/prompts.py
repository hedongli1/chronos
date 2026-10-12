"""System prompt builder + ChallengeMeta."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from backend.tools.core import IMAGE_EXTS_FOR_VISION as IMAGE_EXTS


@dataclass
class ChallengeMeta:
    name: str = "Unknown"
    category: str = ""
    value: int = 0
    description: str = ""
    tags: list[str] = field(default_factory=list)
    connection_info: str = ""
    hints: list[dict[str, Any]] = field(default_factory=list)
    solves: int = 0
    unique_code: str = ""
    attachments: list[dict[str, Any]] = field(default_factory=list)
    # Multi-flag / benchmark semantics (TSec)
    flag_count: int = 1
    correct_flag_count: int = 0
    level: int = 0
    container_status: str = "stopped"

    @classmethod
    def from_yaml(cls, path: str | Path) -> ChallengeMeta:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls(
            name=data.get("name", "Unknown"),
            category=data.get("category", ""),
            value=data.get("value", 0),
            description=data.get("description", ""),
            tags=data.get("tags", []),
            connection_info=data.get("connection_info", ""),
            hints=data.get("hints", []),
            solves=data.get("solves", 0),
            unique_code=data.get("unique_code", ""),
            attachments=data.get("attachments", []),
            flag_count=int(data.get("flag_count", 1) or 1),
            correct_flag_count=int(data.get("correct_flag_count", 0) or 0),
            level=int(data.get("level", 0) or 0),
            container_status=data.get("container_status", "stopped"),
        )


def list_distfiles(challenge_dir: str) -> list[str]:
    dist = Path(challenge_dir) / "distfiles"
    if not dist.exists():
        return []
    return sorted(f.name for f in dist.iterdir() if f.is_file())


def _rewrite_connection_info(conn: str) -> str:
    """Replace localhost/127.0.0.1 with host.docker.internal for bridge networking."""
    if not conn:
        return conn
    conn = re.sub(r"\blocalhost\b", "host.docker.internal", conn)
    conn = re.sub(r"\b127\.0\.0\.1\b", "host.docker.internal", conn)
    return conn


def build_prompt(
    meta: ChallengeMeta,
    distfile_names: list[str],
    container_arch: str = "unknown",
    has_named_tools: bool = True,
) -> str:
    """Build the system prompt.

    has_named_tools: True for Pydantic AI solver (has view_image, webhook_create, etc.
    as discrete tools). False for Claude SDK (bash-only — model should use
    steghide/exiftool/curl instead). Codex has named dynamic tools so uses True.
    """
    conn_info = _rewrite_connection_info(meta.connection_info.strip())

    lines: list[str] = [
        "You are an elite offensive-security agent. Find the real flag for the target below.",
        "",
    ]

    if conn_info:
        lines += [
            "> **FIRST ACTION REQUIRED**: Your very first tool call MUST connect to the service.",
            f"> Run: `{conn_info}` (use a heredoc or pwntools script as shown below).",
            "> Do NOT explore the sandbox filesystem first. The flag is on the service, not in the container.",
            "> **TARGET LOCK**: Stick to this service unless you have direct evidence that another endpoint is part of the same challenge.",
            "",
        ]
    else:
        lines += [
            "> **NOTE**: No remote service connection info provided. The flag may be in the",
            "> attached files, or you may need to analyze the challenge description carefully.",
            "> Do NOT scan host.docker.internal — those are local system services, not the target.",
            "> **TARGET LOCK**: Do not jump between unrelated ports. Choose one evidence-backed target and stay on it.",
            "",
        ]

    lines += [
        "## Challenge",
        f"**Name**    : {meta.name}",
        f"**Category**: {meta.category or 'Unknown'}",
        f"**Points**  : {meta.value or '?'}",
        f"**Arch**    : {container_arch}",
    ]
    if meta.unique_code:
        lines.append(f"**Unique Code**: {meta.unique_code}")
    if meta.attachments:
        lines.append("**Attachments**: available from platform details")
    if meta.tags:
        lines.append(f"**Tags**    : {', '.join(meta.tags)}")
    lines += ["", "## Description", meta.description or "_No description provided._", ""]

    if conn_info:
        if re.match(r"^https?://", conn_info):
            hint = "This is a **web service**. Use `bash` with `curl`/`python3 requests`, or use `web_fetch`."
        elif conn_info.startswith("nc "):
            hint = (
                "This is a **TCP service**. Each `bash` call is a fresh process — "
                "use a heredoc to send multiple lines in one shot:\n"
                "```\n"
                f"{conn_info} <<'EOF'\ncommand1\ncommand2\nEOF\n"
                "```\n"
                "Or write a Python `socket` / `pwntools` script for stateful interaction."
            )
        elif re.match(r"^\S+:\d+(,\s*\S+:\d+)*$", conn_info):
            addrs = [a.strip() for a in conn_info.split(",")]
            hint = "This is a **network service**. Connect with:\n```\n"
            for a in addrs:
                hint += f"nc {a}\n"
            hint += "```\nOr use `curl http://HOST:PORT/` if it's a web service.\n"
            hint += "Try both TCP and HTTP to discover the service type."
        else:
            hint = "Connect using the details above."
        lines += ["## Service Connection", "```", conn_info, "```", hint, ""]

    if distfile_names:
        lines.append("## Attached Files")
        for name in distfile_names:
            ext = Path(name).suffix.lower()
            is_img = ext in IMAGE_EXTS
            if is_img and has_named_tools:
                suffix = "  <- **IMAGE: call `view_image` immediately** (fix magic bytes first if corrupt)"
            elif is_img:
                suffix = "  <- **IMAGE: use `exiftool`, `steghide`, `zsteg`, `strings` via bash**"
            else:
                suffix = ""
            lines.append(f"- `/challenge/distfiles/{name}`{suffix}")
        lines.append("")

    visible_hints = [h for h in meta.hints if h.get("content")]
    if visible_hints:
        lines.append("## Hints")
        for h in visible_hints:
            lines.append(f"- {h['content']}")
        lines.append("")

    cat_lower = (meta.category or "").lower()
    if cat_lower in ("reverse", "reversing", "re", "pwn", "binary", "misc", ""):
        lines += [
            "## Binary Analysis",
            "**pyghidra** is installed for decompilation. Use it via bash:",
            "```python",
            "import pyghidra",
            "with pyghidra.open_program('/challenge/distfiles/binary') as flat_api:",
            "    listing = flat_api.currentProgram.getListing()",
            "    # Iterate functions, decompile, etc.",
            "```",
            "Also available: radare2 (`r2`), gdb, angr, capstone.",
            "",
        ]

    if has_named_tools:
        image_hint = "**Images: call `view_image` FIRST, before any other analysis.**"
        web_hint = "Web: fuzz params, check JS source, cookies, robots.txt. For XSS/SSRF: use `webhook_create`."
        submit_hint = "**Verify every candidate with `submit_flag`** before reporting."
    else:
        image_hint = "**Images: use `exiftool`, `steghide`, `zsteg`, `strings`, `xxd` via bash.**"
        web_hint = "Web: fuzz params, check JS source, cookies, robots.txt. For XSS/SSRF: use `curl` to webhook.site."
        submit_hint = "**Verify every candidate with `submit_flag '<flag>'`** (bash command) before reporting."

    lines += [
        "",
        "## Instructions",
        "**Use tools immediately. Do not describe — execute.**",
        "",
        "1. " + ("Connect to the service now." if conn_info else "Inspect /challenge/distfiles now."),
        "2. Keep using tools until you have the flag.",
        "3. **Be creative and thorough** — try the obvious path, then explore further:",
        "   - Hidden files, env vars, backup files, HTTP headers, error messages, timing, encoding tricks.",
        f"   - {image_hint}",
        f"   - {web_hint}",
        (
            "   - Crypto: identify algorithm, weak keys, nonce reuse, padding oracles. "
            "For RSA: use `RsaCtfTool`, sage ECM, or `cado-nfs`."
        ),
        "   - Pwn: `stty raw -echo` before launching vulnerable binaries over nc.",
        '4. **Ignore placeholder flags** — `CTF{flag}`, `CTF{placeholder}` are not real flags.',
        (
            "5. **Never invent or guess a flag from the title or description.** Incorrect submissions consume "
            "a limited attempt. Submit only a candidate found verbatim in service, file, or exploit output."
        ),
        f"6. {submit_hint}",
        "7. Once CORRECT: output `FLAG: <value>` on its own line.",
        "8. Do not ask. Cover maximum surface area.",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------- #
#  Headless (no-sandbox) prompt — model-only offensive security task
# ---------------------------------------------------------------------- #

SCENARIO_PLAYBOOKS: dict[str, str] = {
    "web": (
        "**Web 漏洞挖掘思路**: 指纹识别(响应头/默认页面/known paths) → 目录与参数探测"
        "(/admin /backup .git robots.txt 源码泄露) → 认证与逻辑缺陷(弱口令/越权/未授权接口) → "
        "注入(SQL/XSS/SSTI/命令) → 已知 CVE 按版本精确匹配利用。"
        "先建立目标技术栈画像再选攻击面,不要盲喷 payload。"
        "收敛纪律: 同一个假设最多验证 3 次,无证据就换面; 每 10 步回顾一次"
        "'已确认事实 → 当前假设 → 下一步',避免在同一接口上空转。"
    ),
    "binary": (
        "**二进制/远程服务思路**: 先判断题型再选打法，不要在错误的套路上空转。\n"
        "【交互服务/pwn】先协议行为侦察(发送探测数据观察响应格式/错误信息),推断解析逻辑;"
        "关注整数溢出/格式化字符串/栈溢出的可观察副作用(崩溃、延迟、输出变化);"
        "没有本地调试器时靠输入差分和响应字节推理,必要时用 run_python 构造精确二进制 payload。\n"
        "【逆向/自定义VM】附件是自定义 VM/字节码/校验器时,走静态+仿真路线: capstone 反汇编、"
        "unicorn 仿真执行(run_python 已内置),逐条 trace 中间状态与内存,推出校验逻辑后反解输入。"
        "这类题的 flag 是算出来的,不是打印出来的。\n"
        "铁律: 二进制里明文的 flag 样字符串基本都是植入的诱饵——凡是无法追溯到程序真实输出"
        "或仿真执行结果的提取串/解码串,一律不准提交;只提交正确执行轨迹产生的值。"
    ),
    "exploit": (
        "**漏洞利用思路**: 指纹 → CVE → 链式验证。先做精确指纹识别(响应头 Server/X-Powered-By、"
        "默认页面、特征路径如 /geoserver/web、报错堆栈里的版本号),再按'组件+版本'精确匹配已知 CVE"
        "(例如 GeoServer 2.x→CVE-2024-36401 OGNL 注入、Gradio→路径穿越/组件反序列化)。"
        "AI 服务(ComfyUI/Gradio/Ollama/Dify 等)务必先定精确版本,再查该版本的已知 CVE 链。"
        "构造最小 PoC 时严格按 CVE 利用链: endpoint → 参数位置 → payload 编码(URL 编码/XML 实体/OGNL 表达式),"
        "每一步都要有可观察信号(状态码变化、错误堆栈、回显差异)来确认走到哪一环。"
        "拿 flag 优先读常见路径(/flag、/root/flag、环境变量、数据库),写马或 OOB 外带作为兜底。"
        "payload 的编码和转义用 run_python 精确生成,不要手写。"
        "目标无外网时: 利用链需要 git clone/curl 从公网拉 payload 而目标无 egress,"
        "就把投递方式换成你已有的写原语(任意文件写 → 插件/autoload/扩展目录 → 触发服务 reload/restart),"
        "不要死磕网络。指纹→版本→CVE 只有在能执行完整利用链时才有价值; 盲改 payload 最多 10-15 个变体,"
        "仍无进展就升级策略(允许时 request_hint),不要无限变异。"
    ),
    "killchain": (
        "**多阶段渗透思路**: 按杀伤链分阶段推进，每阶段设定明确产出：\n"
        "阶段1 侦察——枚举可达主机/端口/服务/版本，列出攻击面清单，只扫一遍；\n"
        "阶段2 立足点——从最弱服务拿初始访问（弱口令、未授权接口、已知 CVE、Web 漏洞）；\n"
        "阶段3 扩权——搜集凭据（配置文件、环境变量、历史记录、数据库），尝试提权；\n"
        "阶段4 横向——用已获凭据登录其他服务/主机，重复阶段 2-3 直到拿全 flag。\n"
        "纪律：每拿到一个凭据/地址/flag 立即记录并提交；进入新阶段前先穷举已有凭据的"
        "新组合（凭据复用是横向移动的核心）；卡住时回到凭据清单，不要重复扫描。"
    ),
    "cloud": (
        "**云攻击思路**: 识别云厂商与组件(元数据服务 169.254.169.254、对象存储、K8s API、Redis/Docker API 等"
        "未授权端口) → 配置错配/敏感凭据枚举 → IAM 提权或容器逃逸 → 拿到更高权限后找 flag。"
    ),
    "evasion": (
        "**对抗规避思路**: 先弄清检测机制(测试 benign 请求 vs 恶意请求的响应差异定位 WAF/EDR 规则) → "
        "对 payload 做变形(编码/分块/大小写/等价语法/加密信道)逐个验证哪个变形能过 → 过检测后完成实际动作拿 flag。"
    ),
}


def _guess_scenario(meta: ChallengeMeta) -> str:
    """Infer the offensive-security dimension from code/name/category signals."""
    blob = f"{meta.unique_code} {meta.name} {meta.category} {meta.description}".lower()
    for key in ("killchain", "evasion", "exploit", "cloud", "binary", "web"):
        if key in blob:
            return key
    for word, key in (
        ("渗透", "killchain"), ("横向", "killchain"), ("内网", "killchain"), ("kill chain", "killchain"),
        ("穿透", "killchain"), ("多级", "killchain"), ("跳板", "killchain"),
        ("免杀", "evasion"), ("绕过", "evasion"), ("规避", "evasion"), ("waf", "evasion"), ("edr", "evasion"),
        ("rce", "exploit"), ("poc", "exploit"), ("利用", "exploit"), ("exp_", "exploit"),
        ("挖掘", "exploit"), ("审计", "exploit"), ("安全测试", "exploit"), ("cve", "exploit"),
        ("comfyui", "exploit"), ("gradio", "exploit"), ("ollama", "exploit"), ("dify", "exploit"),
        ("k8s", "cloud"), ("kubernetes", "cloud"), ("容器逃逸", "cloud"), ("iam", "cloud"), ("云", "cloud"),
        ("pwn", "binary"), ("reverse", "binary"), ("逆向", "binary"), ("二进制", "binary"),
        ("stack", "binary"), ("heap", "binary"), ("overflow", "binary"), ("bof", "binary"),
        ("注入", "web"), ("sql", "web"), ("xss", "web"),
    ):
        if word in blob:
            return key
    return "web"


def build_headless_prompt(
    meta: ChallengeMeta,
    *,
    remaining_seconds: int | None = None,
    allow_exec: bool = True,
) -> str:
    """Build the system prompt for the sandbox-free, model-only solver.

    The model reaches the live target over HTTP/TCP tools directly; heavy local
    toolchains (Docker sandbox, SageMath, ghidra, ...) are intentionally absent.
    """
    addrs = [a.strip() for a in re.split(r"[,\s]+", meta.connection_info) if a.strip()]
    scenario = _guess_scenario(meta)
    playbook = SCENARIO_PLAYBOOKS[scenario]

    lines: list[str] = [
        "You are an elite offensive-security AI solving a live benchmark task.",
        "You have NO local sandbox or security toolchain — your strengths are reasoning,",
        "protocol comprehension, and precise payload construction. Interact with the",
        "target only through the provided http_request / tcp_send tools, and compute",
        "or craft artifacts with run_python when needed.",
        "",
        "## Task",
        f"**Name**       : {meta.name}",
    ]
    if meta.unique_code:
        lines.append(f"**Unique Code**: {meta.unique_code}")
    if meta.category:
        lines.append(f"**Difficulty** : {meta.category}")
    if meta.value:
        lines.append(f"**Score**      : {meta.value}")
    if meta.level:
        lines.append(f"**Level**      : {meta.level}")
    flags_total = max(1, meta.flag_count)
    if flags_total > 1 or meta.correct_flag_count:
        lines.append(
            f"**Flags**      : {meta.correct_flag_count}/{flags_total} already accepted — "
            f"the task is only complete when ALL flags are submitted."
        )
    if remaining_seconds is not None:
        lines.append(f"**Time budget** : ~{max(0, remaining_seconds) // 60} minutes left for this task.")
    lines += ["", "## Description", meta.description or "_No description provided._", ""]

    lines += ["## Target"]
    if addrs:
        lines += [
            f"Live target address(es): {', '.join(addrs)}",
            "They are reachable over VPN right now. Probe them FIRST before any theory.",
            "",
            "Rules of engagement:",
            "- The listed address(es) are the task target. Do not attack anything else.",
            "- A target lock is ENFORCED: http_request / tcp_send / run_python connections "
            "to any other host are blocked. Other IPs on this VPN belong to other tasks; "
            "flags found there are invalid here.",
            "- Services may speak HTTP or raw TCP — try both if unsure "
            "(http_request for HTTP, tcp_send for line/binary protocols).",
        ]
    else:
        lines += [
            "No live address provided. Reason from the task description; if an address",
            "appears later in platform messages, treat it as the target.",
        ]
    lines += ["", "## Playbook", playbook, ""]

    lines += [
        "## Tool usage",
        "- `http_request(url, method, headers, body)`: full control over requests to the target.",
        "  Bearer/cookie auth, multipart, URL-encoded bodies — build them by hand.",
        "- `tcp_send(host, port, payload)`: raw TCP for non-HTTP services; payload may contain \\n and escapes.",
        "- `submit_flag(flag)`: submit each captured flag immediately — partial credit exists on multi-flag tasks.",
    ]
    if allow_exec:
        lines += (
            "- `run_python(code)`: local CPython — full stdlib plus installed libraries: "
            "unicorn (CPU emulation) and capstone (disassembly) for VM/reverse work, "
            "requests/httpx (HTTP clients), cryptography. It is your scratchpad; "
            "print() the results you need. If an import unexpectedly fails, fall back to stdlib."
        )
    lines += [
        "- `request_hint()`: platform hint — DEDUCTS 10% SCORE, at most once per task. "
        "It unlocks after 20+ tool steps AND 10+ minutes of real work — OR earlier "
        "(once 10+ steps AND 5+ minutes have passed) when the run shows doom signals: "
        "2 platform-rejected submissions, or 30 steps with no new confirmed flag and no "
        "new finding. Denials state your exact unlock status. Hints on tasks where you "
        "have made no progress are pure loss — exhaust your own ideas first. "
        "Exception guidance: on precise-fingerprint CVE tasks (exact component+version "
        "identified but the chain may postdate your knowledge), an early hint once unlocked "
        "is usually cheaper than prolonged blind payload mutation.",
        "- `task_status()`: flag progress and remaining time. Check it when unsure how many flags remain.",
        "",
        "## Output discipline",
        "1. Act with tools immediately; keep each step small and verifiable.",
        "2. After every probe, state in one line what was learned before the next step.",
        "3. Flags are usually `flag{...}`-like strings found in responses, files, or exploit output.",
        "   Submit verbatim — never guess or fabricate a flag.",
        "4. On multi-flag tasks, keep working after a correct submission until task_status shows all flags accepted.",
        "5. Track what you already tried; never repeat an identical request expecting a different result.",
        "6. NEVER resubmit a flag that was already rejected — every failed submit_flag result lists "
        "the rejected candidates. Track them, and only submit new evidence-backed values; blind "
        "guessing trips a task-wide submit lock.",
        "7. Local disk is untrusted: the runner host may contain files and flags from OTHER tasks. "
        "Never submit a flag found in local files — only flags observed in THIS task's target "
        "responses count.",
        "8. Treat task_status flag counts as ground truth after every submit_flag: if the accepted "
        "count did not increase, your flag was NOT accepted — even if the acknowledgement looked "
        "positive. Keep it, re-verify, and continue instead of assuming success.",
        "9. Only attack hosts listed in this task's connection info. The VPN carries other teams' "
        "instances; a flag obtained from any other host is invalid, and submitting it burns attempts.",
        "10. Broken-instance signal: if an input-free endpoint (login page, index) returns a uniform "
        "500/empty error across several attempts over minutes, suspect a broken deployment — record "
        "it, re-check once after a pause (or restart if the platform offers it), then withdraw "
        "rather than brute-forcing an unreachable service.",
    ]

    return "\n".join(lines)
