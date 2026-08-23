"""Streamlit 主入口:整车看板/耐久衰减/多车对比/报告导出。

重构说明(2026-08-23 针对 Streamlit Cloud 冷启动崩溃):
- 11 个 Tab 业务逻辑全部抽成 `_render_tab_*` 渲染函数,顶层 `with tab_xxx:` 只做函数调用。
- 每个渲染函数统一套 `@tab_safe_render` 装饰器:
    - 捕获所有 Exception -> 显示红色错误框 (非全页 Oh no 崩溃)
    - 展开面板里附完整 Traceback, 便于复制给开发。
- 另外修复了 2 处会导致 `KeyError: 'duration'` 的逻辑:
    - `_render_tab_performance` 在 aggregate_segments() 返回空时先判断 columns 再读。
    - `create_performance_figure` 列名用 `run_time_at_mid`, 这里传的也是这个, 不再写字符串 `run_time`。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent))

# 日志配置(必须在其它模块 import 之前完成,以便统一控制格式与级别)
from src.log_config import setup_logging
setup_logging(level=__import__("logging").INFO)
# 可选:同时写日志到文件,便于历史排查
# setup_logging(level=logging.DEBUG, log_file="logs/app.log")

import logging
logger = logging.getLogger(__name__)

# ============================================================
# ✅ 全局生效: 启动就加载 DB 降级机制 (必须在其他业务 import 前完成,
# 这样 Tab 台架预警 / 飞书对接 页面的 DB 操作天然带降级保护)
# ============================================================
from durability import database as _db_module
from durability.database import (
    init_db as _db_init,
    render_streamlit_db_status,
    print_console_db_status,
    get_db_backend_info,
    # ===== 新增:落库写入 A1/B1/C1 =====
    db_upsert_data_file,
    db_write_vehicle_minute,
    db_write_durability_stages,
    db_write_bench_cycle_stats,
    db_count_data_files,
    # ===== 冷启动回填 A2/B1/C1 =====
    db_list_vehicles_in_db,
    db_load_vehicle_minute,
    db_load_vehicle_minute_preview,
    db_load_durability_stages,
    db_load_bench_cycle_stats,
    db_bench_ids_by_event,
    # ===== 上传历史记录 =====
    db_get_upload_summary,
    db_list_data_files_paginated,
    db_list_data_files,
)
# 启动期 DB 初始化 (MySQL 不可达自动降级 SQLite)
_db_init()
# 控制台同步输出一份状态横幅(方便 streamlit run 时看终端)
print_console_db_status("Streamlit 启动 · DB 初始化状态")

# ============================================================
# ✅ 全局预检: 启动期在终端打印一次 车辆目录 + 飞书对接人清单
# ============================================================
from src.metrics import _safe_num
from src.data_quality import classify_risk

def _app_precheck_banner() -> None:
    """Streamlit 启动期, 在终端输出与 run_e2e.py 对齐的预检横幅。

    ⚠️  性能考虑 (Streamlit 每轮 rerun 都会重跑本脚本的顶层代码):
    - 车辆预检只做目录级元数据统计 (目录名 + CSV 数量), 不读任何 CSV 列内容
      → 20 辆车也在毫秒级完成, 每轮 rerun 无压力。
    - 飞书预检是纯 DB 读取 <100ms, 每轮跑也 OK (还可以自动感知 DB 变化)。

    真正的「0 占比 / 风险等级」全量扫描请在 Streamlit 页面的台架预警 Tab,
    或命令行运行 python scan_hyd_zero.py。
    """
    ROOT = Path(__file__).resolve().parent
    CSV_BASE = ROOT / "企业资料包02_氢质氢离" / "02_整车数据处理"
    bar = "─" * 70
    print("\n" + bar)
    print("  Streamlit 启动预检 · 整车目录自动识别 (新增车型 ← 这里自动看到)")
    print(bar)
    if CSV_BASE.exists():
        car_dirs = sorted([d for d in CSV_BASE.iterdir() if d.is_dir()])
        print(f"  扫描目录: {CSV_BASE}")
        print(f"  自动识别车辆数: {len(car_dirs)}")
        header = f"  {'车辆':<10}{'CSV分片数':>12}  ✅ = 已被纳入 run_e2e / 页面下拉菜单"
        print("  " + "-" * (len(header) + 8))
        print(header)
        total_csv = 0
        for car_dir in car_dirs:
            files = sorted(car_dir.glob("*.csv"))
            total_csv += len(files)
            print(f"  {car_dir.name:<10} {len(files):>12}  ✅")
        print("-" * 20)
        print(f"  合计 CSV 分片: {total_csv}")
    else:
        print(f"  ⚠ 内置 CSV 目录不存在: {CSV_BASE}")
        print("     可在 Streamlit 侧边栏选择「上传文件」模式导入。")

    # ---------- 飞书联系人预检 ----------
    try:
        from durability.feishu_contacts import (
            list_contacts as _feishu_list,
            detect_all_credentials_status as _detect_creds,
            credentials_status_text as _creds_text,
        )
        contacts = _feishu_list()
        info = get_db_backend_info()
        print(bar)
        print("  Streamlit 启动预检 · 飞书人员对接 (新增联系人 ← 这里自动看到)")
        print(bar)
        print(f"  存储后端: {info.get('backend_display', info.get('backend', 'N/A'))}")
        if info.get("backend") == "mysql":
            print(f"  Host: {info.get('host')}:{info.get('port')}  DB: {info.get('dbname')}  User: {info.get('user')}")
            print(f"  🔁 降级: MySQL 外网断开会自动切 SQLite (终端/日志会打印 [DB 降级] 横幅)")
        print("-" * 30)
        if not contacts:
            print("  ⚠ 还没有任何飞书联系人 → 进入 📡 飞书人员对接 Tab 新增")
        else:
            verified_cnt = sum(1 for c in contacts if c.get("verified"))
            enabled_cnt = sum(1 for c in contacts if c.get("enabled", True))
            print(f"  总联系人: {len(contacts)} | 启用: {enabled_cnt} | 已验证(飞书推送绿灯): {verified_cnt}")
            # ✅ 自动检测密钥是否过期 (Streamlit Cloud / 本机启动都自动打一次)
            # ⚠️  用线程池强制超时 8 秒 (Cloud 外网到飞书 API 可能慢, 不能卡启动)
            print("-" * 10 + " 🔑 密钥预检开始 (超时8秒自动跳过) " + "-" * 20)
            _key_result = None
            try:
                import concurrent.futures as _fut
                with _fut.ThreadPoolExecutor(max_workers=1) as _pool:
                    _future = _pool.submit(_detect_creds)
                    _key_result = _future.result(timeout=8)
            except _fut.TimeoutError:
                print("  ⏱ 密钥预检超时(>8秒,外网到飞书慢) → 已跳过,可进入飞书人员对接Tab手动触发")
                logger.warning("[Streamlit启动预检·密钥] 超时>8秒自动跳过(Cloud外网到飞书慢)")
                _key_result = None
            except Exception as e:
                print(f"  ⚠ 密钥预检失败(不影响页面主功能): {e}")
                logger.warning("[Streamlit启动预检·密钥] 失败: %s", e, exc_info=True)
                _key_result = None
            if _key_result is not None:
                result = _key_result
                summary = result.get("summary", {})
                age = result.get("checked_seconds_ago", 0)
                logger.info(
                    "[Streamlit启动预检·密钥] cache_hit=%s age_s=%.1f app_groups=%d 总耗时=%dms summary=%s",
                    result.get("cache_hit", False), age,
                    len(result.get("app_group_results", [])),
                    int(result.get("total_elapsed_ms", 0)), summary,
                )
                print(
                    f"  📊 汇总 | 有效={summary.get('valid',0)} 失效={summary.get('invalid',0)} "
                    f"超时={summary.get('timeout',0)} 网络错={summary.get('network_err',0)} "
                    f"跳过禁用={summary.get('skipped_disabled',0)}"
                )
                per = result.get("per_contact", {})
                for c in contacts:
                    cid = c.get("id")
                    name = c.get("name", "")
                    app_id = c.get("app_id", "")
                    en = "✅" if c.get("enabled", True) else "🔲"
                    vf = "✅" if c.get("verified") else "🔲"
                    if cid in per:
                        c_info = per[cid]
                        st_line = _creds_text(c_info.get("status"), c_info.get("code"))
                        el_ms = c_info.get("elapsed_ms", 0)
                        oid = c.get("open_id", "") or ""
                        oid_m = oid[:10] + "..." if len(oid) > 10 else oid
                        print(f"    · {name:<10} 启用={en} 验证={vf}  {app_id:<14} open_id={oid_m:<16}   {st_line} ({el_ms:.0f}ms)")
                    else:
                        print(f"    · {name:<10} 启用={en} 验证={vf}  {app_id:<14}  🔑 N/A (跳过)")
                print(f"[密钥巡检] ✅ 完成 (总耗时={int(result.get('total_elapsed_ms',0))}ms, cache_age={age:.1f}s)")
    except Exception as e:
        logger.warning("[Streamlit启动预检] 飞书模块加载失败, 跳过飞书预检: %s", e, exc_info=True)
        print(f"  ⚠ 飞书模块加载失败(不影响页面主功能): {e}")
    print(bar + "\n")


# Streamlit 每轮 rerun 都会重跑模块顶层;这里用全局锁确保「终端预检横幅」只打印一次。
try:
    if not _APP_PRECHECK_DONE:  # type: ignore[name-defined]
        _app_precheck_banner()
except NameError:
    _app_precheck_banner()
finally:
    try:
        _APP_PRECHECK_DONE = True  # noqa: F841
    except Exception:
        pass


from src.data_loader import (
    load_durability_docx,
    load_vehicle_csvs,
    parse_csv_filename,
    peek_docx_structure,
)
from src.metrics import (
    cell_voltage_consistency,
    fault_time_series,
    h2_system,
    power_summary,
    vehicle_overview,
    vehicle_speed_profile,
)
from src.plots import (
    fig_before_after_overlay,
    fig_cell_voltage,
    fig_compare_overlay,
    fig_durability_trend,
    fig_fault_bar,
    fig_power_curve,
    fig_speed_hydrogen,
)
from src.report import build_report_html
from datetime import datetime

from components.filter_bar import render_filter_bar
from components.stats import render_stats
from components.chart import create_figure
from utils.helpers import filter_by_time, resample_data, detect_anomalies, SIGNAL_MAP
from utils.mock_data import generate_mock_data
from components.theme import apply_custom_css
from components.data_quality import render_data_quality
from components.letter_glitch import render_letter_glitch
# 燃电性能统计及预测(Tab8)相关模块
from components.performance_filter import render_performance_filter
from components.performance_chart import create_performance_figure
from performance.steady_state_selector import find_steady_segments
from performance.segment_aggregator import aggregate_segments
from performance.polarization_curve import (
    fit_polarization_curve,
    create_polarization_figure,
)
from performance.degradation_analyzer import (
    analyze_degradation,
    create_degradation_figure,
)
# 绝缘阻值统计及预测(Tab9)相关模块
from insulation.data_processor import process_insulation_data
from insulation.predictor import predict_insulation_trend
from insulation.state_analyzer import (
    analyze_state_distribution,
    create_state_distribution_figure,
)
from components.insulation_filter import render_insulation_filter
from components.insulation_stats import render_insulation_stats
from components.insulation_chart import create_insulation_figure
from insulation.vehicle_comparison import (
    create_vehicle_comparison,
    generate_comparison_table,
)

# Tab 安全装饰器(每个渲染函数统一兜底, 避免 Oh no 全页崩)
from utils.tab_safety import tab_safe_render, apply_tab_safety_globals
apply_tab_safety_globals()


st.set_page_config(
    page_title="设备测试分析助手",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

# 企业级深色科技主题(全局 CSS 注入,仅需调用一次)
apply_custom_css()

# 顶部标题栏 - LetterGlitch 故障风格动画
render_letter_glitch(
    glitch_colors=['#2b4539', '#61dca3', '#61b3dc'],
    glitch_speed=50,
    center_vignette=True,
    outer_vignette=False,
    smooth=True,
    height=120,
)

# 标题文字覆盖层
st.markdown("""
<div style="
    display: flex; justify-content: center; align-items: center;
    padding: 16px 0;
    margin-top: -120px; margin-bottom: 100px;
    position: relative; z-index: 10;
">
    <div style="text-align: center;">
        <div style="
            font-size: 2.2rem; font-weight: 700;
            color: #00D4FF;
            text-shadow: 0 0 20px rgba(0,212,255,0.5), 0 0 40px rgba(0,212,255,0.3);
            letter-spacing: 0.05em;
            font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif;
        ">设备测试分析助手</div>
        <div style="
            font-size: 0.9rem; color: #6B7894; margin-top: 8px;
            letter-spacing: 0.1em;
        ">
            Fuel Cell Vehicle Testing Data Analysis Platform
        </div>
    </div>
