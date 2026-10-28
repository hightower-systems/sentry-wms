# 船厂虚拟用户

前期使用虚拟账号验证角色、页面权限和仓库隔离，不需要真实员工账号。

## 创建

仅允许在 `development` 或 `test` 环境运行：

```bash
FLASK_ENV=development \
SHIPYARD_DEMO_WAREHOUSE_IDS=1 \
python scripts/seed_shipyard_demo_users.py
```

脚本是幂等的，可以重复执行。它会更新同名演示账号、移动功能授权和页面
权限，但不会删除其他用户。

默认口令仅用于本地演示，可通过 `SHIPYARD_DEMO_PASSWORD` 覆盖。生产环境
应使用真实账号流程，脚本会拒绝 `FLASK_ENV=production`。

## 账号

| 用户名 | 角色 |
| --- | --- |
| `demo_shipyard_admin` | `ADMIN` |
| `demo_shipyard_manager` | `SHIPYARD_MANAGER` |
| `demo_shipyard_receiver` | `RECEIVER` |
| `demo_shipyard_outbound` | `OUTBOUND` |
| `demo_shipyard_viewer` | `VIEWER` |

## 权限边界

- 管理员拥有全部权限。
- 船厂主管维护全局船号绑定和当前仓库区域定置，导入入库/出库清单并查看审计。
- 收货员需要 `RECEIVER` 角色、`inbound` 页面权限和已绑定仓库，只能调用吊牌拍照
  识别、实收入和少件补货接口。
- 出库员需要 `OUTBOUND` 角色、`shipyard-outbound` 页面权限和已绑定仓库，只能在
  小程序查看出库任务、逐托拍照识别、整票核销和装车确认，不能修改单据。
- 审计员只读取已授权仓库的页面和审计记录，所有写接口返回 `403`。
- 所有非管理员仍受 `warehouse_ids` 仓库隔离。
- 区域船号映射的更新按映射 ID 当前所属仓库校验。仓库 2 的主管即使拥有
  `shipyard-settings` 页面权限，也不能修改仓库 1 的映射。
- 船号/工程号/制作图绑定是跨仓库共享的系统级主数据，不按 `warehouse_ids`
  复制。只有同时具备 `SHIPYARD_MANAGER` 角色和 `shipyard-settings` 页面权限
  才能修改，所有变更进入审计日志；仓库隔离继续作用于区域映射和业务单据。

角色和页面权限是两道独立门禁。只有 `RECEIVER` 或 `OUTBOUND` 角色但没有对应
页面权限时，移动端接口返回 `403`，即使调用方知道有效任务 ID 也不能绕过。
