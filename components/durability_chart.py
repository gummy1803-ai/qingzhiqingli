"""台架耐久数据可视化组件。

按循环和功率点展示信号变化趋势,支持多信号对比。
提供两种视图(通过 Streamlit 子 Tab 切换):
1. 循环衰减趋势(按功率分面): 每个功率点一个子图, X=循环编号, Y=信号值, 含线性趋势线
2. 功率特性曲线(按循环分面): 每个循环一个子图, X=功率点, Y=信号值, 含极化拟合

核心函数:
- create_durability_figure: 纯 plotly 图表生成(无 Streamlit 依赖)
- render_durability_chart: Streamlit UI 封装(Tab 切换 + 图表渲染)
"""
from __future__ import annotations

import logging
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

logger = logging.getLogger(__name__)

# ---------- 颜色方案 ----------
# 信号颜色: 不同色系, 高对比度(适配深色主题)
_SIGNAL_PALETTE: List[str] = [
    '#EF553B',  # 红
    '#00CC96',  # 绿
    '#FFA15A',  # 橙
    '#AB63FA',  # 紫
    '#636EFA',  # 蓝
    '#FECB52',  # 黄
    '#FF6692',  # 粉
    '#19A3A3',  # 青
]

# 循环 0-4: 浅蓝 -> 深蓝渐变(用于 View2 子图标题着色, 指示循环进展)
_CYCLE_COLORS: List[str] = [
    '#BBD6F5',  # 浅蓝(循环0)
    '#7FB0E8',
    '#4A8AD8',
    '#1E66C2',
    '#0A3A8C',  # 深蓝(循环4)
]

# 深色科技主题(与 components/theme.py 一致)
_PAPER_BG = 'rgba(11,14,23,0.4)'
_PLOT_BG = 'rgba(17,21,36,0.6)'
_GRID_COLOR = 'rgba(255,255,255,0.06)'
_FONT_COLOR = '#E5E7EB'
_TRENDLINE_OPACITY = 0.45

# 默认功率点(与 durability_filter.py / data_parser.py 一致)
_DEFAULT_POWER_POINTS: List[float] = [33.0, 58.5, 117.0, 156.0, 175.5, 195.0]


# ---------- 内部工具函数 ----------

def _resolve_signal_column(df: pd.DataFrame, signal: str,
                           agg_method: str = 'mean') -> Optional[str]:
    """从聚合 DataFrame 解析信号对应列名。

    聚合输出列名格式: {signal}_{agg_method} (如 FC_AvgCellVoltage_mean)。
    匹配优先级: {signal}_{agg_method} > {signal}_* (排除 _std) > signal 本身。
    """
    # 1. 精确匹配 {signal}_{agg_method}
    col = f'{signal}_{agg_method}'
    if col in df.columns:
        return col
    # 2. 匹配 {signal}_ 前缀(排除 _std 稳定性列)
    candidates = [c for c in df.columns
                  if c.startswith(f'{signal}_') and not c.endswith('_std')]
    if candidates:
        return candidates[0]
    # 3. 直接匹配
    if signal in df.columns:
        return signal
    return None


def _fit_trend(
    x: np.ndarray,
    y: np.ndarray,
    degree: int = 1,
) -> Tuple[Optional[np.ndarray], Optional[np.ndarray]]:
    """多项式拟合趋势线, 返回 (x_fit, y_fit) 或 (None, None)。

    防御性处理: NaN / 长度不足 / X 全相同 / 矩阵奇异。
    """
    if len(x) == 0:
        return None, None
    mask = ~(np.isnan(x) | np.isnan(y))
    x_c, y_c = x[mask], y[mask]
    if len(x_c) < degree + 1:
        return None, None
    if np.nanstd(x_c) == 0:
        return None, None
    try:
        coeffs = np.polyfit(x_c, y_c, degree)
        x_fit = np.linspace(float(x_c.min()), float(x_c.max()), 50)
        y_fit = np.polyval(coeffs, x_fit)
        return x_fit, y_fit
    except (np.linalg.LinAlgError, ValueError) as e:
        logger.warning("趋势拟合失败(deg=%d n=%d): %s", degree, len(x_c), e)
        return None, None