</div>
""", unsafe_allow_html=True)

DATA_ROOT = Path(__file__).parent / "企业资料包02_氢质氢离"


# ---------- 缓存数据加载 ----------

@st.cache_data(show_spinner=False)
def load_default_csvs() -> dict[str, pd.DataFrame]:
    """加载内置的 212/345 CSV 数据(首次访问触发)。"""
    logger.info("=== 加载内置 CSV(整车)===")
    base = DATA_ROOT / "02_整车数据处理"
    if not base.exists():
        logger.error("内置 CSV 目录不存在: %s", base)
        return {}
    result: dict[str, pd.DataFrame] = {}
    for car_dir in base.iterdir():
        if not car_dir.is_dir():
            continue
        files = sorted(car_dir.glob("*.csv"))
        if not files:
            logger.warning("子目录无 CSV: %s", car_dir)
            continue
        logger.info(">> 车辆 %s: %d 个 CSV 文件", car_dir.name, len(files))
        df = load_vehicle_csvs([str(f) for f in files])
        if len(df):
            result[car_dir.name] = df
            logger.info(">> %s 加载完成: %d 行", car_dir.name, len(df))
        else:
            logger.warning(">> %s 加载为空", car_dir.name)
    logger.info("=== 内置 CSV 加载结束: %d 辆车 ===", len(result))
    return result


@st.cache_data(show_spinner=False)
def load_default_durability() -> pd.DataFrame:
    """加载内置耐久 docx(长表)。"""
    logger.info("=== 加载内置 docx(耐久)===")
    base = DATA_ROOT / "01_耐久原始数据处理"
    if not base.exists():
        logger.error("内置 docx 目录不存在: %s", base)
        return pd.DataFrame()
    files = sorted(base.glob("*.docx"))
    if not files:
        logger.warning("耐久 docx 目录无文件")
        return pd.DataFrame()
    logger.info(">> 共 %d 个 docx", len(files))
    return load_durability_docx([str(f) for f in files])


# ---------- 侧边栏 ----------

with st.sidebar:
    st.markdown("""
    <div style="padding: 8px 0 12px 0; border-bottom: 1px solid rgba(0,212,255,0.1); margin-bottom: 12px;">
        <div style="font-size: 1.1rem; font-weight: 700; color: #00D4FF;">设备测试分析助手</div>
        <div style="font-size: 0.72rem; color: #6B7894; margin-top: 2px;">氢质氢离 · 燃料电池全流程</div>
    </div>
    """, unsafe_allow_html=True)

    st.subheader("数据来源")
    use_builtin = st.radio(
        "选择数据",
        ["使用内置数据(自动扫描)", "上传文件"],
        index=0,
        label_visibility="collapsed",
    )

    # 数据目录说明(告诉用户如何扔新数据进来)
    with st.expander("💡 如何加入新车辆数据?", expanded=False):
        st.markdown(
            "**方式 1: 放到本地目录(推荐批量数据)**\n\n"
            "把新车辆 CSV 分片放到:\n"
            "```\n"
            f"{(DATA_ROOT / '02_整车数据处理').resolve()}\n"
            "```\n"
            "**每个车辆一个子目录**(以车辆编号命名),目录内放该车的所有 CSV 分片。\n"
            "格式: `<车辆编号>_<起时间>_<止时间>_CH0_<导入时间>.csv`\n\n"
            "放好后刷新本页面即可自动识别。\n\n"
            "**方式 2: 上传文件(推荐临时数据)**\n"
            "选\"上传文件\"后,可拖入多种格式:\n"
            "- CSV 分片 → 合并成整车数据\n"
            "- Word(.doc/.docx) → 自动识别耐久 docx\n"
            "- Excel(.xls/.xlsx) → 表格数据规范化"
        )

    uploaded_files = None
    if use_builtin == "上传文件":
        uploaded_files = st.file_uploader(
            "拖入 CSV / Word / Excel 文件(可多份,可混合)",
            type=["csv", "doc", "docx", "xls", "xlsx"],
            accept_multiple_files=True,
        )

    st.divider()
    st.subheader("时间区间")
    time_range_preset = st.selectbox(
        "选择时间区间",
        ["全部数据", "最近 1 小时", "最近 6 小时", "最近 24 小时",
         "最近 7 天", "自定义区间"],
        index=0,
        help="选\"自定义区间\"后,可在整车看板中精确设置起止时间",
    )

    st.divider()
    st.subheader("燃电看板数据源")
    fc_data_mode = st.radio(
        "新看板数据源",
        ["模拟数据 (mock)", "真实数据", "测试异常CSV", "稳态测试CSV(95A)"],
        index=0,
        key="fc_data_mode",
        help="仅影响「燃电运行看板」Tab;默认 mock 可独立演示",
    )

    st.caption(f"内置数据路径: {DATA_ROOT}")

    # ✅ 侧边栏底部: DB 状态卡片 + 降级醒目警告
    render_streamlit_db_status(st.sidebar)


# ---------- 数据装载 ----------

import io  # 文件上传 BytesIO 处理

data: dict[str, pd.DataFrame] = {}
if use_builtin == "使用内置数据(自动扫描)":
    data = load_default_csvs()


# ============================================================
# 📌 冷启动数据回填(方案 A · SQLite):当内置 data/dur_df/bench_parts 为空时,
#    从 A2(vehicle_minute_samples) / B1(durability_stages) / C1(bench_cycle_stats)
#    自动加载用户之前上传过且已落库的数据, 让 Streamlit Cloud 刷新后也不用重传。
# ============================================================
def _hydrate_from_db(
    data_ref: dict[str, pd.DataFrame],
) -> tuple[pd.DataFrame, list[pd.DataFrame]]:
    """冷启动回填 data / dur_df / bench_parts。
    返回 (hydrated_dur_df, hydrated_bench_parts_list)。
    """
    dur_out = pd.DataFrame()
    bench_out: list[pd.DataFrame] = []
    t0_all = time.perf_counter()
    logger.info("[回填DB] ═══ 冷启动回填开始 ═══ 内置数据车辆数=%d (内置空时走DB)",
                len(data_ref))
    try:
        # ---- 整车: data 空 就去 A2 按 vehicle_id 拉 ----
        if not data_ref:
            vlist = db_list_vehicles_in_db()
            logger.info("[回填DB] 🚗 A2 侧边栏汇总返回 %d 辆车,开始逐车回拉明细", len(vlist))
            for idx, item in enumerate(vlist, 1):
                vid = str(item.get("vehicle_id") or "")
                if not vid:
                    logger.warning("[回填DB] 🚗 A2 第%d条记录 vehicle_id 为空,跳过: %s", idx, item)
                    continue
                vdf = db_load_vehicle_minute(vid)
                if len(vdf):
                    data_ref[vid] = vdf
                    ts_from = vdf["Timestamp"].min().strftime("%Y-%m-%d %H:%M")
                    ts_to = vdf["Timestamp"].max().strftime("%Y-%m-%d %H:%M")
                    logger.info(
                        "[回填DB] 🚗 A2[%d/%d] 车=%s 载入 %d 行 | %s ~ %s",
                        idx, len(vlist), vid, len(vdf), ts_from, ts_to,
                    )
                else:
                    logger.warning(
                        "[回填DB] 🚗 A2[%d/%d] 车=%s 汇总行存在但明细表为空(脏数据?),跳过",
                        idx, len(vlist), vid,
                    )
            if data_ref:
                logger.info("[回填DB] 🚗 A2 完成 载入 %d 车: %s",
                            len(data_ref), list(data_ref.keys()))
            else:
                logger.info("[回填DB] 🚗 A2 完成 无车辆数据(表为空或汇总明细不一致)")
        else:
            logger.info("[回填DB] 🚗 A2 跳过 使用内置扫描数据 %d 车,不覆盖", len(data_ref))
        # ---- 耐久: B1 拉中文列名宽表 ----
        dur_out = db_load_durability_stages()
        if len(dur_out):
            file_set = dur_out["file"].dropna().unique().tolist() if "file" in dur_out.columns else []
            logger.info("[回填DB] 📉 B1 完成 载入 %d 条工步,来源文件=%d 个:%s",
                        len(dur_out), len(file_set), file_set[:5])
        else:
            logger.info("[回填DB] 📉 B1 完成 无耐久工步数据")
        # ---- 台架: C1 拉回 aggregate 格式 df ----
        bench_agg = db_load_bench_cycle_stats()
        if len(bench_agg):
            bench_out = [bench_agg]
            cycles = sorted(bench_agg["cycle_id"].astype(int).unique().tolist())
            rigs = bench_agg.get("rig_id", pd.Series(dtype=str)).dropna().unique().tolist() if "rig_id" in bench_agg.columns else []
            logger.info("[回填DB] 🔬 C1 完成 载入 %d 行 (cycle×功率点),cycle=%s,台架=%s",
                        len(bench_agg), cycles[:6], rigs)
        else:
            logger.info("[回填DB] 🔬 C1 完成 无台架循环数据")
        logger.info("[回填DB] ═══ 冷启动回填完成 ═══ 总耗时=%.1fms",
                    (time.perf_counter() - t0_all) * 1000)
    except Exception as ex:
        logger.error("[回填DB] ═══ 回填失败(不阻塞UI) ═══ err=%s", ex, exc_info=True)
    return dur_out, bench_out


_use_db_hydrate = (use_builtin != "使用内置数据(自动扫描)"
                   or not bool(data))
_hydrated_dur = pd.DataFrame()
_hydrated_bench: list[pd.DataFrame] = []
if _use_db_hydrate:
    _hydrated_dur, _hydrated_bench = _hydrate_from_db(data)
    if _hydrated_bench:
        st.session_state["_bench_parts"] = _hydrated_bench
        st.session_state["_bench_parts_count"] = sum(
            int(len(b)) for b in _hydrated_bench)

# ============================================================
# 📌 通用文件上传处理 —— 严格按后缀优先分类(与三大目录一一对应):
#   .docx          → 100% 耐久工步       (目录 01_耐久原始数据处理)
#   .csv 含 Timestamp → 整车数据         (目录 02_整车数据处理)
#   .csv 无 Timestamp → 台架循环(命中关键词) (目录 03_台架耐久数据)
#   .xlsx/.xls 同 CSV 规则(方便用户两种格式都能传)
# ============================================================
csv_parts: list[pd.DataFrame] = []    # ① 整车:含 Timestamp 的 CSV/Excel
docx_parts: list[pd.DataFrame] = []   # ② 耐久工步:所有 .docx(100% 归入耐久衰减)
xls_parts: list[pd.DataFrame] = []    # ① 整车:含 Timestamp 的 Excel(和 CSV 走同一路由)
bench_parts: list[pd.DataFrame] = []  # ③ 台架循环:无 Timestamp 但含台架关键词

def _is_bench_dataframe(df: pd.DataFrame) -> tuple[bool, list[str]]:
    """无 Timestamp 的表 → 判断是否为台架循环。
    返回 (是否台架, 命中的关键词前4个)。
    """
    cols_lower = {str(c).lower() for c in df.columns}
    bench_hits = [k for k in {"循环", "loop", "cycle", "循环编号", "循环次数", "功率点",
                              "电流密度", "效率", "hydrogen利用率", "氢气利用率", "stack_voltage",
                              "单体平均电压", "系统效率", "net_power", "net_efficiency", "spec_power_density"}
                  if k in cols_lower]
    return len(bench_hits) >= 2, bench_hits[:4]

if uploaded_files:
    import tempfile
    import hashlib
    t_up_start = time.perf_counter()
    logger.info(
        "[上传预检] ═══ 文件上传开始 ═══ 本次共 %d 个文件: %s",
        len(uploaded_files), [f.name for f in uploaded_files],
    )
    # ── 原始文件字节缓存(用于计算 file_hash 去重 + A1 落库) ──
    _veh_raw_bytes: dict[str, bytes] = {}   # file_name → bytes
    _docx_raw_bytes: list[tuple[str, bytes, pd.DataFrame]] = []  # (file_name, bytes, tmp_df)
    _bench_raw_bytes: list[tuple[str, bytes, pd.DataFrame]] = []  # (file_name, bytes, raw_df)

    for f_idx, f in enumerate(uploaded_files, 1):
        suffix = Path(f.name).suffix.lower()
        logger.info("[上传预检] [%d/%d] 处理 %s | suffix=%s | size≈%s bytes",
                    f_idx, len(uploaded_files), f.name, suffix,
                    "(未知-Seekable未读)" if hasattr(f, "size") else "(StreamlitUploadedFile)")
        try:
            # ------------------------------------------------------------
            # ✅ 规则 1: .docx → 100% 耐久工步(走标准解析,不做任何关键词判断)
            # ------------------------------------------------------------
            if suffix in (".doc", ".docx"):
                if suffix == ".doc":
                    logger.warning("[上传预检] [%d/%d] 跳过 .doc 旧版格式: %s",
                                   f_idx, len(uploaded_files), f.name)
                    st.warning(f"{f.name}: 旧版 .doc 格式不受支持,请另存为 .docx 后再上传")
                    continue
                f_bytes = f.read()
                f_hash = hashlib.sha256(f_bytes).hexdigest()[:12]
                logger.info(
                    "[上传预检] [%d/%d] 📉 耐久(docx规则) size=%d byte | sha256=%s… 开始load_durability_docx",
                    f_idx, len(uploaded_files), len(f_bytes), f_hash,
                )
                with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
                    tmp.write(f_bytes)
                    tmp_path = tmp.name
                try:
                    tmp_df = load_durability_docx([tmp_path])
                    if not tmp_df.empty:
                        tmp_df["file"] = f.name
                        docx_parts.append(tmp_df)
                        _docx_raw_bytes.append((f.name, f_bytes, tmp_df))
                        logger.info(
                            "[上传预检] [%d/%d] 📉 耐久解析成功 rows=%d cols=%d | 列名(前8)=%s",
                            f_idx, len(uploaded_files), len(tmp_df), len(tmp_df.columns),
                            list(tmp_df.columns)[:8],
                        )
                        st.info(f"{f.name}: 📉 **耐久工步数据** (.docx 按规则直接归入) → 「耐久衰减」Tab")
                    else:
                        logger.warning(
                            "[上传预检] [%d/%d] 📉 耐久解析后为空表(0 rows),可能 docx 内无工步表格: %s",
                            f_idx, len(uploaded_files), f.name,
                        )
                finally:
                    try:
                        Path(tmp_path).unlink(missing_ok=True)
                    except Exception:
                        pass
                continue

            # ------------------------------------------------------------
            # ✅ 规则 2&3: .csv / .xlsx → 先看有没有 Timestamp(整车),否则看台架关键词
            # ------------------------------------------------------------
            if suffix == ".csv":
                f_bytes = f.read()
                df = pd.read_csv(io.BytesIO(f_bytes))
            elif suffix in (".xls", ".xlsx"):
                f_bytes = f.read()
                df = pd.read_excel(io.BytesIO(f_bytes))
            else:
                logger.warning("[上传预检] [%d/%d] 跳过不支持的格式: %s (suffix=%s)",
                               f_idx, len(uploaded_files), f.name, suffix)
                st.warning(f"{f.name}: 不支持的格式 ({suffix})")
                continue

            f_hash = hashlib.sha256(f_bytes).hexdigest()[:12]
            logger.info(
                "[上传预检] [%d/%d] CSV/Excel解析完成 rows=%d cols=%d size=%dB sha256=%s… 列名(前10)=%s",
                f_idx, len(uploaded_files), len(df), len(df.columns),
                len(f_bytes), f_hash, list(df.columns)[:10],
            )

            if "Timestamp" in df.columns:
                # ----- 有 Timestamp → 整车数据 -----
                ts_before = len(df)
                df["Timestamp"] = pd.to_datetime(df["Timestamp"], errors="coerce")
                ts_invalid = df["Timestamp"].isna().sum()
                ts_min = df["Timestamp"].min()
                ts_max = df["Timestamp"].max()
                if suffix == ".csv":
                    csv_parts.append(df)
                else:
                    xls_parts.append(df)
                _veh_raw_bytes[f.name] = f_bytes
                logger.info(
                    "[上传预检] [%d/%d] 🚗 整车(Timestamp命中) 无效Timestamp=%d/%d "
                    "时间=%s ~ %s → 入整车分片缓存(%s)",
                    f_idx, len(uploaded_files), int(ts_invalid), ts_before,
                    ts_min, ts_max, "csv" if suffix == ".csv" else "xlsx",
                )
                st.info(f"{f.name}: 🚗 **整车数据** (检测到 Timestamp 列) → 整车看板/燃电/性能等 8 个 Tab")
            else:
                # ----- 无 Timestamp → 仅判断台架循环(耐久不可能是 CSV/Excel) -----
                _is_bench, _hits = _is_bench_dataframe(df)
                logger.info(
                    "[上传预检] [%d/%d] 🔍 无Timestamp,台架关键词判断: is_bench=%s hits=%s",
                    f_idx, len(uploaded_files), _is_bench, _hits,
                )
                if _is_bench:
                    if "file" not in df.columns:
                        df["file"] = f.name
                    bench_parts.append(df)
                    _bench_raw_bytes.append((f.name, f_bytes, df))
                    logger.info(
                        "[上传预检] [%d/%d] 🏭 台架(关键词命中) rows=%d → 入台架缓存",
                        f_idx, len(uploaded_files), len(df),
                    )
                    st.info(f"{f.name}: 🏭 **台架循环数据** (命中台架关键词 {_hits}) → 「🔬 台架耐久统计及预警」Tab")
                else:
                    logger.warning(
                        "[上传预检] [%d/%d] ❌ 无法归类: 无Timestamp 且 台架关键词<2个。"
                        "实际列名=%s",
                        f_idx, len(uploaded_files), list(df.columns)[:12],
                    )
                    st.warning(
                        f"{f.name}: ❌ 无法归类\n\n"
                        f"三大数据类型规则:\n"
                        f"• 🚗 整车 → .csv 必须含 `Timestamp` 列(当前缺失)\n"
                        f"• 📉 耐久 → 必须是 .docx(当前是 {suffix.upper()})\n"
                        f"• 🏭 台架 → .csv 无 Timestamp 但需含「循环/功率点/效率」等台架关键词(当前未命中)\n"
                    )
        except Exception as e:
            tip = " (旧版 .doc 格式不受支持,请另存为 .docx 后再上传)" if suffix == ".doc" else ""
            logger.error(
                "[上传预检] [%d/%d] ❌ 文件解析异常 name=%s suffix=%s err=%s",
                f_idx, len(uploaded_files), f.name, suffix, e, exc_info=True,
            )
            st.warning(f"{f.name} 解析失败{tip}: {e}")

    logger.info(
        "[上传预检] ═══ 分类完成 ═══ 耗时=%.1fs | 整车(csv=%d+xls=%d)=%d 分片 | "
        "耐久docx=%d 份 | 台架循环=%d 份",
        time.perf_counter() - t_up_start,
        len(csv_parts), len(xls_parts), len(csv_parts) + len(xls_parts),
        len(_docx_raw_bytes), len(_bench_raw_bytes),
    )
    # ✅ 台架循环行总数 → 存到 session_state,让顶部识别卡片能看到(跨模块跨 rerun 共享)
    if bench_parts:
        st.session_state["_bench_parts"] = bench_parts
        st.session_state["_bench_parts_count"] = sum(int(len(b)) for b in bench_parts)
    else:
        st.session_state.pop("_bench_parts", None)
        st.session_state.pop("_bench_parts_count", None)

    # ─────────────────────────────────────────────────────────
    # ✅ 方案 A 落库: 整车(A1+A2) / 耐久工步(A1+B1) / 台架循环(A1+C1)
    #    — 按 SHA256(file_bytes) 去重,避免同一文件上传 N 次重复灌数据
    # ─────────────────────────────────────────────────────────
    _db_st_msgs: list[str] = []

    # ---------- 整车 ----------
    all_csv_parts = csv_parts + xls_parts
    if all_csv_parts:
        t_p0 = time.perf_counter()
        logger.info("[落库] 🚗 整车 A1+A2 开始: 分片数=%d", len(all_csv_parts))
        before_rows = sum(len(p) for p in all_csv_parts)
        merged = pd.concat(all_csv_parts, ignore_index=True)
        dup_removed = len(merged) - len(merged.drop_duplicates(subset=["Timestamp"], keep="first"))
        merged = merged.drop_duplicates(subset=["Timestamp"], keep="first")
        merged = merged.sort_values("Timestamp").reset_index(drop=True)
        meta = (parse_csv_filename(uploaded_files[0].name)
                if Path(uploaded_files[0].name).suffix.lower() == ".csv"
                else {"vehicle": "上传"})
        vehicle_id = str(meta.get("vehicle") or "上传")
        data[vehicle_id] = merged
        logger.info(
            "[落库] 🚗 整车合并完成: 原始行数=%d → 去重后=%d (Timestamp重复删除=%d) "
            "车辆ID=%s 文件名元数据=%s",
            before_rows, len(merged), int(dup_removed), vehicle_id, meta,
        )
        # A1 + A2 落库
        try:
            # 用"合并后字节集"的哈希去重(顺序拼接 _veh_raw_bytes 所有 value)
            concat_bytes = b"".join(_veh_raw_bytes.get(fn, b"")
                                    for fn in sorted(_veh_raw_bytes.keys())) or (
                b"merged_" + f"{len(merged)}rows".encode())
            ts_min_pd = pd.to_datetime(merged["Timestamp"], errors="coerce").dropna()
            ts_min = (ts_min_pd.min().strftime("%Y-%m-%d %H:%M:%S")
                      if len(ts_min_pd) else None)
            ts_max = (ts_min_pd.max().strftime("%Y-%m-%d %H:%M:%S")
                      if len(ts_min_pd) else None)
            numeric_cols = [c for c in merged.columns
                            if c != "Timestamp" and pd.api.types.is_numeric_dtype(merged[c])]
            logger.info(
                "[落库] 🚗 A1 upsert 入参: concat_bytes=%dByte 数值列=%d "
                "信号列(前10)=%s 时间范围=%s~%s",
                len(concat_bytes), len(numeric_cols), numeric_cols[:10], ts_min, ts_max,
            )
            fid, inserted, fhash = db_upsert_data_file(
                "整车",
                file_name="+".join(sorted(_veh_raw_bytes.keys())) or f"{vehicle_id}-merged",
                file_bytes=concat_bytes,
                vehicle_id=vehicle_id,
                row_count=int(len(merged)),
                time_min=ts_min,
                time_max=ts_max,
                col_signals=numeric_cols,
                status="uploaded",
            )
            logger.info(
                "[落库] 🚗 A1 upsert 返回: fid=%s inserted=%s hash=%s… (新文件才继续A2聚合写入)",
                fid, inserted, fhash[:12],
            )
            if inserted:
                agg_cnt = db_write_vehicle_minute(vehicle_id, merged, file_id=fid)
                # A1 状态回写
                logger.info("[落库] 🚗 A2 聚合完成 分钟桶=%d → 回写A1状态: uploaded→aggregated",
                            int(agg_cnt))
                db_upsert_data_file(
                    "整车",
                    file_name="+".join(sorted(_veh_raw_bytes.keys())) or f"{vehicle_id}-merged",
                    file_hash=fhash,
                    vehicle_id=vehicle_id,
                    row_count=int(len(merged)),
                    time_min=ts_min,
                    time_max=ts_max,
                    col_signals=numeric_cols,
                    status="aggregated",
                    agg_rows=int(agg_cnt),
                )
                _db_st_msgs.append(
                    f"🚗 整车已持久化: {len(merged):,} 行 → 分钟桶 {agg_cnt:,} 行 (车 {vehicle_id})")
            else:
                _db_st_msgs.append(
                    f"🚗 整车(车 {vehicle_id})内容未变(hash 已存在),跳过重复入库")
            logger.info(
                "[落库] 🚗 整车 A1+A2 完成 | 耗时=%.1fs | msg=%s",
                time.perf_counter() - t_p0, _db_st_msgs[-1],
            )
        except Exception as ex:
            logger.error("[落库] ❌ 整车失败 vehicle=%s err=%s", vehicle_id, ex, exc_info=True)
            _db_st_msgs.append(f"🚗 整车落库失败(不影响查看): {ex}")

        # 数据质量扫描 + 邮件报警(发现高危时)
        try:
            from src.data_quality import scan_df, generate_brief, save_brief
            from src.email_alert import send_alert

            scan_result = scan_df(merged, vehicle=vehicle_id)
            brief = generate_brief(scan_result)
            brief_path = save_brief(brief, vehicle=vehicle_id)

            with st.expander("📋 数据质量简报", expanded=True):
                if scan_result["overall_risk"] == "高危":
                    st.error(f"⚠ 检测到高危字段: "
                             f"{', '.join(scan_result['high_risk_fields'])}")
                elif scan_result["overall_risk"] == "中危":
                    st.warning("检测到中危字段,请关注")
                else:
                    st.success("✓ 数据质量正常,未发现高危字段")
                st.code(brief, language="text")
                with open(brief_path, "rb") as bf:
                    st.download_button(
                        "下载质量简报", bf,
                        file_name=brief_path.name, mime="text/plain",
                    )

            if scan_result["overall_risk"] == "高危":
                subject = (f"[数据质量告警] 车辆 {vehicle_id} "
                           f"发现 {len(scan_result['high_risk_fields'])} 个高危字段")
                sent = send_alert(subject=subject, body=brief,
                                  attachment=brief_path)
                if sent:
                    st.info("📧 报警邮件已发送")
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "质量扫描或邮件报警执行失败(不影响主流程): %s", e, exc_info=True)

    # ---------- 耐久工步(docx): B1 + A1 ----------
    if docx_parts:
        t_b0 = time.perf_counter()
        try:
            all_docx_df = pd.concat(docx_parts, ignore_index=True)
            logger.info(
                "[落库] 📉 耐久工步 A1+B1 开始: docx=%d 份 总工步=%d 行",
                len(_docx_raw_bytes), len(all_docx_df),
            )
            for bi, (name, fbytes, _tdf) in enumerate(_docx_raw_bytes, 1):
                # sample_name: docx 解析不到单独的样品列, 用文件名 stem(去扩展名)
                sample_n = Path(name).stem
                logger.info(
                    "[落库] 📉 耐久[%d/%d] A1 upsert name=%s bytes=%d rows=%d sample=%s",
                    bi, len(_docx_raw_bytes), name, len(fbytes), len(_tdf), sample_n,
                )
                fid, inserted, _fh = db_upsert_data_file(
                    "耐久工步", name, file_bytes=fbytes,
                    row_count=int(len(_tdf)),
                    extra_meta={"sample": sample_n},
                    status="uploaded",
                )
                logger.info(
                    "[落库] 📉 耐久[%d/%d] A1 返回 fid=%s inserted=%s hash=%s…",
                    bi, len(_docx_raw_bytes), fid, inserted, _fh[:12],
                )
                if inserted:
                    wcnt = db_write_durability_stages(
                        _tdf, file_id=fid, sample_name=sample_n, raw_file_name=name)
                    logger.info(
                        "[落库] 📉 耐久[%d/%d] B1 写入=%d行 → 回写A1状态 uploaded→aggregated",
                        bi, len(_docx_raw_bytes), int(wcnt),
                    )
                    db_upsert_data_file(
                        "耐久工步", name, file_hash=_fh,
                        row_count=int(len(_tdf)),
                        extra_meta={"sample": sample_n},
                        status="aggregated", agg_rows=int(wcnt),
                    )
                else:
                    logger.info(
                        "[落库] 📉 耐久[%d/%d] hash已存在(已入库过),跳过 B1 重复写入",
                        bi, len(_docx_raw_bytes),
                    )
            if _docx_raw_bytes:
                msg = f"📉 耐久工步已持久化: {len(all_docx_df):,} 条工步 × {len(_docx_raw_bytes)} 个 docx"
                _db_st_msgs.append(msg)
                logger.info("[落库] 📉 耐久工步 A1+B1 完成 | 耗时=%.1fs | %s",
                            time.perf_counter() - t_b0, msg)
        except Exception as ex:
            logger.error("[落库] ❌ 耐久工步失败 err=%s", ex, exc_info=True)
            _db_st_msgs.append(f"📉 耐久工步落库失败(不影响查看): {ex}")

    # ---------- 台架循环(无 Timestamp CSV/Excel): C1 + A1 ----------
    if bench_parts:
        from durability.data_parser import parse_durability_data
        from durability.statistics_aggregator import aggregate_durability_stats

        t_c0 = time.perf_counter()
        try:
            # 每个上传文件独立解析 + 聚合 + 落 C1(便于 rig_id 区分)
            _BENCH_SIG_COPY: list[str] = [
                "FC_AvgCellVoltage", "FC_AvgCellVoltDev", "FC_VARVoltage",
                "FC_LFR", "FC_HFR",
                "FC_CurrOut", "FC_VoltOut", "FC_NetPwrOut", "FC_MinCellVoltage",
            ]
            logger.info(
                "[落库] 🔬 台架循环 A1+C1 开始: 文件=%d 份 聚合信号=%s",
                len(_bench_raw_bytes), _BENCH_SIG_COPY,
            )
            _total_c1_rows = 0
            for ci, (name, fbytes, raw_df) in enumerate(_bench_raw_bytes, 1):
                rig_n = f"上传::{Path(name).stem}"
                cycles_before = (int(raw_df["cycle_id"].fillna(-1).nunique())
                                 if "cycle_id" in raw_df.columns else 0)
                logger.info(
                    "[落库] 🔬 台架[%d/%d] 开始 name=%s bytes=%d raw_rows=%d cycle数≈%d rig=%s",
                    ci, len(_bench_raw_bytes), name, len(fbytes),
                    len(raw_df), cycles_before, rig_n,
                )
                parsed = parse_durability_data(raw_df)
                if len(parsed) == 0:
                    logger.warning("[落库] 🔬 台架[%d/%d] parse_durability_data 返回空,跳过",
                                   ci, len(_bench_raw_bytes))
                    continue
                logger.info(
                    "[落库] 🔬 台架[%d/%d] parse后 rows=%d cols=%d | 列名(前8)=%s",
                    ci, len(_bench_raw_bytes), len(parsed), len(parsed.columns),
                    list(parsed.columns)[:8],
                )
                agg_df = aggregate_durability_stats(parsed, _BENCH_SIG_COPY)
                if len(agg_df) == 0:
                    logger.warning("[落库] 🔬 台架[%d/%d] aggregate返回空(没有cycle×功率点?),跳过",
                                   ci, len(_bench_raw_bytes))
                    continue
                cycles_uniq = sorted(agg_df["cycle_id"].astype(int).unique().tolist())
                pps_uniq = sorted(agg_df["power_point"].astype(float).unique().tolist())
                logger.info(
                    "[落库] 🔬 台架[%d/%d] 聚合完成 rows=%d cycle=%s 功率点=%s → 开始A1 upsert",
                    ci, len(_bench_raw_bytes), len(agg_df), cycles_uniq[:6], pps_uniq[:6],
                )
                fid, inserted, _fh = db_upsert_data_file(
                    "台架循环", name, file_bytes=fbytes,
                    row_count=int(len(raw_df)),
                    extra_meta={"rig": rig_n,
                                "cycles": int(parsed.get("cycle_id").fillna(-1).nunique())
                                if "cycle_id" in parsed.columns else 0},
                    status="uploaded",
                )
                logger.info(
                    "[落库] 🔬 台架[%d/%d] A1 返回 fid=%s inserted=%s hash=%s…",
                    ci, len(_bench_raw_bytes), fid, inserted, _fh[:12],
                )
                wcnt, c1_ids = db_write_bench_cycle_stats(
                    agg_df, file_id=fid, rig_id=rig_n, source_file_name=name)
                _total_c1_rows += int(wcnt)
                logger.info(
                    "[落库] 🔬 台架[%d/%d] C1 写入/更新=%d ids_count=%d",
                    ci, len(_bench_raw_bytes), int(wcnt), len(c1_ids),
                )
                if inserted:
                    logger.info(
                        "[落库] 🔬 台架[%d/%d] 新文件 → 回写A1状态 uploaded→aggregated agg_rows=%d",
                        ci, len(_bench_raw_bytes), int(wcnt),
                    )
                    db_upsert_data_file(
                        "台架循环", name, file_hash=_fh,
                        row_count=int(len(raw_df)),
                        extra_meta={"rig": rig_n},
                        status="aggregated", agg_rows=int(wcnt),
                    )
                else:
                    logger.info(
                        "[落库] 🔬 台架[%d/%d] hash已存在,A1 inserted=False → C1已做upsert合并(OK)",
                        ci, len(_bench_raw_bytes),
                    )
            if _bench_raw_bytes:
                msg = (f"🏭 台架循环已持久化: {_total_c1_rows} 行循环×功率点聚合 "
                       f"× {len(_bench_raw_bytes)} 份文件")
                _db_st_msgs.append(msg)
                logger.info(
                    "[落库] 🔬 台架循环 A1+C1 完成 | 耗时=%.1fs | %s",
                    time.perf_counter() - t_c0, msg,
                )
        except Exception as ex:
            logger.error("[落库] ❌ 台架循环失败 err=%s", ex, exc_info=True)
            _db_st_msgs.append(f"🏭 台架循环落库失败(不影响查看): {ex}")

    # ── 所有落库消息合并成一条 toast/info(不打断主流程) ──
    if _db_st_msgs:
        with st.expander("💾 文件已保存到本地数据库(SQLite) · 下次刷新页面无需重传",
                         expanded=True):
            for m in _db_st_msgs:
                st.markdown(f"- {m}")


# 耐久数据:内置 + 上传的 Word/Excel 补充 + 冷启动从 B1(SQLite) 回填
dur_df = (load_default_durability()
          if use_builtin == "使用内置数据(自动扫描)" else pd.DataFrame())
if docx_parts:
    uploaded_dur = pd.concat(docx_parts, ignore_index=True)
    if dur_df.empty:
        dur_df = uploaded_dur
    else:
        dur_df = pd.concat([dur_df, uploaded_dur], ignore_index=True)
elif len(_hydrated_dur):
    # 有从 B1(SQLite) 拉回来的工步数据 → 补进 dur_df
    if dur_df.empty:
        dur_df = _hydrated_dur
    else:
        dur_df = pd.concat([dur_df, _hydrated_dur], ignore_index=True)

# 台架循环数据(上传的无 Timestamp 但命中台架关键词的 CSV/Excel)
try:
    _bench_count = int(st.session_state.get("_bench_parts_count", 0))
except Exception:
    _bench_count = 0


# ---------- 顶部状态 ----------

st.title("📊 设备测试数据分析与自动报告助手")
st.caption("上传或使用内置数据,自动完成合并 / 清洗 / 指标计算 / 可视化 / 一键导出报告")

# 🔧 Bug 修复 + 三类型放行: 整车 data / 耐久 dur_df / 台架循环 bench_count, 任一有就不 stop
_has_any = bool(data) or bool(dur_df is not None and len(dur_df) > 0) or _bench_count > 0
if not _has_any:
    st.warning(
        "⚠️ 未检测到任何数据。\n\n"
        "👉 上传格式提示(自动识别分类归入对应Tab):\n"
        "- **① 整车分析**(02_整车数据处理):CSV / Excel,必须含 `Timestamp` 列 → 整车看板/燃电/性能等 8 个 Tab\n"
        "- **② 耐久工步**(01_耐久原始数据处理):Word(.docx) / 耐久XX-YY.xlsx → 「耐久衰减」Tab\n"
        "- **③ 台架循环**(03_台架耐久数据):含「循环/功率点/效率」等关键词的 CSV / Excel → 「台架耐久统计及预警」Tab\n"
        "- 旧版 `.doc` 请另存为 `.docx` 后再上传"
    )
    st.stop()
elif not data and (dur_df is None or len(dur_df) == 0) and _bench_count > 0:
    st.info(
        f"🏭 已检测到 **台架循环数据 {_bench_count:,} 行** (无整车 CSV / 耐久 docx 数据)。\n\n"
        "可直接切换到「🔬 台架耐久统计及预警」Tab 分析;\n整车看板/耐久衰减等其他 Tab 会显示空数据提示。"
    )
elif not data:
    # 有耐久 + 可能还有台架
    if _bench_count > 0:
        st.info(
            f"💡 已检测到 **耐久数据 {len(dur_df):,} 条工步** + 🏭 **台架循环 {_bench_count:,} 行** (无整车 CSV)。\n\n"
            "耐久 → 「耐久衰减」Tab;台架 → 「🔬 台架耐久统计及预警」Tab。"
        )
    else:
        st.info(
            f"💡 已检测到 **耐久数据 {len(dur_df):,} 条** (无整车 CSV 数据)。\n\n"
            "可直接切换到「耐久衰减」Tab 查看 docx 分析;\n整车看板 / 燃电看板等需要 CSV Timestamp 列的 Tab 会显示空数据提示。"
        )
else:
    # 有整车
    extra_msgs = []
    if len(dur_df):
        extra_msgs.append(f"耐久 {len(dur_df):,} 条工步")
    if _bench_count > 0:
        extra_msgs.append(f"台架 {_bench_count:,} 行")
    if extra_msgs:
        st.info(
            f"📦 上传解析结果:整车 CSV {len(data)} 辆车 · {' · '.join(extra_msgs)}"
            " (耐久→「耐久衰减」Tab;台架→「台架耐久统计及预警」Tab)"
        )

cars = list(data.keys())
_msgs: list[str] = []
if cars:
    _msgs.append(f"已加载 {len(cars)} 辆车: {', '.join(cars)}")
if len(dur_df):
    _msgs.append(f"耐久 docx: {len(dur_df):,} 条工步")
if _bench_count > 0:
    _msgs.append(f"台架循环: {_bench_count:,} 行")
st.success(" | ".join(_msgs) if _msgs else "暂无已加载数据")

# ============================================================
# 📌 三数据类型自动识别 + 一键直达对应 Tab
# 与企业资料包02_氢质氢离 下三个子目录严格一一对应:
#   ① 02_整车数据处理  → 带 Timestamp 的 CSV / Excel → 整车看板/燃电/性能/绝缘 等 8 个 Tab
#   ② 01_耐久原始数据处理 → 耐久XX-YY.docx → 耐久衰减 Tab
#   ③ 03_台架耐久数据   → 循环/功率点 CSV → 台架耐久统计及预警 Tab
# ============================================================
def _detect_data_type_tags(df: pd.DataFrame) -> set[str]:
    """无 Timestamp 的表 → 判断归属耐久工步 / 台架循环。"""
    if df is None or df.empty:
        return set()
    cols_lower = {str(c).lower() for c in df.columns}
    tags = set()
    # 耐久工步(docx标准宽表):含 平均单体电压 / 净输出功率 / 目标功率 / 电堆电流 等工步列
    if any(k in cols_lower for k in {"平均单体电压(v)", "净输出功率(kw)", "目标功率(kw)", "电堆电流(a)", "stage_start_h", "stage", "电压方差", "离均差", "hfr"}):
        tags.add("耐久工步")
    # 台架循环:含 循环/loop/cycle/功率点/电流密度/效率 等台架特征列
    if any(k in cols_lower for k in {"循环", "loop", "cycle", "循环编号", "循环次数", "功率点", "电流密度", "效率", "hydrogen利用率", "氢气利用率"}):
        tags.add("台架循环")
    return tags

_recognized: list[dict] = []

# ---------- ① 整车数据 ----------
if bool(data):
    _total_rows = sum(int(len(v)) for v in data.values())
    _recognized.append({
        "kind": "整车数据",
        "dir": "02_整车数据处理",
        "tab_name": "整车看板",
        "summary": f"{len(cars)} 辆车 · 合计 {_total_rows:,} 行",
        "emoji": "🚗",
        "extra_tabs": ["⚡ 燃电运行看板", "📈 性能统计预测", "趋势预测", "🔌 绝缘阻值统计", "报告导出", "AI 助手", "多车对比"],
    })

# ---------- ② 耐久工步数据(docx) ----------
_dur_工步_tags = _detect_data_type_tags(dur_df)
if dur_df is not None and len(dur_df) > 0 and ("耐久工步" in _dur_工步_tags or "stage_start_h" in (dur_df.columns if dur_df is not None else []) or "平均单体电压(V)" in (dur_df.columns if dur_df is not None else [])):
    _n_stages = int(dur_df["stage"].nunique()) if "stage" in dur_df.columns else 0
    _recognized.append({
        "kind": "耐久工步数据",
        "dir": "01_耐久原始数据处理",
        "tab_name": "耐久衰减",
        "summary": f"{len(dur_df):,} 条工步 · {_n_stages} 个阶段",
        "emoji": "📉",
    })

# ---------- ③ 台架循环数据(上传的无 Timestamp CSV/Excel 若命中台架关键词) ----------
try:
    _bench_parts_total_rows = 0
    if "_bench_parts_count" in st.session_state:
        _bench_parts_total_rows = int(st.session_state["_bench_parts_count"])
except Exception:
    _bench_parts_total_rows = 0
if _bench_parts_total_rows > 0:
    _recognized.append({
        "kind": "台架循环数据",
        "dir": "03_台架耐久数据",
        "tab_name": "🔬 台架耐久统计及预警",
        "summary": f"{_bench_parts_total_rows:,} 行循环数据",
        "emoji": "🏭",
    })
# 内置台架目录的CSV也算
import os as _os
_bench_builtin_dir = Path(__file__).resolve().parent / "企业资料包02_氢质氢离" / "03_台架耐久数据"
if _bench_builtin_dir.exists() and use_builtin == "使用内置数据(自动扫描)":
    _bench_csvs = list(_bench_builtin_dir.glob("*.csv"))
    if _bench_csvs:
        _recognized.append({
            "kind": "台架循环数据",
            "dir": "03_台架耐久数据",
            "tab_name": "🔬 台架耐久统计及预警",
            "summary": f"内置 {len(_bench_csvs)} 份 CSV (进入 Tab 加载)",
            "emoji": "🏭",
        })

if _recognized:
    # ---------- 11 个 Tab 的固定顺序与索引(用于引导提示第几个) ----------
    # 顺序必须与 st.tabs(...) 完全一致:前4=企业核心功能,后7=补充功能
    _TAB_ORDER = ["⚡ 燃电运行看板", "📈 性能统计预测", "🔌 绝缘阻值统计",
                  "🔬 台架耐久统计及预警",
                  "整车看板", "耐久衰减", "趋势预测", "多车对比", "报告导出",
                  "AI 助手", "📡 飞书人员对接"]

    _jump_card = st.container(border=True)
    with _jump_card:
        st.markdown("### 📌 已自动识别数据类型 · 点按钮后看上方 👆 标签栏")
        _cols = st.columns(min(len(_recognized), 3))
        for _i, _r in enumerate(_recognized):
            with _cols[_i % len(_cols)]:
                st.markdown(
                    f"#### {_r['emoji']} {_r['kind']}\n"
                    f"- 📁 目录: `{_r['dir']}`\n"
                    f"- 📊 {_r['summary']}\n"
                    f"- 🎯 主 Tab: **{_r['tab_name']}**"
                    + (f"\n- 💡 其他可用 Tab: {'、'.join(_r['extra_tabs'])}" if _r.get("extra_tabs") else "")
                )
                _btn = st.button(
                    f"👉 去「{_r['tab_name']}」分析",
                    type="primary",
                    key=f"jump_btn_{_r['tab_name']}_{_i}",
                    use_container_width=True,
                )
                if _btn:
                    _tab_label = _r['tab_name']
                    _tab_idx = _TAB_ORDER.index(_tab_label) + 1 if _tab_label in _TAB_ORDER else "?"
                    # ASCII 箭头指向目标 Tab 的位置示意图
                    _arrow_bar = "  ".join([f"{'👇' if i+1==_tab_idx else '──'}" for i in range(len(_TAB_ORDER))])
                    _idx_bar  = "  ".join([f"[{i+1}]" for i in range(len(_TAB_ORDER))])
                    st.toast(f"🎯 第 {_tab_idx} 个 Tab「{_tab_label}」→ 看标签栏!", icon="✅")
                    st.success(
                        f"## 👆 请点击页面**最上方标签栏第 {_tab_idx} 个**「{_tab_label}」\n\n"
                        f"```\n"
                        f"Tab 顺序: {_idx_bar}\n"
                        f"箭头指: {_arrow_bar}\n"
                        f"```\n\n"
                        f"↑↑↑ 11 个标签就排在当前这段话的正上方,"
                        f"找到标 🔵「{_tab_label}」的那个直接点一下就行。"
                        + (f"\n\n💡 其他相关 Tab: {', '.join(_r['extra_tabs'])}" if _r.get("extra_tabs") else ""),
                        icon="🎯",
                    )



# ---------- 主区域 Tab（按企业优先级排序） ----------
# [核心功能区 1-4] 企业需求四大功能
# [补充功能区 5-12] 原整车看板/耐久衰减/趋势预测等辅助功能
# 顺序编号用于「一键跳转」按钮的 ASCII 箭头引导提示
tab_fc, tab_perf, tab_insul, tab_bench, tab_overview, tab_dur, tab_forecast, tab_cmp, tab_report, tab_ai, tab_contacts, tab_history = st.tabs([
    # 核心功能区(前4)
    "⚡ 燃电运行看板",     # 功能1: 燃电关键运行数据显示 (企业需求第1项)
    "📈 性能统计预测",     # 功能2: 燃电性能统计及预测 (企业需求第2项)
    "🔌 绝缘阻值统计",     # 功能3: 绝缘阻值统计及预测 (企业需求第3项)
    "🔬 台架耐久统计及预警",# 功能4: 台架耐久数据统计及预警 (企业需求第4项)
    # 补充功能区(后8)
    "整车看板",            # 辅助:整车数据汇总概览
    "耐久衰减",            # 辅助:耐久工步(docx)衰减分析
    "趋势预测",            # 辅助:整车历史线性回归预测
    "多车对比",            # 辅助:多车横向对比
    "报告导出",            # 系统:报告一键导出
    "AI 助手",            # 系统:AI 智能解答(全产品)
    "📡 飞书人员对接",     # 系统:飞书预警联系人配置
    "📁 上传历史",         # 系统:上传历史记录及数据回看
])


# ============================================================
# 以下:所有 Tab 渲染函数(懒加载 + 全异常兜底装饰器)
# ============================================================

@tab_safe_render
def _render_tab_overview(
    cars: list[str],
    data: dict[str, pd.DataFrame],
    time_range_preset: str,
) -> None:
    """Tab1: 整车看板。"""
    if not cars:
        st.info("未检测到整车 CSV 数据。请在侧边栏选择「上传文件」并拖入带 `Timestamp` 列的 CSV / Excel,或使用内置数据模式。")
        return
    if not data:
        st.warning("暂无已加载的整车数据,请检查数据源或切换数据模式。")
        return
    sel_car = st.selectbox("选择车辆", cars, key="overview_car")
    if sel_car is None or sel_car not in data:
        st.warning("请先上传/选择有效车辆数据。")
        return
    df = data[sel_car]

    # 时间区间过滤(支持预设 + 自定义)
    if "Timestamp" in df.columns and len(df):
        t_min = df["Timestamp"].min()
        t_max = df["Timestamp"].max()

        if time_range_preset == "全部数据":
            pass  # 不过滤
        elif time_range_preset == "自定义区间":
            c1, c2 = st.columns(2)
            lo = c1.datetime_input("起始", value=t_min.to_pydatetime())
            hi = c2.datetime_input("结束", value=t_max.to_pydatetime())
            df = df[(df["Timestamp"] >= pd.Timestamp(lo))
                    & (df["Timestamp"] <= pd.Timestamp(hi))]
        else:
            # 解析预设:最近 N 小时/天
            import re
            m = re.match(r"最近 (\d+) (小时|天)", time_range_preset)
            if m:
                n = int(m.group(1))
                unit = m.group(2)
                td = pd.Timedelta(hours=n) if unit == "小时" else pd.Timedelta(days=n)
                cutoff = t_max - td
                df = df[df["Timestamp"] >= cutoff]
                st.caption(f"已过滤到 {time_range_preset}: "
                           f"{cutoff.strftime('%Y-%m-%d %H:%M')} → "
                           f"{t_max.strftime('%Y-%m-%d %H:%M')},"
                           f"共 {len(df):,} 行")

    if len(df) == 0:
        st.warning("当前区间无数据")
        return

    # 概览卡片
    ov = vehicle_overview(df)
    card_cols = st.columns(4)
    cards = [
        ("运行时长(h)", ov.get("运行时长(h)", "-")),
        ("行驶里程(km)", ov.get("行驶里程(km)", "-")),
        ("平均车速(km/h)", ov.get("平均车速(km/h)", "-")),
        ("启动次数", ov.get("启动次数", "-")),
        ("百公里氢耗均值(kg)", ov.get("百公里氢耗均值(kg)", "-")),
        ("瞬时氢耗均值(kg/h)", ov.get("瞬时氢耗均值(kg/h)", "-")),
        ("故障码种类", ov.get("故障码种类", "-")),
        ("采样点数", ov.get("采样点数", "-")),
    ]
    for i, (k, v) in enumerate(cards):
        with card_cols[i % 4]:
            st.metric(k, v)

    # 曲线
    c1, c2 = st.columns(2)
    with c1:
        st.plotly_chart(fig_cell_voltage(df), use_container_width=True)
    with c2:
        st.plotly_chart(fig_power_curve(df), use_container_width=True)
    c3, c4 = st.columns(2)
    with c3:
        st.plotly_chart(fig_speed_hydrogen(df), use_container_width=True)
    with c4:
        st.plotly_chart(fig_fault_bar(ov.get("故障码Top10", {})), use_container_width=True)

    with st.expander("详细指标(单片一致性 / 功率 / 氢系统)"):
        st.json({
            "单片电压一致性": cell_voltage_consistency(df),
            "功率与效率": power_summary(df),
            "氢系统状态": h2_system(df),
        })


@tab_safe_render
def _render_tab_durability(dur_df: pd.DataFrame) -> None:
    """Tab2: 耐久衰减(docx 聚合分析 + 曲线)。"""
    if dur_df is None or dur_df.empty:
        st.info("未检测到耐久 docx 数据。可去侧边栏切到『上传文件』后拖入 docx / 无 Timestamp 的 Excel。")
        return

    # ✅ 防御:列结构完整性检查(上传的 Excel 可能缺标准列)
    req_cols = ["stage_start_h", "stage", "平均单体电压(V)", "净输出功率(kW)", "电堆电流(A)"]
    missing = [c for c in req_cols if c not in dur_df.columns]
    if missing:
        st.warning(
            f"上传的耐久数据缺少以下必需列: `{missing}`\n\n"
            f"当前数据有 {len(dur_df)} 行,实际列: {list(dur_df.columns)}\n"
            f"建议:拖入「耐久XX-YY.docx」,标准解析会自动产出 stage_start_h / 各指标列。"
        )
        st.subheader(f"当前上传耐久原始数据预览 ({len(dur_df)} 行,{len(dur_df.columns)} 列)")
        st.dataframe(dur_df.head(100), use_container_width=True)
        return

    st.subheader("耐久 docx 元数据")
    from src.data_loader import load_durability_metadata
    meta_base = DATA_ROOT / "01_耐久原始数据处理"
    if meta_base.exists():
        meta_files = sorted(meta_base.glob("*.docx"))
        if meta_files:
            meta_df = load_durability_metadata([str(f) for f in meta_files])
            st.dataframe(meta_df, use_container_width=True, hide_index=True)

    st.subheader(f"耐久数据(共 {len(dur_df)} 条工步,跨 {dur_df['stage'].nunique()} 个阶段)")
    st.dataframe(dur_df, use_container_width=True, hide_index=True)

    # 按阶段聚合(用 stage_start_h 排序保证真实耐久先后顺序)
    st.subheader("各阶段指标聚合")
    # 防御:有 stage 但全是 NaN 时,groupby 会掉所有行 → 先 sort_values 再按 stage_start_h 分组
    dur_sorted = dur_df.sort_values(["stage_start_h", "step_idx"], kind="stable").reset_index(drop=True)
    agg_cols_map = {
        "平均单体电压": "平均单体电压(V)",
        "净输出功率": "净输出功率(kW)",
        "电堆电流": "电堆电流(A)",
    }
    # 仅对存在的数值列 agg(可能列不齐),避免 KeyError
    agg_dict = {new: (old, "mean") for new, old in agg_cols_map.items() if old in dur_sorted.columns}
    opt_cols = {"离均差", "电压方差"}
    for c in opt_cols:
        if c in dur_sorted.columns:
            agg_dict[c] = (c, "mean")
    if not agg_dict:
        st.warning(f"数据中找不到任何可聚合数值列,实际列: {list(dur_sorted.columns)}")
        st.dataframe(dur_sorted.head(100), use_container_width=True)
        return
    agg = dur_sorted.groupby(["stage_start_h", "stage"]).agg(**agg_dict).reset_index()
    agg = agg.sort_values("stage_start_h", kind="stable").reset_index(drop=True)

    if agg.empty:
        st.warning("按阶段聚合后无数据(可能 stage 或 stage_start_h 全为空),请检查上传文件格式。")
        st.dataframe(dur_sorted.head(100), use_container_width=True)
        return

    st.dataframe(dur_sorted.head(100), use_container_width=True, hide_index=True)
    st.caption(f"数据预览(前 100 行) · 全量 {len(dur_sorted)} 行 × {dur_sorted['stage'].nunique()} 个阶段")

    # KPI 卡片:整体衰减
    if "平均单体电压" in agg.columns and len(agg) >= 1:
        k1, k2, k3 = st.columns(3)
        first_v = float(agg.iloc[0]["平均单体电压"])
        last_v = float(agg.iloc[-1]["平均单体电压"])
        with k1:
            st.metric("首阶段平均电压(V)", round(first_v, 3))
        with k2:
            st.metric("末阶段平均电压(V)", round(last_v, 3))
        with k3:
            delta = round(last_v - first_v, 3)
            st.metric("衰减量(mV)", int(delta * 1000), delta=int(delta * 1000), delta_color="inverse")

    st.subheader(f"阶段聚合表 ({len(agg)} 阶段)")
    st.dataframe(agg, use_container_width=True, hide_index=True)

    # 衰减趋势图(用 stage_start_h 作为 X,显示真实耐久小时数)
    has_v = "平均单体电压" in agg.columns
    has_p = "净输出功率" in agg.columns
    if has_v or has_p:
        from src.plots import _base_layout
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots

        fig = make_subplots(specs=[[{"secondary_y": True}]])
        if has_v:
            fig.add_trace(go.Scatter(
                x=agg["stage_start_h"], y=agg["平均单体电压"],
                mode="lines+markers", name="平均单体电压(V)",
                line=dict(color="#1f77b4", width=2),
                hovertemplate="%{x:.0f}h<br>%{y:.4f} V<extra></extra>",
            ), secondary_y=False)
        if has_p:
            fig.add_trace(go.Scatter(
                x=agg["stage_start_h"], y=agg["净输出功率"],
                mode="lines+markers", name="净输出功率(kW)",
                line=dict(color="#ff7f0e", width=2, dash="dot"),
                hovertemplate="%{x:.0f}h<br>%{y:.2f} kW<extra></extra>",
            ), secondary_y=True)
        fig.update_layout(**_base_layout("耐久衰减趋势:平均单体电压 + 净输出功率"))
        fig.update_yaxes(title_text="平均单体电压 (V)", secondary_y=False)
        fig.update_yaxes(title_text="净输出功率 (kW)", secondary_y=True)
        fig.update_xaxes(title_text="耐久起始小时数 stage_start_h (h)")
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("当前数据缺「平均单体电压」或「净输出功率」列,跳过衰减趋势图。")

    # 同阶段内工步曲线(选取代表性阶段)
    st.subheader("阶段内功率-电压特性曲线(极化曲线)")

    def _stage_sort_key(s: str):
        """阶段名兼容: '40-45' 取 40;纯文件名 fallback 到 0。"""
        if isinstance(s, str) and "-" in s:
            try:
                return int(s.split("-")[0])
            except Exception:
                pass
        try:
            return int(float(str(s)))
        except Exception:
            return 0

    unique_stages = list(dur_sorted["stage"].dropna().unique())
    if not unique_stages:
        st.warning("无可用阶段数据,跳过极化曲线。")
        return
    sel_stage = st.selectbox(
        "选择阶段",
        sorted(unique_stages, key=_stage_sort_key),
        key="dur_stage",
    )
    sub = dur_sorted[dur_sorted["stage"] == sel_stage]
    if "step_idx" in sub.columns:
        sub = sub.sort_values("step_idx")
    if "电堆电流(A)" in sub.columns and "平均单体电压(V)" in sub.columns:
        fig2 = go.Figure()
        fig2.add_trace(go.Scatter(
            x=sub["电堆电流(A)"], y=sub["平均单体电压(V)"],
            mode="lines+markers", name=f"阶段 {sel_stage}",
            line=dict(color="#2ca02c", width=2),
        ))
        fig2.update_layout(**_base_layout(f"阶段 {sel_stage} 极化曲线"))
        fig2.update_xaxes(title_text="电堆电流 (A)")
        fig2.update_yaxes(title_text="平均单体电压 (V)")
        st.plotly_chart(fig2, use_container_width=True)
    else:
        st.info(f"当前阶段缺「电堆电流(A)」或「平均单体电压(V)」列,无法画极化曲线。阶段内数据预览:")
        st.dataframe(sub, use_container_width=True)


@tab_safe_render
def _render_tab_bench() -> None:
    """Tab3: 台架耐久统计及预警。"""
    st.subheader("台架耐久数据统计与预警")
    st.caption("解析台架 CSV → 按循环/功率点聚合 → 可视化趋势 + 飞书预警 + 历史记录")

    bench_dir = DATA_ROOT / "03_台架耐久数据"
    csv_files = sorted(bench_dir.glob("*.csv")) if bench_dir.exists() else []

    # ✅ 读取上传时存入 session_state 的台架循环数据(无 Timestamp 但命中台架关键词)
    uploaded_bench_frames: list[pd.DataFrame] = []
    try:
        _sbp = st.session_state.get("_bench_parts", [])
        if isinstance(_sbp, list):
            for _df in _sbp:
                if isinstance(_df, pd.DataFrame) and not _df.empty:
                    uploaded_bench_frames.append(_df.copy())
    except Exception as e:
        logger.warning("读取 session_state 台架数据失败: %s", e, exc_info=True)

    if not csv_files and not uploaded_bench_frames:
        st.info(
            f"未检测到台架耐久数据。\n\n"
            f"方式 1: 将 CSV 放到内置目录 `{bench_dir}`\n"
            f"方式 2: 在侧边栏「上传文件」处拖入含「循环/功率点/效率」等关键词的 CSV/Excel"
        )
        return

    from durability.data_parser import parse_durability_data
    from durability.statistics_aggregator import aggregate_durability_stats
    from components.durability_filter import render_durability_filter
    from components.durability_chart import render_durability_chart
    from components.durability_alert_log import render_alert_log

    # 企业需求: 补 LFR(低频阻抗) / HFR(高频阻抗) 两个阻抗字段聚合
    _SIGNAL_COLS = [
        'FC_AvgCellVoltage', 'FC_AvgCellVoltDev',
        'FC_LFR', 'FC_HFR',            # 两个阻抗字段(如缺失会自动跳过)
        'FC_NetPwrOut', 'FC_CurrOut',
    ]

    # ---------- ① 内置目录 CSV(走缓存,按文件 mtime 失效) ----------
    @st.cache_data(ttl=60, show_spinner="解析台架耐久 CSV...")
    def _load_bench_builtin(files: tuple[str, ...], _mtimes: tuple[float, ...]) -> pd.DataFrame:
        frames = []
        for f in files:
            try:
                df = pd.read_csv(f)
                parsed = parse_durability_data(df)
                frames.append(parsed)
                logger.info("解析内置台架 CSV: %s | %d 行", f, len(parsed))
            except Exception as e:
                logger.error("解析失败 %s: %s", f, e)
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

    builtin_parsed = pd.DataFrame()
    import os
    if csv_files:
        _mtimes = tuple(os.path.getmtime(str(f)) for f in csv_files)
        builtin_parsed = _load_bench_builtin(tuple(str(f) for f in csv_files), _mtimes)

    # ---------- ② 上传的台架数据(不走 cache,每次都解析,避免 session_state 变了缓存不更新) ----------
    uploaded_parsed_list: list[pd.DataFrame] = []
    if uploaded_bench_frames:
        for _i, _udf in enumerate(uploaded_bench_frames):
            try:
                _p = parse_durability_data(_udf)
                uploaded_parsed_list.append(_p)
                logger.info("解析上传台架数据[%d] | %d 行", _i, len(_p))
            except Exception as e:
                logger.warning("上传台架数据[%d]解析失败: %s", _i, e, exc_info=True)
                st.warning(f"上传台架文件 #{_i+1} 解析失败: {e}")

    # ---------- ③ 合并 + 聚合 ----------
    all_frames: list[pd.DataFrame] = []
    if not builtin_parsed.empty:
        all_frames.append(builtin_parsed)
    if uploaded_parsed_list:
        all_frames.extend(uploaded_parsed_list)

    if not all_frames:
        st.warning("所有台架数据源解析后均为空,请检查文件内容格式。")
        return

    merged_all = pd.concat(all_frames, ignore_index=True)
    agg_df = aggregate_durability_stats(merged_all, _SIGNAL_COLS)
    _parts_msg = []
    if not builtin_parsed.empty:
        _parts_msg.append(f"内置 {len(builtin_parsed):,} 行")
    if uploaded_parsed_list:
        _u_rows = sum(int(len(x)) for x in uploaded_parsed_list)
        _parts_msg.append(f"上传 {_u_rows:,} 行")
    st.success(f"已聚合 {len(agg_df)} 组 (cycle × power_point) · {' + '.join(_parts_msg)}")

    filter_opts = render_durability_filter()

    # 信号筛选(企业需求:signal_columns 多选) → 视图 1/2 用用户选的信号,视图 3 固定 4 图
    user_signals = filter_opts.get('signal_columns') or _SIGNAL_COLS
    # 功率筛选(selected_powers 已从 render_durability_filter 返回 power_points)
    sel_powers = (filter_opts.get('power_points')
                  or filter_opts.get('selected_powers')
                  or [])
    render_durability_chart(
        agg_df, user_signals,      # 功率筛选 + 信号筛选 均从筛选栏返回
        sel_powers,
        filter_opts.get('agg_method', 'mean'),
    )

    st.markdown("---")
    st.subheader("预警历史记录")

    # ============================================================
    # 🚨 预警检测与飞书推送(功能4核心要求:真正发消息,不再测试模式)
    # ============================================================
    # ---------- ① 用户可配置预警阈值(企业默认值 50mV / 600mV) ----------
    with st.expander("⚙️ 预警阈值与推送配置（企业默认值已填）", expanded=True):
        cfg_cols = st.columns(4)
        with cfg_cols[0]:
            _dev_thresh = st.number_input(
                "离均差上限(mV)", min_value=5.0, max_value=500.0, value=50.0, step=5.0,
                help="FC_AvgCellVoltDev 高于此值触发预警(企业标准:50mV)",
            )
        with cfg_cols[1]:
            _avg_thresh = st.number_input(
                "平均单体电压下限(mV)", min_value=100.0, max_value=1200.0, value=600.0, step=25.0,
                help="FC_AvgCellVoltage 低于此值且>0触发预警(企业标准:600mV)",
            )
        with cfg_cols[2]:
            _rig_id = st.text_input(
                "台架编号", value="台架A",
                help="用于飞书消息里的台架标识(如 台架A / 测试台-01)",
            )
        with cfg_cols[3]:
            _enable_push = st.checkbox(
                "启用飞书推送", value=True,
                help="关闭后仅记录预警事件到数据库,不发送飞书(调试用)",
            )

    # ---------- ② 命中预警事件(用用户配置的阈值) ----------
    raw_alert_events: list[dict] = []
    if not agg_df.empty:
        dev_col = [c for c in agg_df.columns if 'AvgCellVoltDev' in c and 'mean' in c]
        avg_col = [c for c in agg_df.columns if 'AvgCellVoltage' in c and 'mean' in c]
        cnt_col = '数据量' if '数据量' in agg_df.columns else None
        qual_col = '质量标记' if '质量标记' in agg_df.columns else None

        logger.info(
            "[预警检测] 开始扫描 agg_df: rows=%d dev_col=%s avg_col=%s "
            "阈值=离均差>%.0fmV 电压<%.0fmV",
            len(agg_df), dev_col, avg_col, _dev_thresh, _avg_thresh,
        )
        n_hits_dev = 0
        n_hits_avg = 0
        for _, row in agg_df.iterrows():
            ts = datetime.now()
            cyc = int(row.get('cycle_id', 0))
            pp = float(row.get('power_point', 0))
            cnt = int(row.get(cnt_col, 0)) if cnt_col else 0
            qual = str(row.get(qual_col, '正常')) if qual_col else '正常'

            if dev_col:
                dev = float(row[dev_col[0]]) if pd.notna(row[dev_col[0]]) else 0
                if dev > _dev_thresh:
                    n_hits_dev += 1
                    raw_alert_events.append({
                        'timestamp': ts, 'cycle_id': cyc, 'power_point': pp,
                        'condition': f'离均差>{_dev_thresh:.0f}mV',
                        'value': dev, 'threshold': float(_dev_thresh),
                        'signal': 'FC_AvgCellVoltDev', 'unit': 'mV', 'operator': '>',
                        'label': '离均差', 'data_count': cnt, 'quality': qual,
                        'message': f"离均差>{_dev_thresh:.0f}mV: {dev:.1f}mV > {_dev_thresh:.0f}mV",
                    })
            if avg_col:
                avg_v = float(row[avg_col[0]]) if pd.notna(row[avg_col[0]]) else 0
                if 0 < avg_v < _avg_thresh:
                    n_hits_avg += 1
                    raw_alert_events.append({
                        'timestamp': ts, 'cycle_id': cyc, 'power_point': pp,
                        'condition': f'平均单体电压<{_avg_thresh:.0f}mV',
                        'value': avg_v, 'threshold': float(_avg_thresh),
                        'signal': 'FC_AvgCellVoltage', 'unit': 'mV', 'operator': '<',
                        'label': '平均单体电压', 'data_count': cnt, 'quality': qual,
                        'message': (
                            f"平均单体电压<{_avg_thresh:.0f}mV: "
                            f"{avg_v:.1f}mV < {_avg_thresh:.0f}mV"
                        ),
                    })

        logger.info(
            "[预警检测] 扫描完成: 总行数=%d 命中离均差=%d 命中电压=%d 合计命中=%d",
            len(agg_df), n_hits_dev, n_hits_avg, len(raw_alert_events),
        )

    # ---------- ③ 幂等 + 真实 DB 写入 + 飞书推送 ----------
    from durability.database import (
        db_save_event, db_get_event_status_map, db_set_event_status,
        db_log_push, db_get_verified_contacts,
    )

    # session 级幂等:避免 Streamlit 页面 rerun 时同一事件被反复推送
    if "_bench_push_sent_ids" not in st.session_state:
        st.session_state["_bench_push_sent_ids"] = set()
    session_sent_ids: set[str] = st.session_state["_bench_push_sent_ids"]
    logger.info(
        "[台架预警流程] 进入推送流程:命中事件=%d 阈值=离均差>%.0fmV/单体<%.0fmV "
        "飞书推送=%s session已标记发送=%d条",
        len(raw_alert_events), _dev_thresh, _avg_thresh,
        "ON" if _enable_push else "OFF", len(session_sent_ids),
    )

    display_events: list[dict] = []
    if raw_alert_events:
        # 先逐个写库,用 db_save_event 返回的真实 event_id(含 ts YmdHMS),
        # 不自己猜算法,避免和数据库 _make_event_id 对不上
        ev_ids: list[str] = []
        for ev_idx, ev in enumerate(raw_alert_events, 1):
            _ev_desc = (
                f"[{ev_idx}/{len(raw_alert_events)}] "
                f"cycle={ev.get('cycle_id')} pp={ev.get('power_point',0):.1f}kW "
                f"{ev.get('condition')} value={ev.get('value',0):.1f}{ev.get('unit','mV')}"
            )
            try:
                t_save = time.perf_counter()
                real_eid = db_save_event(ev)  # INSERT OR IGNORE,天然幂等
                dt_save_ms = (time.perf_counter() - t_save) * 1000
                ev_ids.append(real_eid)
                logger.info(
                    "[台架预警流程] 步骤1 写alert_events: %s → eid=%s | 耗时=%.0fms",
                    _ev_desc, (real_eid[:30] + "…") if len(real_eid) > 30 else real_eid,
                    dt_save_ms,
                )
            except Exception as e:
                logger.error(
                    "[台架预警流程] 步骤1 写alert_events失败: %s err=%s",
                    _ev_desc, e, exc_info=True,
                )
                ev_ids.append("")  # 失败占位
        valid_ids = [e for e in ev_ids if e]
        status_map = db_get_event_status_map(valid_ids)
        logger.info(
            "[台架预警流程] 步骤1.5 拉取DB状态: 共%d个有效eid,status_map=%s",
            len(valid_ids), status_map,
        )

        # 哪些事件需要推送:DB里状态 != sent,且 session 级未标记已推送过
        events_to_push: list[tuple[str, dict]] = []
        skip_reason_stats: dict[str, int] = {"写库失败": 0, "已sent": 0, "session已标记": 0, "推送开关OFF": 0, "待推送": 0}
        for eid, ev in zip(ev_ids, raw_alert_events):
            if not eid:
                # 写库失败的事件跳过推送,但仍展示给用户
                skip_reason_stats["写库失败"] += 1
                display_ev = dict(ev)
                display_ev["event_id"] = "<写库失败>"
                display_ev["db_status"] = "failed"
                display_ev["sent"] = False
                display_ev["send_error"] = "❌ 写数据库失败,详见日志"
                display_events.append(display_ev)
                continue
            db_status = status_map.get(eid, "pending")
            already_sent_in_session = eid in session_sent_ids
            needs_push = (
                _enable_push
                and not already_sent_in_session
                and db_status not in ("sent",)  # sent 不再重发; pending/failed/partial 都重试
            )
            if needs_push:
                events_to_push.append((eid, ev))
                skip_reason_stats["待推送"] += 1
            elif not _enable_push:
                skip_reason_stats["推送开关OFF"] += 1
            elif db_status == "sent":
                skip_reason_stats["已sent"] += 1
            elif already_sent_in_session:
                skip_reason_stats["session已标记"] += 1
            # 准备展示字段(先默认用数据库状态)
            display_ev = dict(ev)
            display_ev["event_id"] = eid
            display_ev["db_status"] = db_status
            display_ev["sent"] = db_status in ("sent", "partial")
            display_ev["send_error"] = "已推送成功" if db_status == "sent" else (
                "部分成功" if db_status == "partial" else (
                    f"推送失败:DB状态={db_status}" if db_status == "failed" else
                    ("推送已关闭" if not _enable_push else "等待推送")
                )
            )
            display_events.append(display_ev)

        # ---------- 打印推送判定汇总日志(判断"为什么没推"最关键的一行) ----------
        logger.info(
            "[台架预警流程] 步骤2 推送判定汇总: 命中=%d 写库失败=%d 推送开关OFF=%d "
            "DB已sent=%d session已标记=%d → 真正进入推送链路=%d条 | 明细=%s",
            len(raw_alert_events),
            skip_reason_stats["写库失败"], skip_reason_stats["推送开关OFF"],
            skip_reason_stats["已sent"], skip_reason_stats["session已标记"],
            len(events_to_push), str(skip_reason_stats),
        )

        # 真正推送: 统一走 feishu_contacts.send_alert_to_contacts
        if events_to_push:
            from durability.feishu_contacts import send_alert_to_contacts
            # 提前查一次已验证联系人,给用户看数量提示
            t_vc = time.perf_counter()
            verified_contacts = db_get_verified_contacts()
            dt_vc_ms = (time.perf_counter() - t_vc) * 1000
            enabled_cnt = sum(1 for c in verified_contacts if c.get("enabled"))
            verified_names = [
                f"{str(c.get('name','?'))}({('启用' if c.get('enabled') else '禁用')}"
                f" verified={bool(c.get('verified'))})"
                for c in verified_contacts
            ]
            logger.info(
                "[台架预警流程] 步骤3 联系人预览: 已拉取%d人(enabled=%d) | 耗时=%.0fms "
                "名单=%s | 待推送事件=%d条 rig_id=%s",
                len(verified_contacts), enabled_cnt, dt_vc_ms,
                verified_names, len(events_to_push), _rig_id,
            )
            push_status_placeholder = st.empty()
            push_status_placeholder.warning(
                f"📨 正在推送 {len(events_to_push)} 条预警给"
                f" {enabled_cnt} 位已验证联系人(总配置{len(verified_contacts)}位), "
                f"飞书限速 0.5s/人/条,请稍候..."
            )

            for ev_step, (eid, ev) in enumerate(events_to_push, 1):
                eid_short = eid[:30] + ("…" if len(eid) > 30 else "")
                logger.info(
                    "[台架预警流程] 步骤4 事件[%d/%d]调用飞书推送: eid=%s "
                    "cycle=%s pp=%.1fkW cond=%s",
                    ev_step, len(events_to_push), eid_short,
                    ev.get("cycle_id"), float(ev.get("power_point", 0)),
                    ev.get("condition"),
                )
                t_ev = time.perf_counter()
                try:
                    push_results = send_alert_to_contacts(ev, rig_id=_rig_id)
                except Exception as e:
                    dt_ev_ms = (time.perf_counter() - t_ev) * 1000
                    logger.exception(
                        "[台架预警流程] 步骤4 事件[%d/%d]推送顶层异常(捕获降级为[]): "
                        "eid=%s 耗时=%.0fms err=%s",
                        ev_step, len(events_to_push), eid_short, dt_ev_ms, e,
                    )
                    push_results = []
                dt_ev_ms = (time.perf_counter() - t_ev) * 1000
                logger.info(
                    "[台架预警流程] 步骤4 事件[%d/%d]推送返回: eid=%s 共返回%d条结果 | 耗时=%.0fms",
                    ev_step, len(events_to_push), eid_short,
                    len(push_results), dt_ev_ms,
                )

                # 写推送日志(每个联系人一条)
                success_count = 0
                t_all_log = time.perf_counter()
                for pr_idx, pr in enumerate(push_results, 1):
                    cname = str(pr.get("name", "?"))
                    ok = bool(pr.get("success", False))
                    msg_trunc = str(pr.get("message", ""))[:80]
                    try:
                        t_lp = time.perf_counter()
                        db_log_push(
                            event_id=eid,
                            contact_id=str(pr.get("contact_id", "")),
                            contact_name=cname,
                            success=ok,
                            message=str(pr.get("message", "")),
                        )
                        dt_lp_ms = (time.perf_counter() - t_lp) * 1000
                        logger.info(
                            "[台架预警流程] 步骤5 写alert_push_log [%d/%d] eid=%s "
                            "contact=%s success=%s | 耗时=%.0fms | msg=%s",
                            pr_idx, len(push_results), eid_short, cname,
                            "✅OK" if ok else "❌FAIL", dt_lp_ms, msg_trunc,
                        )
                        if ok:
                            success_count += 1
                    except Exception as e:
                        logger.error(
                            "[台架预警流程] 步骤5 写alert_push_log失败 [%d/%d] "
                            "eid=%s contact=%s err=%s",
                            pr_idx, len(push_results), eid_short, cname, e,
                            exc_info=True,
                        )
                dt_all_log_ms = (time.perf_counter() - t_all_log) * 1000
                logger.info(
                    "[台架预警流程] 步骤5 推送日志写入完成: 共%d条 | 成功=%d 失败=%d | 总耗时=%.0fms",
                    len(push_results), success_count,
                    len(push_results) - success_count, dt_all_log_ms,
                )

                # 汇总推送状态,写回 alert_events.status
                total_push = len(push_results)
                if total_push == 0:
                    new_status = "failed"
                elif success_count == total_push:
                    new_status = "sent"
                elif success_count > 0:
                    new_status = "partial"
                else:
                    new_status = "failed"
                try:
                    t_status = time.perf_counter()
                    db_set_event_status(eid, new_status)
                    dt_status_ms = (time.perf_counter() - t_status) * 1000
                    logger.info(
                        "[台架预警流程] 步骤6 状态回写alert_events: eid=%s "
                        "%s→%s (成功=%d/%d) | 耗时=%.0fms",
                        eid_short, status_map.get(eid, "pending"),
                        new_status, success_count, total_push, dt_status_ms,
                    )
                except Exception as e:
                    logger.error(
                        "[台架预警流程] 步骤6 状态回写失败 eid=%s -> %s err=%s",
                        eid_short, new_status, e, exc_info=True,
                    )

                # session 级标记已推送(防止 rerun 再次触发)
                st.session_state["_bench_push_sent_ids"].add(eid)

                # 更新 display_events 中这条的展示字段
                for dev in display_events:
                    if dev.get("event_id") == eid:
                        dev["db_status"] = new_status
                        dev["sent"] = new_status in ("sent", "partial")
                        dev["push_summary"] = f"{success_count}/{total_push} 成功"
                        if new_status == "sent":
                            dev["send_error"] = f"✅ 飞书推送成功 {success_count}/{total_push}"
                        elif new_status == "partial":
                            dev["send_error"] = f"⚠️ 部分成功 {success_count}/{total_push}"
                        else:
                            fails = [f"{p.get('name','?')}:{p.get('message','')}"
                                     for p in push_results if not p.get("success")]
                            dev["send_error"] = (
                                f"❌ 推送失败 {success_count}/{total_push}: "
                                + "; ".join(fails[:2])
                            )
                        break

            push_status_placeholder.empty()

    # ---------- ④ 页面展示 ----------
    if raw_alert_events:
        st.caption(
            f"检测到 {len(raw_alert_events)} 条预警事件 · "
            f"阈值:离均差>{_dev_thresh:.0f}mV / 平均单体电压<{_avg_thresh:.0f}mV · "
            f"飞书推送:{'✅ 启用' if _enable_push else '⛔ 已关闭(仅入库)'}"
        )
        # 推送结果摘要条
        _sum_cols = st.columns(4)
        statuses = [d.get("db_status", "pending") for d in display_events]
        _sum_cols[0].metric(f"🎯 预警总数", f"{len(display_events)}")
        _sum_cols[1].metric(f"✅ 已推送(sent)", f"{statuses.count('sent')}")
        _sum_cols[2].metric(f"⚠️ 部分成功(partial)", f"{statuses.count('partial')}")
        _sum_cols[3].metric(f"❌ 失败/待处理(failed/pending)",
                           f"{statuses.count('failed') + statuses.count('pending')}")
        render_alert_log(display_events)
    else:
        st.success(
            f"✅ 当前数据无预警事件 · "
            f"阈值:离均差>{_dev_thresh:.0f}mV / 平均单体电压<{_avg_thresh:.0f}mV"
        )


@tab_safe_render
def _render_tab_contacts() -> None:
    """Tab11: 飞书人员对接。"""
    from components.feishu_contacts import render_feishu_contacts
    render_feishu_contacts()


@st.cache_data(ttl=30, show_spinner=False)
def _cached_upload_summary(_backend_tag: str) -> dict:
    """缓存上传汇总(30秒 TTL,_backend_tag 用于后端切换时失效)。"""
    return db_get_upload_summary()


@st.cache_data(ttl=30, show_spinner=False)
def _cached_data_files(_backend_tag: str, data_kind: str | None, limit: int) -> list:
    """缓存文件列表(30秒 TTL)。"""
    return db_list_data_files_paginated(data_kind=data_kind, limit=limit, offset=0)


@tab_safe_render
def _render_tab_history() -> None:
    """Tab12: 上传历史记录及数据回看(优化版:缓存+按需加载)。"""
    st.header("📁 上传历史记录")
    st.caption("查看所有已上传的数据文件历史,支持按类型筛选和数据回看")

    _backend = get_db_backend_info().get("backend", "unknown")
    _tag = f"{_backend}"

    # === 1. 汇总卡片 (30秒缓存) ===
    summary = _cached_upload_summary(_tag)
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("总文件数", summary['total_files'])
    with col2:
        st.metric("总行数", f"{summary['total_rows']:,}")
    with col3:
        kinds = summary.get('by_kind', {})
        st.metric("数据类型", len(kinds))
    with col4:
        latest = summary.get('latest_upload', '')
        st.metric("最新上传", latest or "暂无")

    # 按类型分组
    if kinds:
        _cols = st.columns(min(len(kinds), 4))
        kind_icons = {'整车': '🚗', '耐久工步': '📉', '台架循环': '🔬'}
        for i, (kind, info) in enumerate(kinds.items()):
            with _cols[i % len(_cols)]:
                icon = kind_icons.get(kind, '📄')
                st.markdown(f"**{icon} {kind}**")
                st.caption(f"{info['count']} 个文件 · {info['rows']:,} 行")

    # 按车辆分组
    by_vehicle = summary.get('by_vehicle', {})
    if by_vehicle:
        with st.expander(f"按车辆统计 ({len(by_vehicle)} 辆车)", expanded=False):
            for vehicle_id, info in by_vehicle.items():
                st.markdown(f"- **车辆 {vehicle_id}**: {info['files']} 个文件 · {info['rows']:,} 行")

    st.divider()

    # === 2. 文件列表 (30秒缓存) ===
    filter_col1, filter_col2 = st.columns([2, 1])
    with filter_col1:
        kind_filter = st.selectbox(
            "按类型筛选",
            options=["全部", "整车", "耐久工步", "台架循环"],
            index=0,
            key="history_kind_filter",
        )
    with filter_col2:
        page_size = st.selectbox("每页显示", [10, 20, 50], index=1, key="history_page_size")

    kind_param = None if kind_filter == "全部" else kind_filter
    files = _cached_data_files(_tag, kind_param, page_size)

    if not files:
        st.info("暂无上传记录。请前往首页「上传文件」或使用「内置数据」模式导入数据。")
        return

    # 文件列表表格
    display_df = pd.DataFrame(files)
    show_cols = ['id', 'data_kind', 'vehicle_id', 'file_name', 'row_count', 'uploaded_at', 'status']
    show_cols = [c for c in show_cols if c in display_df.columns]
    display_df = display_df[show_cols]
    col_names = {
        'id': 'ID', 'data_kind': '类型', 'vehicle_id': '车辆',
        'file_name': '文件名', 'row_count': '行数',
        'uploaded_at': '上传时间', 'status': '状态',
    }
    display_df = display_df.rename(columns={k: v for k, v in col_names.items() if k in show_cols})
    st.dataframe(display_df, use_container_width=True, hide_index=True)

    # === 3. 数据回看 (按需加载,不自动查询) ===
    st.subheader("📂 数据回看")
    sel_idx = st.selectbox(
        "选择文件",
        options=range(len(files)),
        format_func=lambda i: f"{files[i].get('file_name', '?')} (ID:{files[i].get('id')})",
        index=0,
        key="history_file_selector",
    )

    sel_file = files[sel_idx] if sel_idx is not None else None
    if sel_file:
        col1, col2 = st.columns([1, 2])
        with col1:
            st.markdown("**文件信息:**")
            st.json({
                '类型': sel_file.get('data_kind'),
                '车辆': sel_file.get('vehicle_id'),
                '文件名': sel_file.get('file_name'),
                '行数': sel_file.get('row_count'),
                '上传时间': sel_file.get('uploaded_at'),
                '状态': sel_file.get('status'),
            })
        with col2:
            kind = sel_file.get('data_kind', '')
            vehicle_id = sel_file.get('vehicle_id', '')

            # 按需加载:点击按钮才查数据
            if kind == '整车' and vehicle_id:
                if st.button("加载该车辆历史数据", key="load_vehicle_hist", type="primary"):
                    with st.spinner("正在加载..."):
                        hist_df = db_load_vehicle_minute_preview(vehicle_id, limit=100)
                    if len(hist_df) > 0:
                        st.dataframe(hist_df, use_container_width=True, hide_index=True)
                        st.caption(f"显示最近 100 条分钟数据(按时间倒序)")
                    else:
                        st.info("该车辆暂无分钟级数据")

            elif kind == '耐久工步':
                if st.button("加载耐久工步数据", key="load_dur_hist", type="primary"):
                    with st.spinner("正在加载..."):
                        dur_df = db_load_durability_stages()
                    if len(dur_df) > 0:
                        st.dataframe(dur_df.head(100), use_container_width=True, hide_index=True)
                        st.caption(f"共 {len(dur_df)} 条,显示前 100 条")
                    else:
                        st.info("暂无耐久工步数据")

            elif kind == '台架循环':
                if st.button("加载台架循环数据", key="load_bench_hist", type="primary"):
                    with st.spinner("正在加载..."):
                        bench_df = db_load_bench_cycle_stats()
                    if len(bench_df) > 0:
                        st.dataframe(bench_df.head(100), use_container_width=True, hide_index=True)
                        st.caption(f"共 {len(bench_df)} 条,显示前 100 条")
                    else:
                        st.info("暂无台架循环数据")
            else:
                st.info("暂无可回看的数据类型")

    st.divider()
    st.caption(f"数据来源: {_backend} | 共 {len(files)} 条记录 | 缓存30秒")


@tab_safe_render
def _render_tab_compare(
    cars: list[str],
    data: dict[str, pd.DataFrame],
) -> None:
    """Tab5: 多车对比。"""
    if not cars:
        st.info("暂无车辆数据。")
        return

    cmp_mode = st.radio(
        "对比模式",
        ["多车横向对比", "同车前后对比"],
        index=0,
        horizontal=True,
        help="多车横向:多辆车同指标叠加; 同车前后:同一辆车两个时段叠加",
    )
    cmp_col = st.selectbox(
        "对比指标",
        # 选项1: 企业 9 个核心字段(按 SIGNAL_MAP 顺序)
        [
            "FC_CurrOut", "FC_VoltOut", "FC_NetPwrOut",
            "FC_MinCellVoltage", "FC_AvgCellVoltage",
            "FC_AvgCellVoltDev", "FC_VehicleIsolationR",
            "FC_RunTime_Hours",
            # 选项2: 扩展常用辅助字段
            "FC_VehicleSpd",
        ],
        index=2,   # 默认系统净功率输出(企业关注核心)
        format_func=lambda c: (
            SIGNAL_MAP.get(c, c)
            if c in SIGNAL_MAP
            else {"FC_VehicleSpd": "车辆车速 (km/h)"}.get(c, c)
        ),
    )

    if cmp_mode == "多车横向对比":
        sel_cars = st.multiselect(
            "选择车辆 (可多选)", cars, default=cars[:2],
            key="cmp_cars",
        )
        if len(sel_cars) < 2:
            st.info("多车横向对比至少选 2 辆车。")
        else:
            dfs_sel = [data[c] for c in sel_cars]
            st.plotly_chart(
                fig_compare_overlay(dfs_sel, cmp_col, sel_cars),
                use_container_width=True,
            )
            keys = ["运行时长(h)", "行驶里程(km)", "平均车速(km/h)",
                    "百公里氢耗均值(kg)", "故障码种类"]
            rows = []
            ovs = {c: vehicle_overview(data[c]) for c in sel_cars}
            for k in keys:
                row = {"指标": k}
                for c in sel_cars:
                    row[c] = ovs[c].get(k, "-")
                rows.append(row)
            st.dataframe(pd.DataFrame(rows), use_container_width=True)
    else:
        import datetime as _dt
        car_one = st.selectbox("选择车辆", cars, key="cmp_before_after_car")
        df_one = data[car_one]
        if "Timestamp" not in df_one.columns or len(df_one) == 0:
            st.info("该车无 Timestamp 数据,无法做前后对比。")
            return
        t_min = df_one["Timestamp"].min().to_pydatetime()
        t_max = df_one["Timestamp"].max().to_pydatetime()
        mid = t_min + (t_max - t_min) / 2
        if isinstance(mid, _dt.timedelta):
            mid = t_min + mid
        st.caption(f"该车数据区间: {t_min} → {t_max}")
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**前段区间**")
            b_lo = st.datetime_input("前段起始", value=t_min, key="ba_lo")
            b_hi = st.datetime_input("前段结束", value=mid, key="ba_hi")
        with c2:
            st.markdown("**后段区间**")
            a_lo = st.datetime_input("后段起始", value=mid, key="a_lo")
            a_hi = st.datetime_input("后段结束", value=t_max, key="a_hi")
        st.plotly_chart(
            fig_before_after_overlay(
                df_one, cmp_col, b_lo, b_hi, a_lo, a_hi),
            use_container_width=True,
        )
        seg_before = df_one[(df_one["Timestamp"] >= pd.Timestamp(b_lo))
                            & (df_one["Timestamp"] <= pd.Timestamp(b_hi))]
        seg_after = df_one[(df_one["Timestamp"] >= pd.Timestamp(a_lo))
                           & (df_one["Timestamp"] <= pd.Timestamp(a_hi))]
        keys = ["运行时长(h)", "行驶里程(km)", "平均车速(km/h)",
                "百公里氢耗均值(kg)", "故障码种类"]
        rows = []
        ov_b = vehicle_overview(seg_before)
        ov_a = vehicle_overview(seg_after)
        for k in keys:
            rows.append({
                "指标": k,
                "前段": ov_b.get(k, "-"),
                "后段": ov_a.get(k, "-"),
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True)


@tab_safe_render
def _render_tab_report(
    cars: list[str],
    data: dict[str, pd.DataFrame],
) -> None:
    """Tab6: 报告导出。"""
    st.subheader("一键生成测试报告")
    if st.button("生成 HTML 报告(浏览器 Ctrl+P 可打印为 PDF)", type="primary"):
        rep_car = cars[0]
        rep_df = data[rep_car]
        logger.info("=== 生成 HTML 报告: 车辆=%s 数据=%d 行 ===", rep_car, len(rep_df))
        try:
            rep_ov = vehicle_overview(rep_df)
            html = build_report_html(
                vehicle=rep_car,
                df=rep_df,
                overview=rep_ov,
                cell_consist=cell_voltage_consistency(rep_df),
                power=power_summary(rep_df),
                h2=h2_system(rep_df),
            )
            logger.info("HTML 报告生成完成: %d 字节", len(html))
            st.download_button(
                "下载 HTML 报告",
                data=html.encode("utf-8"),
                file_name=f"测试报告_{rep_car}.html",
                mime="text/html",
            )
            st.components.v1.html(html, height=800, scrolling=True)
        except Exception as e:
            logger.error("HTML 报告生成失败: %s", e, exc_info=True)
            st.error(f"报告生成失败: {e}")


@tab_safe_render
def _render_tab_ai(sel_car_default: str | None = None) -> None:
    """Tab10: AI 助手(全产品智能解答)。"""
    st.header("🤖 燃料电池数据分析 AI 助手")
    st.caption("全产品智能顾问：4 大核心功能操作流程 · 字段含义/单位 · 计算筛选逻辑 · 预警阈值说明 · Tab 导航")

    # ---------- LLM / 说明书状态 ----------
    try:
        from src.ai_assistant import load_llm_config, load_dictionary
        cfg = load_llm_config()
        has_dict = bool(load_dictionary())
        status_cols = st.columns(3)
        with status_cols[0]:
            if cfg:
                st.success(f"✅ 云端 LLM 已启用 ({cfg['model']})")
            else:
                st.warning("⚠️ 未配置 LLM,已降级为本地检索(仅返回说明书匹配段)")
        with status_cols[1]:
            if has_dict:
                st.success("✅ 数据说明书已加载")
            else:
                st.warning("⚠️ 说明书 docs/DATA_DICTIONARY.md 不存在")
        with status_cols[2]:
            st.info("💡 4 大核心功能 Tab 已排最前:①燃电运行 ②性能 ③绝缘 ④台架")
    except Exception as e:
        st.error(f"AI 模块加载失败: {e}")

    if "ai_messages" not in st.session_state:
        st.session_state.ai_messages = [
            {"role": "assistant",
             "content": "你好!我是产品全功能 AI 顾问,可以回答下列任何问题(示例):\n\n"
                        "**📌 操作流程类**\n"
                        "- 想看单体电压随时间变化,应该去哪个 Tab?步骤是什么?\n"
                        "- 我要分析 95A 稳态点的衰减趋势,怎么设置筛选条件?\n"
                        "- 上传 .docx 后,耐久工步数据在哪里看?\n\n"
                        "**📊 数据解读类**\n"
                        "- 离均差(FC_AvgCellVoltDev)是什么含义?数值大代表什么?\n"
                        "- 350 kΩ 和 250 kΩ 两条报警线分别代表什么等级?\n"
                        "- 电压字段单位是 V 还是 mV?为什么数字这么大?\n\n"
                        "**⚙️ 计算逻辑类**\n"
                        "- 稳态段是怎么筛选出来的?180秒规则是什么意思?\n"
                        "- 绝缘的有效值为什么要每10分钟取最小值?哪些值会被过滤?\n"
                        "- 台架 6 个功率点(33~195kW)具体是哪 6 档?\n\n"
                        "**🚨 预警说明类**\n"
                        "- 台架什么情况下会推飞书消息?推给谁?\n"
                        "- 绝缘阻值触碰 250kΩ 报警线意味着什么?该怎么办?\n"
                        "- 飞书密钥过期了,怎么在「飞书人员对接」Tab 重新验证?"}
        ]

    for msg in st.session_state.ai_messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    if prompt := st.chat_input("问任何关于4大功能操作/数据解读/预警说明/Tab导航的问题..."):
        st.session_state.ai_messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            with st.spinner("AI 思考中(结合4大功能手册+说明书)..."):
                try:
                    from src.ai_assistant import ask
                    ctx = {"当前查看车辆": sel_car_default} if sel_car_default else None
                    answer = ask(prompt, context=ctx)
                except Exception as e:
                    answer = f"AI 调用异常: {e}"
                st.markdown(answer)
                st.session_state.ai_messages.append(
                    {"role": "assistant", "content": answer})

    st.divider()
    st.subheader("⚡ 快捷问题(按分类点击)")
    quick_categories = {
        "🎯 Tab 导航(去哪个)": [
            "想看单体电压+电流双轴曲线,去哪个 Tab?步骤是什么?",
            "上传台架 CSV 后,分析结果在第几个 Tab 看?",
            "我传了个 .docx(耐久工步),应该看哪里?"
        ],
        "📊 功能1/2 解读": [
            "离均差(FC_AvgCellVoltDev)是什么?数值大会有什么影响?",
            "稳态段怎么算出来的?180秒 规则解释一下",
            "燃电极化曲线是什么意思?能判断什么?"
        ],
        "🔌 功能3 绝缘": [
            "350 kΩ 和 250 kΩ 两条报警线分别是什么含义?",
            "绝缘的有效值是怎么从原始数据算出来的?哪些坏值会被过滤?",
            "绝缘阻值预测触碰报警线多久,是怎么算出来的?"
        ],
        "🏭 功能4 台架预警": [
            "台架耐久的 6 档标准功率点具体是哪 6 个?",
            "台架触发飞书预警的具体条件是什么?阈值是多少?",
            "飞书预警推送给哪些人?怎么新增/修改联系人?"
        ],
    }
    for cat, questions in quick_categories.items():
        st.markdown(f"**{cat}**")
        cols = st.columns(len(questions))
        for i, q in enumerate(questions):
            if cols[i].button(q, key=f"q_{cat}_{i}", use_container_width=True):
                st.session_state.ai_messages.append({"role": "user", "content": q})
                with st.chat_message("user"):
                    st.markdown(q)
                with st.chat_message("assistant"):
                    with st.spinner("AI 思考中..."):
                        try:
                            from src.ai_assistant import ask
                            answer = ask(q)
                        except Exception as e:
                            answer = f"AI 调用异常: {e}"
                        st.markdown(answer)
                        st.session_state.ai_messages.append(
                            {"role": "assistant", "content": answer})


@tab_safe_render
def _render_tab_forecast(
    cars: list[str],
    data: dict[str, pd.DataFrame],
) -> None:
    """Tab8: 趋势预测。"""
    st.header("📈 趋势预测")
    st.caption("基于历史数据线性回归预测未来走势,支持 7 项指标:压差/氢耗/故障频率/净功率/绝缘电阻/平均单体电压/离均差。")

    if not cars:
        st.warning("请先在侧边栏加载数据")
        return

    col_f1, col_f2 = st.columns(2)
    with col_f1:
        fc_car = st.selectbox("选择车辆", cars, key="forecast_car")
    with col_f2:
        future_h = st.select_slider(
            "预测时长",
            options=[1, 6, 24, 72, 168],
            value=24,
            format_func=lambda x: {1: "1 小时", 6: "6 小时",
                                   24: "24 小时", 72: "3 天",
                                   168: "7 天"}[x],
            key="forecast_hours",
        )

    df_fc = data[fc_car]
    st.caption(f"输入: {len(df_fc):,} 行 / 预测窗口: {future_h} 小时")

    if st.button("开始预测", type="primary", key="run_forecast"):
        with st.spinner("预测计算中..."):
            try:
                from src.forecast import forecast_all, fig_forecast
                results = forecast_all(df_fc, float(future_h))

                if not results:
                    st.warning("数据不足,无法预测。请确保所选车辆含 "
                               "FC_MaxCellVoltage/FC_MinCellVoltage/"
                               "FC_HydCmInstts/FC_ErrorCode/FC_NetPwrOut/"
                               "FC_VehicleIsolationR/FC_AvgCellVoltage/"
                               "FC_AvgCellVoltDev 等字段")
                else:
                    st.success(f"完成 {len(results)} / 7 项预测")
                    for r in results:
                        with st.expander(
                            f"{r.metric_name}  (斜率={r.slope:+.4f}/h, "
                            f"R²={r.r2:.3f})", expanded=True
                        ):
                            st.write(r.interpretation)
                            st.write(f"置信带宽: ±{1.96 * r.extra.get('resid_std', 0):.2f}")
                            fig = fig_forecast(r)
                            st.plotly_chart(fig, use_container_width=True)
            except Exception as e:
                logger.error("趋势预测失败: %s", e, exc_info=True)
                st.error(f"预测失败: {e}")


@tab_safe_render
def _render_tab_fc(
    data: dict[str, pd.DataFrame],
    fc_data_mode: str,
) -> None:
    """Tab9: 燃电运行看板(整合 filter_bar/stats/chart)。"""
    st.title("⚡ 燃电关键运行数据看板")
    st.caption("燃电核心运行信号实时监控 · 双Y轴可视化 · 异常自动标注")

    fc_state = render_filter_bar()
    vehicle_id = fc_state["vehicle_id"]
    start_dt = fc_state["start_time"]
    end_dt = fc_state["end_time"]
    selected_signals = fc_state["selected_signals"]

    use_mock = fc_data_mode.startswith("模拟")
    use_test_csv = fc_data_mode == "测试异常CSV"
    use_steady_csv = fc_data_mode == "稳态测试CSV(95A)"
    with st.spinner("正在加载数据..."):
        if use_mock:
            raw_fc = generate_mock_data(
                vehicle_id,
                pd.Timestamp(start_dt),
                pd.Timestamp(end_dt),
            )
        elif use_test_csv:
            _csv = Path(__file__).parent / "tests" / "fixtures" / "anomaly_test.csv"
            raw_fc = pd.read_csv(_csv) if _csv.exists() else pd.DataFrame()
        elif use_steady_csv:
            _csv = Path(__file__).parent / "tests" / "fixtures" / "steady_95a_test.csv"
            raw_fc = pd.read_csv(_csv) if _csv.exists() else pd.DataFrame()
        else:
            if vehicle_id in data and len(data[vehicle_id]):
                src_df = data[vehicle_id]
                if "Timestamp" in src_df.columns:
                    ts = pd.to_datetime(src_df["Timestamp"], errors="coerce")
                    mask = (ts >= pd.Timestamp(start_dt)) & (ts <= pd.Timestamp(end_dt))
                    raw_fc = src_df.loc[mask].copy()
                else:
                    raw_fc = src_df.copy()
            else:
                raw_fc = pd.DataFrame()

    if raw_fc is None or len(raw_fc) == 0:
        st.warning("所选时间范围内无数据,请调整范围或切换数据源")
        fig = create_figure(pd.DataFrame(), selected_signals)
        st.plotly_chart(fig, use_container_width=True)
    elif not selected_signals:
        st.info("请在上方选择至少一个信号")
        fig = create_figure(raw_fc, [])
        st.plotly_chart(fig, use_container_width=True)
    else:
        if use_test_csv:
            df_fc = raw_fc.copy()
        else:
            df_fc = filter_by_time(raw_fc, start_dt, end_dt)
        df_fc = resample_data(df_fc, "1S")
        if df_fc.index.name == "Timestamp":
            df_fc = df_fc.reset_index()
        else:
            df_fc = df_fc.reset_index(drop=True)
        df_fc = detect_anomalies(df_fc)

        st.session_state["fc_processed_df"] = df_fc

        if "is_anomaly" in df_fc.columns:
            cnt = int(df_fc["is_anomaly"].sum())
            if cnt > 0:
                st.toast(f"检测到 {cnt} 处异常事件!", icon="⚠️")

        render_stats(df_fc, selected_signals)

        mock_cmp, real_cmp = None, None
        if st.session_state.get('dq_compare'):
            if use_mock:
                mock_cmp = df_fc
                if vehicle_id in data and len(data[vehicle_id]):
                    src_q = data[vehicle_id]
                    if "Timestamp" in src_q.columns:
                        tsq = pd.to_datetime(src_q["Timestamp"], errors="coerce")
                        mq = (tsq >= pd.Timestamp(start_dt)) & (tsq <= pd.Timestamp(end_dt))
                        real_cmp = src_q.loc[mq].copy()
            else:
                real_cmp = df_fc
                mock_cmp = generate_mock_data(
                    vehicle_id, pd.Timestamp(start_dt), pd.Timestamp(end_dt))
        render_data_quality(df_fc, use_mock, vehicle_id, start_dt, end_dt,
                            real_df=real_cmp, mock_df=mock_cmp)

        fig = create_figure(df_fc, selected_signals)
        st.plotly_chart(fig, use_container_width=True)

    st.divider()
    if use_mock:
        src_label = "模拟数据(mock)"
    elif use_test_csv:
        src_label = "测试异常CSV"
    else:
        src_label = "真实数据"
    latest_ts = ""
    if (not use_mock) and (not use_test_csv) and vehicle_id in data \
            and len(data[vehicle_id]) and "Timestamp" in data[vehicle_id].columns:
        tcol = pd.to_datetime(data[vehicle_id]["Timestamp"], errors="coerce").dropna()
        if len(tcol):
            latest_ts = f" | 最新数据: {tcol.max():%Y-%m-%d %H:%M:%S}"
    st.caption(
        f"当前车辆: {vehicle_id} | 数据源: {src_label}{latest_ts} | "
        f"页面更新: {datetime.now():%Y-%m-%d %H:%M:%S}"
    )
    st.caption("© 2026 燃料电池监控系统 | 数据更新频率: 1s")

    _c1, _c2, _c3 = st.columns([8, 1, 2])
    if _c3.button("📄 导出报告", key="fc_export_btn", use_container_width=True):
        st.info("导出报告功能开发中(占位)")


@tab_safe_render
def _render_tab_performance(
    data: dict[str, pd.DataFrame],
    fc_data_mode: str,
) -> None:
    """Tab10: 燃电性能统计及趋势预测(稳态筛选→聚合→衰减→极化)。"""
    st.title("📈 燃电性能统计及趋势预测")
    st.caption("基于稳态工况筛选,分析电堆性能衰减趋势 · 极化曲线拟合 · 衰减速率")

    perf_cfg = render_performance_filter()
    if not perf_cfg["valid"]:
        st.info("请完成筛选条件(车辆+时间+电流点+最短持续时长)后继续。")
        return

    vehicle_id = perf_cfg["vehicle_id"]
    start_dt = perf_cfg["start_time"]
    end_dt = perf_cfg["end_time"]
    current_points = perf_cfg["current_points"]
    min_duration = perf_cfg["min_duration"]

    use_mock = fc_data_mode.startswith("模拟")
    use_test_csv = fc_data_mode == "测试异常CSV"
    use_steady_csv = fc_data_mode == "稳态测试CSV(95A)"
    with st.spinner("正在加载数据..."):
        if use_mock:
            raw_perf = generate_mock_data(
                vehicle_id, pd.Timestamp(start_dt), pd.Timestamp(end_dt))
        elif use_test_csv:
            _csv = Path(__file__).parent / "tests" / "fixtures" / "anomaly_test.csv"
            raw_perf = pd.read_csv(_csv) if _csv.exists() else pd.DataFrame()
        elif use_steady_csv:
            _csv = Path(__file__).parent / "tests" / "fixtures" / "steady_95a_test.csv"
            raw_perf = pd.read_csv(_csv) if _csv.exists() else pd.DataFrame()
        else:
            if vehicle_id in data and len(data[vehicle_id]):
                _src = data[vehicle_id]
                if "Timestamp" in _src.columns:
                    _ts = pd.to_datetime(_src["Timestamp"], errors="coerce")
                    _m = (_ts >= pd.Timestamp(start_dt)) & (_ts <= pd.Timestamp(end_dt))
                    raw_perf = _src.loc[_m].copy()
                else:
                    raw_perf = _src.copy()
            else:
                raw_perf = pd.DataFrame()

    if raw_perf is None or len(raw_perf) == 0:
        st.warning("所选时间范围内无数据,请调整范围或切换数据源")
        return

    df_perf = resample_data(raw_perf, "1S")
    if df_perf.index.name == "Timestamp":
        df_perf = df_perf.reset_index()
    else:
        df_perf = df_perf.reset_index(drop=True)

    all_segs = []
    progress = st.progress(0.0, "正在筛选稳态段...")
    for i, pt in enumerate(current_points):
        segs = find_steady_segments(
            df_perf, pt["target"], pt["tolerance"], min_duration)
        for s in segs:
            s["current_target"] = pt["target"]
        all_segs.extend(segs)
        progress.progress(
            (i + 1) / len(current_points),
            text=f"已分析 {pt['target']:.1f}±{pt['tolerance']:.1f}A "
                 f"({i + 1}/{len(current_points)}) 找到 {len(segs)} 段",
        )
    progress.empty()

    if not all_segs:
        st.warning(
            "未找到有效稳态段,请调整电流目标值/容差/最短持续时长,或扩大时间范围"
        )
        return

    # ---------- 动态信号聚合:Y 轴信号为主 + 辅助信号(电流/电压/功率/最低电压) ----------
    _base = ["FC_VoltOut", "FC_NetPwrOut", "FC_MinCellVoltage"]
    if perf_cfg["y_signal"] not in _base:
        _perf_sigs = [perf_cfg["y_signal"]] + _base
    else:
        _perf_sigs = list(dict.fromkeys([perf_cfg["y_signal"]] + _base))
    # 企业需求: 离均差 + 方差 两个信号同时出现在信号列表里(便于后续切换也能立刻显示)
    for ext in ("FC_AvgCellVoltDev", "FC_VARVoltage"):
        if ext not in _perf_sigs:
            _perf_sigs.append(ext)
    agg_df = aggregate_segments(
        all_segs, _perf_sigs, exclude_anomaly=False,
        warmup_seconds=perf_cfg["warmup_seconds"])

    if len(agg_df) == 0 or "duration" not in agg_df.columns:
        st.warning("聚合后无有效段(可能全部含异常被剔除)")
        return

    total_dur_h = float(agg_df["duration"].sum()) / 3600.0
    range_sec = max((pd.Timestamp(end_dt) - pd.Timestamp(start_dt)).total_seconds(), 1)
    coverage = min(float(agg_df["duration"].sum()) / range_sec * 100, 100.0)
    warmup_total = float(agg_df.get("warmup_dropped", pd.Series([0])).sum()) \
        if "warmup_dropped" in agg_df.columns else 0.0
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("📊 有效数据段", f"{len(agg_df)} 个")
    c2.metric("⏱️ 总有效时长", f"{total_dur_h:.2f} 小时")
    c3.metric("📈 数据覆盖率", f"{coverage:.1f}%")
    c4.metric("⚡ 电流点覆盖", f"{len(current_points)} 个")
    c5.metric("🔥 丢弃过渡热机", f"{warmup_total:.0f} s")

    st.success(
        f"分析完成!共找到 {len(all_segs)} 个有效数据段 "
        f"(稳态丢弃前 {perf_cfg['warmup_seconds']}s 过渡热机期)"
    )

    # ---------- 性能趋势图: Y 轴信号 + X 轴模式 + 多项式阶数 全部动态接入 ----------
    y_col_full = f'{perf_cfg["y_signal"]}_mean'
    x_map_label = {"run_time": "⏱ 累计运行时间 (h)", "datetime": "📅 实际日期"}
    st.markdown(
        f"#### 性能趋势 · 「{perf_cfg['y_label']}」 vs 「{x_map_label.get(perf_cfg['x_mode'], perf_cfg['x_mode'])}」"
        f" · 趋势线 {perf_cfg['poly_degree']}阶"
    )
    y_axis_label = f"{perf_cfg['y_label']} ({perf_cfg['y_unit']})"
    if y_col_full not in agg_df.columns:
        st.error(
            f"聚合结果中缺失列 `{y_col_full}` (当前 Y 轴信号={perf_cfg['y_signal']})。"
            f"可用列: {[c for c in agg_df.columns if c.endswith('_mean')]}"
        )
        return
    fig_perf = create_performance_figure(
        agg_df,
        x_col=perf_cfg["x_mode"],   # 'run_time' -> run_time_at_mid / 'datetime' -> mid_time
        y_col=y_col_full,
        group_col="current_target",
        degree=perf_cfg["poly_degree"],
        show_trend=True,
        y_label=y_axis_label,
    )
    st.plotly_chart(fig_perf, use_container_width=True)

    with st.expander("📋 有效段明细表", expanded=False):
        st.dataframe(agg_df.drop(columns=["segment_data"], errors="ignore"),
                     use_container_width=True, hide_index=True)

    with st.expander("📉 极化曲线拟合", expanded=False):
        _m = st.selectbox("拟合方法", ["empirical", "polynomial", "linear"],
                          index=0, key="polar_method")
        if "current_avg" in agg_df.columns and "FC_VoltOut_mean" in agg_df.columns \
                and len(agg_df) >= 2:
            pol = fit_polarization_curve(
                agg_df, current_col="current_avg",
                voltage_col="FC_VoltOut_mean", fit_method=_m)
            if pol["fit_success"]:
                fig_pol = create_polarization_figure(
                    agg_df, pol, current_col="current_avg",
                    voltage_col="FC_VoltOut_mean")
                st.plotly_chart(fig_pol, use_container_width=True)
                st.caption(f"公式: {pol['equation']}")
                st.dataframe(pd.DataFrame([pol["parameters"]]),
                             use_container_width=True)
            else:
                st.warning("极化曲线拟合失败,样本可能不足或数据不适合")
        else:
            st.info("需至少 2 个稳态段且含电压均值列才能拟合极化曲线")

    with st.expander("📊 衰减速率分析", expanded=False):
        if "run_time_at_mid" in agg_df.columns and len(agg_df) >= 2:
            deg = analyze_degradation(
                agg_df, y_col="FC_AvgCellVoltage_mean",
                time_col="run_time_at_mid", group_col="current_target",
            )
            if deg["summary_table"].shape[0] > 0:
                st.dataframe(deg["summary_table"], use_container_width=True,
                             hide_index=True)
                fig_deg = create_degradation_figure(
                    deg, agg_df, "FC_AvgCellVoltage_mean",
                    "run_time_at_mid", "current_target",
                    "平均单体电压 (V)",
                )
                st.plotly_chart(fig_deg, use_container_width=True)
                _emoji = {"green": "🟢", "yellow": "🟡", "red": "🔴"}
                for g in deg["groups"]:
                    if g.get("skip"):
                        continue
                    _acc = " · ⚠ 加速衰减" if g.get("is_accelerating") else ""
                    st.markdown(
                        f"{_emoji.get(g['health_status'], '⚪')} "
                        f"**{g['label']}** 健康度 {g['health_score']} 分"
                        f" ({g['health_status']}) · 衰减 "
                        f"{g['slope_mv_per_1000h']} mV/1000h · "
                        f"剩余寿命 {g['remaining_life_hours']} h{_acc}"
                    )
                st.caption("健康度:绿(80-100) 黄(60-80) 红(<60)。"
                           "衰减速率负值=性能下降;加速衰减=后段斜率更负。"
                           "剩余寿命:按平均单体电压阈值 3.0V 外推。")
            else:
                st.info("样本不足,无法拟合衰减趋势(需每组≥2个稳态段)")
        else:
            st.info("需含运行时间列且至少2个段才能分析衰减速率")

    with st.expander("📥 导出报告", expanded=False):
        _exp = agg_df.drop(columns=["segment_data"], errors="ignore")
        st.download_button(
            "⬇️ 导出段统计 CSV", _exp.to_csv(index=False).encode("utf-8"),
            file_name=f"performance_{vehicle_id}_{datetime.now():%Y%m%d_%H%M}.csv",
            mime="text/csv",
        )
        st.caption("PDF 报告导出功能开发中(占位)")


@tab_safe_render
def _render_tab_insulation(
    data: dict[str, pd.DataFrame],
    fc_data_mode: str,
) -> None:
    """Tab11: 绝缘阻值统计及预测。"""
    st.title("🔌 绝缘阻值统计及趋势预测")
    st.caption("基于10分钟最小值的绝缘健康度监控与报警预测 · 状态分布 · 寿命预测")

    ins_cfg = render_insulation_filter()
    if not ins_cfg["valid"]:
        st.info("请完成筛选条件(车辆+时间+阈值+预测天数)后继续。")
        return

    _vehicle = ins_cfg["vehicle_id"]
    _start = ins_cfg["start_time"]
    _end = ins_cfg["end_time"]
    _interval = ins_cfg["interval"]
    _primary = ins_cfg["primary_threshold"]
    _secondary = ins_cfg["secondary_threshold"]
    _forecast = ins_cfg["forecast_days"]
    _degree = ins_cfg["poly_degree"]

    use_mock = fc_data_mode.startswith("模拟")
    use_test_csv = fc_data_mode == "测试异常CSV"
    use_steady_csv = fc_data_mode == "稳态测试CSV(95A)"
    with st.spinner("正在加载绝缘数据..."):
        if use_mock:
            raw_insul = generate_mock_data(
                _vehicle, pd.Timestamp(_start), pd.Timestamp(_end))
        elif use_test_csv:
            _csv = Path(__file__).parent / "tests" / "fixtures" / "anomaly_test.csv"
            raw_insul = pd.read_csv(_csv) if _csv.exists() else pd.DataFrame()
        elif use_steady_csv:
            _csv = Path(__file__).parent / "tests" / "fixtures" / "steady_95a_test.csv"
            raw_insul = pd.read_csv(_csv) if _csv.exists() else pd.DataFrame()
        else:
            if _vehicle in data and len(data[_vehicle]):
                _src = data[_vehicle]
                if "Timestamp" in _src.columns:
                    _ts = pd.to_datetime(_src["Timestamp"], errors="coerce")
                    _m = (_ts >= pd.Timestamp(_start)) & (_ts <= pd.Timestamp(_end))
                    raw_insul = _src.loc[_m].copy()
                else:
                    raw_insul = _src.copy()
            else:
                raw_insul = pd.DataFrame()

    if raw_insul is None or len(raw_insul) == 0:
        st.warning("所选时间范围内无绝缘数据,请调整范围或切换数据源")
        return

    if "FC_MainSts" not in raw_insul.columns:
        raw_insul["FC_MainSts"] = 4
        st.info("ℹ 数据无 FC_MainSts 列,已默认按运行态(4)处理")

    df_insul = process_insulation_data(raw_insul, interval_minutes=_interval)

    if len(df_insul) == 0:
        st.warning("清洗后无有效绝缘数据(检查:绝缘值是否<=0/65535/>=9999,状态是否为4/8)")
        return

    n_valid = int(df_insul["FC_VehicleIsolationR"].notna().sum())

    # ---------- 坏值清洗摘要卡片(企业需求 65535/≥9999 坏值追踪) ----------
    clean_stats: dict = df_insul.attrs.get('clean_stats', {}) \
        if hasattr(df_insul, 'attrs') else {}
    raw_rows = int(clean_stats.get('raw_rows', len(raw_insul) if raw_insul is not None else 0))
    bad_65535 = int(clean_stats.get('bad_65535', 0))
    bad_ge9999 = int(clean_stats.get('bad_ge9999', 0))
    bad_le0 = int(clean_stats.get('bad_le0', 0))
    bad_state = int(clean_stats.get('bad_state', 0))
    kept_rows = int(clean_stats.get('kept_rows',
                                    (len(raw_insul) if raw_insul is not None else 0)
                                    - bad_65535 - bad_ge9999 - bad_le0 - bad_state))
    _bc1, _bc2, _bc3, _bc4, _bc5, _bc6 = st.columns(6)
    _bc1.metric("📥 原始总行数", f"{raw_rows:,}")
    _bc2.metric("✅ 保留有效行", f"{kept_rows:,}",
                delta=f"{kept_rows/raw_rows*100:.1f}%" if raw_rows else None)
    _bc3.metric("❌ ==65535 传感器故障", f"{bad_65535:,}",
                delta_color="inverse" if bad_65535 else "off")
    _bc4.metric("⚠️ ≥9999 溢出坏值", f"{bad_ge9999:,}",
                delta_color="inverse" if bad_ge9999 else "off")
    _bc5.metric("🔻 ≤0 或 NaN", f"{bad_le0:,}",
                delta_color="inverse" if bad_le0 else "off")
    _bc6.metric("🚫 非4/8状态行", f"{bad_state:,}",
                delta_color="inverse" if bad_state else "off")
    with st.expander("🧹 坏值清洗规则说明", expanded=False):
        st.markdown(
            "- `FC_VehicleIsolationR == 65535` → **传感器故障默认值**,直接剔除\n"
            "- `FC_VehicleIsolationR ≥ 9999` → **AD 采样溢出**,直接剔除\n"
            "- `FC_VehicleIsolationR ≤ 0` 或 `NaN` → **无效值**,直接剔除\n"
            "- `FC_MainSts ∉ {4, 8}` → **非运行/上电状态**,直接剔除\n"
            "- 清洗后剩余数据按 10 分钟窗口×状态(4/8)取最小绝缘值供趋势分析"
        )

    if n_valid < 20:
        st.warning(
            f"数据不足,无法进行趋势预测(至少需要20个有效点,当前{n_valid}个)"
        )
        return

    prediction = predict_insulation_trend(
        df_insul,
        alarm_values=[_primary, _secondary],
        predict_days=_forecast,
        poly_order=_degree,
    )

    render_insulation_stats(df_insul, prediction)

    # ---------- 叠加原始散点(raw_df) + 坏值摘要 ----------
    st.markdown("#### 绝缘阻值趋势(原始散点按状态4/8分色 + 10min聚合 + 报警线 + 预测)")
    fig_insul = create_insulation_figure(
        df_insul,
        primary_alarm=_primary,
        secondary_alarm=_secondary,
        predict_days=_forecast,
        poly_order=_degree,
        raw_df=raw_insul,   # 企业需求: 叠加非聚合原始散点,按状态4/8分色透明显示
    )
    st.plotly_chart(fig_insul, use_container_width=True)

    _has_states = ("FC_MainSts" in raw_insul.columns
                   and raw_insul["FC_MainSts"].isin([4, 8]).any())
    state_res = analyze_state_distribution(raw_insul) if _has_states else {}

    col1, col2, col3 = st.columns(3)

    with col1:
        with st.expander("📊 状态分布对比", expanded=False):
            if state_res and (state_res.get("n_state4", 0) > 0
                              or state_res.get("n_state8", 0) > 0):
                fig_box = create_state_distribution_figure(raw_insul)
                st.plotly_chart(fig_box, use_container_width=True)
                tt = state_res.get("t_test", {})
                if tt.get("p_value") is not None:
                    _sig = "" if tt.get("significant") else "不"
                    st.caption(
                        f"t检验: p={tt['p_value']:.4f}({_sig}显著) · "
                        f"运行态阻值更低: {tt.get('state4_lower')}"
                    )
                    s4 = state_res.get("state4_stats", {})
                    s8 = state_res.get("state8_stats", {})
                    if s4 and s8:
                        st.caption(
                            f"运行态均值 {s4.get('mean', 0):.0f} kΩ · "
                            f"上电态均值 {s8.get('mean', 0):.0f} kΩ"
                        )
            else:
                st.info("无状态分布数据(可能只有单状态或无 FC_MainSts)")

    with col2:
        with st.expander("📋 异常事件(骤降)", expanded=False):
            drop_events = state_res.get("drop_events", []) if state_res else []
            if drop_events:
                st.dataframe(
                    pd.DataFrame(drop_events),
                    use_container_width=True,
                    hide_index=True,
                )
                _ds = state_res.get("drop_summary", {})
                st.caption(
                    f"共 {len(drop_events)} 次骤降事件(1小时内下降>200kΩ) · "
                    f"平均下降 {_ds.get('avg_drop', 0):.0f} kΩ"
                )
            else:
                st.info("未检测到骤降异常事件")

    with col3:
        with st.expander("📈 拟合质量", expanded=False):
            st.metric("R²", f"{prediction['r_squared']:.3f}")
            st.metric("RMSE", f"{prediction['rmse']:.1f} kΩ")
            st.metric("数据点数", n_valid)
            st.metric("衰减速率", f"{prediction['degradation_rate']:.2f} kΩ/天")
            st.caption(
                "R²>0.8 高置信 / >0.5 中 / <0.5 低(衰减趋势不显著)"
            )

    st.session_state['data'] = data
    with st.expander("🆚 多车绝缘对比", expanded=False):
        _all_cars = sorted(data.keys()) if data else []
        cmp_vehicles = st.multiselect(
            "选择对比车辆(至少2辆)",
            _all_cars,
            default=_all_cars[:2] if len(_all_cars) >= 2 else _all_cars,
            key='ins_cmp_vehicles',
            help="从内置数据中选择多辆车横向对比绝缘趋势 (自动扫描 02_整车数据处理 目录)",
        )
        if len(cmp_vehicles) >= 2:
            try:
                cmp_fig, cmp_result = create_vehicle_comparison(
                    cmp_vehicles, _start, _end,
                    alarm_values=[_primary, _secondary],
                )
                st.plotly_chart(cmp_fig, use_container_width=True)
                if cmp_result:
                    st.markdown("**对比汇总表**(按健康度升序,最差在上)")
                    st.dataframe(
                        generate_comparison_table(cmp_result),
                        use_container_width=True,
                        hide_index=True,
                    )
                    _emoji = {"green": "🟢", "yellow": "🟡", "red": "🔴"}
                    for r in cmp_result:
                        _hs = r.get('health_score', 0)
                        _st_color = ('green' if _hs >= 70
                                     else 'yellow' if _hs >= 40 else 'red')
                        _f = r.get('forecast_350', {})
                        _days = _f.get('days')
                        _days_str = f"{_days:.0f}天" if _days is not None else "永不"
                        st.markdown(
                            f"{_emoji.get(_st_color, '⚪')} "
                            f"**车辆 {r['vehicle_id']}** "
                            f"健康度 {_hs}/100 · "
                            f"当前 {r.get('current', 0):.0f} kΩ · "
                            f"衰减 {r.get('degradation_rate', 0):.2f} kΩ/天 · "
                            f"触碰350kΩ: {_days_str}"
                        )
                else:
                    st.info("所选车辆在时间范围内无有效绝缘数据")
            except Exception as _e:
                st.warning(f"多车对比失败: {_e}")
        else:
            st.info("请至少选择 2 辆车进行对比")

    st.caption(
        "© 2026 绝缘阻值统计及预测 · "
        "报警阈值可在筛选栏调整 · 预测基于多项式拟合,置信度取决于数据质量"
    )


# ============================================================
# 顶层 Tab 容器:只做懒加载函数调用 (Streamlit Cloud 冷启动不崩)
# ============================================================
# Tab 渲染调用顺序(与 st.tabs(...) 一一对应):
# [1-4 核心功能区] 企业四大功能优先
# [5-11 补充功能区] 辅助 + 系统配置
# ============================================================

# ---- 核心功能区(前4) ----
with tab_fc:                            # [1] 功能1:燃电关键运行数据显示
    _render_tab_fc(data, fc_data_mode)

with tab_perf:                          # [2] 功能2:燃电性能统计及预测
    _render_tab_performance(data, fc_data_mode)

with tab_insul:                         # [3] 功能3:绝缘阻值统计及预测
    _render_tab_insulation(data, fc_data_mode)

with tab_bench:                         # [4] 功能4:台架耐久数据统计及预警
    _render_tab_bench()

# ---- 补充功能区(后7) ----
with tab_overview:                      # [5] 整车数据汇总概览(辅助)
    _render_tab_overview(cars, data, time_range_preset)

with tab_dur:                           # [6] 耐久工步(docx)衰减分析(辅助)
    _render_tab_durability(dur_df)

with tab_forecast:                      # [7] 整车历史线性回归预测(辅助)
    _render_tab_forecast(cars, data)

with tab_cmp:                           # [8] 多车横向对比(辅助)
    _render_tab_compare(cars, data)

with tab_report:                        # [9] 报告一键导出(系统)
    _render_tab_report(cars, data)

with tab_ai:                            # [10] AI 智能解答(系统,全产品)
    _render_tab_ai(sel_car_default=cars[0] if cars else None)

with tab_contacts:                      # [11] 飞书预警联系人配置(系统)
    _render_tab_contacts()

with tab_history:                       # [12] 上传历史记录及数据回看(系统)
    _render_tab_history()
