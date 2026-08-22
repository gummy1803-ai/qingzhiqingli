# 技术复盘 · SQLAlchemy `updated_at` 假字段导致 CompileError 崩溃

> 发生时间：2026-08-22（本次 Session 期间）
> 影响模块：`durability/database.py::db_set_contact_verified`
> 关联功能：飞书人员对接「📤发送测试消息」成功后把 `verified` 写回 `True`
> 事故等级：**P2 · 阻断飞书对接流程**（只要测试消息能发到飞书，回写 DB 时必崩，后续预警即使密钥正确也无法把联系人标记为已验证）

---

## 0. 一句话结论

`db_set_contact_verified()` 在 SQLAlchemy Core 的 `UPDATE` 语句 `values()` 里写了一个表中**根本不存在**的字段 `updated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S')`。
SQLAlchemy 编译 SQL 时发现「`updated_at` 列不在元数据 / 表中」，直接抛出：

```
sqlalchemy.exc.CompileError: Unconsumed column names: updated_at
```

这是一个**纯代码与真实表结构不一致**的问题，和 MySQL vs SQLite、降级、网络、连接池都没有任何关系。

---

## 1. 复现链路（最小步骤）

| 步骤 | 触发入口 | 实际做了什么 | 预期 | 实际 |
|---|---|---|---|---|
| 1 | 在 Streamlit「🚨飞书对接预警 → 👥 人员管理」点某人的「📤 发送测试消息」 | UI 调 `durability.feishu_contacts.send_test_message(cid)` | 若飞书返回成功 → 把此人 `verified` 置为 True，DB 写回成功 | 若飞书成功 → 在写回 DB 瞬间抛异常，verified 仍然 False |
| 2 | 后端内部：`send_test_message()` 写回 `verified=True` | 调用 `durability.database.db_set_contact_verified(cid, verified=True)` | SQL 只更新 `feishu_contacts.verified` 一列 | 抛出 `CompileError: Unconsumed column names: updated_at` |
| 3 | 调用点（UI 层 / 冒烟脚本） | catch 住异常后展示为「写回失败」 | — | 让用户以为「密钥有效但保存失败」 |

**最小复现代码**（不需要飞书真的发出去，只要能走 `verified=True` 写回分支即可）：

```python
from durability.database import init_db, db_set_contact_verified
init_db()
# 拿任意一个存在的 cid (从 feishu_contacts 表选一行)
ok, msg = db_set_contact_verified("fc_xxx", True)
print(ok, msg)
```

---

## 2. 关键错误现场

**异常栈（截取关键帧）**：

```
Traceback (most recent call last):
  ... 中间省略 ...
  File "durability/feishu_contacts.py", line 580, in send_test_message
    ok_v, msg_v = db_set_contact_verified(cid, True)
  File "durability/database.py", line 570, in db_set_contact_verified
    result = conn.execute(stmt)
  File "sqlalchemy/engine/base.py", line 1416, in execute
    return meth(self, multiparams, params, _EMPTY_EXECUTION_OPTS)
  ...
sqlalchemy.exc.CompileError: Unconsumed column names: updated_at
```

**错误代码（出问题前）**：

```python
# durability/database.py → db_set_contact_verified (旧)
stmt = _feishu_contacts.update().where(
    _feishu_contacts.c.id == cid
).values(
    verified=int(verified),
    # 👇 这一行就是根因：表 feishu_contacts 里根本没有 updated_at 列！
    updated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
)
```

**表结构的实际列**（从 `_feishu_contacts = Table("feishu_contacts", metadata, ...)` 元数据能看到的真实 DDL）：

```
id          PRIMARY KEY, VARCHAR(64)
name        VARCHAR(64)
open_id     VARCHAR(64)
phone       VARCHAR(32)
app_id      VARCHAR(64)
app_secret  VARCHAR(128)
verified    TINYINT        ← 只有它才是我们要更新的
enabled     TINYINT
created_at  DATETIME      ← 注意：是 created_at (创建时写一次)，没有 updated_at
extra_json  TEXT
```

---

## 3. 根因分析（5 Whys）

### 3.1 Why 1：为什么会抛 CompileError？
因为 `_feishu_contacts.update().values(updated_at=...)` 里的列名 `updated_at` 没在 `Table(...)` 定义中注册。SQLAlchemy Core 编译阶段会严格检查 `values()` 里的 key 是否都是 `table.c.*` 已知的列，发现未知 key 就抛 `Unconsumed column names`——这是 ORM 特意做的**编译期保护**，避免往数据库发一个 MySQL 本地也会报错的 `UPDATE ... SET updated_at=...`（SQL 层会报 `Unknown column 'updated_at' in 'field list'`）。

