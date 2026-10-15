# 更新日志

本项目的重要变更记录在此文件中，格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

> 说明：chronos 是一个面向智能体攻防测评（TSecBench）的**极简** headless 求解系统，
> 设计基线见 [`docs/rewrite-plan.md`](docs/rewrite-plan.md)（Less Is More：0 Skill / 0 RAG /
> 无角色分解 / 提示词为中性契约）。本日志只记录**对外可观察的行为变化**（功能、修复、
> 配置项、契约），不记录纯内部重构的细节。

## [Unreleased]

> 对应 `upgrade/round2-artex-and-security` 分支：一轮对抗性审计（5 路并行 + 逐条验证）
> 驱动的安全/正确性修复，加上两个借鉴 ARTEX 的中性功能增强与文档手册。全程仅静态开发 +
> 隔离 mock 单测，未对平台/VPN/模型做任何实测。

### 修复的问题（安全）

- **修复 `run_python` target-lock 沙箱逃逸（高危）**：此前 guard 的预序列（preamble）与模型
  代码共享同一 `__main__` 命名空间，且只 `del` 了模块别名，导致 `_orig_connect` / `_guard` /
  `_ALLOWED_HOSTS` 等全部作为**模块全局**可被模型代码触达——`_orig_connect(s, ('10.0.9.9', 80))`
  可直连非授权主机、`_guard = lambda…` 可整体瘫痪护栏、改写 `_ALLOWED_HOSTS` 可放行任意主机，
  与 `deps.py` "无绕过、纯结构性" 的声明相矛盾。现把整个护栏的安装收进一个函数，原始句柄 /
  guard / 允许主机集全部成为**闭包局部量**，模型代码运行在不含任何护栏内部的干净命名空间里；
  并尽力补丁原始 C 类 `_socket.socket`（静态类型拒绝时降级，Python 子类补丁仍覆盖常规用法）。
  补 6 条回归测试（命名空间隔离 / 改全局 guard 无效 / 子进程仍被拦 / 本地计算不受影响）。
- **修复 `run_python` 子进程开关可被代码现场翻开（高危）**：`spawn_error` 之前在**调用时**读
  `os.environ['RUN_PYTHON_ALLOW_SUBPROCESS']`，模型代码 `os.environ[...] = '1'` 即可解锁
  `subprocess`/`os.system` 逃逸。现改为在护栏安装时**一次性**读入闭包常量，事后改环境变量无效。
  补回归测试。
- **修正 target-lock 的过度声明**：`deps.py` 与 `local_exec.py` 文档此前称 "无绕过、纯结构性"。
  实事求是地改为：这是**防意外漂移**（c-03 事故：误打其他队伍实例）的结构性护栏，**不是**
  对抗刻意逆向的沙箱——铁了心的代码仍可经 `__closure__` / `ctypes` / 原始 C socket 触达原始句柄；
  真正的隔离需要 OS 级边界（netns/防火墙），超出本无 Docker 工具的范围。

### 新增的功能

- **同题 solver 过程级检索 `search_findings(query)`**：借鉴 ARTEX「worker 间过程级信息交换」，
  在原有 message bus（sibling findings 推送 + 一次性 `check_findings` 拉取）之上，新增**按关键字
  重复检索**同题其他 solver 已发布发现的能力。检索不消耗游标、可多次调用，命中项带来源 solver
  标识、按时间倒序。仍是同构 worker 间的知识共享，不引入角色分工、不接 LLM 协调，随 swarm 默认启用。
- **结构化终止原因（terminal reason）**：借鉴 ARTEX `terminalreason.go`，solver 结束时在 trace 的
  `finish` 事件与 `SolverResult` 上记录结构化 `terminal_reason`（`flag_found` / `gave_up` /
  `error` / `wall_clock_timeout` / `operator_kill` / `submit_circuit_breaker` /
  `partial_stall_withdraw` / `sibling_won` / `swarm_complete_no_flag` 等），便于事后分析
  "为什么这道题没拿下"。纯观测产物，不改变任何调度/评分行为。

### 文档

