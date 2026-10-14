# 架构说明（升级后基线）

> 本文档沉淀 upgrade/quality-and-features 分支后的系统架构与关键不变量。
> 设计哲学与取舍见 [`rewrite-plan.md`](rewrite-plan.md)（唯一基线）；本文不与之冲突，只做展开。

## 一页架构

```
TSecBench API (tsec_platform.py, TLS verify 默认开)
   ↓
Poller (backend/poller.py) ── 轮询题目/已解状态，last_poll_ok/consecutive_failures 跟踪
   ↓
机械调度器 (agents/coordinator_loop.py + coordinator_core.py + scheduler.py)
   · 分值降序 + 难度升序填满 3 题滚动窗口
   · 容量单一事实源：open_containers | pending_spawns（两侧口径已统一）
   · 单题时限 / 全局预算 / 容器生命周期 / 死锁自愈 / REPL / ghost reap
   ↓
ChallengeSwarm (agents/swarm.py) ── 一题一群，同构多 solver 竞速
   · try_submit_flag：去重 + 冷却 + 熔断（仅 incorrect 计入；unknown/异常不烧 flag）
   · max_steps_seen：供 early-exit 判定，避免"0 步误判重跑"
   ↓
HeadlessSolver × N (agents/headless_solver.py)
   · pydantic-ai 2.x Agent + 中性工具面
   · target lock：纯结构白名单，空 host 失败关闭，重定向逐跳复检
   ↓
工具面：http_request / tcp_send / run_python / submit_flag / request_hint /
        task_status / check_findings / notify_coordinator
        （opt-in：remember / recall_memory / record_finding）
```

## 关键不变量（改动这些前必读）

1. **target lock 是纯结构性的**：solver 出网只允许 `connection_info` 解析出的主机。
   三条防线：a) 工具入口 `_target_lock_block`（空 host 一律 BLOCK）；b) `do_raw_http`
   逐跳重定向白名单（越界 302 拒绝跟随）；c) `run_python` 进程内 socket 补丁
   （connect/connect_ex/sendto/sendmsg/create_connection/asyncio 路径；子进程默认拒绝）。
2. **调度器无 LLM**：`coordinator_loop.py` 不含任何模型调用；free-form 文本一律忽略。
3. **"平台判了才算错"**：`swarm.try_submit_flag` 只有 `incorrect` 状态计入错误计数并
   触发熔断；`unknown`/网络异常不烧 flag、不计错、可重试。
4. **成功轮询是结束的充要证据**：调度器只有在 `poller.last_poll_ok` 为真且无未解题目时
   才宣告结束；从未成功轮询过就退出视为失败（RuntimeError），绝不假成功。
5. **usage 账本不漏**：每次 `agent.run` 传入独立 `RunUsage` 累加器，取消/异常路径通过
   `_record_interrupted_usage` 补记 token；`--no-submit` 三层防线（swarm 早退 +
   solver 工具守卫 + deps 标志）。
6. **opt-in 子系统默认关**：跨任务记忆 / findings 输出不开启时零行为差异；
   开启时也只追加写入 `logs/`，不改变任何评分路径。

## 模块职责（一图流）

| 模块 | 职责 | 关键对外接口 |
|---|---|---|
| `config.py` | Settings（.env + 环境变量别名） | `Settings()` |
| `models.py` | provider→pydantic-ai Model 解析 | `resolve_model`, `resolve_model_settings` |
| `poller.py` | 轮询 + 事件队列 + 健康跟踪 | `PlatformPoller` |
| `agents/coordinator_loop.py` | 主循环、REPL、预算、死锁自愈 | `build_deps`, `run_event_loop` |
| `agents/coordinator_core.py` | spawn/reap/kill 生命周期 | `do_spawn_swarm` |
| `agents/swarm.py` | 竞速编排、提交语义、状态快照 | `ChallengeSwarm` |
| `agents/headless_solver.py` | 单 solver 循环、工具注册、救援 | `HeadlessSolver` |
| `agents/tracing_toolset.py` | 工具调用包装：trace + loop 遥测 | `TracingToolset` |
| `tsec_platform.py` | 平台 REST 适配 + 容器生命周期 | `TSecBenchmarkPlatform` |
| `tools/*` | 中性工具实现（无 pydantic-ai 依赖） | `do_raw_http` 等 |
| `memory.py` | opt-in 记忆/产物记录（JSONL） | `CrossTaskMemory`, `FindingsRecorder` |
| `web.py` | 观测/操控层（FastAPI + WS） | `run_web_scheduler` |

## 测试基座

`tests/` 全部为**隔离单元测试**：mock 平台、mock HTTP 客户端、不触网、不调模型。
运行：`python -m uv run pytest`（40 用例）。静态门禁：`ruff check backend tests scripts`、
`ty check backend`（0 诊断）。测试不依赖 TSecBench 平台、VPN 或任何 API Key。

## 依赖与可复现

- Python ≥3.14；依赖下限钉在 pyproject.toml，精确 pin 在 **uv.lock（已入库）**。
- 关键生态：pydantic-ai 2.56.x（API 面：Agent/FunctionToolset/WrapperToolset/
  RunUsage/OpenAIProvider），openai 3.30.x（httpx2 生态），httpx 0.28.x。
- DeepSeek/GLM/DashScope chat 端点强制 profile
  `openai_chat_supports_max_completion_tokens=False`：输出上限必须以 `max_tokens`
  字段上线（否则 provider 拒绝或忽略）。

## 升级记录

十轮升级全过程的缺陷清单、门禁状态与决策记录见 [`upgrade-rounds.md`](upgrade-rounds.md)
与 [`audit-r1.json`](audit-r1.json)。
