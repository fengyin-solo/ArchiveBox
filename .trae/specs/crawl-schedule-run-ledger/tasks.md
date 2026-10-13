# CrawlSchedule 持久化运行账本 - 实施计划

所有任务在 `dev` 分支上进行；Python 命令一律 `uv run`。每项先写/同步红测试再实现（测试请求要求且可行时）。测试禁用 mock，使用真实 ORM 行、真实子进程、真实线程并发。

## Task 1: 时区/cron 计算工具与 DST 显式处理
- **Status**: `completed`
- **Priority**: high
- **Depends On**: None
- **Completion Evidence**:
  - `archivebox/crawls/schedule_util.py` 新增 validate_timezone/get_schedule_timezone/PlannedRun/iter_planned_runs/next_run_for_schedule(tzname) 与全部 reason 常量；采用 naive 本地墙钟迭代+zoneinfo fold 显式分类（实测 croniter 6.2.4 aware 模式会静默改点/重复发，naive 方案规避）。
  - `archivebox/tests/test_crawl_schedule_runs.py` 8 个用例全绿：`uv run pytest archivebox/tests/test_crawl_schedule_runs.py -q` → 8 passed（EST/EDT、spring gap、fall ambiguous、截断、非法 tz/cron）。
- **Description**:
  - 在 `archivebox/crawls/schedule_util.py` 增加：`validate_timezone(name)`（空串→""，非法 IANA 名抛 ValueError）、`get_schedule_timezone(name)`（空→`django.conf.settings.TIME_ZONE` 对应的 ZoneInfo）、`iter_planned_runs(schedule, tzname, anchor, now, *, max_enumerate=10000)` 生成 `(planned_at_utc, dst_note)`：croniter 以 aware 锚点在 schedule 时区计算；用 PEP 495 fold/utcoffset 比较显式检测 gap（不存在）与 ambiguous（重复）；gap 产出 skip 标记，ambiguous 取 fold=0；枚举超过硬上限时返回截断标记与总数。
  - 保留 `normalize_schedule/validate_schedule/next_run_for_schedule` 兼容签名；`next_run_for_schedule` 支持可选 tzname。
  - 定义稳定 reason 常量（dst_gap / dst_ambiguous_first / backfill_limited / invalid_cron / invalid_timezone 等，可放 schedule_util 或 models 常量，后续任务复用）。
- **Acceptance Criteria Addressed**: AC-7, AC-8, AC-9
- **Test Requirements**:
  - `rule` TR-1.1: America/New_York `0 9 * * *` 在 2026-01（EST）与 2026-07（EDT）的 planned UTC 分别为 14:00/13:00，与独立 zoneinfo+croniter 计算一致；证据：pytest 输出。
  - `rule` TR-1.2: `30 2 * * *` 枚举跨过 2026-03-08 产出 dst_gap 标记且本地墙钟为 02:30；`30 1 * * *` 跨过 2026-11-01 仅产生一次 fold=0（05:30 UTC）计划并带 dst_ambiguous_first 标记；证据：pytest 断言。
  - `rule` TR-1.3: `validate_timezone("Mars/Olympus")` 抛 ValueError；空串合法；`validate_schedule("nonsense")` 抛 ValueError；证据：pytest 断言。

## Task 2: CrawlScheduleRun 账本模型与 CrawlSchedule 状态列
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 1
- **Completion Evidence**:
  - `archivebox/crawls/models.py` 新增 CrawlScheduleRun（唯一约束 csrun_sched_planned_uniq + 两索引）与 CrawlSchedule 12 个游标/状态列；save() 校验 tz/cron；next_run_at/last_run_at/is_due 改走持久化游标+时区。
  - 测试见 test_crawl_schedule_runs.py：唯一约束 IntegrityError、非法 tz/cron 保存拒绝、next_run 时区/回退均通过（27 用例全绿）。
