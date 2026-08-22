"""Plotly 双Y轴燃料电池数据图表组件(项目核心可视化)。

设计要点:
- 第一个信号绑左轴(yaxis),第二个绑右轴(yaxis2 overlaying y),
  第三个及以后绑左轴。
- 深色科技风:透明背景 + 浅网格 + 科技蓝主色。
- hovermode='x unified' 统一悬停;底部 range slider + 顶部时间快捷选择。
- 数据含 is_anomaly 列时,在最小单体电压曲线上标红圈并加垂直虚线。
"""
from __future__ import annotations

from typing import List

import pandas as pd
import plotly.graph_objects as go

from utils.helpers import SIGNAL_MAP

# ---------- 配色 ----------
# 左轴循环配色
_LEFT_COLORS = ['#00D4FF', '#F5C842', '#2ED573']
# 右轴配色(警示橙)
_RIGHT_COLOR = '#FF6B35'
# 单信号科技蓝
_SINGLE_COLOR = '#00D4FF'
# 异常标记色
_ANOMALY_COLOR = '#FF3B3B'

# ---------- 深色主题常量 ----------
_GRID_COLOR = 'rgba(255,255,255,0.08)'
_FONT_COLOR = '#E8EDF5'
_PLOT_BG = 'rgba(0,0,0,0)'
_PAPER_BG = 'rgba(0,0,0,0)'
_CHART_HEIGHT = 550
_MARGIN = dict(l=60, r=60, t=40, b=40)

# 用于异常标记的最小单体电压信号
_MIN_VOLT_SIG = 'FC_MinCellVoltage'


def _label_of(sig: str) -> str:
    """获取信号中文显示名(含单位),未知信号原样返回。"""
    return SIGNAL_MAP.get(sig, sig)


def _empty_state(fig: go.Figure, text: str) -> go.Figure:
    """渲染空状态提示图(无坐标轴,仅居中文字)。"""
    fig.update_layout(
        paper_bgcolor=_PAPER_BG,
        plot_bgcolor=_PLOT_BG,
        height=_CHART_HEIGHT,
        margin=_MARGIN,
        font=dict(color=_FONT_COLOR),
        xaxis=dict(visible=False),
        yaxis=dict(visible=False),
    )
    fig.add_annotation(
        text=text,
        xref='paper', yref='paper',
        x=0.5, y=0.5,
        showarrow=False,
        font=dict(size=20, color=_FONT_COLOR),
    )
    return fig


