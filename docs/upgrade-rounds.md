# 升级轮次记录（Upgrade Rounds）

> 本文件记录 `upgrade/quality-and-features` 分支的十轮自完善过程。
> 每轮结束时追加结果与门禁状态。审计/修复以**正确性第一**为优先级。

## 总目标

1. 依赖升级到最新稳定版并验证兼容（pydantic-ai 2.x / openai 3.x / httpx2）。
2. 质量加固：pytest 测试基座、ruff/ty 静态检查全绿、错误处理与类型标注完善。
3. 新功能：findings 漏洞产物输出通道（rewrite-plan §6 待拍板项，已拍板：做）。
4. 新功能：跨任务记忆子系统（**opt-in**，默认关闭；与 rewrite-plan 原则 1 的冲突已获拍板，
   按基线文档规则"先改文档再改码"执行，见 R8）。
5. 记忆沉淀：仓库内文档（ADR / 架构 / 决策记录）+ Claude Code 持久记忆库。
6. 交付：分支推送 well-lzz/chronos 并开 PR。

## 环境事实（2026-10-14）

- Python 3.14.7；uv 0.13.0（`python -m uv` 调用）；无 gh CLI（PR 走 GCM 凭证 + REST API）。
- pydantic-ai 2.56.0、openai 3.30.0（httpx2 生态）、httpx 0.28.1、fastapi 0.143.0。
- 办公网约束：**仅静态开发**，不进行任何渗透/实测；网络工具仅用于下载依赖与 git 操作。
- 基线门禁：`ruff check` 通过；`ty check` 4 个诊断；22 个模块 import 全部通过。

## 轮次

| 轮 | 主题 | 状态 | 门禁 |
|---|---|---|---|
| R1 | 全量代码审计（6 路审计 + 对抗验证） | 进行中 | — |
| R2 | 修复高危/中危缺陷（正确性优先，逐项带测试） | 待办 | pytest 绿 |
| R3 | 依赖锁定与工具链（uv.lock 入库、版本 pin、pytest 配置） | 待办 | ruff+ty 绿 |
| R4 | 测试基座（全模块 pytest 套件） | 待办 | pytest 全绿 |
| R5 | 静态检查清零（ty 4 诊断 → 0） | 待办 | ty 0 诊断 |
| R6 | 对抗性交叉验证（R2–R5 改动的独立复审） | 待办 | 验证通过 |
| R7 | findings 漏洞产物输出通道（含基线文档更新） | 待办 | 新测试绿 |
| R8 | 跨任务记忆子系统（opt-in；基线文档先行修订） | 待办 | 新测试绿 |
| R9 | 文档沉淀（ADR/架构/升级日志/README）+ 完备性批判 | 待办 | 文档-代码一致 |
| R10 | 终验门：全量测试 + 静态检查 + PR 文案 + 推送开 PR | 待办 | 全部门禁 |

## R1 结果

全量审计完成：9 个 agent，107 万 subagent tokens，6 路审计 + 3 路对抗验证。
**94 条确认缺陷**：high 7 / medium 34 / low 53。分类：correctness 44、safety 10、
concurrency 7、missing-test 10、api-compat 2、maintainability 7、dead-code 10、docs 4。

## R2 结果（进行中）

正确性优先，逐项带测试。已修复并通过测试（24 tests green, ruff green）：

**高危（7/7 已修复）：**
- H1 空/不可达题目列表被误判为"全部已解" → poller 增加 `last_poll_ok`/`consecutive_failures`，
  调度器终止条件要求"至少一次成功轮询"，否则 raise RuntimeError（不再假成功退出）。
- H2 target-lock 空 hostname 失败放行 + `//host` URL 走私 → `_target_lock_block` 对空 host 一律 BLOCK；
  `_parse_allowed_hosts` 支持 IPv6 括号主机。
- H3/H4 `--no-submit` 干跑全链路失效（真实提交 flag）→ `no_submit` 打通
  `ChallengeSwarm → HeadlessSolver → HeadlessDeps`，并在 `try_submit_flag` 加防御性早退。
- H5 `unknown` 状态被当作猜错计入熔断 → 仅 `incorrect` 计错；`unknown`/异常不烧 flag、不进熔断。
- H6 `get_event` 吞掉 `CancelledError` 致关停挂起 → 只吞 timeout，让取消传播。
- H7 HTTP 重定向绕过 target-lock → `do_raw_http` 改显式逐跳重定向 + 每跳白名单复检，
  越界重定向拒绝跟随。

**中危（已修复 6）：**
- M 早退重试恒 0 步（`swarm.run` 丢弃非胜结果）→ 已在修复清单（R2 尾部）……
- M 平台 URL 优先级（`--benchmark-base-url` 无效果）→ 待修
- M 死锁判定把 stale deferred 也算、spawn_failures 误重置 → 待修
- M 中断运行的 token 不计费 → `usage` 累加器传入 run + 中断路径补录
- M 确认 flag 在异常路径被丢弃 → 异常路径镜像取消守卫
- M 救援自动提交喂熔断 → `RESCUE_MAX_SUBMITS = 3` 上限
- M bump 中途落地被吞 → 改为 `_pending_bump` 延迟注入
- M 兄弟确认后 task_status 冻结 → `_push_flag_progress` 同步到各 solver deps
- M cost_tracker provider id 错误（qwen/glm 成本算 0）→ 待修
- M `max_tokens=128000` 超 DeepSeek 上限、映射到 `max_completion_tokens` → 已修
  （DeepSeek profile 固定 `max_tokens`，上限 8192/reasoner 65536）

## 调整后的轮次计划（用户 2026-10-14 二轮确认）

- **R1 全量审计** ✅（94 条缺陷入库 docs/audit-r1.json）
- **R2 高危/中危修复** ✅（7 high + 6 medium，24 tests）
- **R3 缺陷清零**：清完剩余中危 + 高价值低危（安全/正确性优先），静态修复
- **R4 两个新功能**：findings 漏洞产物输出通道 + opt-in 跨任务记忆子系统（先修订 rewrite-plan 基线，再写码；全部默认关闭，不改变参评行为）
- **R5 文档沉淀 + 对抗复核**：ADR、README、env.example 同步；只读批判 agent 复核整个 diff
- **R6 终验与交付**：静态检查（ruff/ty）、提交序列、推送分支、准备 PR

**硬性边界（用户明确要求，全程遵守）**：
- 办公环境，**禁止任何网络测试与实测**：不调用 DeepSeek/任何模型 API、不探测平台、
  不连 VPN、不访问 tsecbench 平台页面、不在本地真实运行 Agent 去打靶。
- 允许：**隔离单测**（pytest 全 mock，不触网、不调模型、不连平台）、静态分析工具
  （ruff/ty）、静态开发（编辑代码/文档）、git 操作与 GitHub 下载（用户授权）。
- 测试以隔离单测为主；任何需要真实平台/真实模型才能验证的行为，在 PR 中如实标注
  "未经实测"边界，绝不越界去跑。

用户澄清：该 Agent 是 TSecBench 平台（https://tsecbench.zc.tencent.com/agent/20786）的
**Agent 测评**对象；用户要求：
1. 先"grillme 一轮"（逼问/拷问一轮，弄清被测对象与需求）。
2. 再判断精细化几轮进行完善。
3. **不要用用户本地环境进行测试**（本地环境即用于实测测评的平台/VPN，绝不能跑）。
   这与既有办公网约束一致：全程静态开发 + 本地单测（隔离 mock，不触网），绝不实测。