- **Description**:
  - 在 `archivebox/crawls/models.py` 新增 `CrawlScheduleRun`（FR-1 全部字段）：id CompactUUIDField(uuid7)、schedule FK(CASCADE, related_name="runs")、planned_at(db_index)、started_at、finished_at(null)、status TextChoices(claimed/succeeded/failed/skipped/cancelled)、reason CharField(32,blank)、message TextField(blank)、owner CharField(128,blank)、source CharField(32,blank)、run_kind CharField(16)、crawl FK(Crawl, SET_NULL, null, blank, related_name="schedule_runs")、created_at；Meta 约束 `UniqueConstraint(fields=["schedule","planned_at"], name="crawlschedulerun_schedule_planned_uniq")`，索引 (schedule,status)、(status,planned_at)。
  - CrawlSchedule 增列（FR-5）：timezone CharField(64,blank,default="")、max_backfill_runs PositiveSmallIntegerField(default=1, validators 1..100)、last_planned_run_at DateTimeField(null,blank,db_index)、missed_runs_skipped PositiveBigIntegerField(default=0)、last_skip_at(null)/last_skip_count(PositiveBigIntegerField default 0)/last_skip_first_at(null)/last_skip_last_at(null)/last_skip_reason CharField(32 blank default)、last_error_at(null)/last_error_code CharField(64 blank)/last_error_message TextField(blank)。
  - `save()` 增加 timezone 校验与 cron 校验复用；无效即 ValueError（不落库）。
  - 重写 `last_run_at/next_run_at/is_due` 基于 last_planned_run_at（回填前回退 created_at），next_run 走 schedule 时区；保留属性名兼容 admin/CLI 展示。
- **Acceptance Criteria Addressed**: AC-1, AC-7, AC-9, AC-14
- **Test Requirements**:
  - `rule` TR-2.1: 同 (schedule, planned_at) 第二条 `CrawlScheduleRun.objects.create` 抛 IntegrityError；证据：ORM 测试。
  - `rule` TR-2.2: CrawlSchedule(timezone="Mars/X") / schedule="bogus" 保存抛 ValueError；证据：ORM 测试。
  - `rule` TR-2.3: next_run_at 在 tz=America/New_York 下按本地时区计算；游标为空时回退 created_at；证据：ORM 测试。

## Task 3: 迁移 0020（schema + 约束 + 游标回填）
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 2
- **Completion Evidence**:
  - `0020_schedule_run_ledger.py`：12 AddField + CreateModel(含约束/索引) + RunPython 回填；回滚可用。
  - `makemigrations --check` 无缺额；真实升级演练：0019 库插入 2 条旧 schedule（crawl-kind 有 3 天前物化 Crawl、update-kind 无 Crawl）后升 0020，crawl 游标=最新物化 Crawl created_at、update 游标=modified_at、默认 N=1。
- **Description**:
  - `archivebox/crawls/migrations/0020_schedule_run_ledger.py`：AddField 全部新列；CreateModel CrawlScheduleRun + 唯一约束与索引；RunPython 回填存量 schedule 的 last_planned_run_at = 最新关联 Crawl created_at（update-kind 用 modified_at 旧语义）否则 template.created_at 否则 schedule.created_at；reverse 为 noop + RemoveField/RemoveModel 可回滚结构（沿用仓库迁移风格，支持 sqlite/postgres）。
  - `uv run python -m django makemigrations --check --dry-run` 无缺额；`uv run pytest` 既有迁移相关测试通过。
- **Acceptance Criteria Addressed**: AC-14
- **Test Requirements**:
  - `rule` TR-3.1: 在已初始化 data 目录上 `archivebox migrate`（或 pytest 迁移建表）成功，`crawls_crawlschedulerun` 表与唯一索引存在；存量 schedule 游标回填值正确；证据：命令输出 + SQL/ORM 断言。
  - `rule` TR-3.2: `makemigrations --check` 退出 0；证据：命令输出。

