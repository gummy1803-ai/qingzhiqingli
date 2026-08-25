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
    # ===== 上传历史: 删除 / 重命名 / 重排 =====
    db_delete_data_file,
    db_rename_data_file,
    db_get_data_file,
    db_ensure_display_order,
    db_swap_data_file_order,
    db_update_display_order_batch,
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

def _safe_print(*args, **kwargs):
    """Windows GBK 控制台安全 print: emoji 自动降级为 ASCII。"""
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        safe_args = []
        for a in args:
            if isinstance(a, str):
                safe_args.append(a.encode("ascii", errors="replace").decode("ascii"))
            else:
                safe_args.append(a)
        try:
            print(*safe_args, **kwargs)
        except Exception:
            pass


def _app_precheck_banner() -> None:
    """Streamlit 启动期, 在终端输出与 run_e2e.py 对齐的预检横幅。

    性能考虑 (Streamlit 每轮 rerun 都会重跑本脚本的顶层代码):
    - 车辆预检只做目录级元数据统计 (目录名 + CSV 数量), 不读任何 CSV 列内容
    - 飞书预检是纯 DB 读取 <100ms, 每轮跑也 OK

    真正的「0 占比 / 风险等级」全量扫描请在 Streamlit 页面的台架预警 Tab,
    或命令行运行 python scan_hyd_zero.py。
    """
    _p = _safe_print
    ROOT = Path(__file__).resolve().parent
    CSV_BASE = ROOT / "企业资料包02_氢质氢离" / "02_整车数据处理"
    bar = "-" * 70
    _p("\n" + bar)
    _p("  Streamlit 启动预检 · 整车目录自动识别 (新增车型 ← 这里自动看到)")
    _p(bar)
    if CSV_BASE.exists():
        car_dirs = sorted([d for d in CSV_BASE.iterdir() if d.is_dir()])
        _p(f"  扫描目录: {CSV_BASE}")
        _p(f"  自动识别车辆数: {len(car_dirs)}")
        header = f"  {'车辆':<10}{'CSV分片数':>12}  [OK] = 已被纳入 run_e2e / 页面下拉菜单"
        _p("  " + "-" * (len(header) + 8))
        _p(header)
        total_csv = 0
        for car_dir in car_dirs:
            files = sorted(car_dir.glob("*.csv"))
            total_csv += len(files)
            _p(f"  {car_dir.name:<10} {len(files):>12}  [OK]")
        _p("-" * 20)
        _p(f"  合计 CSV 分片: {total_csv}")
    else:
        _p(f"  [!] 内置 CSV 目录不存在: {CSV_BASE}")
        _p("     可在 Streamlit 侧边栏选择「上传文件」模式导入。")

    # ---------- 飞书联系人预检 ----------
    try:
        from durability.feishu_contacts import (
            list_contacts as _feishu_list,
            detect_all_credentials_status as _detect_creds,
            credentials_status_text as _creds_text,
        )
        contacts = _feishu_list()
        info = get_db_backend_info()
        _p(bar)
        _p("  Streamlit 启动预检 · 飞书人员对接 (新增联系人 ← 这里自动看到)")
        _p(bar)
        _p(f"  存储后端: {info.get('backend_display', info.get('backend', 'N/A'))}")
        if info.get("backend") == "mysql":
            _p(f"  Host: {info.get('host')}:{info.get('port')}  DB: {info.get('dbname')}  User: {info.get('user')}")
            _p(f"  [LOOP] 降级: MySQL 外网断开会自动切 SQLite (终端/日志会打印 [DB 降级] 横幅)")
        _p("-" * 30)
        if not contacts:
            _p("  [!] 还没有任何飞书联系人 → 进入 [FEISHU] 飞书人员对接 Tab 新增")
        else:
            verified_cnt = sum(1 for c in contacts if c.get("verified"))
            enabled_cnt = sum(1 for c in contacts if c.get("enabled", True))
            _p(f"  总联系人: {len(contacts)} | 启用: {enabled_cnt} | 已验证(飞书推送绿灯): {verified_cnt}")
            _p("-" * 10 + " [KEY] 密钥预检开始 (超时8秒自动跳过) " + "-" * 20)
            _key_result = None
            try:
                import concurrent.futures as _fut
                with _fut.ThreadPoolExecutor(max_workers=1) as _pool:
                    _future = _pool.submit(_detect_creds)
                    _key_result = _future.result(timeout=8)
            except _fut.TimeoutError:
                _p("  [TIME] 密钥预检超时(>8秒,外网到飞书慢) → 已跳过,可进入飞书人员对接Tab手动触发")
                logger.warning("[Streamlit启动预检·密钥] 超时>8秒自动跳过(Cloud外网到飞书慢)")
                _key_result = None
            except Exception as e:
                _p(f"  [!] 密钥预检失败(不影响页面主功能): {e}")
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
                _p(
                    f"  [STAT] 汇总 | 有效={summary.get('valid',0)} 失效={summary.get('invalid',0)} "
                    f"超时={summary.get('timeout',0)} 网络错={summary.get('network_err',0)} "
                    f"跳过禁用={summary.get('skipped_disabled',0)}"
                )
                per = result.get("per_contact", {})
                for c in contacts:
                    cid = c.get("id")
                    name = c.get("name", "")
                    app_id = c.get("app_id", "")
                    en = "[ON]" if c.get("enabled", True) else "[OFF]"
                    vf = "[V]" if c.get("verified") else "[--]"
                    if cid in per:
                        c_info = per[cid]
                        st_line = _creds_text(c_info.get("status"), c_info.get("code"))
                        el_ms = c_info.get("elapsed_ms", 0)
                        oid = c.get("open_id", "") or ""
                        oid_m = oid[:10] + "..." if len(oid) > 10 else oid
                        _p(f"    · {name:<10} 启用={en} 验证={vf}  {app_id:<14} open_id={oid_m:<16}   {st_line} ({el_ms:.0f}ms)")
                    else:
                        _p(f"    · {name:<10} 启用={en} 验证={vf}  {app_id:<14}  [KEY] N/A (跳过)")
                _p(f"[密钥巡检] [OK] 完成 (总耗时={int(result.get('total_elapsed_ms',0))}ms, cache_age={age:.1f}s)")
    except Exception as e:
        logger.warning("[Streamlit启动预检] 飞书模块加载失败, 跳过飞书预检: %s", e, exc_info=True)
        _p(f"  [!] 飞书模块加载失败(不影响页面主功能): {e}")
    _p(bar + "\n")


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

# ============================================================
# 🎯 顶部 Tab 栏 · 横向滑块 (解决窄屏时右侧 Tab 被「封锁」看不到的问题)
# - 对页面所有 st.tabs(...) 全局生效
# - 绝不修改 Streamlit Tab DOM 结构, 只加 CSS/同级插入按钮/事件监听
# - 手动拉窄窗口 / 平板 / 手机上都可拖动/点 ◀ ▶ 看被遮挡的「整车/耐久/报告...」Tab
from components.tab_slider import enable_tab_slider
enable_tab_slider()
# ============================================================

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

    # 数据来源: 紧凑布局(移除路径/冗余说明)
    # NOTE: 这里的取值 "使用内置数据(自动扫描)" / "上传文件"
    #       必须与后续所有 use_builtin 判断分支完全一致,否则会出现数据不加载的问题
    use_builtin = st.radio(
        "数据来源",
        ["使用内置数据(自动扫描)", "上传文件"],
        index=0,
        label_visibility="visible",
        help=("① 使用内置数据: 无需上传,自动加载 企业资料包02_氢质氢离/ 下三个子目录里的样例数据(开箱即用)\n"
              "② 上传文件: 拖拽你自己的 CSV/Word/Excel 进来,系统自动识别数据类型并分析")
    )

    uploaded_files = None
    if use_builtin == "上传文件":
        uploaded_files = st.file_uploader(
            "拖入 CSV / Word / Excel 文件",
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

    st.divider()

    # ============================================================
    # 📁 侧边栏: 分支文件系统(用户指定: 放到侧边栏)
    # ============================================================
    from components.branch_ui import render_sidebar_file_structure
    render_sidebar_file_structure()

    # ============================================================
    # 🧭 侧边栏 · 13 个 Tab 分组索引(防止横向滚动找不到 Tab)
    # ============================================================
    st.divider()
    st.markdown(
        "<div style='font-size:1rem; font-weight:700; margin-bottom:8px;'>🧭 功能导航 · 13 Tab 速查</div>"
        "<div style='font-size:0.72rem; color:#6B7894; margin-bottom:10px;'>"
        "Tab 栏太多横向滚动看不见 → 对照下面的分组找对应 Tab</div>",
        unsafe_allow_html=True,
    )

    # -------- ① 核心功能区(前4 Tab) --------
    with st.container(border=True):
        st.markdown(
            "<div style='font-weight:700; color:#00D4FF; margin-bottom:4px;'>🏆 核心功能区(Tab 1-4)</div>",
            unsafe_allow_html=True,
        )
        st.caption("1️⃣ ⚡ 燃电运行看板 · 整车燃电实时/仿真关键指标")
        st.caption("2️⃣ 📈 性能统计预测 · 稳态段识别 + 衰减/极化分析")
        st.caption("3️⃣ 🔌 绝缘阻值统计 · 绝缘监测 + 趋势回归")
        st.caption("4️⃣ 🔬 台架耐久统计及预警 · 循环聚合 + 🔔飞书推送")

    # -------- ② 辅助分析区(中5 Tab) --------
    with st.container(border=True):
        st.markdown(
            "<div style='font-weight:700; color:#8AD86C; margin-bottom:4px;'>📊 辅助分析区(Tab 5-9)</div>",
            unsafe_allow_html=True,
        )
        st.caption("5️⃣ 整车看板 · 分钟级 KPI 汇总 + 下钻")
        st.caption("6️⃣ 耐久衰减 · docx工步解析 + 趋势分析")
        st.caption("7️⃣ 趋势预测 · 车辆指标线性回归预测")
        st.caption("8️⃣ 多车对比 · 多车同图叠加 + 指标对照表")
        st.caption("9️⃣ 报告导出 · 一键 HTML 测试报告 (Ctrl+P→PDF)")

    # -------- ③ 系统管理区(最后4 Tab) --------
    with st.container(border=True):
        st.markdown(
            "<div style='font-weight:700; color:#FFD93D; margin-bottom:4px;'>⚙️ 系统管理区(Tab 10-13)</div>",
            unsafe_allow_html=True,
        )
        st.caption("🔟 AI 助手 · 全产品智能问答 + 字段/阈值说明")
        # 🌟 飞书特别高亮
        st.markdown(
            "<div style='margin:6px 0 4px 0; padding:6px 10px; "
            "background:rgba(255,94,94,0.10); border:1px solid rgba(255,94,94,0.35); "
            "border-radius:8px; font-weight:700; color:#FF5E5E;'>"
            "1️⃣1️⃣ 📡 飞书人员对接 ← 新增联系人/发测试消息/密钥预检</div>",
            unsafe_allow_html=True,
        )
        st.caption("1️⃣2️⃣ 📁 上传历史 · 已上传文件回查 + 重命名/删除")
        st.caption("1️⃣3️⃣ 🌿 分支管理 · 文件版本控制 + Git 对比")

    # -------- 飞书状态小速览(只读,一眼看到有没有已验证联系人) --------
    try:
        from durability.feishu_contacts import list_contacts
        _fc_ct = list_contacts()
        if _fc_ct:
            _enabled = sum(1 for c in _fc_ct if c.get("enabled"))
            _verified = sum(1 for c in _fc_ct if c.get("verified"))
            with st.container(border=True):
                col_a, col_b, col_c = st.columns(3)
                col_a.metric("飞书联系人", len(_fc_ct))
                col_b.metric("已启用", _enabled)
                col_c.metric("✅已验证", _verified,
                             delta="可推送预警" if _verified > 0 else "需点📤发测试消息",
                             delta_color="normal" if _verified > 0 else "inverse")
        else:
            with st.container(border=True):
                st.markdown(
                    "⚠️ **还没配置飞书联系人** → 切到「📡 飞书人员对接」Tab,"
                    "点「📝新增联系人」填 App ID/Secret → 再「📤发送测试消息」激活。",
                )
    except Exception as _e_fc:
        logger.debug("侧边栏飞书联系人速览加载失败(不阻塞): %s", _e_fc)


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
        parsed = parse_csv_filename(uploaded_files[0].name) if Path(uploaded_files[0].name).suffix.lower() == ".csv" else None
        meta = parsed if parsed else {"vehicle": "上传"}
        vehicle_id = str((meta or {}).get("vehicle") or "上传")
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
        "- 旧版 `.doc` 请另存为 `.docx` 后再上传\n\n"
        "⬇️ 下方 13 个功能 Tab 已预先渲染,切换 Tab 可查看各模块「暂无数据」占位框架。"
    )
    # ❌ 不再 st.stop()! 继续向下创建 st.tabs(),让各个 Tab 显示预先渲染的空状态卡片
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