- 新增 [`docs/manual.md`](docs/manual.md) 使用手册：安装、配置、运行模式（全量调度 / 单题 /
  只读自检 / Web 控制台）、REPL 命令、opt-in 子系统、故障排查与"未经实测"边界声明。
- 新增本 `CHANGELOG.md`。

### 计划中（后续轮次，尚未实现）

- Web 控制台「发现」页（聚合 `record_finding` 产物）与成本/Token 明细卡片。
- 本轮审计确认的其余中/低危修复（net_tools 超时/重置、swarm 多 flag 胜利判定、web 致命错误
  透出、平台 pull_challenge 重试阻塞等）将在后续轮次逐条处理。

## [0.2.0] - 2026-10-14

> 对应 `upgrade/quality-and-features` 分支（PR #2）：一次以**正确性第一**的全量升级。
> 先全库审计（94 条确认缺陷），逐条修复并配回归测试，再做对抗性交叉复审（28 条确认，
> 含 2 条升级过程中引入的高危回归，已全部修复）。全程仅静态开发 + 隔离 mock 单测。

### 新增的功能

- **跨任务记忆（opt-in，默认关闭）**：`CROSS_TASK_MEMORY_ENABLED=1` 时 solver 获得
  `remember(text)` / `recall_memory(query)` 工具，写入 `logs/cross-task-memory.jsonl`。
  召回是纯字符串匹配（无 RAG、无 embedding、无 LLM 协调），记忆**永不自动注入系统提示词**，
  只按显式 recall 返回。是对基线原则 1 的有意识、受限豁免。
- **漏洞产物输出（opt-in，默认关闭）**：`FINDINGS_OUTPUT_ENABLED=1` 时 solver 获得
  `record_finding(summary, evidence, severity)` 工具，写入 `logs/findings-<题目>.jsonl`。
  纯观测产物，不接任何评分通道。
- **内嵌 Web 控制台**：`--web` 启动 FastAPI + WebSocket 单页控制台，实时可视化题目网格 /
  solver 状态 / 成本，支持暂停/恢复/终止某题、盯某个 solver 的推理轨迹、并向其注入一条
  操作员提示（bump）。纯观测 + 操控层，调度器与 solver 代码零改动。

### 修复的问题

> 本节是 R1 全量审计（94 条）+ R6 对抗性复审（28 条）的结论，每条都先定位、再修、再补回归测试。

- **修复空/不可达题目列表被误判为"全部已解"（高危）**：调度器此前在轮询失败时可能把空列表
  当成"没有未解题"而宣告成功退出。现要求**至少一次成功轮询**（`poller.last_poll_ok`）才可能
  结束，否则 `RuntimeError` 退出，绝不假成功。
- **修复 target-lock 空 hostname 失败放行（高危）**：`//host` 这类 URL 可绕过 `urlparse` 的
  hostname 解析走私到非授权主机。现对空 host 一律 BLOCK（失败关闭），并支持 IPv6 括号主机解析。
- **修复 HTTP 重定向绕过 target-lock（高危）**：`do_raw_http` 改为显式逐跳重定向，每一跳都
  重新做白名单复检，越界重定向拒绝跟随。
- **修复 `--no-submit` 干跑仍真实提交 flag（高危）**：`no_submit` 现贯通
  `ChallengeSwarm → HeadlessSolver → HeadlessDeps` 三层，并在 `try_submit_flag` 加防御性早退。
- **修复 `unknown` 提交状态被当作猜错计入熔断（高危）**：只有平台明确返回 `incorrect` 才计错；
  `unknown`/网络异常不烧 flag、不进熔断、可重试。
- **修复 poller 吞掉 `CancelledError` 致关停挂起（高危）**：`get_event` 只吞 timeout，
  让取消正常传播，Web 控制台关停不再挂起。
- **修复中断/失败运行的 token 计费丢失（中危）**：每次 `agent.run` 传入独立 `RunUsage` 累加器，
  取消/异常路径通过 `_record_interrupted_usage` 补记。
