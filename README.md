# 应急演练 · 防护物资/演练设备预约服务（本地模拟）

在**不改变既有区段互斥规则**的前提下，为演练计划新增本地模拟的防护物资/演练设备
预约能力：计划可声明所需资源、数量与占用窗口；即使两个计划在互斥矩阵中允许并行，
同一稀缺资源在重叠窗口也**绝不超卖**。

- 运行时零三方依赖：Python 3.11+ 标准库（`http.server` + `sqlite3` + `threading`）
- 持久化：SQLite（WAL），写事务 `BEGIN IMMEDIATE` 全局串行 + 触发器硬约束双保险
- 测试：标准库 `unittest`，真实 HTTP + 真实多线程并发

## 快速开始

```bash
python3 -m app --db data/app.db --host 127.0.0.1 --port 8080
# OpenAPI 文档
curl http://127.0.0.1:8080/api/v1/openapi
```

## 运行测试

```bash
python3 -m unittest discover -s tests -v
```

32 个集成测试全部基于真实 HTTP 服务，覆盖七项验收标准（含并发竞争与进程重启）。

## 设计要点

### 1. 既有区段互斥规则保持不变（`app/sections.py`）

- 对称互斥矩阵，区段与自身恒互斥；室内区段 A/B/C 两两互斥；D 与露天集结区
  Z1..Z8 与室内区段允许并行（新增区段只是扩展矩阵，A/B/C/D 之间的既有取值不变）。
- 窗口使用半开区间 `[start, end)`：**首尾相接不算冲突/不重叠**。
- 资源稀缺性是**独立的第二道维度**：区段允许并行 ≠ 资源可超卖。

### 2. 不超卖：应用事务 + 数据库触发器双保险（`app/db.py`）

- 每次写操作 `BEGIN IMMEDIATE`，SQLite 将写者串行排队，消除 read-then-write 竞争；
- 触发器 `trg_alloc_no_overlap`：同一实例上任何与新承诺**窗口重叠**的有效承诺
  （明细行 `HELD`）都会被数据库直接拒绝（绕过应用也无法写入）；
- 触发器 `trg_alloc_unit_ready` / `trg_alloc_resource_online`：维修/失效实例与
  下线资源不允许新增承诺；
- 失败为整单原子（all-or-nothing），不会出现“部分占量”。

### 3. 资源状态模型

- 资源（catalog）：`online` 上线/下线；推导状态 `AVAILABLE / OCCUPIED / MAINTENANCE`；
- 实例（unit）：存储状态 `AVAILABLE / MAINTENANCE`，`OCCUPIED` 由活动承诺实时推导
  （避免状态位翻转在异常/重启下不一致）。

### 4. 开工前逐项阻断与替换恢复（`app/service.py`）

- `GET /plans/{id}/readiness` 与 `POST /plans/{id}/start` 对每条明细、每个实例逐项
  检查，返回 `blockers`（含 `line_id/resource_code/unit_id/reason`）；
- 存在失效项时开工返回 `409 RESOURCE_NOT_READY` 并写 `START_BLOCKED` 审计；
- `POST /plans/{id}/replacements` 支持两种替换：
  - 同资源换机：`{line_id, old_unit_id, new_unit_id}`
  - 替代物资/设备：`{line_id, replacement_resource_code, quantity}`
  替换在同一事务内“删旧承诺→建新承诺”，新承诺仍受触发器校验；替换后重新给出就绪结论。

### 5. 幂等与释放

- `POST /reservations` 携带 `Idempotency-Key`：同键 + 同请求体重放返回首次结果
  （响应头 `Idempotency-Replayed: true`），**不重复占量**；同键不同体返回
  `409 IDEMPOTENCY_CONFLICT`；
- 无键重复预约返回 `409 PLAN_ALREADY_RESERVED`；
- `POST /release` 销记：条件更新 `... WHERE status='CONFIRMED'` 保证并发下**仅释放一次**，
  重复调用返回 `already_released=true`；释放后窗口库存立即可再订。

### 6. 审计与重启一致性

- 关键动作（创建/预约/拒绝/阻断/开工/释放/替换/资源运维）写 `audit_log`；
- 触发器禁止 `UPDATE`/`DELETE` 审计日志（append-only）；
- 预约、承诺、库存、审计全部落 SQLite，重启后库存、预约状态与历史记录逐行一致。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET  | `/api/v1/health` | 健康检查 |
| GET  | `/api/v1/openapi` | OpenAPI 3.0.3 规范 |
| GET  | `/api/v1/sections` | 区段与互斥矩阵（既有规则） |
| GET/POST | `/api/v1/resources` | 资源清单 / 登记资源 |
| GET  | `/api/v1/resources/{code}` | 资源详情与实时库存 |
| POST | `/api/v1/resources/{code}/online` `/offline` | 资源上线 / 维修下线 |
| GET  | `/api/v1/resource-units?resource_code=` | 实例清单与状态 |
| POST | `/api/v1/resource-units/{id}/available` `/maintenance` | 单件恢复 / 失效 |
| GET/POST | `/api/v1/plans` | 计划列表 / 创建（执行区段互斥校验） |
| GET  | `/api/v1/plans/{id}` | 计划详情（含预约） |
| POST | `/api/v1/plans/{id}/reservations` | 预约（支持 `Idempotency-Key`） |
| GET  | `/api/v1/plans/{id}/readiness` | 开工就绪逐项检查 |
| POST | `/api/v1/plans/{id}/start` | 开工（失效逐项阻断） |
| POST | `/api/v1/plans/{id}/replacements` | 替换失效占用 |
| POST | `/api/v1/plans/{id}/release` | 销记释放（幂等，仅一次） |
| GET  | `/api/v1/audit` | 审计日志（分页/过滤） |

### 预约请求示例

```json
POST /api/v1/plans/1/reservations
Idempotency-Key: reserve-plan-1-001

{
  "items": [
    {"resource_code": "OXYGEN_KIT", "quantity": 2,
     "window_start": "2026-10-01T00:00:00Z",
     "window_end":   "2026-10-01T02:00:00Z"},
    {"resource_code": "PROTECTIVE_SUIT", "quantity": 3}
  ]
}
```

明细窗口可省略，缺省取计划窗口。

## 验收对照

| 验收项 | 测试 |
| --- | --- |
| 重叠窗口竞争时仅一个计划成功 | `test_overlapping_windows_only_one_plan_wins` |
| 首尾相接窗口可复用 | `test_back_to_back_window_reuses_units` / `test_trigger_blocks_overlap_at_database_level` |
| 重复预约/重试不重复占量 | `test_repeated_reserve_same_key_does_not_double_allocate` / `test_duplicate_reserve_without_key_rejected` / `test_concurrent_same_key_single_winner` |
| 并发请求不超卖 | `test_eight_parallel_plans_three_units_exactly_three_win` / `test_20_concurrent_requests_never_oversell` |
| 开工前下线被明确阻断 | `test_prestart_failure_blocks_item_by_item` / `test_resource_offline_before_start_blocks` |
| 替换后恢复开工 | `test_same_resource_unit_replacement_recovers_start` / `test_alternative_resource_replacement_recovers_start` |
| 销记仅释放一次 | `test_release_only_once_and_concurrent` |
| 重启后库存/预约/历史一致 | `test_restart_preserves_inventory_reservations_and_history` |
| 持久化硬约束 / 审计只追加 | `tests/test_persistence.py`（触发器直写验证 + `test_audit_log_is_append_only`） |
