# ArchiveBox 写接口 Idempotency-Key 支持 - 产品需求文档

## Overview
- **Summary**: 为 ArchiveBox REST API（django-ninja v1）的写接口增加标准化的 `Idempotency-Key` 请求头支持。同一创建者在同一接口路径上用同一个键配合同一请求体重复提交时，服务端只执行一次副作用，后续请求原样重放第一次的状态码与响应体；请求体不一致返回冲突错误；第一次请求仍在进行时返回明确的"进行中"结果，保证不并发执行两次副作用。键记录持久化在数据库中，服务重启后仍然有效，并按可配置的保留时间过期回收。
- **Purpose**: 客户端超时重试、网关/代理重放、脚本重复执行等场景下，当前写接口（`create_snapshot`、`create_crawl`、各处 update/delete）会产生重复的 Snapshot、Crawl、ArchiveResult 或无意义的重复修改，且调用方拿不到第一次响应，无法安全重试。幂等键把"恰好一次副作用"变成 API 契约的一部分。
- **Target Users**: 通过 REST API 自动化调用 ArchiveBox 的集成方、浏览器扩展、脚本与网关客户端。

## Goals
- 覆盖 Snapshot、Crawl、ArchiveResult 三个资源现有的全部创建、更新、删除 REST 路径（共 8 个端点，见 FR-1）。
- 重复提交安全：同键同体只执行一次，后续重放第一次的完整响应（状态码 + 响应体），并带可识别的重放标记。
- 冲突可辨：同键异体返回明确冲突错误，说明键已被其它请求占用；同键并发提交在首请求进行中时得到明确的"进行中"结果，绝不执行第二次副作用。
- 隔离与回收：键按创建者 + HTTP 方法 + 接口路径隔离；保留时间可配置；过期后键可被重新使用。
- 可靠持久：键记录存于普通数据库表，遵循仓库的短事务/自动提交并发契约，SQLite 与 PostgreSQL 均正确，服务重启后已完成的键仍然有效。
- 向后兼容：不携带 `Idempotency-Key` 请求头的请求行为与现状完全一致，不产生任何键记录。

## Non-Goals
- 不为 ArchiveResult 新增 DELETE 端点（当前 API 不存在该路由，无既有删除路径可覆盖）。
- 不覆盖 `/api/v1/cli/`（CLI 子命令桥接）、`/api/v1/machine/`、`/api/v1/personas/`、auth、webhooks 等其它写接口；中间件以显式路由白名单方式实现，后续扩展只需加规则。
- 不引入跨机器的文件租约或定时器选举；协调完全依赖数据库行与约束（遵循 AGENTS.md 并发契约）。
- 不提供键的查询/管理 REST API（仅 Django Admin 只读可观测 + 惰性回收）。
- 不改变现有端点的响应 schema 与业务逻辑。