### 3.2 Why 2：为什么写代码时会顺手加上 `updated_at`？
直觉思维：「既然有 `created_at`，那肯定也该有 `updated_at`」，写 `db_set_contact_verified` 时顺手加了一个「自动更新时间」的行为——这个意图本身是**好的**（运维时能看到 verified 最后改的时间），但**跳过了最关键的一步：先确认表结构里是否真的存在该列**。

### 3.3 Why 3：为什么开发时没发现？
- 这个写回分支只在 `send_test_message()` 走**成功路径**时才会触发。之前多次冒烟脚本要么：① 密钥就是错的（永远走失败分支 `verified` 保持旧值）；② 测试时只验证了「发送失败」路径的异常处理。
- 项目里没有「每次改 ORM SQL 就做一次 DB 元数据比对」的守护脚本。

### 3.4 Why 4：为什么 IDE / linter 没拦住？
SQLAlchemy Core 是**纯运行时字符串/Table 对象反射**，静态 lint 无法检查 `values(key=...)` 里的 key 是否属于某张表。只有当代码真的走到 `.compile()` / `conn.execute(stmt)` 时才会炸。

### 3.5 Why 5：为什么不是 MySQL / SQLite 的锅？
两张后端用的是**同一份 Table 元数据**（`CREATE TABLE IF NOT EXISTS feishu_contacts (...)` 在 `init_db()` 两处都会执行，都不会创建 `updated_at`），所以切换到 SQLite 也会报同样的错——本次 Session 中我们在降级脚本里确实观察到了这个现象，直接排除了存储后端差异的嫌疑。

---

## 4. 修复方案

### 4.1 第一阶段（立即止血 · 已落地）

**修改文件**：`durability/database.py: db_set_contact_verified()`

**修复前**：
```python
stmt = _feishu_contacts.update().where(
    _feishu_contacts.c.id == cid
).values(
    verified=int(verified),
    updated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),  # 假字段！
)
```

**修复后**：
```python
stmt = _feishu_contacts.update().where(
    _feishu_contacts.c.id == cid
).values(
    verified=int(verified),
    # 没有 updated_at 列就不要写，任何「想改表结构」的需求先改 DDL + 跑迁移
)
```

同时补上了 **结构化日志**（便于以后一眼知道「写回到底触没触、改了几行、是 MySQL 还是 SQLite」）：

```python
logger.info(
    "[DB: 设置联系人 verified] ✅ 写回成功 | cid=%s 新值=%s 影响行数=%d 耗时=%dms backend=%s",
    cid, verified, result.rowcount, (time.perf_counter() - t0) * 1000, get_db_backend_info().get("backend"),
)
```

### 4.2 第二阶段（防护措施 · 本次也一并落地）

为避免同类问题再发生，我们给所有 UPDATE/INSERT 相关函数加了「模式」：

1. **函数级统一日志前缀**：`[DB: 取联系人详情]` / `[DB: 设置联系人 verified]` / `[DB 调用]` 三级标签，方便后续直接 grep 标签而不是读整段堆栈。
2. **所有写操作都返回** `(bool, str)`：成功时 msg 是 `已更新验证状态 (影响 X 行)`，失败时 msg 包含异常类名和第一行 message，不会让调用方吞掉异常还以为成功。
3. **失败自动捕获 + 降级**：在 DB 层所有 conn.execute 外层套 try/except，抛错后 `_try_atomic_fallback()` 自动降级 SQLite 再重试一次。即使以后类似「假字段」问题真的上了生产，也能先在 MySQL 失败、降级 SQLite 再把错误完整打到日志里，不会静默丢失。
4. **冒烟脚本 `_persist_test_msg_logs.py`**：持久化「密钥请求→消息发送→DB 写回→读回校验」的全链路日志到 `logs/smoke_test_message.log`，任何人提交前跑一次，失败立即能看到 [DB: 设置联系人 verified] 有没有 40ms 内成功。

### 4.3 第三阶段（可选增强，未做，留给后续迭代）

如果未来希望真的有「updated_at 最后修改时间」字段，需要走**正经的迁移**而不是随手在 values() 里加一列：

```sql
-- 1) 改 DDL: 给 feishu_contacts 加列
ALTER TABLE feishu_contacts ADD COLUMN updated_at DATETIME NULL;
-- SQLite 里也等价：
ALTER TABLE feishu_contacts ADD COLUMN updated_at TEXT;

-- 2) 改 python 侧 Table 元数据（_feishu_contacts = Table(...)）：
Column("updated_at", DateTime, nullable=True)

-- 3) 再在 UPDATE 里写：
.values(verified=int(verified), updated_at=datetime.now())
```