## Task 4: 领取服务：事务性 claim + dispatch + backfill 决策 + 对账
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 2, Task 3
- **Completion Evidence**:
  - models.py 实现 evaluate/dispatch_due/claim_run/reconcile_runs/对账/backfill 聚合/owner；锁冲突（sqlite locked/pg lock/deadlock）在 dispatch_due 记 locked 跳过不崩溃；update-kind maintenance 在事务外执行。
  - test_crawl_schedule_runs.py 27 用例全绿（含真实两线程 Barrier 并发、5 周期/10 周期 backfill、busy 阻塞+seal 后续跑、DST gap/ambiguous、脏 cron/tz last_error 与恢复、sealed/stale 对账）。
- **Description**:
  - 新增 owner 标识 helper（machine.guid+pid+source，截断 128）。
  - 在 CrawlSchedule 上实现核心方法（models.py 内，避免多余分层）：
    - `reconcile_runs(now)`：claimed→succeeded（crawl SEALED）、failed/crawl_deleted（crawl 没了且非 update）、failed/stale_claim（update-kind 无 crawl 且 started_at 超 STALE 阈值，常量如 1800s）。
    - `has_open_claim()` / open claim 查询。
    - classmethod `dispatch_due(*, now=None, source=..., schedules_qs=None) -> dict`：遍历启用 schedule；每 schedule：捕获 cron/tz 异常→写 last_error_* + logger.warning；有 open claimed→跳过（FR-7）；枚举 due 触发时刻（内存决策，DST gap→skipped 行+推进游标，ambiguous→reason 标记行）；超 max_backfill 的更早周期写聚合列（missed_runs_skipped/last_skip_*），逐周期短事务 claim：
    - 实例方法 `claim_run(planned_at, dst_note, source, now)`：atomic 内 `get_or_create`/INSERT 冲突即返回 None；crawl-kind 同事务内复用 `enqueue()` 逻辑建 Crawl 并写 run.crawl_id；update-kind 仅插入 claimed 行，事务外执行 `run_scheduled_maintenance()`，再短事务置 succeeded/failed（异常→failed + message，daemon 不崩）。
    - 成功后短事务推进 last_planned_run_at、清 last_error_*（成功时）。
  - 任何网络/FS/hook 工作不得在事务内。
  - 保留 `dispatch()/enqueue()` 的兼容包装或更新全部调用方（runner/CLI）。
- **Acceptance Criteria Addressed**: AC-1, AC-2, AC-3, AC-4, AC-6, AC-8, AC-9, AC-13
- **Test Requirements**:
  - `rule` TR-4.1: 游标前 5 周期、N=1：1 Crawl、1 终态账本、skipped 聚合=4（对应 AC-2）。
  - `rule` TR-4.2: 游标前 10 周期、N=3：3 Crawl、3 行账本 planned_at=最近 3 个触发时刻、聚合=7（对应 AC-3）。
  - `rule` TR-4.3: 两线程独立连接 barrier 并发 dispatch_due：总新增 Crawl=1、账本行=1、无异常泄漏（对应 AC-4）。
  - `rule` TR-4.4: open claimed（queued crawl）时跨周期评估：新增 Crawl=0；其 seal 后下轮评估可领取（对应 AC-6/AC-13a）。
  - `rule` TR-4.5: DST gap→skipped/dst_gap 行+游标推进；ambiguous→单运行+dst_ambiguous_first（对应 AC-8）。
  - `rule` TR-4.6: 脏 cron/tz schedule：last_error_* 写入、WARNING、无崩溃；修复后下轮正常（对应 AC-9）。
  - `rule` TR-4.7: claimed+crawl SEALED→succeeded；陈旧 update claimed→failed/stale_claim（对应 AC-13）。
  - `rule` TR-4.8: 枚举硬上限：分钟级 cron 游标前推 1 年不产生海量行，聚合列记录超限次数；证据：运行时长与行数断言。

