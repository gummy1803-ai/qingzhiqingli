# 新增车型 & 飞书对接人 操作指南

> 最后更新: 2026-08-22
> 对应代码版本: 完成了「新增车型自动识别」「飞书联系人统一入库 (feishu_contacts 表 + 自动降级 SQLite)」「发送测试消息 → 自动 verified=True」三段改造后整理。

---

## 一、项目架构 & 核心能力速览

```
项目根目录
├─ run_e2e.py                        # 控制台主程序: CSV→指标→预警→DB→HTML 报告
├─ app.py                            # Streamlit Web 界面 (所有看板 + 飞书对接)
├─ scan_hyd_zero.py                  # 控制台: 氢耗字段 0值扫描
├─ extract_345_raw.py                # 控制台: 导出单辆车原始 CSV 给采集方排查
├─ test_alert_log.py                 # Streamlit 小页面: 预警历史面板效果预览
├─ durability/
│   ├─ database.py                   # 🗄️ 核心: MySQL/SQLite 双模式 + 自动降级 + 联系人/预警/推送 CRUD
│   ├─ feishu_contacts.py            # 📡 飞书联系人管理 + 发送测试消息 + verified 自动打标 + 预警推送
│   ├─ feishu_alerter.py             # 飞书告警汇总入口
│   └─ durability_alert_log.py       # 预警历史面板 UI
├─ components/feishu_contacts.py     # Streamlit 飞书对接 UI (📝新增/👥管理/🚨测试)
├─ src/                              # 指标 / 数据加载 / 报告生成
├─ 企业资料包02_氢质氢离/
│   └─ 02_整车数据处理/               # 新车目录放这里
│       ├─ 212/                       # 车 212
│       ├─ 345/                       # 车 345
│       └─ 666/                       # 新增车型号 / 车号, 目录名=车号
├─ reports/                          # 所有输出 (HTML 报告 / CSV / 摘要)
├─ logs/                             # 业务日志 (含 [DB 降级] 横幅 / 联系人操作日志)
└─ data/
    ├─ app.db                        # 本地 SQLite (当 MySQL 不可达 / .env 未配置时自动启用, 降级模式)
    └─ feishu_contacts.json          # 旧版 JSON, 首次启动会自动迁到 DB, 迁完改名为 .migrated 备份
```

### 🧩 数据库降级机制 (全局通用)

```
启动 run_e2e / app.py / scan_hyd_zero / extract_345_raw
    │
    ▼
立即 durability.database.init_db()  ──► 打印 Step 0 横幅: "MySQL (腾讯云) gz-cdb-6t38h4z0...:25578 hydrogen_analytics admin"
    │
    ├─ 腾讯云可达, 密码正确 ─► 所有后续 DB 操作 (联系人/预警/推送) 走 MySQL ✅
    │                       └─ 中途外网断开 → 原子降级到 SQLite, 终端 & 日志打印醒目 [DB 降级] 横幅
    │
    └─ .env 没配 / 密码错 / 超时 → 启动期即降级 SQLite, 所有联系人存到 data/app.db ✅
                               └─ 入口结尾再次打印 "DB 运行时状态" 横幅, 一眼可见是否发生降级
```

---

## 二、新增车型 (零代码, 只需放目录)

### 2.1 操作步骤

在下面目录下新建一个子目录，**名字 = 车型号 / 车号**（数字、VIN 后几位都行，字符串直接作为车号在所有入口展示）：

```
企业资料包02_氢质氢离 / 02_整车数据处理 /
  ├── 212/                         ← 已有
  ├── 345/                         ← 已有
  └── 555/                         ← 你新增的车, 名字=555, 里面放所有 *.csv 分片
```

就这么简单。**不需要改代码、不需要改配置**，所有入口都会自动识别。

### 2.2 验证方式

