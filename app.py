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
            print("-" * 10 + " 🔑 密钥预检开始 " + "-" * 30)
            try:
                result = _detect_creds()
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
                        info = per[cid]
                        st_line = _creds_text(info.get("status"), info.get("code"))
                        el_ms = info.get("elapsed_ms", 0)
                        oid = c.get("open_id", "") or ""
                        oid_m = oid[:10] + "..." if len(oid) > 10 else oid
                        print(f"    · {name:<10} 启用={en} 验证={vf}  {app_id:<14} open_id={oid_m:<16}   {st_line} ({el_ms:.0f}ms)")
                    else:
                        print(f"    · {name:<10} 启用={en} 验证={vf}  {app_id:<14}  🔑 N/A (跳过)")
                print(f"[密钥巡检] ✅ 完成 (总耗时={int(result.get('total_elapsed_ms',0))}ms, cache_age={age:.1f}s)")
            except Exception as e:
                print(f"  ⚠ 密钥预检失败(不影响页面主功能): {e}")
                logger.warning("[Streamlit启动预检·密钥] 失败: %s", e, exc_info=True)
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
from utils.helpers import filter_by_time, resample_data, detect_anomalies
from utils.mock_data import generate_mock_data
from components.theme import apply_custom_css
from components.data_quality import render_data_quality
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
    page_title="氢质氢离 · 设备测试数据分析与自动报告助手",
    page_icon="📊",
    layout="wide",
)

# 工业科技感暗色主题(全局 CSS 注入,仅需调用一次)
apply_custom_css()

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
    st.title("📊 设备测试分析助手")
    st.caption("氢质氢离 · 燃料电池整车 + 耐久")

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

# 通用文件上传处理:按后缀分发到 CSV 合并 / Word 表格 / Excel 表格
csv_parts: list[pd.DataFrame] = []
docx_rows: list[dict] = []
xls_parts: list[pd.DataFrame] = []

if uploaded_files:

    for f in uploaded_files:
        suffix = Path(f.name).suffix.lower()
        try:
            if suffix == ".csv":
                df = pd.read_csv(f)
                if "Timestamp" in df.columns:
                    df["Timestamp"] = pd.to_datetime(df["Timestamp"], errors="coerce")
                    csv_parts.append(df)
                else:
                    st.warning(f"{f.name}: 缺 Timestamp 列,跳过")
            elif suffix in (".doc", ".docx"):
                # Word 文档按耐久 docx 流程处理
                from docx import Document
                # 旧 .doc 格式 python-docx 不支持,会抛异常,在 except 提示
                doc = Document(io.BytesIO(f.read()))
                stage = Path(f.name).stem.split("-")[0].replace("耐久", "")
                for ti, t in enumerate(doc.tables):
                    for ri, r in enumerate(t.rows):
                        for ci, c in enumerate(r.cells):
                            docx_rows.append({
                                "file": f.name, "stage": stage,
                                "table_idx": ti, "row_idx": ri,
                                "col_idx": ci, "value": c.text.strip(),
                            })
            elif suffix in (".xls", ".xlsx"):
                # Excel 表格:统一规范化为 DataFrame
                xls_df = pd.read_excel(io.BytesIO(f.read()))
                # 如果含 Timestamp 列,作为整车数据
                if "Timestamp" in xls_df.columns:
                    xls_df["Timestamp"] = pd.to_datetime(
                        xls_df["Timestamp"], errors="coerce")
                    xls_parts.append(xls_df)
                else:
                    # 无 Timestamp 的 Excel 表,作为耐久补充数据
                    # 把列名 + 值铺平成 docx_rows 同结构
                    for ri, row in xls_df.iterrows():
                        for ci, col in enumerate(xls_df.columns):
                            docx_rows.append({
                                "file": f.name, "stage": Path(f.name).stem,
                                "table_idx": 0, "row_idx": int(ri),
                                "col_idx": ci, "value": str(row[col]),
                            })
            else:
                st.warning(f"{f.name}: 不支持的格式 ({suffix})")
        except Exception as e:
            tip = ""
            if suffix == ".doc":
                tip = " (旧版 .doc 格式不受支持,请另存为 .docx 后再上传)"
            st.warning(f"{f.name} 解析失败{tip}: {e}")

    # 合并 CSV 部分(含 Excel 转 CSV 的)
    all_csv_parts = csv_parts + xls_parts
    if all_csv_parts:
        merged = pd.concat(all_csv_parts, ignore_index=True)
        merged = merged.drop_duplicates(subset=["Timestamp"], keep="first")
        merged = merged.sort_values("Timestamp").reset_index(drop=True)
        meta = (parse_csv_filename(uploaded_files[0].name)
                if Path(uploaded_files[0].name).suffix.lower() == ".csv"
                else {"vehicle": "上传"})
        data[meta["vehicle"]] = merged

        # 数据质量扫描 + 邮件报警(发现高危时)
        try:
            from src.data_quality import scan_df, generate_brief, save_brief
            from src.email_alert import send_alert

            scan_result = scan_df(merged, vehicle=meta["vehicle"])
            brief = generate_brief(scan_result)
            brief_path = save_brief(brief, vehicle=meta["vehicle"])

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
                subject = (f"[数据质量告警] 车辆 {meta['vehicle']} "
                           f"发现 {len(scan_result['high_risk_fields'])} 个高危字段")
                sent = send_alert(subject=subject, body=brief,
                                  attachment=brief_path)
                if sent:
                    st.info("📧 报警邮件已发送")
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "质量扫描或邮件报警执行失败(不影响主流程): %s", e, exc_info=True)

