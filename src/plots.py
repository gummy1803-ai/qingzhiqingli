"""Plotly 图表模块:整车/耐久/对比图表。"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots


def _safe_num(series: pd.Series) -> pd.Series:
    """安全数值化:非数值列强制转 float,无法转换的变 NaN。

    用于在过滤之前防御字符串/混合类型列导致的 TypeError。
    """
    if pd.api.types.is_numeric_dtype(series):
        return series
    return pd.to_numeric(series, errors="coerce")


def _base_layout(title: str, height: int = 400) -> dict:
    return dict(
        title=title,
        height=height,
        margin=dict(l=40, r=20, t=50, b=40),
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
        template="plotly_white",
    )


def fig_cell_voltage(df: pd.DataFrame) -> go.Figure:
    """单片电压一致性曲线:最大/最小/平均叠加。"""
    fig = go.Figure()
    cols = [("FC_MaxCellVoltage", "最大"), ("FC_AvgCellVoltage", "平均"),
            ("FC_MinCellVoltage", "最小")]
    for col, name in cols:
        if col in df.columns:
            sub = df[["Timestamp", col]].copy()
            v = _safe_num(sub[col])
            sub = sub[(v > 0) & (v < 2000)]
            fig.add_trace(go.Scatter(
                x=sub["Timestamp"], y=sub[col],
                mode="lines", name=name, line=dict(width=1.2),
            ))
    fig.update_layout(**_base_layout("单片电压一致性"))
    fig.update_yaxes(title_text="电压 (V)")
    return fig


def fig_power_curve(df: pd.DataFrame) -> go.Figure:
    """功率/电流/电压叠加(双 Y 轴)。"""
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    if "FC_NetPwrOut" in df.columns:
        sub = df[["Timestamp", "FC_NetPwrOut"]].copy()
        v = _safe_num(sub["FC_NetPwrOut"])
        sub = sub[(v > 0) & (v < 100000)]
        fig.add_trace(go.Scatter(
            x=sub["Timestamp"], y=sub["FC_NetPwrOut"],
            name="净功率(kW)", line=dict(color="#1f77b4", width=1.2),
        ), secondary_y=False)
    if "FC_CurrOut" in df.columns:
        sub = df[["Timestamp", "FC_CurrOut"]].copy()
        v = _safe_num(sub["FC_CurrOut"])
        sub = sub[(v > 0) & (v < 1000)]
        fig.add_trace(go.Scatter(
            x=sub["Timestamp"], y=sub["FC_CurrOut"],
            name="电流(A)", line=dict(color="#ff7f0e", width=1),
        ), secondary_y=True)
    fig.update_layout(**_base_layout("功率与电流"))
    fig.update_yaxes(title_text="功率 (kW)", secondary_y=False)
    fig.update_yaxes(title_text="电流 (A)", secondary_y=True)
    return fig


def fig_speed_hydrogen(df: pd.DataFrame) -> go.Figure:
    """车速与瞬时氢耗曲线(双 Y 轴)。"""
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    if "FC_VehicleSpd" in df.columns:
        sub = df[["Timestamp", "FC_VehicleSpd"]].copy()
        v = _safe_num(sub["FC_VehicleSpd"])
        sub = sub[(v >= 0) & (v < 200)]
        fig.add_trace(go.Scatter(
            x=sub["Timestamp"], y=sub["FC_VehicleSpd"],
            name="车速(km/h)", line=dict(color="#2ca02c", width=1.2),
        ), secondary_y=False)
    if "FC_HydCmInstts" in df.columns:
        sub = df[["Timestamp", "FC_HydCmInstts"]].copy()
        v = _safe_num(sub["FC_HydCmInstts"])
        sub = sub[(v >= 0) & (v < 1000)]
        fig.add_trace(go.Scatter(
            x=sub["Timestamp"], y=sub["FC_HydCmInstts"],
            name="瞬时氢耗(kg/h)", line=dict(color="#d62728", width=1),
        ), secondary_y=True)
    fig.update_layout(**_base_layout("车速与瞬时氢耗"))
    fig.update_yaxes(title_text="车速 (km/h)", secondary_y=False)
    fig.update_yaxes(title_text="氢耗 (kg/h)", secondary_y=True)
    return fig


def fig_fault_bar(fault_top: dict) -> go.Figure:
    """故障码 Top10 柱图。"""
    if not fault_top:
        fig = go.Figure()
        fig.update_layout(
            title="故障码分布(无故障)",
            height=300,
            template="plotly_white",
            xaxis=dict(visible=False),
            yaxis=dict(visible=False),
            annotations=[dict(text="无故障码", showarrow=False, x=0.5, y=0.5)],
        )
        return fig
    codes = [str(k) for k in fault_top.keys()]
    counts = list(fault_top.values())
    fig = go.Figure(go.Bar(
        x=codes, y=counts, text=counts, textposition="outside",
        marker_color="#9467bd",
    ))
    fig.update_layout(**_base_layout("故障码 Top10", height=300))
    fig.update_xaxes(title_text="故障码")
    fig.update_yaxes(title_text="出现次数")
    return fig


def fig_durability_trend(dur_df: pd.DataFrame) -> go.Figure:
    """耐久阶段趋势图(每 5h 一段)。

    输入:耐久长表中按 stage 分组的统计(预先聚合好的 DataFrame),
    列:[stage_order, stage, mean_voltage, mean_power]
    """
    fig = go.Figure()
    if "mean_voltage" in dur_df.columns:
        fig.add_trace(go.Scatter(
            x=dur_df["stage"], y=dur_df["mean_voltage"],
            mode="lines+markers", name="平均电压",
            line=dict(color="#1f77b4", width=2),
        ))
    fig.update_layout(**_base_layout("耐久衰减趋势(按 5h 段)"))
    fig.update_xaxes(title_text="耐久阶段")
    fig.update_yaxes(title_text="电压 (V)")
    return fig


def fig_compare_overlay(dfs: list[pd.DataFrame], col: str,
                        labels: list[str] | None = None) -> go.Figure:
    """多车同指标叠加曲线(以相对时间为 X,便于横向对比)。

    Args:
        dfs: 多辆车的 DataFrame 列表(可 2 辆及以上)
        col: 要对比的指标列名
        labels: 每辆车的标签(默认用 0/1/2...)
    """
    fig = go.Figure()

    def _rel_time(df: pd.DataFrame) -> pd.Series:
        if "Timestamp" not in df.columns or len(df) == 0:
            return pd.Series([])
        return (df["Timestamp"] - df["Timestamp"].iloc[0]).dt.total_seconds() / 3600

    if labels is None:
        labels = [str(i) for i in range(len(dfs))]

    for i, (df, lab) in enumerate(zip(dfs, labels)):
        if col not in df.columns:
            continue
        sub = df[["Timestamp", col]].copy()
        v = _safe_num(sub[col])
        sub = sub[(v > 0) & (v < 100000)]
        if len(sub) == 0:
            continue
        sub["t"] = _rel_time(sub)
        fig.add_trace(go.Scatter(
            x=sub["t"], y=sub[col], mode="lines",
            name=lab, line=dict(width=1.2),
        ))
    n = len(dfs)
    fig.update_layout(**_base_layout(f"{n} 车横向对比 - {col}"))
    fig.update_xaxes(title_text="相对时间 (h)")
    fig.update_yaxes(title_text=col)
    return fig


def fig_before_after_overlay(df: pd.DataFrame, col: str,
                              t0, t1, t2, t3,
                              label_before: str = "前段",
                              label_after: str = "后段") -> go.Figure:
    """同辆车前后两时段对比叠加(以相对时间为 X)。

    Args:
        df: 单辆车完整 DataFrame
        col: 对比指标列名
        t0, t1: 前段起止时间
        t2, t3: 后段起止时间
        label_before, label_after: 图例标签
    """
    fig = go.Figure()

    def _rel_time(df: pd.DataFrame) -> pd.Series:
        if "Timestamp" not in df.columns or len(df) == 0:
            return pd.Series([])
        return (df["Timestamp"] - df["Timestamp"].iloc[0]).dt.total_seconds() / 3600

    if col not in df.columns:
        fig.update_layout(title=f"列 {col} 不存在")
        return fig

    segs = [(t0, t1, label_before), (t2, t3, label_after)]
    for ts, te, lab in segs:
        sub = df[(df["Timestamp"] >= pd.Timestamp(ts))
                 & (df["Timestamp"] <= pd.Timestamp(te))][["Timestamp", col]].copy()
        v = _safe_num(sub[col])
        sub = sub[(v > 0) & (v < 100000)]
        if len(sub) == 0:
            continue
        sub["t"] = _rel_time(sub)
        fig.add_trace(go.Scatter(
            x=sub["t"], y=sub[col], mode="lines",
            name=lab, line=dict(width=1.2),
        ))
    fig.update_layout(**_base_layout(f"前后对比 - {col}"))
    fig.update_xaxes(title_text="相对时间 (h)")
    fig.update_yaxes(title_text=col)
    return fig