建议这类变更放到**一次性迁移脚本**里做（例如 `durability/_migrate_002_add_updated_at.py`），执行后 `.migrated` 打标，下次启动不重复跑。不要让应用层每次启动都 `ALTER TABLE`。

---

## 5. 回归验证（本次修复后已通过）

**验证命令**：`python _persist_test_msg_logs.py`

**观察点**（`logs/smoke_test_message.log` 里可 grep）：

| 标签 | 预期结果 | 实际 |
|---|---|---|
| `[DB: 设置联系人 verified] ✅ 写回成功` | `影响行数=1`，耗时 < 500ms | ✅ `影响行数=1 耗时=105ms backend=MySQL` |
| `[DB: 取联系人详情] ✅ 命中` | `verified=True` | ✅ `verified=True enabled=True 耗时=79ms` |
| `[测试消息] ✅ 全流程成功`（若密钥真有效） | `DB写回耗时=...ms 总耗时=...ms` | —（当前用假密钥只测到失败分支写回保持 False，C 段手工调用 `db_set_contact_verified(True)` 验证了成功分支） |
| `CompileError: Unconsumed column names: updated_at` | **不再出现** | ✅ 已 0 出现 |

---

## 6. 预防措施（Checklist · 团队统一遵循）

改任何写 DB 的 SQLAlchemy 语句前，**强制在心里过一遍 4 条**：

- [ ] 先 grep `_xxx_table = Table("xxx"` 或数据库的 DDL 建表语句，**确认你要 SET 的列名在表结构里真实存在**（拼写、大小写、是否是 created_at vs updated_at）。
- [ ] INSERT/UPDATE 的 `values()` 里，每一个 key 都能在 `_xxx_table.c.*` 里找到（或者至少你能「指着 `Table(Column(...))` 那一行说 yes」）。
- [ ] 新增字段必须走**迁移脚本 + 同步改元数据**；不能只在业务代码里偷偷加一列，期望数据库自己变出来。
- [ ] 提交前跑一次最小冒烟：`_persist_test_msg_logs.py` / 对应模块的 e2e 脚本，确认日志里有你预期的 `[DB: xxx] ✅ ...` 成功标签，而不是 CompileError / IntegrityError。

推荐的自动化守护：

```bash
# 方式 A：在 durability/database.py 新增自检函数，init_db 后立即对所有写函数跑一次「空更新」
#        即使 id 不存在也至少能保证 SQL 能编译通过，不会卡在 CompileError。
# 方式 B：CI 中加一步 `pytest tests/test_db_compile.py`，
#        把所有 UPDATE/DELETE/INSERT 语句先 stmt.compile() 一遍再 execute。
```

---

## 7. 关联代码位置

| 代码点 | 文件 | 行号区间 | 说明 |
|---|---|---|---|
| 修复点（本次事故） | `durability/database.py` | `db_set_contact_verified()` 函数内 | 移除 values() 中的 `updated_at=...` |
| 表结构定义（真实列清单） | `durability/database.py` | `_feishu_contacts = Table("feishu_contacts", metadata, ...)` | 定义了 id/name/open_id/phone/app_id/app_secret/verified/enabled/created_at/extra_json → **没有 updated_at** |
| 调用方（触发写回） | `durability/feishu_contacts.py` | `send_test_message()` 末尾 | 成功时调 `db_set_contact_verified(cid, True)` |
| 飞书密钥验证日志 | `durability/feishu_contacts.py` | `verify_credentials()` 内 | 标签 `[飞书密钥]`，打印 `HTTP=xxx code=10003 耗时=600ms` |
| 三处新增🔑密钥过期预检 | `run_e2e.py` / `app.py` / `components/feishu_contacts.py` | Step 0.6 / 启动横幅 / 数据源卡片第三列 | `detect_all_credentials_status()` 返回每个联系人 code=10003 时显示为「❌ 已过期/错误密钥 (code=10003)」 |
| 冒烟日志持久化脚本 | `_persist_test_msg_logs.py` | — | 输出 `logs/smoke_test_message.log`，本复盘第 5 节回归验证直接使用 |
| 本次用户要查看的「发送测试消息完整日志」 | `logs/smoke_test_message.log` | L16–L52 | 完整证据链：[测试消息] 开始 → [DB: 取联系人详情] → [飞书密钥] code=10003 → [飞书发消息] 中止 → [测试消息] 失败保持 verified 不变 → [DB: 设置联系人 verified] ✅ |
