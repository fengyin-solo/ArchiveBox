# ArchiveBox 写接口 Idempotency-Key 支持 - 实施计划

## Task 1: 新增配置项、IdempotencyKey 模型、迁移与 Admin
- **Status**: `completed`
- **Completion Evidence**:
  - `ServerConfig.IDEMPOTENCY_KEY_RETENTION_DAYS` 默认 30（实测 `TTL_DEFAULT= 30`），env 覆盖 7/0 生效，-1 被 pydantic 拒绝。
  - 全新临时集合 `archivebox init --quick` 后 `api_idempotencykey` 表、4 个普通索引与唯一索引 `unique_idempotency_key_per_creator_path` 均存在（sqlite_master 实测）。
  - `makemigrations --check --dry-run` 输出 No changes detected（退出 0）；迁移可回滚到 0001（表删除）并再次正向应用（唯一索引重建）。
  - IdempotencyKeyAdmin 已注册（全字段只读、禁增删改）。
- **Priority**: high
- **Depends On**: None
- **Description**:
  - 在 `archivebox/config/common.py` 的 `ServerConfig` 增加 `IDEMPOTENCY_KEY_RETENTION_DAYS: int = Field(default=30, ge=0)`（字段名即环境变量名，自动纳入现有配置合并/覆盖体系）。
  - 在 [archivebox/api/models.py](file:///Users/applepig/项目/字节/swe/30-archivebox--archivebox/tasks/T004-archivebox/T004-archivebox-workspace/archivebox/api/models.py) 新增 `IdempotencyKey` 模型：CompactUUIDField 主键(uuid7)、`created_by` FK(CASCADE, 非空, db_index)、`key`(255)/`request_method`(10)/`request_path`(255)/`request_hash`(64)、`status`(pending/completed)、`status_code`(null)、`response_content_type`(255, default="")、`response_body`(TextField, default="")、`locked_at`(db_index)、`completed_at`(null)、`expires_at`(null, db_index)、`created_at`/`modified_at`；`UniqueConstraint(fields=("created_by","request_method","request_path","key"))`；Meta `app_label="api"`，加 `__str__`。
  - 新增 `archivebox/api/migrations/0002_idempotencykey.py`：完全遵循 0001 范式——SQLite 用手写 `RunSQL`（CREATE TABLE IF NOT EXISTS + created_by/created_at/locked_at/expires_at 索引 + 四列 UNIQUE 索引，含正反向 SQL），PostgreSQL 用 `rebuild_models_from_migration_state`/`drop_models_on_postgres`；state_operations 为完整 CreateModel + AddConstraint。
  - 在 [archivebox/api/admin.py](file:///Users/applepig/项目/字节/swe/30-archivebox--archivebox/tasks/T004-archivebox/T004-archivebox-workspace/archivebox/api/admin.py) 注册只读友好的 `IdempotencyKeyAdmin`（list_display=created_at/key/created_by/request_method/request_path/status/expires_at，search_fields，list_filter=status/created_by，readonly 全部字段），在 `register_admin` 中注册。
- **Acceptance Criteria Addressed**: FR-11, FR-13, FR-14, FR-16, NFR-4
- **Test Requirements**:
  - `rule` TR-1.1: 全新 SQLite 库执行 `migrate` 后存在 `api_idempotencykey` 表、全部列、四列唯一索引与状态相关索引；回滚迁移可删除该表。证据：在临时初始化集合上执行 `uv run --project .. archivebox manage migrate api 0001/0002`（或等价 manage shell ddl 查询）成功并检查 `sqlite_master`/ORM `introspection`。
  - `rule` TR-1.2: `get_config().IDEMPOTENCY_KEY_RETENTION_DAYS == 30`；设置环境变量 `IDEMPOTENCY_KEY_RETENTION_DAYS=7` 后解析为 7；负数被 pydantic 拒绝。证据：`uv run pytest -k idempotency_config` 或一次性 manage shell 输出。
  - `rule` TR-1.3: 模型在迁移状态与当前模型间无差异（`makemigrations --check --dry-run` 对 api app 无新迁移生成）。证据：命令退出码 0 输出。
  - `rule` TR-1.4: Django Admin 中 IdempotencyKey 已注册且为只读。证据：admin registry 断言（测试或 shell 输出）。
- **Notes**: 参考 0001_initial.py 的 SeparateDatabaseAndState 双轨结构；不新增依赖。

## Task 2: 实现请求体指纹与幂等状态机核心
- **Status**: `completed`
- **Completion Evidence**:
  - 新增 [archivebox/api/idempotency.py](file:///Users/applepig/项目/字节/swe/30-archivebox--archivebox/tasks/T004-archivebox/T004-archivebox-workspace/archivebox/api/idempotency.py)：规范化指纹（JSON/multipart/urlencoded/原始字节，独立 upload handler + 私有 BytesIO，不污染下游解析）、白名单匹配、INSERT 声明/IntegrityError 重读、completed 重放/异体冲突、pending 进行中/过期 pending 单条 CAS 抢占、completed 过期惰性删除复用、有界过期清理、响应构建器。
  - 全部为自动提交短语句，无跨副作用事务/内存锁（`rule` TR-2.4 自评 5/5）。
  - 行为由 Task 4/5 端到端测试真实覆盖：multipart POST/PATCH 真实落盘、并发 CAS 3 轮稳定。
- **Priority**: high
- **Depends On**: Task 1
- **Description**:
  - 新增 `archivebox/api/idempotency.py`，包含：
    - 常量：`IDEMPOTENCY_HEADER = "Idempotency-Key"`、`IDEMPOTENCY_KEY_MAX_LENGTH = 255`、`IDEMPOTENCY_IN_FLIGHT_MAX_AGE = timedelta(hours=1)`、状态值 pending/completed。
    - `canonical_request_fingerprint(request) -> str`：JSON 规范化（sort_keys、紧凑分隔；非法 JSON 回退原始字节）；multipart 用独立 upload handler 从 `request.body` 副本解析，规范化字段列表 + 文件（字段名/文件名/content_type/内容 SHA-256），完成后重置 `_body/_stream/_read_started/_post/_files` 使下游 ninja Form 与端点内手动 MultiPartParser 行为不变；urlencoded 用 QueryDict 规范化；其它类型取原始字节；摘要前混入 content-type 主类型。
    - `IdempotencyOutcome`（dataclass：action ∈ {execute, replay, conflict_body, in_progress, invalid_key} 及携带的响应/行）与 `begin_idempotent_request(request) / finish_idempotent_request(record, response)`：
      - 白名单匹配 (method, path)；解析创建者（`authenticated_user_from_request`，匿名→不启用）；读取并校验头（空→不启用；超长→invalid 400）。
      - 首次：短自动提交 INSERT pending 行 → IntegrityError 后重新读取并分流。
      - completed 未过期：指纹一致→replay；不一致→conflict_body。
      - pending：`locked_at` 新→in_progress；超过在途上限→一条 CAS UPDATE（WHERE status=pending AND locked_at<cutoff）抢占，抢占成功→execute，失败→重新读取分流。
      - completed 已过期：短 DELETE 旧行后重试 INSERT（IntegrityError→重新读取）。
      - 完成：短 UPDATE 写入 status_code/response_body/response_content_type/completed_at/expires_at(=now+配置 TTL)/status=completed；响应体按 response.charset 解码。
      - 每次成功 INSERT 后以可移植 SQL（子查询 LIMIT 批量）顺带清理过期行，有界且吞不掉主流程。
    - `build_replay_response(record)`、`build_conflict_response(...)`、`build_in_progress_response(...)`：均为 JSON `HttpResponse`，携带 `Idempotency-Status` 头，重放额外带 `Idempotency-Replayed: true`，进行中带 `Retry-After: 2`；重放响应按记录的完整 Content-Type 与原始字节重建。
  - 全部 DB 操作保持自动提交短语句，禁止跨业务副作用持事务/锁。
- **Acceptance Criteria Addressed**: FR-3, FR-4, FR-5, FR-6, FR-7, FR-8, FR-10, FR-12, NFR-1, NFR-2, NFR-3
- **Test Requirements**:
  - `rule` TR-2.1: 指纹稳定性——两个不同 multipart boundary（手工构造含不同 boundary 但字段/文件内容相同的原始请求）的指纹相同；字段值/文件内容/JSON 语义变化产生不同指纹；JSON key 顺序不同但语义相同指纹一致。证据：pytest 指纹用例（RequestFactory + 真实 MultiPartParser 构造）。
  - `rule` TR-2.2: 中间件解析 multipart 指纹后，下游 POST Form 端点与 PATCH 端点内手动 MultiPartParser 仍能正确读取字段与文件（文件真实落盘）。证据：既有 archiveresult 上传测试在 Task 3 接入后全绿 + 新增显式用例。
  - `rule` TR-2.3: 状态机以真实 DB 行驱动：首次 INSERT、同键同体重放、异体 conflict_body、pending→in_progress、回拨 locked_at 的过期 pending 仅一方 CAS 抢占成功、expires_at 过期后删除复用。证据：pytest 用例直接经 HTTP 栈观察（Task 4/5 端到端覆盖），核心分流以测试客户端请求完成，不用 mock。
  - `rubric` TR-2.4: 事务纪律与双后端可移植性；scale 1-5；anchors 1=存在跨副作用长事务或后端专有 SQL；3=功能正确但有一处可移植性隐患；5=全部短自动提交/CAS、SQL 两后端兼容、无进程内锁；threshold >= 4；证据：代码审查 + SQLite 目标测试。
- **Notes**: Task 4/5 的端到端用例是本任务主要行为证据；本任务结束时模块可被中间件直接调用。

## Task 3: 接入 Django 中间件与路由白名单
- **Status**: `completed`
- **Completion Evidence**:
  - `IdempotencyMiddleware` 注册于 MIDDLEWARE 最内层（HostRoutingMiddleware 之后、MessageMiddleware 之前），重放/冲突请求仍经过全部认证与安全中间件；CORS Allow-Headers 增加 `Idempotency-Key`。
  - 非白名单路由/GET/空键直通零 SQL；256 字符键返回 400（isolation_lifecycle 用例实测）；OPTIONS 预检返回含 Idempotency-Key 的 CORS 头。
- **Priority**: high
- **Depends On**: Task 2
- **Description**:
  - 在 `archivebox/api/idempotency.py` 实现 `IdempotencyMiddleware`：仅对 FR-1 的 8 个 (method, regex path) 规则与带键请求生效；无键/非白名单请求零开销直通；业务响应经 `finish_idempotent_request` 落盘后原样返回；重放/冲突/进行中/非法键短路返回对应 JSON 响应。
  - 在 [archivebox/core/settings.py](file:///Users/applepig/项目/字节/swe/30-archivebox--archivebox/tasks/T004-archivebox/T004-archivebox-workspace/archivebox/core/settings.py) MIDDLEWARE 中将其注册在 `archivebox.api.middleware.ApiCorsMiddleware` 之后，保证 CORS/OPTIONS 行为不变。
  - 非法键（去空白后为空字符串视为未携带；>255 字符）返回 400 JSON 且不触达业务、不写行。
- **Acceptance Criteria Addressed**: FR-1, FR-2, FR-9, FR-15, NFR-3
- **Test Requirements**:
  - `rule` TR-3.1: 8 个白名单路由带键时产生键行；GET、非白名单写路由（如 `/api/v1/core/tags/create/`、`/api/v1/cli/add`）带键也不产生任何键行、行为不变。证据：pytest 白名单/排除用例 + DB 行计数。
  - `rule` TR-3.2: 不带键请求响应与现状逐字节一致且无 `Idempotency-*` 头、无额外键行；256 字符键返回 400 且业务对象未创建。证据：pytest 用例。
  - `rule` TR-3.3: OPTIONS 预检与 CORS 头在带/不带键时均正常。证据：pytest 或请求断言。
- **Notes**: 不使用 ninja 私有 API；仅依赖 Django 中间件协议与现有 auth 助手。

## Task 4: 端到端测试——8 个端点的同键同体重放
- **Status**: `completed`
- **Completion Evidence**:
  - 新增 [test_api_v1_idempotency_replay.py](file:///Users/applepig/项目/字节/swe/30-archivebox--archivebox/tasks/T004-archivebox/T004-archivebox-workspace/archivebox/tests/test_api_v1_idempotency_replay.py) 9 个用例：snapshot create/patch/delete、archiveresult POST/PATCH(multipart)、crawl create/patch/delete 同键同体均返回相同状态码/逐字节响应体 + replayed 头，对象计数不增加、删除重放不 404、上传文件只写一次；另有 400 首响原样重放。`9 passed`。
- **Priority**: high
- **Depends On**: Task 3
- **Description**:
  - 新增 `archivebox/tests/test_api_v1_idempotency_replay.py`（真实 Django test Client + `api_headers`/双用户令牌 + 真实 DB/文件断言）：
    - `POST /core/snapshots`：两次同键同体 → 同状态同响应体（同 id）、Snapshot+Crawl 各仅 1 行、第二次带头标记。
    - `PATCH /core/snapshot/{id}`（tags/status）：重放响应一致，标签只被设置一次。
    - `DELETE /core/snapshot/{id}`：重放返回第一次的 200 删除响应体而非 404，对象只删一次，deleted_count 一致。
    - `POST /core/archiveresults`（multipart）：重放后文件内容不重复写入、ArchiveResult 仅 1 行、output_files/size 一致。
    - `PATCH /core/archiveresult/{id}`（multipart）：同上，文件不重复/不追加。
    - `POST /crawls/crawls`：Crawl 仅 1 行、响应同 id。
    - `PATCH /crawl/{id}`（tags_str/action 等）：重放一致。
    - `DELETE /crawl/{id}`：重放 200 原响应而非 404。
    - 首请求 4xx（如缺 url）同键同体重放返回相同 400 体。
    - 每次重放断言 `Idempotency-Replayed: true` / `Idempotency-Status: replayed`，首次响应无该头。
- **Acceptance Criteria Addressed**: AC-1
- **Test Requirements**:
  - `rule` TR-4.1: 上述每个端点用例均断言两次响应 status 相同、body 逐字节一致、重放头正确、业务对象计数/文件系统副作用只发生一次。证据：`uv run pytest archivebox/tests/test_api_v1_idempotency_replay.py -q` 全绿输出。
- **Notes**: multipart 使用仓库既有的 `SimpleUploadedFile` + 固定/显式 BOUNDARY 方式构造。

## Task 5: 端到端测试——冲突、并发、隔离、过期、重启持久化、无键兼容
- **Status**: `completed`
- **Completion Evidence**:
  - [test_api_v1_idempotency_conflict.py](file:///Users/applepig/项目/字节/swe/30-archivebox--archivebox/tasks/T004-archivebox/T004-archivebox-workspace/archivebox/tests/test_api_v1_idempotency_conflict.py) 5 用例：JSON/multipart 异体 409 conflict 且无副作用后原体可重放；pending 行 409 in-progress+Retry-After；他人 pending 不干扰本创建者。
  - [test_api_v1_idempotency_concurrency.py](file:///Users/applepig/项目/字节/swe/30-archivebox--archivebox/tasks/T004-archivebox/T004-archivebox-workspace/archivebox/tests/test_api_v1_idempotency_concurrency.py) 3 用例：真实线程+真实 `crawl_lifecycle_lock` 阻塞 DELETE，重复请求得 in-progress 且对象未删，完成后重放；单请求陈旧 pending 抢占；双线程 Barrier 并发抢占恰一次副作用（3 轮重复运行稳定）。
  - [test_api_v1_idempotency_isolation_lifecycle.py](file:///Users/applepig/项目/字节/swe/30-archivebox--archivebox/tasks/T004-archivebox/T004-archivebox-workspace/archivebox/tests/test_api_v1_idempotency_isolation_lifecycle.py) 10 用例：创建者隔离、路径/方法隔离、回拨 expires_at 过期复用（默认 30 天断言）、TTL=0 立即过期、connection.close() 模拟重启后重放、无键/空白键行为不变零键行、超长键 400、非白名单与 GET 忽略、CORS 预检。
  - 三个文件 18 用例全部通过（`18 passed`）。
- **Priority**: high
- **Depends On**: Task 3
- **Description**:
  - 新增 `archivebox/tests/test_api_v1_idempotency_conflict.py`：
    - 请求体冲突：POST snapshots 同键不同 url → 409 `Idempotency-Status: conflict`，消息说明键被占用，第二个 Snapshot 未创建；随后原体重放成功。multipart 不同文件内容再一例。
    - pending 冲突（非并发）：直接 ORM 插入 pending 行 → 同键同体返回 409 in-progress + Retry-After，不执行业务。
  - 新增 `archivebox/tests/test_api_v1_idempotency_concurrency.py`：真实线程 + 真实 `crawl_lifecycle_lock`：持锁线程让 DELETE /snapshot 在业务中阻塞 → R2 同键得到 409 in-progress 且对象未删、键表 1 行；释放后 R1 200、对象消失；R3 重放 200；全程仅一次删除副作用。
  - 在同一测试文件或新文件覆盖：
    - 创建者隔离：两个 superuser + 各自 token/Client，同键同体在同路径各创建 1 个 Snapshot（共 2 行），各自重放各自的 id；同用户同键打不同路径（POST snapshots vs POST crawls）互不冲突。
    - 过期回收：完成后 ORM 回拨 `expires_at` → 同键同体重新执行（新 Crawl/Snapshot、新响应无重放头、新行 TTL 正确），旧行被删除；TTL=0 环境/配置下完成即过期。
    - 重启持久化：完成后 `connection.close()` 重连（模拟进程重启）再重放成功；崩溃恢复：pending 行回拨 `locked_at` 超过在途上限后，两个并发请求仅一个 CAS 抢占成功并执行业务。
    - 无键兼容：无键 POST 不产生键行、无幂等头；超长键 400；非白名单写路由（tags/create）带键不产生行。
- **Acceptance Criteria Addressed**: AC-2, AC-3, AC-4, AC-5, AC-6, AC-7
- **Test Requirements**:
  - `rule` TR-5.1: 每个用例均以真实 HTTP 响应状态码/头/体 + 真实 DB 行数 + 真实文件系统状态断言，无 mock/monkeypatch/sleep 轮询之外的伪造。证据：三个新测试文件 `uv run pytest` 全绿。
  - `rule` TR-5.2: 并发用例稳定复现：R2 必定在 R1 持锁窗口内得到 in-progress，且线程清理后无悬挂连接/进程。证据：重复运行（`-p repeat*` 或多次调用）均通过；测试超时时间合理。
  - `rule` TR-5.3: 过期/TTL 用例断言 `expires_at` 与配置一致，且配置覆盖生效（默认 30、环境覆盖、0 语义）。证据：pytest 输出。
- **Notes**: 线程内使用独立 `Client()` 实例；遵循仓库已有持锁测试范式（test_api_v1_core_snapshots.py）。

## Task 6: 回归验证、lint 与独立评审整改闭环
- **Status**: `completed`
- **Completion Evidence**:
  - 新增 27 个幂等测试全部通过（R1 后再补充边界用例）；受影响既有测试 71 passed / 1 failed（`test_snapshot_pause_resume_api_leaves_archiveresult_facts_unchanged` 在 stash 掉本次改动后的干净代码上同样失败，根因是沙箱无法下载 wget/archivewebpage 二进制与 github 连接超时，非本次回归）。
  - 迁移/认证/token 副作用 5 文件 23 passed；额外 10 个 core/crawls API 文件 30 passed。`test_urls.py` 的 31 errors 在干净代码上完全一致（沙箱子进程守护进程限制），非本次回归。
  - `uv run prek run --all-files` 全部钩子 Passed；`makemigrations --check` 无差异；全新集合迁移 0002 正/反向验证通过。
- **Priority**: high
- **Depends On**: Task 4, Task 5
- **Description**:
  - 运行受影响既有测试：`test_api_v1_core_snapshots.py`、`test_api_v1_core_snapshot_snapshot_id.py`、`test_api_v1_core_archiveresults.py`、`test_api_v1_core_archiveresult_archiveresult_id.py`、`test_api_v1_crawls_crawls.py`、`test_api_v1_crawls_crawl_crawl_id.py`、`test_api_v1_auth_*`、`test_migrations_fresh.py`。
  - 运行 `uv run prek run --all-files`（ruff 等）并修复全部问题。
  - 验证迁移在全新集合上的正向应用（`archivebox init --install`/migrate 路径）与模型状态一致（makemigrations --check）。
  - 支持独立 Review：根据 review.md 的发现建立 issue 并整改至 pass。
- **Acceptance Criteria Addressed**: AC-7, AC-8, NFR-4
- **Test Requirements**:
  - `rule` TR-6.1: 上述既有测试与全部新增幂等测试在同一 `uv run pytest` 调用中全绿。证据：命令输出。
  - `rule` TR-6.2: `uv run prek run --all-files` 退出码 0。证据：命令输出。
  - `rule` TR-6.3: 全新临时 DATA_DIR 上迁移链应用成功且 `api_idempotencykey` 存在；无未生成迁移。证据：命令输出。
  - `rubric` TR-6.4: 实现质量评分（=AC-8）；scale 1-5；threshold >= 4；证据：独立 review.md 结论。

## Issue I-1: 0002 迁移删除未使用导入（ruff F401）
- **Status**: `completed`
- **Priority**: medium
- **Discovered By**: Review R1（F1，actionable）
- **Description**: `archivebox/api/migrations/0002_idempotencykey.py` 第 12 行 `import archivebox.api.models` 未被引用，`ruff check .` 报 F401；新文件未被 git 跟踪时 prek 漏扫。
- **Acceptance Criteria Addressed**: AC-8
- **Completion Evidence**: 已删除该导入；`uv run ruff check .` 退出 0。
- **Test Requirements**:
  - `rule` TR-I1.1: `uv run ruff check .` 退出 0；`makemigrations --check` 仍无差异。

## Issue I-2: models.py 按 add-trailing-comma/ruff-format 要求格式化
- **Status**: `completed`
- **Priority**: medium
- **Discovered By**: Review R1（F2，actionable）
- **Description**: IdempotencyKey.created_by 长行未按 add-trailing-comma 钩子拆行，prek 在文件被跟踪后会失败。
- **Acceptance Criteria Addressed**: AC-8
- **Completion Evidence**: 已接受钩子格式化（多行 + 尾逗号）；对全部新文件显式跑 prek hooks 稳定通过。
- **Test Requirements**:
  - `rule` TR-I2.1: `uv run prek run --files <全部新增/修改文件>` 退出 0 且二次运行无修改。

## Issue I-3: 边界硬化与补充回归用例（R1 advisory F4/F5/F6/F7/F10）
- **Status**: `completed`
- **Priority**: medium
- **Discovered By**: Review R1（advisory 合并整改）
- **Description**:
  - F7/F10：创建者改为与端点完全相同的 token 解析（`token_from_request`+`auth_using_token`），匿名/无 token 请求在长度校验之前旁路（由端点返回 401），消除会话用户/token 用户不一致归属。
  - F6：request_path 超过 255 字符时旁路幂等处理，交由路由返回自身 4xx，杜绝 DataError 500。
  - F5：陈旧 pending CAS 抢占成功后同样触发有界惰性 purge。
  - F3：spec FR-7 措辞与实现对齐（pending 异体统一 in-progress，仅 completed 异体为 conflict）。
  - F4：补充边界回归：multipart 自定义 boundary 等价（HTTP 级 + RequestFactory 指纹级）、JSON 键序/unicode 指纹、匿名带键旁路、同路径异方法隔离、purge 行为、非法 JSON 确定性回退重放、255 字符键接受、超长路径旁路。
  - F9：修正 tasks.md 用例计数。
- **Acceptance Criteria Addressed**: FR-3, FR-4, FR-7, FR-8, FR-10, AC-2, AC-3, AC-4, AC-7, AC-8
- **Completion Evidence**: 新增 test_api_v1_idempotency_edge_cases.py；全部新测试通过；行为变更由既有 27 用例与新用例共同覆盖。
- **Test Requirements**:
  - `rule` TR-I3.1: 边界用例全部通过；四个幂等测试文件 + edge_cases 合计全绿。
  - `rule` TR-I3.2: 既有 conflict/isolation 用例在身份解析改为 token 直连后仍全绿。
