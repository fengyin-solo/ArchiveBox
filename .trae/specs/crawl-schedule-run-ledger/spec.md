# CrawlSchedule 持久化运行账本 - 产品需求文档

## Overview
- **Summary**: 为 `CrawlSchedule` 增加持久化的计划运行账本（run ledger）：每个周期开始时在短事务中原子领取下一次计划运行，记录计划时间、实际开始时间、owner 与唯一 run id；领取门禁同时约束 runner 调度循环、多个 runner 实例与管理员 `schedule --run-all`。支持停机跨周期的有界 backfill、时区/DST 显式审计、禁用/模板修改/取消时清理领取状态。
- **Purpose**: 现状调度只在 runner 循环里内存式检查 `is_due()`/`dispatch()`，不持久化 `last_run_at`、`next_run_at` 或运行状态。服务停机跨过多个周期后只会补一次；多个 runner 或管理员手动 run-all 与 runner 并发时可能为同一 schedule 的同一周期重复创建 Crawl；runner 崩溃后无法区分"正在运行"与"从未领取"。
- **Target Users**: 自托管 ArchiveBox 的运维者（CLI/API 管理员、Django admin 用户）、多个 orchestrator/runner 进程共享同一 SQLite/未来 PostgreSQL 的部署。

## Goals
- 每个 schedule 的每个 cron 周期至多产生一次运行（Crawl 或 inline maintenance），并发进程下由数据库唯一约束保证。
- 持久化计划时间（planned_at）、实际开始/结束时间、owner（machine+pid+来源）、唯一 run id、运行状态与关联 Crawl。
- runner 自动调度与管理员 `--run-all` 走同一个领取函数与同一套门禁。
- 停机错过多个周期时只补最近 N 次（每 schedule 可配置 `max_backfill_runs`，默认 1），未补次数与时间范围持久化到 schedule 状态，可审计。
- 显式支持每 schedule 的 IANA 时区；cron 在该时区计算；DST 春季跳时（不存在的本地时间）跳过并记审计行，秋季重复时刻按确定性规则（第一次发生）执行并在账本留痕。
- invalid cron / invalid timezone 在保存时拒绝；存量脏数据在领取时给出可审计错误（schedule 错误状态列 + WARNING 日志），不静默跳过。
- 计划禁用、计划/模板定义修改、任务取消时清理领取状态，不留下永久 claimed 的幽灵行；禁用后重新启用不追补禁用期间周期。
- 已领取周期在上一周期仍在运行时不并发领取：跳过新周期并记账，避免同一 schedule 堆积并发 Crawl。

## Non-Goals
- 不做跨机器的 orchestrator 选举；owner 仅用于审计与同机恢复判断，不引入文件租约或定时器选主（遵循 AGENTS.md 并发契约）。
- 不改变 Crawl/Snapshot 的执行引擎、lease/heartbeat 机制与 hook 行为。
- 不把全局 `TIMEZONE` 常量改为用户可配置；时区粒度是"每个 schedule"。
- 不为账本提供编辑/删除的用户写接口（账本只由调度流程写入；admin 只读展示）。
- 不改 `archivebox schedule` 的既有命令形态（只新增可选项）。

## Background & Context
- `archivebox/crawls/models.py` 中 `CrawlSchedule.last_run_at/next_run_at/is_due/dispatch/enqueue` 是当前全部调度逻辑：`last_run_at` 从最新 Crawl 的 `created_at` 派生，`next_run_at` 用 croniter 即时计算，无任何持久化游标。
- `archivebox/services/runner.py::run_pending_crawls` 在 daemon 分支遍历 `CrawlSchedule.objects.filter(is_enabled=True)`，`is_due(now)` 即 `dispatch(queued_at=now)`；无领取、无去重。
- `archivebox/cli/archivebox_schedule.py` 的 `--run-all` 对每个启用 schedule 直接 `dispatch()`，与 runner 之间无共享门禁。
- 既有并发原语：`ModelWithQueue.claim_for_worker`（retry_at CAS 短更新）、croniter>=6（支持 aware datetime）、`uuid7`/`CompactUUIDField`、`Machine.current().guid` 与 `Process.current()` 提供 owner 身份。
- 全局 `USE_TZ=True`、`TIME_ZONE="UTC"`（`archivebox/config/constants.py`）。
- SQLite 为默认库，要求短事务/autocommit；网络、hook、文件系统工作不得跨事务（AGENTS.md 并发契约）。
- `Crawl.cancel()` 是用户取消入口（admin/API 均经此）；模板 Crawl 的内容编辑主要发生在 `CrawlAdmin` 保存表单时；API `PATCH /crawl/{id}` 只改 status/tags/retry_at。

