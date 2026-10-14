# 攻防 Agent — TSecBench

面向智能体攻防（Agentic Offensive Security）的**极简**自动化系统。核心信念：决定成绩的是模型本身的 agentic 推理能力，不是外层臃肿的 Agent 架构，也不是一堆 Skill / 预设流程。因此本项目刻意做减法——**0 Skill、0 RAG、0 MCP、无任务角色分解、无预设攻击流程、无跨任务记忆**；只保留「模型 + 中性工具面 + 遵守平台规则所必需的机械外壳」。

设计基线与取舍记录见 [`docs/rewrite-plan.md`](docs/rewrite-plan.md)。

当前主攻 [TSecBench](https://tsecbench.zc.tencent.com/)（腾讯云鼎实验室智能攻防 Agent 评测平台）跑分。`Tsecbench v1` 题集共 63 题、23000 分、总时限 360 分钟，覆盖六大能力域：Web 漏洞挖掘、二进制漏洞挖掘、漏洞利用、多阶段渗透、云攻击、对抗规避。多 flag 题按 flag 部分给分，查看 hint 按比例扣分。

## 任务模型：一切都是搜索

Agent 的核心循环被建模为「**搜索 + 随手捕获每一个产物**」，而不是「追逐那一个终极 flag」。`submit_flag` 只是"捕获一个恰好等于可验证终极目标的产物"的特例；多 flag 题与多阶段渗透题本就是"搜索产出多个中间产物、Goal 弥散（要集齐）"的结构，所以发现一个 flag 就立即提交、继续搜索，直到集齐或预算耗尽。

## 执行形态

**Headless（无沙箱）模式，唯一形态**：不需要 Docker、不需要本地工具链，Solver 只依靠大模型自身的推理能力 + 极简中性工具面（`http_request` 直连靶场、`tcp_send` 原始协议交互、`run_python` 本机计算）完成解题。底层模型默认 DeepSeek（`deepseek/deepseek-chat`），架构与模型解耦。

## 系统架构

```text
TSecBench API
   ↓
Poller 轮询器（题目列表 / 已解状态 / 分值 / 难度）
   ↓
机械调度器（无 LLM）：按分值降序 + 难度升序填满并发窗口，
   管单题时限 / 全局预算 / 容器生命周期 / 死锁自愈 / REPL
   ↓
Challenge Swarm 题目群组（一题一群）
   ↓
HeadlessSolver × N（同构，可并行竞速；默认单模型）
   ↓
模型 + 中性工具面：http_request / tcp_send / run_python / submit_flag / task_status
```

调度层是**纯机械**的，没有 LLM 协调器做策略推理——确定、可复现、可调试。同题的多个 Solver 之间通过 Message Bus 共享发现（sibling findings / bump），这是同构 worker 的知识共享，不是任务角色分解。

辅助通道：Operator REPL、Trace 日志（JSONL）、Cost Tracker。

## 平台规则适配

- **单题时间预算**默认 1800s（30 分钟，可用 `--time-limit` 覆盖）；超时仍未解出即关闭靶场、标记 `DEFERRED`，并发名额立即让给下一道未解题（之后由"deferred 滚动重试"自动重开，也可 `/resume` 手动重开）
- **并发窗口**严格限制为最多 3 个活跃靶场容器（按容器数限流）；题目解决或超时后自动 close 释放名额，并自动补入下一题
- **容器防重**：同一题在进程内只 start 一次（地址缓存复用）；容器启动中的等待走只读轮询
- **多 flag 语义**：`correct` 但未通关返回部分得分提示，Solver 继续竞速直到 `challenge_complete`
- **hint**：`request_hint` 是一个普通工具，获取平台提示（会扣分、每题限 1 次），由模型自行决定是否值得——不设门槛、不做 benchmark 行为调参
- **deferred 滚动重试**：窗口一旦有空位且没有"新鲜"（从未尝试过）的题可填，就自动把 deferred 的题捞回来重试——**无需操作员 /resume**。每题最多 `MAX_CHALLENGE_ATTEMPTS`（默认 3）次,之后维持 deferred 直至人工重开；距上次 defer 要过 `DEFER_RETRY_COOLDOWN_SECONDS`（默认 120s）冷却才重试,避免刚失败就被塞回原位空转
- **死锁自愈**：所有未解题都被 defer 且无活跃 swarm 的状态持续 240 秒（`AUTO_RESUME_IDLE_SECONDS`）后，自动清空 deferred 列表重新补位（兜底：连 attempts 用尽的题也会在收尾阶段再试一轮）
- **早夭重试**：swarm 不足 15 步就异常结束（容器未就绪/瞬时报错）时自动重拉重试一次
- **提交熔断**：一题累计 4 次错误提交（跨模型合计）即锁定本题提交并让 swarm 退场，防止"猜 flag"烧光时间预算
- **部分得分止损**：多 flag 题拿到部分分后，若 600 秒（`PARTIAL_STALL_SECONDS`）内没有新 flag 被接受，带着已有分数撤退换题
- **差异化时限**：多阶段渗透（killchain）类题目自动放宽到 2700 秒（`KILLCHAIN_TIME_LIMIT_SECONDS`），其余题 1800 秒
- **DeepSeek 多 Key 轮换**：`DEEPSEEK_EXTRA_API_KEYS` 可配额外 API Key（逗号分隔），每个 solver 实例按轮转分配 Key；429 重试上限提升到 6 次（指数退避）
- **启动前 VPN 健康检查**：探测 `VPN_HEALTH_CHECK_URL`（默认 `http://10.0.100.58`），未连接 VPN 直接退出
- **全局时间预算感知**（可选，默认关闭）：设置 `TOTAL_BENCHMARK_BUDGET_SECONDS`（如 360 分钟 = 21600）后，调度器会在总预算只剩 `MIN_REMAINING_SECONDS_TO_SPAWN`（默认 300 秒）时停止开新题，并把新题的单题时限钳制到"实际剩余时间"以内
- **调度优先级**：自动补位按"分值降序、难度等级升序"排列候选队列，而非题名字母序

## 快速开始

### 1. 安装依赖

```bash
uv sync
```

### 2. 配置环境变量

```bash
cp .env.example .env
```

在 TSecBench 平台「开始跑分 → 本地模式」创建跑分任务，获取 `BENCHMARK_BASE_URL`、`BENCHMARK_TOKEN`，并按平台指引连接靶场 VPN。然后填写 `.env`：

```env
PLATFORM_MODE=tsec
BENCHMARK_BASE_URL=https://tsecbench.zc.tencent.com   # 以平台下发为准
BENCHMARK_TOKEN=<创建跑分任务后下发的 token>
DEEPSEEK_API_KEY=your_deepseek_key
```

### 3. 启动

```bash
uv run ctf-solve -i
```

默认用 `deepseek/deepseek-chat` 单模型直连官方 API。如需禁用本机代码执行加 `--no-exec`；想跑多模型同题竞速，显式传多个 `--models`。

### 单题调试

```bash
uv run ctf-solve --challenge <题目目录、题名或 unique_code>
```

### 只读自检

```bash
uv run ctf-solve --smoke-test         # 平台连通性（题目列表 + 已解状态）
uv run ctf-solve --model-smoke-test   # 大模型 API 连通性
```

### Web 控制台（可视化 + 与 solver 对话）

```bash
uv run ctf-solve --web                 # 默认 http://127.0.0.1:8000
uv run ctf-solve --web --web-port 9000
```

内嵌 FastAPI + WebSocket 服务,机械调度器在后台正常跑分,浏览器里实时看到:

- **汇总**:已解 / 未解 / 活跃 / deferred / 成本 / 剩余全局预算
- **题目网格**:按状态着色,带分值 / 难度 / 多 flag 进度,可按状态筛选
- **Solver 面板**:选中某题看每个 solver 的实时状态、步数、成本、进度条;可**暂停 / 恢复 / 终止**该题
- **Solver 控制台**:点某个 solver 的 "Watch",实时流式显示它的推理轨迹(工具调用 / 结果 / flag / usage);底部输入框可**向该 solver 发送一条提示**——这就是"与 Agent 对话",即把一条操作员 guidance 以 bump 的形式注入正在解题的 solver

它是纯**观测 + 操控层**:调度器与 solver 代码零改动(状态靠读共享状态、轨迹靠 tail JSONL、命令走与 REPL 相同的 operator 通道)。调度器本身是机械的,不接自然语言,所以"对话"的对象是**正在思考的 solver**,不是调度器。

## 核心机制

### 机械调度器

无 LLM，确定性编排：轮询题目、按分值/难度优先填满 3 题滚动窗口、管生命周期与预算、死锁自愈。交互式 REPL 命令：

- `/status`：查看当前状态
- `/pause X` / `/resume X`（`/resume all` 清空整个 deferred 列表）
- `/kill X`：停止某个题目的 swarm
- `/quit`：退出调度器

自由文本不再有 LLM 接收，会被忽略。

### Swarm 与 Solver

一道题对应一个 swarm，可包含多个同构 solver 并行竞速；任一 solver 确认全部 flag 即取消其余。solver 卡住（GAVE_UP/ERROR）时会被注入兄弟 agent 的发现继续（bump，最多 5 次）。

### Headless Solver 工具面（中性）

| 工具 | 用途 |
|---|---|
| `http_request` | 全控制 HTTP（方法/头/body），直连 VPN 段靶场 |
| `tcp_send` | 原始 TCP 协议交互（支持 hex payload） |
| `run_python` | 本机 CPython 草稿本（计算/解码/payload 构造），`--no-exec` 可禁用 |
| `submit_flag` | 提交 flag（去重 + 错误提交递增冷却 0/30/120/300/600s） |
| `request_hint` | 平台提示（扣分，由模型自行决定是否使用） |
| `task_status` | flag 进度 x/y + 剩余时间 |
| `check_findings` / `notify_coordinator` | 同题兄弟发现共享 / 向调度器汇报 |

系统提示是一份**纯契约**：目标（在授权目标上搜索并捕获 flag）、工具契约、提交格式、硬约束（范围锁定、不许编造 flag、本地磁盘不可信）。**不含任何攻击方法论或题型 playbook**。目标锁定（Target Lock）是纯结构性的：solver 自身的出网一律限制在题目 connection_info 声明的主机内，无任何绕过。

### 过程可追踪

- `logs/trace-<题名>-<模型>-<时间>.jsonl`：逐步工具调用/结果、bump、usage、finish 事件
- Cost Tracker：按 agent 聚合 token 与成本，运行结束打印汇总

## 命令行参考

```text
uv run ctf-solve [OPTIONS]

--models SPEC               模型规格，可多次（如 deepseek/deepseek-chat、qwen/qwen-plus）
--challenge REF             单题模式（目录、题名或 unique_code）
--max-challenges N          并发靶场窗口（1-3，默认 3，按存活容器数限流）
--time-limit SECONDS        单题墙钟上限（默认 1800，超时关靶场换题）
--total-budget-seconds N    全局总预算（默认关闭；设置后钳制单题时限 + 预算告急停止开新题）
--no-submit                 干跑，不提交 flag
--no-exec                   禁用 run_python
--smoke-test / --model-smoke-test
--web                       启动 Web 控制台（可视化 + 操控 + 盯 solver / 对话）
--web-host / --web-port     Web 控制台监听地址（默认 127.0.0.1:8000）
-i                          交互式 REPL
-v                          详细日志
```

## 目录结构

- `backend/`：机械调度器、headless solver、TSecBench 平台适配器、中性工具
- `backend/web.py` + `backend/webui/`：Web 控制台（FastAPI 服务 + 单页前端）
- `challenges/`：拉取后的题目元数据
- `logs/`：trace 日志与运行记录
- `.ctf-state/`：run_python 的 per-run 工作区
- `docs/rewrite-plan.md`：设计基线与删/留取舍

## 运行环境要求

- Python 3.14+
- 无需 Docker；至少一个可用的 LLM API Key（推荐 DeepSeek），以及评测平台的 VPN 连通
