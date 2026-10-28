# 船厂配送需求、派工单与发货单 API（第三切片）

本文记录船厂配送需求 Excel 识别、派工单匹配、前置仓发货单生成和打印接口。
所有接口位于 `/api/shipyard/outbound`，复用 Sentry 的账号、仓库授权、审计
日志和数据库事务。

## 业务链路

```text
船厂托盘配送需求 Excel
    → 船厂物资提货派工单
    → 启东前置仓发货单
    → 实物发货和接收签收
```

三份单据当前不要求存在唯一贯穿编号。系统采用以下匹配边界：

- 自动匹配键：规范化后的 `工程号 + 托盘号`；
- 唯一候选：`MATCHED`；
- 多条候选：`AMBIGUOUS`，保留候选供人工选择；
- 没有候选：`UNMATCHED`；
- 管理员选择候选后：`MANUAL`。

`AMBIGUOUS` 和 `UNMATCHED` 不阻止派工单先保存，避免现场因为电子需求尚未
同步而停收。匹配只在同一仓库内进行，一条配送需求行最多关联一条派工明细。

## 配送需求识别

### 中文管理端

管理端页面路径为 `/shipyard-outbound`，侧边栏名称为 `Shipyard Outbound`，
使用页面权限键 `shipyard-outbound`。页面当前提供：

- 拖入或选择船厂 `.xlsx` 托盘工单；
- 查看识别警告、逐票来源行和原文件下载；
- 确认或填写原因后作废配送需求；
- 查看派工单匹配汇总，并在歧义候选间人工匹配；
- 生成、查看和打开 A4 横向打印发货单。

浏览器上传使用 `multipart/form-data`，由浏览器自行生成 boundary，管理端请求
层不会错误设置 JSON `Content-Type`。

### 上传并解析

`POST /api/shipyard/outbound/distribution-imports/preview`

使用 `multipart/form-data`：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `warehouse_id` | 是 | 需求所属仓库 |
| `file` | 是 | `.xlsx` 托盘工单 |

接口会保存原始文件、SHA-256、解析器版本、工作表名、表头行、字段映射和每个
来源业务行。文件哈希按仓库去重；作废文件不占用哈希。

解析器要求至少有：

- 工程号；
- 分段号；
- 托盘号；
- 托盘名称。

同时识别工单状态、申请人、部门、配送地址、联系人、需求时间、配送资源、
配送员、确认人、完成时间和确认状态。

当前样例中的 F 列没有表头但全部为“未到”。解析器只在业务区内恰有一个
无表头有数据列时，暂按 `delivery_status` 保存，并返回
`UNNAMED_HEADER_MAPPED` 警告，不能把该推断当成船厂正式字段定义。

需求时间按 `Asia/Shanghai` 解析，支持 Excel 日期时间和
`开始时间~结束时间` 形式。重复托盘、缺失必填值、无法解析的时间会让整批
文件返回 `422` 和逐行诊断，不上线半套数据。

### 查询、下载和生命周期

- `GET /api/shipyard/outbound/distribution-imports?warehouse_id=1`
- `GET /api/shipyard/outbound/distribution-imports/<import_id>`
- `GET /api/shipyard/outbound/distribution-imports/<import_id>/file`
- `POST /api/shipyard/outbound/distribution-imports/<import_id>/confirm`
- `POST /api/shipyard/outbound/distribution-imports/<import_id>/void`

状态为 `PARSED` 的记录确认后变为 `CONFIRMED`，只有已确认需求参与自动或
人工匹配。已经关联派工单的需求不能作废；未关联需求可填写原因后作废，
作废后允许重新导入同一文件。

## 派工单

### 创建

`POST /api/shipyard/outbound/dispatches`

```json
{
  "warehouse_id": 1,
  "dispatch_no": "PT202610278A00910008",
  "dispatch_date": "2026-10-27",
  "source_type": "IMAGE_OCR",
  "source_file_name": "dispatch.jpg",
  "idempotency_key": "dispatch-20261027-0001",
  "lines": [
    {
      "line_no": "1",
      "engineering_no": "H1404",
      "pallet_no": "E063-4002(1008)",
      "material_spec_raw": "E063-4002(1008) 0083-85 电舾件",
      "material_code": null,
      "material_name": "电舾件",
      "specification": "0083-85",
      "quantity": 4,
      "unit": "件",
      "expected_arrival_date": "2026-10-28",
      "keeper_name": "测试保管员",
      "keeper_phone": "13800000000",
      "remarks": "0#平台",
      "demand_line_id": null
    }
  ]
}
```