def create_figure(df: pd.DataFrame, selected_signals: List[str]) -> go.Figure:
    """构建燃料电池数据双Y轴图表。

    Args:
        df: 含 Timestamp 列的整车数据;可选含 is_anomaly 列触发异常标记
        selected_signals: 选择的信号列名列表

    Returns:
        plotly Figure:
        - 空选择 → 空状态提示图
        - 单信号 → 仅左轴,科技蓝
        - >=2 信号 → 第1个左轴,第2个右轴(yaxis2),第3个+ 左轴
    """
    fig = go.Figure()

    # ---------- 1. 空选择:空状态图 ----------
    if not selected_signals:
        return _empty_state(fig, '请选择至少一个信号')

    # ---------- 空数据兜底 ----------
    if df is None or len(df) == 0 or 'Timestamp' not in df.columns:
        return _empty_state(fig, '无可用数据')

    # ---------- 数据准备 ----------
    work = df.copy()
    work['Timestamp'] = pd.to_datetime(work['Timestamp'], errors='coerce')
    work = work.dropna(subset=['Timestamp']).sort_values('Timestamp')

    has_anomaly = 'is_anomaly' in work.columns
    n = len(selected_signals)
    has_right = n >= 2  # 是否启用右轴

    # ---------- 2/3. 绘制曲线 ----------
    left_idx = 0  # 左轴颜色循环索引
    for i, sig in enumerate(selected_signals):
        if sig not in work.columns:
            continue

        # 决定绑定哪个轴与颜色
        if has_right and i == 1:
            yaxis = 'y2'
            color = _RIGHT_COLOR
        else:
            yaxis = 'y'
            color = _LEFT_COLORS[left_idx % len(_LEFT_COLORS)]
            left_idx += 1

        # 单信号时统一用科技蓝
        if n == 1:
            color = _SINGLE_COLOR

        fig.add_trace(go.Scatter(
            x=work['Timestamp'],
            y=pd.to_numeric(work[sig], errors='coerce'),
            mode='lines',
            name=_label_of(sig),
            line=dict(color=color, width=1.6),
            yaxis=yaxis,
            opacity=0.95,
        ))

        # ---------- 5. 异常标记:仅在最小单体电压曲线上 ----------
        if has_anomaly and sig == _MIN_VOLT_SIG:
            anom = work[work['is_anomaly'].astype(bool)]
            if len(anom) > 0:
                # 红色圆圈标记异常点
                fig.add_trace(go.Scatter(
                    x=anom['Timestamp'],
                    y=pd.to_numeric(anom[sig], errors='coerce'),
                    mode='markers',
                    name='异常点',
                    marker=dict(
                        color=_ANOMALY_COLOR, size=9,
                        line=dict(width=1, color='white'),
                        symbol='circle',
                    ),
                    yaxis=yaxis,
                    showlegend=True,
                ))
                # 垂直虚线标注异常发生时刻
                for t in anom['Timestamp']:
                    fig.add_vline(
                        x=t,
                        line=dict(color=_ANOMALY_COLOR, dash='dash', width=1),
                        opacity=0.5,
                    )

    # ---------- 4/6. 布局与交互 ----------
    left_label = _label_of(selected_signals[0])
    right_label = _label_of(selected_signals[1]) if has_right else None

    yaxis_cfg = dict(
        title=dict(text=left_label, font=dict(color=_FONT_COLOR)),
        gridcolor=_GRID_COLOR,
        zerolinecolor=_GRID_COLOR,
        color=_FONT_COLOR,
    )

    layout_kwargs = dict(
        paper_bgcolor=_PAPER_BG,
        plot_bgcolor=_PLOT_BG,
        height=_CHART_HEIGHT,
        margin=_MARGIN,
        font=dict(color=_FONT_COLOR),
        # 统一悬停:鼠标移动时显示所有曲线在该时刻的值
        hovermode='x unified',
        legend=dict(
            orientation='h', yanchor='bottom', y=1.02,
            xanchor='right', x=1,
            font=dict(color=_FONT_COLOR),
        ),
        xaxis=dict(
            gridcolor=_GRID_COLOR,
            color=_FONT_COLOR,
            # 时间快捷选择按钮
            rangeselector=dict(
                buttons=list([
                    dict(count=5, label='5m',
                         step='minute', stepmode='backward'),
                    dict(count=15, label='15m',
                         step='minute', stepmode='backward'),
                    dict(count=30, label='30m',
                         step='minute', stepmode='backward'),
                    dict(count=1, label='1h',
                         step='hour', stepmode='backward'),
                    dict(step='all', label='全部'),
                ]),
                bgcolor='rgba(255,255,255,0.05)',
                activecolor='rgba(0,212,255,0.3)',
                font=dict(color=_FONT_COLOR),
            ),
            # 底部范围滑动条
            rangeslider=dict(visible=True, thickness=0.05),
            type='date',
        ),
        yaxis=yaxis_cfg,
    )

    # ---------- 双Y轴:右轴配置 ----------
    if has_right:
        layout_kwargs['yaxis2'] = dict(
            title=dict(text=right_label, font=dict(color=_RIGHT_COLOR)),
            gridcolor=_GRID_COLOR,
            zerolinecolor=_GRID_COLOR,
            color=_RIGHT_COLOR,
            overlaying='y',  # 与左轴叠加
            side='right',
        )

    fig.update_layout(**layout_kwargs)
    return fig