# ------- 先算一下各数据源标记(用于卡片里"数据源"字段) -------
def _merge_source_tags(tags_list: list[str]) -> str:
    """去重并按固定顺序拼接来源标签(📦内置 → 📤上传 → 💾DB回填)。"""
    order = {"📦 内置样例": 0, "📤 上传文件": 1, "💾 数据库回填": 2}
    uniq = []
    for t in tags_list:
        if t and t not in uniq:
            uniq.append(t)
    uniq.sort(key=lambda t: order.get(t, 99))
    return " + ".join(uniq) if uniq else "未知来源"

# ---------- ① 整车数据 ----------
if bool(data):
    _v_tags = []
    if use_builtin == "使用内置数据(自动扫描)":
        _v_tags.append("📦 内置样例")
    if bool(uploaded_files):
        _v_tags.append("📤 上传文件")
    # 冷启动回填的 DB 数据会在 data 为空时才触发,所以这里不用单独判断
    _total_rows = sum(int(len(v)) for v in data.values())
    _recognized.append({
        "kind": "整车数据",
        "dir": "02_整车数据处理",
        "tab_name": "整车看板",
        "summary": f"{len(cars)} 辆车 · 合计 {_total_rows:,} 行",
        "emoji": "🚗",
        "extra_tabs": ["⚡ 燃电运行看板", "📈 性能统计预测", "趋势预测", "🔌 绝缘阻值统计", "报告导出", "AI 助手", "多车对比"],
        "source": _merge_source_tags(_v_tags),
    })

# ---------- ② 耐久工步数据(docx) ----------
_dur_工步_tags = _detect_data_type_tags(dur_df)
if dur_df is not None and len(dur_df) > 0 and ("耐久工步" in _dur_工步_tags or "stage_start_h" in (dur_df.columns if dur_df is not None else []) or "平均单体电压(V)" in (dur_df.columns if dur_df is not None else [])):
    _d_tags = []
    # 内置耐久:用了 load_default_durability() 说明走了内置分支
    if use_builtin == "使用内置数据(自动扫描)":
        _d_tags.append("📦 内置样例")
    if docx_parts:
        _d_tags.append(f"📤 上传文件({len(docx_parts)}份)")
    if (_hydrated_dur is not None and not _hydrated_dur.empty
            and not docx_parts and use_builtin != "使用内置数据(自动扫描)"):
        _d_tags.append("💾 数据库回填")
    _n_stages = int(dur_df["stage"].nunique()) if "stage" in dur_df.columns else 0
    _recognized.append({
        "kind": "耐久工步数据",
        "dir": "01_耐久原始数据处理",
        "tab_name": "耐久衰减",
        "summary": f"{len(dur_df):,} 条工步 · {_n_stages} 个阶段",
        "emoji": "📉",
        "source": _merge_source_tags(_d_tags),
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
        "source": "📤 上传文件",
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
            "source": "📦 内置样例",
        })

