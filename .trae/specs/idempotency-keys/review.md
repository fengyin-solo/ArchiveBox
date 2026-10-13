# ArchiveBox 写接口 Idempotency-Key 支持 - Independent Review

- [ ] CP-R1: 8 个白名单写路由同键同体重放首次完整响应且副作用仅一次
  - **Type**: `rule`
  - **Covers**: AC-1, FR-5, FR-6, FR-13
  - **Evidence**: Pending

- [ ] CP-R2: 同键异体返回 409 conflict 且不执行业务，原体仍可重放
  - **Type**: `rule`
  - **Covers**: AC-2, FR-7
  - **Evidence**: Pending

- [ ] CP-R3: 进行中并发返回 409 in-progress、绝不重复副作用；陈旧 pending 经 CAS 唯一抢占
  - **Type**: `rule`
  - **Covers**: AC-3, FR-8, NFR-1
  - **Evidence**: Pending

- [ ] CP-R4: 键按创建者 + 方法 + 路径隔离
  - **Type**: `rule`
  - **Covers**: AC-4, FR-3
  - **Evidence**: Pending

- [ ] CP-R5: 过期回收与可配置保留时间（默认 30 天，0=立即过期）
  - **Type**: `rule`
  - **Covers**: AC-5, FR-10, FR-11
  - **Evidence**: Pending

- [ ] CP-R6: 服务重启后已完成键仍可重放；崩溃遗留 pending 可恢复
  - **Type**: `rule`
  - **Covers**: AC-6, FR-12
  - **Evidence**: Pending

- [ ] CP-R7: 无键行为不变、非法键 400、白名单精确、CORS 放行
  - **Type**: `rule`
  - **Covers**: AC-7, FR-1, FR-2, FR-9, FR-15
  - **Evidence**: Pending

- [ ] CP-U1: 实现质量——事务/并发纪律、双后端迁移、仓库范式一致性、最小改动
  - **Type**: `rubric`
  - **Covers**: AC-8, FR-14, FR-16, NFR-2, NFR-3, NFR-4
  - **Scale**: 1-5
  - **Anchors**: 1 = 存在跨副作用长事务/长锁、mock 测试、破坏既有范式或双后端迁移缺陷；3 = 功能可用但有抽象冗余或一处并发/移植隐患；5 = 短自动提交/CAS、竞态闭环、SQLite raw DDL + PG rebuild 双轨与 0001 一致、无新依赖、改动聚焦
  - **Pass Threshold**: >= 4
  - **Evidence**: Pending

## Review History

### Review R1
- **Result**: Pending
- **Evidence**: Pending
