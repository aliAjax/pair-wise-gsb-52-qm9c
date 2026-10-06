# 特殊教育支持计划合规

纯Python标准库实现的特殊教育支持计划合规原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误（可携带结构化详情）和基础校验。
- `src/rules.py`：状态转换、同意、服务履约、复查期限、计划版本、争议复核授权范围和缺口检查。
- `src/repository.py`：SQLite建表、事务工作区、争议/修订/服务记录/批次/草稿查询。
- `src/service.py`：用例编排、权限检查、乐观并发、补服务对账、批次幂等、草稿保留和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、失败场景与争议处理链测试。

## 争议处理链

支持计划（records）与家长异议（disputes）、计划修订（amendments）、服务记录（service_rows）按下列规则接成一条链：

1. **受理冻结**：家长对生效计划提交异议（`file_dispute`）后，记录进入`disputed`，所有“已提出、未确认”的修订立即冻结（`proposed → frozen`），争议期间不能提交或确认修订；已确认版本仍为现行依据。
2. **原依据保留**：争议期间已发生的服务照常登记，服务行写入当时的计划修订号与计划快照（`basis_revision`/`basis_snapshot`），审计时间线同样保留原依据，事后可说明按哪一版执行。
3. **按授权裁定**：`resolve_dispute`由复核人按授权范围裁定。`administrator`可裁定全部议题（service_gap/overdue/amendment/consent），`review_officer`只能裁定`service_gap`、`overdue`；越权返回403。裁定维持计划时冻结修订恢复为草案，撤回同意时修订作废。
4. **补服务失效与快照保留**：计划确认更新（`confirm_amendment`/`amend`）或同意撤回（`withdraw_consent`）时，“未开始”的补服务（pending）按旧版作废（void），按当前计划修订重算；已确认（confirmed）的服务与补服务保留当时快照、不重算。
5. **并发窗口**：家长异议窗口与学校修订窗口同时提交、版本冲突时，后到者得到409，其填写内容被存为草稿（drafts），响应中给出`current_version`与草稿，可在`GET /api/records/{id}/drafts`取回后基于新版本重提。
6. **批次恢复**：`POST /api/records/{id}/batch`以`batch_key`提交完整批次。批次为单事务原子提交；写入失败后用同一完整批次重试，运行中批次的未确认残留行先清理再整体执行；已完成批次直接回放结果，不重复执行，因此不会重复生成补服务记录。
7. **结案拦截**：存在未决异议、复查超期且仍有服务缺口、或待执行补服务依据版本与当前修订不一致时，`close`返回409，`details.gaps`逐条列出缺口；`GET /api/records/{id}/gaps`为只读缺口视图。

## 状态机

`draft → consented → active ⇄ under_review → closed`

争议链附加：`active → disputed → active`；同意撤回为`active/disputed → consent_withdrawn/disputed_consent_withdrawn → consented`。

## 启动

```bash
python3 app.py --db ./data.db --port 8328
```

默认端口为`8328`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/records/{id}/disputes`：异议列表。
- `GET /api/records/{id}/amendments`：修订列表（含 proposed/frozen/confirmed/rejected）。
- `GET /api/records/{id}/service-rows`：服务与补服务记录（含依据修订号与快照）。
- `GET /api/records/{id}/drafts?kind=...`：当前调用者在版本冲突中保留的草稿。
- `GET /api/records/{id}/gaps`：结案前缺口明细。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。
  动作包括原有 `consent/activate/log_service/review/amend/close`，以及争议链
  `file_dispute/resolve_dispute/withdraw_consent/propose_amendment/confirm_amendment/confirm_makeup`。
- `POST /api/records/{id}/batch`：幂等批次，请求体为
  `{"expected_version":1,"batch_key":"...","operations":[{"action":"log_service","data":{...}}]}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。角色包括
`case_manager`、`parent_rep`、`specialist`、`administrator`、`review_officer`与通配的`admin`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突，以及异议受理冻结、授权裁定、补服务失效重算、并发草稿、批次幂等恢复和结案缺口拦截。