- **修复确认 flag 在异常路径被丢弃（中危）**：异常路径镜像取消守卫，已确认的 flag 不再丢失。
- **修复 `max_tokens` 超 provider 上限 / 线上字段错误（中危）**：修正为各 provider 真实上限，
  DeepSeek/GLM/Qwen chat 端点强制以 `max_tokens` 字段上线（profile
  `openai_chat_supports_max_completion_tokens=False`）。
- **修复 cost tracker provider id 不存在致 qwen/glm 成本静默算 0（中危）**：移除 genai-prices
  里不存在的 provider id，补 fallback 计价。
- **修复 run_python 沙箱守卫多处缺口（中危）**：补齐 `connect_ex`/`sendto`/`sendmsg`/asyncio
  路径；子进程逃逸默认拒绝；`sendmsg` 补丁加 `hasattr` 守卫（修复 Windows 上每次 run_python
  崩溃的回归）；mkstemp fd 泄漏、Windows 超时 taskkill 同步阻塞修复。
- **修复 `step_count` property 插入位置致 `_result()` 全路径 AttributeError（高危回归）**：
  R6 复审抓出——property 误插在 `__init__` 中间，四个 `_traced_*` 初始化变成 property 体内
  死代码。已修正并加回归锁测试。
- **平台适配器加固（中/低危）**：TLS 校验默认开启（可 opt-out）、重复 `/start` 防重、
  4xx "already closed" 判成功、索引按最新列表重建、Qwen 默认 `chat` 模式。
- **Web 控制台加固（低危）**：平台字段 XSS 转义、`preparing` 状态过滤器、trace 事件兜底、
  WebSocket 队列有界。

### 依赖与可复现

- 依赖下限钉在 `pyproject.toml`，精确 pin 入库 `uv.lock`，保证可复现安装。
- 关键生态：pydantic-ai 2.56.x、openai 3.30.x（httpx2 生态）、httpx 0.28.x、fastapi 0.143.x。

## [0.1.0] - 2026-10-12

> 初始极简架构：把早期的多后端 / LLM 协调器 / checkpoint 体系收敛为"模型 + 中性工具面 +
> 机械外壳"。对应 `refactor(1a)`–`refactor(5)` 系列。

### 新增的功能

- **机械调度器（无 LLM）**：确定性编排——轮询题目、按分值降序 + 难度升序填满 3 题滚动并发
  窗口、管单题时限 / 全局预算 / 容器生命周期 / 死锁自愈 / 交互式 REPL。
- **HeadlessSolver + 同题 swarm**：一题一群，可多同构 solver 并行竞速；中性工具面
  `http_request` / `tcp_send` / `run_python` / `submit_flag` / `request_hint` / `task_status`。
- **结构性 target-lock**：solver 出网严格限制在题目 `connection_info` 声明的主机内，纯结构、
  无关键词猜测、无绕过。
- **TSecBench 平台适配器**：题目拉取、容器 start/close 生命周期、flag 提交、hint、已解状态轮询。
- **可观测性**：JSONL trace（逐步工具调用/结果/usage/finish）+ CostTracker（按 agent 聚合
  token 与成本）。

### 变更

- **删除任务语义脚手架**：移除六大能力域 playbook、场景分类提示词塑形、按历史数据调出的行为
  干预阈值——提示词收敛为纯契约（目标 / 工具契约 / 提交格式 / 硬约束），不含任何攻击方法论。
- **删除 LLM 协调器与 legacy 多后端**：移除 `pydantic_coordinator` / `claude_*` / `codex_*` /
  docker sandbox / checkpoint-resume 子系统（Less Is More；崩了就在时限内重开）。

### 后续（作者主线，0.2.0 之前）

- **deferred 滚动重试**：窗口有空位且无"新鲜"题可填时，自动把 deferred 的题捞回重试，无需
  操作员 `/resume`；每题最多 `MAX_CHALLENGE_ATTEMPTS`（默认 3）次，距上次 defer 需过
  `DEFER_RETRY_COOLDOWN_SECONDS`（默认 120s）冷却，避免刚失败就被塞回原位空转。
- **不再立即重开刚失败的题**：早夭 swarm 改为统一走冷却门控的滚动重试，而非立即重拉。
