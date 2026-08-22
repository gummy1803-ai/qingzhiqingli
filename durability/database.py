"""MySQL 数据库模块（腾讯云 MySQL 兼容 + 运行时自动降级 SQLite）。

三张表:
1. feishu_contacts  - 飞书对接人员(凭证/验证状态/启用状态)
2. alert_events     - 预警事件(循环/功率/条件/数值/状态)
3. alert_push_log   - 预警推送记录(事件ID/联系人ID/推送结果)

降级机制:
- 启动阶段: .env 缺失 或 init_db 连不上 MySQL → 立即降级 SQLite, 打印醒目日志
- 运行阶段: 任何 CRUD 操作触发 MySQL 连接类异常(OperationalError/InterfaceError)
            → 原子切换到 SQLite 引擎, 打印「降级横幅」日志, 并以 SQLite 重试一次操作
            → 降级只执行一次, 后续全部走 SQLite(不会每次都抛异常重试)
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any, Callable

from dotenv import load_dotenv
from sqlalchemy import (
    create_engine, MetaData, Table, Column, String, Integer, Float,
    Text, Index, text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError, InterfaceError, DBAPIError

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = _PROJECT_ROOT / ".env"

_FALLBACK_BANNER = "═" * 55


# ---------- 连接配置加载 ----------

def _load_db_config() -> Dict[str, str]:
    """从 .env 加载 MySQL 配置, 缺失字段返回空 dict。"""
    if _ENV_PATH.exists():
        load_dotenv(_ENV_PATH, override=True)
    keys = ["DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME"]
    cfg = {k: os.getenv(k, "").strip() for k in keys}
    if all(cfg.values()):
        return cfg
    logger.warning(
        "\n%s\n[DB 降级] .env 未配置完整 MySQL 参数, 启动即使用本地 SQLite\n"
        "[DB 降级] 如需启用腾讯云 MySQL, 请检查 .env 中的 5 项连接参数\n%s",
        _FALLBACK_BANNER, _FALLBACK_BANNER,
    )
    return {}


_USE_MYSQL: bool = False
_DB_CFG: Dict[str, str] = _load_db_config()
if _DB_CFG:
    _USE_MYSQL = True


def _backend_name() -> str:
    """统一返回当前后端短名 (MySQL / SQLite), 用于日志分类标记。

    ⚠️ 运行期降级切换 MySQL→SQLite 后: 这里取的是当前状态。
    """
    return "MySQL" if _USE_MYSQL else "SQLite"

# 降级锁: 保证多线程环境下只会有一个线程执行降级动作
_state_lock = threading.RLock()
_fallback_triggered = False

# ---------- SQLAlchemy Engine ----------

_SQLITE_PATH = _PROJECT_ROOT / "data" / "app.db"
_metadata = MetaData()


def _build_mysql_engine(cfg: Dict[str, str], with_db: bool = True) -> Engine:
    """构建 MySQL Engine。with_db=False 时连接到 server 级(用于 CREATE DATABASE)。"""
    user = cfg["DB_USER"]
    pwd = cfg["DB_PASSWORD"]
    host = cfg["DB_HOST"]
    port = cfg["DB_PORT"]
    db = cfg["DB_NAME"] if with_db else ""
    url = f"mysql+pymysql://{user}:{pwd}@{host}:{port}/{db}?charset=utf8mb4"
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=3600,
        future=True,
        echo=False,
        connect_args={"connect_timeout": 10},
    )


def _build_sqlite_engine() -> Engine:
    _SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{_SQLITE_PATH}", future=True, echo=False)
    # 确保 SQLite 下所有表都存在
    _metadata.create_all(engine, checkfirst=True)
    return engine


if _USE_MYSQL:
    _engine = _build_mysql_engine(_DB_CFG, with_db=True)
else:
    _engine = _build_sqlite_engine()


# ---------- 降级核心 ----------

def _is_connection_exception(exc: BaseException) -> bool:
    """判断异常是否属于 MySQL 连接类错误(应触发降级)。"""
    if isinstance(exc, (OperationalError, InterfaceError)):
        return True
    if isinstance(exc, DBAPIError):
        orig = getattr(exc, "orig", None)
        return isinstance(orig, (OperationalError, InterfaceError,
                                 ConnectionError, TimeoutError, OSError))
    # pymysql 原始异常(在 engine 构建期直接抛出的情况)
    for base in (ConnectionError, TimeoutError, OSError):
        if isinstance(exc, base):
            return True
    return False


def _extract_error_summary(exc: BaseException) -> str:
    """从异常中提取一行可读的错误原因(不含堆栈)。"""
    msg = str(exc).strip().splitlines()
    first_line = msg[0] if msg else repr(exc)
    # 去 sqlalchemy 外层包装的冗余前缀, 保留真实原因
    if ")" in first_line and "(" in first_line:
        inner = first_line[first_line.rfind("("):]
        if len(inner) < len(first_line):
            first_line = inner
    if len(first_line) > 240:
        first_line = first_line[:237] + "..."
    return first_line


def _trigger_fallback(stage: str, exc: BaseException) -> None:
    """执行降级动作: 切引擎到 SQLite, 打印醒目日志。线程安全, 只执行一次。"""
    global _engine, _USE_MYSQL, _fallback_triggered
    with _state_lock:
        if not _USE_MYSQL:
            return  # 已经在 SQLite 模式, 无需降级
        summary = _extract_error_summary(exc)
        logger.warning(
            "\n%s\n[DB 降级] ⚠️  MySQL 不可用(%s阶段), 已自动切换到本地 SQLite\n"
            "[DB 降级] 原因: %s\n"
            "[DB 降级] SQLite 路径: %s\n"
            "[DB 降级] 后续所有 DB 操作都走本地文件, 外网恢复后请重启应用\n%s",
            _FALLBACK_BANNER, stage, summary, _SQLITE_PATH, _FALLBACK_BANNER,
        )
        # 完整 ERROR 堆栈用于事后排查(只打一行, 不刷屏)
        logger.error(
            "MySQL %s 阶段连接失败, 启动降级到 SQLite; 详细堆栈见下",
            stage, exc_info=exc,
        )
        sqlite_engine = _build_sqlite_engine()
        try:
            _engine.dispose()
        except Exception:
            pass
        _engine = sqlite_engine
        _USE_MYSQL = False
        _fallback_triggered = True


def _run_with_fallback(op_name: str, func: Callable):
    """
    带降级保护的执行器:
    1. 用当前引擎执行 func() (func 里必须自己打开新的 connection, 不可复用旧连接)
    2. 若抛出连接类异常 → 原子降级到 SQLite, 并以新引擎重试一次 func
    3. 若重试仍失败 → 原样抛给调用方(非连接类错误也直接抛出)
    """
    t_op = time.perf_counter()
    backend_start = "MySQL" if _USE_MYSQL else "SQLite"
    logger.info("[DB 调用] → %s 开始 | 后端=%s", op_name, backend_start)
    try:
        result = func()
        cost_ms = (time.perf_counter() - t_op) * 1000
        logger.info("[DB 调用] ✅ %s 成功 | 后端=%s | 耗时=%.0fms",
                    op_name, backend_start, cost_ms)
        return result
    except Exception as e:
        if not _USE_MYSQL or not _is_connection_exception(e):
            cost_ms = (time.perf_counter() - t_op) * 1000
            logger.error("[DB 调用] ❌ %s 失败(非连接类错误) | 后端=%s | 耗时=%.0fms | err=%s",
                         op_name, backend_start, cost_ms, e, exc_info=True)
            raise
        # 首次遇到 MySQL 连接异常: 降级
        logger.warning("[DB 调用] ⚠️  %s 触发 MySQL 连接异常, 开始降级并重试 | 原因=%s | 已耗时=%.0fms",
                       op_name, _extract_error_summary(e),
                       (time.perf_counter() - t_op) * 1000)
        _trigger_fallback(f"CRUD({op_name})", e)
        # 降级后重试一次
        t_retry = time.perf_counter()
        try:
            result = func()
            retry_ms = (time.perf_counter() - t_retry) * 1000
            total_ms = (time.perf_counter() - t_op) * 1000
            logger.info(
                "[DB 调用] ✅ %s 降级重试成功 | 后端=MySQL→SQLite | "
                "MySQL耗时=%.0fms 降级切换耗时已计入 | 重试耗时=%.0fms | 总耗时=%.0fms",
                op_name, (t_retry - t_op) * 1000, retry_ms, total_ms,
            )
            return result
        except Exception as e2:
            total_ms = (time.perf_counter() - t_op) * 1000
            logger.error(
                "[DB 调用] ❌ %s 降级重试也失败 | 后端=MySQL→SQLite | 总耗时=%.0fms | err=%s",
                op_name, total_ms, e2, exc_info=True,
            )
            raise


# ---------- 表定义（同时兼容 SQLite/MySQL） ----------

_feishu_contacts = Table(
    "feishu_contacts", _metadata,
    Column("id", String(64), primary_key=True),
    Column("name", String(128), nullable=False),
    Column("open_id", String(128), nullable=False),
    Column("phone", String(32), default=""),
    Column("app_id", String(128), nullable=False),
    Column("app_secret", String(256), nullable=False),
    Column("verified", Integer, default=0),
    Column("enabled", Integer, default=1),
    Column("created_at", String(32), nullable=False),
    Column("last_alert", String(32), default=""),
    mysql_engine="InnoDB",
    mysql_charset="utf8mb4",
    mysql_collate="utf8mb4_unicode_ci",
)

_alert_events = Table(
    "alert_events", _metadata,
    Column("id", String(256), primary_key=True),
    Column("timestamp", String(32), nullable=False),
    Column("cycle_id", Integer),
    Column("power_point", Float(precision=53)),
    Column("condition", String(256)),
    Column("label", String(128)),
    Column("value", Float(precision=53)),
    Column("threshold", Float(precision=53)),
    Column("operator", String(8)),
    Column("signal", String(128)),
    Column("unit", String(32)),
    Column("data_count", Integer),
    Column("quality", String(32)),
    Column("message", Text),
    Column("status", String(32), default="pending"),
    Column("created_at", String(32), nullable=False),
    Index("idx_events_status", "status"),
    Index("idx_events_cycle_power", "cycle_id", "power_point"),
    mysql_engine="InnoDB",
    mysql_charset="utf8mb4",
    mysql_collate="utf8mb4_unicode_ci",
)

_alert_push_log = Table(
    "alert_push_log", _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("event_id", String(256), nullable=False, index=True),
    Column("contact_id", String(64), nullable=False, index=True),
    Column("contact_name", String(128)),
    Column("success", Integer, default=0),
    Column("message", Text, default=""),
    Column("push_time", String(32), nullable=False),
    mysql_engine="InnoDB",
    mysql_charset="utf8mb4",
    mysql_collate="utf8mb4_unicode_ci",
)


# ---------- 初始化 ----------

def _ensure_mysql_database() -> None:
    """若 MySQL 中 DB_NAME 不存在, 自动创建数据库(utf8mb4)。"""
    if not _USE_MYSQL:
        return
    db_name = _DB_CFG["DB_NAME"]
    server_engine = _build_mysql_engine(_DB_CFG, with_db=False)
    try:
        with server_engine.connect() as conn:
            existing = conn.execute(
                text(f"SHOW DATABASES LIKE '{db_name}'")
            ).fetchone()
            if not existing:
                conn.execute(text(
                    f"CREATE DATABASE `{db_name}` "
                    f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                ))
                conn.commit()
                logger.info("MySQL: 自动创建数据库 %s", db_name)
    finally:
        server_engine.dispose()


def init_db() -> None:
    """初始化数据库, 创建所有表。MySQL 失败会自动降级到 SQLite。"""
    global _engine, _USE_MYSQL
    t0 = time.perf_counter()
    logger.info(
        "[DB 初始化] 开始 init_db | 预期后端: %s",
        "MySQL(.env已加载)" if _USE_MYSQL else "SQLite(.env未配置)",
    )
    if _USE_MYSQL:
        try:
            logger.debug(
                "[DB 初始化] Step1 连接 MySQL server(无DB) 并检查是否需要 CREATE DATABASE: "
                "host=%s port=%s user=%s db=%s",
                _DB_CFG["DB_HOST"], _DB_CFG["DB_PORT"],
                _DB_CFG["DB_USER"], _DB_CFG["DB_NAME"],
            )
            _ensure_mysql_database()
            logger.info("[DB 初始化] Step1 完成 | 数据库 %s 已存在或已创建", _DB_CFG["DB_NAME"])
        except Exception as e:
            summary = _extract_error_summary(e)
            logger.warning("[DB 初始化] Step1 MySQL 建库失败, 将降级 SQLite | 原因: %s", summary)
            _trigger_fallback("启动初始化", e)

    try:
        backend_before = "MySQL" if _USE_MYSQL else "SQLite"
        logger.info("[DB 初始化] Step2 create_all 检查表结构 | 后端=%s", backend_before)
        _metadata.create_all(_engine, checkfirst=True)
        cost_ms = (time.perf_counter() - t0) * 1000
        if _USE_MYSQL:
            logger.info(
                "\n%s\n[DB 后端] ✅ 腾讯云 MySQL 初始化成功 (耗时 %.0fms)\n"
                "[DB 后端] Host:   %s:%s\n"
                "[DB 后端] DB:     %s\n"
                "[DB 后端] User:   %s\n"
                "[DB 后端] 表:     feishu_contacts / alert_events / alert_push_log\n"
                "[DB 后端] 若外网中断, 将自动降级到本地 SQLite\n%s",
                _FALLBACK_BANNER, cost_ms,
                _DB_CFG["DB_HOST"], _DB_CFG["DB_PORT"],
                _DB_CFG["DB_NAME"], _DB_CFG["DB_USER"],
                _FALLBACK_BANNER,
            )
        else:
            logger.info(
                "\n%s\n[DB 后端] 🗃️  当前使用本地 SQLite (耗时 %.0fms)\n"
                "[DB 后端] 路径: %s\n"
                "[DB 后端] 表:   feishu_contacts / alert_events / alert_push_log\n%s",
                _FALLBACK_BANNER, cost_ms, _SQLITE_PATH, _FALLBACK_BANNER,
            )
    except Exception as e:
        if _USE_MYSQL and _is_connection_exception(e):
            logger.warning("[DB 初始化] Step2 create_all MySQL 连接失败, 降级 SQLite | %s",
                           _extract_error_summary(e))
            _trigger_fallback("启动建表", e)
            _metadata.create_all(_engine, checkfirst=True)
            cost_ms = (time.perf_counter() - t0) * 1000
            logger.info("[DB 初始化] SQLite 初始化完成(启动建表失败降级) | 耗时 %.0fms 路径=%s",
                        cost_ms, _SQLITE_PATH)
        else:
            logger.error("[DB 初始化] init_db 失败 | 总耗时 %.0fms  err=%s",
                         (time.perf_counter() - t0) * 1000, e, exc_info=True)
            raise


# ---------- 内部工具 ----------

def _rows_to_list(rows) -> List[Dict[str, Any]]:
    return [dict(r._mapping) for r in rows]


def _next_seq(table_name: str) -> int:
    t = {"feishu_contacts": _feishu_contacts,
         "alert_events": _alert_events,
         "alert_push_log": _alert_push_log}.get(table_name)
    if t is None:
        return 0
    try:
        from sqlalchemy import func, select

        def _do():
            with _engine.connect() as conn:
                return conn.execute(select(func.count()).select_from(t)).scalar() or 0
        return _run_with_fallback(f"_next_seq<{table_name}>", _do)
    except Exception:
        return 0


def _make_event_id(event: Dict) -> str:
    cycle = event.get("cycle_id", -1)
    power = event.get("power_point", 0.0)
    cond = event.get("condition", "unknown")
    ts = event.get("timestamp")
    ts_str = ts.strftime("%Y%m%d%H%M%S") if isinstance(ts, datetime) else str(ts)
    return f"{cycle}_{power:.1f}_{cond}_{ts_str}"


# ---------- feishu_contacts CRUD ----------

def db_add_contact(
    name: str, open_id: str, phone: str,
    app_id: str, app_secret: str, verified: bool = False,
) -> Tuple[bool, str]:
    cid = f"fc_{int(time.time())}_{_next_seq('feishu_contacts')}"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stmt = _feishu_contacts.insert().values(
        id=cid, name=name.strip(), open_id=open_id.strip(),
        phone=phone.strip(), app_id=app_id.strip(),
        app_secret=app_secret.strip(), verified=int(verified),
        enabled=1, created_at=now, last_alert="",
    )
    logger.info(
        "[业务写入] db_add_contact 准备写入: id=%s name=%s phone=%s verified=%s",
        cid, name, phone[:3] + "****" + phone[-4:] if len(phone) >= 7 else "***",
        verified,
    )

    def _do() -> Tuple[bool, str]:
        t = time.perf_counter()
        with _engine.connect() as conn:
            cur = conn.execute(stmt)
            conn.commit()
        cost = (time.perf_counter() - t) * 1000
        affected = getattr(cur, "rowcount", 1)
        logger.info(
            "[业务写入] ✅ db_add_contact 提交成功 | 表=feishu_contacts "
            "rows=%d | 耗时=%.0fms | id=%s name=%s",
            affected, cost, cid, name,
        )
        return True, cid

    try:
        return _run_with_fallback("db_add_contact", _do)
    except Exception as e:
        logger.error("[业务写入] ❌ db_add_contact 写入失败 | name=%s err=%s",
                     name, e, exc_info=True)
        return False, f"写入失败: {e}"


def db_list_contacts() -> List[Dict]:
    def _do() -> List[Dict]:
        stmt = _feishu_contacts.select().order_by(_feishu_contacts.c.created_at)
        with _engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        result = []
        for r in _rows_to_list(rows):
            secret = r.get("app_secret", "")
            r["app_secret_masked"] = secret[:4] + "****" if len(secret) > 4 else "****"
            r.pop("app_secret", None)
            r["verified"] = bool(r.get("verified", 0))
            r["enabled"] = bool(r.get("enabled", 1))
            result.append(r)
        return result
    return _run_with_fallback("db_list_contacts", _do)


def db_get_verified_contacts() -> List[Dict]:
    def _do() -> List[Dict]:
        stmt = _feishu_contacts.select().where(
            (_feishu_contacts.c.verified == 1) & (_feishu_contacts.c.enabled == 1)
        )
        with _engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        result = []
        for r in _rows_to_list(rows):
            r["verified"] = bool(r.get("verified", 0))
            r["enabled"] = bool(r.get("enabled", 1))
            result.append(r)
        return result
    return _run_with_fallback("db_get_verified_contacts", _do)


def db_remove_contact(cid: str) -> Tuple[bool, str]:
    stmt = _feishu_contacts.delete().where(_feishu_contacts.c.id == cid)

    def _do() -> Tuple[bool, str]:
        with _engine.connect() as conn:
            cur = conn.execute(stmt)
            conn.commit()
        if cur.rowcount > 0:
            logger.info("DB: 删除联系人 id=%s", cid)
            return True, "删除成功"
        return False, "联系人不存在"

    try:
        return _run_with_fallback("db_remove_contact", _do)
    except Exception as e:
        return False, f"删除失败: {e}"


def db_toggle_contact(cid: str, enabled: bool) -> Tuple[bool, str]:
    stmt = _feishu_contacts.update().where(
        _feishu_contacts.c.id == cid
    ).values(enabled=int(enabled))

    def _do() -> Tuple[bool, str]:
        with _engine.connect() as conn:
            cur = conn.execute(stmt)
            conn.commit()
        if cur.rowcount > 0:
            return True, "操作成功"
        return False, "联系人不存在"

    try:
        return _run_with_fallback("db_toggle_contact", _do)
    except Exception as e:
        return False, f"操作失败: {e}"


def db_update_last_alert(cid: str) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stmt = _feishu_contacts.update().where(
        _feishu_contacts.c.id == cid
    ).values(last_alert=now)

    def _do() -> None:
        with _engine.connect() as conn:
            conn.execute(stmt)
            conn.commit()
    try:
        _run_with_fallback("db_update_last_alert", _do)
    except Exception as e:
        logger.warning("DB: 更新 last_alert 失败 cid=%s err=%s", cid, e)


def db_get_contact_raw(cid: str) -> Optional[Dict]:
    """按 ID 取单个联系人完整记录 (含原 app_secret)。

    仅用于「发送消息」等内部操作, 返回值绝不能直接暴露给列表 UI。
    """
    logger.info("[DB: 取联系人详情] 请求 cid=%s backend=%s", cid, _backend_name())
    stmt = _feishu_contacts.select().where(_feishu_contacts.c.id == cid)

    def _do() -> Optional[Dict]:
        t0 = time.perf_counter()
        with _engine.connect() as conn:
            row = conn.execute(stmt).mappings().first()
        dt_ms = (time.perf_counter() - t0) * 1000
        if row is None:
            logger.warning("[DB: 取联系人详情] ❌ 查无记录 cid=%s 耗时=%.0fms backend=%s",
                           cid, dt_ms, _backend_name())
            return None
        d = dict(row)
        d["verified"] = bool(d.get("verified"))
        d["enabled"] = bool(d.get("enabled"))
        sec_len = len(str(d.get("app_secret") or ""))
        logger.info(
            "[DB: 取联系人详情] ✅ 命中 | cid=%s name=%s app_id=%s "
            "app_secret_len=%d verified=%s enabled=%s 耗时=%.0fms backend=%s",
            cid, d.get("name"), d.get("app_id"), sec_len,
            d["verified"], d["enabled"], dt_ms, _backend_name(),
        )
        return d

    try:
        return _run_with_fallback("db_get_contact_raw", _do)
    except Exception as e:
        logger.warning("[DB: 取联系人详情] 💥 异常 cid=%s err=%s", cid, e)
        return None


def db_set_contact_verified(cid: str, verified: bool) -> Tuple[bool, str]:
    """设置联系人 verified 字段 (True=测试消息已发送成功, 可进入推送列表)。"""
    logger.info(
        "[DB: 设置联系人 verified] 请求 cid=%s 目标值=%s backend=%s",
        cid, bool(verified), _backend_name(),
    )
    stmt = _feishu_contacts.update().where(
        _feishu_contacts.c.id == cid
    ).values(verified=int(verified))

    def _do() -> Tuple[bool, str]:
        t0 = time.perf_counter()
        with _engine.connect() as conn:
            cur = conn.execute(stmt)
            conn.commit()
        dt_ms = (time.perf_counter() - t0) * 1000
        rc = cur.rowcount if cur and hasattr(cur, "rowcount") else 0
        if rc > 0:
            logger.info(
                "[DB: 设置联系人 verified] ✅ 写回成功 | cid=%s 新值=%s "
                "影响行数=%d 耗时=%.0fms backend=%s",
                cid, bool(verified), rc, dt_ms, _backend_name(),
            )
            return True, f"已更新验证状态 (影响 {rc} 行)"
        logger.warning(
            "[DB: 设置联系人 verified] ⚠️ 0 行受影响(联系人可能不存在) | "
            "cid=%s 新值=%s 耗时=%.0fms backend=%s",
            cid, bool(verified), dt_ms, _backend_name(),
        )
        return False, "联系人不存在 / 未匹配到待更新行"

    try:
        ok, msg = _run_with_fallback("db_set_contact_verified", _do)
        return ok, msg
    except Exception as e:
        logger.error("[DB: 设置联系人 verified] 💥 异常 cid=%s err=%s", cid, e)
        return False, f"操作失败: {e}"


# ---------- alert_events CRUD ----------

def db_save_event(event: Dict) -> str:
    eid = _make_event_id(event)
    ts = event.get("timestamp", datetime.now())
    ts_str = ts.strftime("%Y-%m-%d %H:%M:%S") if isinstance(ts, datetime) else str(ts)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    values = dict(
        id=eid, timestamp=ts_str,
        cycle_id=int(event.get("cycle_id", -1)),
        power_point=float(event.get("power_point", 0)),
        condition=event.get("condition", ""),
        label=event.get("label", ""),
        value=float(event.get("value", 0)),
        threshold=float(event.get("threshold", 0)),
        operator=event.get("operator", ">"),
        signal=event.get("signal", ""),
        unit=event.get("unit", "mV"),
        data_count=int(event.get("data_count", 0)),
        quality=event.get("quality", "正常"),
        message=event.get("message", ""),
        status="pending",
        created_at=now,
    )
    logger.info(
        "[业务写入] db_save_event 准备写入: id=%s cycle=%d pp=%.1fkW cond=%s "
        "value=%.1f%s threshold=%.0f%s status=pending",
        values["id"], values["cycle_id"], values["power_point"],
        values["condition"], values["value"], values["unit"],
        values["threshold"], values["unit"],
    )

    def _do() -> str:
        t = time.perf_counter()
        if _USE_MYSQL:
            upsert_sql = text(
                "INSERT INTO alert_events "
                "(id,`timestamp`,cycle_id,power_point,`condition`,label,value,threshold,"
                "`operator`,`signal`,`unit`,data_count,quality,message,status,created_at) "
                "VALUES (:id,:timestamp,:cycle_id,:power_point,:condition,:label,:value,"
                ":threshold,:operator,:signal,:unit,:data_count,:quality,:message,"
                ":status,:created_at) "
                "ON DUPLICATE KEY UPDATE id=id"
            )
            with _engine.connect() as conn:
                cur = conn.execute(upsert_sql, values)
                conn.commit()
            mode = "INSERT_OR_IGNORE_DUP(MySQL)"
            affected = getattr(cur, "rowcount", 0)
        else:
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert
            stmt = sqlite_insert(_alert_events).values(**values).prefix_with("OR IGNORE")
            with _engine.connect() as conn:
                cur = conn.execute(stmt)
                conn.commit()
            mode = "INSERT_OR_IGNORE_DUP(SQLite)"
            affected = getattr(cur, "rowcount", 0)
        cost_ms = (time.perf_counter() - t) * 1000
        logger.info(
            "[业务写入] ✅ db_save_event 提交成功 | 表=alert_events "
            "mode=%s rows=%d | 耗时=%.0fms | id=%s cycle=%d pp=%.1fkW",
            mode, affected, cost_ms, eid, values["cycle_id"], values["power_point"],
        )
        return eid
    return _run_with_fallback("db_save_event", _do)


def db_list_events(
    status_filter: Optional[str] = None,
    condition_filter: Optional[str] = None,
) -> List[Dict]:
    from sqlalchemy import select

    def _do() -> List[Dict]:
        stmt = select(_alert_events)
        clauses = []
        if status_filter and status_filter != "all":
            clauses.append(_alert_events.c.status == status_filter)
        if condition_filter:
            clauses.append(_alert_events.c.condition.like(f"%{condition_filter}%"))
        if clauses:
            stmt = stmt.where(*clauses)
        stmt = stmt.order_by(_alert_events.c.timestamp.desc())
        with _engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return _rows_to_list(rows)
    return _run_with_fallback("db_list_events", _do)


def db_get_event_status(eid: str) -> str:
    from sqlalchemy import select

    def _do() -> str:
        stmt = select(_alert_events.c.status).where(_alert_events.c.id == eid)
        with _engine.connect() as conn:
            row = conn.execute(stmt).fetchone()
        return row._mapping["status"] if row else "pending"
    return _run_with_fallback("db_get_event_status", _do)


def db_get_event_status_map(eids: List[str]) -> Dict[str, str]:
    if not eids:
        return {}
    from sqlalchemy import select

    def _do() -> Dict[str, str]:
        stmt = select(_alert_events.c.id, _alert_events.c.status).where(
            _alert_events.c.id.in_(eids)
        )
        with _engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return {r._mapping["id"]: r._mapping["status"] for r in rows}
    return _run_with_fallback("db_get_event_status_map", _do)


def db_set_event_status(eid: str, status: str) -> None:
    stmt = _alert_events.update().where(
        _alert_events.c.id == eid
    ).values(status=status)
    logger.info("[业务更新] db_set_event_status: id=%s 状态切换 -> %s", eid, status)

    def _do() -> None:
        t = time.perf_counter()
        with _engine.connect() as conn:
            cur = conn.execute(stmt)
            conn.commit()
        cost_ms = (time.perf_counter() - t) * 1000
        rows = getattr(cur, "rowcount", 0)
        if rows > 0:
            logger.info(
                "[业务更新] ✅ db_set_event_status 生效 | 表=alert_events "
                "rows=%d | 耗时=%.0fms | id=%s -> %s",
                rows, cost_ms, eid, status,
            )
        else:
            logger.warning(
                "[业务更新] ⚠️  db_set_event_status 未匹配到行(可能事件id不存在) "
                "| 表=alert_events rows=0 | 耗时=%.0fms | id=%s",
                cost_ms, eid,
            )
    _run_with_fallback("db_set_event_status", _do)


def db_count_events() -> int:
    from sqlalchemy import func, select

    def _do() -> int:
        stmt = select(func.count()).select_from(_alert_events)
        with _engine.connect() as conn:
            return conn.execute(stmt).scalar() or 0
    try:
        return _run_with_fallback("db_count_events", _do)
    except Exception:
        return 0


# ---------- alert_push_log CRUD ----------

def db_log_push(
    event_id: str, contact_id: str, contact_name: str,
    success: bool, message: str,
) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stmt = _alert_push_log.insert().values(
        event_id=event_id, contact_id=contact_id,
        contact_name=contact_name, success=int(success),
        message=message, push_time=now,
    )
    short_msg = message[:80] + ("…" if len(message) > 80 else "")
    logger.info(
        "[业务写入] db_log_push 准备写入: eid=%s contact=%s success=%s msg=%s",
        event_id[:40] + ("…" if len(event_id) > 40 else ""),
        contact_name, success, short_msg,
    )

    def _do() -> None:
        t = time.perf_counter()
        with _engine.connect() as conn:
            cur = conn.execute(stmt)
            conn.commit()
        cost_ms = (time.perf_counter() - t) * 1000
        rows = getattr(cur, "rowcount", 1)
        logger.info(
            "[业务写入] ✅ db_log_push 提交成功 | 表=alert_push_log rows=%d | 耗时=%.0fms "
            "| eid=%s contact=%s success=%s",
            rows, cost_ms,
            event_id[:40] + ("…" if len(event_id) > 40 else ""),
            contact_name, success,
        )
    try:
        _run_with_fallback("db_log_push", _do)
    except Exception as e:
        logger.error("[业务写入] ❌ db_log_push 写入失败 | eid=%s contact=%s err=%s",
                     event_id, contact_name, e, exc_info=True)


def db_list_push_logs(event_id: Optional[str] = None) -> List[Dict]:
    from sqlalchemy import select

    def _do() -> List[Dict]:
        if event_id:
            stmt = select(_alert_push_log).where(
                _alert_push_log.c.event_id == event_id
            ).order_by(_alert_push_log.c.push_time.desc())
        else:
            stmt = select(_alert_push_log).order_by(
                _alert_push_log.c.push_time.desc()
            ).limit(100)
        with _engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        result = []
        for r in _rows_to_list(rows):
            r["success"] = bool(r.get("success", 0))
            result.append(r)
        return result
    return _run_with_fallback("db_list_push_logs", _do)


# ---------- 诊断 ----------

def get_db_backend_info() -> Dict[str, str]:
    with _state_lock:
        backend = "MySQL (腾讯云)" if _USE_MYSQL else "SQLite (本地降级)"
    info: Dict[str, str] = {"backend": backend}
    if _USE_MYSQL:
        info.update({
            "host": _DB_CFG["DB_HOST"],
            "port": _DB_CFG["DB_PORT"],
            "database": _DB_CFG["DB_NAME"],
            "user": _DB_CFG["DB_USER"],
        })
    else:
        info["path"] = str(_SQLITE_PATH)
        if _fallback_triggered:
            info["note"] = "运行时从 MySQL 自动降级; 外网恢复后请重启应用"
    return info


def print_console_db_status(header: str = "DB 运行时状态") -> None:
    """给控制台脚本用的 DB 横幅打印。

    调用时机: main() 开头 (db_init() 之后) 或 main() 结束前。
    """
    info = get_db_backend_info()
    backend = info["backend"]
    bar = "═" * 62
    print(f"\n{bar}")
    print(f"  {header}")
    print(f"{bar}")
    print(f"  当前后端: {backend}")
    if "MySQL" in backend:
        print(f"  Host:     {info.get('host','')}:{info.get('port','')}")
        print(f"  DB:       {info.get('database','')}")
        print(f"  User:     {info.get('user','')}")
        print(f"  降级机制: 外网断开自动切 SQLite (日志/控制台会打出 [DB 降级] 横幅)")
    else:
        print(f"  SQLite:   {info.get('path','')}")
        if info.get("note"):
            print(f"  ⚠️  注意:   {info['note']}")
    print(f"{bar}")


def render_streamlit_db_status(
    container,  # st.sidebar 或任意 st.container
    position: str = "sidebar",
) -> None:
    """给 Streamlit 页面用的 DB 状态卡片 + 降级警告。

    - 正常 MySQL: 显示一个 info/ success 提示 (host/db/user)
    - 降级 SQLite: 醒目 error 横幅提示用户注意
    """
    info = get_db_backend_info()
    backend = info["backend"]
    with container:
        if position == "sidebar":
            import streamlit as _st
            _st.divider()
            _st.subheader("🗄️ 数据库状态")
        if "MySQL" in backend:
            container.success(
                f"**后端: MySQL (腾讯云)**\n\n"
                f"Host: `{info.get('host','')}:{info.get('port','')}`  \n"
                f"DB: `{info.get('database','')}`  User: `{info.get('user','')}`\n\n"
                f"若外网中断, 系统会自动降级到本地 SQLite, 日志中将出现 `[DB 降级]` 横幅。"
            )
        else:
            note = info.get("note", "")
            if note:
                container.error(
                    f"**⚠️  当前: SQLite (本地降级)**\n\n"
                    f"MySQL 不可用, 已自动切换到本地 SQLite。  \n"
                    f"{note}  \n"
                    f"文件: `{info.get('path','')}`"
                )
            else:
                container.info(
                    f"**后端: SQLite (本地)**\n\n"
                    f".env 未配置 MySQL 或启动阶段已降级。  \n"
                    f"文件: `{info.get('path','')}`"
                )



# ---------- 单元测试 ----------

if __name__ == "__main__":
    import logging as _lg
    _lg.basicConfig(level=_lg.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    print(f"\n===== DB Backend: {get_db_backend_info()} =====")

    print("\n===== 测试1: 初始化数据库 =====")
    init_db()
    print("  [PASS] 初始化完成")

    print("\n===== 测试2: 添加联系人 =====")
    ok, cid = db_add_contact("测试员", "ou_abc123", "13800001111",
                             "cli_test", "sec_test", verified=True)
    assert ok, f"添加失败: {cid}"
    print(f"  [PASS] 添加成功: id={cid}")

    print("\n===== 测试3: 列表(隐藏 secret) =====")
    contacts = db_list_contacts()
    assert len(contacts) >= 1
    c0 = contacts[0]
    assert "app_secret" not in c0
    assert "app_secret_masked" in c0
    assert c0["verified"] is True
    print(f"  [PASS] 列表 {len(contacts)} 人, secret={c0['app_secret_masked']}")

    print("\n===== 测试4: 获取已验证联系人(含完整 secret) =====")
    verified = db_get_verified_contacts()
    assert len(verified) >= 1
    assert "app_secret" in verified[0]
    print(f"  [PASS] 已验证 {len(verified)} 人")

    print("\n===== 测试5: 保存预警事件 =====")
    event = {
        "timestamp": datetime.now(), "cycle_id": 0, "power_point": 175.5,
        "condition": "离均差>50mV", "label": "离均差", "value": 55.0,
        "threshold": 50.0, "operator": ">", "signal": "VoltDev",
        "unit": "mV", "data_count": 100, "quality": "正常",
        "message": "离均差>50mV: 55.0mV > 50mV",
    }
    eid = db_save_event(event)
    assert db_count_events() >= 1
    print(f"  [PASS] 保存事件: id={eid}")

    print("\n===== 测试6: 查询事件(按状态) =====")
    pending = db_list_events(status_filter="pending")
    assert len(pending) >= 1
    print(f"  [PASS] pending 事件 {len(pending)} 条")

    print("\n===== 测试7: 更新事件状态 =====")
    db_set_event_status(eid, "confirmed")
    assert db_get_event_status(eid) == "confirmed"
    print(f"  [PASS] 状态已更新为 confirmed")

    print("\n===== 测试8: 记录推送日志 =====")
    db_log_push(eid, cid, "测试员", True, "发送成功")
    logs = db_list_push_logs(eid)
    assert len(logs) >= 1
    assert logs[0]["success"] is True
    print(f"  [PASS] 推送日志 {len(logs)} 条")

    print("\n===== 测试9: 删除联系人 =====")
    ok9, msg9 = db_remove_contact(cid)
    assert ok9
    print(f"  [PASS] 删除成功")

    print("\n===== 测试10: 清理测试数据 =====")
    def _cleanup():
        with _engine.connect() as conn:
            conn.execute(_alert_push_log.delete())
            conn.execute(_alert_events.delete())
            conn.execute(_feishu_contacts.delete().where(
                _feishu_contacts.c.name == "测试员"
            ))
            conn.commit()
    _run_with_fallback("cleanup", _cleanup)
    print(f"  [PASS] 已清理")

    print("\n===== 全部测试通过 =====")