## Functional Requirements

### 领取与账本
- **FR-1**: 新模型 `CrawlScheduleRun`（表 `crawls_crawlschedulerun`）持久化每次计划运行：`id`(uuid7)、`schedule_id`(CASCADE)、`planned_at`、`started_at`、`finished_at`、`status`、`reason`、`message`、`owner`、`source`、`run_kind`（crawl/update）、`crawl_id`（SET_NULL，可空）、`created_at`/`modified_at`。
- **FR-2**: `(schedule_id, planned_at)` 上存在数据库级唯一约束，作为"同一周期不可重复领取"的最终门禁；并为 `(schedule_id, status)`、`(status, planned_at)` 建索引。
- **FR-3**: `status` 取值：`claimed`（已领取/进行中）、`succeeded`、`failed`、`skipped`、`cancelled`。`reason` 使用稳定机器可读码：`dst_gap`、`dst_ambiguous_first`、`backfill_limited`（仅状态聚合，见 FR-8）、`schedule_disabled`、`schedule_modified`、`template_modified`、`crawl_cancelled`、`crawl_deleted`、`stale_claim`、`invalid_cron`、`invalid_timezone`。
- **FR-4**: 领取在短事务中完成：条件插入账本行（冲突即该周期已被领取，返回 None/跳过，不报错刷屏），crawl-kind 在同一短事务内创建物化 Crawl 并回写 `crawl_id`；事务内不得执行网络/hook/文件系统工作。update-kind 在事务外执行 maintenance，结束后短事务收尾。
- **FR-5**: `CrawlSchedule` 新增持久化游标与状态列：`timezone`（IANA 名，空串=UTC）、`max_backfill_runs`（≥1，默认 1）、`last_planned_run_at`（游标，可空）、`missed_runs_skipped`（累计未补次数）、`last_skip_at/last_skip_count/last_skip_first_at/last_skip_last_at/last_skip_reason`、`last_error_at/last_error_code/last_error_message`。
- **FR-6**: runner daemon 循环与 `--run-all` 都改为调用同一个 `CrawlSchedule.dispatch_due(...)` 服务入口（携带 `source`：`runner` / `manual_run_all` 与 owner 标识）。run-all 对已存在 open claimed 行的 schedule 跳过并在输出/返回结构中报告跳过数；不得为同一 schedule 重复建 Crawl。
- **FR-7**: 同一 schedule 存在任意 `claimed` 行时，不领取后续周期（不并发）；后续到期周期在下次评估时按 backfill 规则处理（最近 N 次补跑，其余计入跳过聚合）。

### Backfill
- **FR-8**: 每次评估以 `last_planned_run_at` 为锚点枚举 `(anchor, now]` 内的 cron 触发时刻（硬上限枚举次数，超限部分同样计入跳过聚合，防止病态表达式海量行）。超过 `max_backfill_runs` 时只领取最近 N 个触发时刻；更早的错过周期**不**逐行落库，而是把次数、首末计划时间、原因（`backfill_limited`）与发生时间累积写入 schedule 跳过状态列。
- **FR-9**: 成功领取一个周期后推进 `last_planned_run_at` 为该周期 planned_at；skipped（DST）周期同样推进游标。枚举/领取过程先在内存计算完整决策，再逐周期短事务落库。