def _build_hovertext(cycle, power, signal, value, data_count,
                     quality) -> str:
    """构建数据点 hover 文本(点击/悬停显示详细信息)。"""
    if pd.isna(value) or value is None:
        val_str = 'N/A'
    else:
        val_str = f'{float(value):.4f}'
    cyc_str = (f'{int(cycle)}' if not pd.isna(cycle) else 'N/A')
    return (f"循环: {cyc_str}<br>"
            f"功率点: {float(power):.1f} kW<br>"
            f"信号: {signal}<br>"
            f"数值: {val_str}<br>"
            f"数据量: {data_count}<br>"
            f"质量: {quality}")


def _apply_dark_theme(fig: go.Figure) -> go.Figure:
    """应用深色科技主题(与 components/theme.py 配色一致)。"""
    fig.update_layout(
        paper_bgcolor=_PAPER_BG,
        plot_bgcolor=_PLOT_BG,
        font=dict(color=_FONT_COLOR, size=11),
        legend=dict(
            bgcolor='rgba(0,0,0,0)',
            bordercolor='rgba(255,255,255,0.1)',
            borderwidth=1,
            orientation='h',
            yanchor='bottom', y=1.02,
            xanchor='right', x=1,
        ),
        margin=dict(l=50, r=20, t=60, b=40),
        hovermode='closest',
        dragmode='zoom',  # 默认拖拽=缩放(X/Y 均可)
    )
    fig.update_xaxes(
        gridcolor=_GRID_COLOR,
        zerolinecolor=_GRID_COLOR,
        linecolor='rgba(255,255,255,0.2)',
        tickfont=dict(size=10),
    )
    fig.update_yaxes(
        gridcolor=_GRID_COLOR,
        zerolinecolor=_GRID_COLOR,
        linecolor='rgba(255,255,255,0.2)',
        tickfont=dict(size=10),
    )
    return fig


def _make_placeholder(text: str) -> go.Figure:
    """生成占位空图(数据为空时使用)。"""
    fig = go.Figure()
    fig.add_annotation(
        text=text,
        xref='paper', yref='paper', x=0.5, y=0.5,
        showarrow=False, font=dict(size=16, color='#888'),
    )
    return _apply_dark_theme(fig)


# ---------- 主函数 ----------

def create_durability_figure(
    df_agg: pd.DataFrame,
    signal_cols: List[str],
    selected_powers: List[float],
    view_mode: str = 'cycle_trend',
    agg_method: str = 'mean',
) -> go.Figure:
    """创建耐久数据可视化图表。

    Args:
        df_agg: aggregate_durability_stats 输出的聚合 DataFrame, 需含
                cycle_id, power_point, {signal}_{agg_method}, 数据量, 质量标记
        signal_cols: 展示的信号列(如 ['FC_AvgCellVoltage', 'FC_AvgCellVoltDev'])
        selected_powers: 选中的功率点(如 [33.0, 58.5, 117.0])
        view_mode: 'cycle_trend'=循环衰减趋势(按功率分面)
                   'power_curve'=功率特性曲线(按循环分面)
        agg_method: 聚合方法列后缀(mean/median/min/max)

    Returns:
        plotly Figure 对象(空数据返回带提示的占位图)
    """
    logger.info("耐久图表渲染开始: view=%s signals=%s pps=%s rows=%d",
                view_mode, signal_cols, selected_powers,
                len(df_agg) if df_agg is not None else 0)

    # ---------- 输入校验 ----------
    if df_agg is None or len(df_agg) == 0:
        logger.warning("聚合数据为空, 返回占位图")
        return _make_placeholder("暂无聚合数据, 请先在筛选栏选择台架/功率点并加载数据")

    if not signal_cols:
        logger.warning("未选择信号列, 返回占位图")
        return _make_placeholder("请至少选择一个展示信号")

    if view_mode == 'cycle_trend':
        return _build_cycle_trend_view(df_agg, signal_cols,
                                       selected_powers, agg_method)
    elif view_mode == 'power_curve':
        return _build_power_curve_view(df_agg, signal_cols,
                                       selected_powers, agg_method)
    else:
        logger.error("未知 view_mode: %s, 回退到 cycle_trend", view_mode)
        return _build_cycle_trend_view(df_agg, signal_cols,
                                       selected_powers, agg_method)