| 你想做什么 | 入口 / 命令 | 新车如何体现 |
|---|---|---|
| 氢耗扫描 & 风险分类 | `python scan_hyd_zero.py` | 扫描到目录数从 2 → 3；输出表格里多一行 555；`reports/quality_scan_<timestamp>.csv` 新增对应行 |
| 生成 HTML 指标报告 | `python run_e2e.py` | 报告生成循环为每辆车各出一份 `reports/<车号>_<YYYYMMDD_HHMM>_report.html`，首车仍额外写一份 `reports/测试报告_<首车>.html`（老路径兼容） |
| 看板可视化 | `streamlit run app.py` | 所有选车下拉框 / 多车对比多选框自动出现 555 选项；整车看板选 555 立刻显示指标 / 图表 |
| 导出原始 CSV 送采集方排查 | `python extract_345_raw.py 555` | 输出 `reports/555_raw_export.csv` + `reports/555_raw_export_summary.txt` |

### 2.3 实跑验证结果 (本项目已实跑)

> 测试方式：临时把 `212/` 整份复制为 `666/`（89 个 CSV / 114 万行），全流程跑一遍。

```
scan_hyd_zero
  └─ ✅ 扫描到 ['212', '345', '666'] → 666 指标与 212 一致 (氢耗 6.92kg, 运行时长 748h, 114 万行 / 48 列)

run_e2e Step 1 加载
  └─ ✅ load_vehicle_csvs(666) = 1,142,636 行 / 48 列 / 耗时 ~4.4s
     vehicle_overview 出全部关键指标

app.py 整车看板
  └─ ✅ 整车看板 selectbox / 多车绝缘对比 multiselect 自动出现 666
```

### 2.4 常见注意事项

1. **CSV 必须带 `Timestamp` + `FC_HydCmPerHundred` 列**
   缺了这两列，氢耗扫描和报告都会显示 0 或 NaN。如果是新厂牌/新字段结构，请先联系我补适配层。
2. **`run_e2e.py` 主报告默认写第一辆车**（兼容历史路径），其它车都有独立带时间戳的报告，不会互相覆盖。
3. **`extract_345_raw.py` 只接受一个车号参数**，不传就默认抽 345。目录不存在时会自动打印 `02_整车数据处理` 下现有所有车号，方便直接复制。

---

## 三、新增飞书对接人 + 发送测试消息 (一键自动激活 verified)

### 3.1 操作界面位置

```
streamlit run app.py  →  左侧切到 「📡 飞书固定人员对接系统」 Tab
   └─ 顶部会看到一张信息卡片: 当前联系人存储后端 (MySQL/SQLite + 旧 JSON 迁移日志)
```

整个对接分成三个子 Tab。

### 3.2 📝 新增对接子 Tab (推荐一次性流程)

1. 填完 6 项必填：
   | 字段 | 说明 |
   |---|---|
   | 姓名 | 任意字符串 |
   | 飞书 Open ID | 必须是真实用户 open_id（一般 `ou_` 开头），**写错测试消息就永远收不到** |
   | 手机号 | 可选，用于表格展示 |
   | App ID / App Secret | 从飞书管理后台拿到；**同一个 App ID 只能绑定给一个联系人**（Python 层会查重，防止重复） |

2. **先点「🔍 验证密钥」**：此时系统调 `tenant_access_token` 接口，只验证 App ID/Secret 是否正确，不保存任何东西。

3. **点「💾 保存对接」**：成功后界面会出现绿色「✅ 对接成功」提示，联系人会保存到 `feishu_contacts` 表（MySQL 或降级 SQLite），`verified=False`（此时还**不会**进入预警推送列表）。

4. **立即点「📤 给 [XXX] 发送测试消息 (送达后自动 verified=True)」** ⭐ 新增的按钮 ⭐
   - 系统会用这个联系人的 App ID / Secret 走一遍**真实飞书 Open API**（tenant_access_token → `im/v1/messages` 发送 text 消息）
   - 如果对方飞书**成功收到消息**：
     - 系统显示 `✅ 测试消息已送达 XXX, 已自动标记为已验证 (verified=True)`
     - 同时自动更新 `last_alert` 时间
     - 该联系人立刻进入 `db_get_verified_contacts()` 推送列表
   - 如果发送失败：显示 `❌ 飞书接口返回错误: XXX`，下面附常见 3 条排查指引；**verified 仍保留 False，防止把错误配置的联系人加入真正的推送列表**。