### 时区与 DST
- **FR-10**: `CrawlSchedule.save()` 校验 schedule cron（沿用 `validate_schedule`）与 timezone（zoneinfo 可解析）；非法值抛 ValueError，CLI/API/admin 不得保存成功。
- **FR-11**: cron 触发时刻在 schedule 指定时区（空=UTC）中用 aware datetime 经 croniter 计算并转回 UTC 存储。春季 DST 缺口（本地时刻不存在）：该周期落一条 `skipped/dst_gap` 账本行（含本地墙钟时间与 tz 的 message），推进游标，继续后续周期。秋季重复（ambiguous）时刻：确定性选取第一次发生（fold=0）执行，该行 `reason=dst_ambiguous_first` 且 message 记录解析方式。
- **FR-12**: 存量脏数据（绕过校验写入的非法 cron/timezone）在领取评估时被捕获：schedule 写入 `last_error_*` 列并输出 WARNING 日志，本次跳过该 schedule；下一 tick 重试，错误不静默、不致 daemon 崩溃。

### 清理
- **FR-13**: `is_enabled` 由 true→false 时：该 schedule 的 open `claimed` 行置 `cancelled/schedule_disabled`（含 finished_at），并把 `last_planned_run_at` 重置为当前时刻（重新启用后不追补禁用期间周期，不产生补跑风暴）。
- **FR-14**: schedule 定义字段（`schedule` cron、`timezone`、`config`、`template`、`max_backfill_runs`）在已存在行上被修改时：取消尚未产生 STARTED Crawl 的 open claimed 行（`cancelled/schedule_modified`）；其物化 Crawl 若仍为 queued，一并 seal 取消，避免旧定义幽灵运行；已 STARTED 的运行不受影响。修改后游标设为修改时刻，按新定义重新计时。
- **FR-15**: 模板 Crawl 内容（urls/depth/tags/config 等）经 `CrawlAdmin` 保存修改时，取消关联 schedule 尚未 STARTED 的 open claimed 行（`cancelled/template_modified`，queued 物化 Crawl 一并 seal），下一周期按新模板物化。
- **FR-16**: `Crawl.cancel()` 时，把以该 Crawl 为 `crawl_id` 的 open claimed 账本行置 `cancelled/crawl_cancelled`，使该 schedule 可以领取后续周期。

### 恢复与对账
- **FR-17**: runner 每次评估前对账 claimed 行：关联 Crawl 已 SEALED → `succeeded`（finished_at）；Crawl 被删除（crawl_id 置空且非 update-kind）→ `failed/crawl_deleted`；update-kind 无 Crawl 且 `started_at` 早于陈旧阈值 → `failed/stale_claim`（覆盖进程在领取后、收尾前崩溃）。
- **FR-18**: 迁移 0020 增加新列/新表与唯一约束、索引，并把存量 schedule 的 `last_planned_run_at` 回填为旧语义的 last_run（最新 Crawl created_at，否则 template.created_at，否则 schedule.created_at），升级后行为不突变。

### 接口面
- **FR-19**: `archivebox schedule --add/--every` 支持 `--timezone <IANA>` 与 `--max-backfill <N>` 选项；`--show` 输出 timezone、next_run、open/最近运行状态与错误/跳过计数；`--run-all` 输出 enqueued/maintained/skipped 计数；API `ScheduleCommandSchema` 同步增加可选字段。
- **FR-20**: Django admin 的 CrawlSchedule 编辑页可配置 timezone/max_backfill_runs，并只读展示游标、最近错误、跳过聚合；新增 CrawlScheduleRun 只读 admin（可按 schedule/status/reason 过滤）用于审计。

## Non-Functional Requirements
- **NFR-1（并发正确性）**: 并发领取完全由数据库行/唯一约束裁决，不依赖进程内锁、flock 或 sleep；SQLite 与 PostgreSQL 行为一致（短 autocommit/CAS 语句，禁止长事务包裹网络/hook/FS 工作）。
- **NFR-2（可观测性）**: 每次领取、跳过、取消、失败均有账本行或 schedule 状态聚合可查；异常路径使用 `logging` WARNING 及 CLI 可见输出。
- **NFR-3（向后兼容）**: 既有 CLI/API/UI 用法与输出关键文案保持兼容（如 `Enqueued 1 scheduled crawl`、result 字典既有键保留，新键为追加）；迁移可在现有 SQLite 库上无数据损失执行。
- **NFR-4（性能）**: 每个 daemon tick 的 schedule 评估保持有界：枚举有硬上限、账本写入为逐行短事务、查询走索引；schedule 数量为常见自托管规模（数十至数百）。
- **NFR-5（可测试性）**: 测试使用真实 Django ORM 行、真实子进程 CLI、真实线程并发与真实唯一约束冲突，禁止 mock/monkeypatch/fake（遵循 AGENTS.md）。