## Task 5: 清理语义——禁用、定义修改、取消
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 4
- **Completion Evidence**:
  - save() 全字段读取旧值判定 true→false 禁用（取消 open claim+游标重置）、false→true 启用（仅重置游标，不追补）、定义变更（取消未 STARTED claim+重置）；disable_many 批量路径；Crawl.cancel() 联动 crawl_cancelled；CrawlAdmin.save_model 模板修改钩子（只封 QUEUED 行，STARTED 保留）。
  - test_crawl_schedule_runs.py 5 个清理用例 + test_ui_admin_crawl.py 真实 ModelAdmin save_model 用例全绿。
- **Description**:
  - CrawlSchedule：实现 `cancel_open_claims(reason, now, *, seal_queued_crawls=True)`（仅取消 crawl 非 STARTED 的 open 行；queued 物化 Crawl 调 `cancel()` seal；写 finished_at/reason/message）。
  - `save()` 中检测：is_enabled True→False：cancel_open_claims(schedule_disabled) + 游标重置为 now；定义字段（schedule/timezone/config/template/max_backfill_runs）变更：cancel_open_claims(schedule_modified) + 游标重置 now（需在更新前加载旧值对比；注意 save 内既有 template 回写逻辑）。
  - `Crawl.cancel()`：末尾（短事务/条件 update 后）将 crawl.schedule_runs 中 open claimed 行置 cancelled/crawl_cancelled + finished_at。
  - `CrawlAdmin.save_model`（archivebox/crawls/admin.py）：模板 Crawl 内容保存后，若存在 `CrawlSchedule.objects.filter(template_id=crawl.pk)`，对其调用 cancel_open_claims(template_modified)；仅在内容字段实际变化时触发，避免误杀。
- **Acceptance Criteria Addressed**: AC-10, AC-11, AC-12
- **Test Requirements**:
  - `rule` TR-5.1: 禁用：open 行 cancelled/schedule_disabled+finished_at、queued crawl sealed、游标≈now；推进 3 周期后重启用评估：0 补跑、只领未来周期（AC-10）。
  - `rule` TR-5.2: 改 cron/tz/config：未 STARTED open 行 cancelled/schedule_modified 且旧 queued crawl sealed；下周期新 Crawl 反映新 cron（AC-11a）。
  - `rule` TR-5.3: CrawlAdmin 保存修改模板 urls：open 行 cancelled/template_modified；下一周期新 Crawl.urls 为新值（AC-11b，真实 ModelAdmin/表单保存路径或直接调用 admin save 流程）。
  - `rule` TR-5.4: claimed 行的 Crawl 已 STARTED 时做上述修改：行保持 claimed 不被取消（AC-11）。
  - `rule` TR-5.5: `Crawl.cancel()` 后行 cancelled/crawl_cancelled，且下轮评估可领新周期（AC-12）。

## Task 6: runner daemon 循环接入统一入口
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 4, Task 5
- **Completion Evidence**:
  - runner.py daemon 块改为单行 `CrawlSchedule.dispatch_due(source="runner")`；tick 频率/sleep 与其他路径不变。
  - 端到端 `test_schedule_due_crawl_runs_over_server_and_saves_real_content`、`test_add_remains_one_shot_when_schedule_is_due`、test_cli_run.py 69/70（唯一失败为沙箱无法访问 clients2.google.com 下载扩展，与调度无关）。
- **Description**:
  - `archivebox/services/runner.py::run_pending_crawls` daemon schedule 块（约 1769-1773 行）替换为：先 `CrawlSchedule.reconcile_all_stale()`（或在 dispatch_due 内逐 schedule 对账），再 `CrawlSchedule.dispatch_due(source="runner")`；owner 取当前 machine/pid。
  - 评估频率沿用 daemon tick（当前每轮循环），保持 sleep(2) 行为不变；maintenance(update-kind) 在该入口内同步执行的行为与现状一致（由 claim_run 封装）。
  - 非 daemon、crawl_id 指定、maintenance_only 路径行为不变。