### 3.3 👥 人员管理子 Tab

- 顶部三个 metric 卡片：总对接人数 / 已验证 / 已启用
- 表格列：姓名 / 手机 / Open ID / App ID / App Secret（打码成前 4 位+****，不会外泄）/ 验证状态 / 启用状态 / 对接时间 / 最后预警
- 每个联系人下方有独立操作区：
  - `⏸️ 禁用 / ▶️ 启用`：暂时不要推给这个人（不删除，保留配置）
  - `🗑️ 删除`：永久删除
  - **`📤 发送测试消息并激活` / `📤 重测验证`**（新增，按当前是否已验证切换文案）：对老联系人也能补发测试，成功自动改 verified=True
- 发送成功后界面会自动刷新，表格里"验证状态"会从 ❌ 未验证 → ✅ 已验证。

### 3.4 🚨 预警测试子 Tab

- 先填预警参数（循环编号/功率点/条件等），再点「🚀 发送测试预警」
- 系统会调用 `send_alert_to_contacts(event=None → 默认从 db_get_verified_contacts() 取所有 verified=True 且 enabled=True 的联系人)` 做群发
- 结果逐个列出 ✅/❌，并写入 `alert_push_log` 表

### 3.5 旧版 JSON 联系人兼容 (自动迁移)

如果你之前在 `data/feishu_contacts.json` 里已经存过联系人：

```
只要任意入口启动过一次 (import 了 durability.feishu_contacts)
    → 自动把 JSON 按 open_id / app_id 去重迁到 feishu_contacts 表
    → 原 JSON 文件改名为 data/feishu_contacts.json.migrated_<时间戳> (备份, 不会重复导入)
    → 飞书对接 Tab 顶部卡片会显示迁移条数
```

### 3.6 实跑验证结果 (本项目已实跑)

| 场景 | 结果 |
|---|---|
| 腾讯云 MySQL 正常: UI 通路新增联系人 | ✅ 写入 `fc_xxx` → list_contacts 回读 name/open_id/phone 正确，app_secret 列表打码，`app_secret` 字段不出现在 UI 返回中 |
| 降级 SQLite (临时移走 `.env` 启动期降级): UI 通路新增联系人 | ✅ 启动期醒目 `[DB 降级]` 横幅 → 新增→回读→删除全部成功 |
| `send_test_message` 状态机 | ✅ 空 cid / 不存在 cid / 禁用状态 → 正确错误消息；**发送失败 verified 保持 False，不污染推送列表** |
| `send_test_message` 手工 verified=True → `get_verified_contacts()` | ✅ API 会返回带原 app_secret，适合 `send_alert_to_contacts` 直接推送 |

### 3.7 常见排查指引

- **App ID 正确但测试消息永远收不到**
  1. Open ID 是不是自己拼错了？（必须从飞书后台「通讯录→员工详情→open_id」复制，不要瞎写）
  2. 应用有没有「发消息给个人」的 API 权限？飞书开放平台的权限开关需要管理员手动授权
  3. 接收人是不是在该应用的可见范围内？一般要先把该员工加入应用的"可见范围"
- **新增联系人时提示 `App ID 已被 XX 使用`**
  一个 App ID 只能绑一个联系人（防止你把同一个应用给多人发消息，API 配额会翻倍消耗）。要给多人发，请在飞书后台创建多个应用或先找我改造为「一个 App ID + 多 open_id 列表」的结构。
- **列表里 verified 是 ❌，但对方明明能收到消息**
  直接在人员管理里，点这个联系人的「📤 发送测试消息并激活」，一次成功就自动变 ✅。

