# 船厂正式出库、整票核销与装车 API（第四切片）

本文记录船厂固定出库清单识别、库存分配、整票多吊牌拍照识别、照片留证、库存扣减、
装车确认和微信小程序接口。所有接口位于 `/api/shipyard/outbound`，复用 Sentry
的登录、角色、仓库权限、审计日志和数据库事务。

## 数据边界

086 新增两层独立单据：

- `shipyard_outbound_manifests`：船厂下发的原始出库需求，保留原文件、模板版本、
  清单行和库存匹配预览。
- `shipyard_outbound_orders`：管理员确认后生成的正式出库单，包含不可拆的整票
  `shipyard_outbound_tasks`。

票据层级如下：

```text
船厂正式出库清单
  → 正式出库单
    → 出库需求行
      → 1..N 张整票任务（每张任务对应一个 cargo ticket）
        → 全部托盘吊牌照片
          → 一次库存扣减
```

清单确认只把票货置为 `ALLOCATED`，不改变库存数量。只有现场拍齐全部托盘吊牌、
OCR 字段全部通过并与当前票货一致后提交整票核销时，才在同一事务中扣减
`on_hand_quantity`、写库存流水、更新照片和出库单状态。

## 物料与库存强校验

正式出库清单的以下字段必须存在：

- 船号；
- 工程号；
- 制作图编号；
- 物料编码；
- 物料名称；
- 需求数量。

库存匹配同时要求：

- 船号/工程号/制作图命中当前生效的项目绑定；
- `material_code`、`item_name`、`item_spec` 规范化后完全一致；
- 票货有可用在库数量；
- 全部实际托盘后缀存在、唯一且属于同一票货；
- 出库清单给出的预计托盘数和托盘后缀与库存一致；
- 一张票货只分配一次，不能跨出库任务复用。

系统不会按品名静默建立 SKU，也不会在物料编码缺失时退化为品名匹配。历史入库票货
若没有物料编码，会在预览中显示为无匹配，需先补齐来源数据或走受控调整。

## 出库清单识别

### 上传预览

`POST /api/shipyard/outbound/formal-manifests/preview`

使用 `multipart/form-data`：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `warehouse_id` | 是 | 所属仓库 |
| `file` | 是 | 固定模板 `.xlsx`，最大 10 MiB |
| `factory_shipment_no` | 否 | 文件未提供船厂出货单号时补充 |
| `manifest_no` | 否 | 内部导入号；不填则自动生成 |

固定模板至少需要识别：

- 清单行号；
- 船厂出货单号；
- 船号、工程号、制作图号；
- 物料编码、物料名称、规格；
- 需求数量、单位、需求日期；
- 预计托盘数、托盘后缀。

接口返回逐行绑定状态、候选整票、区域快照、托盘后缀和库存缺口。同一文件
SHA-256 或未作废的内部导入号重复时返回 `409`。

### 查询、原文件和生命周期

```text
GET  /api/shipyard/outbound/formal-manifests?warehouse_id=1
GET  /api/shipyard/outbound/formal-manifests/<manifest_id>
GET  /api/shipyard/outbound/formal-manifests/<manifest_id>/file
POST /api/shipyard/outbound/formal-manifests/<manifest_id>/confirm
POST /api/shipyard/outbound/formal-manifests/<manifest_id>/void
```

确认接口无编辑字段，使用服务端重新计算后的分配结果生成正式出库单。库存不足不会
被写成负库存；没有合适整票的需求行保留 `SHORTAGE`。已生成出库单的清单不能作废。

## 正式出库单和移动任务

管理端查询：

```text
GET /api/shipyard/outbound/orders?warehouse_id=1
GET /api/shipyard/outbound/orders/<outbound_order_id>
```

小程序查询：

```text
GET /api/shipyard/outbound/mobile/orders?warehouse_id=1
GET /api/shipyard/outbound/mobile/orders/<outbound_order_id>
```

移动端接口同时校验角色和 `shipyard-outbound` 页面权限。列表、详情、吊牌拍照识别、
旧扫码/照片兼容接口、整票确认、装车确认和出库照片下载都受同一门禁保护；只有
`OUTBOUND` 角色但没有页面权限时返回 `403`。知道有效的任务或照片 ID 也不能绕过
页面权限。

出库单状态：