- **Acceptance Criteria Addressed**: AC-6, AC-13, AC-16
- **Test Requirements**:
  - `rule` TR-6.1: 既有端到端 `test_schedule_due_crawl_runs_over_server_and_saves_real_content` 不弱化通过，且成功后 DB 有对应账本行并达 succeeded（AC-16）。
  - `rule` TR-6.2: daemon 路径下 update-kind schedule 经账本领用并完成 maintenance，行=succeeded；证据：ORM/CLI 测试。
  - `rubric` TR-6.3: 调度块仍为有界短操作，无长事务/网络在事务内；尺度 1-5，锚点同 AC-17；阈值 >=4；证据：独立审查。

## Task 7: CLI/API 接入（run-all 门禁、--timezone、--max-backfill、--show）
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 4
- **Completion Evidence**:
  - archivebox_schedule.py 新增 --timezone/--max-backfill；--run-all 改走 dispatch_due(force)，输出 Enqueued/Skipped/error；--show 显示 tz/missed/error；result 追加 run_all_skipped/run_all_errors，既有键保留；--clear 改走 disable_many。v1_cli ScheduleCommandSchema 同步字段。
  - 真实子进程：run-all 二次 0 enqueue+Skipped 1；--timezone/--max-backfill 落库与显示；非法 tz 非零退出；--help 含新项；API schema 落库测试通过（4+3 用例）。
- **Description**:
  - `archivebox/cli/archivebox_schedule.py`：
    - `--run-all` 改调 `CrawlSchedule.dispatch_due(source="manual_run_all")`；返回 enqueued/maintained/skipped；输出 `Enqueued N ...` 文案保持，追加 skipped 汇总；maintained 仍同步执行（入口内完成），不再额外调 `run_pending_crawls(maintenance_only=True)`（避免与既有 maintenance 流程重复——核对现有语义后决定保留与否，以测试为准）。
    - 新增 `--timezone`、`--max-backfill` 选项并透传到 CrawlSchedule.create；`--show` 与默认输出增加 tz、next_run、missed/error 摘要。
    - result 字典保留既有键，追加 `run_all_skipped`、`timezone` 等。
  - `archivebox/api/v1_cli.py::ScheduleCommandSchema` 增加 `timezone: str | None = None`、`max_backfill: int | None = None`，cli_schedule 透传。
  - 创建后打印 next_run 用时区感知计算。
- **Acceptance Criteria Addressed**: AC-5, AC-9, AC-15
- **Test Requirements**:
  - `rule` TR-7.1: 真实子进程连续两次 `schedule --run-all`：第一次 Enqueued 1；第二次 skipped=1 且 Crawl 总数不增（AC-5），覆盖与既有 `test_schedule_run_all_enqueues_scheduled_crawl` 兼容。
  - `rule` TR-7.2: `schedule --every=daily --timezone=America/New_York --max-backfill=3 URL` 创建行字段正确；`--timezone=Bogus` 退出非 0 含错误；--help 含新选项（AC-9/AC-15）。
  - `rule` TR-7.3: API schema 接受 timezone/max_backfill 并落库；非法值 400/错误响应；证据：现有 API 测试风格的新断言。

## Task 8: Admin 审计面（schedule 字段 + 账本只读注册）
- **Status**: `completed`
- **Priority**: medium
- **Depends On**: Task 5, Task 7
- **Completion Evidence**:
  - CrawlScheduleAdmin：timezone_name/max_backfill_runs 可编辑；Run Ledger State/Last Error 只读分组；list_display 增 is_enabled/next_run；list_filter 增 tz/skip/error。新增只读 CrawlScheduleRunAdmin（changelist/filter，禁增改删）并注册。CrawlAdmin.save_model 模板修改钩子。
  - test_ui_admin_crawl.py 26 用例全绿，含账本字段渲染、账本 changelist+过滤+禁新增、模板修改经真实 ModelAdmin 路径失效 claim。
