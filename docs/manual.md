# chronos 使用手册

> 面向操作者的实战手册：安装、配置、运行、解读输出、排障。
> 设计哲学与架构取舍见 [`rewrite-plan.md`](rewrite-plan.md) 与 [`architecture.md`](architecture.md)；
> 本手册只讲"怎么用"。变更历史见 [`../CHANGELOG.md`](../CHANGELOG.md)。

---

## 1. chronos 是什么

chronos 是一个面向**智能体攻防测评**（当前主攻 [TSecBench](https://tsecbench.zc.tencent.com/)）的
极简 headless 求解系统。它的核心信念是：决定成绩的是**模型本身的 agentic 推理能力**，不是外层
臃肿的 Agent 架构。因此系统刻意做减法——**0 Skill、0 RAG、0 MCP、无角色分解、无预设攻击流程**，
只保留「模型 + 中性工具面 + 遵守平台规则所必需的机械外壳」。

它**不是**一个通用渗透平台，也不内置任何攻击方法论。系统提示是一份纯契约（目标 / 工具 / 提交
格式 / 硬约束），"怎么打"完全交给模型自己推理。

---

## 2. 安装

### 2.1 环境要求

- **Python 3.14+**（开发验证于 3.14.7）
- **uv**（推荐，锁定依赖见 `uv.lock`）或 pip
- 至少一个可用的 **LLM API Key**（推荐 DeepSeek）
- 评测平台的 **VPN 连通**（TSecBench 靶场在 VPN 段内）
- **无需 Docker**，无需本地安全工具链

### 2.2 安装依赖

```bash
# 方式一：uv（推荐，按 uv.lock 精确复现）
uv sync

# 方式二：pip
python -m pip install -e .
```

### 2.3 配置环境变量

```bash
cp .env.example .env
```

在 TSecBench 平台「开始跑分 → 本地模式」创建跑分任务，获取 `BENCHMARK_BASE_URL` 与
`BENCHMARK_TOKEN`，并按平台指引连接靶场 VPN。然后编辑 `.env`：

```env
PLATFORM_MODE=tsec
BENCHMARK_BASE_URL=https://tsecbench.zc.tencent.com   # 以平台下发为准
BENCHMARK_TOKEN=<创建跑分任务后下发的 token>
DEEPSEEK_API_KEY=your_deepseek_key
```

完整配置项见 [§4 配置参考](#4-配置参考)。

---

## 3. 运行

### 3.1 全量调度（默认）

```bash
uv run ctf-solve          # 非交互，后台跑分
uv run ctf-solve -i       # 交互式 REPL（推荐人工盯盘）
```

调度器会：轮询题目 → 按"分值降序 + 难度升序"填满最多 3 题的滚动并发窗口 → 每题起一个 swarm
（可多同构 solver 竞速）→ 管单题时限 / 全局预算 / 容器生命周期 → 死锁自愈。`Ctrl+C` 停止。

### 3.2 单题调试

```bash
uv run ctf-solve --challenge <题目目录 | 题名 | unique_code>
```

只跑一道题，便于复现与调试。仍受 target-lock 与时间预算约束。

### 3.3 只读自检（不消耗靶场/模型预算）

```bash
uv run ctf-solve --smoke-test         # 平台连通性：题目列表 + 已解状态
uv run ctf-solve --model-smoke-test   # 大模型 API 连通性
```

> 自检只做最小连通性探测，**不进行任何渗透/实测**。

### 3.4 Web 控制台（可视化 + 操控 + 对话）

```bash
uv run ctf-solve --web                    # 默认 http://127.0.0.1:8000
uv run ctf-solve --web --web-port 9000    # 自定义端口
```

浏览器里实时看到：

- **汇总**：已解 / 未解 / 活跃 / deferred / 成本 / 剩余全局预算
- **题目网格**：按状态着色，带分值 / 难度 / 多 flag 进度，可按状态筛选
- **Solver 面板**：选中某题看每个 solver 的实时状态、步数、成本、进度条；可**暂停 / 恢复 / 终止**
- **Solver 控制台**：点某个 solver 的 "Watch" 流式看它的推理轨迹（工具调用 / 结果 / flag / usage）；
  底部输入框可**向该 solver 发一条提示**（即"与 Agent 对话"——把一条操作员 guidance 以 bump 注入）

它是纯**观测 + 操控层**：调度器与 solver 代码零改动（状态读共享内存、轨迹 tail JSONL、命令走
与 REPL 相同的 operator 通道）。"对话"的对象是**正在思考的 solver**，不是调度器（调度器是机械的，
不接自然语言）。

### 3.5 干跑（不提交 flag）

```bash
uv run ctf-solve --no-submit
```

全链路打通但不向平台提交任何 flag，用于安全演练/调试。

---

## 4. 配置参考

所有配置走 `.env` + 环境变量（pydantic-settings）。常用项：

### 4.1 平台与模型

| 变量 | 默认 | 说明 |
|---|---|---|
| `PLATFORM_MODE` | `tsec` | 平台适配器选择 |
| `BENCHMARK_BASE_URL` | — | 平台下发的跑分 base URL |
| `BENCHMARK_TOKEN` | — | 跑分任务 token |
| `PLATFORM_TLS_VERIFY` | `true` | 平台客户端 TLS 校验；仅在企业 MITM 代理下置 `false`（有 token 暴露风险） |
| `DEEPSEEK_API_KEY` | — | 主力模型 Key |
| `DEEPSEEK_EXTRA_API_KEYS` | — | 额外 DeepSeek Key（逗号分隔）；并发 solver 轮转分配，倍增限流配额 |
| `QWEN_API_KEY` / `QWEN_BASE_URL` / `QWEN_API_MODE` | — / DashScope / `chat` | 备用模型 |
| `GLM_API_KEY` / `GLM_BASE_URL` / `GLM_MODEL_ID` | — / coding 端点 / `glm-5.3` | 备用模型 |

### 4.2 调度与时间预算

| 变量 | 默认 | 说明 |
|---|---|---|
| `MAX_CONCURRENT_CHALLENGES` | `3` | 并发靶场窗口（硬上限 3，按存活容器数限流） |
| `CHALLENGE_TIME_LIMIT_SECONDS` | `1800` | 单题墙钟上限；超时关靶场、defer、让位下一题 |
| `KILLCHAIN_TIME_LIMIT_SECONDS` | `2700` | 多阶段渗透（killchain）题的放宽时限 |
| `TOTAL_BENCHMARK_BUDGET_SECONDS` | `0` | 全局总预算（如 360 分钟 = 21600）；0 = 不跟踪 |
| `MIN_REMAINING_SECONDS_TO_SPAWN` | `300` | 总预算剩余少于此值时停止开新题 |
| `PARTIAL_STALL_SECONDS` | `600` | 多 flag 题拿到部分分后，此窗口内无新 flag 即带分撤退 |
| `AUTO_RESUME_IDLE_SECONDS` | `240` | 全部 defer 且无活跃 swarm 持续此秒数后，清空 deferred 重新补位（死锁自愈兜底） |
| `MAX_CHALLENGE_ATTEMPTS` | `3` | 每题最多 spawn 尝试次数（滚动重试上限）；0 = 不限 |
| `DEFER_RETRY_COOLDOWN_SECONDS` | `120` | 题目被 defer 后到自动重试的冷却；避免刚失败就被塞回原位 |
| `SOLVER_RETRY_DELAY_SECONDS` | `2` | solver 续跑（bump retry）间隔 |

### 4.3 执行与安全

| 变量 | 默认 | 说明 |
|---|---|---|
| `ALLOW_LOCAL_EXEC` | `true` | 是否允许 `run_python` 本机计算工具（`--no-exec` 可关） |
| `RUN_PYTHON_ALLOW_SUBPROCESS` | `false` | run_python 子进程逃逸开关；默认拒绝（子进程会绕过 socket 白名单），置 1 是**有文档记录的风险**，由操作者决定 |
| `VPN_HEALTH_CHECK_URL` | `http://10.0.100.58` | 启动前探测的 VPN-only 健康端点；未连 VPN 直接退出 |
| `CTF_STATE_DIR` | `.ctf-state` | run_python 的 per-run 工作区根目录 |

### 4.4 opt-in 子系统（默认全部关闭）

> 以下能力默认关闭，对 TSecBench 跑分**零行为差异**。见 [`rewrite-plan.md`](rewrite-plan.md) "已拍板"。

| 变量 / 旗标 | 默认 | 说明 |
|---|---|---|
| `CROSS_TASK_MEMORY_ENABLED=1` / `--cross-task-memory` | 关 | solver 获得 `remember(text)` / `recall_memory(query)`，写 `logs/cross-task-memory.jsonl`。纯字符串召回，无 RAG，**永不自动注入系统提示词** |
| `FINDINGS_OUTPUT_ENABLED=1` / `--findings-output` | 关 | solver 获得 `record_finding(summary, evidence, severity)`，写 `logs/findings-<题目>.jsonl`。纯观测产物，不接评分 |

---

## 5. 交互式 REPL 命令

`-i` / `--interactive` 启动后，或 Web 控制台的操作通道，支持：

| 命令 | 作用 |
|---|---|
| `/status` | 查看当前状态（已解/未解/活跃/deferred/成本/预算） |
| `/pause X` | 暂停题目 X 的 swarm |
| `/resume X` | 重开题目 X（`/resume all` 清空整个 deferred 列表，并重置尝试计数） |
| `/kill X` | 终止题目 X 的 swarm |
| `/quit` | 退出调度器 |

> 自由文本不再有 LLM 接收，会被忽略——调度器是纯机械的。要"和 Agent 对话"请用 Web 控制台
> 的 Solver 控制台（向某个正在跑的 solver 注入一条 bump 提示）。

---

## 6. 解读输出

### 6.1 Trace 日志

`logs/trace-<题名>-<模型>-<时间>.jsonl`，逐行 JSON 事件：

- `tool_call` / `tool_result`：每步工具调用与结果（截断到安全长度）
- `model_response`：模型输出 + 本步 token
- `usage`：累计 token / 成本
- `finish`：solver 结束，含 `status` 与结构化 `terminal_reason`（为什么结束）
- `bump`：注入的兄弟发现 / 操作员提示

### 6.2 Cost Tracker

运行结束打印按 agent 聚合的 token（输入/输出/缓存读写）与成本汇总。Web 控制台汇总页也有
实时成本卡片。

### 6.3 opt-in 产物

- `logs/cross-task-memory.jsonl`：跨任务记忆条目（仅开启时）
- `logs/findings-<题目>.jsonl`：漏洞产物记录（仅开启时），逐行 JSON（summary / evidence /
  severity / 时间戳）。直接读文件查看；Web 控制台的「发现」聚合页计划在后续轮次提供。

---

## 7. 平台规则适配（TSecBench）

- **单题时间预算**默认 1800s；超时未解出即关靶场、标记 `DEFERRED`，名额让给下一题。
- **并发窗口**严格 ≤3 个活跃靶场容器（按容器数限流）。
- **容器防重**：同一题进程内只 start 一次（地址缓存复用）。
- **多 flag 语义**：`correct` 但未通关返回部分得分提示，solver 继续竞速直到 `challenge_complete`。
- **hint**：`request_hint` 是普通工具（会扣分、每题限 1 次），由模型自行决定是否值得。
- **deferred 滚动重试**：窗口有空位且无"新鲜"题时，自动捞回 deferred 的题重试，无需 `/resume`；
  每题最多 `MAX_CHALLENGE_ATTEMPTS` 次，距上次 defer 需过冷却。
- **死锁自愈**：全部 defer 且无活跃 swarm 持续 `AUTO_RESUME_IDLE_SECONDS` 后，清空 deferred 重新补位。
- **提交熔断**：一题累计 4 次**错误**提交（跨模型合计，仅 `incorrect` 计入）即锁定本题提交并退场，
  防止"猜 flag"烧光预算。
- **部分得分止损**：多 flag 题拿到部分分后 `PARTIAL_STALL_SECONDS` 内无新 flag 即带分撤退。

---

## 8. 故障排查

| 症状 | 排查 |
|---|---|
| 启动即退出 "VPN check failed" | 确认靶场 VPN 已连接；`VPN_HEALTH_CHECK_URL` 是否被平台改动 |
| 题目列表为空 / "全部已解" 但实际没解 | 调度器要求**至少一次成功轮询**才可能宣告结束；检查 `BENCHMARK_TOKEN` 与 base URL，看日志有无轮询失败 |
| 某题反复 defer | 看 trace 的 `terminal_reason`：`wall_clock`=超时、`circuit_breaker`=错误提交过多、`early exit`=启动即失败（容器未就绪/瞬时报错）。滚动重试会在冷却后自动重开，达 `MAX_CHALLENGE_ATTEMPTS` 后需 `/resume` |
| run_python 报 socket/subprocess 被拒 | target-lock 生效：出网仅限题目 `connection_info` 主机；子进程默认拒绝（`RUN_PYTHON_ALLOW_SUBPROCESS=1` 可开，有风险） |
| 成本显示为 0 | 检查 provider id 是否在 genai-prices 中；qwen/glm 走 fallback 计价 |
| Web 控制台连不上 | 确认 `--web-host`/`--web-port`；反代部署注意 WebSocket 同源 |

---

## 9. 测试与质量门禁

```bash
python -m uv run pytest          # 隔离单测（mock 平台/模型/HTTP，不触网）
ruff check backend tests scripts # 风格 + 静态
ty check backend                 # 类型（0 诊断）
```

所有测试均为**隔离 mock 单测**，不依赖 TSecBench 平台、VPN 或任何 API Key。

---

## 10. 诚实边界（重要）

本项目全程**仅静态开发 + 隔离 mock 单测**：

- **未**对 TSecBench 平台、靶场 VPN 或任何模型 API 做实测。
- 需要真实环境才能验证的行为（平台 API 字段名、模型接口语义、容器生命周期时序）在代码与
  PR 中如实标注为"未经实测"。
- 任何"这道题能不能真解出来"的结论，都必须在授权的真实测评环境里跑，不在本仓库的开发约束内。