如创建时直接选择需求行，可填写 `demand_line_id`，系统按 `MANUAL` 保存。
否则系统自动匹配。

幂等键作用域为仓库。相同幂等键、相同派工单号和相同请求内容会返回首次
结果；相同键但内容变化会返回 `409 dispatch_state_conflict`。

### 查询和人工匹配

- `GET /api/shipyard/outbound/dispatches?warehouse_id=1`
- `GET /api/shipyard/outbound/dispatches/<dispatch_order_id>`
- `PUT /api/shipyard/outbound/dispatch-lines/<dispatch_line_id>/demand-match`

人工匹配请求：

```json
{
  "demand_line_id": "00000000-0000-0000-0000-000000000000"
}
```

系统重新校验需求状态、仓库、工程号、托盘号以及是否已被其他派工明细占用。
发货单生成后不允许继续修改匹配关系。

## 发货单

### 生成

`POST /api/shipyard/outbound/dispatches/<dispatch_order_id>/delivery-note`

```json
{
  "delivery_note_no": "202610270004",
  "issue_date": "2026-10-27",
  "idempotency_key": "delivery-note-20261027-0001"
}
```

一张派工单只生成一张发货单。发货单行固化以下打印证据：

- 来源派工单号和日期；
- 来源配送需求导入号及 Excel 来源行；
- 工程号、分段号和托盘号；
- 物资编码、名称、规格和原始描述；
- 数量、单位、保管员和备注；
- 接收人及接收时间字段，当前由现场签收环节后续填写。

相同派工单已生成单号或日期不同的发货单时返回
`409 delivery_note_state_conflict`，避免网络重试覆盖原单。

### 查询和打印

- `GET /api/shipyard/outbound/delivery-notes?warehouse_id=1`
- `GET /api/shipyard/outbound/delivery-notes/<delivery_note_id>`
- `GET /api/shipyard/outbound/delivery-notes/<delivery_note_id>/print`

打印接口返回 A4 横向 HTML，浏览器可直接打印或另存 PDF。所有业务文本经过
HTML 转义；页面显示来源派工单、来源文件、需求导入行、分段和物资规格。

## 后续正式出库切片

- 本切片只负责配送需求、派工单、发货单和打印，尚未扣减票货库存。
- 派工单 API 接收 OCR 客户端提交的结构化行，尚未提供图片上传 OCR 端点。
- 发货单接收人、接收时间和实收备注需要在出库核销切片中写入。
- 船厂票货仍使用独立库存权威账，尚未同步 Sentry 通用 `items` /
  `inventory`；物料编码映射确认前不能静默创建通用 SKU。

船厂正式出库清单识别、整票多吊牌拍照 OCR 核销、出库照片、库存扣减和装车确认已由
[`formal-outbound-api.md`](formal-outbound-api.md) 实现。085 的配送需求、派工单
和前置仓发货单仍保持独立链路。

## 验证

- 船厂业务与 085 迁移：`50 passed`，`services/shipyard` 覆盖率 `88%`；
- 船厂、085 迁移和认证回归：`111 passed`；
- 全后端：`3016 passed, 55 skipped, 0 failed`；
- 真实样例 `生产部门管电舾装件日配送需求10-26.xlsx`：识别 1 个有效工作表、
  第 1 行表头、6 条托盘需求、3 个工程号、6 个托盘号，并返回无表头列警告；
- 085 在开发库、测试库连续执行两次成功；
- 全新空库执行 `db/schema.sql` 后 085 的 7 张表全部存在；
- Ruff、格式检查、Bandit 和 `git diff --check` 通过；
- `pip-audit` 无已知漏洞，`npm audit` 0 个漏洞；
- 管理端 32 个测试文件、199 项测试和生产构建通过；
- 本期涉及的管理端文件 ESLint、全量生产构建通过；全仓仍有上游基线的
  `62 errors / 13 warnings`，不能描述为全仓 lint 清洁。