if _recognized:
    # ---------- 11 个 Tab 的固定顺序与索引(用于引导提示第几个) ----------
    _TAB_ORDER = ["⚡ 燃电运行看板", "📈 性能统计预测", "🔌 绝缘阻值统计",
                  "🔬 台架耐久统计及预警",
                  "整车看板", "耐久衰减", "趋势预测", "多车对比", "报告导出",
                  "AI 助手", "📡 飞书人员对接"]

    def _simplify_dir(raw_dir: str) -> str:
        """去掉目录名前缀编号(如 '02_整车数据处理'→'整车数据处理'),保持简洁。"""
        import re
        return re.sub(r"^\d+[_-]\s*", "", str(raw_dir or "")).strip() or str(raw_dir)

    # ---------- 识别结果卡片的 CSS(企业级卡片风格, 列高等宽) ----------
    st.markdown("""<style>
    .rec-card-wrap { display: flex; gap: 18px; }
    .rec-card {
        flex: 1; display: flex; flex-direction: column;
        background: linear-gradient(160deg, rgba(20,30,55,0.85) 0%, rgba(15,22,42,0.92) 100%);
        border: 1px solid rgba(120,160,220,0.18);
        border-radius: 14px;
        padding: 22px 22px 10px 22px;
        min-height: 300px;
        transition: border-color .2s ease, transform .2s ease, box-shadow .2s ease;
        box-shadow: 0 6px 22px rgba(0,0,0,0.28);
    }
    .rec-card:hover {
        border-color: rgba(0,212,255,0.45);
        transform: translateY(-2px);
        box-shadow: 0 10px 32px rgba(0,0,0,0.38);
    }
    .rec-card-header {
        display: flex; align-items: center; gap: 12px;
        padding-bottom: 14px; margin-bottom: 16px;
        border-bottom: 1px solid rgba(120,160,220,0.14);
    }
    .rec-card-icon {
        width: 44px; height: 44px; border-radius: 10px;
        display: flex; align-items: center; justify-content: center;
        font-size: 22px; background: rgba(0,212,255,0.08);
        border: 1px solid rgba(0,212,255,0.22); flex-shrink: 0;
    }
    .rec-card-title {
        font-size: 1.15rem; font-weight: 700; color: #E6EEFB;
        font-family: 'Segoe UI','Microsoft YaHei',sans-serif; letter-spacing: 0.01em;
    }
    .rec-summary-pill {
        display: inline-block; margin: 0 0 14px 0;
        padding: 6px 12px; border-radius: 999px;
        background: rgba(0,212,255,0.08);
        border: 1px solid rgba(0,212,255,0.22);
        color: #7FE4FF; font-size: 0.84rem; font-weight: 600;
        letter-spacing: 0.01em;
    }
    .rec-field {
        display: flex; gap: 10px; padding: 7px 0;
        font-size: 0.88rem; line-height: 1.55;
    }
    .rec-field-label {
        min-width: 62px; color: #7B88A6;
        font-weight: 600; flex-shrink: 0;
    }
    .rec-field-value { color: #C9D4EA; word-break: break-all; }
    .rec-field-value code {
        background: rgba(120,160,220,0.08);
        border: 1px solid rgba(120,160,220,0.14);
        color: #B8C5E0; padding: 1px 6px; border-radius: 4px;
        font-size: 0.8rem;
    }
    .rec-tab-tag {
        display: inline-block; padding: 2px 9px; border-radius: 6px;
        background: rgba(94,234,212,0.08);
        border: 1px solid rgba(94,234,212,0.28);
        color: #5EEAD4; font-size: 0.78rem; font-weight: 600; margin: 0 4px 4px 0;
    }
    .rec-tab-tag.main {
        background: rgba(0,212,255,0.12);
        border-color: rgba(0,212,255,0.38);
        color: #00E0FF;
    }
    .rec-card-body { flex: 1; }
    .rec-card-footer { margin-top: 18px; }
    .rec-header-row {
        display: flex; align-items: center; justify-content: space-between;
        margin-bottom: 18px; gap: 16px; flex-wrap: wrap;
    }
    .rec-header-title {
        font-size: 1.5rem; font-weight: 700; color: #00D4FF;
        letter-spacing: 0.02em;
        font-family: 'Segoe UI','Microsoft YaHei',sans-serif;
    }
    .rec-header-title .pin {
        display: inline-block; margin-right: 8px;
        color: #5EEAD4;
    }
    .rec-header-sub {
        color: #7B88A6; font-size: 0.88rem;
    }
    .rec-count-pill {
        padding: 6px 14px; border-radius: 999px;
        background: rgba(94,234,212,0.08);
        border: 1px solid rgba(94,234,212,0.28);
        color: #5EEAD4; font-weight: 600; font-size: 0.84rem;
        white-space: nowrap;
    }
    </style>""", unsafe_allow_html=True)

    _outer = st.container(border=False)
    with _outer:
        # ---------- 顶部标题行 ----------
        _hdr_col1, _hdr_col2 = st.columns([5, 1.3])
        with _hdr_col1:
            st.markdown(
                f'<div class="rec-header-row">'
                f'  <div>'
                f'    <div class="rec-header-title"><span class="pin">📌</span>已识别数据类型</div>'
                f'    <div class="rec-header-sub">自动识别 {len(_recognized)} 类数据源 · 点击卡片下方按钮直接跳转到对应分析 Tab(标签栏位于当前卡片上方)</div>'
                f'  </div>'
                f'</div>',
                unsafe_allow_html=True,
            )
        with _hdr_col2:
            st.markdown(
                f'<div style="text-align:right;"><span class="rec-count-pill">共 {len(_recognized)} 类就绪</span></div>',
                unsafe_allow_html=True,
            )

        # ---------- 卡片列(严格等宽) ----------
        _n_cols = max(1, min(len(_recognized), 3))
        _card_cols = st.columns(_n_cols, gap="medium")

        for _i, _r in enumerate(_recognized):
            with _card_cols[_i % _n_cols]:
                _col_tab_tag = f'jump_tag_a_{_r["tab_name"]}_{_i}'
                _col_summ_html = ""
                if _r.get("summary"):
                    _col_summ_html = f'<div class="rec-summary-pill">📊 {_r["summary"]}</div>'

                # --- 主 Tab 标签 + 相关 Tab 标签 ---
                _main_tab_html = (
                    f'<span class="rec-tab-tag main">🎯 {_r["tab_name"]}</span>'
                    if _r.get("tab_name") else ""
                )
                _extra_tabs_html = ""
                if _r.get("extra_tabs"):
                    _extra_tabs_html = "&nbsp;".join(
                        f'<span class="rec-tab-tag">{t}</span>' for t in _r["extra_tabs"]
                    )

                _dir_short = _simplify_dir(_r["dir"])
                _dir_full = str(_r.get("dir", ""))
                _source_label = str(_r.get("source") or "📦 内置样例")

                # --- 渲染卡片 HTML(仅展示部分) ---
                st.markdown(
                    f'<div class="rec-card">'
                    f'  <div class="rec-card-header">'
                    f'    <div class="rec-card-icon">{_r.get("emoji","📦")}</div>'
                    f'    <div class="rec-card-title">{_r["kind"]}</div>'
                    f'  </div>'
                    f'  <div class="rec-card-body">'
                    f'    {_col_summ_html}'
                    f'    <div class="rec-field">'
                    f'      <div class="rec-field-label">数据源</div>'
                    f'      <div class="rec-field-value"><span class="rec-tab-tag main">{_source_label}</span></div>'
                    f'    </div>'
                    f'    <div class="rec-field">'
                    f'      <div class="rec-field-label">目录</div>'
                    f'      <div class="rec-field-value" title="{_dir_full}"><code>{_dir_short}</code></div>'
                    f'    </div>'
                    f'    <div class="rec-field">'
                    f'      <div class="rec-field-label">跳转</div>'
                    f'      <div class="rec-field-value" style="margin-top:-2px;">'
                    f'        {_main_tab_html}{_extra_tabs_html}'
                    f'      </div>'
                    f'    </div>'
                    f'  </div>'
                    f'  <div class="rec-card-footer">',
                    unsafe_allow_html=True,
                )

                # --- 原生 Streamlit 按钮(放在 footer 区域,保证在底部) ---
                _btn = st.button(
                    f"前往「{_r['tab_name']}」",
                    type="primary",
                    key=f"jump_btn_{_r['tab_name']}_{_i}",
                    use_container_width=True,
                )

                # 卡片闭合标签
                st.markdown(
                    f'  </div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

                if _btn:
                    _tab_label = _r['tab_name']
                    _tab_idx = _TAB_ORDER.index(_tab_label) + 1 if _tab_label in _TAB_ORDER else "?"
                    _arrow_bar = "  ".join([f"{'👇' if i+1==_tab_idx else '──'}" for i in range(len(_TAB_ORDER))])
                    _idx_bar  = "  ".join([f"[{i+1}]" for i in range(len(_TAB_ORDER))])
                    st.toast(f"🎯 第 {_tab_idx} 个 Tab「{_tab_label}」→ 看标签栏!", icon="✅")
                    st.success(
                        f"## 👆 请点击页面**最上方标签栏第 {_tab_idx} 个**「{_tab_label}」\n\n"
                        f"```\n"
                        f"Tab 顺序: {_idx_bar}\n"
                        f"箭头指: {_arrow_bar}\n"
                        f"```\n\n"
                        f"↑↑↑ 标签栏就在当前这段话的正上方,找到标 🔵「{_tab_label}」的标签点一下即可。"
                        + (f"\n\n💡 其他相关 Tab: {', '.join(_r['extra_tabs'])}" if _r.get("extra_tabs") else ""),
                        icon="🎯",
                    )



# ---------- 主区域 Tab（按企业优先级排序） ----------
# [核心功能区 1-4] 企业需求四大功能
# [补充功能区 5-12] 原整车看板/耐久衰减/趋势预测等辅助功能
# 顺序编号用于「一键跳转」按钮的 ASCII 箭头引导提示
tab_fc, tab_perf, tab_insul, tab_bench, tab_overview, tab_dur, tab_forecast, tab_cmp, tab_report, tab_ai, tab_contacts, tab_history, tab_branch = st.tabs([
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
    "🌿 分支管理",         # 系统:文件分支管理与版本控制
])


# ============================================================
# 以下:所有 Tab 渲染函数(懒加载 + 全异常兜底装饰器)
# ============================================================


def _render_empty_state(
    title: str = "暂无数据",
    desc: str = "",
    action_hint: str = "",
    icon: str = "📭",
) -> None:
    """统一的空状态卡片:数据没到时**预先渲染框架**,避免页面空白/跳动。

    Args:
        title:       主标题 (如:暂无整车数据)
        desc:        补充说明 (较长的提示文字,可多行)
        action_hint: 下一步操作指引 (短,高亮显示)
        icon:        emoji 图标
    """
    with st.container(border=True):
        col_ic, col_txt = st.columns([1, 9], gap="medium")
        with col_ic:
            st.markdown(
                f"<div style='font-size:2.8rem; text-align:center; "
                f"padding:8px 0; opacity:0.8;'>{icon}</div>",
                unsafe_allow_html=True,
            )
        with col_txt:
            st.markdown(
                f"<div style='font-size:1.1rem; font-weight:700; "
                f"margin-bottom:4px;'>{title}</div>",
                unsafe_allow_html=True,
            )
            if desc:
                for line in str(desc).splitlines():
                    st.caption(line.strip())
            if action_hint:
                st.markdown(
                    f"<div style='margin-top:6px; padding:4px 10px; "
                    f"background:rgba(0,212,255,0.10); border-left:3px solid #00D4FF; "
                    f"color:#8ad; font-size:0.85rem; border-radius:0 6px 6px 0;'>"
                    f"💡 {action_hint}</div>",
                    unsafe_allow_html=True,
                )


@tab_safe_render
def _render_tab_overview(
    cars: list[str],
    data: dict[str, pd.DataFrame],
    time_range_preset: str,
) -> None:
    """Tab1: 整车看板。"""
    # === 始终先渲染框架: 标题 + 区域分割线 ===
    st.subheader("🚗 整车数据汇总概览")
    st.caption("多维度 KPI 卡片 · 核心信号曲线 · 故障码统计 · 详细指标下钻")
    st.markdown("---")

    has_valid_base = True
    if not cars:
        _render_empty_state(
            title="暂无可选车辆",
            desc="系统内未扫描到含 `Timestamp` 列的整车 CSV / Excel 数据。",
            action_hint="在侧边栏切换数据源为「上传文件」，拖入整车 CSV / Excel；或选择「使用内置数据(自动扫描)」体验 Demo",
            icon="🚙",
        )
        has_valid_base = False
    elif not data:
        _render_empty_state(
            title="暂无已加载的整车数据",
            desc="车辆列表存在,但数据字典为空,可能是加载流程未执行。",
            action_hint="切换侧边栏「燃电看板数据源」到 模拟数据(mock) → 即可立刻看到框架内的占位填充",
            icon="📦",
        )
        has_valid_base = False

    if has_valid_base:
        sel_car = st.selectbox("选择车辆", cars, key="overview_car")
        if sel_car is None or sel_car not in data:
            _render_empty_state(
                title="所选车辆无数据",
                desc=f"下拉选中的车辆 `{sel_car}` 在已加载数据字典中找不到对应 DataFrame。",
                action_hint="重新选择车辆,或检查文件上传是否成功",
                icon="⚠️",
            )
            has_valid_base = False

    df = None
    if has_valid_base:
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

    has_data = has_valid_base and (df is not None) and (len(df) > 0)
    if has_valid_base and (df is None or len(df) == 0):
        _render_empty_state(
            title="当前时间区间无数据",
            desc=f"车辆: {sel_car if 'sel_car' in dir() else '-'} · 时间区间: {time_range_preset}",
            action_hint="扩大时间范围,或切换到「使用内置数据」查看演示内容",
            icon="⏳",
        )

    # === 始终渲染 KPI 卡片容器:有数据→填真实值;无数据→填占位===
    st.markdown("### 📊 概览 KPI")
    card_cols = st.columns(4)
    cards_placeholder = [
        ("运行时长(h)", "-"),
        ("行驶里程(km)", "-"),
        ("平均车速(km/h)", "-"),
        ("启动次数", "-"),
        ("百公里氢耗均值(kg)", "-"),
        ("瞬时氢耗均值(kg/h)", "-"),
        ("故障码种类", "-"),
        ("采样点数", "-"),
    ]
    ov = None
    if has_data:
        ov = vehicle_overview(df)
        cards_placeholder = [
            ("运行时长(h)", ov.get("运行时长(h)", "-")),
            ("行驶里程(km)", ov.get("行驶里程(km)", "-")),
            ("平均车速(km/h)", ov.get("平均车速(km/h)", "-")),
            ("启动次数", ov.get("启动次数", "-")),
            ("百公里氢耗均值(kg)", ov.get("百公里氢耗均值(kg)", "-")),
            ("瞬时氢耗均值(kg/h)", ov.get("瞬时氢耗均值(kg/h)", "-")),
            ("故障码种类", ov.get("故障码种类", "-")),
            ("采样点数", ov.get("采样点数", "-")),
        ]
    for i, (k, v) in enumerate(cards_placeholder):
        with card_cols[i % 4]:
            st.metric(k, v)

    # === 始终渲染曲线 2×2 容器:无数据→空状态卡片替代===
    st.markdown("---\n### 📈 核心曲线")
    c1, c2 = st.columns(2)
    with c1:
        if has_data:
            st.plotly_chart(fig_cell_voltage(df), use_container_width=True)
        else:
            _render_empty_state("电芯电压分布曲线", action_hint="加载整车 CSV 后自动渲染", icon="🔋")
    with c2:
        if has_data:
            st.plotly_chart(fig_power_curve(df), use_container_width=True)
        else:
            _render_empty_state("功率-效率曲线", action_hint="含 Timestamp 数据即可渲染", icon="⚡")
    c3, c4 = st.columns(2)
    with c3:
        if has_data:
            st.plotly_chart(fig_speed_hydrogen(df), use_container_width=True)
        else:
            _render_empty_state("车速-氢耗曲线", action_hint="含 FC_VehicleSpd / FC_Hyd* 列即可", icon="💨")
    with c4:
        if has_data and ov is not None:
            st.plotly_chart(fig_fault_bar(ov.get("故障码Top10", {})), use_container_width=True)
        else:
            _render_empty_state("故障码 Top10 统计", desc="无故障码或无数据时显示为空",
                                action_hint="上传 FC_ErrorCode 列数据后自动统计", icon="🚨")

    # 下钻区:始终渲染 expander 框架
    st.markdown("---")
    with st.expander("详细指标(单片一致性 / 功率 / 氢系统)", expanded=False):
        if has_data:
            st.json({
                "单片电压一致性": cell_voltage_consistency(df),
                "功率与效率": power_summary(df),
                "氢系统状态": h2_system(df),
            })
        else:
            _render_empty_state("暂无详细指标数据",
                                action_hint="上传整车 CSV 后可展开查看数值",
                                icon="📋")


@tab_safe_render
def _render_tab_durability(dur_df: pd.DataFrame) -> None:
    """Tab2: 耐久衰减(docx 聚合分析 + 曲线)。"""
    # === 始终渲染框架:标题/说明/分区线 ===
    st.subheader("📉 耐久工步衰减分析")
    st.caption("解析耐久 Word/Excel → 按 stage 聚合 → 电压衰减 KPI · 趋势曲线 · 极化曲线")
    st.markdown("---")

    has_data = True
    if dur_df is None or dur_df.empty:
        _render_empty_state(
            title="未检测到耐久数据",
            desc="当前没有耐久 docx / 无 Timestamp 的 Excel 数据。",
            action_hint="在侧边栏「上传文件」处拖入「耐久XX-YY.docx」或匹配耐久关键词的 Excel → 自动归入本 Tab 解析",
            icon="📝",
        )
        has_data = False

    # ✅ 防御:列结构完整性检查(上传的 Excel 可能缺标准列)
    req_cols = ["stage_start_h", "stage", "平均单体电压(V)", "净输出功率(kW)", "电堆电流(A)"]
    missing = [c for c in req_cols if c not in (dur_df.columns if has_data else [])]
    if has_data and missing:
        st.warning(
            f"上传的耐久数据缺少以下必需列: `{missing}`\n\n"
            f"当前数据有 {len(dur_df)} 行,实际列: {list(dur_df.columns)}\n"
            f"建议:拖入「耐久XX-YY.docx」,标准解析会自动产出 stage_start_h / 各指标列。"
        )
        st.subheader(f"当前上传耐久原始数据预览 ({len(dur_df)} 行,{len(dur_df.columns)} 列)")
        st.dataframe(dur_df.head(100), use_container_width=True)
        has_data = False

    # === 框架 1:元数据区(无论有无数据,都展示标题+容器) ===
    st.markdown("### 📋 耐久 docx 元数据")
    meta_df_preview = None
    if has_data:
        from src.data_loader import load_durability_metadata
        meta_base = DATA_ROOT / "01_耐久原始数据处理"
        if meta_base.exists():
            meta_files = sorted(meta_base.glob("*.docx"))
            if meta_files:
                try:
                    meta_df_preview = load_durability_metadata([str(f) for f in meta_files])
                except Exception as _meta_e:
                    st.caption(f"元数据加载失败(不阻塞主流程): {_meta_e}")
        if meta_df_preview is not None and len(meta_df_preview):
            st.dataframe(meta_df_preview, use_container_width=True, hide_index=True)
        else:
            _render_empty_state("暂无 docx 元数据",
                                desc="尚未扫描到内置目录下的 docx 文件。",
                                action_hint="拖入 docx 到 01_耐久原始数据处理 目录,或直接上传解析",
                                icon="🗂️")
    else:
        _render_empty_state("暂无 docx 元数据", action_hint="上传耐久 docx 后自动填充", icon="🗂️")

    # === 框架 2:原始数据表 ===
    st.markdown("### 📄 耐久数据明细")
    if has_data:
        n_stages = dur_df["stage"].nunique() if "stage" in dur_df.columns else 0
        st.caption(f"共 {len(dur_df):,} 条工步 · 跨 {n_stages} 个阶段")
        st.dataframe(dur_df, use_container_width=True, hide_index=True, height=220)
    else:
        _render_empty_state("暂无原始数据预览",
                            desc="无耐久数据时明细区保留框架,数据加载后自动填入",
                            action_hint="上传耐久 docx / Excel 后自动填充此表格",
                            icon="📑")

    # === 按阶段聚合 ===
    st.markdown("---\n### 🧮 各阶段指标聚合")
    agg_ok = False
    dur_sorted = None
    agg = None
    if has_data:
        dur_sorted = dur_df.sort_values(["stage_start_h", "step_idx"], kind="stable").reset_index(drop=True)
        agg_cols_map = {
            "平均单体电压": "平均单体电压(V)",
            "净输出功率": "净输出功率(kW)",
            "电堆电流": "电堆电流(A)",
        }
        agg_dict = {new: (old, "mean") for new, old in agg_cols_map.items() if old in dur_sorted.columns}
        for c in ("离均差", "电压方差"):
            if c in dur_sorted.columns:
                agg_dict[c] = (c, "mean")
        if not agg_dict:
            _render_empty_state(
                "无可用数值列",
                desc=f"数据中找不到任何可聚合数值列,实际列: {list(dur_sorted.columns)}",
                action_hint="检查上传的列是否包含 平均单体电压/净输出功率/电堆电流 等标准字段",
                icon="⚠️",
            )
            st.dataframe(dur_sorted.head(100), use_container_width=True)
        else:
            agg = dur_sorted.groupby(["stage_start_h", "stage"]).agg(**agg_dict).reset_index()
            agg = agg.sort_values("stage_start_h", kind="stable").reset_index(drop=True)
            if agg.empty:
                _render_empty_state(
                    "按阶段聚合后无数据",
                    desc="可能 stage 或 stage_start_h 全为空/NaN,groupby 后无有效行。",
                    action_hint="检查上传文件是否含 stage_start_h 与 stage 两个关键列",
                    icon="🔎",
                )
                st.dataframe(dur_sorted.head(100), use_container_width=True)
            else:
                agg_ok = True

    if not has_data:
        _render_empty_state("暂无聚合结果",
                            desc="上传耐久数据后自动按 stage_start_h + stage 分组聚合",
                            action_hint="拖入耐久 docx / Excel → 立刻看到阶段聚合表",
                            icon="🧪")

    # === 聚合表预览 + KPI + 趋势图 框架 ===
    k_col1, k_col2, k_col3 = st.columns(3)
    with k_col1:
        st.metric("首阶段平均电压 (V)", "-")
    with k_col2:
        st.metric("末阶段平均电压 (V)", "-")
    with k_col3:
        st.metric("累计衰减量 (mV)", "-", delta_color="inverse")

    if agg_ok:
        st.caption(f"数据预览(前 100 行) · 全量 {len(dur_sorted)} 行 × {dur_sorted['stage'].nunique()} 个阶段")
        st.dataframe(dur_sorted.head(100), use_container_width=True, hide_index=True, height=200)
        # 回填真实 KPI
        if "平均单体电压" in agg.columns and len(agg) >= 1:
            first_v = float(agg.iloc[0]["平均单体电压"])
            last_v = float(agg.iloc[-1]["平均单体电压"])
            delta = round(last_v - first_v, 3)
            k_col1.metric("首阶段平均电压 (V)", round(first_v, 3))
            k_col2.metric("末阶段平均电压 (V)", round(last_v, 3))
            k_col3.metric("累计衰减量 (mV)", int(delta * 1000),
                         delta=int(delta * 1000), delta_color="inverse")

    st.markdown(f"#### 阶段聚合表 ({len(agg)} 阶段)" if agg_ok else "#### 阶段聚合表 (—)")
    if agg_ok:
        st.dataframe(agg, use_container_width=True, hide_index=True)
    elif has_data:
        _render_empty_state("阶段聚合表暂无数据", icon="📊")
    else:
        _render_empty_state("暂无聚合表数据", action_hint="上传耐久 docx 后自动生成", icon="📊")

    # === 衰减趋势图 框架 ===
    st.markdown("---\n### 📉 耐久衰减趋势")
    trend_empty = True
    if agg_ok:
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
            trend_empty = False
    if trend_empty:
        _render_empty_state(
            "暂无衰减趋势",
            desc="需要有效聚合后的「平均单体电压」和/或「净输出功率」列。",
            action_hint="上传耐久 docx → 自动生成衰减趋势曲线",
            icon="📈",
        )

    # === 极化曲线 框架 ===
    st.markdown("---\n### 🔌 阶段内功率-电压特性曲线(极化曲线)")
    polar_empty = True
    if agg_ok:
        def _stage_sort_key(s: str):
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
        if unique_stages:
            sel_stage = st.selectbox(
                "选择阶段",
                sorted(unique_stages, key=_stage_sort_key),
                key="dur_stage",
            )
            sub = dur_sorted[dur_sorted["stage"] == sel_stage]
            if "step_idx" in sub.columns:
                sub = sub.sort_values("step_idx")
            if "电堆电流(A)" in sub.columns and "平均单体电压(V)" in sub.columns:
                from src.plots import _base_layout
                import plotly.graph_objects as go
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
                polar_empty = False
            else:
                st.info(f"当前阶段缺「电堆电流(A)」或「平均单体电压(V)」列,无法画极化曲线。数据预览:")
                st.dataframe(sub, use_container_width=True)
                polar_empty = False
    if polar_empty:
        with st.container():
            _render_empty_state(
                "暂无极化曲线数据",
                desc="需按 stage 聚合成功,且目标阶段含「电堆电流(A)」+「平均单体电压(V)」列。",
                action_hint="上传耐久 docx / 匹配列结构的 Excel → 自动可切换阶段看极化曲线",
                icon="🧲",
            )


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

    has_input = bool(csv_files or uploaded_bench_frames)
    st.markdown("---\n### 🔬 数据解析与聚合")
    if not has_input:
        _render_empty_state(
            title="未检测到台架耐久数据",
            desc=f"内置目录 `{bench_dir}` 中无 CSV,也没有上传命中关键词的文件。",
            action_hint="方式1:将 CSV 放入内置目录 03_台架耐久数据\n方式2:侧边栏上传含「循环/功率点/效率」等关键词的 CSV/Excel",
            icon="🏭",
        )

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
    parsed_ok = False
    agg_df = pd.DataFrame()
    if has_input:
        if not builtin_parsed.empty:
            all_frames.append(builtin_parsed)
        if uploaded_parsed_list:
            all_frames.extend(uploaded_parsed_list)

        if not all_frames:
            _render_empty_state(
                "所有台架数据源解析后均为空",
                desc="CSV / Excel 可能是空表或缺少必要列。",
                action_hint="检查文件内容是否含 cycle/power_point/FC_* 等标准台架字段",
                icon="⚠️",
            )
        else:
            merged_all = pd.concat(all_frames, ignore_index=True)
            agg_df = aggregate_durability_stats(merged_all, _SIGNAL_COLS)
            _parts_msg = []
            if not builtin_parsed.empty:
                _parts_msg.append(f"内置 {len(builtin_parsed):,} 行")
            if uploaded_parsed_list:
                _u_rows = sum(int(len(x)) for x in uploaded_parsed_list)
                _parts_msg.append(f"上传 {_u_rows:,} 行")
            st.success(f"已聚合 {len(agg_df)} 组 (cycle × power_point) · {' + '.join(_parts_msg)}")
            parsed_ok = True

    st.markdown("---\n### 📊 循环/功率点趋势图")
    if parsed_ok:
        filter_opts = render_durability_filter()
        user_signals = filter_opts.get('signal_columns') or _SIGNAL_COLS
        sel_powers = (filter_opts.get('power_points')
                      or filter_opts.get('selected_powers')
                      or [])
        render_durability_chart(
            agg_df, user_signals,
            sel_powers,
            filter_opts.get('agg_method', 'mean'),
        )
    else:
        _render_empty_state("暂无趋势图",
                            desc="聚合成功后可按循环、功率点筛选多信号趋势。",
                            action_hint="上传台架数据后,此处会出现筛选器和多子图趋势可视化",
                            icon="📉")

    st.markdown("---\n### 🚨 预警阈值与历史记录")

    # ============================================================
    # 🚨 预警检测与飞书推送(功能4核心要求:真正发消息,不再测试模式)
    # ============================================================
    # ---------- ① 用户可配置预警阈值(企业默认值 50mV / 600mV) ----------
    with st.expander("⚙️ 预警阈值与推送配置（企业默认值已填）", expanded=parsed_ok):
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
    if parsed_ok and not agg_df.empty:
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
    if not parsed_ok:
        _render_empty_state(
            "暂无预警命中结果",
            desc="未加载台架数据或数据聚合为空时,预警检测流程不执行。",
            action_hint="上传 CSV/Excel → 聚合成功后自动根据阈值检测离均差 & 电压下限预警",
            icon="✅",
        )
    elif raw_alert_events:
        st.caption(
            f"检测到 {len(raw_alert_events)} 条预警事件 · "
            f"阈值:离均差>{_dev_thresh:.0f}mV / 平均单体电压<{_avg_thresh:.0f}mV · "
            f"飞书推送:{'✅ 启用' if _enable_push else '⛔ 已关闭(仅入库)'}"
        )
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

    st.markdown("---\n#### 📜 独立历史记录(全量数据库)")
    try:
        render_alert_log()
    except Exception as _hist_e:
        _render_empty_state("历史记录加载失败",
                            desc=f"{_hist_e}",
                            action_hint="首次启动会自动初始化数据库,刷新后可正常查看",
                            icon="📜")


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
    """Tab12: 上传历史记录及数据回看。
    - 支持筛选、分页、按类型汇总
    - 编辑模式下: 行内直接「重命名 / 上下移 / 删除」+ 撤销栈(顺序和文件名可撤回)
    """
    from datetime import datetime
    import time as _time_mod

    kind_icons = {'整车': '🚗', '耐久工步': '📉', '台架循环': '🔬'}

    # ===================== 初始化会话态 =====================
    if "history_edit_mode" not in st.session_state:
        st.session_state["history_edit_mode"] = False
    if "history_undo_stack" not in st.session_state:
        st.session_state["history_undo_stack"] = []   # List[dict]
    MAX_UNDO = 50
    _UNDO_KEY = "history_undo_stack"
    _EDIT_KEY = "history_edit_mode"

    def _snap(desc: str, files_now: list[dict], op: str) -> None:
        """操作前打快照,入撤销栈。"""
        ids_ordered = [int(f.get("id")) for f in files_now if isinstance(f.get("id"), int)]
        names = {int(f.get("id")): str(f.get("file_name", ""))
                 for f in files_now if isinstance(f.get("id"), int)}
        snap = {"op": op, "desc": desc,
                "time": datetime.now().strftime("%H:%M:%S"),
                "ids_ordered": ids_ordered, "file_names": names}
        stack = st.session_state.get(_UNDO_KEY, [])
        stack.append(snap)
        if len(stack) > MAX_UNDO:
            stack = stack[-MAX_UNDO:]
        st.session_state[_UNDO_KEY] = stack
        logger.info("[上传历史-快照] %s | 撤销栈深度=%d | %s",
                    snap["time"], len(stack), desc[:80])

    def _fmt_ts(v):
        if not v:
            return "—"
        try:
            if isinstance(v, datetime):
                return v.strftime('%Y-%m-%d %H:%M')
            if isinstance(v, str):
                if " " in v and len(v) >= 16:
                    return v[:16].replace("T", " ")
                return datetime.fromisoformat(str(v)).strftime('%Y-%m-%d %H:%M')
        except Exception:
            pass
        return str(v)[:16]

    def _status_tag(s: str) -> str:
        s = (s or "uploaded").lower()
        m = {"uploaded": ("已上传", "📤"),
             "aggregated": ("已入库", "✅"),
             "failed": ("失败", "⚠️")}
        t, ic = m.get(s, (s or "未知", "📄"))
        return f"{ic} {t}"

    # ===================== 页面渲染 =====================
    # --- 标题行(含编辑模式 & 撤销) ---
    st.header("📁 上传历史记录")
    st.caption("查看所有已入库的数据文件;开启「编辑模式」可直接 重命名 / 移动位置 / 删除。")

    t1, t2, t3 = st.columns([6, 1.1, 1.1])
    with t2:
        edit_mode = st.toggle("✏️ 编辑模式", key="tgl_history_edit",
                              value=bool(st.session_state.get(_EDIT_KEY, False)))
        st.session_state[_EDIT_KEY] = edit_mode
    with t3:
        undo_stack: list[dict] = st.session_state.get(_UNDO_KEY, [])
        can_undo = len(undo_stack) > 0
        if st.button(f"↩️ 撤销 ({len(undo_stack)})", key="history_undo_btn",
                     disabled=not can_undo, use_container_width=True):
            snap = undo_stack.pop()
            st.session_state[_UNDO_KEY] = undo_stack
            # 撤销:顺序 + 文件名逐项回写
            order_ok, order_msg = db_update_display_order_batch(snap["ids_ordered"])
            name_restored = 0
            name_failed = 0
            for fid, old_name in snap["file_names"].items():
                ok_n, _ = db_rename_data_file(int(fid), str(old_name))
                if ok_n:
                    name_restored += 1
                else:
                    name_failed += 1
            logger.info("[上传历史-撤销] op=%s desc=%s | order_ok=%s(%s) names_restored=%d failed=%d",
                        snap.get("op"), snap.get("desc"),
                        order_ok, order_msg[:40], name_restored, name_failed)
            # 清缓存
            try:
                st.cache_data.clear()
            except Exception:
                pass
            st.toast(f"↩️ 已撤销:「{snap['desc']}」({snap['time']})", icon="♻️")
            st.info(
                f"♻️ **撤销完成** →「{snap['desc']}」 ({snap['time']})\n\n"
                f"- 显示顺序: {'✅ 已还原' if order_ok else '❌ ' + order_msg}\n"
                f"- 文件名恢复: {name_restored} 条成功 / {name_failed} 条跳过(可能已被级联删除,级联删除不可恢复)"
            )
            _time_mod.sleep(0.3)
            st.rerun()

    # --- 汇总卡片 ---
    _backend = get_db_backend_info().get("backend", "unknown")
    summary = _cached_upload_summary(f"{_backend}_hist")
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
        for i, (kind, info) in enumerate(kinds.items()):
            with _cols[i % len(_cols)]:
                icon = kind_icons.get(kind, '📄')
                st.markdown(f"**{icon} {kind}**")
                st.caption(f"{info['count']} 个文件 · {info['rows']:,} 行")

    by_vehicle = summary.get('by_vehicle', {})
    if by_vehicle:
        with st.expander(f"按车辆统计 ({len(by_vehicle)} 辆车)", expanded=False):
            for vehicle_id, info in by_vehicle.items():
                st.markdown(f"- **车辆 {vehicle_id}**: {info['files']} 个文件 · {info['rows']:,} 行")

    st.divider()

    # --- 筛选 ---
    filter_col1, filter_col2 = st.columns([2, 1])
    with filter_col1:
        kind_filter = st.selectbox(
            "按类型筛选",
            options=["全部", "整车", "耐久工步", "台架循环"],
            index=0,
            key="history_kind_filter_v2",
        )
    with filter_col2:
        page_size = st.selectbox("每页显示", [10, 20, 50], index=1, key="history_page_size_v2")

    kind_param = None if kind_filter == "全部" else kind_filter
    # 进入页之前先兜底确保 display_order 全有值(避免第一次移动时报 NULL)
    try:
        db_ensure_display_order(kind_param)
    except Exception as _e:
        logger.warning("[上传历史] ensure_display_order 兜底失败(不阻塞): %s", _e)

    files = _cached_data_files(f"{_backend}_hist", kind_param, page_size)

    has_files = bool(files)
    st.markdown("### 📋 上传记录列表")
    if not files:
        _render_empty_state(
            title="暂无上传记录",
            desc="数据库中还没有已登记的上传文件记录。",
            action_hint="切换到侧边栏「上传文件」,拖入 CSV/Word/Excel → 记录会自动出现在此处",
            icon="📥",
        )

    if has_files:
        # ===================== 行内操作区(编辑模式) & 简洁卡片区 =====================
        if edit_mode:
            st.caption("✏️ **编辑模式**:直接在输入框改文件名(失焦即保存);行尾 ⬆️⬇️ 调位置;🗑️ 级联删除(不可恢复)。")

        file_ids_now: list[int] = [int(f.get("id")) for f in files]

    # 每个文件一行
    for idx, f in enumerate(files):
        fid = int(f.get("id"))
        kind = f.get("data_kind", "") or "未知"
        icon = kind_icons.get(kind, "📄")
        fname_cur = str(f.get("file_name", "?"))
        vehicle = f.get("vehicle_id", "") or "—"
        rows = int(f.get("row_count") or 0)
        uploaded = _fmt_ts(f.get("uploaded_at"))
        status_s = _status_tag(str(f.get("status", "uploaded")))
        disp = int(f.get("display_order") or (idx + 1))

        if not edit_mode:
            # ---------- 非编辑模式: 简洁卡片 ----------
            with st.container(border=False):
                c1, c2, c3, c4, c5, c6 = st.columns([0.5, 2.6, 1, 1.1, 1.3, 1])
                c1.markdown(
                    f"<div style='text-align:center; font-size:22px;'>{icon}</div>",
                    unsafe_allow_html=True,
                )
                c2.markdown(
                    f"**{fname_cur}**  \n"
                    f"<span style='color:#7B88A6;font-size:0.8rem;'>ID: {fid} · 顺序 #{disp}</span>",
                    unsafe_allow_html=True,
                )
                c3.markdown(
                    f"<span class='rec-tab-tag main' style='margin-top:2px;'>{kind}</span>" if True else kind,
                    unsafe_allow_html=True,
                )
                c4.write(vehicle)
                c5.write(f"{rows:,} 行")
                c6.write(uploaded)
        else:
            # ---------- 编辑模式: 行内操作 ----------
            with st.container(border=True):
                # 每行: 编号+上移下移(2col) | 文件元信息(3col) | 重命名输入框(4col) | 删除(1col)
                oc1, oc2, oc3, oc4, oc5 = st.columns([0.9, 2.4, 1.2, 3.5, 1])

                # --- 列 1: 编号 + 上下移按钮 ---
                with oc1:
                    st.markdown(
                        f"<div style='font-weight:700; color:#00D4FF; margin-top:2px;'>#{disp}</div>"
                        f"<div style='font-size:0.75rem; color:#7B88A6;'>ID {fid} · {icon}</div>",
                        unsafe_allow_html=True,
                    )
                    mc1, mc2 = st.columns(2)
                    disabled_up = idx == 0
                    disabled_down = idx == len(files) - 1
                    with mc1:
                        if st.button("⬆️", key=f"hist_up_{fid}", disabled=disabled_up,
                                     help="上移一位"):
                            prev_fid = file_ids_now[idx - 1]
                            _snap(f"上移:文件[{fid}]↔文件[{prev_fid}]", files, "swap_up")
                            ok, msg = db_swap_data_file_order(fid, prev_fid)
                            if ok:
                                logger.info("[上传历史-上移] ✅ id=%d↔id=%d | %s",
                                            fid, prev_fid, msg)
                                try:
                                    st.cache_data.clear()
                                except Exception:
                                    pass
                                st.rerun()
                            else:
                                st.error(f"⬆️ 上移失败: {msg}")
                                logger.warning("[上传历史-上移] ❌ id=%d↔id=%d msg=%s",
                                               fid, prev_fid, msg)
                    with mc2:
                        if st.button("⬇️", key=f"hist_down_{fid}", disabled=disabled_down,
                                     help="下移一位"):
                            next_fid = file_ids_now[idx + 1]
                            _snap(f"下移:文件[{fid}]↔文件[{next_fid}]", files, "swap_down")
                            ok, msg = db_swap_data_file_order(fid, next_fid)
                            if ok:
                                logger.info("[上传历史-下移] ✅ id=%d↔id=%d | %s",
                                            fid, next_fid, msg)
                                try:
                                    st.cache_data.clear()
                                except Exception:
                                    pass
                                st.rerun()
                            else:
                                st.error(f"⬇️ 下移失败: {msg}")
                                logger.warning("[上传历史-下移] ❌ id=%d↔id=%d msg=%s",
                                               fid, next_fid, msg)

                # --- 列 2: 类型 + 车辆 ---
                with oc2:
                    st.markdown(
                        f"<span class='rec-tab-tag main'>{icon} {kind}</span><br>"
                        f"<div style='font-size:0.82rem; color:#7B88A6; margin-top:2px;'>"
                        f"🚙 {vehicle} · {rows:,} 行<br>{uploaded} · {status_s}</div>",
                        unsafe_allow_html=True,
                    )

                # --- 列 3: 数据规模小提示 ---
                with oc3:
                    st.caption(
                        f"行数\n**{rows:,}**\n\n"
                        f"状态\n{status_s}"
                    )

                # --- 列 4: 文件名编辑框(on_change 保存) ---
                def _do_rename(cur_fid=fid, cur_old=fname_cur):
                    val = st.session_state.get(f"hist_name_{cur_fid}", cur_old)
                    new_val = (val or "").strip()
                    if not new_val:
                        st.warning(f"文件名不能为空, 已恢复原名: {cur_old}")
                        st.session_state[f"hist_name_{cur_fid}"] = cur_old
                        logger.warning("[上传历史-重命名] ⚠️ 文件[%d]拒绝空文件名", cur_fid)
                        return
                    if new_val == cur_old:
                        return
                    # 重命名前打撤销快照
                    _snap(f"重命名文件[{cur_fid}]: {cur_old[:30]} → {new_val[:30]}",
                          files, "rename")
                    ok, msg = db_rename_data_file(int(cur_fid), new_val)
                    if ok:
                        logger.info("[上传历史-重命名] ✅ id=%d | %s", cur_fid, msg)
                        try:
                            st.cache_data.clear()
                        except Exception:
                            pass
                        st.rerun()
                    else:
                        # 失败把输入框值恢复
                        st.session_state[f"hist_name_{cur_fid}"] = cur_old
                        logger.warning("[上传历史-重命名] ❌ id=%d 原因=%s",
                                       cur_fid, msg)
                        st.error(f"❌ 重命名失败: {msg}")

                with oc4:
                    st.text_input(
                        f"文件名 (ID {fid})",
                        value=fname_cur,
                        max_chars=512,
                        key=f"hist_name_{fid}",
                        label_visibility="collapsed",
                        on_change=_do_rename,
                    )
                    st.caption("👆 输入文件名后,按回车或点击其他区域(失焦)即保存。")

                # --- 列 5: 删除 ---
                with oc5:
                    confirm_del = st.checkbox(
                        "确认删除", key=f"hist_del_conf_{fid}", value=False,
                        help="勾选后「🔥删除」按钮才可用,级联删除不可恢复。"
                    )
                    if st.button("🔥 删除", key=f"hist_del_btn_{fid}",
                                 disabled=not confirm_del, type="primary",
                                 use_container_width=True):
                        _snap(f"删除文件[{fid}]: {fname_cur[:30]} (顺序#{disp})", files, "delete")
                        ok, msg = db_delete_data_file(int(fid), op_user="streamlit-history-tab")
                        if ok:
                            logger.warning("[上传历史-删除] ⚠️ 已级联删除 id=%d | %s",
                                           fid, msg)
                            st.success(f"✅ {msg}")
                            try:
                                st.cache_data.clear()
                            except Exception:
                                pass
                            # 清空自己的确认框,避免再点
                            st.session_state[f"hist_del_conf_{fid}"] = False
                            st.rerun()
                        else:
                            logger.error("[上传历史-删除] ❌ id=%d 原因=%s",
                                         fid, msg, exc_info=True)
                            st.error(f"❌ 删除失败: {msg}")

    st.divider()

    # --- 编辑模式底部管理区 ---
    if edit_mode:
        st.subheader("🛠️ 管理说明")
        st.markdown(
            "**修改方式:**\n"
            "- **重命名:** 改文件名输入框 → 按回车 / 点其他区域就会写入数据库\n"
            "- **移动(排序):** 每行开头的 ⬆️⬇️ 按钮和相邻文件交换顺序, 改动会持久化到数据库 `display_order` 列, 所有人都会看到新顺序\n"
            "- **删除:** 先勾「确认删除」再点「🔥删除」, **会同时级联删除该车分钟级/耐久工步/台架统计的关联数据 → 不可恢复**\n\n"
            "**撤销说明(↩️ 撤销按钮):**\n"
            "- 可撤销: 重命名、上下移 造成的文件名/显示顺序变化(最多 50 步)\n"
            "- 无法完全撤销: 删除操作会真实删除数据, 撤销只能还原其他文件的顺序/名称, 已删除的数据无法回来"
        )

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
    st.subheader("🆚 多车/多时段对比分析")
    st.caption("支持多车横向指标叠加对比 & 同车前后分段对比 (基于时间轴重对齐)")
    st.markdown("---")

    has_cars = bool(cars)
    if not has_cars:
        _render_empty_state(
            title="暂无整车数据可对比",
            desc="系统内没有含 Timestamp 列的整车 CSV / Excel,无法构建对比列表。",
            action_hint="在侧边栏上传至少 2 份整车数据 → 回到本 Tab 做对比",
            icon="🆚",
        )

    if has_cars:
        cmp_mode = st.radio(
            "对比模式",
            ["多车横向对比", "同车前后对比"],
            index=0,
            horizontal=True,
            help="多车横向:多辆车同指标叠加; 同车前后:同一辆车两个时段叠加",
        )
        cmp_col = st.selectbox(
            "对比指标",
            [
                "FC_CurrOut", "FC_VoltOut", "FC_NetPwrOut",
                "FC_MinCellVoltage", "FC_AvgCellVoltage",
                "FC_AvgCellVoltDev", "FC_VehicleIsolationR",
                "FC_RunTime_Hours",
                "FC_VehicleSpd",
            ],
            index=2,
            format_func=lambda c: (
                SIGNAL_MAP.get(c, c)
                if c in SIGNAL_MAP
                else {"FC_VehicleSpd": "车辆车速 (km/h)"}.get(c, c)
            ),
        )

    # ========= 多车横向对比 区块 框架 =========
    st.markdown("### 📊 多车横向 / 同车前后对比结果")
    if not has_cars:
        _render_empty_state("暂无对比数据",
                            desc="无车辆时对比图区域保留框架。",
                            action_hint="上传多份整车 CSV → 立刻可以在此处叠加曲线",
                            icon="📈")
    elif cmp_mode == "多车横向对比":
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
    st.subheader("📄 一键生成测试报告")
    st.caption("基于当前已加载的整车数据,一键导出 HTML 格式测试报告(可 Ctrl+P 打印为 PDF)")
    st.markdown("---")

    has_data = bool(cars) and bool(data)
    if not has_data:
        _render_empty_state(
            title="暂无可导出的整车数据",
            desc="需要至少 1 辆已加载的整车 CSV 数据,才能运行 HTML 报告生成流程。",
            action_hint="侧边栏上传整车 CSV / 切换内置数据模式 → 回到此处点「生成报告」",
            icon="📑",
        )

    st.markdown("### 🚀 执行生成")
    if not has_data:
        st.button("生成 HTML 报告(浏览器 Ctrl+P 可打印为 PDF)",
                  type="primary", disabled=True)
        _render_empty_state("等待数据加载后即可生成",
                            desc="无数据时按钮禁用,避免报错。",
                            action_hint="加载 1 辆车数据后按钮自动启用",
                            icon="⏳")
    elif st.button("生成 HTML 报告(浏览器 Ctrl+P 可打印为 PDF)", type="primary"):
        rep_car = cars[0]
        if rep_car not in data or len(data[rep_car]) == 0:
            _render_empty_state(f"车辆 {rep_car} 对应数据为空",
                                action_hint="检查数据字典中是否存在该车辆的 DataFrame",
                                icon="⚠️")
        else:
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

    # ============ ⚡ 快捷问题(支持编辑/移动/删除 + 撤销 + 详细日志) ============
    DEFAULT_QUICK_CATEGORIES = [
        {"name": "🎯 Tab 导航(去哪个)", "questions": [
            "想看单体电压+电流双轴曲线,去哪个 Tab?步骤是什么?",
            "上传台架 CSV 后,分析结果在第几个 Tab 看?",
            "我传了个 .docx(耐久工步),应该看哪里?"
        ]},
        {"name": "📊 功能1/2 解读", "questions": [
            "离均差(FC_AvgCellVoltDev)是什么?数值大会有什么影响?",
            "稳态段怎么算出来的?180秒 规则解释一下",
            "燃电极化曲线是什么意思?能判断什么?"
        ]},
        {"name": "🔌 功能3 绝缘", "questions": [
            "350 kΩ 和 250 kΩ 两条报警线分别是什么含义?",
            "绝缘的有效值是怎么从原始数据算出来的?哪些坏值会被过滤?",
            "绝缘阻值预测触碰报警线多久,是怎么算出来的?"
        ]},
        {"name": "🏭 功能4 台架预警", "questions": [
            "台架耐久的 6 档标准功率点具体是哪 6 个?",
            "台架触发飞书预警的具体条件是什么?阈值是多少?",
            "飞书预警推送给哪些人?怎么新增/修改联系人?"
        ]},
    ]
    MAX_UNDO_HISTORY = 50

    def _deepcopy_categories(src_list: list) -> list:
        return [
            {"name": str(c.get("name", "")),
             "questions": [str(q) for q in (c.get("questions") or [])]}
            for c in (src_list or [])
        ]

    if "quick_categories" not in st.session_state:
        logger.info("[快捷题目] 初始化默认题目: %d 个分类", len(DEFAULT_QUICK_CATEGORIES))
        st.session_state.quick_categories = _deepcopy_categories(DEFAULT_QUICK_CATEGORIES)
    if "quick_categories_undo" not in st.session_state:
        st.session_state.quick_categories_undo = []

    def _snapshot_for_undo(operation_desc: str) -> None:
        try:
            snap = _deepcopy_categories(st.session_state.quick_categories)
            st.session_state.quick_categories_undo.append({
                "desc": operation_desc,
                "snapshot": snap,
                "ts": datetime.now().strftime("%H:%M:%S"),
            })
            if len(st.session_state.quick_categories_undo) > MAX_UNDO_HISTORY:
                dropped = st.session_state.quick_categories_undo.pop(0)
                logger.info("[快捷题目-撤销栈] 超上限,丢弃最老操作: %s", dropped["desc"])
            logger.info("[快捷题目-快照] 「%s」入栈 | 撤销栈深度=%d",
                        operation_desc, len(st.session_state.quick_categories_undo))
        except Exception as _e:
            logger.error("[快捷题目-快照] 保存失败 op=%s err=%s", operation_desc, _e, exc_info=True)

    def _do_undo() -> bool:
        stack = st.session_state.quick_categories_undo
        if not stack:
            logger.warning("[快捷题目-撤销] 撤销栈为空")
            return False
        try:
            item = stack.pop()
            st.session_state.quick_categories = item["snapshot"]
            logger.info("[快捷题目-撤销] ✅ 回滚「%s」(TS=%s) | 剩余撤销栈=%d",
                        item["desc"], item["ts"], len(stack))
            st.info(f"↩️ 已撤销:「{item['desc']}」 ({item['ts']})")
            return True
        except Exception as _e:
            logger.error("[快捷题目-撤销] 异常: %s", _e, exc_info=True)
            st.error(f"撤销失败: {_e}")
            return False

    # ------- 顶部标题行 + 撤销按钮 -------
    hdr_col1, hdr_col2, hdr_col3 = st.columns([3, 1, 1])
    with hdr_col1:
        st.subheader("⚡ 快捷问题(按分类点击)")
    with hdr_col2:
        edit_mode = st.toggle("✏️ 编辑模式", key="ai_quick_edit_mode", value=False)
    with hdr_col3:
        undo_depth = len(st.session_state.quick_categories_undo)
        if st.button(f"↩️ 撤销 ({undo_depth})", key="ai_quick_undo",
                     use_container_width=True, disabled=(undo_depth == 0),
                     help="最多保留 {} 步".format(MAX_UNDO_HISTORY)):
            if _do_undo():
                st.rerun()

    def _ask_question(q_text: str) -> None:
        """统一的快捷问题发送逻辑(避免重复代码)。"""
        logger.info("[快捷题目-AI] 发送问题: %s (len=%d)", q_text[:50], len(q_text))
        st.session_state.ai_messages.append({"role": "user", "content": q_text})
        with st.chat_message("user"):
            st.markdown(q_text)
        with st.chat_message("assistant"):
            with st.spinner("AI 思考中..."):
                try:
                    from src.ai_assistant import ask
                    t0 = time.perf_counter()
                    answer = ask(q_text)
                    logger.info("[快捷题目-AI] 返回 OK | 耗时=%dms | 答案len=%d",
                                int((time.perf_counter() - t0) * 1000), len(answer))
                except Exception as e:
                    answer = f"AI 调用异常: {e}"
                    logger.error("[快捷题目-AI] 调用异常: %s", e, exc_info=True)
                st.markdown(answer)
                st.session_state.ai_messages.append(
                    {"role": "assistant", "content": answer})

    pending_cat_op = None  # 收集分类级操作,避免 for 循环内修改索引问题

    # ===== 遍历每个分类 =====
    for ci, cat in enumerate(st.session_state.quick_categories):
        cat_key = f"ai_cat_{ci}"
        with st.container(border=True):
            if edit_mode:
                t_col1, t_col2, t_col3, t_col4, t_col5 = st.columns(
                    [4, 0.9, 0.9, 0.9, 0.9], gap="small"
                )
                with t_col1:
                    # ---- 分类名 on_change 处理 + 快照 + 日志 ----
                    def _on_cat_name_changed(_ci=ci, _old=cat["name"], _key=f"{cat_key}_name"):
                        _new = st.session_state.get(_key, "")
                        if not _new.strip():
                            logger.warning("[快捷题目-分类改名] ⚠️ 分类[%d]新名字为空,拒绝保存 old=%r", _ci, _old)
                            st.session_state[_key] = _old
                            return
                        if _new == _old:
                            return
                        _snapshot_for_undo(f"分类[{_ci}]改名: {_old[:20]} → {_new[:20]}")
                        try:
                            st.session_state.quick_categories[_ci]["name"] = _new.strip()
                            logger.info("[快捷题目-分类改名] ✅ 分类[%d] | old=%r → new=%r", _ci, _old, _new)
                        except Exception as _rn_e:
                            logger.error("[快捷题目-分类改名] ❌ 保存失败 ci=%d err=%s", _ci, _rn_e, exc_info=True)
                            st.error(f"分类名保存失败: {_rn_e}")
                    st.text_input("分类名", value=cat["name"],
                                  key=f"{cat_key}_name", label_visibility="collapsed",
                                  on_change=_on_cat_name_changed)
                with t_col2:
                    if st.button("⬆️ 上移", key=f"{cat_key}_up",
                                 disabled=(ci == 0), use_container_width=True):
                        pending_cat_op = ("move_up", ci)
                with t_col3:
                    if st.button("⬇️ 下移", key=f"{cat_key}_down",
                                 disabled=(ci == len(st.session_state.quick_categories) - 1),
                                 use_container_width=True):
                        pending_cat_op = ("move_down", ci)
                with t_col4:
                    if st.button("➕ 题目", key=f"{cat_key}_addq", use_container_width=True):
                        pending_cat_op = ("add_q", ci)
                with t_col5:
                    if st.button("🗑️ 删除分类", key=f"{cat_key}_del",
                                 type="secondary", use_container_width=True):
                        pending_cat_op = ("delete", ci)
            else:
                st.markdown(f"**{cat['name']}**")

            questions = cat["questions"]
            if questions:
                pending_q_op = None
                for qi, q in enumerate(questions):
                    q_key = f"{cat_key}_q_{qi}"
                    q_row = st.columns([0.8, 5, 0.7, 0.7, 0.7, 0.7], gap="small") \
                        if edit_mode else st.columns([1])

                    if edit_mode:
                        q_row[0].caption(f"Q{qi+1}")
                        # ---- 题目内容 on_change + 快照 + 日志 ----
                        def _on_q_changed(_ci=ci, _qi=qi, _old_q=q, _k=f"{q_key}_edit"):
                            _new_q = st.session_state.get(_k, "")
                            if _new_q == _old_q:
                                return
                            if not _new_q.strip():
                                logger.warning("[快捷题目-题目编辑] ⚠️ 分类[%d]题[%d]内容为空,拒绝保存", _ci, _qi)
                                st.session_state[_k] = _old_q
                                return
                            _snapshot_for_undo(f"分类[{_ci}]题[{_qi}]编辑")
                            try:
                                st.session_state.quick_categories[_ci]["questions"][_qi] = _new_q
                                logger.info("[快捷题目-题目编辑] ✅ 分类[%d]题[%d] | 前len=%d 后len=%d",
                                            _ci, _qi, len(_old_q), len(_new_q))
                            except Exception as _qe:
                                logger.error("[快捷题目-题目编辑] ❌ 保存失败 ci=%d qi=%d err=%s",
                                             _ci, _qi, _qe, exc_info=True)
                                st.error(f"题目保存失败: {_qe}")
                        q_row[1].text_input("题目内容", value=q, key=f"{q_key}_edit",
                                            label_visibility="collapsed", on_change=_on_q_changed)

                        btn_up = q_row[2].button("⬆️", key=f"{q_key}_up",
                                                 disabled=(qi == 0), use_container_width=True)
                        btn_down = q_row[3].button("⬇️", key=f"{q_key}_down",
                                                   disabled=(qi == len(questions) - 1),
                                                   use_container_width=True)
                        btn_ask = q_row[4].button("💬", key=f"{q_key}_ask",
                                                  use_container_width=True, help="发送给 AI")
                        btn_del = q_row[5].button("🗑️", key=f"{q_key}_del",
                                                  type="secondary", use_container_width=True)
                        if btn_ask:
                            _ask_question(st.session_state.quick_categories[ci]["questions"][qi])
                        if btn_up and qi > 0:
                            pending_q_op = ("q_up", ci, qi)
                        if btn_down and qi < len(questions) - 1:
                            pending_q_op = ("q_down", ci, qi)
                        if btn_del:
                            pending_q_op = ("q_delete", ci, qi)
                    else:
                        with q_row[0]:
                            if st.button(q, key=f"{q_key}_btn", use_container_width=True):
                                _ask_question(q)

                # ---- 统一执行题目级操作 ----
                if pending_q_op:
                    op, _ci, _qi = pending_q_op
                    lst = st.session_state.quick_categories[_ci]["questions"]
                    if op == "q_up":
                        _snapshot_for_undo(f"分类[{_ci}]题[{_qi}]上移")
                        try:
                            lst[_qi - 1], lst[_qi] = lst[_qi], lst[_qi - 1]
                            logger.info("[快捷题目-题目移动] ✅ 分类[%d]题%d↔题%d 上移", _ci, _qi, _qi - 1)
                            st.rerun()
                        except Exception as _e:
                            logger.error("[快捷题目-题目移动] ❌ 上移失败 ci=%d qi=%d err=%s", _ci, _qi, _e, exc_info=True)
                            st.error(f"题上移失败: {_e}")
                    elif op == "q_down":
                        _snapshot_for_undo(f"分类[{_ci}]题[{_qi}]下移")
                        try:
                            lst[_qi], lst[_qi + 1] = lst[_qi + 1], lst[_qi]
                            logger.info("[快捷题目-题目移动] ✅ 分类[%d]题%d↔题%d 下移", _ci, _qi, _qi + 1)
                            st.rerun()
                        except Exception as _e:
                            logger.error("[快捷题目-题目移动] ❌ 下移失败 ci=%d qi=%d err=%s", _ci, _qi, _e, exc_info=True)
                            st.error(f"题下移失败: {_e}")
                    elif op == "q_delete":
                        _snapshot_for_undo(f"分类[{_ci}]删除题[{_qi}]")
                        try:
                            removed = lst.pop(_qi)
                            logger.info("[快捷题目-题目删除] ✅ 分类[%d]删除题%d | %r", _ci, _qi, removed[:50])
                            st.rerun()
                        except Exception as _e:
                            logger.error("[快捷题目-题目删除] ❌ 删除失败 ci=%d qi=%d err=%s", _ci, _qi, _e, exc_info=True)
                            st.error(f"题删除失败: {_e}")
            elif edit_mode:
                st.caption("(暂无题目,点右侧「➕ 题目」添加)")

    # ---- 统一执行分类级操作 ----
    if pending_cat_op:
        op_c, idx_c = pending_cat_op
        cats_ref = st.session_state.quick_categories
        if op_c == "move_up":
            _snapshot_for_undo(f"分类[{idx_c}]上移")
            try:
                cats_ref[idx_c - 1], cats_ref[idx_c] = cats_ref[idx_c], cats_ref[idx_c - 1]
                logger.info("[快捷题目-分类移动] ✅ 分类%d↔分类%d 上移 | %s",
                            idx_c, idx_c - 1, [c["name"] for c in cats_ref])
                st.rerun()
            except Exception as _e:
                logger.error("[快捷题目-分类移动] ❌ 上移失败 ci=%d err=%s", idx_c, _e, exc_info=True)
                st.error(f"分类上移失败: {_e}")
        elif op_c == "move_down":
            _snapshot_for_undo(f"分类[{idx_c}]下移")
            try:
                cats_ref[idx_c], cats_ref[idx_c + 1] = cats_ref[idx_c + 1], cats_ref[idx_c]
                logger.info("[快捷题目-分类移动] ✅ 分类%d↔分类%d 下移 | %s",
                            idx_c, idx_c + 1, [c["name"] for c in cats_ref])
                st.rerun()
            except Exception as _e:
                logger.error("[快捷题目-分类移动] ❌ 下移失败 ci=%d err=%s", idx_c, _e, exc_info=True)
                st.error(f"分类下移失败: {_e}")
        elif op_c == "delete":
            _snapshot_for_undo(f"删除分类[{idx_c}] {cats_ref[idx_c]['name'][:30]}")
            try:
                removed_cat = cats_ref.pop(idx_c)
                logger.info("[快捷题目-分类删除] ✅ 删除分类 idx=%d name=%r 题数=%d | 剩余=%s",
                            idx_c, removed_cat["name"], len(removed_cat["questions"]),
                            [c["name"] for c in cats_ref])
                st.rerun()
            except Exception as _e:
                logger.error("[快捷题目-分类删除] ❌ 删除失败 ci=%d err=%s", idx_c, _e, exc_info=True)
                st.error(f"删除分类失败: {_e}")
        elif op_c == "add_q":
            _snapshot_for_undo(f"分类[{idx_c}]新增题目")
            try:
                cats_ref[idx_c]["questions"].append("新题目(点 ✏️ 编辑)")
                logger.info("[快捷题目-新增题] ✅ 分类[%d](%s)新增1题 | 总数=%d",
                            idx_c, cats_ref[idx_c]["name"], len(cats_ref[idx_c]["questions"]))
                st.rerun()
            except Exception as _e:
                logger.error("[快捷题目-新增题] ❌ 分类[%d]失败 err=%s", idx_c, _e, exc_info=True)
                st.error(f"新增题目失败: {_e}")

    # ===== 编辑模式: 底部新增分类 + 重置 =====
    if edit_mode:
        st.divider()
        mgmt_cols = st.columns([2, 1, 1])
        with mgmt_cols[0]:
            new_cat_name = st.text_input(
                "新增分类名(带 emoji 更清晰,例如 🆘 常见报错)",
                key="ai_new_cat_name", placeholder="例如: 🆘 常见报错排查"
            )
        with mgmt_cols[1]:
            if st.button("➕ 新增分类", key="ai_add_cat_btn", use_container_width=True,
                         disabled=(not new_cat_name.strip())):
                nc_name = new_cat_name.strip()
                _snapshot_for_undo(f"新增分类 {nc_name}")
                try:
                    st.session_state.quick_categories.append({
                        "name": nc_name,
                        "questions": ["示例题目(可编辑)"]
                    })
                    logger.info("[快捷题目-新增分类] ✅ 新增 %r | 当前分类数=%d",
                                nc_name, len(st.session_state.quick_categories))
                    st.rerun()
                except Exception as _e:
                    logger.error("[快捷题目-新增分类] ❌ 失败 name=%r err=%s", nc_name, _e, exc_info=True)
                    st.error(f"新增分类失败: {_e}")
        with mgmt_cols[2]:
            if st.button("♻️ 恢复默认题目", key="ai_reset_cat_btn", use_container_width=True,
                         type="secondary"):
                _snapshot_for_undo("恢复默认题目(重置)")
                try:
                    st.session_state.quick_categories = _deepcopy_categories(DEFAULT_QUICK_CATEGORIES)
                    st.session_state.quick_categories_undo = []
                    logger.info("[快捷题目-重置] ✅ 已恢复默认 %d 分类,撤销栈已清空",
                                len(DEFAULT_QUICK_CATEGORIES))
                    st.rerun()
                except Exception as _e:
                    logger.error("[快捷题目-重置] ❌ 失败 err=%s", _e, exc_info=True)
                    st.error(f"恢复默认题目失败: {_e}")
        st.caption(
            "💡 编辑模式说明:①分类名/题目输入框直接编辑(失焦或回车即保存,自动打撤销点) "
            "②⬆️⬇️ 调整分类/题目顺序 ③🗑️ 删除 ④「➕ 题目/新增分类」添加 "
            "⑤误操作时点右上角「↩️ 撤销 (N)」按钮回滚(最多保留 {} 步) "
            "⑥「♻️ 恢复默认题目」一键回到出厂状态".format(MAX_UNDO_HISTORY)
        )


@tab_safe_render
def _render_tab_forecast(
    cars: list[str],
    data: dict[str, pd.DataFrame],
) -> None:
    """Tab8: 趋势预测。"""
    st.header("📈 趋势预测")
    st.caption("基于历史数据线性回归预测未来走势,支持 7 项指标:压差/氢耗/故障频率/净功率/绝缘电阻/平均单体电压/离均差。")
    st.markdown("---")

    has_cars = bool(cars)
    if not has_cars:
        _render_empty_state(
            title="无历史数据可做趋势预测",
            desc="需要加载整车 CSV 后,才能按时间排序线性回归预测。",
            action_hint="在侧边栏上传整车 CSV / 切换到内置数据模式 → 回到本 Tab 选择指标预测",
            icon="🔮",
        )

    # ====== 参数区:始终展示(无数据时禁用占位) ======
    st.markdown("### 🛠️ 预测参数")
    col_f1, col_f2 = st.columns(2)
    with col_f1:
        if has_cars:
            fc_car = st.selectbox("选择车辆", cars, key="forecast_car")
        else:
            st.selectbox("选择车辆", ["(暂无车辆)"], disabled=True, key="forecast_car_ph")
            fc_car = None
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

    # ====== 预测结果区 框架 ======
    st.markdown("---\n### 🔮 预测结果区")
    if has_cars and fc_car:
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
    else:
        if not has_cars:
            _render_empty_state("待加载整车数据",
                                desc="无车辆数据时,「开始预测」按钮不触发。",
                                action_hint="上传整车 CSV → 回到此处点「开始预测」",
                                icon="⏳")
        else:
            st.info("请先选择车辆后再开始预测")


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
    st.markdown("---")

    st.markdown("### 🎚️ 筛选条件")
    perf_cfg = render_performance_filter()
    filter_ok = bool(perf_cfg.get("valid"))
    if not filter_ok:
        _render_empty_state(
            title="请完成筛选条件",
            desc="稳态分析需要先指定: 车辆 + 起止时间 + 至少 1 个电流点 + 最短持续时长。",
            action_hint="在上方筛选器填好各项后,「确认」按钮会让筛选状态变为有效",
            icon="🎚️",
        )

    st.markdown("---\n### 🔎 稳态段筛选 & 聚合")
    data_ok = False
    agg_df = pd.DataFrame()
    all_segs: list = []
    df_perf = pd.DataFrame()
    if filter_ok:
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
            _render_empty_state(
                "所选时间范围无数据",
                desc=f"车辆: {vehicle_id} · 当前数据源: {fc_data_mode}",
                action_hint="扩大时间范围 / 切换数据源(如模拟数据) / 检查当前车辆是否含数据",
                icon="⏱️",
            )
        else:
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
            else:
                _base = ["FC_VoltOut", "FC_NetPwrOut", "FC_MinCellVoltage"]
                if perf_cfg["y_signal"] not in _base:
                    _perf_sigs = [perf_cfg["y_signal"]] + _base
                else:
                    _perf_sigs = list(dict.fromkeys([perf_cfg["y_signal"]] + _base))
                for ext in ("FC_AvgCellVoltDev", "FC_VARVoltage"):
                    if ext not in _perf_sigs:
                        _perf_sigs.append(ext)
                agg_df = aggregate_segments(
                    all_segs, _perf_sigs, exclude_anomaly=False,
                    warmup_seconds=perf_cfg["warmup_seconds"])

                if len(agg_df) == 0 or "duration" not in agg_df.columns:
                    st.warning("聚合后无有效段(可能全部含异常被剔除)")
                else:
                    data_ok = True
    else:
        _render_empty_state("等待筛选有效后自动执行",
                            desc="筛选条件有效 → 数据加载 → 稳态段扫描 → 聚合分析",
                            action_hint="先把上方筛选条件「确认」为有效状态",
                            icon="🧪")

    st.markdown("---\n### 📉 衰减分析 & 极化曲线")
    if not (filter_ok and data_ok):
        _render_empty_state(
            "暂无聚合结果",
            desc="需要:筛选有效 + 找到稳态段 + 聚合成功,三个条件同时满足。",
            action_hint="切换到「模拟数据」数据源 → 确认筛选 → 立刻看到衰减 & 极化曲线",
            icon="📊",
        )
    else:
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
        else:
            fig_perf = create_performance_figure(
                agg_df,
                x_col=perf_cfg["x_mode"],
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
    st.markdown("---")

    st.markdown("### 🎚️ 筛选条件(车辆+时间+阈值+预测)")
    ins_cfg = render_insulation_filter()
    cfg_ok = bool(ins_cfg.get("valid"))
    if not cfg_ok:
        _render_empty_state(
            title="请完成筛选条件",
            desc="绝缘分析需先: 选择车辆 + 起止时间 + 初级/次级阈值 + 预测天数",
            action_hint="上方筛选器填完后「确认」→ 筛选状态变为有效即自动开始加载",
            icon="🎚️",
        )

    st.markdown("---\n### 📊 数据加载 & 坏值清洗")
    data_ok = False
    df_insul = pd.DataFrame()
    if cfg_ok:
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
            _render_empty_state(
                "所选时间范围无绝缘数据",
                desc=f"车辆: {_vehicle} · 数据源: {fc_data_mode}",
                action_hint="扩大时间 / 切换到「模拟数据」/ 检查当前车辆是否含 FC_VehicleIsolationR 列",
                icon="⏱️",
            )
        else:
            if "FC_MainSts" not in raw_insul.columns:
                raw_insul["FC_MainSts"] = 4
                st.info("ℹ 数据无 FC_MainSts 列,已默认按运行态(4)处理")

            df_insul = process_insulation_data(raw_insul, interval_minutes=_interval)
            if len(df_insul) == 0:
                _render_empty_state(
                    "清洗后无有效绝缘数据",
                    desc="可能原因: 绝缘值<=0 / =65535 / >=9999,或运行状态非4/8",
                    action_hint="切换为「模拟数据」可立刻看到坏值追踪 & 清洗统计",
                    icon="🧹",
                )
            else:
                data_ok = True
    else:
        _render_empty_state("等待筛选有效后自动加载",
                            desc="筛选条件有效 → 加载原始数据 → 清洗坏值 → 统计&预测",
                            action_hint="先让筛选状态变为有效",
                            icon="🧪")

    st.markdown("---\n### 🔋 健康度分布 & 寿命预测")
    if not (cfg_ok and data_ok):
        _render_empty_state(
            "暂无绝缘健康度分析",
            desc="需要筛选有效 + 清洗后仍有数据，两个条件满足后自动展示。",
            action_hint="切换数据源到「模拟数据」→ 确认筛选 → 立刻看到绝缘趋势 & 报警预测",
            icon="📉",
        )
    else:
        n_valid = int(df_insul["FC_VehicleIsolationR"].notna().sum())
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
            _render_empty_state(
                "有效数据点不足,无法趋势预测",
                desc=f"当前仅 {n_valid} 个有效绝缘值点,至少需要 20 个才能做回归预测。",
                action_hint="扩大时间范围 / 切换数据源(模拟数据自动生成充足样本)",
                icon="📐",
            )
        else:
            prediction = predict_insulation_trend(
                df_insul,
                alarm_values=[_primary, _secondary],
                predict_days=_forecast,
                poly_order=_degree,
            )
            render_insulation_stats(df_insul, prediction)

            st.markdown("#### 绝缘阻值趋势(原始散点按状态4/8分色 + 10min聚合 + 报警线 + 预测)")
            fig_insul = create_insulation_figure(
                df_insul,
                primary_alarm=_primary,
                secondary_alarm=_secondary,
                predict_days=_forecast,
                poly_order=_degree,
                raw_df=raw_insul,
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

with tab_branch:                        # [13] 文件分支管理与版本控制(系统)
    from components.branch_ui import render_branch_management_page
    render_branch_management_page()
