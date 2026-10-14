# 升级：正确性修复 + 缺陷清零 + 两个 opt-in 新功能

对 `chronos` 做了一次以**正确性第一**的全量升级：先全库审计（94 条确认缺陷），逐条修复并配回归测试，再做对抗性交叉复审（28 条确认，含 2 条本升级引入的高危回归，已全部修复）。全程**仅静态开发 + 隔离 mock 单测**，未对平台/VPN/模型做任何实测。

## 变更概览

**高危修复（部分）**
- 空/不可达题目列表不再被误判为"全部已解"：调度器只有在**至少一次成功轮询**后才可能宣告结束，否则报错退出。
- target-lock 空 hostname 失败关闭（封堵 `//host` URL 走私）+ HTTP 重定向逐跳白名单复检。
- `--no-submit` 干跑全链路打通（此前真实提交 flag，与契约相反）。
- `unknown` 提交状态（平台/网络错误）不再计入错误猜测，不会误触 swarm 熔断。
- poller 取消语义修复（`CancelledError` 不再被吞，Web 控制台关停不挂起）。

**中/低危修复**
- 中断/失败运行的 token 计费不再丢失；确认 flag 在异常路径不再被丢弃。
- `max_tokens` 修正为各 provider 真实上限，并固定 `max_tokens` 线上字段（DeepSeek/GLM/Qwen chat 端点）。
- cost tracker 移除 genai-prices 里不存在的 provider id（qwen/glm 成本不再静默算 0）。
- run_python 守卫：connect_ex/sendto/sendmsg/asyncio 路径补齐；子进程逃逸默认拒绝；mkstemp fd 泄漏、Windows 超时 taskkill 同步阻塞修复。
- 平台适配器：TLS 校验默认开启（可 opt-out）、重复 /start 防重、4xx "already closed" 判成功、索引按最新列表重建、Qwen 默认 `chat` 模式等。
- Web 控制台：平台字段 XSS 转义、`preparing` 过滤器、trace 事件兜底、WS 队列有界。

**新功能（opt-in，默认关闭，不改变任何跑分行为）**
- 跨任务记忆 `remember` / `recall_memory`（`CROSS_TASK_MEMORY_ENABLED=1`）——纯字符串召回，无 RAG、无 LLM 协调，永不自动注入系统提示词。
- 漏洞产物输出 `record_finding`（`FINDINGS_OUTPUT_ENABLED=1`）——写入 `logs/findings-<题目>.jsonl`，纯观测产物，不接评分。
- 两者均为对 `rewrite-plan.md` 原则 1 的**有意识、受限豁免**，基线文档已同步更新（"已拍板"）。

## 质量门禁

- **42 个隔离单测通过**（mock 平台/模型/HTTP，不触网）
- `ruff check backend tests scripts` 通过
- `ty check backend` **0 诊断**
- 依赖下限钉在 `pyproject.toml`，精确 pin 在 **uv.lock（已入库）** 保证可复现安装

## 关键文件

- [docs/audit-r1.json](docs/audit-r1.json) — R1 全量审计 94 条缺陷清单
- [docs/upgrade-rounds.md](docs/upgrade-rounds.md) — 升级轮次记录与门禁状态
- [docs/architecture.md](docs/architecture.md) — 升级后架构与关键不变量
- [docs/rewrite-plan.md](docs/rewrite-plan.md) — 设计基线（新增"已拍板"段）

## 测试说明（诚实边界）

测试均为隔离 mock 单测，**未对 TSecBench 平台、VPN 或任何模型 API 做实测**（办公环境约束）。需要真实环境才能验证的行为（平台 API 字段名、模型接口语义、容器生命周期时序）在 PR 中如实标注为"未经实测"。

---

*提交：6 commits，+~4900/-~250 行 vs `main`。*