## Constraints
- **Technical**: Python 3.12+、Django ORM、croniter>=6（aware 支持）、zoneinfo（tzdata 已在依赖中）、SQLite 优先；使用 `uv run pytest`；模型风格沿用 `ModelWithUUID`/CompactUUIDField/uuid7。
- **Business**: 不发布包/镜像/tag；不改 release 流程；保持 `dev` 分支。
- **Dependencies**: `crawls` app 既有迁移链（最新 0019）；runner daemon 循环；CrawlAdmin 保存路径。

## Assumptions
- Backfill 默认 N=1（与现状"补一次"一致），每个 schedule 可单独调大（1..100）。
- 同一 schedule 上一周期 claimed/running 时，新周期不并发、被跳过并按 backfill/聚合规则记账。
- 禁用再启用不追补禁用期间周期（锚点重置为禁用时刻）。
- DST 春季缺口跳过+审计行；秋季重复按 fold=0 执行+审计标记。
- owner 字符串格式 `{machine_guid}:{pid}:{source}`，仅用于审计；不据此做跨机互斥判定。
- `--run-all` 的语义是"领取每个启用 schedule 的下一个计划周期提前执行"，而非绕过账本强制建 Crawl；已有 open claimed 行则跳过。

## Acceptance Criteria

### AC-1: 账本行模型与唯一约束
- **Type**: `rule`
- **Given**: 一个启用的 CrawlSchedule
- **When**: 在事务中为同一 `(schedule, planned_at)` 并发/重复插入两条 CrawlScheduleRun
- **Then**: 只有一条插入成功，另一条因唯一约束失败（IntegrityError），且不产生第二个 Crawl
- **Pass Condition**: 模型含 FR-1 全部字段；迁移后数据库存在唯一约束；直接重复插入触发 IntegrityError
- **Evidence**: 迁移文件；`uv run pytest` 中真实 ORM 并发测试的通过输出与 DB 行数断言

### AC-2: 跨周期停机只补一次（默认）
- **Type**: `rule`
- **Given**: hourly schedule，游标被置于 5 个周期之前，max_backfill_runs=1
- **When**: runner 执行一次 dispatch_due 评估
- **Then**: 恰好创建 1 个新 Crawl、1 条 claimed→succeeded（或对账后终态）账本行；missed_runs_skipped 增加 4，last_skip_* 记录首末计划时间与 backfill_limited
- **Pass Condition**: Crawl 新增数=1；账本 claimed/终态行数=1；schedule 跳过聚合列值正确
- **Evidence**: ORM 测试断言 DB 行与字段值

### AC-3: backfill 上限可配置且只补最近 N 次
- **Type**: `rule`
- **Given**: 同上但 max_backfill_runs=3，游标位于 10 个周期前
- **When**: 一次 dispatch_due 评估
- **Then**: 领取最近 3 个周期（planned_at 为最近 3 个触发时刻），创建 3 个 Crawl，missed_runs_skipped=7 且 last_skip_first_at/last_at 指向最早 7 个错过周期
- **Pass Condition**: 新建 Crawl 数=3；3 行账本 planned_at 与 croniter 独立计算的最近 3 个时刻一致；聚合列值=7
- **Evidence**: ORM 测试

### AC-4: 重复 runner 并发不重复领取
- **Type**: `rule`
- **Given**: 一个 due 的 schedule，两个真实线程（独立 DB 连接）同时调用 dispatch_due
- **When**: 并发执行并汇合
- **Then**: 该周期仅 1 条账本行、仅 1 个物化 Crawl；另一方得到"已领取"结果而非异常/重复行
- **Pass Condition**: 账本行数=1、Crawl 数增加=1；无 IntegrityError 泄漏到调用方
- **Evidence**: 真实线程并发测试（barrier + 独立连接）输出