# ---------- 视图1: 循环衰减趋势(按功率分面) ----------

def _build_cycle_trend_view(
    df_agg: pd.DataFrame,
    signal_cols: List[str],
    selected_powers: List[float],
    agg_method: str,
) -> go.Figure:
    """视图1: 循环衰减趋势(按功率分面)。

    每个功率点一个子图, X=循环编号, Y=信号值, 多信号对比 + 线性趋势线。
    适用场景: 观察同一功率点下性能随循环次数的衰减。
    """
    # 校验必需列
    if 'cycle_id' not in df_agg.columns or 'power_point' not in df_agg.columns:
        logger.error("聚合数据缺少 cycle_id 或 power_point 列")
        return _make_placeholder("聚合数据缺少 cycle_id / power_point 列")

    # 选取实际存在于数据中且被选中的功率点
    available_powers = sorted(df_agg['power_point'].unique())
    if selected_powers:
        powers = [p for p in available_powers
                  if any(np.isclose(p, sp) for sp in selected_powers)]
    else:
        powers = list(available_powers)
    if not powers:
        powers = list(available_powers)[:6]
    n_powers = len(powers)

    # 行列布局(默认 2x3)
    if n_powers <= 3:
        n_rows, n_cols = 1, max(n_powers, 1)
    else:
        n_rows = (n_powers + 2) // 3
        n_cols = 3

    subplot_titles = [f'⚡ {p:.1f} kW' for p in powers]
    fig = make_subplots(
        rows=n_rows, cols=n_cols,
        subplot_titles=subplot_titles,
        vertical_spacing=0.15,
        horizontal_spacing=0.07,
    )

    trace_count = 0
    for i, power in enumerate(powers):
        row = i // n_cols + 1
        col = i % n_cols + 1
        sub = df_agg[np.isclose(df_agg['power_point'], power)].sort_values('cycle_id')

        for sig_idx, signal in enumerate(signal_cols):
            col_name = _resolve_signal_column(df_agg, signal, agg_method)
            if col_name is None or col_name not in sub.columns:
                logger.warning("功率 %.1f kW 信号 %s 无对应列, 跳过", power, signal)
                continue

            color = _SIGNAL_PALETTE[sig_idx % len(_SIGNAL_PALETTE)]
            x = sub['cycle_id'].to_numpy(dtype=float)
            y = pd.to_numeric(sub[col_name], errors='coerce').to_numpy(dtype=float)
            dcounts = (sub['数据量'].to_numpy() if '数据量' in sub.columns
                       else np.full(len(sub), 0))
            quals = (sub['质量标记'].to_numpy() if '质量标记' in sub.columns
                     else np.full(len(sub), '', dtype=object))

            hover_texts = [
                _build_hovertext(c, power, signal, yv, dc, q)
                for c, yv, dc, q in zip(x, y, dcounts, quals)
            ]

            # 数据折线+标记点(点击显示详细信息)
            fig.add_trace(
                go.Scatter(
                    x=x, y=y,
                    mode='lines+markers',
                    name=signal,
                    line=dict(color=color, width=2),
                    marker=dict(
                        size=7, color=color,
                        line=dict(width=1, color='rgba(255,255,255,0.4)'),
                        symbol='circle',
                    ),
                    text=hover_texts,
                    hovertemplate='%{text}<extra></extra>',
                    showlegend=(i == 0),  # 只在第一个子图显示图例
                    legendgroup=signal,
                ),
                row=row, col=col,
            )
            trace_count += 1

            # 趋势线(线性, 虚线)
            x_fit, y_fit = _fit_trend(x, y, degree=1)
            if x_fit is not None:
                fig.add_trace(
                    go.Scatter(
                        x=x_fit, y=y_fit,
                        mode='lines',
                        name=f'{signal} 趋势',
                        line=dict(color=color, width=1.5, dash='dash'),
                        opacity=_TRENDLINE_OPACITY,
                        showlegend=False,
                        hoverinfo='skip',
                        legendgroup=signal,
                    ),
                    row=row, col=col,
                )

        # 子图坐标轴标题
        fig.update_xaxes(title_text='循环编号', row=row, col=col)
        fig.update_yaxes(title_text='信号值', row=row, col=col)

    fig.update_layout(
        title=dict(
            text='📈 循环衰减趋势(按功率点分面)',
            font=dict(size=16, color=_FONT_COLOR),
        ),
        height=420 * n_rows,
    )
    logger.info("视图1 完成: %d 个功率点子图, %d 条数据 trace", n_powers, trace_count)
    return _apply_dark_theme(fig)