- `PENDING`：已生成，等待现场出库；
- `PARTIAL`：多张整票中部分已完成，或仍有库存缺口；
- `OUTBOUND`：全部已分配整票完成且无缺口；
- `LOADED`：整单装车已确认。

单张任务只有 `PENDING → OUTBOUND`，不存在单票部分出库。

## 整票吊牌拍照识别与留证

现场不使用二维码、条码或扫码设备。正常路径是每托拍一张吊牌原图，原图留证，
同时由公网 OCR 识别吊牌打印字段。二维码字段只为历史数据兼容，不参与新流程。

### 拍摄并识别当前吊牌

`POST /api/shipyard/outbound/tasks/<task_id>/label-recognition`

使用 `multipart/form-data` 上传一个 `file`：JPEG、PNG 或 WebP，最大 10 MiB。

服务端按以下顺序处理：

1. 原图写入 `shipyard_photos`；
2. 调用配置的 OCR 服务识别打印文字；
3. 保存原始文字、结构化字段、提供商、置信度和失败原因；
4. 与当前出库任务的物料编码、品名、整票数量及托盘后缀比对；
5. 返回字段冲突、是否需要补选托盘后缀，以及其他票货的候选任务。

成功响应示例：

```json
{
  "matched": true,
  "needs_suffix": false,
  "recognized": {
    "material_code": "MAT-001",
    "item_name": "船用支架",
    "batch_no": "H1613-B01",
    "tag_suffix": "1-1",
    "label_quantity": "4"
  },
  "mismatches": [],
  "candidate_tasks": [],
  "photo": {
    "photo_id": "00000000-0000-0000-0000-000000000000",
    "file_sha256": "..."
  },
  "recognition": {
    "recognition_id": "11111111-1111-1111-1111-111111111111",
    "status": "MATCHED"
  }
}
```

默认强制字段由 `SHIPYARD_OCR_REQUIRED_FIELDS` 配置，出库默认是
`material_code,item_name,tag_suffix`。字段一旦识别到就必须与任务完全一致；
未识别到强制字段时返回 `mismatches[].reason=REQUIRED`，不能用任务快照代替
吊牌值。

如果 OCR 只漏读托盘后缀，接口返回 `needs_suffix=true` 和本任务全部托盘后缀。
小程序从“尚未拍摄的后缀”中选择实际托盘，不再要求重打整张吊牌。照片已经保存，
正式确认时仍会重新验证 OCR 字段、照片、托盘和任务。

如果吊牌物料编码和品名命中另一张待出库任务，响应中的
`candidate_tasks` 给出任务号、出库单号和任务 ID，小程序可直接跳转到正确任务。

OCR 服务未配置或暂时不可用时，照片和 `FAILED` 识别记录仍会保存，接口分别返回
`503 ocr_unavailable` 或 `502 ocr_provider_error`。失败记录不能用于核销。

旧接口 `POST /api/shipyard/outbound/tasks/<task_id>/scan` 和
`POST /api/shipyard/outbound/tasks/<task_id>/photos` 仅为历史客户端兼容保留，
不在当前现场操作和文档流程中使用。

## 确认整票出库

`POST /api/shipyard/outbound/tasks/<task_id>/confirm`

```json
{
  "idempotency_key": "outbound-20261027-0001",
  "client_device_id": "WX-MINI-001",
  "pallets": [
    {
      "tag_suffix": "1-1",
      "label_recognition_id": "11111111-1111-1111-1111-111111111111",
      "photo_id": "00000000-0000-0000-0000-000000000000"
    },
    {
      "tag_suffix": "1-2",
      "label_recognition_id": "22222222-2222-2222-2222-222222222222",
      "photo_id": "11111111-1111-1111-1111-111111111111"
    }
  ]
}
```

事务顺序：

1. 锁定任务、票货和全部托盘；
2. 校验已拍照托盘后缀集合与任务托盘集合完全相等，缺托、多托、重复托均拒绝；
3. 校验每托 OCR 记录、强制字段、批次、照片归属和照片唯一性；
4. 写一条出库核销记录和逐托关联记录；
5. 将票货 `on_hand_quantity` 置零、状态改为 `OUTBOUND`，全部托盘改为
   `OUTBOUND`；
6. 写一条负数库存流水和审计日志；
7. 更新需求行、整票任务和整单状态。

任一步失败整笔回滚。同一任务用相同幂等键重试返回首次结果；使用新键重复核销返回
`409 outbound_state_conflict`。