---

## 四、所有入口文件清单 (功能一目了然)

| 文件 | 类型 | 启动命令 | 新车支持？ | 飞书对接 UI/DB？ | DB 降级横幅？ |
|---|---|---|---|---|---|
| `run_e2e.py` | 控制台主程序 | `python run_e2e.py` | ✅ 循环出每辆车报告 | ✅ DB 层 (API 可用) | ✅ Step 0 + 结尾各一次 |
| `app.py` | Streamlit 主界面 | `streamlit run app.py` | ✅ 所有选车下拉框自动 | ✅ 完整 UI (新增/管理/测试 + 后端卡片) | ✅ Streamlit 启动期终端 + sidebar 卡片 |
| `scan_hyd_zero.py` | 氢耗扫描 | `python scan_hyd_zero.py` | ✅ 自动扫所有目录 | ✅ DB 层（若要推送触发） | ✅ Step 0 + 结尾各一次 |
| `extract_345_raw.py` | 单车原始 CSV 导出 | `python extract_345_raw.py <车号>` | ✅ `sys.argv[1]` 传 | ✅ DB 层 | ✅ Step 0 + 结尾各一次 |
| `test_alert_log.py` | 预警面板预览 | `streamlit run test_alert_log.py` | — | ✅ 侧边栏 DB 卡片 | ✅ 启动期终端 + sidebar |

---

## 五、代码层扩展点 (如果要二次开发)

需要写代码批量导入 / 批量发消息时，直接调用下面两个模块的公开 API：

```python
# ---------- 1. 数据库层 (MySQL 或 SQLite 自动降级) ----------
from durability.database import (
    db_add_contact,           # 新增联系人 (ok, id_or_msg)
    db_list_contacts,         # 列表 (app_secret 打码, 安全)
    db_get_verified_contacts, # 已验证列表 (带原 app_secret, 用于推送)
    db_remove_contact,        # 删除
    db_toggle_contact,        # 启用/禁用
    db_get_contact_raw,       # 按 ID 取单个(含原 secret, 内部用)
    db_set_contact_verified,  # 设置 verified=True/False
)

# ---------- 2. 飞书业务层 (封装发消息 + 推送状态机) ----------
from durability.feishu_contacts import (
    add_contact,              # 等同于 UI 的 💾保存
    send_test_message,        # 等同于 UI 的 📤发送测试消息 (成功→自动 verified=True)
    send_alert_to_contacts,   # 群发预警 (参数缺省就自动取 db_get_verified_contacts)
    list_contacts,
    remove_contact,
    toggle_contact,
)
```

两个层都已经接入 `_run_with_fallback`，**外网断开时无需改任何代码，自动切换 SQLite，所有返回值契约保持不变**。

---

## 六、日志 & 问题追踪位置

1. **数据库降级横幅 / 联系人操作日志**：
   - 控制台入口的终端直接打印（Step 0 / 结尾各一张 62 字符横幅）
   - `logs/e2e_run.log` / `logs/_smoke_*.log` / Streamlit 终端里，降级、新增、删除、测试消息发送等所有动作都有结构化日志

2. **数据落库真实值核对**：
   - 腾讯云 MySQL：`gz-cdb-6t38h4z0.sql.tencentcdb.com:25578 / hydrogen_analytics`
     三张表：`feishu_contacts`（联系人）/ `alert_events`（预警事件）/ `alert_push_log`（推送日志）
   - 本地 SQLite（降级）：`data/app.db`，表结构完全一致

3. **报告产物**：`reports/` 目录（带时间戳不会互相覆盖）
   - `<车号>_<YYYYMMDD_HHMM>_report.html`：HTML 指标报告（Ctrl+P 直接打 PDF）
   - `quality_scan_<timestamp>.csv`：氢耗扫描汇总
   - `<车号>_raw_export.csv` + `_summary.txt`：单车原始数据，送采集方排查