# ---------- 视图2: 功率特性曲线(按循环分面) ----------

def _build_power_curve_view(
    df_agg: pd.DataFrame,
    signal_cols: List[str],
    selected_powers: List[float],
    agg_method: str,
) -> go.Figure:
    """视图2: 功率特性曲线(按循环分面)。

    每个循环一个子图, X=功率点, Y=信号值, 多信号对比 + 极化拟合(二次)。
    子图标题按循环序号着色(浅蓝->深蓝渐变, 指示循环进展)。
    适用场景: 观察同一循环下不同功率点的性能表现。
    """
    if 'cycle_id' not in df_agg.columns or 'power_point' not in df_agg.columns:
        logger.error("聚合数据缺少 cycle_id 或 power_point 列")
        return _make_placeholder("聚合数据缺少 cycle_id / power_point 列")

    # 只取完整循环(cycle_id >= 0), 按编号排序
    cycles = sorted([int(c) for c in df_agg['cycle_id'].unique() if c >= 0])
    n_cycles = len(cycles)
    if n_cycles == 0:
        logger.warning("无完整循环数据(cycle_id >= 0)")
        return _make_placeholder("无完整循环数据(cycle_id >= 0)")

    if n_cycles <= 3:
        n_rows, n_cols = 1, max(n_cycles, 1)
    else:
        n_rows = (n_cycles + 2) // 3
        n_cols = 3

    subplot_titles = [f'🔄 循环 {c}' for c in cycles]
    fig = make_subplots(
        rows=n_rows, cols=n_cols,
        subplot_titles=subplot_titles,
        vertical_spacing=0.15,
        horizontal_spacing=0.07,
    )

    # 给子图标题着色(循环渐变: 浅蓝->深蓝)
    for i in range(n_cycles):
        color = _CYCLE_COLORS[i % len(_CYCLE_COLORS)]
        # make_subplots 的标题是 annotations 的前 n_cycles 个
        if i < len(fig.layout.annotations):
            fig.layout.annotations[i].font = dict(color=color, size=13)

    trace_count = 0
    for i, cycle in enumerate(cycles):
        row = i // n_cols + 1
        col = i % n_cols + 1
        sub = df_agg[df_agg['cycle_id'] == cycle].sort_values('power_point')

        # 只保留选中的功率点
        if selected_powers:
            mask = sub['power_point'].apply(
                lambda p: any(np.isclose(p, sp) for sp in selected_powers)
            )
            sub = sub[mask]

        for sig_idx, signal in enumerate(signal_cols):
            col_name = _resolve_signal_column(df_agg, signal, agg_method)
            if col_name is None or col_name not in sub.columns:
                logger.warning("循环 %d 信号 %s 无对应列, 跳过", cycle, signal)
                continue

            color = _SIGNAL_PALETTE[sig_idx % len(_SIGNAL_PALETTE)]
            x = sub['power_point'].to_numpy(dtype=float)
            y = pd.to_numeric(sub[col_name], errors='coerce').to_numpy(dtype=float)
            dcounts = (sub['数据量'].to_numpy() if '数据量' in sub.columns
                       else np.full(len(sub), 0))
            quals = (sub['质量标记'].to_numpy() if '质量标记' in sub.columns
                     else np.full(len(sub), '', dtype=object))

            hover_texts = [
                _build_hovertext(cycle, p, signal, yv, dc, q)
                for p, yv, dc, q in zip(x, y, dcounts, quals)
            ]

            # 数据折线+标记点
            fig.add_trace(
                go.Scatter(
                    x=x, y=y,
                    mode='lines+markers',
                    name=signal,
                    line=dict(color=color, width=2),
                    marker=dict(
                        size=7, color=color,
                        line=dict(width=1, color='rgba(255,255,255,0.4)'),
                        symbol='diamond',
                    ),
                    text=hover_texts,
                    hovertemplate='%{text}<extra></extra>',
                    showlegend=(i == 0),
                    legendgroup=signal,
                ),
                row=row, col=col,
            )
            trace_count += 1

            # 极化曲线趋势(二次多项式拟合, 点线)
            x_fit, y_fit = _fit_trend(x, y, degree=2)
            if x_fit is not None:
                fig.add_trace(
                    go.Scatter(
                        x=x_fit, y=y_fit,
                        mode='lines',
                        name=f'{signal} 拟合',
                        line=dict(color=color, width=1.5, dash='dot'),
                        opacity=_TRENDLINE_OPACITY,
                        showlegend=False,
                        hoverinfo='skip',
                        legendgroup=signal,
                    ),
                    row=row, col=col,
                )

        fig.update_xaxes(title_text='功率点 (kW)', row=row, col=col)
        fig.update_yaxes(title_text='信号值', row=row, col=col)

    fig.update_layout(
        title=dict(
            text='📊 功率特性曲线(按循环分面)',
            font=dict(size=16, color=_FONT_COLOR),
        ),
        height=420 * n_rows,
    )
    logger.info("视图2 完成: %d 个循环子图, %d 条数据 trace", n_cycles, trace_count)
    return _apply_dark_theme(fig)