### AC-5: 手动 run-all 遵守同一门禁
- **Type**: `rule`
- **Given**: 启用 schedule（刚创建，无 open 行）；分两次执行 `archivebox schedule --run-all`（子进程），或 run-all 与 runner 并发
- **When**: 第一次 run-all 后该 schedule 存在 claimed/open 行，再执行第二次
- **Then**: 第一次 enqueued=1；第二次 skipped=1、enqueued=0、Crawl 总数不增加；输出含跳过信息
- **Pass Condition**: 两次 CLI 退出码与输出计数符合；DB 中 Crawl 数不增加；result 字典含 skipped 键
- **Evidence**: `test_cli_schedule.py` 真实 CLI 子进程测试

### AC-6: 上一周期运行中不并发领取新周期
- **Type**: `rule`
- **Given**: schedule 有一个 claimed 行且其 Crawl 处于 queued/started
- **When**: 时间推进跨过下一个 cron 周期后执行评估
- **Then**: 不创建新 Crawl/新 claimed 行；被越过周期在 open 行结束后的评估中按 backfill 规则处理
- **Pass Condition**: 越过周期评估时新 Crawl 数=0；open 行结束后再评估，行为符合 FR-7/FR-8
- **Evidence**: ORM 测试

### AC-7: 时区按 schedule 配置生效
- **Type**: `rule`
- **Given**: schedule 配置 `timezone=America/New_York`、cron=`0 9 * * *`，游标在某已知日期
- **When**: 计算 next/due 时刻
- **Then**: 计划时间对应纽约本地 09:00（UTC 存储随夏令时在 13:00/14:00 间切换）
- **Pass Condition**: 账本 planned_at（UTC）与独立 zoneinfo/croniter 计算一致，覆盖 EDT/EST 两侧日期
- **Evidence**: ORM/单元测试

### AC-8: DST 春季缺口跳过并审计、秋季重复确定性执行
- **Type**: `rule`
- **Given**: America/New_York 下 cron 分别为 `30 2 * * *`（2026-03-08 缺口）与 `30 1 * * *`（2026-11-01 重复）
- **When**: 评估窗口跨过对应日期
- **Then**: 缺口周期产生 `skipped/dst_gap` 账本行（message 含本地时刻与 tz），游标推进；重复周期仅执行一次（fold=0，UTC-4 侧），账本行 `reason=dst_ambiguous_first` 且有 message
- **Pass Condition**: 两条场景的账本 status/reason/planned_at 与 message 断言均成立，无重复 Crawl
- **Evidence**: ORM 测试

### AC-9: invalid cron/timezone 保存拒绝与存量脏数据审计
- **Type**: `rule`
- **Given**: (a) CLI/API/admin 尝试保存非法 cron 或非法 IANA 时区；(b) DB 中存在绕过校验的脏 schedule
- **When**: (a) 执行保存；(b) runner 执行评估
- **Then**: (a) 保存失败并返回 "Invalid schedule"/时区错误信息，不落库；(b) schedule 写入 last_error_at/code/message 并产生 WARNING，daemon 不崩溃、下次 tick 可恢复
- **Pass Condition**: CLI 退出码非 0 且输出含错误；脏 schedule 评估后 last_error_* 非空且无 Crawl/账本脏行
- **Evidence**: CLI 测试 + ORM 测试 + 日志断言

### AC-10: 禁用清理领取状态且不追补
- **Type**: `rule`
- **Given**: schedule 有 open claimed 行（queued 物化 Crawl）
- **When**: 将 is_enabled 置 false；推进时间跨多个周期；再置回 true 并评估
- **Then**: open 行变 cancelled/schedule_disabled（有 finished_at），queued Crawl 被 seal；last_planned_run_at≈禁用时刻；重新启用后不补跑禁用期间周期，只领取未来周期
- **Pass Condition**: 账本状态、Crawl 状态、游标时间、再启用后 Crawl 增量断言全部成立
- **Evidence**: ORM 测试