## 装车确认

`POST /api/shipyard/outbound/orders/<outbound_order_id>/loading`

```json
{
  "idempotency_key": "loading-20261027-0001",
  "client_device_id": "WX-MINI-001",
  "notes": "车辆沪A12345，两托已装车"
}
```

只有全部整票任务完成且所有需求行无库存缺口时，整单才处于 `OUTBOUND` 并允许装车。
确认后写 `shipyard_outbound_loading_confirmations`，状态变为 `LOADED`，记录装车人、
装车时间、设备和备注。装车动作不改库存，不得与逐票出库合并。

## 微信小程序

`miniprogram/` 提供：

- 收货任务与整票连续拍摄；
- 出库单、整票任务和区域路径；
- 逐托拍吊牌、公网 OCR 字段比对和原图留证；
- 整票核销和整单装车确认。

小程序使用 `/api/auth/login` 获取现有 JWT，不创建第二套用户。正式部署须配置
HTTPS API 域名、微信 request/uploadFile 合法域名和真实 AppID。

## 当前限制

- 船厂库存仍以 `shipyard_cargo_tickets`/`shipyard_stock_ledger` 为权威账，尚未
  同步到 Sentry 通用 `items`/`inventory` 聚合表。
- 当前没有主管异常强出、撤销装车和撤销出库审批流；相关入口保留为后续切片。
- 公网 OCR 会把吊牌原图发送到所配置的云服务，现场需知悉该数据流向；
- 首期只接百度通用高精度文字识别，真实吊牌识别率需用现场样本验证和调优；
- 当前自动化测试使用固定 OCR 结果，未使用真实百度凭据，不能把 mock 结果当成现场
  识别率验收；
- 固定模板字段别名覆盖常见船厂表头，但正式上线前仍需用真实样例锁定模板版本。

## 验证

- 全后端：`3050 passed, 55 skipped, 11 warnings`；
- 087 迁移在 `sentry_test` 连续执行两次成功；
- 全新空库执行 `db/schema.sql` 后，正式出库、任务、核销和装车 4 类核心表均存在；
- 管理端：33 个测试文件、204 项测试通过，生产构建通过；当前 Windows 主机上
  默认并行模式会触发 Vitest fork/jsdom 超时，因此全量以 4 个分片、单 worker
  运行；超时文件单独重跑均通过，未发现页面断言缺陷；
- 微信小程序：17 项 Node 测试通过，触及 JavaScript 文件语法检查通过；
- 触及 Python 文件 Ruff、格式检查、Bandit 和 `git diff --check` 通过；
- `pip-audit` 无已知漏洞，`npm audit` 为 0 个漏洞。

全量权限矩阵固定覆盖 49 个船厂路由：无页面权限全部返回 `403`；26 个非 GET
路由在拥有页面的只读账号下全部返回业务角色 `403`。区域船号映射更新已按映射
实际所属仓库校验，仓库 2 主管不能使用仓库 1 的映射 ID 修改配置。

本切片使用独立测试库完成了一次服务级完整闭环：创建船号绑定和区域 → 导入并确认
到货清单 → 两次吊牌 OCR 收货 → 导入并确认正式出库清单 → 两次吊牌 OCR
核验物料编码/品名 → 扣减库存 → 整单装车。最终库存余额为 `0.000`、出库单为
`LOADED`，每托照片和 OCR 记录一一绑定。OCR 调用使用固定结果，不等同于真实云
OCR、小程序真机和现场吊牌 UAT。

可重复的真实 HTTP E2E 由 `api/tests/e2e_shipyard_http.py` 执行。该脚本会启动
本机回环 Flask 服务，完整走登录、配置、清单确认、两托 OCR 收货、缺后缀补选、
整票扣库、装车、后台重新查询和原图字节校验；同时覆盖缺托、重复照片、错误物料
编码/品名、OCR provider 失败、角色和页面权限、跨仓库隔离、同幂等键并发、新幂等键
重复操作 `409`，以及并发装车只写一次。收货、核销、装车分别提交 50 个请求，
最多 10 路并发；结束后检查票货余额、库存流水、收货累计、照片/OCR 归属、托盘
状态和装车确认唯一性。额外使用 250 请求、50 并发连续执行两轮并通过；E2E 启动
时重置进程内限流计数，避免重复运行继承上一轮的 `300/min` 配额。脚本最后恢复
测试库种子数据。