# ---------- Streamlit UI 封装 ----------

def render_durability_chart(
    df_agg: pd.DataFrame,
    signal_cols: List[str],
    selected_powers: List[float],
    agg_method: str = 'mean',
) -> None:
    """Streamlit UI: 子 Tab 切换两种视图并渲染图表。

    Args:
        df_agg: 聚合后的 DataFrame
        signal_cols: 展示信号列表
        selected_powers: 选中的功率点
        agg_method: 聚合方法(mean/median/min/max)
    """
    import streamlit as st

    logger.info("渲染耐久图表 UI: signals=%s pps=%s agg=%s",
                signal_cols, selected_powers, agg_method)

    tab1, tab2 = st.tabs([
        '📈 循环衰减趋势(按功率分面)',
        '📊 功率特性曲线(按循环分面)',
    ])

    with tab1:
        fig1 = create_durability_figure(
            df_agg, signal_cols, selected_powers,
            view_mode='cycle_trend', agg_method=agg_method,
        )
        st.plotly_chart(fig1, use_container_width=True, key='durability_chart_cycle_trend',
                        config={'scrollZoom': True, 'displayModeBar': True})

    with tab2:
        fig2 = create_durability_figure(
            df_agg, signal_cols, selected_powers,
            view_mode='power_curve', agg_method=agg_method,
        )
        st.plotly_chart(fig2, use_container_width=True, key='durability_chart_power_curve',
                        config={'scrollZoom': True, 'displayModeBar': True})


# ---------- 单元测试 ----------