- **Description**:
  - `archivebox/crawls/admin.py`：CrawlScheduleAdmin fieldsets 增加 timezone、max_backfill_runs；readonly 增加 last_planned_run_at、missed_runs_skipped、last_skip_*、last_error_* 展示分组；list_display 增加 next_run/启用状态可选列（以方法形式，避免贵查询）。
  - 新增 `CrawlScheduleRunAdmin`（只读：无 add/change 写权限；list_display=id/schedule/planned_at/started_at/finished_at/status/reason/owner/source/run_kind/crawl；list_filter=status/reason/run_kind；search schedule id；raw_id/select_related schedule,crawl），register_admin 注册。
- **Acceptance Criteria Addressed**: AC-15
- **Test Requirements**:
  - `rule` TR-8.1: admin 变更页包含 timezone/max_backfill_runs 输入与只读状态字段；账本 changelist 可访问且按 status 过滤；证据：扩展 `test_ui_admin_crawl.py` 真实 admin 请求测试。
  - `rule` TR-8.2: 通过 admin 保存 timezone/max_backfill 修改成功落库（并触发 Task 5 的修改清理语义）；证据：admin 测试。

## Task 9: 新测试文件与全量回归
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 6, Task 7, Task 8
- **Completion Evidence**:
  - test_crawl_schedule_runs.py 共 28 用例（8 纯函数 + 20 ORM/并发/清理）全绿；test_cli_schedule.py 新增 run-all 门禁/tz/backfill/help 用例并全绿；test_api_v1_cli_schedule.py 3 全绿；test_ui_admin_crawl.py 26 全绿；frozen_config/reindex/add_view 相关用例全绿；端到端 server 出片 2 用例全绿；cancel API 11 全绿；0019→0020 真实迁移+回填演练通过；`uv run prek run --all-files` 全绿。
  - 唯一已知失败：test_cli_run.py 中 1 例因沙箱无法访问 clients2.google.com 下载 istilldontcareaboutcookies 扩展（网络环境限制，与本次改动无关，69/70 通过）。
- **Description**:
  - 新增 `archivebox/tests/test_crawl_schedule_runs.py`（或在合适的既有测试文件中分块），覆盖 TR-4/5/6 的 ORM 级用例（跨周期停机、backfill、并发线程、DST、脏数据、对账、禁用、修改、取消），全部真实 DB 行与真实时间推进（游标更新方式模拟停机，不 mock timezone.now：直接构造历史游标/ planned_at）。
  - 扩展 `test_cli_schedule.py`（run-all 二次跳过、--timezone/--max-backfill、--show）。
  - 扩展 `test_ui_admin_crawl.py`（admin 审计面、模板修改清理）。
  - 运行：`uv run pytest archivebox/tests/test_crawl_schedule_runs.py archivebox/tests/test_cli_schedule.py archivebox/tests/test_api_v1_cli_schedule.py archivebox/tests/test_ui_admin_crawl.py -q` 以及 schedule 相关端到端用例；最后 `uv run prek run --all-files`。
- **Acceptance Criteria Addressed**: AC-1..AC-16
- **Test Requirements**:
  - `rule` TR-9.1: 用户要求的 7 类场景（跨周期停机、重复 runner、手动 run-all、backfill 上限、时区、禁用、取消）均有直接对名测试且通过；证据：pytest 收集与通过清单。
  - `rule` TR-9.2: 目标测试文件与相关既有测试全绿；pre-commit 全绿；证据：命令输出。

## Task 10: 自检与独立审查配合
- **Status**: `pending`
- **Priority**: medium
- **Depends On**: Task 9
- **Description**:
  - 对照 AC 逐条补 Completion Evidence；检查无长事务跨网络/FS、无 mock、无多余抽象层；核对文档/帮助文案一致性。
  - 配合独立 reviewer（只读新上下文），修复 review.md 中全部 actionable findings 并复审至 pass。
- **Acceptance Criteria Addressed**: AC-17
- **Test Requirements**:
  - `rubric` TR-10.1: 工作流保真度与代码契合度（AC-17 尺度 1-5，阈值 >=4）；证据：review.md 独立评分与结论 pass。
