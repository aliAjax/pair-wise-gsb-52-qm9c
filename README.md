# 特殊教育支持计划合规

纯Python标准库实现的特殊教育支持计划合规原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 争议处理链

```
支持计划(v1) ──家长异议──▶ open ──受理──▶ accepted ──复核裁定──▶ upheld（冻结修订作废）
                 │                         │                      └▶ rejected（修订解冻）
                 └──学校可同时提交修订草案（proposed），受理后冻结为 frozen
计划确认修订 ──▶ plan_version+1（留快照），未开始补服务 void 后按新版重算，已确认补服务保留当时快照
监护人撤回同意 ──▶ 未开始补服务 void 且不再重算；已有服务与审计保留原依据版本
结案 ──▶ 先算缺口：未决异议 / 超期缺口 / 版本不一致（含未确认修订）任一存在即 409 拦截
```

关键规则：

- **受理冻结**：异议受理（accept）后，全部 `proposed` 修订转为 `frozen`；冻结期不能新增或确认修订。已有服务台账与审计事件一律保留原 `basis_version`，不重写。
- **授权范围**：复核人必须显式持有 `dispute:uphold` 或 `dispute:reject` 才能做对应裁定；跨机构还需 `dispute:any_org`（`admin` 角色放行）。
- **版本快照**：计划创建、每次确认修订、每次常规/补服务登记都在 `plan_snapshots` 与 `service_entries.plan_snapshot` 留快照；补服务按确认当时版本计入履约。
- **补服务重算**：`planned` 记录在计划升版或同意撤回时置 `void`；升版后按 `service_minutes - delivered_minutes - 已确认补服务` 重算（60分钟一段）；`confirmed` 记录不参与失效。
- **双窗口冲突**：异议与修订（或两个修订）基于同一 `expected_version` 并发提交时，后到者收到 409，`details.current_version` 给出新版本，`details.draft` 原样回传其填写内容。
- **完整批次**：所有写操作可用 `X-Batch-Id` 幂等化。提交前崩溃 → 批次停留在 `reserved`，同批次重试在一个事务内完整重放；提交后响应丢失 → 重试直接回放首次结果。补服务生成以批次为幂等键，重试不重复。
- **结案拦截**：`GET /api/records/{id}/readiness` 与 close 动作都返回 `blockers` 明细（`open_dispute` / `overdue_gap` / `version_mismatch`）。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型（含 Actor.scopes）、错误和基础校验。
- `src/rules.py`：状态转换、授权范围、争议/修订/补服务生命周期、重算与结案缺口规则。
- `src/repository.py`：SQLite建表、事务网关（TxGateway）、批次表、幂等键与故障注入钩子。
- `src/service.py`：用例编排（异议受理、裁定、修订确认、补服务、批次恢复、readiness）。
- `src/http_api.py`：HTTP路由、`X-Batch-Id`/`X-Scopes` 头与统一错误响应（409携带details）。
- `src/audit.py`：事件时间线。
- `static/index.html`：争议链演示页面（双窗口、冲突保留填写、缺口详情）。
- `tests/`：完整流程、规则计算、失败注入/批次恢复、HTTP端到端测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8328
```

默认端口为`8328`，默认数据库位于项目目录。服务启动时自动建表（含对旧库的轻量迁移）。

## 主要接口

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`、`X-Scopes`（空格/逗号分隔）和`X-Batch-Id`（写操作幂等）。

原有：

- `GET /health` / `GET /`
- `GET /api/records?state=&limit=` / `GET /api/records/{id}` / `GET /api/stats`
- `GET /api/records/{id}/audit`
- `POST /api/records`：`{"reference":"...","data":{...}}`
- `POST /api/records/{id}/actions/{action}`：action 为
  `consent`、`withdraw_consent`、`activate`、`log_service`、`review`、`amend`、`close`；
  请求体 `{"expected_version":1,"data":{...}}`。

争议链：

- `POST /api/records/{id}/disputes`：家长提交异议（`data.reason` 必填）。
- `GET  /api/records/{id}/disputes`
- `POST /api/disputes/{did}/accept`：受理并冻结未确认修订（administrator/admin）。
- `POST /api/disputes/{did}/decide`：`data.decision=upheld|rejected`，`data.decision_note` 必填；按 `X-Scopes` 授权裁定。
- `POST /api/records/{id}/amendments`：提交修订草案（不直接升版；`data.amendment_reason`、`data.updated_goals`、可选 `data.service_minutes`）。
- `GET  /api/records/{id}/amendments`
- `POST /api/amendments/{aid}/confirm`：确认修订，计划升版、留快照、补服务失效重算。
- `GET  /api/records/{id}/service-entries`：服务台账（regular/opening/makeup，含 basis_version 与快照）。
- `GET  /api/records/{id}/snapshots`：计划版本快照列表。
- `POST /api/makeup/{mid}/confirm`：确认一条补服务，保留确认当时版本快照并计入履约。
- `GET  /api/records/{id}/readiness`：结案就绪检查，返回 `can_close` 与 `blockers` 明细。
- `GET  /api/batches/{batchId}`：查询批次状态（reserved/committed、attempts、首次响应）。

冲突响应示例（后到者保留填写内容）：

```json
{
  "error": "conflict",
  "message": "版本冲突，请刷新后重试；您填写的内容已保留",
  "details": {"current_version": 5, "expected_version": 4, "draft": {"reason": "我的异议填写"}}
}
```

写入中断时返回可恢复错误，凭同一 `X-Batch-Id` 重试即可：

```json
{"error": "conflict", "details": {"recoverable": true, "batch_id": "batch-7f3a"}}
```

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突、受理冻结、scope 授权、
补服务失效重算与快照保留、双窗口并发、提交前/提交后故障注入下的批次恢复，以及 HTTP 端到端链路。
