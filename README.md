# 攻防 Agent — TSecBench

面向智能体攻防（Agentic Offensive Security）场景的自动化对抗系统。它的目标不是只靠单个模型"硬猜"，而是让 AI Agent 像一支自动化红队一样工作：自主侦察目标、识别攻击面、构造并利用漏洞、横向推进、夺取 flag——并把平台对接、靶场调度、并发对抗、多 flag 提交和过程追踪整合成一条完整流水线。

当前主攻 [TSecBench](https://tsecbench.zc.tencent.com/)（腾讯云鼎实验室智能攻防 Agent 评测平台）跑分，但能力不局限于 CTF 式题目：Web 漏洞挖掘、二进制漏洞挖掘、漏洞利用、多阶段渗透、云攻击、对抗规避六大方向均为真实攻防能力的拆解，同一套 Agent 框架也适用于授权靶场演练、漏洞验证、红队自动化等场景。

`Tsecbench v1` 题集共 63 题、23000 分、总时限 360 分钟，覆盖六大能力域：Web 漏洞挖掘（18）、二进制漏洞挖掘（13）、漏洞利用（9）、多阶段渗透（3）、云攻击（6）、对抗规避（14）。多 flag 题按 flag 部分给分，查看 hint 按比例扣分。本系统对上述规则均已适配（单题预算 30 分钟、靶场并发窗口本地严格限制为 3）。

## 执行形态

- **Headless（无沙箱）模式（默认，tsec 平台）**：不需要 Docker、不需要本地工具链，Solver 只依靠大模型自身的推理能力 + 极简工具面（`http_request` 直连靶场、`tcp_send` 原始协议交互、`run_python` 本机计算）完成解题。
- **Docker 沙箱模式（local 平台）**：每个 Solver 运行在带全套安全分析工具链的独立容器中，适合离线回放本地靶场目录。

通过 `--executor headless|docker|auto` 显式选择；`auto` 时 tsec 默认 headless，local 默认 docker。

## 系统架构

```text
TSecBench API
   ↓
Poller 轮询器（题目列表 / 已解状态）
   ↓
Coordinator 协调器（LLM 驱动，REPL 可干预）
   ↓
Challenge Swarm 题目群组（一题一群）
   ↓
多个 Solver 并行竞速（FIRST_COMPLETED）
   ↓
Headless：模型 + http_request / tcp_send / run_python
```

辅助通道：Operator REPL、Trace 日志（JSONL）、Cost Tracker、Message Bus（兄弟 Solver 共享发现）、Checkpoint 断点续跑。

## 平台规则适配

- **单题时间预算**默认 1800s（30 分钟，可用 `--time-limit` 覆盖）；超时仍未解出即关闭靶场、标记 `DEFERRED`，并发名额立即让给下一道未解题（事后可用 `/resume` 重开，会启动新容器并从 checkpoint 续做）
- **并发窗口**严格限制为最多 3 个活跃靶场容器（按容器数限流，不是按进程数）；题目解决或超时后自动 close 释放名额，并自动补入下一题
- **容器防重**：同一题在进程内只 start 一次（地址缓存复用）；容器启动中的等待一律走只读轮询，绝不重复调用 start
- **多 flag 语义**：`correct` 但未通关返回部分得分提示，不计冷却、不结算，Solver 继续竞速直到 `challenge_complete`
- **hint 扣分**：`request_hint` 工具带硬门槛——每题限 1 次，且必须做满 20 步 + 10 分钟实际工作后才解锁（历史数据：无进展时看 hint 是纯亏损）
- **死锁自愈**：所有未解题都被 defer 且无活跃 swarm 的状态持续 240 秒（`AUTO_RESUME_IDLE_SECONDS`）后，自动清空 deferred 列表重新补位，无人值守也能跑满全程
- **早夭重试**：swarm 不足 15 步就异常结束（容器未就绪/瞬时报错）时自动重拉重试一次
- **无进展止损**：连续 40/80/120 步没有提交过 flag 时向模型注入逐级加严的警告，强制其做出"提交或换面"的决策（历史数据：100+ 步无提交的题从未解出）
- **提交熔断**：一题累计 4 次错误提交（跨模型合计）即锁定本题提交并让 swarm 退场，防止"猜 flag"烧光时间预算
- **部分得分止损**：多 flag 题拿到部分分后，若 600 秒（`PARTIAL_STALL_SECONDS`）内没有新 flag 被接受，带着已有分数撤退换题
- **差异化时限**：多阶段渗透（killchain）类题目自动放宽到 2700 秒（`KILLCHAIN_TIME_LIMIT_SECONDS`），其余题 1800 秒
- **DeepSeek 多 Key 轮换**：`DEEPSEEK_EXTRA_API_KEYS` 可配额外 API Key（逗号分隔），每个 solver/协调器实例启动时按轮转分配 Key，并发下的限流配额成倍放大；429 重试上限从 SDK 默认 2 次提升到 6 次（指数退避）
- **启动前 VPN 健康检查**：探测 `http://10.0.100.58`，未连接 VPN 直接退出

## 快速开始

### 1. 安装依赖

```bash
uv sync
```

### 2. 配置环境变量

```bash
copy .env.example .env
```

在 TSecBench 平台「开始跑分 → 本地模式」创建跑分任务，获取 `BENCHMARK_BASE_URL`、`BENCHMARK_TOKEN`，并按平台指引连接靶场 VPN（健康检查地址 `http://10.0.100.58`）。然后填写 `.env`：

```env
PLATFORM_MODE=tsec
BENCHMARK_BASE_URL=https://tsecbench.zc.tencent.com   # 以平台下发为准（注意：不带 /benchmark 后缀）
BENCHMARK_TOKEN=<创建跑分任务后下发的 token>
DEEPSEEK_API_KEY=your_deepseek_key
```

### 3. 启动

```bash
uv run ctf-solve -i
```

配置了 `DEEPSEEK_API_KEY` 后，solver 与协调器默认都只用 `deepseek/deepseek-chat` 单模型直连官方 API（不需要传 `--coordinator`/`--models`）。如需禁用本机代码执行，加 `--no-exec`；想临时跑多模型竞速，显式传多个 `--models` 即可恢复 swarm 并行特性。

### 单题调试

```bash
uv run ctf-solve --challenge <题目目录、题名或 unique_code>
```

### 只读自检

```bash
# 平台连通性（只读：题目列表 + 已解状态）
uv run ctf-solve --smoke-test

# 大模型 API 连通性
uv run ctf-solve --model-smoke-test
```

## 核心机制

### 协调器 Coordinator

协调器负责全局调度：拉取题目、维持 3 题滚动窗口、汇总状态、接收人工指令。交互式 REPL 命令：

- `/status`：查看当前状态
- `/pause X`：暂停某个题目
- `/resume X`：恢复某个被 defer 的题目
- `/kill X`：停止某个题目的 swarm
- `/quit`：退出协调器

### Swarm 与 Solver

一道题对应一个 swarm，可包含多个不同模型的 solver 并行竞速；任一 solver 确认全部 flag 即取消其余。solver 卡住（GAVE_UP/ERROR）时会被注入兄弟 agent 的发现继续（bump，最多 5 次）。

### Headless Solver 工具面

| 工具 | 用途 |
|---|---|
| `http_request` | 全控制 HTTP（方法/头/body），直连 VPN 段靶场 |
| `tcp_send` | 原始 TCP 协议交互（支持 hex payload） |
| `run_python` | 本机 CPython 草稿本（计算/解码/payload 构造），`--no-exec` 可禁用 |
| `submit_flag` | 提交 flag（去重 + 错误提交递增冷却 0/30/120/300/600s） |
| `request_hint` | 平台提示（扣分，慎用） |
| `task_status` | flag 进度 x/y + 剩余时间 |
| `check_findings` / `notify_coordinator` | 兄弟发现共享 / 向协调器汇报 |

系统提示按任务特征自动匹配六大能力域攻防 playbook（web/binary/exploit/killchain/cloud/evasion），并带目标锁定（Target Lock）约束，防止在多端口之间漂移。

### 过程可追踪

- `logs/trace-<题名>-<模型>-<时间>.jsonl`：逐步工具调用/结果、bump、usage、finish 事件
- Cost Tracker：按 agent 聚合 token 与成本，运行结束打印汇总
- `.ctf-state/checkpoints/`：solver 断点（步数、发现、候选 flag、下一步），崩溃后从最新 trace 重建，重启自动续跑
- `.ctf-state/coordinator/`：协调器对话轮次持久化，跨进程重启可续

## 命令行参考

```text
uv run ctf-solve [OPTIONS]

--platform [tsec|local]     平台模式（默认 tsec；local 为离线题目目录回放）
--executor [auto|docker|headless]
--models SPEC               模型规格，可多次（如 deepseek/deepseek-chat、qwen/qwen-plus）
--coordinator BACKEND       claude|codex|deepseek|qwen|glm（默认 deepseek）
--challenge REF             单题模式（目录、题名或 unique_code）
--max-challenges N          并发靶场窗口（1-3，默认 3，按存活容器数限流）
--time-limit SECONDS        单题墙钟上限（默认 1800，超时关靶场换题）
--no-submit                 干跑，不提交 flag
--no-exec                   禁用 run_python
--smoke-test / --model-smoke-test
-i                          交互式 REPL
-v                          详细日志
```

## 目录结构

- `backend/`：协调器、solver、TSecBench 平台适配器、沙箱、工具
- `sandbox/`：Docker 镜像与沙箱运行环境（docker executor 用）
- `challenges/`：拉取后的题目元数据和本地资产
- `logs/`：trace 日志与运行记录
- `.ctf-state/`：checkpoint、协调器历史、工作区

## 运行环境要求

- Python 3.14+
- **Headless 模式**：无需 Docker；至少一个可用的 LLM API Key（推荐 DeepSeek），以及评测平台的 VPN 连通
- **Docker 沙箱模式**（local 回放）：Docker Desktop 或兼容的 Docker Engine
