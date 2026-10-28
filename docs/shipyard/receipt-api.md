# 船厂拍照 OCR 收货与少件补货 API（第二切片）

本文记录吊牌拍照 OCR 校验、原图留证、按实收入库、少件报送和原批次补货接口。
所有接口位于 `/api/shipyard`，复用 Sentry 的账号、仓库授权、审计日志和
数据库事务。

## 运行时识别边界

- 现场不读取二维码或条码，每托只拍一张吊牌原图。
- 固定 Excel 清单由程序按模板解析，不调用 AI。
- 吊牌打印字段由公网 OCR 识别，原图同时作为不可替换的证据保存。
- 服务端重新执行绑定、票货、物料编码、品名、托盘后缀、数量和照片校验；
  OCR 只是字段提取器，不决定业务是否通过。

## 接口

### 拍摄并识别吊牌

`POST /api/shipyard/inbound/tickets/<ticket_id>/label-recognition`

使用 `multipart/form-data` 上传一个 `file`：JPEG、PNG 或 WebP，最大 10 MiB。

原图先保存，再调用公网 OCR。接口返回识别字段、字段冲突、是否需要补录托盘后缀、
照片和识别记录。默认强制字段是 `material_code,item_name,tag_suffix`；其他已识别
字段若与清单不一致也会定位到具体字段。

如果 OCR 漏读托盘后缀，小程序要求现场按吊牌核实后补录 `1-1` 格式后缀。其他强制
字段漏读时不提供手填代替，必须调整角度重拍。OCR 未配置或服务失败时，照片和
`FAILED` 识别记录仍保留，但不能用于入库。

旧接口 `POST /api/shipyard/inbound/tickets/<ticket_id>/scan` 和
`POST /api/shipyard/inbound/tickets/<ticket_id>/photos` 仅供历史客户端兼容，
不属于当前现场操作流程。

### 确认收货

`POST /api/shipyard/inbound/tickets/<ticket_id>/receipts`

```json
{
  "idempotency_key": "receipt-20261027-0001",
  "received_quantity": 3,
  "difference_reason": "少 1 件，已联系工厂补货",
  "client_device_id": "PDA-A01",
  "pallets": [
    {
      "tag_suffix": "1-1",
      "label_recognition_id": "11111111-1111-1111-1111-111111111111",
      "photo_id": "00000000-0000-0000-0000-000000000000"
    }
  ],
  "group_photo_id": null,
  "area_photo_id": null,
  "difference_photo_id": null
}
```

同一次请求包含多个托盘时，后缀必须同属一个托组号、互不重复，并保持
`1-1`、`1-2` 格式。若票货已配置预计托盘数，则整票数量到齐时必须同时完成
托盘齐套，未齐套不能标记为全部到货。

正常无差异时，小程序默认把本次实收数量填为票货剩余应收数量。允许少件少托的
部分到货：只要本次实收少于剩余应收数量，就必须填写差异原因；此时不要求预计托盘
全部到齐。若本次实收使整票数量达到计划数量，则全部预计托盘必须已经登记。后续
补收货以“剩余应收数量”为上限，不会再把整票计划数量误当成本次应收数量。

小程序一次拍摄同时完成“识别”和“留证”：同一张原图只上传一次，既产生照片记录，
也产生 OCR 记录。正常路径不打开扫码框。

### 少件报送

`PUT /api/shipyard/inbound/tickets/<ticket_id>/shortage`

```json
{
  "status": "REPORTED",
  "note": "已微信通知工厂补 1 件"
}
```

`status` 支持 `REPORTED`（已报工厂）和 `IN_TRANSIT`（补货在途）。只有尚未
全部到货的票货可以登记。

### 查看照片

`GET /api/shipyard/photos/<photo_id>`

需具备对应仓库访问权限，以附件方式返回原图。

## 原子入库动作

确认收货在同一数据库事务中完成：

1. 校验仓库权限、清单状态、票货状态和剩余应收数量。
2. 校验 OCR 强制字段、已识别的船号、工程号、制作图、厂家和吊牌数量。
3. 校验收货记录属于当前票货、照片未被复用、托盘后缀唯一、托组号一致及预计托盘数。
4. 新增票货下的托盘单元，并记录 OCR 结构化字段、安装托盘号、批次号、收货人和到货时间。
5. 新增收货确认、托盘关联、照片关联和库存流水。
6. 按本次实收数量增加票货累计实收和在库数量，吊牌总件数不重复累加。
7. 更新清单行和清单状态，少件保留待补数量但不冻结已收库存。
8. 写审计日志并提交；任一步失败则整笔回滚。

`idempotency_key` 以票货为作用域唯一。网络重试或连续点击返回首次成功结果，
不会重复增加库存。

## 状态

票货状态：

- `EXPECTED`：待收货。
- `PARTIAL`：部分到货，已收库存可正常使用。
- `IN_STOCK`：计划数量已经全部实收。

缺件状态：

- `NONE`：无缺件。
- `PENDING_REPORT`：存在缺件，等待报工厂。
- `REPORTED`：已报工厂。
- `IN_TRANSIT`：工厂补货在途。
- `PARTIAL_REPLENISHMENT`：已经补到一部分，仍有缺口。
- `REPLENISHED`：原票货累计数量已经补足。

清单状态在收货过程中由 `CONFIRMED` 自动转为 `PARTIAL`，全部票货到齐后转为
`CLOSED`。

## 当前限制

- `shipyard_cargo_tickets`、`shipyard_receipts` 和
  `shipyard_stock_ledger` 当前是船厂库存的权威账。
- 尚未把船厂票货同步到 Sentry 通用 `items` / `inventory` 聚合表；需要先确定
  船厂物料编码和通用物料主数据映射，不能按品名静默创建 SKU。
- 配送需求、派工单、发货单和打印已由第三切片实现，见
  [`outbound-documents-api.md`](outbound-documents-api.md)。
- 正式出库清单识别、整票多吊牌拍照 OCR、照片留证、库存扣减和装车确认见
  [`formal-outbound-api.md`](formal-outbound-api.md)。
- 当前自动化测试使用固定 OCR 结果；配置真实百度凭据后仍需用现场吊牌验证识别率。

## 验证

- 全后端：`3043 passed, 55 skipped, 11 warnings`；
- 吊牌 OCR 解析、provider 契约、收货/出库完整闭环共 8 项专项测试通过；
- 管理端 32 个测试文件、203 项测试通过，生产构建通过；
- 微信小程序 17 项 Node 测试通过；
- 触及 Python 文件 Ruff、格式检查、Bandit、迁移幂等检查通过；
- `pip-audit` 和 `npm audit` 均无已知漏洞。