### AC-11: 计划/模板修改清理未开始的领取
- **Type**: `rule`
- **Given**: schedule 有 open claimed 行 A（Crawl queued，未 STARTED）
- **When**: (a) 修改 cron/timezone/config/template/max_backfill；或 (b) 经 CrawlAdmin 保存修改模板 urls；之后评估
- **Then**: A 行变 cancelled/schedule_modified 或 template_modified，queued Crawl 被 seal；新周期按新定义/新模板物化（新 Crawl 内容反映修改）；已 STARTED 的 claimed 行不受影响
- **Pass Condition**: 账本/旧 Crawl 状态与新 Crawl 字段（urls）断言成立；STARTED 场景行保持 claimed
- **Evidence**: ORM 测试（含 admin save_model 路径或 CrawlAdmin 保存的真实调用）

### AC-12: 任务取消释放领取
- **Type**: `rule`
- **Given**: schedule 有 claimed 行关联 queued/started Crawl
- **When**: 调用 `Crawl.cancel()`（admin/API 同路径）
- **Then**: 账本行变 cancelled/crawl_cancelled 且 finished_at 有值；下次评估可领取新周期
- **Pass Condition**: 行状态/原因/时间戳断言；取消后再评估可创建下一周期 Crawl
- **Evidence**: ORM 测试

### AC-13: 崩溃恢复与对账
- **Type**: `rule`
- **Given**: (a) claimed 行关联的 Crawl 已 SEALED；(b) claimed 行 crawl_id 为空（update-kind，started_at 陈旧超阈值）
- **When**: runner 执行对账
- **Then**: (a) 行变 succeeded（finished_at 有值）；(b) 行变 failed/stale_claim；schedule 随后可再次领取
- **Pass Condition**: 两类行的终态、reason、finished_at 断言成立
- **Evidence**: ORM 测试

### AC-14: 迁移与存量数据
- **Type**: `rule`
- **Given**: 含 0019 及以前数据的库（有 schedule 与历史 Crawl）
- **When**: 执行 migrate 到 0020
- **Then**: 新表/列/约束/索引存在；每个存量 schedule 的 last_planned_run_at 按 FR-18 回填；迁移后既有 schedule/CLI 测试全部通过
- **Pass Condition**: `makemigrations --check` 无缺额；迁移后字段非空规则与回填值抽查正确
- **Evidence**: 迁移文件 + 迁移后测试输出

### AC-15: CLI/API/admin 配置与审计面
- **Type**: `rule`
- **Given**: 管理员
- **When**: 通过 CLI `--timezone/--max-backfill`、API schema、admin 页面配置并查看
- **Then**: 三处均可设置时区/backfill；`--show` 显示 next_run/tz/状态；CrawlScheduleRun admin 可按 schedule/status/reason 查看账本
- **Pass Condition**: CLI 帮助含新选项且创建成功；schema 含新字段；admin 字段集/只读展示存在
- **Evidence**: CLI help/API 单测/admin 测试

### AC-16: 端到端 due 计划真实出片不回归
- **Type**: `rule`
- **Given**: 现有 `test_schedule_due_crawl_runs_over_server_and_saves_real_content` 场景
- **When**: make due 后启动 server
- **Then**: 经新账本路径领取并完成真实快照抓取，只产生一次计划 Crawl
- **Pass Condition**: 既有端到端测试不加弱化修改通过，且 DB 中账本行状态为 succeeded/等价终态
- **Evidence**: 真实 server + HTTP 测试输出

### AC-17: 实现与仓库模式的契合度
- **Type**: `rubric`
- **Dimension**: 代码遵循仓库既有模式（短 CAS/autocommit、claim_for_worker 风格、模型/util 分层、无多余抽象层、无 mock），命名、日志、admin/CLI 风格一致
- **Scale**: 1-5
- **Anchors**: 1 = 引入长事务/睡眠选主/mock 或大段重复代码；3 = 功能正确但有少量风格偏离或可合并的重复；5 = 完全契合既有模式、最小聚焦改动
- **Pass Threshold**: >= 4
- **Evidence**: 独立审查阅读 diff 与对照 `workers/models.py`、runner 调度块的评价

## Open Questions
- 无（用户选择采用推荐默认：backfill 默认 1、运行中跳过、禁用重置锚点、DST 缺口跳过）。如实现中发现 PostgreSQL 特定 DDL 不兼容点，在 tasks.md 记录决策。