# 耐久数据:内置 + 上传的 Word/Excel 补充
dur_df = (load_default_durability()
          if use_builtin == "使用内置数据(自动扫描)" else pd.DataFrame())
if docx_rows:
    dur_df = pd.concat([dur_df, pd.DataFrame(docx_rows)], ignore_index=True)


# ---------- 顶部状态 ----------

st.title("📊 设备测试数据分析与自动报告助手")
st.caption("上传或使用内置数据,自动完成合并 / 清洗 / 指标计算 / 可视化 / 一键导出报告")

if not data:
    st.warning("未检测到数据,请选择内置或上传。")
    st.stop()

cars = list(data.keys())
st.success(f"已加载 {len(cars)} 辆车数据: {', '.join(cars)}  |  耐久 docx: {len(dur_df)} 条")

# ---------- 主区域 Tab ----------

tab_overview, tab_perf, tab_dur, tab_bench, tab_contacts, tab_cmp, tab_report, tab_ai, tab_forecast, tab_fc, tab_insul = st.tabs([
    "整车看板", "📈 性能统计预测", "耐久衰减", "🔬 台架耐久统计及预警", "📡 飞书人员对接", "多车对比", "报告导出", "AI 助手", "趋势预测", "⚡ 燃电运行看板", "🔌 绝缘阻值统计",
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
    sel_car = st.selectbox("选择车辆", cars, key="overview_car")
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
    if dur_df.empty:
        st.info("未检测到耐久 docx 数据。可去侧边栏切到『上传 docx 耐久』后拖入文件。")
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

    # 按阶段聚合
    st.subheader("各阶段指标聚合")
    agg = dur_df.groupby(["stage_start_h", "stage"]).agg(
        平均单体电压=("平均单体电压(V)", "mean"),
        净输出功率=("净输出功率(kW)", "mean"),
        电堆电流=("电堆电流(A)", "mean"),
        离均差=("离均差", "mean"),
        电压方差=("电压方差", "mean"),
    ).reset_index()

    # KPI 卡片:整体衰减
    k1, k2, k3 = st.columns(3)
    first_v = float(agg.iloc[0]["平均单体电压"])
    last_v = float(agg.iloc[-1]["平均单体电压"])
    with k1:
        st.metric("首阶段平均电压(V)", round(first_v, 2))
    with k2:
        st.metric("末阶段平均电压(V)", round(last_v, 2))
    with k3:
        delta = round(last_v - first_v, 2)
        st.metric("衰减量(V)", delta, delta=delta, delta_color="inverse")

    st.dataframe(agg, use_container_width=True, hide_index=True)

    # 衰减趋势图(用 stage_start_h 作为 X,显示真实耐久小时数)
    from src.plots import _base_layout
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Scatter(
        x=agg["stage"], y=agg["平均单体电压"],
        mode="lines+markers", name="平均单体电压(V)",
        line=dict(color="#1f77b4", width=2),
    ), secondary_y=False)
    fig.add_trace(go.Scatter(
        x=agg["stage"], y=agg["净输出功率"],
        mode="lines+markers", name="净输出功率(kW)",
        line=dict(color="#ff7f0e", width=2, dash="dot"),
    ), secondary_y=True)
    fig.update_layout(**_base_layout("耐久衰减趋势:平均单体电压 + 净输出功率"))
    fig.update_yaxes(title_text="平均单体电压 (V)", secondary_y=False)
    fig.update_yaxes(title_text="净输出功率 (kW)", secondary_y=True)
    fig.update_xaxes(title_text="耐久阶段 (h)")
    st.plotly_chart(fig, use_container_width=True)

    # 同阶段内工步曲线(选取代表性阶段)
    st.subheader("阶段内功率-电压特性曲线")
    sel_stage = st.selectbox(
        "选择阶段",
        sorted(dur_df["stage"].unique(), key=lambda s: int(s.split("-")[0])),
        key="dur_stage",
    )
    sub = dur_df[dur_df["stage"] == sel_stage].sort_values("step_idx")
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


@tab_safe_render
def _render_tab_bench() -> None:
    """Tab3: 台架耐久统计及预警。"""
    st.subheader("台架耐久数据统计与预警")
    st.caption("解析台架 CSV → 按循环/功率点聚合 → 可视化趋势 + 飞书预警 + 历史记录")

    bench_dir = DATA_ROOT / "03_台架耐久数据"
    csv_files = sorted(bench_dir.glob("*.csv")) if bench_dir.exists() else []

    if not csv_files:
        st.info(f"未检测到台架耐久 CSV 数据。请将文件放到:\n`{bench_dir}`")
        return

    from durability.data_parser import parse_durability_data
    from durability.statistics_aggregator import aggregate_durability_stats
    from components.durability_filter import render_durability_filter
    from components.durability_chart import render_durability_chart
    from components.durability_alert_log import render_alert_log

    _SIGNAL_COLS = [
        'FC_AvgCellVoltage', 'FC_NetPwrOut', 'FC_CurrOut',
        'FC_AvgCellVoltDev',
    ]

    @st.cache_data(ttl=60, show_spinner="解析台架耐久 CSV...")
    def _load_bench_agg(files: tuple[str, ...], _mtimes: tuple[float, ...]) -> pd.DataFrame:
        frames = []
        for f in files:
            try:
                df = pd.read_csv(f)
                parsed = parse_durability_data(df)
                frames.append(parsed)
                logger.info("解析台架 CSV: %s | %d 行", f, len(parsed))
            except Exception as e:
                logger.error("解析失败 %s: %s", f, e)
        if not frames:
            return pd.DataFrame()
        merged = pd.concat(frames, ignore_index=True)
        return aggregate_durability_stats(merged, _SIGNAL_COLS)

    import os
    _mtimes = tuple(os.path.getmtime(str(f)) for f in csv_files)
    agg_df = _load_bench_agg(tuple(str(f) for f in csv_files), _mtimes)
    st.success(f"已聚合 {len(agg_df)} 组 (cycle × power_point)")

    filter_opts = render_durability_filter()

    render_durability_chart(
        agg_df, _SIGNAL_COLS,
        filter_opts.get('selected_powers', []),
        filter_opts.get('agg_method', 'mean'),
    )

    st.markdown("---")
    st.subheader("预警历史记录")

    alert_events: list[dict] = []
    if not agg_df.empty:
        dev_col = [c for c in agg_df.columns if 'AvgCellVoltDev' in c and 'mean' in c]
        avg_col = [c for c in agg_df.columns if 'AvgCellVoltage' in c and 'mean' in c]
        cnt_col = '数据量' if '数据量' in agg_df.columns else None
        qual_col = '质量标记' if '质量标记' in agg_df.columns else None

        for _, row in agg_df.iterrows():
            ts = datetime.now()
            cyc = int(row.get('cycle_id', 0))
            pp = float(row.get('power_point', 0))
            cnt = int(row.get(cnt_col, 0)) if cnt_col else 0
            qual = str(row.get(qual_col, '正常')) if qual_col else '正常'

            if dev_col:
                dev = float(row[dev_col[0]]) if pd.notna(row[dev_col[0]]) else 0
                if dev > 50:
                    alert_events.append({
                        'timestamp': ts, 'cycle_id': cyc, 'power_point': pp,
                        'condition': '离均差>50mV', 'value': dev, 'threshold': 50.0,
                        'signal': 'FC_AvgCellVoltDev', 'unit': 'mV', 'operator': '>',
                        'label': '离均差', 'data_count': cnt, 'quality': qual,
                        'message': f"离均差>50mV: {dev:.1f}mV > 50mV",
                        'sent': False, 'send_error': '测试模式(未发送)',
                    })
            if avg_col:
                avg_v = float(row[avg_col[0]]) if pd.notna(row[avg_col[0]]) else 0
                if 0 < avg_v < 600:
                    alert_events.append({
                        'timestamp': ts, 'cycle_id': cyc, 'power_point': pp,
                        'condition': '平均单体电压<600mV', 'value': avg_v,
                        'threshold': 600.0, 'signal': 'FC_AvgCellVoltage',
                        'unit': 'mV', 'operator': '<', 'label': '平均单体电压',
                        'data_count': cnt, 'quality': qual,
                        'message': f"平均单体电压<600mV: {avg_v:.1f}mV < 600mV",
                        'sent': False, 'send_error': '测试模式(未发送)',
                    })

    if alert_events:
        st.caption(f"检测到 {len(alert_events)} 条预警事件")
        render_alert_log(alert_events)
    else:
        st.success("✅ 当前数据无预警事件")


@tab_safe_render
def _render_tab_contacts() -> None:
    """Tab4: 飞书人员对接。"""
    from components.feishu_contacts import render_feishu_contacts
    render_feishu_contacts()


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
        ["FC_NetPwrOut", "FC_VoltOut", "FC_CurrOut",
         "FC_AvgCellVoltage", "FC_VehicleSpd"],
        index=0,
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
    """Tab7: AI 助手。"""
    st.header("🤖 AI 数据助手")
    st.caption("不知道某项数据是什么意思?问 AI 助手吧。基于《数据说明书》回答,不编造。")

    try:
        from src.ai_assistant import load_llm_config, load_dictionary
        cfg = load_llm_config()
        has_dict = bool(load_dictionary())
        if cfg and has_dict:
            st.success(f"已配置 LLM({cfg['model']}),可智能回答")
        elif has_dict:
            st.info("未配置 LLM,使用本地检索模式(只返回说明书相关段落)。"
                    "配置 config/llm_config.ini 可启用智能回答")
        else:
            st.warning("说明书 docs/DATA_DICTIONARY.md 不存在,请先创建")
    except Exception as e:
        st.error(f"AI 模块加载失败: {e}")

    if "ai_messages" not in st.session_state:
        st.session_state.ai_messages = [
            {"role": "assistant",
             "content": "你好!我是数据助手。可以问我任何关于报告里数字、字段、计算方式的问题,"
                        "比如:\n- \"压差 mean=7.4 是什么意思?\"\n- \"百公里氢耗怎么算出来的?\"\n"
                        "- \"345 的氢耗为什么显示 -?\""}
        ]

    for msg in st.session_state.ai_messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    if prompt := st.chat_input("问任何关于数据的问题..."):
        st.session_state.ai_messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            with st.spinner("思考中..."):
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
    st.subheader("快捷问题")
    quick_qs = [
        "压差 mean=7.4 是什么意思?",
        "百公里氢耗怎么算出来的?",
        "345 车辆氢耗为什么显示 -?",
        "最弱通道 Top1 怎么理解?",
        "电压字段单位是 V 还是 mV?",
    ]
    cols = st.columns(len(quick_qs))
    for i, q in enumerate(quick_qs):
        if cols[i].button(q, key=f"quick_{i}"):
            st.session_state.ai_messages.append({"role": "user", "content": q})
            with st.chat_message("user"):
                st.markdown(q)
            with st.chat_message("assistant"):
                with st.spinner("思考中..."):
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
    st.caption("基于历史数据线性回归预测未来走势,包含压差/氢耗/故障频率/净功率 4 项。")

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
                               "FC_HydCmInstts/FC_ErrorCode/FC_NetPwrOut 等字段")
                else:
                    st.success(f"完成 {len(results)} / 4 项预测")
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

    _perf_sigs = ["FC_VoltOut", "FC_AvgCellVoltage",
                  "FC_NetPwrOut", "FC_MinCellVoltage"]
    agg_df = aggregate_segments(all_segs, _perf_sigs, exclude_anomaly=False)

    if len(agg_df) == 0 or "duration" not in agg_df.columns:
        st.warning("聚合后无有效段(可能全部含异常被剔除)")
        return

    total_dur_h = float(agg_df["duration"].sum()) / 3600.0
    range_sec = max((pd.Timestamp(end_dt) - pd.Timestamp(start_dt)).total_seconds(), 1)
    coverage = min(float(agg_df["duration"].sum()) / range_sec * 100, 100.0)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("📊 有效数据段", f"{len(agg_df)} 个")
    c2.metric("⏱️ 总有效时长", f"{total_dur_h:.2f} 小时")
    c3.metric("📈 数据覆盖率", f"{coverage:.1f}%")
    c4.metric("⚡ 电流点覆盖", f"{len(current_points)} 个")

    st.success(f"分析完成!共找到 {len(all_segs)} 个有效数据段")

    st.markdown("#### 性能趋势(按电流分组,含多项式趋势线)")
    fig_perf = create_performance_figure(
        agg_df, x_col="run_time_at_mid", y_col="FC_AvgCellVoltage_mean",
        group_col="current_target", degree=2, show_trend=True,
        y_label="平均单体电压 (V)",
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

    st.markdown("#### 绝缘阻值趋势 + 报警线 + 预测")
    fig_insul = create_insulation_figure(
        df_insul,
        primary_alarm=_primary,
        secondary_alarm=_secondary,
        predict_days=_forecast,
        poly_order=_degree,
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

# tab_overview: 需要 time_range_preset 侧边栏值做过滤
with tab_overview:
    _render_tab_overview(cars, data, time_range_preset)

with tab_dur:
    _render_tab_durability(dur_df)

with tab_bench:
    _render_tab_bench()

with tab_contacts:
    _render_tab_contacts()

with tab_cmp:
    _render_tab_compare(cars, data)

with tab_report:
    _render_tab_report(cars, data)

with tab_ai:
    # 作为 AI 上下文默认车
    _render_tab_ai(sel_car_default=cars[0] if cars else None)

with tab_forecast:
    _render_tab_forecast(cars, data)

with tab_fc:
    _render_tab_fc(data, fc_data_mode)

with tab_perf:
    _render_tab_performance(data, fc_data_mode)

with tab_insul:
    _render_tab_insulation(data, fc_data_mode)
