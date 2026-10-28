# 船厂入库 API（第一切片）

本文记录船厂业务域的第一组可用接口。接口位于 `/api/shipyard`，复用
Sentry WMS 的账号、仓库授权、审计日志和数据库事务。

第二切片“吊牌拍照 OCR、照片证据、少件补货”见
[`receipt-api.md`](receipt-api.md)。

第三切片“配送需求识别、派工单、发货单和打印”见
[`outbound-documents-api.md`](outbound-documents-api.md)。

## 已实现范围

### 船号/工程号/制作图绑定

- `GET /api/shipyard/bindings`
- `POST /api/shipyard/bindings`
- `PUT /api/shipyard/bindings/<binding_id>`

同一组 `ship_no + engineering_no + production_drawing_no +
manufacturer_name` 只能存在一条有效绑定。

船号/工程号/制作图绑定是系统级项目主数据，所有仓库共享，不复制成仓库级记录。
这样同一船号跨多个仓库存放时不会产生互相冲突的绑定副本。写入必须同时具备
`SHIPYARD_MANAGER` 角色和 `shipyard-settings` 页面权限，并写入审计日志。
清单、票货、托盘、照片和区域定置仍按所属仓库执行隔离。

### 船号与二级区域定置

中文管理端入口为 `/shipyard-settings`（侧边栏 `Shipyard Settings`）。船厂
主管可以在页面维护全局船号/工程号/制作图绑定和当前仓库的区域船号定置，
操作员不能修改。

- `GET /api/shipyard/zone-mappings?warehouse_id=1`
- `POST /api/shipyard/zone-mappings`
- `PUT /api/shipyard/zone-mappings/<mapping_id>`

二级区域直接使用 Sentry 的 `bins`，完整路径由所属 `zones` 和 `bins`
组成。一个区域可以配置多个船号；一个船号当前只能有一个有效主入库区域。

配置保存 `effective_from` / `effective_to` 时间窗。数据库使用 PostgreSQL
`tstzrange` 排斥约束阻止同一船号在同一时间出现两个主区域，同时允许今天、
下月等相邻的非重叠窗口预先配置。API 只使用当前时间窗内的映射。

### 入库清单识别

- `POST /api/shipyard/inbound/manifests/preview`
- `GET /api/shipyard/inbound/manifests?warehouse_id=1`
- `GET /api/shipyard/inbound/manifests/<manifest_id>`
- `GET /api/shipyard/inbound/manifests/<manifest_id>/file`
- `POST /api/shipyard/inbound/manifests/<manifest_id>/confirm`
- `POST /api/shipyard/inbound/manifests/<manifest_id>/void`

预览接口使用 `multipart/form-data`：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `warehouse_id` | 是 | 收货仓库 |
| `file` | 是 | `.xlsx` 工厂发货清单 |
| `manifest_no` | 否 | 不填时按日期和文件哈希生成 |
| `manufacturer_name` | 否 | 不填时从清单标题中的公司名提取 |

预览会创建状态为 `PARSED` 的入库清单和逐行明细，并返回：

- 识别出的表头行、工作表、忽略的辅助工作表；
- 每条清单行的来源行号、船号、图号、图名、件数、送货单号和 PO 号；
- 按船号计算出的指定二级区域；
- 缺少主区域的船号；
- 是否允许确认 `validation.can_confirm`。

清单行合同固定为：

> 一个来源清单行 = 一张票货/托组，行内件数 = 整票或整托组总件数。

因此多托 `1-1/1-2` 不会在清单阶段把总件数重复相加。预计托盘数、每托
吊牌和实际实收数量由小程序吊牌拍照识别收货接口补充。

## 确认规则

确认接口只接受空 JSON 对象 `{}`。确认时系统重新读取当前区域配置，并执行：

1. 只允许 `PARSED` 状态确认，重复确认返回 `409`；
2. 每条明细必须唯一匹配当前有效的船号/工程号/制作图/厂家绑定；
3. 每个船号必须存在且只存在一个当前有效的主入库区域；
4. 一条清单行生成一张状态为 `EXPECTED` 的票货；
5. 票货保存区域路径快照，后续区域改名不破坏历史；
6. 票货写入 Sentry 的 `stock_lot_number` 映射值，为后续库存入账预留；
7. 清单状态改为 `CONFIRMED`，同时写入审计日志。

未配置区域时允许预览，但禁止确认，确保现场不能绕过船号定置规则。

预览同时保存原始 `.xlsx` 文件和 SHA-256。文件可通过 `/file` 下载用于追溯。
尚未确认的清单可填写原因后废弃；废弃状态不占用文件哈希，因此同一文件可以
重新导入。首期原文件存于 PostgreSQL，后续可平滑迁移到对象存储。

## 模板识别

解析器不写死 Excel 行号，而是在前 30 行中查找规范化后的表头。当前支持
的主要别名包括：

- 船号；
- 品名/物料名称/物资名称；
- 图号/制作图编号/制作图号；
- 图名/制作图名称/安装托盘名称；
- 件数/数量/整票件数；
- 物料编码（有则保存，正式出库必须与物料名称和规格一起匹配）；
- 送货单号；
- PO 号、PO NO；
- 送货地点、采购员、重量和货物类别。

当前验证样例：

- 工作表：`外高桥 `；
- 表头行：第 3 行；
- 业务行：36；
- 船号：7；
- 合计件数：2,326；
- `Sheet1`：识别为辅助页并忽略。

## 验证命令

```powershell
docker compose exec -T `
  -e TEST_DATABASE_URL=$testDbUrl `
  -e FLASK_ENV=development `
  -e SENTRY_ALLOW_INTERNAL_WEBHOOKS=false `
  -e SENTRY_ALLOW_HTTP_WEBHOOKS=false `
  api python -m pytest tests/shipyard tests/test_082_shipyard_core_migration.py -q
```

船厂业务、082-084 迁移及认证回归验证结果：`110 passed`。仅运行船厂业务与
082-084 迁移时为 `54 passed`，`services/shipyard` 覆盖率为 90%。