def _make_test_agg_df(n_cycles: int = 5,
                      powers: Optional[List[float]] = None) -> pd.DataFrame:
    """构造模拟聚合数据(类似 aggregate_durability_stats 输出)。"""
    if powers is None:
        powers = _DEFAULT_POWER_POINTS
    np.random.seed(42)
    rows = []
    for c in range(n_cycles):
        for p in powers:
            # 模拟衰减: 循环越大电压越低, 功率越大电压越低
            v1 = 3.65 - 0.02 * c - 0.001 * p + np.random.randn() * 0.005
            v2 = 0.05 + 0.003 * c + np.random.randn() * 0.003
            rows.append({
                'cycle_id': c,
                'power_point': float(p),
                'FC_AvgCellVoltage_mean': round(float(v1), 4),
                'FC_AvgCellVoltage_std': round(float(np.random.rand() * 0.002), 4),
                'FC_AvgCellVoltDev_mean': round(float(v2), 4),
                'FC_AvgCellVoltDev_std': round(float(np.random.rand() * 0.003), 4),
                'FC_NetPwrOut_mean': round(float(p * 0.95), 4),
                '数据量': 50 + int(np.random.rand() * 20),
                '质量标记': '正常',
            })
    return pd.DataFrame(rows)


if __name__ == '__main__':
    import re
    import sys
    import logging as _lg
    # Windows 控制台 GBK 编码无法打印 emoji, 强制 UTF-8 输出
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    _lg.basicConfig(level=_lg.INFO,
                    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')

    def _strip_emoji(s: str) -> str:
        """去除 emoji 以兼容 GBK 控制台打印。"""
        return re.sub(r'[^\x00-\x7F\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]+',
                      '', str(s)).strip()

    print("===== 测试1: 空数据返回占位图 =====")
    fig = create_durability_figure(pd.DataFrame(), ['FC_AvgCellVoltage'],
                                   [33.0])
    assert len(fig.data) == 0
    assert len(fig.layout.annotations) == 1
    assert "暂无聚合数据" in fig.layout.annotations[0].text
    print("  [PASS] 空数据 -> 占位图")

    print("\n===== 测试2: 无信号列返回占位图 =====")
    df = _make_test_agg_df()
    fig = create_durability_figure(df, [], [33.0])
    assert len(fig.data) == 0
    assert "至少选择一个展示信号" in fig.layout.annotations[0].text
    print("  [PASS] 无信号 -> 占位图")

    print("\n===== 测试3: 视图1(循环衰减)正常渲染 =====")
    df = _make_test_agg_df(n_cycles=5)
    fig = create_durability_figure(
        df, ['FC_AvgCellVoltage', 'FC_AvgCellVoltDev'],
        _DEFAULT_POWER_POINTS, view_mode='cycle_trend',
    )
    # 6 个功率点 -> 2x3 布局
    assert len(fig.data) > 0, "应有数据 trace"
    # 子图标题数 = 6
    titles = [a.text for a in fig.layout.annotations if a.text and 'kW' in str(a.text)]
    assert len(titles) == 6, f"应有6个功率点子图标题, 实际{len(titles)}"
    print(f"  数据 trace 数: {len(fig.data)}, 子图标题: {[_strip_emoji(t) for t in titles]}")
    print("  [PASS] 视图1 渲染 6 个功率点子图")

    print("\n===== 测试4: 视图2(功率特性)正常渲染 =====")
    fig2 = create_durability_figure(
        df, ['FC_AvgCellVoltage', 'FC_AvgCellVoltDev'],
        _DEFAULT_POWER_POINTS, view_mode='power_curve',
    )
    assert len(fig2.data) > 0
    titles2 = [a.text for a in fig2.layout.annotations
               if a.text and '循环' in str(a.text)]
    assert len(titles2) == 5, f"应有5个循环子图标题, 实际{len(titles2)}"
    print(f"  数据 trace 数: {len(fig2.data)}, 子图标题: {[_strip_emoji(t) for t in titles2]}")
    print("  [PASS] 视图2 渲染 5 个循环子图")

    print("\n===== 测试5: 子图标题着色(循环渐变) =====")
    colors = [a.font.color for a in fig2.layout.annotations
              if a.text and '循环' in str(a.text)]
    assert len(colors) == 5
    for i, c in enumerate(colors):
        expected = _CYCLE_COLORS[i % len(_CYCLE_COLORS)]
        assert c == expected, f"循环{i} 颜色 {c} != {expected}"
    print(f"  循环标题颜色: {colors}")
    print("  [PASS] 循环0-4 使用浅蓝->深蓝渐变")

    print("\n===== 测试6: 缺失信号列容错 =====")
    df_missing = df.drop(columns=['FC_NetPwrOut_mean'])
    fig = create_durability_figure(
        df_missing, ['FC_NetPwrOut', 'FC_AvgCellVoltage'],
        [33.0], view_mode='cycle_trend',
    )
    # FC_NetPwrOut 被跳过, FC_AvgCellVoltage 正常
    assert len(fig.data) > 0
    # 只应有 FC_AvgCellVoltage 的 trace(不含 NetPwrOut)
    names = [t.name for t in fig.data if t.name and 'FC_AvgCellVoltage' in str(t.name)]
    assert len(names) > 0, "FC_AvgCellVoltage 应有 trace"
    netpwr_names = [t.name for t in fig.data if t.name and 'NetPwrOut' in str(t.name)]
    assert len(netpwr_names) == 0, "FC_NetPwrOut 应被跳过"
    print(f"  FC_AvgCellVoltage traces: {len(names)}, FC_NetPwrOut traces: {len(netpwr_names)}(应为0)")
    print("  [PASS] 缺失信号列被优雅跳过")

    print("\n===== 测试7: hover 文本包含完整信息 =====")
    fig = create_durability_figure(
        df, ['FC_AvgCellVoltage'], [33.0], view_mode='cycle_trend',
    )
    first_trace = fig.data[0]
    assert first_trace.text is not None, "应有 hover 文本"
    sample_text = first_trace.text[0]
    assert "循环" in sample_text, "hover 应含循环"
    assert "功率点" in sample_text, "hover 应含功率点"
    assert "信号" in sample_text, "hover 应含信号"
    assert "数据量" in sample_text, "hover 应含数据量"
    assert "质量" in sample_text, "hover 应含质量"
    print(f"  hover 示例: {sample_text.replace('<br>', ' | ')}")
    print("  [PASS] hover 含循环/功率/信号/数值/数据量/质量")

    print("\n===== 测试8: 趋势线存在(虚线/点线) =====")
    fig1 = create_durability_figure(
        df, ['FC_AvgCellVoltage'], _DEFAULT_POWER_POINTS,
        view_mode='cycle_trend',
    )
    # 数据 trace + 趋势 trace
    trend_traces = [t for t in fig1.data
                    if t.line and t.line.dash in ('dash', 'dot')
                    and t.showlegend is False]
    assert len(trend_traces) > 0, "应有趋势线 trace"
    print(f"  视图1 趋势线数: {len(trend_traces)} (dash=虚线, 线性)")

    fig2 = create_durability_figure(
        df, ['FC_AvgCellVoltage'], _DEFAULT_POWER_POINTS,
        view_mode='power_curve',
    )
    trend_traces2 = [t for t in fig2.data
                     if t.line and t.line.dash in ('dash', 'dot')
                     and t.showlegend is False]
    assert len(trend_traces2) > 0, "应有极化拟合 trace"
    print(f"  视图2 极化拟合数: {len(trend_traces2)} (dot=点线, 二次)")
    print("  [PASS] 两视图均含趋势线")

    print("\n===== 测试9: 选定功率点过滤 =====")
    # 只选 2 个功率点
    fig = create_durability_figure(
        df, ['FC_AvgCellVoltage'], [33.0, 58.5],
        view_mode='cycle_trend',
    )
    titles = [a.text for a in fig.layout.annotations if a.text and 'kW' in str(a.text)]
    assert len(titles) == 2, f"应只渲染2个功率点, 实际{len(titles)}"
    assert '33.0 kW' in titles[0]
    assert '58.5 kW' in titles[1]
    print(f"  选定 [33.0, 58.5] -> 渲染子图: {[_strip_emoji(t) for t in titles]}")
    print("  [PASS] 功率点过滤生效")

    print("\n===== 测试10: 深色主题应用 =====")
    fig = create_durability_figure(
        df, ['FC_AvgCellVoltage'], [33.0], view_mode='cycle_trend',
    )
    assert fig.layout.paper_bgcolor == _PAPER_BG
    assert fig.layout.plot_bgcolor == _PLOT_BG
    assert fig.layout.font.color == _FONT_COLOR
    assert fig.layout.dragmode == 'zoom', "dragmode 应为 zoom(支持缩放)"
    print(f"  paper_bg={fig.layout.paper_bgcolor}")
    print(f"  plot_bg={fig.layout.plot_bgcolor}")
    print(f"  font_color={fig.layout.font.color}")
    print(f"  dragmode={fig.layout.dragmode}")
    print("  [PASS] 深色主题 + 缩放模式已应用")

    print("\n===== 测试11: 单信号渲染 =====")
    fig = create_durability_figure(
        df, ['FC_AvgCellVoltage'], _DEFAULT_POWER_POINTS,
        view_mode='power_curve',
    )
    # 每个循环子图至少 1 条数据 trace
    data_traces = [t for t in fig.data if t.showlegend is not False or t.showlegend is None]
    assert len(fig.data) > 0
    print(f"  单信号 trace 总数: {len(fig.data)}")
    print("  [PASS] 单信号正常渲染")

    print("\n===== 测试12: 图例可切换(legendgroup 设置) =====")
    fig = create_durability_figure(
        df, ['FC_AvgCellVoltage', 'FC_AvgCellVoltDev'],
        _DEFAULT_POWER_POINTS, view_mode='cycle_trend',
    )
    # 检查 legendgroup 已设置(图例点击可隐藏/显示整组)
    groups = set(t.legendgroup for t in fig.data if t.legendgroup)
    assert 'FC_AvgCellVoltage' in groups
    assert 'FC_AvgCellVoltDev' in groups
    print(f"  legendgroup: {groups}")
    print("  [PASS] 图例分组(legendgroup)已设置, 支持点击隐藏/显示")

    print("\n===== 测试13: _resolve_signal_column 列解析 =====")
    assert _resolve_signal_column(df, 'FC_AvgCellVoltage', 'mean') == 'FC_AvgCellVoltage_mean'
    # agg_method 不匹配时回退到前缀匹配
    assert _resolve_signal_column(df, 'FC_AvgCellVoltage', 'median') == 'FC_AvgCellVoltage_mean'
    # 不存在的信号
    assert _resolve_signal_column(df, 'NonExistent', 'mean') is None
    print("  mean 精确匹配 -> _mean")
    print("  median 回退 -> _mean(前缀匹配)")
    print("  不存在 -> None")
    print("  [PASS] 信号列解析优先级正确")

    print("\n===== 测试14: _fit_trend 边界处理 =====")
    # 正常拟合
    x = np.array([0, 1, 2, 3, 4], dtype=float)
    y = np.array([3.6, 3.55, 3.5, 3.45, 3.4], dtype=float)
    xf, yf = _fit_trend(x, y, degree=1)
    assert xf is not None and yf is not None
    assert len(xf) == 50
    # 长度不足
    xf2, _ = _fit_trend(np.array([0, 1]), np.array([1, 2]), degree=2)
    assert xf2 is None
    # X 全相同
    xf3, _ = _fit_trend(np.array([5, 5, 5]), np.array([1, 2, 3]), degree=1)
    assert xf3 is None
    # 含 NaN
    xf4, _ = _fit_trend(np.array([0, np.nan, 2, 3]), np.array([1, 2, 3, 4]), degree=1)
    assert xf4 is not None, "NaN 应被过滤后拟合"
    print("  正常拟合: 50 点")
    print("  长度不足: None")
    print("  X 全相同: None")
    print("  含 NaN: 过滤后拟合成功")
    print("  [PASS] 趋势拟合边界处理正确")

    print("\n===== 测试15: render_durability_chart 函数存在 =====")
    assert callable(render_durability_chart)
    import inspect
    sig = inspect.signature(render_durability_chart)
    params = list(sig.parameters.keys())
    assert params == ['df_agg', 'signal_cols', 'selected_powers', 'agg_method']
    print(f"  签名: {sig}")
    print("  [PASS] render_durability_chart 就绪(需 Streamlit 运行时测试)")

    print("\n[OK] 全部测试通过")
