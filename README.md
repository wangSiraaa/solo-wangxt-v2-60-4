# 本地防护物资 / 演练设备预约服务

在不改变现有区段互斥规则的前提下，提供本地模拟的防护物资/演练设备预约能力。
计划可声明所需资源、数量与占用窗口；即使互斥矩阵允许两个计划并行，也不能
超卖同一稀缺资源。实现仅依赖 Python 3.11 标准库（`sqlite3` +
`http.server`），无第三方依赖。

## 运行

```bash
python3 -m app.app --db data/reservation.db --host 127.0.0.1 --port 8080
```

- `GET /openapi.json`：OpenAPI 3.0 描述
- `GET /healthz`：健康检查

## 测试

```bash
python3 -m unittest discover -s tests -v
```

集成测试通过真实 HTTP 请求覆盖全部验收点：重叠窗口竞争仅一个成功、首尾相
接窗口复用、重复预约/重试不重复占量、并发不超卖、开工前资源下线逐项阻断、
替换后恢复开工、销记仅释放一次、重启后库存/预约/审计一致，以及数据库触发
器对绕开服务的直接写入的兜底拦截。

## 设计要点

### 区段互斥规则（只读，不修改）

- `segment_rules` 表承载本地互斥矩阵（规范化区段对为主键）。
- 预约时先按矩阵校验并行性；资源层只做**额外**的稀缺资源约束，永不绕过或
  改写区段规则。`GET /segment-rules` 可查看矩阵。

### 资源模型与状态

- `resource_skus`：资源品类；`resource_units`：可预约的最小实物单元。
- 基础状态：`available` / `maintenance` / `retired`（`retired` 不可再流转）。
- 有效状态额外派生 `occupied`：当前时刻落在某未销记计划占用窗口内即为占用。
- 维修中的资源不参与分配；开工中的资源禁止进入维修（`resource_in_use`）。

### 不超卖的三层防线

1. 所有写操作使用 `BEGIN IMMEDIATE` 事务，SQLite WAL + 写锁串行化并发请求。
2. 分配查询在同一事务内按窗口重叠（半开区间 `[start, end)`）筛选可用单元，
   首尾相接的窗口天然可复用同一资源。
3. 数据库触发器 `trg_item_no_overlap_*` 兜底：任何（包括绕开服务的）重叠
   活跃占用写入都会被拒绝。

### 幂等

- 预约：`idempotency_key` 唯一；相同请求体重放返回首个结果（`replayed=true`），
  相同键不同请求体返回 `idempotency_conflict`；`plan_id` 唯一防止重复占量。
- 替换：可携带 `idempotency_key`，重试返回原替换结果。
- 销记：重复调用返回 `already_released=true`，只产生一条 `reservation_released`
  审计记录。

### 开工前阻断与替换恢复

- `GET /reservations/{plan_id}/readiness` 与 `POST .../start` 逐项检查每个
  已分配单元；任一单元失效（维修/退役）即返回 `prestart_blocked` 及逐项明细。
- `POST /reservations/{plan_id}/replacements` 支持指定或自动选择同品类可用
  单元替换；旧占用行保留为历史（`active=0`），替换后 readiness 恢复、可开工。

### 审计

`audit_log` 仅追加，覆盖资源创建/扩量/状态变更、预约成功与失败、开工、开工
阻断、替换、销记，均带 `event_id`、计划、请求标识与明细 JSON。

## 主要 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/resources` | 创建资源品类与实物单元 |
| POST | `/resources/{sku}/expand` | 扩充实物单元 |
| GET | `/resources` / `/resources/{sku}/units` | 库存与单元状态 |
| POST | `/resources/units/{unit_id}/status` | 可用/维修/退役流转 |
| POST | `/reservations` | 声明需求并原子预约（幂等） |
| GET | `/reservations` / `/reservations/{plan_id}` | 预约查询 |
| GET | `/reservations/{plan_id}/readiness` | 开工前逐项检查 |
| POST | `/reservations/{plan_id}/start` | 开工（失效即阻断） |
| POST | `/reservations/{plan_id}/replacements` | 失效单元替换 |
| POST | `/reservations/{plan_id}/release` | 销记释放（仅一次） |
| GET | `/segment-rules` | 现有区段互斥矩阵 |
| GET | `/audit-logs` | 审计历史（可按 `plan_id` 过滤） |
