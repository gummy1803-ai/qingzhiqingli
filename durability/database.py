"""MySQL 数据库模块（腾讯云 MySQL 兼容 + 运行时自动降级 SQLite）。

——— 数据表一览 ———
[A类·整车]
  vehicle_data_files        : 上传过的整车/耐久/台架 文件目录(含 file_hash 去重)
  vehicle_minute_samples    : 整车数据按 1 分钟 resample 后的聚合明细(核心大表)
[B类·耐久工步]
  durability_stages         : docx 解析后的耐久工步(一条 stage × step = 一行)
[C类·台架循环]
  bench_cycle_stats         : 台架 CSV 按 (循环×功率点) 聚合后的明细
[预警·联系人]
  feishu_contacts           : 飞书对接人员(凭证/验证状态/启用状态)
  alert_events              : 预警事件(循环/功率/条件/数值/状态) + bench_cycle_id
  alert_push_log            : 预警推送记录(事件ID/联系人ID/推送结果)

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

import pandas as pd

from dotenv import load_dotenv
from sqlalchemy import (
    create_engine, MetaData, Table, Column, String, Integer, Float,
    Text, Index, text, JSON as _JSON, DateTime, SmallInteger, Boolean,
)
from sqlalchemy.types import JSON as _SQLA_JSON
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError, InterfaceError, DBAPIError

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = _PROJECT_ROOT / ".env"

_FALLBACK_BANNER = "═" * 55


# ---------- 连接配置加载 ----------

def _load_db_config() -> Dict[str, str]:
    """加载 MySQL 配置,三源依次优先级覆盖:
    1. Streamlit Cloud Secrets 面板(通过 st.secrets, 填了就优先)
    2. 项目根目录 .env 文件(通过 python-dotenv 注入 os.environ)
    3. 当前进程 os.environ(CI / 手动 export DB_HOST=...)

    ⚠️  Streamlit Secrets 不会自动写入 os.environ, 所以我们手动把 DB_*
    搬到 os.environ, 这样后续所有代码(不管用没用 st.secrets)都能吃到。
    """
    keys = ["DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME"]

    # --- 优先级 1:尝试从 Streamlit Cloud Secrets 回填 ---
    try:
        import streamlit as st  # 延迟 import, 在没有 st 上下文的命令行环境也不会崩
        # hasattr 防止旧版本 Streamlit 没 secrets
        secrets_keys = getattr(st, "secrets", None)
        if secrets_keys is not None:
            got_all = True
            got_any = False
            for k in keys:
                try:
                    v = str(secrets_keys[k]).strip() if k in secrets_keys else ""
                except Exception:
                    v = ""
                if v:
                    got_any = True
                    os.environ[k] = v  # ← 关键:搬进 os.environ, 后续代码零改动
                else:
                    got_all = False
            if got_any:
                if got_all:
                    logger.info(
                        "[DB 配置] 已从 Streamlit Secrets 读取 5 项 MySQL 连接参数"
                        " (已写入 os.environ)"
                    )
                else:
                    logger.warning(
                        "[DB 配置] Streamlit Secrets 里 MySQL 参数不完整,"
                        " 请确认 5 个 DB_* 键名都填了"
                    )
    except Exception as _se:
        # 本地 / 单元测试 / 脚本刚 import 还没进入 st 上下文:都会抛错, 静默跳过
        logger.debug(
            "[DB 配置] 跳过 Streamlit Secrets(非 Cloud 环境或 context 未就绪): %s", _se
        )

    # --- 优先级 2:读本地 .env (override=True 会覆盖上面 Secrets 写的值, 本地调试更灵活) ---
    if _ENV_PATH.exists():
        try:
            load_dotenv(_ENV_PATH, override=True)
            logger.info("[DB 配置] 已加载 .env 文件: %s", _ENV_PATH)
        except Exception as _de:
            logger.warning("[DB 配置] .env 加载失败(不影响 Cloud Secrets 逻辑): %s", _de)

    cfg = {k: os.getenv(k, "").strip() for k in keys}
    if all(cfg.values()):
        return cfg
    logger.warning(
        "\n%s\n[DB 降级] .env + Streamlit Secrets 均未提供完整 MySQL 5 项参数,"
        " 启动即使用本地 SQLite\n"
        "[DB 降级] 已填的参数: %s\n"
        "[DB 降级] 如需启用腾讯云 MySQL, 请检查 .env 或 Streamlit Secrets\n%s",
        _FALLBACK_BANNER,
        {k: ("***" if k == "DB_PASSWORD" and cfg[k] else (cfg[k] or "(空)")) for k in keys},
        _FALLBACK_BANNER,
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

# Streamlit Cloud 持久化存储路径(应用重启不丢失)
_STREAMLIT_CLOUD_ROOT = Path("/mount/src/qingzhiqingli")
if _STREAMLIT_CLOUD_ROOT.exists():
    _SQLITE_PATH = _STREAMLIT_CLOUD_ROOT / "data" / "app.db"
    logger.info("[DB 存储] 检测到 Streamlit Cloud 环境, 使用持久化路径: %s", _SQLITE_PATH)
else:
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
    Column("bench_cycle_id", Integer),   # 🔗 指向 bench_cycle_stats.id, 预警事件反向找到台架行
    Column("created_at", String(32), nullable=False),
    Index("idx_events_status", "status"),
    Index("idx_events_cycle_power", "cycle_id", "power_point"),
    Index("idx_events_bench_cycle", "bench_cycle_id"),
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


# =========================================================================
# 新增 4 张数据落库表(对应方案 A:先 SQLite,后续配白名单后可切 MySQL 零改动)
# =========================================================================

# ---------- A1: vehicle_data_files ----------
# 所有上传文件的"目录"索引(整车 / 耐久 / 台架 三类一起记录)
# 唯一键: file_hash(按文件字节 SHA256),传同一文件 N 次只入库 1 次
_vehicle_data_files = Table(
    "vehicle_data_files", _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("data_kind", String(16), nullable=False, comment="整车/耐久工步/台架循环"),
    Column("vehicle_id", String(32), default=""),
    Column("file_name", String(512), nullable=False),
    Column("file_hash", String(64), nullable=False, unique=True),
    Column("row_count", Integer, default=0),
    Column("time_min", String(32)),
    Column("time_max", String(32)),
    Column("col_signals", _SQLA_JSON),
    Column("upload_user", String(64), default="cloud"),
    Column("uploaded_at", String(32), nullable=False),
    Column("status", String(16), default="uploaded",
           comment="uploaded/aggregated/failed"),
    Column("status_note", Text, default=""),
    Column("agg_rows", Integer, default=0, comment="聚合后入明细行数"),
    Column("extra_meta", _SQLA_JSON, comment="耐久sample/台架rig等额外元"),
    Index("idx_vdf_vehicle", "vehicle_id"),
    Index("idx_vdf_kind", "data_kind"),
    Index("idx_vdf_uploaded", "uploaded_at"),
    mysql_engine="InnoDB",
    mysql_charset="utf8mb4",
    mysql_collate="utf8mb4_unicode_ci",
)

# ---------- A2: vehicle_minute_samples (真正支撑整车看板/燃电/性能的核心明细表) ----------
# 企业 9 个核心字段 + 扩展字段(车速/里程/氢耗等),联合唯一键 vehicle_id x minute_ts
# 单位口径: 严格跟企业对齐(MinCellVoltage/AvgCellVoltage/AvgCellVoltDev 均为 mV)
_vehicle_minute_samples = Table(
    "vehicle_minute_samples", _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("vehicle_id", String(32), nullable=False),
    Column("minute_ts", String(32), nullable=False,
           comment="ISO 格式字符串分钟桶(如 '2026-08-23T10:15:00')"),
    Column("file_id", Integer),
    # 企业 9 字段 ↓↓↓
    Column("FC_CurrOut", Float(precision=53)),
    Column("FC_VoltOut", Float(precision=53)),
    Column("FC_NetPwrOut", Float(precision=53)),
    Column("FC_MinCellVoltage", Float(precision=53), comment="mV"),
    Column("FC_MinVoltageChannel", Integer),
    Column("FC_AvgCellVoltage", Float(precision=53), comment="mV"),
    Column("FC_AvgCellVoltDev", Float(precision=53), comment="mV"),
    Column("FC_VehicleIsolationR", Float(precision=53), comment="kΩ"),
    Column("FC_RunTime_Hours", Float(precision=53)),
    # 扩展字段 ↓↓↓ (metrics.py 需要的氢耗/里程)
    Column("FC_VehicleSpd", Float(precision=53), comment="km/h"),
    Column("FC_VehicleKM", Float(precision=53), comment="km"),
    Column("FC_HydCmInstts", Float(precision=53)),
    Column("FC_HydCmPerHundred", Float(precision=53), comment="kg/100km"),
    Column("FC_ErrorCode", Integer),
    Column("FC_MainSts", Integer),
    # 兜底: 上传的文件里可能还带别的列, 这里不一一加列, 缺列时按 NULL 写入即可
    Index("ix_vms_vid_ts", "vehicle_id", "minute_ts", unique=True),
    Index("ix_vms_vid_range", "vehicle_id", "minute_ts"),
    mysql_engine="InnoDB",
    mysql_charset="utf8mb4",
    mysql_collate="utf8mb4_unicode_ci",
)

# ---------- B1: durability_stages ----------
# docx 解析后每条工步一行。去重联合唯一键: file_id + stage + step_idx
_durability_stages = Table(
    "durability_stages", _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("file_id", Integer, index=True),
    Column("sample_name", String(256), default=""),
    Column("stage", String(64), comment="耐久阶段标签,如 '0-5'"),
    Column("stage_start_h", Float(precision=53)),
    Column("step_idx", Integer),
    # docx 标准列(中文名原样):数值化后落库
    Column("target_power_kw", Float(precision=53)),
    Column("humidity_pct", Float(precision=53)),
    Column("temperature_c", Float(precision=53)),
    Column("net_power_kw", Float(precision=53)),
    Column("stack_current_a", Float(precision=53)),
    Column("avg_cell_voltage_v", Float(precision=53)),
    Column("avg_voltage_deviation_v", Float(precision=53), comment="离均差,V"),
    Column("compressor_power_kw", Float(precision=53)),
    Column("pump_power_kw", Float(precision=53)),
    Column("coolant_in_c", Float(precision=53)),
    Column("coolant_out_c", Float(precision=53)),
    Column("hfr", Float(precision=53)),
    Column("lfr", Float(precision=53)),
    Column("voltage_variance", Float(precision=53)),
    Column("raw_file_name", String(512)),
    Column("uploaded_at", String(32)),
    Index("ix_ds_fileid_stage_step", "file_id", "stage", "step_idx", unique=True),
    Index("ix_ds_sample", "sample_name"),
    Index("ix_ds_stage_start", "stage_start_h"),
    mysql_engine="InnoDB",
    mysql_charset="utf8mb4",
    mysql_collate="utf8mb4_unicode_ci",
)

# ---------- C1: bench_cycle_stats ----------
# 台架 aggregate_durability_stats 每行直接一条。唯一 (rig_id, cycle_id, power_point)
_bench_cycle_stats = Table(
    "bench_cycle_stats", _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("file_id", Integer, index=True),
    Column("rig_id", String(64), default="unknown"),
    Column("cycle_id", Integer, nullable=False),
    Column("power_point", Float(precision=53), nullable=False),
    Column("data_count", Integer),
    Column("quality", String(32), default="正常"),
    # 均值列(按 _SIGNAL_COLS / signal_columns 动态,有就写没有 NULL)
    Column("FC_AvgCellVoltage_mean", Float(precision=53), comment="mV"),
    Column("FC_AvgCellVoltDev_mean", Float(precision=53), comment="mV"),
    Column("FC_VARVoltage_mean", Float(precision=53)),
    Column("FC_LFR_mean", Float(precision=53)),
    Column("FC_HFR_mean", Float(precision=53)),
    Column("FC_CurrOut_mean", Float(precision=53)),
    Column("FC_VoltOut_mean", Float(precision=53)),
    Column("FC_NetPwrOut_mean", Float(precision=53)),
    Column("FC_MinCellVoltage_mean", Float(precision=53), comment="mV"),
    # 波动列(选写,只把关键信号的 std 也落一份便于 UI 稳定性展示)
    Column("FC_AvgCellVoltage_std", Float(precision=53)),
    Column("FC_AvgCellVoltDev_std", Float(precision=53)),
    Column("source_file_name", String(512)),
    Column("created_at", String(32)),
    Index("ix_bcs_rig_cycle_pp", "rig_id", "cycle_id", "power_point", unique=True),
    Index("ix_bcs_cycle_pp", "cycle_id", "power_point"),
    mysql_engine="InnoDB",
    mysql_charset="utf8mb4",
    mysql_collate="utf8mb4_unicode_ci",
)

# ---------- Branch Management Tables ----------

# 分支表: 存储分支信息
_branches = Table(
    "branches", _metadata,
    Column("id", String(32), primary_key=True),
    Column("name", String(128), nullable=False, unique=True),
    Column("description", Text, default=""),
    Column("is_active", Boolean, default=False),
    Column("parent_branch_id", String(32), comment="来源分支ID,用于fork"),
    Column("created_at", String(32), nullable=False),
    Column("updated_at", String(32), nullable=False),
    Column("file_count", Integer, default=0),
    Column("total_size", Float(precision=53), default=0),
    Index("ix_branches_name", "name"),
    mysql_engine="InnoDB",
    mysql_charset="utf8mb4",
    mysql_collate="utf8mb4_unicode_ci",
)

# 文件快照表: 存储每个分支的文件状态
_branch_file_snapshots = Table(
    "branch_file_snapshots", _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("branch_id", String(32), nullable=False, index=True),
    Column("file_path", String(1024), nullable=False),
    Column("file_name", String(512), nullable=False),
    Column("file_hash", String(64), nullable=False, index=True),
    Column("file_size", Float(precision=53), default=0),
    Column("file_type", String(32), default=""),
    Column("data_kind", String(16), comment="整车/耐久/台架"),
    Column("vehicle_id", String(32), default=""),
    Column("is_valid", Boolean, default=True),
    Column("status", String(16), default="new",
           comment="new/modified/deleted/unchanged"),
    Column("change_time", String(32)),
    Column("metadata", _SQLA_JSON),
    Index("ix_bfs_branch", "branch_id"),
    Index("ix_bfs_hash", "file_hash"),
    Index("ix_bfs_path", "file_path"),
    mysql_engine="InnoDB",
    mysql_charset="utf8mb4",
    mysql_collate="utf8mb4_unicode_ci",
)

# 版本历史表: 记录分支的变更历史
_branch_versions = Table(
    "branch_versions", _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("branch_id", String(32), nullable=False, index=True),
    Column("version_number", Integer, nullable=False),
    Column("change_type", String(32),
           comment="create/commit/merge/rename/delete"),
    Column("change_summary", Text, default=""),
    Column("changed_files", Integer, default=0),
    Column("created_at", String(32), nullable=False),
    Column("created_by", String(64), default="system"),
    Index("ix_bv_branch", "branch_id"),
    Index("ix_bv_version", "branch_id", "version_number"),
    mysql_engine="InnoDB",
    mysql_charset="utf8mb4",
    mysql_collate="utf8mb4_unicode_ci",
)

# 合并冲突表: 记录合并时的冲突
_merge_conflicts = Table(
    "merge_conflicts", _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("source_branch_id", String(32), nullable=False, index=True),
    Column("target_branch_id", String(32), nullable=False, index=True),
    Column("file_path", String(1024), nullable=False),
    Column("source_hash", String(64), nullable=False),
    Column("target_hash", String(64), nullable=False),
    Column("resolution", String(32), default="pending",
           comment="pending/keep_source/keep_target/manual"),
    Column("resolved_at", String(32)),
    Column("resolved_by", String(64)),
    Column("notes", Text, default=""),
    Column("detected_at", String(32), nullable=False),
    Index("ix_mc_source", "source_branch_id"),
    Index("ix_mc_target", "target_branch_id"),
    Index("ix_mc_resolution", "resolution"),
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
        # ---- Step2.5: 在线 ALTER TABLE 补齐"已有表的新列"(老版本 SQLite/MySQL 兼容) ----
        # create_all(checkfirst=True) 不会给已存在的表加新列(比如 alert_events 的 bench_cycle_id,
        # 是这轮方案A新增的), 所以这里独立做一次"列存在性探测→缺了就ALTER"。
        # 跨后端兼容: SQLite/MySQL 都支持 ALTER TABLE t ADD COLUMN ...
        _apply_schema_migrations(_engine)
        cost_ms = (time.perf_counter() - t0) * 1000
        if _USE_MYSQL:
            logger.info(
                "\n%s\n[DB 后端] ✅ 腾讯云 MySQL 初始化成功 (耗时 %.0fms)\n"
                "[DB 后端] Host:   %s:%s\n"
                "[DB 后端] DB:     %s\n"
                "[DB 后端] User:   %s\n"
                "[DB 后端] 表(联系人·预警): feishu_contacts / alert_events / alert_push_log\n"
                "[DB 后端] 表(数据落库×7): vehicle_data_files / vehicle_minute_samples\n"
                "[DB 后端]                    durability_stages / bench_cycle_stats\n"
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
                "[DB 后端] 表(联系人·预警): feishu_contacts / alert_events / alert_push_log\n"
                "[DB 后端] 表(数据落库×7): vehicle_data_files / vehicle_minute_samples\n"
                "[DB 后端]                    durability_stages / bench_cycle_stats\n%s",
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
            # 降级后也要补齐迁移列
            try:
                _apply_schema_migrations(_engine)
            except Exception as _mg_ex:
                logger.warning("[DB 初始化] 迁移列补齐(降级后)失败: %s", _mg_ex)
        else:
            logger.error("[DB 初始化] init_db 失败 | 总耗时 %.0fms  err=%s",
                         (time.perf_counter() - t0) * 1000, e, exc_info=True)
            raise


# ---------- 内部工具 ----------

# ------ 在线迁移(ALTER TABLE t ADD COLUMN ...) ------
# 解决「SQLAlchemy create_all(checkfirst=True) 不会给已存在的老表加新列」问题。
# 结构: _SCHEMA_MIGRATIONS = [(table_name, column_name, sqlalchemy_column_obj), ...]
_SCHEMA_MIGRATIONS: List[Tuple[str, str, Column]] = [
    ("alert_events", "bench_cycle_id",
     Column("bench_cycle_id", Integer)),
]


def _apply_schema_migrations(engine) -> None:
    """对每一条迁移规则:先查该列是否存在,不存在就 ADD COLUMN。"""
    if len(_SCHEMA_MIGRATIONS) == 0:
        return
    t0 = time.perf_counter()
    _applied = 0
    _skipped = 0
    _failed = 0
    logger.info("[DB 迁移] 开始 检查 %d 条 schema 迁移规则 | backend=%s",
                len(_SCHEMA_MIGRATIONS), engine.dialect.name)
    try:
        insp = engine.dialect.has_table  # type: ignore[attr-defined]
    except Exception:
        insp = None  # 兜底走 pragma 直接查
    import sqlalchemy as _sa
    with engine.connect() as conn:
        for table_name, col_name, col_obj in _SCHEMA_MIGRATIONS:
            rule_key = f"{table_name}.{col_name}"
            try:
                # 跨后端统一方法: insp.get_columns
                cols = _sa.inspect(conn).get_columns(table_name)
                if any(c["name"] == col_name for c in cols):
                    logger.debug("[DB 迁移] ↻ 列 %s 已存在,跳过", rule_key)
                    _skipped += 1
                    continue
            except Exception as _e:
                logger.warning("[DB 迁移] ⚠ 探测列 %s 失败: %s (尝试直接 ALTER)",
                               rule_key, _e)
            # 拼接 DDL: 用 compile 方式拿后端兼容的 ADD COLUMN 子句
            try:
                ddl = str(
                    _sa.schema.AddColumn(table_name, col_obj)
                    .compile(dialect=engine.dialect)
                )
            except Exception:
                # 兜底手动拼
                type_str = "INTEGER"  # 目前唯一的迁移列 bench_cycle_id 就是 INTEGER
                ddl = f"ALTER TABLE {table_name} ADD COLUMN {col_name} {type_str}"
            logger.info("[DB 迁移] → 补列 DDL: %s", ddl.strip())
            try:
                conn.execute(text(ddl))
                conn.commit()
                _applied += 1
                logger.info("[DB 迁移] ✅ 列 %s 补列成功", rule_key)
            except Exception as al_ex:
                # SQLite/MySQL 有时候 "duplicate column" 也会抛, 属于安全忽略
                al_str = str(al_ex).lower()
                if "duplicate" in al_str or "already exists" in al_str:
                    logger.info("[DB 迁移] ↻ 列 %s 已存在(DDL报重复,安全忽略)", rule_key)
                    _skipped += 1
                else:
                    _failed += 1
                    logger.error("[DB 迁移] ❌ 列 %s 补列失败: %s", rule_key, al_ex)
                try:
                    conn.rollback()
                except Exception:
                    pass
    logger.info(
        "[DB 迁移] 完成 总规则=%d 新应用=%d 跳过=%d 失败=%d | 耗时=%.1fms",
        len(_SCHEMA_MIGRATIONS), _applied, _skipped, _failed,
        (time.perf_counter() - t0) * 1000,
    )


def _rows_to_list(rows) -> List[Dict[str, Any]]:
    return [dict(r._mapping) for r in rows]


def _row_to_dict(row) -> Dict[str, Any]:
    """把单条 RowResult 转 dict（row=None 时返回空 dict，不安全；调用方自己判 None）。"""
    return dict(row._mapping)


def _next_seq(table_name: str) -> int:
    t = {"feishu_contacts": _feishu_contacts,
         "alert_events": _alert_events,
         "alert_push_log": _alert_push_log,
         "vehicle_data_files": _vehicle_data_files,
         "vehicle_minute_samples": _vehicle_minute_samples,
         "durability_stages": _durability_stages,
         "bench_cycle_stats": _bench_cycle_stats}.get(table_name)
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
    t0 = time.perf_counter()

    def _do() -> Dict[str, str]:
        stmt = select(_alert_events.c.id, _alert_events.c.status).where(
            _alert_events.c.id.in_(eids)
        )
        with _engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return {r._mapping["id"]: r._mapping["status"] for r in rows}
    result = _run_with_fallback("db_get_event_status_map", _do)
    logger.info("[DB查询] db_get_event_status_map eids=%d 返回=%d | 耗时=%.1fms",
                len(eids), len(result), (time.perf_counter() - t0) * 1000)
    return result


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


# =========================================================================
# 落库 CRUD · 通用工具
# =========================================================================

def _sha256_bytes(raw: bytes) -> str:
    import hashlib
    return hashlib.sha256(raw).hexdigest()


def _upsert_by_unique(table, unique_where: Dict, values: Dict,
                      conn) -> Any:
    """在 conn 内先 select(按唯一键) → 有就 update(仅更新非唯一值列),无就 insert。
    返回 (inserted_flag, row) 。注意: conn 不 commit, 由调用方控制事务。
    """
    from sqlalchemy import select
    sel_cond = [table.c[k] == v for k, v in unique_where.items()]
    exist = conn.execute(select(table).where(*sel_cond)).fetchone()
    non_unique_cols = {c.name for c in table.columns
                       if c.primary_key is False
                       and not any(
                           (idx.unique and c.name in {cc.name for cc in idx.columns})
                           for idx in (table.indexes or set())
                       )}
    if exist is not None:
        upd_cols = {c: v for c, v in values.items()
                    if c in non_unique_cols and c not in unique_where}
        if upd_cols:
            conn.execute(table.update().where(*sel_cond).values(**upd_cols))
        pk_name = [c.name for c in table.columns if c.primary_key][0]
        return False, dict(exist._mapping).get(pk_name) or dict(exist._mapping)
    merged = {**unique_where, **values}
    cur = conn.execute(table.insert().values(**merged))
    pk_name = [c.name for c in table.columns if c.primary_key][0]
    # SQLite / MySQL: cur.lastrowid
    new_id = getattr(cur, "lastrowid", None)
    if new_id is None and table.c[pk_name].type.python_type == int:
        # fallback: 再查一次
        exist2 = conn.execute(select(table).where(*sel_where)).fetchone()
        if exist2 is not None:
            new_id = dict(exist2._mapping).get(pk_name)
    return True, new_id


# =========================================================================
# A1 · vehicle_data_files 上传文件索引 CRUD
# =========================================================================

def db_upsert_data_file(
    data_kind: str,
    file_name: str,
    file_bytes: Optional[bytes] = None,
    file_hash: Optional[str] = None,
    *,
    vehicle_id: str = "",
    row_count: int = 0,
    time_min: Optional[str] = None,
    time_max: Optional[str] = None,
    col_signals: Optional[List[str]] = None,
    upload_user: str = "cloud",
    status: str = "uploaded",
    status_note: str = "",
    agg_rows: int = 0,
    extra_meta: Optional[Dict] = None,
) -> Tuple[int, bool, str]:
    """写入或更新上传文件索引,按 file_hash 唯一。
    返回 (file_id, 是否新插入, file_hash)。"""
    if file_hash is None:
        if file_bytes is None:
            raise ValueError("db_upsert_data_file 需要 file_bytes 或 file_hash 之一")
        file_hash = _sha256_bytes(file_bytes)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    vals = dict(
        data_kind=data_kind,
        vehicle_id=vehicle_id,
        file_name=file_name,
        row_count=row_count,
        time_min=time_min,
        time_max=time_max,
        col_signals=list(col_signals) if col_signals else [],
        upload_user=upload_user,
        uploaded_at=now,
        status=status,
        status_note=status_note,
        agg_rows=agg_rows,
        extra_meta=dict(extra_meta) if extra_meta else {},
    )

    def _do() -> Tuple[int, bool, str]:
        with _engine.connect() as conn:
            inserted, pk = _upsert_by_unique(
                _vehicle_data_files, {"file_hash": file_hash}, vals, conn)
            conn.commit()
        if pk is None:
            logger.error("db_upsert_data_file 未取到主键 file_hash=%s", file_hash)
            return 0, bool(inserted), file_hash
        if isinstance(pk, dict):
            pk = pk.get("id", 0)
        return int(pk), bool(inserted), file_hash

    try:
        fid, inserted, fhash = _run_with_fallback("db_upsert_data_file", _do)
        short = file_name[-40:]
        if inserted:
            logger.info(
                "[落库] ✅ 新文件索引 kind=%s vehicle=%s file=%s row=%d fid=%s",
                data_kind, vehicle_id, short, row_count, fid)
        else:
            logger.info("[落库] ↻ 文件已存在,跳过 hash=%s… fid=%s", fhash[:12], fid)
        return fid, inserted, fhash
    except Exception as e:
        logger.error("[落库] ❌ db_upsert_data_file 失败 name=%s err=%s", file_name, e, exc_info=True)
        raise


def db_list_data_files(data_kind: Optional[str] = None) -> List[Dict]:
    from sqlalchemy import select
    def _do() -> List[Dict]:
        stmt = select(_vehicle_data_files)
        if data_kind:
            stmt = stmt.where(_vehicle_data_files.c.data_kind == data_kind)
        stmt = stmt.order_by(_vehicle_data_files.c.uploaded_at.desc())
        with _engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return _rows_to_list(rows)
    try:
        return _run_with_fallback("db_list_data_files", _do)
    except Exception as e:
        logger.warning("db_list_data_files 失败: %s", e)
        return []


def db_count_data_files(kind: Optional[str] = None) -> int:
    from sqlalchemy import func, select
    def _do() -> int:
        stmt = select(func.count()).select_from(_vehicle_data_files)
        if kind:
            stmt = stmt.where(_vehicle_data_files.c.data_kind == kind)
        with _engine.connect() as conn:
            return conn.execute(stmt).scalar() or 0
    t0 = time.perf_counter()
    try:
        n = _run_with_fallback("db_count_data_files", _do)
        logger.info("[落库A1] 📊 文件目录统计 kind=%s count=%d | 耗时=%.1fms",
                    kind or "(全部)", n, (time.perf_counter() - t0) * 1000)
        return int(n)
    except Exception as e:
        logger.warning("[落库A1] 📊 文件目录统计失败 kind=%s err=%s",
                       kind or "(全部)", e)
        return 0


def db_get_upload_summary() -> Dict:
    """获取上传历史汇总统计(按类型分组)。

    Returns:
        {
            'total_files': int,
            'total_rows': int,
            'by_kind': {'整车': {'count': N, 'rows': N}, ...},
            'by_vehicle': {'212': {'files': N, 'rows': N}, ...},
            'latest_upload': str,
        }
    """
    from sqlalchemy import func, select

    def _do() -> Dict:
        with _engine.connect() as conn:
            # 总计
            total = conn.execute(
                select(func.count()).select_from(_vehicle_data_files)
            ).scalar() or 0
            total_rows = conn.execute(
                select(func.coalesce(func.sum(_vehicle_data_files.c.row_count), 0))
            ).scalar() or 0

            # 按类型分组
            by_kind_rows = conn.execute(
                select(
                    _vehicle_data_files.c.data_kind,
                    func.count().label('cnt'),
                    func.coalesce(func.sum(_vehicle_data_files.c.row_count), 0).label('rows'),
                )
                .group_by(_vehicle_data_files.c.data_kind)
            ).fetchall()

            by_kind = {
                r[0] or 'unknown': {'count': r[1], 'rows': r[2]}
                for r in by_kind_rows
            }

            # 按车辆分组 (仅整车类)
            by_vehicle_rows = conn.execute(
                select(
                    _vehicle_data_files.c.vehicle_id,
                    func.count().label('cnt'),
                    func.coalesce(func.sum(_vehicle_data_files.c.row_count), 0).label('rows'),
                )
                .where(_vehicle_data_files.c.data_kind == '整车')
                .where(_vehicle_data_files.c.vehicle_id != '')
                .group_by(_vehicle_data_files.c.vehicle_id)
            ).fetchall()

            by_vehicle = {
                r[0]: {'files': r[1], 'rows': r[2]}
                for r in by_vehicle_rows
            }

            # 最新上传时间
            latest = conn.execute(
                select(func.max(_vehicle_data_files.c.uploaded_at))
            ).scalar() or ''

        return {
            'total_files': int(total),
            'total_rows': int(total_rows),
            'by_kind': by_kind,
            'by_vehicle': by_vehicle,
            'latest_upload': str(latest),
        }

    t0 = time.perf_counter()
    try:
        result = _run_with_fallback("db_get_upload_summary", _do)
        logger.info("[上传汇总] files=%d rows=%d kinds=%d vehicles=%d | 耗时=%.1fms",
                    result['total_files'], result['total_rows'],
                    len(result['by_kind']), len(result['by_vehicle']),
                    (time.perf_counter() - t0) * 1000)
        return result
    except Exception as e:
        logger.warning("[上传汇总] 失败: %s", e)
        return {'total_files': 0, 'total_rows': 0, 'by_kind': {}, 'by_vehicle': {}, 'latest_upload': ''}


def db_list_data_files_paginated(
    data_kind: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> List[Dict]:
    """分页获取上传文件列表(用于前端展示)。"""
    from sqlalchemy import select

    def _do() -> List[Dict]:
        stmt = select(_vehicle_data_files)
        if data_kind:
            stmt = stmt.where(_vehicle_data_files.c.data_kind == data_kind)
        stmt = stmt.order_by(_vehicle_data_files.c.uploaded_at.desc())
        stmt = stmt.limit(limit).offset(offset)
        with _engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return _rows_to_list(rows)

    try:
        return _run_with_fallback("db_list_data_files_paginated", _do)
    except Exception as e:
        logger.warning("db_list_data_files_paginated 失败: %s", e)
        return []


def db_get_data_file(file_id: int) -> Optional[Dict]:
    """按 ID 获取单个文件索引记录。"""
    from sqlalchemy import select

    def _do() -> Optional[Dict]:
        with _engine.connect() as conn:
            row = conn.execute(
                select(_vehicle_data_files).where(_vehicle_data_files.c.id == int(file_id))
            ).fetchone()
        return _row_to_dict(row) if row else None

    try:
        return _run_with_fallback("db_get_data_file", _do)
    except Exception as e:
        logger.warning("[文件查询] db_get_data_file(id=%s) 失败: %s", file_id, e)
        return None


def db_rename_data_file(file_id: int, new_file_name: str) -> Tuple[bool, str]:
    """重命名已入库数据文件（只改 vehicle_data_files.file_name）。

    原子性: 在同一事务内执行; 带唯一性校验（同车号/类型下同名冲突则拒绝）。

    返回: (是否成功, 说明)
    """
    from sqlalchemy import select, and_
    import time as _t

    t0 = _t.perf_counter()
    new_file_name = (new_file_name or "").strip()
    if not new_file_name:
        return False, "新文件名不能为空"
    if len(new_file_name) > 512:
        return False, "文件名长度不能超过 512 字符"
    try:
        file_id_i = int(file_id)
    except (TypeError, ValueError):
        return False, f"非法 file_id: {file_id}"

    logger.info(
        "[文件重命名] 🔄 开始: file_id=%d, new_name=%s",
        file_id_i, new_file_name,
    )

    def _do() -> Tuple[bool, str]:
        with _engine.connect() as conn:
            # 1) 查原文件
            old = conn.execute(
                select(_vehicle_data_files).where(_vehicle_data_files.c.id == file_id_i)
            ).fetchone()
            if old is None:
                return False, f"文件 ID={file_id_i} 不存在"
            old_d = _row_to_dict(old)
            old_name = str(old_d.get("file_name", ""))
            if old_name == new_file_name:
                return True, "文件名未变化,无需更新"

            # 2) 冲突检测: 同 data_kind + vehicle_id 下不能有重名
            kind = old_d.get("data_kind", "")
            vid = old_d.get("vehicle_id", "") or ""
            dup = conn.execute(
                select(_vehicle_data_files.c.id)
                .where(and_(
                    _vehicle_data_files.c.data_kind == kind,
                    _vehicle_data_files.c.vehicle_id == vid,
                    _vehicle_data_files.c.file_name == new_file_name,
                    _vehicle_data_files.c.id != file_id_i,
                ))
            ).fetchone()
            if dup is not None:
                return False, f"命名冲突: 同类型下已存在同名文件「{new_file_name}」"

            # 3) 执行更新
            conn.execute(
                _vehicle_data_files.update()
                .where(_vehicle_data_files.c.id == file_id_i)
                .values(file_name=new_file_name)
            )
            conn.commit()
        return True, f"已重命名:「{old_name}」→「{new_file_name}」"

    try:
        ok, msg = _run_with_fallback("db_rename_data_file", _do)
        cost = (_t.perf_counter() - t0) * 1000
        if ok:
            logger.info(
                "[文件重命名] ✅ 成功 | file_id=%d | %s | 耗时=%.0fms",
                file_id_i, msg, cost,
            )
        else:
            logger.warning(
                "[文件重命名] ⚠️ 被拒绝 | file_id=%d | 原因=%s | 耗时=%.0fms",
                file_id_i, msg, cost,
            )
        return ok, msg
    except Exception as e:
        cost = (_t.perf_counter() - t0) * 1000
        logger.error(
            "[文件重命名] ❌ 异常 | file_id=%d | err=%s | 耗时=%.0fms",
            file_id_i, e, cost, exc_info=True,
        )
        return False, f"重命名失败: {_extract_error_summary(e)}"


def db_delete_data_file(file_id: int, *, op_user: str = "ui") -> Tuple[bool, str]:
    """删除已入库数据文件 + 级联清理三张大表中同 file_id 关联数据。

    级联范围（事务内原子执行）:
      - vehicle_minute_samples WHERE file_id=X
      - durability_stages     WHERE file_id=X
      - bench_cycle_stats     WHERE file_id=X
      - vehicle_data_files    WHERE id=X

    返回: (是否成功, 说明)
    """
    from sqlalchemy import select, delete
    import time as _t

    t0 = _t.perf_counter()
    try:
        file_id_i = int(file_id)
    except (TypeError, ValueError):
        return False, f"非法 file_id: {file_id}"

    logger.info(
        "[文件删除] 🗑️ 开始: file_id=%d, op_user=%s",
        file_id_i, op_user or "(unknown)",
    )

    def _do() -> Tuple[bool, str]:
        with _engine.connect() as conn:
            # 1) 查原文件,确认存在
            old = conn.execute(
                select(_vehicle_data_files).where(_vehicle_data_files.c.id == file_id_i)
            ).fetchone()
            if old is None:
                return False, f"文件 ID={file_id_i} 不存在"
            old_d = _row_to_dict(old)
            file_name = str(old_d.get("file_name", "?"))
            kind = str(old_d.get("data_kind", ""))

            # 2) 级联删除三张大表（按 file_id）
            cnt_vms = conn.execute(
                delete(_vehicle_minute_samples).where(
                    _vehicle_minute_samples.c.file_id == file_id_i
                )
            ).rowcount or 0

            cnt_ds = conn.execute(
                delete(_durability_stages).where(
                    _durability_stages.c.file_id == file_id_i
                )
            ).rowcount or 0

            cnt_bcs = conn.execute(
                delete(_bench_cycle_stats).where(
                    _bench_cycle_stats.c.file_id == file_id_i
                )
            ).rowcount or 0

            # 3) 删除文件索引本身
            cnt_vdf = conn.execute(
                delete(_vehicle_data_files).where(
                    _vehicle_data_files.c.id == file_id_i
                )
            ).rowcount or 0

            conn.commit()
        return (
            True,
            (
                f"已删除「{file_name}」(类型={kind}): "
                f"文件索引 {cnt_vdf} 条, "
                f"整车分钟 {cnt_vms} 条, "
                f"耐久工步 {cnt_ds} 条, "
                f"台架统计 {cnt_bcs} 条"
            ),
        )

    try:
        ok, msg = _run_with_fallback("db_delete_data_file", _do)
        cost = (_t.perf_counter() - t0) * 1000
        if ok:
            logger.info(
                "[文件删除] ✅ 成功 | file_id=%d | op=%s | %s | 耗时=%.0fms",
                file_id_i, op_user, msg, cost,
            )
        else:
            logger.warning(
                "[文件删除] ⚠️ 被拒绝 | file_id=%d | 原因=%s | 耗时=%.0fms",
                file_id_i, msg, cost,
            )
        return ok, msg
    except Exception as e:
        cost = (_t.perf_counter() - t0) * 1000
        logger.error(
            "[文件删除] ❌ 异常 | file_id=%d | err=%s | 耗时=%.0fms",
            file_id_i, e, cost, exc_info=True,
        )
        return False, f"删除失败: {_extract_error_summary(e)}"


# =========================================================================
# A2 · vehicle_minute_samples 整车分钟级明细 CRUD
# =========================================================================

# 入 A2 表实际会用到的所有列名(企业 9 + 扩展)
_VEHICLE_MINUTE_COLS: List[str] = [
    "FC_CurrOut", "FC_VoltOut", "FC_NetPwrOut",
    "FC_MinCellVoltage", "FC_MinVoltageChannel",
    "FC_AvgCellVoltage", "FC_AvgCellVoltDev",
    "FC_VehicleIsolationR", "FC_RunTime_Hours",
    "FC_VehicleSpd", "FC_VehicleKM",
    "FC_HydCmInstts", "FC_HydCmPerHundred",
    "FC_ErrorCode", "FC_MainSts",
]


def _is_mv_scale(df: pd.DataFrame, col: str) -> bool:
    """启发式判断原始列单位是否是 V(不是企业要求的 mV)。
    只要 col 在 MinCellVoltage / AvgCellVoltage / AvgCellVoltDev 中, 且 max<10, 就判定是 V 制 → ×1000 转成 mV。
    """
    if col not in ("FC_MinCellVoltage", "FC_AvgCellVoltage", "FC_AvgCellVoltDev"):
        return False
    s = pd.to_numeric(df[col], errors="coerce").dropna()
    if len(s) == 0:
        return False
    return bool((s.max() or 0) < 10.0)  # 典型 V 制 3.6~3.9 远小于 10


def db_write_vehicle_minute(
    vehicle_id: str,
    df_vehicle: pd.DataFrame,
    file_id: int = 0,
    upsert: bool = True,
) -> int:
    """把整车 DataFrame (已做 Timestamp dropna/sort) 按 1min resample 后写入 A2。
    企业 3 个电压列如果是 V 制(原始 CSV 常见), 自动转 mV 制(企业口径)。
    返回成功写入行数(按分钟桶)。
    """
    if df_vehicle is None or len(df_vehicle) == 0:
        return 0
    work = df_vehicle.copy()
    if "Timestamp" not in work.columns:
        raise ValueError("db_write_vehicle_minute 输入 DataFrame 必须含 Timestamp 列")
    work["Timestamp"] = pd.to_datetime(work["Timestamp"], errors="coerce")
    work = work.dropna(subset=["Timestamp"]).set_index("Timestamp")
    if len(work) == 0:
        return 0
    # ---------- V → mV 自动转换(启发式) ----------
    for col in ("FC_MinCellVoltage", "FC_AvgCellVoltage", "FC_AvgCellVoltDev"):
        if col in work.columns and _is_mv_scale(work, col):
            work[col] = pd.to_numeric(work[col], errors="coerce") * 1000.0
            logger.info("[落库A2] %s: 检测为 V 制, 自动 ×1000 → mV (企业口径)", col)
    # ---------- 1 分钟 resample(通道 last + 数值 mean) ----------
    agg_map: Dict[str, str] = {}
    for col in work.columns:
        if col == "FC_MinVoltageChannel":
            agg_map[col] = "last"
        elif pd.api.types.is_numeric_dtype(work[col]):
            agg_map[col] = "mean"
        else:
            continue  # 非数值/通道号字符串列不写
    rs = work.resample("1min").agg(agg_map).dropna(how="all")
    if len(rs) == 0:
        logger.warning("[落库A2] resample 后为空,跳过 vehicle=%s", vehicle_id)
        return 0
    rows: List[Dict] = []
    for ts, rec in rs.iterrows():
        minute_str = ts.strftime("%Y-%m-%d %H:%M:00")
        row: Dict[str, Any] = {"vehicle_id": vehicle_id,
                               "minute_ts": minute_str,
                               "file_id": int(file_id) or None}
        for c in _VEHICLE_MINUTE_COLS:
            if c in rec.index and pd.notna(rec[c]):
                row[c] = None if pd.isna(rec[c]) else (
                    int(rec[c]) if c == "FC_MinVoltageChannel" or c == "FC_ErrorCode"
                    or c == "FC_MainSts" else float(rec[c]))
        rows.append(row)
    if not rows:
        return 0

    def _do() -> int:
        inserted_rows = 0
        updated_rows = 0
        conflict_batches = 0
        with _engine.connect() as conn:
            # SQLite/MySQL: 按批次 + 唯一键冲突忽略(INSERT OR IGNORE / INSERT IGNORE)
            # 为了兼容两种后端,这里改用"逐行 ORM upsert by 联合唯一键"太慢 → 分批次 insert,
            # 冲突行由 try/except IntegrityError 走单行 select+upsert
            from sqlalchemy import select
            from sqlalchemy.exc import IntegrityError
            BATCH = 300
            total_batches = (len(rows) + BATCH - 1) // BATCH
            logger.info("[落库A2] 写入批次 总桶=%d 批次=%d 每批≤%d (按车+分钟唯一键去重)",
                        len(rows), total_batches, BATCH)
            for i in range(0, len(rows), BATCH):
                batch = rows[i:i + BATCH]
                batch_no = i // BATCH + 1
                try:
                    conn.execute(_vehicle_minute_samples.insert(), batch)
                    inserted_rows += len(batch)
                except IntegrityError:
                    # 命中唯一键(同车同分钟),退回逐行处理
                    conflict_batches += 1
                    conn.rollback()
                    batch_ins = 0
                    batch_upd = 0
                    for r in batch:
                        sel = conn.execute(
                            select(_vehicle_minute_samples).where(
                                _vehicle_minute_samples.c.vehicle_id == r["vehicle_id"],
                                _vehicle_minute_samples.c.minute_ts == r["minute_ts"],
                            )
                        ).fetchone()
                        if sel is None:
                            conn.execute(_vehicle_minute_samples.insert().values(**r))
                            inserted_rows += 1
                            batch_ins += 1
                        elif upsert:
                            upd = {k: v for k, v in r.items()
                                   if k not in ("vehicle_id", "minute_ts") and v is not None}
                            if upd:
                                conn.execute(
                                    _vehicle_minute_samples.update().where(
                                        _vehicle_minute_samples.c.vehicle_id == r["vehicle_id"],
                                        _vehicle_minute_samples.c.minute_ts == r["minute_ts"],
                                    ).values(**upd)
                                )
                                updated_rows += 1
                                batch_upd += 1
                    logger.debug(
                        "[落库A2] 批次#%02d/%d 冲突降级: batch_rows=%d → 新插入=%d 更新=%d",
                        batch_no, total_batches, len(batch), batch_ins, batch_upd)
            conn.commit()
        logger.info(
            "[落库A2] 写入批次完成 总桶=%d 新插入=%d 更新=%d 冲突降级批次=%d/%d",
            len(rows), inserted_rows, updated_rows, conflict_batches, total_batches,
        )
        return inserted_rows + updated_rows

    t0 = time.perf_counter()
    try:
        cnt = _run_with_fallback("db_write_vehicle_minute", _do)
        logger.info(
            "[落库A2] ✅ vehicle=%s 原始行=%d → 分钟桶=%d | 成功写入=%d | 耗时=%.1fs",
            vehicle_id, len(df_vehicle), len(rows), cnt,
            (time.perf_counter() - t0))
        return int(cnt)
    except Exception as e:
        logger.error("[落库A2] ❌ vehicle=%s 失败: %s", vehicle_id, e, exc_info=True)
        raise


def db_load_vehicle_minute(
    vehicle_id: str,
    start_dt: Optional[datetime] = None,
    end_dt: Optional[datetime] = None,
) -> pd.DataFrame:
    """把 A2 中某车某时间范围拉回 DataFrame(Timestamp 列, 跟 data[vehicle_id] 对齐)。"""
    from sqlalchemy import select
    cols = ["vehicle_id", "minute_ts", "file_id", *_VEHICLE_MINUTE_COLS]

    def _do() -> pd.DataFrame:
        stmt = select(*[_vehicle_minute_samples.c[c] for c in cols]) \
            .where(_vehicle_minute_samples.c.vehicle_id == vehicle_id)
        if start_dt is not None:
            stmt = stmt.where(
                _vehicle_minute_samples.c.minute_ts
                >= start_dt.strftime("%Y-%m-%d %H:%M:%S"))
        if end_dt is not None:
            stmt = stmt.where(
                _vehicle_minute_samples.c.minute_ts
                <= end_dt.strftime("%Y-%m-%d %H:%M:%S"))
        stmt = stmt.order_by(_vehicle_minute_samples.c.minute_ts)
        with _engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame([dict(r._mapping) for r in rows])
        df = df.rename(columns={"minute_ts": "Timestamp"})
        df["Timestamp"] = pd.to_datetime(df["Timestamp"], errors="coerce")
        df = df.dropna(subset=["Timestamp"])
        return df.reset_index(drop=True)

    t0 = time.perf_counter()
    try:
        df = _run_with_fallback("db_load_vehicle_minute", _do)
        if len(df):
            ts_min = df["Timestamp"].min()
            ts_max = df["Timestamp"].max()
            dur_h = (ts_max - ts_min).total_seconds() / 3600.0 if len(df) > 1 else 0.0
            cols_present = [c for c in _VEHICLE_MINUTE_COLS if c in df.columns]
            logger.info(
                "[落库A2] ⬇ 回拉 vehicle=%s rows=%d cols(企业)=%d/%d "
                "时间范围=%s ~ %s (跨度≈%.1fh) | 耗时=%.1fms",
                vehicle_id, len(df), len(cols_present), len(_VEHICLE_MINUTE_COLS),
                ts_min.strftime("%Y-%m-%d %H:%M"),
                ts_max.strftime("%Y-%m-%d %H:%M"),
                dur_h, (time.perf_counter() - t0) * 1000,
            )
        else:
            logger.info("[落库A2] ⬇ 回拉 vehicle=%s 无数据(空表) | 耗时=%.1fms",
                        vehicle_id, (time.perf_counter() - t0) * 1000)
        return df
    except Exception as e:
        logger.warning("[落库A2] ⬇ 回拉 vehicle=%s 失败 err=%s", vehicle_id, e, exc_info=True)
        return pd.DataFrame()


def db_load_vehicle_minute_preview(vehicle_id: str, limit: int = 100) -> pd.DataFrame:
    """轻量版:只取最近 N 条分钟数据,用于上传历史 Tab 快速预览。"""
    from sqlalchemy import select

    def _do() -> pd.DataFrame:
        cols = ["vehicle_id", "minute_ts", "file_id", *_VEHICLE_MINUTE_COLS]
        stmt = (
            select(*[_vehicle_minute_samples.c[c] for c in cols])
            .where(_vehicle_minute_samples.c.vehicle_id == vehicle_id)
            .order_by(_vehicle_minute_samples.c.minute_ts.desc())
            .limit(limit)
        )
        with _engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame([dict(r._mapping) for r in rows])
        df = df.rename(columns={"minute_ts": "Timestamp"})
        df["Timestamp"] = pd.to_datetime(df["Timestamp"], errors="coerce")
        df = df.dropna(subset=["Timestamp"])
        return df.sort_values("Timestamp").reset_index(drop=True)

    try:
        return _run_with_fallback("db_load_vehicle_minute_preview", _do)
    except Exception as e:
        logger.warning("[上传历史] 预览 vehicle=%s 失败: %s", vehicle_id, e)
        return pd.DataFrame()


def db_list_vehicles_in_db() -> List[Dict[str, Any]]:
    """返回 A2 表中所有 (vehicle_id, 最早时间, 最晚时间, 桶数) 汇总,给侧边栏回填车用。"""
    from sqlalchemy import func, select
    t = _vehicle_minute_samples

    def _do() -> List[Dict]:
        stmt = (select(t.c.vehicle_id,
                       func.min(t.c.minute_ts).label("time_min"),
                       func.max(t.c.minute_ts).label("time_max"),
                       func.count().label("n_buckets"))
                .group_by(t.c.vehicle_id)
                .order_by(t.c.vehicle_id))
        with _engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [dict(r._mapping) for r in rows]

    t0 = time.perf_counter()
    try:
        out = _run_with_fallback("db_list_vehicles_in_db", _do)
        total_buckets = sum(int(x.get("n_buckets") or 0) for x in out)
        ids = [str(x.get("vehicle_id")) for x in out]
        logger.info(
            "[落库A2] 🚗 侧边栏车列表 车辆=%d 总分钟桶=%d 车ID=%s | 耗时=%.1fms",
            len(out), total_buckets, ids, (time.perf_counter() - t0) * 1000,
        )
        return out
    except Exception as e:
        logger.warning("[落库A2] 🚗 侧边栏车列表查询失败 err=%s", e, exc_info=True)
        return []


# =========================================================================
# B1 · durability_stages 耐久工步 CRUD
# =========================================================================

# docx 中文列名 → B1 英文字段名 映射
_DUR_COL_MAP: Dict[str, str] = {
    "目标功率(kW)": "target_power_kw",
    "湿度": "humidity_pct",
    "温度": "temperature_c",
    "净输出功率(kW)": "net_power_kw",
    "电堆电流(A)": "stack_current_a",
    "平均单体电压(V)": "avg_cell_voltage_v",
    "离均差": "avg_voltage_deviation_v",
    "空压机功耗(kW)": "compressor_power_kw",
    "水泵功耗(kW)": "pump_power_kw",
    "冷却水入口温度(℃)": "coolant_in_c",
    "冷却水出口温度(℃)": "coolant_out_c",
    "HFR": "hfr",
    "LFR": "lfr",
    "电压方差": "voltage_variance",
}
_DUR_FIXED_COLS = {"stage", "stage_start_h", "step_idx", "file"}


def db_write_durability_stages(
    df_docx: pd.DataFrame,
    file_id: int = 0,
    sample_name: str = "",
    raw_file_name: str = "",
) -> int:
    """把 load_durability_docx 产出的 docx 宽表落 B1。去重: file_id + stage + step_idx。
    返回实际写入的行数(新插入 + 更新)。
    """
    if df_docx is None or len(df_docx) == 0:
        return 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 规范化 docx 列 → 英文字段
    rows: List[Dict] = []
    for _, rec in df_docx.iterrows():
        base: Dict[str, Any] = {
            "file_id": int(file_id) or None,
            "sample_name": sample_name,
            "stage": str(rec.get("stage", "")) if pd.notna(rec.get("stage")) else "",
            "stage_start_h": None if pd.isna(rec.get("stage_start_h"))
                              else float(rec["stage_start_h"]),
            "step_idx": None if pd.isna(rec.get("step_idx"))
                         else int(rec["step_idx"]),
            "raw_file_name": str(rec.get("file", raw_file_name) or raw_file_name),
            "uploaded_at": now,
        }
        for zh_col, en_col in _DUR_COL_MAP.items():
            if zh_col in rec.index and pd.notna(rec[zh_col]):
                try:
                    base[en_col] = float(rec[zh_col])
                except Exception:
                    base[en_col] = None
        # 唯一键缺一不可: file_id 未知时退化为 (sample_name, stage, step_idx)
        rows.append(base)

    def _do() -> int:
        from sqlalchemy import select
        from sqlalchemy.exc import IntegrityError
        new_ins = 0
        new_upd = 0
        with _engine.connect() as conn:
            logger.info("[落库B1] 开始逐行 upsert 共 %d 行 (去重键: file_id/stage+step_idx)",
                        len(rows))
            for r in rows:
                # 构造联合唯一条件:file_id 有就用,没有就退 (sample_name+stage+step_idx)
                if r.get("file_id"):
                    conds = [_durability_stages.c.file_id == r["file_id"],
                             _durability_stages.c.stage == r["stage"],
                             _durability_stages.c.step_idx == r["step_idx"]]
                else:
                    conds = [_durability_stages.c.sample_name == r["sample_name"],
                             _durability_stages.c.stage == r["stage"],
                             _durability_stages.c.step_idx == r["step_idx"]]
                exist = conn.execute(select(_durability_stages).where(*conds)).fetchone()
                if exist is None:
                    try:
                        conn.execute(_durability_stages.insert().values(**r))
                        new_ins += 1
                    except IntegrityError:
                        pass  # 并发场景跳过
                else:
                    upd = {k: v for k, v in r.items()
                           if v is not None and k not in ("file_id", "stage", "step_idx",
                                                          "sample_name")}
                    if upd:
                        conn.execute(
                            _durability_stages.update().where(*conds).values(**upd))
                        new_upd += 1
            conn.commit()
        logger.info("[落库B1] 逐行 upsert 完成 新插入=%d 更新=%d 合计=%d",
                    new_ins, new_upd, new_ins + new_upd)
        return new_ins + new_upd

    try:
        cnt = _run_with_fallback("db_write_durability_stages", _do)
        logger.info(
            "[落库B1] ✅ sample=%s raw=%s 输入行=%d 实际写入=%d fid=%s",
            sample_name, raw_file_name or "(空)", len(rows), cnt, file_id)
        return int(cnt)
    except Exception as e:
        logger.error("[落库B1] ❌ 失败 sample=%s raw=%s err=%s", sample_name, raw_file_name, e, exc_info=True)
        raise


def db_load_durability_stages(sample_name: Optional[str] = None) -> pd.DataFrame:
    """把 B1 拉回成与 load_durability_docx 近似对齐的宽表(中文列名 + stage/stage_start_h/step_idx/file)。
    UI 端直接替换 dur_df。
    """
    from sqlalchemy import select
    t = _durability_stages

    def _do() -> pd.DataFrame:
        from sqlalchemy import case
        stmt = select(t)
        if sample_name:
            stmt = stmt.where(t.c.sample_name == sample_name)
        # MySQL 不支持 NULLS LAST 语法, 用 CASE WHEN IS NULL 做跨库兼容
        stage_start_order = case((t.c.stage_start_h.is_(None), 1), else_=0)
        step_idx_order = case((t.c.step_idx.is_(None), 1), else_=0)
        stmt = stmt.order_by(stage_start_order, t.c.stage_start_h.asc(),
                             step_idx_order, t.c.step_idx.asc())
        with _engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        if not rows:
            return pd.DataFrame()
        tmp = pd.DataFrame([dict(r._mapping) for r in rows])
        # 英文 → docx 原文中文列名
        out_cols = {}
        for zh, en in _DUR_COL_MAP.items():
            if en in tmp.columns:
                out_cols[zh] = tmp[en]
        for extra in ("stage", "stage_start_h", "step_idx"):
            if extra in tmp.columns:
                out_cols[extra] = tmp[extra]
        if "raw_file_name" in tmp.columns:
            out_cols["file"] = tmp["raw_file_name"].fillna("")
        df = pd.DataFrame(out_cols)
        return df.reset_index(drop=True)

    t0 = time.perf_counter()
    try:
        df = _run_with_fallback("db_load_durability_stages", _do)
        if len(df):
            samples = df["file"].dropna().unique().tolist() if "file" in df.columns else []
            stages = df["stage"].dropna().unique().tolist() if "stage" in df.columns else []
            logger.info(
                "[落库B1] ⬇ 耐久回拉 rows=%d 样品(文件)=%d stage=%s "
                "时间列_stage_start_h_min=%s_max=%s | 耗时=%.1fms",
                len(df), len(samples), stages[:3],
                df["stage_start_h"].min() if "stage_start_h" in df.columns else "N/A",
                df["stage_start_h"].max() if "stage_start_h" in df.columns else "N/A",
                (time.perf_counter() - t0) * 1000,
            )
            if samples:
                logger.debug("[落库B1] 耐久文件列表: %s", samples[:8])
        else:
            logger.info("[落库B1] ⬇ 耐久回拉 空表(B1无数据) | 耗时=%.1fms",
                        (time.perf_counter() - t0) * 1000)
        return df
    except Exception as e:
        logger.warning("[落库B1] ⬇ 耐久回拉失败 err=%s", e, exc_info=True)
        return pd.DataFrame()


# =========================================================================
# C1 · bench_cycle_stats 台架循环聚合 CRUD
# =========================================================================

# C1 表均值列: 信号 → 列名映射(跟台架 _SIGNAL_COLS 对齐)
_BENCH_MEAN_COL_MAP: Dict[str, str] = {
    "FC_AvgCellVoltage": "FC_AvgCellVoltage_mean",
    "FC_AvgCellVoltDev": "FC_AvgCellVoltDev_mean",
    "FC_VARVoltage": "FC_VARVoltage_mean",
    "FC_LFR": "FC_LFR_mean",
    "FC_HFR": "FC_HFR_mean",
    "FC_CurrOut": "FC_CurrOut_mean",
    "FC_VoltOut": "FC_VoltOut_mean",
    "FC_NetPwrOut": "FC_NetPwrOut_mean",
    "FC_MinCellVoltage": "FC_MinCellVoltage_mean",
}
_BENCH_STD_COL_MAP: Dict[str, str] = {
    "FC_AvgCellVoltage": "FC_AvgCellVoltage_std",
    "FC_AvgCellVoltDev": "FC_AvgCellVoltDev_std",
}


def db_write_bench_cycle_stats(
    agg_df: pd.DataFrame,
    *,
    file_id: int = 0,
    rig_id: str = "unknown",
    source_file_name: str = "",
) -> Tuple[int, List[int]]:
    """把 aggregate_durability_stats 的输出(行=cycle×power_point) 写 C1。
    返回 (写入/更新行数, [本次命中/新写入的 id 列表])。
    """
    if agg_df is None or len(agg_df) == 0:
        return 0, []
    # 要求列 cycle_id / power_point / 数据量 / 质量标记
    if "cycle_id" not in agg_df.columns or "power_point" not in agg_df.columns:
        raise ValueError("db_write_bench_cycle_stats 输入缺 cycle_id / power_point 列")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows: List[Dict] = []
    for _, rec in agg_df.iterrows():
        row: Dict[str, Any] = {
            "file_id": int(file_id) or None,
            "rig_id": str(rig_id or "unknown"),
            "cycle_id": int(rec["cycle_id"]),
            "power_point": float(rec["power_point"]),
            "data_count": int(rec.get("数据量")) if pd.notna(rec.get("数据量")) else None,
            "quality": str(rec.get("质量标记") or "正常"),
            "source_file_name": source_file_name,
            "created_at": now,
        }
        # 动态填均值/std 列
        for signal, col in _BENCH_MEAN_COL_MAP.items():
            key_mean = f"{signal}_mean"
            key_std = f"{signal}_std"
            if key_mean in rec.index and pd.notna(rec[key_mean]):
                v = float(rec[key_mean])
                # 统一 mV 制(跟台架 _SIGNAL_COLS 保持一致:台架数据若上传为 V 制,这里不主动转,
                # 因为 aggregate_durability_stats 是按传入 df 口径聚合的。调用方 app.py 先行换算。)
                row[col] = v
            if signal in _BENCH_STD_COL_MAP and key_std in rec.index and pd.notna(rec[key_std]):
                row[_BENCH_STD_COL_MAP[signal]] = float(rec[key_std])
        rows.append(row)

    def _do() -> Tuple[int, List[int]]:
        from sqlalchemy import select
        new_ins = 0
        new_upd = 0
        ids: List[int] = []
        with _engine.connect() as conn:
            logger.info(
                "[落库C1] 开始逐行 upsert 共 %d 行 (去重键: rig_id+cycle_id+power_point)",
                len(rows))
            for r in rows:
                conds = [_bench_cycle_stats.c.rig_id == r["rig_id"],
                         _bench_cycle_stats.c.cycle_id == r["cycle_id"],
                         _bench_cycle_stats.c.power_point == r["power_point"]]
                exist = conn.execute(select(_bench_cycle_stats).where(*conds)).fetchone()
                if exist is None:
                    cur = conn.execute(_bench_cycle_stats.insert().values(**r))
                    new_id = getattr(cur, "lastrowid", None)
                    if new_id is None:
                        exist2 = conn.execute(
                            select(_bench_cycle_stats).where(*conds)).fetchone()
                        if exist2 is not None:
                            new_id = dict(exist2._mapping).get("id")
                    if new_id is not None:
                        ids.append(int(new_id))
                    new_ins += 1
                else:
                    exist_d = dict(exist._mapping)
                    ids.append(int(exist_d.get("id")))
                    upd = {k: v for k, v in r.items()
                           if v is not None and k not in ("rig_id", "cycle_id", "power_point")}
                    if upd:
                        conn.execute(
                            _bench_cycle_stats.update().where(*conds).values(**upd))
                        new_upd += 1
            conn.commit()
        logger.info("[落库C1] 逐行 upsert 完成 新插入=%d 更新=%d 合计=%d ids_count=%d",
                    new_ins, new_upd, new_ins + new_upd, len(ids))
        return new_ins + new_upd, ids

    try:
        cnt, ids = _run_with_fallback("db_write_bench_cycle_stats", _do)
        logger.info(
            "[落库C1] ✅ rig=%s src=%s rows=%d 写入/更新=%d,ids=%s",
            rig_id, source_file_name or "(空)", len(rows), cnt,
            f"[{ids[0]},…,{ids[-1]}]" if len(ids) > 3 else ids)
        return cnt, list(ids)
    except Exception as e:
        logger.error("[落库C1] ❌ rig=%s src=%s err=%s", rig_id, source_file_name, e, exc_info=True)
        raise


def db_load_bench_cycle_stats(
    rig_id: Optional[str] = None,
    cycle_from: Optional[int] = None,
    cycle_to: Optional[int] = None,
) -> pd.DataFrame:
    """把 C1 拉回成与 aggregate_durability_stats 近似对齐的长表(列含 <sig>_mean / <sig>_std / 质量标记/数据量)。
    台架 Tab 能直接用。
    """
    from sqlalchemy import select
    t = _bench_cycle_stats

    def _do() -> pd.DataFrame:
        stmt = select(t)
        if rig_id:
            stmt = stmt.where(t.c.rig_id == rig_id)
        if cycle_from is not None:
            stmt = stmt.where(t.c.cycle_id >= int(cycle_from))
        if cycle_to is not None:
            stmt = stmt.where(t.c.cycle_id <= int(cycle_to))
        stmt = stmt.order_by(t.c.cycle_id, t.c.power_point)
        with _engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        if not rows:
            return pd.DataFrame()
        tmp = pd.DataFrame([dict(r._mapping) for r in rows])
        # 列名映射回台架聚合格式
        out = pd.DataFrame({
            "cycle_id": tmp["cycle_id"].astype(int),
            "power_point": tmp["power_point"].astype(float),
            "数据量": tmp.get("data_count"),
            "质量标记": tmp.get("quality"),
        })
        for sig, col in _BENCH_MEAN_COL_MAP.items():
            if col in tmp.columns:
                out[f"{sig}_mean"] = tmp[col]
        for sig, col in _BENCH_STD_COL_MAP.items():
            if col in tmp.columns:
                out[f"{sig}_std"] = tmp[col]
        return out.reset_index(drop=True)

    t0 = time.perf_counter()
    try:
        df = _run_with_fallback("db_load_bench_cycle_stats", _do)
        if len(df):
            c_min = int(df["cycle_id"].min())
            c_max = int(df["cycle_id"].max())
            pp_count = df["power_point"].nunique()
            sig_cols_present = [c for c in list(_BENCH_MEAN_COL_MAP.values())
                                if c.replace("_mean", "_mean") in df.columns]
            logger.info(
                "[落库C1] ⬇ 台架回拉 rows=%d cycle范围=%d~%d 功率点=%d "
                "filter(rig=%s,cycle_%s~%s) 聚合信号列=%d/%d | 耗时=%.1fms",
                len(df), c_min, c_max, pp_count,
                rig_id or "(全部)",
                cycle_from if cycle_from is not None else "-∞",
                cycle_to if cycle_to is not None else "+∞",
                len(sig_cols_present), len(_BENCH_MEAN_COL_MAP),
                (time.perf_counter() - t0) * 1000,
            )
        else:
            logger.info(
                "[落库C1] ⬇ 台架回拉 空表(C1无数据) filter(rig=%s,cycle_%s~%s) | 耗时=%.1fms",
                rig_id or "(全部)",
                cycle_from if cycle_from is not None else "-∞",
                cycle_to if cycle_to is not None else "+∞",
                (time.perf_counter() - t0) * 1000)
        return df
    except Exception as e:
        logger.warning("[落库C1] ⬇ 台架回拉失败 err=%s", e, exc_info=True)
        return pd.DataFrame()


def db_bench_ids_by_event(event: Dict) -> List[int]:
    """根据预警 event 字典(含 cycle_id+power_point) 查 C1 里匹配的 id, 便于回写 alert_events.bench_cycle_id。"""
    from sqlalchemy import select
    if not event:
        return []
    cid = event.get("cycle_id")
    pp = event.get("power_point")
    if cid is None or pp is None:
        return []

    def _do() -> List[int]:
        stmt = select(_bench_cycle_stats.c.id).where(
            _bench_cycle_stats.c.cycle_id == int(cid),
            _bench_cycle_stats.c.power_point == float(pp),
        )
        with _engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [int(r[0]) for r in rows]

    try:
        return _run_with_fallback("db_bench_ids_by_event", _do)
    except Exception as e:
        logger.warning("db_bench_ids_by_event 失败 cid=%s pp=%s err=%s", cid, pp, e)
        return []


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


def test_mysql_connection() -> dict:
    """测试 MySQL 连接并返回诊断结果。
    
    Returns:
        dict: {
            'success': bool,
            'latency_ms': float,  # 仅成功时有
            'error': str,         # 仅失败时有
            'suggestion': str,    # 仅失败时有
        }
    """
    import time as _time
    result = {"success": False}
    
    cfg = {k: os.getenv(k, "").strip() for k in 
           ["DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME"]}
    
    # 检查配置是否完整
    missing = [k for k, v in cfg.items() if not v]
    if missing:
        result["error"] = f"缺少配置项: {', '.join(missing)}"
        result["suggestion"] = "请在 Streamlit Cloud Secrets 中添加完整的 DB_HOST/DB_PORT/DB_USER/DB_PASSWORD/DB_NAME"
        return result
    
    # 测试连接
    t0 = _time.perf_counter()
    try:
        import pymysql
        conn = pymysql.connect(
            host=cfg["DB_HOST"],
            port=int(cfg["DB_PORT"]),
            user=cfg["DB_USER"],
            password=cfg["DB_PASSWORD"],
            database=cfg["DB_NAME"],
            connect_timeout=10,
            read_timeout=5,
            write_timeout=5,
            charset="utf8mb4",
        )
        latency = (_time.perf_counter() - t0) * 1000
        
        # 执行一个简单查询验证
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        
        conn.close()
        result["success"] = True
        result["latency_ms"] = latency
        logger.info("[MySQL测试] 连接成功, 耗时: %.1fms", latency)
        
    except pymysql.err.OperationalError as e:
        latency = (_time.perf_counter() - t0) * 1000
        err_code = e.args[0] if e.args else "unknown"
        err_msg = str(e)
        
        if err_code == 1045:  # Access denied
            result["error"] = f"访问被拒绝 (code=1045): 用户名或密码错误"
            result["suggestion"] = "请检查 DB_USER 和 DB_PASSWORD 是否正确"
        elif err_code == 2003:  # Can't connect
            result["error"] = f"无法连接到 MySQL 服务器 (code=2003): 主机地址或端口错误"
            result["suggestion"] = "请检查 DB_HOST 和 DB_PORT 是否正确，以及网络是否可达"
        elif err_code == 2006:  # MySQL server has gone away
            result["error"] = f"MySQL 服务器已断开 (code=2006)"
            result["suggestion"] = "可能是网络不稳定或服务器过载，请稍后重试"
        elif err_code == 1049:  # Unknown database
            result["error"] = f"数据库不存在 (code=1049): {cfg['DB_NAME']}"
            result["suggestion"] = "请在腾讯云控制台创建数据库，或修改 DB_NAME"
        else:
            result["error"] = f"MySQL 错误 (code={err_code}): {err_msg}"
            result["suggestion"] = "请检查配置是否正确"
        
        logger.error("[MySQL测试] 连接失败: %s", result["error"])
        
    except Exception as e:
        result["error"] = f"未知错误: {str(e)}"
        result["suggestion"] = "请检查网络连接或联系管理员"
        logger.error("[MySQL测试] 未知错误: %s", str(e))
    
    return result


def render_streamlit_db_status(
    container,  # st.sidebar 或任意 st.container
    position: str = "sidebar",
) -> None:
    """给 Streamlit 页面用的 DB 状态卡片 + 已入库文件列表。

    - 显示数据库状态(不暴露具体配置)
    - 显示已入库的数据文件列表
    """
    import streamlit as _st
    
    with container:
        if position == "sidebar":
            _st.divider()
            _st.subheader("🗄️ 数据存储")
        
        # 数据库状态(不显示具体配置)
        info = get_db_backend_info()
        backend = info["backend"]
        
        if "MySQL" in backend:
            _st.success("✅ 已连接 MySQL (腾讯云)")
        else:
            note = info.get("note", "")
            if note:
                _st.error(f"⚠️ MySQL 不可用, 已降级到 SQLite")
            else:
                _st.warning("⚠️ 使用本地 SQLite (重启会丢失数据)")
        
        # 显示已入库文件列表
        _st.markdown("**📁 已入库数据文件**")
        try:
            files = db_list_data_files()
            if files:
                # 统计摘要
                summary = db_get_upload_summary()
                total = summary.get('total_files', len(files))
                total_rows = summary.get('total_rows', 0)
                _st.caption(f"共 {total} 个文件, {total_rows:,} 行数据")
                
                # 显示最新10个文件 (每行带操作: 删除 / 重命名)
                recent_files = files[:10]
                for f in recent_files:
                    file_id = f.get('id', 0)
                    fname = f.get('filename', 'unknown')
                    vehicle = f.get('vehicle_id', '')
                    rows = f.get('row_count', 0)
                    uploaded = f.get('uploaded_at', '')
                    if uploaded:
                        try:
                            from datetime import datetime
                            if isinstance(uploaded, datetime):
                                time_str = uploaded.strftime('%Y-%m-%d %H:%M')
                            else:
                                dt = datetime.fromisoformat(str(uploaded))
                                time_str = dt.strftime('%Y-%m-%d %H:%M')
                        except:
                            time_str = str(uploaded)[:16]
                    else:
                        time_str = ''
                    
                    kind = f.get('data_kind', '')
                    kind_icon = {'整车': '🚗', '耐久': '⚙️', '台架': '🔬'}.get(kind, '📄')
                    safe_key = f"dsop_{file_id}"
                    
                    with _st.container(border=True):
                        info_col, op_col = _st.columns([4, 1])
                        with info_col:
                            if vehicle:
                                _st.markdown(f"{kind_icon} **{fname}** | {vehicle} | {rows}行 | {time_str}")
                            else:
                                _st.markdown(f"{kind_icon} **{fname}** | {rows}行 | {time_str}")
                        with op_col:
                            op = _st.selectbox(
                                "操作", ["", "✏️重命名", "🗑️删除"],
                                key=safe_key, label_visibility="collapsed"
                            )
                        
                        if op == "✏️重命名":
                            new_name = _st.text_input(
                                "新文件名", value=fname,
                                key=f"dsrn_{file_id}"
                            )
                            if _st.button("✅ 确认重命名", key=f"dsrnbtn_{file_id}", use_container_width=True):
                                ok, msg = db_rename_data_file(file_id, new_name)
                                if ok:
                                    _st.success(msg)
                                    _st.rerun()
                                else:
                                    _st.error(msg)
                        
                        elif op == "🗑️删除":
                            confirm = _st.checkbox(
                                f"确认删除 `{fname}`?",
                                key=f"dsdelchk_{file_id}"
                            )
                            if _st.button("🗑️ 执行删除", key=f"dsdelbtn_{file_id}", 
                                         type="primary", use_container_width=True,
                                         disabled=not confirm):
                                ok, msg = db_delete_data_file(file_id)
                                if ok:
                                    _st.success(msg)
                                    _st.rerun()
                                else:
                                    _st.error(msg)
                
                if total > 10:
                    _st.caption(f"... 还有 {total-10} 个文件")
            else:
                _st.info("暂无已入库的数据文件, 请上传数据")
        except Exception as e:
            _st.caption(f"加载文件列表失败: {str(e)[:50]}")


# ---------- 辅助解析: 对外暴露 parse_csv_filename (供 BranchManager._persist_business_file 复用) ----------

def _parse_csv_filename(name: str):
    """解析 CSV 文件名,返回 vehicle/start_ts/end_ts/... 或 None。

    这是 src.data_loader.parse_csv_filename 的一个安全重定向:
    优先用 src.data_loader, 避免两套正则不一致; 若导入失败则返回 None。
    """
    try:
        from src.data_loader import parse_csv_filename as _p
        return _p(name)
    except Exception as e:
        logger.debug("_parse_csv_filename 重定向失败: %s", e)
        return None


# ---------- Branch 管理一致性 CRUD: branch_file_snapshots / branches ----------

def db_sync_branch_snapshot(
    branch_name: str,
    branch_files: List[Dict],
    note: str = "",
) -> Tuple[bool, str]:
    """把 branch_files 列表全量同步写入 branch_file_snapshots 表。

    策略（可安全降级 SQLite）:
      1. 先把同 branch_name 下 旧记录 的 status 全部标成 'deleted'
      2. 然后对 branch_files 每条做 upsert: 存在(hash+path)→unchanged; 新建→new; 修改→modified
      3. 最后把仍 'deleted' 但这次又在列表里的记录回滚为 unchanged
      4. 若 branches 表无此分支, 自动插入一条占位记录

    返回: (ok, 摘要)
    """
    from sqlalchemy import select, and_, update
    import time
    t0 = time.perf_counter()
    if not branch_name:
        return False, "branch_name 为空"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    logger.info("[分支快照同步] 开始: branch=%s, files=%d, note=%s",
                branch_name, len(branch_files), note or "(无)")

    try:
        def _do() -> Tuple[bool, str]:
            with _engine.connect() as conn:
                # --- 1) branches 自动建占位（如果没有） ---
                br_exists = conn.execute(
                    select(_branches.c.id).where(_branches.c.name == branch_name)
                ).fetchone()
                branch_id = None
                if br_exists:
                    branch_id = br_exists[0] if isinstance(br_exists, tuple) else br_exists.id
                else:
                    import hashlib
                    branch_id = hashlib.md5(branch_name.encode("utf-8")).hexdigest()[:32]
                    conn.execute(_branches.insert().values(
                        id=branch_id,
                        name=branch_name,
                        description="BranchManager 自动创建",
                        is_active=True,
                        parent_branch_id=None,
                        created_at=now,
                        updated_at=now,
                        file_count=len(branch_files),
                        total_size=sum(f.get("size", 0) for f in branch_files),
                    ))
                    logger.info("[分支快照同步] 新建branches占位: id=%s, name=%s", branch_id, branch_name)

                # --- 2) 把该 branch 旧快照先设为 "可能已删除" ---
                conn.execute(update(_branch_file_snapshots)
                    .where(_branch_file_snapshots.c.branch_id == branch_id)
                    .values(status="deleted", change_time=now))

                # --- 3) 遍历当前文件, 逐个 upsert ---
                new_count = 0
                mod_count = 0
                keep_count = 0
                file_total_size = 0
                for f in branch_files:
                    fpath = str(f.get("path", ""))
                    fname = str(f.get("name", Path(fpath).name))
                    fhash = str(f.get("hash", ""))
                    fsize = float(f.get("size", 0))
                    ftype = str(f.get("type", ""))
                    is_valid = bool(f.get("is_valid", True))
                    file_total_size += fsize

                    # 查找同 branch_id + 同 file_path 的旧记录
                    old_row = conn.execute(
                        select(_branch_file_snapshots).where(and_(
                            _branch_file_snapshots.c.branch_id == branch_id,
                            _branch_file_snapshots.c.file_path == fpath,
                        ))
                    ).fetchone()

                    if old_row is None:
                        # 全新
                        ins_vals = dict(
                            branch_id=branch_id,
                            file_path=fpath,
                            file_name=fname,
                            file_hash=fhash,
                            file_size=fsize,
                            file_type=ftype,
                            data_kind="",      # 由 _persist_business_file 另写 vehicle_data_files
                            vehicle_id="",
                            is_valid=is_valid,
                            status="new",
                            change_time=now,
                            metadata={
                                "sync_note": note,
                                "modified": f.get("modified", ""),
                            },
                        )
                        conn.execute(_branch_file_snapshots.insert().values(**ins_vals))
                        new_count += 1
                    else:
                        # 旧记录存在: 按 hash 是否变化判断 modified vs unchanged
                        old_hash = ""
                        old_cols = old_row._mapping if hasattr(old_row, "_mapping") else {}
                        for k, v in old_cols.items():
                            if str(k) == "file_hash":
                                old_hash = str(v)
                                break
                        if old_hash == fhash:
                            new_status = "unchanged"
                            keep_count += 1
                        else:
                            new_status = "modified"
                            mod_count += 1
                        conn.execute(update(_branch_file_snapshots)
                            .where(and_(
                                _branch_file_snapshots.c.branch_id == branch_id,
                                _branch_file_snapshots.c.file_path == fpath,
                            ))
                            .values(
                                file_name=fname,
                                file_hash=fhash,
                                file_size=fsize,
                                file_type=ftype,
                                is_valid=is_valid,
                                status=new_status,
                                change_time=now,
                            ))

                # --- 4) 更新 branches 汇总字段 ---
                conn.execute(update(_branches)
                    .where(_branches.c.id == branch_id)
                    .values(
                        updated_at=now,
                        file_count=len(branch_files),
                        total_size=float(file_total_size),
                    ))
                conn.commit()
                cost_ms = (time.perf_counter() - t0) * 1000
                msg = (f"OK: files={len(branch_files)}, new={new_count}, "
                       f"modified={mod_count}, unchanged={keep_count}, size_K={file_total_size/1024:.1f}")
                logger.info(
                    "[分支快照同步] ✅ branch=%s | %s | 耗时=%.0fms",
                    branch_name, msg, cost_ms,
                )
                return True, msg
        return _run_with_fallback("db_sync_branch_snapshot", _do)
    except Exception as e:
        cost_ms = (time.perf_counter() - t0) * 1000
        logger.error("[分支快照同步] ❌ branch=%s, err=%s (%.0fms)",
                     branch_name, str(e), cost_ms, exc_info=True)
        return False, str(e)


def db_get_branch_snapshot_status(branch_name: str) -> Dict:
    """UI 侧查询: 返回某分支的快照统计和最新 20 条记录, 用于展示一致性状态。"""
    from sqlalchemy import select
    result: Dict = {"ok": False, "total": 0, "by_status": {}, "records": [], "error": ""}
    if not branch_name:
        result["error"] = "branch_name 为空"
        return result
    try:
        def _do() -> Dict:
            with _engine.connect() as conn:
                # 先拿 branch_id
                r1 = conn.execute(
                    select(_branches).where(_branches.c.name == branch_name)
                ).fetchone()
                if r1 is None:
                    result["ok"] = True
                    result["error"] = f"分支 '{branch_name}' 还没有写入过快照"
                    return result
                bid = r1.id if hasattr(r1, "id") else r1[0]
                # 计数
                rows = conn.execute(
                    select(_branch_file_snapshots.c.status,
                           _branch_file_snapshots.c.file_size,
                           _branch_file_snapshots.c.file_path,
                           _branch_file_snapshots.c.file_name,
                           _branch_file_snapshots.c.change_time,
                           _branch_file_snapshots.c.file_hash,
                           _branch_file_snapshots.c.is_valid,
                           )
                    .where(_branch_file_snapshots.c.branch_id == bid)
                    .order_by(_branch_file_snapshots.c.change_time.desc())
                ).fetchall()
                by_status: Dict[str, int] = {}
                total_size = 0
                for r in rows:
                    cols = r._mapping if hasattr(r, "_mapping") else {}
                    st_val = str(cols.get("status", ""))
                    by_status[st_val] = by_status.get(st_val, 0) + 1
                    total_size += float(cols.get("file_size", 0) or 0)
                result["ok"] = True
                result["total"] = len(rows)
                result["total_size"] = total_size
                result["by_status"] = by_status
                # 最近 20 条
                records = []
                for r in rows[:20]:
                    cols = r._mapping if hasattr(r, "_mapping") else {}
                    records.append({
                        "file_path": str(cols.get("file_path", "")),
                        "file_name": str(cols.get("file_name", "")),
                        "status": str(cols.get("status", "")),
                        "change_time": str(cols.get("change_time", "")),
                        "is_valid": bool(cols.get("is_valid", True)),
                        "size": float(cols.get("file_size", 0) or 0),
                        "hash": str(cols.get("file_hash", ""))[:16],
                    })
                result["records"] = records
                return result
        return _run_with_fallback("db_get_branch_snapshot_status", _do)
    except Exception as e:
        result["ok"] = False
        result["error"] = str(e)
        logger.error("[分支快照查询] ❌ branch=%s, err=%s", branch_name, e, exc_info=True)
        return result



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