## Background & Context
- 写接口现状（无幂等键）：
  - Snapshot：`POST /api/v1/core/snapshots`（[v1_core.py](file:///Users/applepig/项目/字节/swe/30-archivebox--archivebox/tasks/T004-archivebox/T004-archivebox-workspace/archivebox/api/v1_core.py) `create_snapshot`，未带 `crawl_id` 时还会隐式创建 Crawl）、`PATCH /api/v1/core/snapshot/{id}`、`DELETE /api/v1/core/snapshot/{id}`。
  - Crawl：`POST /api/v1/crawls/crawls`、`PATCH /api/v1/crawls/crawl/{id}`、`DELETE /api/v1/crawls/crawl/{id}`（[v1_crawls.py](file:///Users/applepig/项目/字节/swe/30-archivebox--archivebox/tasks/T004-archivebox/T004-archivebox-workspace/archivebox/api/v1_core.py)）。
  - ArchiveResult：`POST /api/v1/core/archiveresults`、`PATCH /api/v1/core/archiveresult/{id}`（multipart 文件上传）；无 DELETE 路由。
- 认证：全部目标端点均要求 API token 认证（`API_AUTH_METHODS`，superuser），`archivebox.api.auth.authenticated_user_from_request` 可从请求中解析创建者用户。
- API app 已有模型与迁移范式：[models.py](file:///Users/applepig/项目/字节/swe/30-archivebox--archivebox/tasks/T004-archivebox/T004-archivebox-workspace/archivebox/api/models.py)（`APIToken`，CompactUUIDField 主键）、[0001_initial.py](file:///Users/applepig/项目/字节/swe/30-archivebox--archivebox/tasks/T004-archivebox/T004-archivebox-workspace/archivebox/api/migrations/0001_initial.py)（SQLite 原生 SQL + PostgreSQL `rebuild_models_from_migration_state` 双轨）。
- 配置：`archivebox.config.common.ServerConfig`（BaseConfigSet，字段名即环境变量名），请求内可通过 `get_request_config(request)` 获取。
- 中间件注册于 [settings.py](file:///Users/applepig/项目/字节/swe/30-archivebox--archivebox/tasks/T004-archivebox/T004-archivebox-workspace/archivebox/core/settings.py) MIDDLEWARE；已有 `archivebox.api.middleware.ApiCorsMiddleware`。
- 并发契约（AGENTS.md）：SQLite 支持并发短写；禁止跨网络/文件系统/长工作持有事务或锁；使用自动提交/CAS 更新。
- 测试范式：pytest + pytest-django（`django_db(transaction=True)`）+ Django test Client 走真实 HTTP 分发与真实数据库行；已有用真实 `crawl_lifecycle_lock` 持锁线程构造请求阻塞的先例（test_api_v1_core_snapshots.py）。禁止 mock/monkeypatch/伪造处理器绕过用户流程。
- 技术栈版本：Django 6.1、django-ninja 1.6.3、Python 3.13。

## Functional Requirements

### 协议
- **FR-1（覆盖范围）**: 仅对以下 8 个写路由启用幂等键处理（method + 路径正则白名单）：
  1. `POST /api/v1/core/snapshots`
  2. `PATCH /api/v1/core/snapshot/{snapshot_id}`
  3. `DELETE /api/v1/core/snapshot/{snapshot_id}``
  4. `POST /api/v1/core/archiveresults`
  5. `PATCH /api/v1/core/archiveresult/{archiveresult_id}`
  6. `POST /api/v1/crawls/crawls`
  7. `PATCH /api/v1/crawls/crawl/{crawl_id}`
  8. `DELETE /api/v1/crawls/crawl/{crawl_id}`
  其它所有路径与 GET/HEAD/OPTIONS 请求不发生任何行为变化。
- **FR-2（键的传递与校验）**: 客户端通过 `Idempotency-Key: <key>` 请求头提供键；去除首尾空白后长度必须为 1–255 字符，否则返回 400 错误且不执行业务、不写键记录。未携带或头值为空时按普通请求处理（FR-9）。
- **FR-3（隔离维度）**: 键的唯一性与查询命名空间为 `(创建者用户, HTTP 方法, 请求路径, 键字符串)` 四元组。不同创建者、不同路径、不同 HTTP 方法下相同键字符串互不干扰。创建者由与端点认证完全相同的 token 解析逻辑确定；无法解析到认证用户的请求不启用幂等处理（目标端点本身随后会返回 401/403）。
- **FR-4（请求体指纹）**: 对请求体计算与传输编码无关的规范化指纹（SHA-256）：
  - `application/json`：解析后以稳定排序、紧凑分隔重新序列化（非法 JSON 回退为原始字节哈希）；
  - `multipart/form-data`：基于字段名/值与上传文件（字段名、文件名、内容哈希、内容类型）规范化，忽略随机 boundary；中间件解析后必须把请求体重置为可再次读取状态，保证 ninja Form 解析与端点内手动 `MultiPartParser` 解析行为不变；
  - `application/x-www-form-urlencoded`：按解析后的字段名/值规范化；
  - 其它类型：原始字节哈希。
  - 指纹包含 Content-Type 主类型，空请求体有确定性表示。
- **FR-5（首次请求）**: 命名空间无有效记录时，以自动提交的短 INSERT 写入一条 `pending` 记录（`locked_at=now`，含请求体指纹），随后仅执行一次业务副作用；业务返回后以短 UPDATE 记录 HTTP 状态码、响应体、响应 Content-Type、`completed_at=now`、`expires_at=now+TTL`，状态置为 `completed`。不得在业务副作用期间持有数据库事务或长锁。
- **FR-6（重放）**: 命名空间存在 `completed`、未过期且请求体指纹一致的记录时，不执行业务，直接返回与第一次**相同的状态码和响应体**（按记录的 Content-Type 重建响应），并附加响应头 `Idempotency-Replayed: true` 与 `Idempotency-Status: replayed`。第一次产生的 4xx 响应同样被重放。
- **FR-7（请求体冲突）**: 同命名空间下存在**已完成（completed）**记录且请求体指纹不一致时，返回 `409 Conflict`，`Idempotency-Status: conflict`，JSON 体明确说明该 Idempotency-Key 已被另一个不同请求体占用；不执行任何业务副作用，不覆盖原记录。对于仍为 `pending` 的记录，无论请求体是否相同一律按 FR-8 返回 in-progress（首请求结果未定时无法安全判定占用语义，且同样保证不执行第二次副作用）。
- **FR-8（进行中并发）**: 同命名空间下存在未过期的 `pending` 记录（且未超过在途上限时长）时，立即返回 `409 Conflict`，`Idempotency-Status: in-progress`，`Retry-After: 2`，JSON 体说明携带该键的请求仍在处理中；不等待、不执行业务。并发的两个首次请求中，仅一个 INSERT 凭唯一约束成功并执行副作用，另一个得到该结果。`pending` 记录的 `locked_at` 超过固定在途上限（1 小时，用于崩溃/被杀进程恢复）时，允许通过一次原子 CAS（仅当仍为该过期 pending 行）抢占后重新执行；抢占失败者重新读取并按 in-progress/重放规则处理。
- **FR-9（无键兼容）**: 未携带有效 `Idempotency-Key` 头的请求与现有行为完全一致：无额外数据库查询/写入，响应无任何幂等相关头。
- **FR-10（过期与回收）**: `completed` 记录在 `expires_at` 过后允许同一键重新使用：惰性删除旧记录后按首次请求流程执行（INSERT 冲突时重新读取并遵循 FR-6/7/8）。每次键写入时附带一次有上限、低成本的批量过期清理（跨 SQLite/PostgreSQL 可移植）。
- **FR-11（可配置保留时间）**: 在 `ServerConfig` 新增 `IDEMPOTENCY_KEY_RETENTION_DAYS: int`，默认 30，约束 >= 0；可通过现有配置体系（ArchiveBox.conf / 环境变量）覆盖。值为 0 时完成记录立即视为过期（每次重试重新执行业务）。
- **FR-12（持久化与重启）**: 所有键状态均为普通数据库表中自动提交的持久行；不依赖进程内状态。服务/worker 重启后，`completed` 键继续可重放；重启遗留的 `pending` 键经 FR-8 的在途上限恢复。

### 数据与实现
- **FR-13（模型）**: 在 `archivebox.api` 新增 `IdempotencyKey` 模型（表名 `api_idempotencykey`）：
  - `id` CompactUUIDField(uuid7) 主键；`created_by` FK→auth_user（CASCADE，非空，索引）；
  - `key` CharField(255)、`request_method` CharField(10)、`request_path` CharField(255)、`request_hash` CharField(64)；
  - `status` CharField：`pending`/`completed`；`status_code` IntegerField(null)、`response_content_type` CharField(255, default="")、`response_body` TextField(default="")；
  - `locked_at` DateTimeField(db_index)、`completed_at` DateTimeField(null)、`expires_at` DateTimeField(null, db_index)、`created_at`/`modified_at`；
  - `UniqueConstraint(fields=("created_by","request_method","request_path","key"))`。
- **FR-14（迁移）**: 新增 `archivebox/api/migrations/0002_idempotencykey.py`，遵循 0001 既有范式：SQLite 走手写 `RunSQL`（CREATE TABLE IF NOT EXISTS + 索引 + 唯一索引），PostgreSQL 走 `rebuild_models_from_migration_state`，状态操作包含完整 CreateModel/AddConstraint，保证两后端 schema 与迁移状态一致、可正向可回滚。
- **FR-15（中间件）**: 新增 `archivebox/api/idempotency.py`，实现一个 Django 中间件（含路由白名单、指纹、声明/重放/冲突/进行中/过期回收逻辑）并注册到 MIDDLEWARE 中 `ApiCorsMiddleware` 之后（使 CORS 头仍能包装其响应；OPTIONS 预检已被上游短路）。
- **FR-16（Admin 可观测）**: 在 `archivebox/api/admin.py` 注册 `IdempotencyKey` 的只读友好 Admin（list_display/search/list_filter，不提供新增/删除按钮的常规只读管控），与 APIToken 注册风格一致。

## Non-Functional Requirements
- **NFR-1（并发正确性）**: 同一键并发提交在 SQLite（WAL/DELETE 两种日志模式）与 PostgreSQL 下都至多执行一次副作用；声明只依赖唯一约束与短 CAS，无跨请求内存锁。
- **NFR-2（事务纪律）**: 任何网络调用、hook、文件系统写入、runner 执行期间不持有数据库事务；键的读写均为自动提交短语句，符合仓库并发契约。
- **NFR-3（性能）**: 无键请求零额外 SQL；带键请求的额外 SQL 为有界常数条主键/唯一索引查询与短写；请求体指纹解析不得破坏上传流式读取语义。
- **NFR-4（可维护性）**: 遵循仓库现有模式（模型/迁移/中间件/配置/Admin/测试），最小改动；不引入新依赖；通过 `prek run --all-files`（ruff 等）。
- **NFR-5（可观测性）**: 重放/冲突/进行中响应带明确响应头；Admin 可查看键状态、占用者、路径、过期时间。

## Constraints
- **Technical**: Python 3.13 / Django 6.1 / django-ninja 1.6.3；同时支持 SQLite（本地文件，绝不在 NFS/SMB）与 PostgreSQL；使用 `uv`/`uv run`；测试走真实 HTTP 分发、真实 DB 行、真实文件、真实 `crawl_lifecycle_lock`，禁止 mock/monkeypatch/伪造处理。
- **Business**: 保持 `dev` 分支；不发布任何包/镜像/tag；不削弱现有断言、不跳过测试。
- **Dependencies**: 无新增第三方依赖；复用 `archivebox.api.auth`、`archivebox.config.common`、`archivebox.misc.db`、`archivebox.uuid_compat`。

## Assumptions
- 目标端点全部要求认证用户，因此"创建者"总能在启用幂等处理时解析为具体 User；匿名请求不启用该机制（也无法触达副作用）。
- 客户端以相同语义请求体重试；multipart 重试时字段与文件内容一致（boundary/per-part 头顺序差异由规范化吸收）。
- 响应体规模有界（Snapshot/Crawl/ArchiveResult schema JSON），以 TextField 存储完整响应体可接受。
- DELETE 端点在内部会获取 `crawl_lifecycle_lock` 并同步运行 runner，可作为并发测试中真实的、可阻塞的用户流程。

## Acceptance Criteria

### AC-1: 同键同体重放返回第一次的完整响应（8 个端点）
- **Type**: `rule`
- **Given**: 认证客户端对任一 FR-1 端点发起携带 `Idempotency-Key: K` 的写请求 R1（JSON 或 multipart，语义相同的请求体）
- **When**: 在 R1 完成后用完全相同的键与语义相同的请求体再次请求 R2
- **Then**: R2 不产生第二次副作用，返回与 R1 相同的状态码与逐字节一致的响应体，并带 `Idempotency-Replayed: true`、`Idempotency-Status: replayed`
- **Pass Condition**: 对全部 8 个端点分别断言：两次响应 status/body 一致、重放头存在、数据库中对应对象计数不增加（删除端点重放不会因对象已删而 404）、ArchiveResult 上传文件不被二次写入
- **Evidence**: `uv run pytest` 新增测试文件中各端点重放用例的通过输出与 DB/文件系统断言

### AC-2: 同键异体返回冲突错误
- **Type**: `rule`
- **Given**: 键 K 已在某端点被请求体 B1 占用（completed 或 pending）
- **When**: 同一创建者以键 K 在同一路径提交不同请求体 B2
- **Then**: 返回 409，`Idempotency-Status: conflict`，JSON 消息说明该键已被其它请求占用；不执行 B2 的任何副作用，不覆盖原记录；随后用 B1 重试仍正常重放第一次响应
- **Pass Condition**: POST snapshots（不同 url）、multipart archiveresult（不同文件内容）各至少一例冲突断言，且对象计数不因冲突请求变化
- **Evidence**: pytest 冲突用例输出

### AC-3: 首请求进行中的并发提交得到明确进行中结果且副作用只执行一次
- **Type**: `rule`
- **Given**: 请求 R1 携带键 K 进入端点后因真实 `crawl_lifecycle_lock` 被持锁线程阻塞在业务执行中，且其 `pending` 键行已落库
- **When**: 另一请求 R2 以同键同体并发提交，随后释放锁让 R1 完成，再以同键同体提交 R3
- **Then**: R2 立即得到 409 + `Idempotency-Status: in-progress` + `Retry-After`，不执行第二次删除；R1 正常完成；R3 重放 R1 的响应；全程业务对象只被删除一次
- **Pass Condition**: 并发用例中 R2 状态/头符合、对象在 R1 完成前仍存在且完成后消失、键表仅 1 行、无重复删除/额外行
- **Evidence**: 基于真实 test Client 多线程 + `crawl_lifecycle_lock` 的 pytest 用例输出与 DB 断言

### AC-4: 键按创建者与路径隔离
- **Type**: `rule`
- **Given**: 两个不同认证用户 U1/U2，与两个不同路径 P1/P2
- **When**: 他们分别使用相同键字符串 K（同体或异体）请求
- **Then**: 各自独立执行业务并独立重放，互不冲突；同一用户在不同路径/方法上使用 K 也互不影响
- **Pass Condition**: 两用户同键各创建一个 Snapshot（共 2 行）且各自重放只返回自己的对象；跨路径同键不产生 conflict
- **Evidence**: pytest 创建者隔离用例输出

### AC-5: 过期回收
- **Type**: `rule`
- **Given**: 键 K 存在 `completed` 记录
- **When**: 通过 ORM 将其 `expires_at` 回拨到过去（等价于保留时间到期），再以同键同体提交
- **Then**: 旧记录被回收，业务重新执行一次，产生新的响应（无重放标记），键表中该命名空间仅存新记录；配置 `IDEMPOTENCY_KEY_RETENTION_DAYS` 默认 30 且可经环境/配置覆盖，TTL=0 时完成即过期
- **Pass Condition**: 回拨后重试产生第二个对象/新响应、新行 expires_at 与新 TTL 一致；配置默认值与覆盖行为有断言
- **Evidence**: pytest 过期用例与配置用例输出

### AC-6: 服务重启后已完成键仍可重放
- **Type**: `rule`
- **Given**: R1 携带键 K 完成后，关闭并重建数据库连接（等价于进程重启，数据位于 SQLite 文件/PostgreSQL 库中，无进程内状态）
- **When**: 再次以同键同体请求
- **Then**: 重放 R1 响应、不重放业务；遗留 `pending` 行超过在途上限后可被原子抢占重新执行
- **Pass Condition**: 重连后重放成功；手工构造的过期 pending 行可被抢占且仅一方胜出
- **Evidence**: pytest 持久化/恢复用例输出

### AC-7: 未携带键时保持现有行为
- **Type**: `rule`
- **Given**: 对 FR-1 端点的普通请求不带 `Idempotency-Key`
- **When**: 正常调用
- **Then**: 请求行为、状态码、响应体与现状一致，无额外 SQL 写入（`api_idempotencykey` 无新增行），响应无 `Idempotency-*` 头；非法超长键返回 400 且不触达业务
- **Pass Condition**: 既有相关测试全部继续通过；新增无键用例断言零键行、无幂等头；超长键 400 用例通过
- **Evidence**: 目标既有测试文件 + 新增用例的 pytest 输出

### AC-8: 实现质量与事务纪律
- **Type**: `rubric`
- **Dimension**: 代码与仓库模式的一致性、事务/并发处理的严谨性、迁移双后端正确性
- **Scale**: 1-5
- **Anchors**: 1 = 引入长事务/跨副作用持锁、mock 测试或破坏既有模式；3 = 功能可用但存在抽象冗余或一处事务隐患；5 = 短自动提交/CAS、唯一约束竞态闭环、SQLite/PG 双轨迁移与既有范式一致、改动聚焦、无新依赖
- **Pass Threshold**: >= 4
- **Evidence**: 代码审查、迁移检查、`uv run prek run --all-files` 与目标 pytest 全绿

## Open Questions
- 无（需求中"等待或进行中"二选一，本方案选择立即返回 409 in-progress + Retry-After，避免服务端长阻塞等待；如评审倾向有限时短暂等待，可在评审中调整，不影响存储模型）。
