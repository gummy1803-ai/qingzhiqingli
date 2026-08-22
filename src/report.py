"""报告生成模块:HTML 报告模板,可浏览器打印为 PDF。"""
from __future__ import annotations

import base64
import io
import logging
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go

from src.log_config import get_logger
from src.plots import (
    fig_cell_voltage,
    fig_fault_bar,
    fig_power_curve,
    fig_speed_hydrogen,
)

logger = get_logger(__name__)

HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>燃料电池测试报告 - {vehicle}</title>
<style>
* {{ box-sizing: border-box; }}
body {{ font-family: "Microsoft YaHei", "PingFang SC", sans-serif; margin: 40px; color: #222; }}
h1 {{ font-size: 26px; border-bottom: 3px solid #1f77b4; padding-bottom: 10px; }}
h2 {{ font-size: 18px; color: #1f77b4; margin-top: 30px; border-left: 4px solid #1f77b4; padding-left: 8px; }}
table {{ width: 100%; border-collapse: collapse; margin: 10px 0; }}
td, th {{ border: 1px solid #ddd; padding: 6px 10px; text-align: left; font-size: 13px; }}
th {{ background: #f5f5f5; }}
.kpi-grid {{ display: grid; grid-template-columns: repeat(4,1fr); gap: 10px; margin: 15px 0; }}
.kpi {{ background: #f9f9f9; padding: 12px; border-radius: 6px; border-left: 4px solid #1f77b4; }}
.kpi .label {{ font-size: 12px; color: #666; }}
.kpi .value {{ font-size: 22px; font-weight: bold; color: #1f77b4; }}
.chart {{ margin: 15px 0; }}
.note {{ background: #fff8e1; border-left: 4px solid #ff9800; padding: 10px 14px; margin: 10px 0; font-size: 13px; color: #6a4a00; border-radius: 4px; }}
.footer {{ margin-top: 40px; font-size: 12px; color: #888; border-top: 1px solid #ddd; padding-top: 10px; }}
@media print {{ body {{ margin: 15px; }} .no-print {{ display: none; }} }}
</style></head>
<body>
<h1>燃料电池整车测试报告</h1>
<p>车辆编号: <b>{vehicle}</b>  |  生成时间: {gen_time}</p>

<h2>一、关键指标概览</h2>
<div class="kpi-grid">{kpi_cards}</div>

<h2>二、详细指标</h2>
<table>{detail_rows}</table>

<h2>三、单片电压一致性</h2>
<div class="note">
  <b>⚠ 数据单位说明:</b> 经样本数据扫描,本数据集 <code>FC_MinCellVoltage / FC_MaxCellVoltage / FC_AvgCellVoltage</code> 字段数值集中在 100~1000 区间(典型值约 600~900),<b>单位疑似 mV 而非 V</b>(燃料电池单片电压物理范围 0.6~0.9 V,对应 600~900 mV)。<br>
  阅读本章节统计值时请按 mV 理解:例如 mean=739.57 应理解为 <b>0.74 V</b>。<br>
  压差 <code>cell_diff</code> 同理:例如 max=49.00 应理解为 <b>0.049 V (49 mV)</b>,属正常单片压差范围。
</div>
<div class="chart">{fig_cell}</div>

<h2>四、功率与电流</h2>
<div class="chart">{fig_power}</div>

<h2>五、车速与瞬时氢耗</h2>
<div class="chart">{fig_speed}</div>

<h2>六、故障码分布</h2>
<div class="chart">{fig_fault}</div>

<h2>七、结论</h2>
<p>{conclusion}</p>

<div class="footer">本报告由『氢质氢离 · 设备测试数据分析与自动报告助手』自动生成。</div>
</body></html>
"""


def _fig_to_html(fig: go.Figure, full_html: bool = False) -> str:
    # config 关闭工具栏减少 DOM 体积
    return fig.to_html(
        include_plotlyjs="cdn",
        full_html=full_html,
        div_id=None,
        config={"displayModeBar": False, "responsive": True},
    )


def _downsample(df: pd.DataFrame, max_points: int = 1000) -> pd.DataFrame:
    """大数据量降采样到 max_points 条(等间隔抽样)。"""
    if len(df) <= max_points:
        return df
    step = max(1, len(df) // max_points)
    out = df.iloc[::step].reset_index(drop=True)
    logger.info("降采样: %d → %d (step=%d)", len(df), len(out), step)
    return out


def _flat_dict(d: dict, prefix: str = "") -> dict:
    """扁平化嵌套字典,键含路径,便于行表展示。"""
    out: dict[str, str] = {}
    if not isinstance(d, dict):
        return out
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            out.update(_flat_dict(v, key))
        else:
            out[key] = str(v)
    return out


def build_report_html(
    vehicle: str,
    df: pd.DataFrame,
    overview: dict,
    cell_consist: dict,
    power: dict,
    h2: dict,
) -> str:
    """组装完整 HTML 报告。"""
    from datetime import datetime
    logger.info("开始组装 HTML 报告: 车辆=%s 行数=%d", vehicle, len(df))

    # KPI 卡片
    kpi_keys = [
        ("运行时长(h)", "运行时长(h)"),
        ("行驶里程(km)", "行驶里程(km)"),
        ("平均车速(km/h)", "平均车速(km/h)"),
        ("百公里氢耗均值(kg)", "百公里氢耗均值(kg)"),
        ("启动次数", "启动次数"),
        ("故障码种类", "故障码种类"),
        ("采样点数", "采样点数"),
        ("里程末值(km)", "里程末值(km)"),
    ]
    cards = []
    for label, key in kpi_keys:
        v = overview.get(key, "-")
        cards.append(
            f'<div class="kpi"><div class="label">{label}</div>'
            f'<div class="value">{v}</div></div>'
        )

    # 详细表
    detail = {}
    detail.update(_flat_dict({"单片一致性": cell_consist}))
    detail.update(_flat_dict({"功率与效率": power}))
    detail.update(_flat_dict({"氢系统": h2}))
    rows = "".join(
        f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in detail.items()
    )

    # 图表(降采样避免 HTML 体积过大)
    df_s = _downsample(df, max_points=1000)
    fig_cell = _fig_to_html(fig_cell_voltage(df_s))
    fig_power = _fig_to_html(fig_power_curve(df_s))
    fig_speed = _fig_to_html(fig_speed_hydrogen(df_s))
    fig_fault = _fig_to_html(fig_fault_bar(overview.get("故障码Top10", {})))

    # 结论(自动)
    dur = overview.get("运行时长(h)", 0)
    km = overview.get("行驶里程(km)", 0)
    h2c = overview.get("百公里氢耗均值(kg)", 0)
    fault_cnt = overview.get("故障码种类", 0)
    conclusion = (
        f"本次测试车辆 {vehicle},运行 {dur} 小时,行驶 {km} km,"
        f"百公里氢耗 {h2c} kg。"
        f"故障码种类 {fault_cnt} 种。"
        "建议结合单片电压一致性与故障时间轴,关注电压最低单体通道及高频故障码。"
    )

    return HTML_TEMPLATE.format(
        vehicle=vehicle,
        gen_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        kpi_cards="".join(cards),
        detail_rows=rows,
        fig_cell=fig_cell,
        fig_power=fig_power,
        fig_speed=fig_speed,
        fig_fault=fig_fault,
        conclusion=conclusion,
    )
