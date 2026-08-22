"""燃电性能趋势图表组件。

对 aggregate_segments 输出的段统计 DataFrame,绘制散点图 +
多项式回归趋势线 + 近似95%置信区间 + 公式/R2 标注,
按电流目标值分组着色,用于"燃电性能统计及预测"页面。

核心函数: create_performance_figure
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple

import numpy as np
import pandas as pd
import plotly.graph_objects as go

logger = logging.getLogger(__name__)

# ---------- 常量 ----------
_DEFAULT_DEGREE = 2  # 默认多项式阶数
# 离散色板(与 components/theme.py 科技感配色一致)
_DISCRETE_COLORS = ['#00D4FF', '#F5C842', '#2ED573', '#FF6B35',
                    '#A78BFA', '#FF4757', '#54A0FF', '#5ED0A0']
_GRID_COLOR = 'rgba(255,255,255,0.08)'
_TEXT_COLOR = '#E8EDF5'
_CI_OPACITY = 0.15  # 置信区间透明度


# ---------- 内部工具 ----------

def _prepare_xy(
    df: pd.DataFrame, x_col: str, y_col: str,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """提取 X(显示用) / X(数值用,供 polyfit) / Y。

    x_col='run_time'   -> run_time_at_mid 列(数值)
    x_col='datetime'   -> mid_time 列(datetime,内部转 timestamp 秒拟合)
    其它               -> 当作数值列名直接取
    """
    if x_col == 'run_time':
        x_disp = pd.to_numeric(df.get('run_time_at_mid'),
                               errors='coerce').to_numpy()
        x_num = x_disp.copy()
    elif x_col == 'datetime':
        dt = pd.to_datetime(df.get('mid_time'), errors='coerce')
        x_disp = dt.to_numpy()  # datetime64,plotly 直接识别
        x_num = (dt.astype('int64').to_numpy() / 1e9).astype(float)
    else:
        x_disp = pd.to_numeric(df.get(x_col), errors='coerce').to_numpy()
        x_num = x_disp.copy()
    y = pd.to_numeric(df[y_col], errors='coerce').to_numpy()
    return x_disp, x_num, y


def _fit_poly(
    x: np.ndarray, y: np.ndarray, degree: int = _DEFAULT_DEGREE,
) -> Optional[dict]:
    """多项式拟合,返回系数/多项式对象/R2/残差标准差。

    样本数 < degree+1 时无法拟合,返回 None。
    """
    mask = ~(np.isnan(x) | np.isnan(y))
    x_c, y_c = x[mask], y[mask]
    if len(x_c) < degree + 1:
        logger.warning("拟合样本不足: %d 个(需 >= %d),跳过趋势线",
                       len(x_c), degree + 1)
        return None
    coeffs = np.polyfit(x_c, y_c, degree)
    poly = np.poly1d(coeffs)
    y_pred = poly(x_c)
    ss_res = float(np.sum((y_c - y_pred) ** 2))
    ss_tot = float(np.sum((y_c - np.mean(y_c)) ** 2))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    rstd = float(np.sqrt(ss_res / max(len(y_c) - degree - 1, 1)))
    logger.info("polyfit: degree=%d n=%d R2=%.4f rstd=%.4g coeffs=%s",
                degree, len(x_c), r2, rstd, np.round(coeffs, 4))
    return {'coeffs': coeffs, 'poly': poly, 'r2': r2,
            'rstd': rstd, 'n': len(x_c)}


def _poly_formula(coeffs: np.ndarray) -> str:
    """将多项式系数(降序)渲染为可读公式字符串。"""
    degree = len(coeffs) - 1
    terms = []
    for i, c in enumerate(coeffs):
        p = degree - i
        if abs(c) < 1e-10:
            continue
        sign = '+' if c >= 0 else '-'
        mag = f'{abs(c):.4g}'
        if p == 0:
            terms.append(f'{sign} {mag}')
        elif p == 1:
            terms.append(f'{sign} {mag}x')
        else:
            terms.append(f'{sign} {mag}x<sup>{p}</sup>')
    if not terms:
        return 'y = 0'
    # 首项符号处理
    body = ' '.join(terms).lstrip('+ ').strip()
    if body.startswith('- '):
        return f'y = {body}'
    return f'y = {body}'


def _x_smooth(x_num: np.ndarray, n: int = 100) -> np.ndarray:
    """在 x 数值域内生成 n 个均匀点用于绘制平滑曲线。"""
    if len(x_num) == 0:
        return np.array([])
    return np.linspace(float(np.nanmin(x_num)), float(np.nanmax(x_num)), n)


# ---------- 主绘图函数 ----------

def create_performance_figure(
    df: pd.DataFrame,
    x_col: str,
    y_col: str,
    group_col: str = 'current_target',
    degree: int = _DEFAULT_DEGREE,
    show_trend: bool = True,
    y_label: Optional[str] = None,
) -> go.Figure:
    """绘制性能散点图 + 多项式趋势线 + 置信区间。

    Args:
        df: aggregate_segments 输出(每行一个有效段),需含 x_col 对应列、
            y_col 列;若按电流分组还需 group_col(如 current_target)
        x_col: 'run_time'(累计运行时间) 或 'datetime'(实际日期)
        y_col: Y 轴性能指标列名,如 'FC_AvgCellVoltage_mean'
        group_col: 分组着色列,默认 'current_target'(电流目标值)
        degree: 多项式阶数,默认 2
        show_trend: 是否绘制趋势线与置信区间
        y_label: Y 轴显示名(默认用 y_col)

    Returns:
        plotly Figure: 散点 + 趋势线 + 95%CI + 公式/R2 标注,
        含"显示/隐藏趋势线"按钮
    """
    fig = go.Figure()

    # ---------- 空状态 ----------
    if df is None or len(df) == 0:
        logger.warning("性能图表: 输入为空,返回空状态图")
        fig.update_layout(
            title='性能趋势(无数据)',
            plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
            font=dict(color=_TEXT_COLOR),
            xaxis=dict(visible=False), yaxis=dict(visible=False),
            annotations=[dict(text='无有效数据段',
                               showarrow=False, x=0.5, y=0.5,
                               font=dict(size=16, color='#8892A8'))],
        )
        return fig

    logger.info("性能图表: rows=%d x=%s y=%s group=%s degree=%d trend=%s",
                len(df), x_col, y_col, group_col, degree, show_trend)

    x_disp, x_num, y = _prepare_xy(df, x_col, y_col)

    # ---------- 分组 ----------
    has_group = group_col in df.columns
    if has_group:
        groups = list(pd.Series(df[group_col]).dropna().unique())
    else:
        groups = [None]
    logger.info("分组数: %d%s", len(groups),
                '' if has_group else '(无分组列,单组)')

    formulas: list[str] = []  # 收集各组公式 + R2,用于标题标注
    trend_trace_indices: list[int] = []  # 趋势线/CI trace 索引(供按钮切换)

    for gi, g in enumerate(groups):
        if has_group:
            mask = (df[group_col].to_numpy() == g)
        else:
            mask = np.array([True] * len(df))
        xs_disp, xs_num, ys = x_disp[mask], x_num[mask], y[mask]
        if len(xs_disp) == 0:
            continue
        color = _DISCRETE_COLORS[gi % len(_DISCRETE_COLORS)]
        glabel = f'{g:g}A' if isinstance(g, (int, float)) else str(g)

        # ---------- 散点 ----------
        # customdata: 持续时长 + 平均电流,供 hover 显示
        dur = df['duration'].to_numpy()[mask] if 'duration' in df.columns else [0]*len(xs_disp)
        cur = df['current_avg'].to_numpy()[mask] if 'current_avg' in df.columns else [np.nan]*len(xs_disp)
        custom = np.column_stack([dur, cur])
        fig.add_trace(go.Scatter(
            x=xs_disp, y=ys, mode='markers', name=glabel,
            marker=dict(color=color, size=11,
                        line=dict(width=1, color='rgba(255,255,255,0.4)')),
            customdata=custom,
            hovertemplate=(
                f'<b>{glabel}</b><br>'
                f'{y_col}: %{{y:.3f}}<br>'
                f'持续: %{{customdata[0]}}s<br>'
                f'平均电流: %{{customdata[1]:.2f}}A'
                '<extra></extra>'),
        ))

        # ---------- 趋势线 + 置信区间 ----------
        if show_trend:
            fit = _fit_poly(xs_num, ys, degree)
            if fit is None:
                continue
            poly, r2, rstd = fit['poly'], fit['r2'], fit['rstd']
            x_sm = _x_smooth(xs_num, 100)
            y_sm = poly(x_sm)
            # 显示用 X:datetime 时转回 datetime64
            x_sm_disp = (pd.to_datetime((x_sm * 1e9).astype('int64'))
                         if x_col == 'datetime' else x_sm)
            half = 1.96 * rstd  # 近似 95% 预测带宽

            # 置信区间带(上下界 fill)
            fig.add_trace(go.Scatter(
                x=np.concatenate([x_sm_disp, x_sm_disp[::-1]]),
                y=np.concatenate([y_sm + half, (y_sm - half)[::-1]]),
                fill='toself', fillcolor=color,
                opacity=_CI_OPACITY,
                line=dict(color='rgba(0,0,0,0)'), showlegend=False,
                name=f'{glabel} 95%CI', hoverinfo='skip',
            ))
            trend_trace_indices.append(len(fig.data) - 1)

            # 趋势线
            fig.add_trace(go.Scatter(
                x=x_sm_disp, y=y_sm, mode='lines',
                name=f'{glabel} 趋势 (R2={r2:.3f})',
                line=dict(color=color, width=2, dash='dash'),
            ))
            trend_trace_indices.append(len(fig.data) - 1)

            formulas.append(f'{glabel}: {_poly_formula(fit["coeffs"])} | R2={r2:.3f}')

    # ---------- 公式/R2 标注(标题下方) ----------
    subtitle = ' · '.join(formulas) if formulas else '(趋势线未拟合或样本不足)'
    title_main = f'性能趋势: {y_label or y_col}'
    # plotly 标题不支持副标题,用顶部 annotation 近似
    fig.update_layout(
        title=dict(text=title_main, x=0.5, xanchor='center',
                   font=dict(color='#00D4FF', size=16)),
        annotations=[dict(
            text=subtitle, xref='paper', yref='paper',
            x=0.5, y=1.005, showarrow=False, yanchor='bottom',
            font=dict(color='#8892A8', size=11),
        )] if subtitle else None,
    )

    # ---------- 显示/隐藏趋势线按钮 ----------
    n_traces = len(fig.data)
    if trend_trace_indices:
        # "仅散点":趋势线/CI 隐藏,散点显示
        vis_scatter_only = [
            (i not in trend_trace_indices) for i in range(n_traces)
        ]
        buttons = [
            dict(label='显示趋势线', method='restyle',
                 args=['visible', [True] * n_traces]),
            dict(label='仅散点', method='restyle',
                 args=['visible', vis_scatter_only]),
        ]
        fig.update_layout(updatemenus=[
            dict(active=0, buttons=buttons,
                 x=1.0, y=1.15, xanchor='right', yanchor='top',
                 direction='left', showactive=True,
                 bgcolor='rgba(0,0,0,0)', font=dict(color=_TEXT_COLOR, size=11)),
        ])

    # ---------- 布局样式(深色科技感) ----------
    fig.update_layout(
        plot_bgcolor='rgba(0,0,0,0)',
        paper_bgcolor='rgba(0,0,0,0)',
        font=dict(color=_TEXT_COLOR),
        hovermode='closest',
        legend=dict(orientation='h', yanchor='bottom', y=1.02,
                    font=dict(color=_TEXT_COLOR)),
        margin=dict(l=60, r=60, t=80, b=40),
        height=520,
    )
    fig.update_xaxes(
        title_text='累计运行时间 (h)' if x_col == 'run_time' else '时间',
        gridcolor=_GRID_COLOR, zerolinecolor=_GRID_COLOR,
        tickfont=dict(color=_TEXT_COLOR),
    )
    fig.update_yaxes(
        title_text=y_label or y_col,
        gridcolor=_GRID_COLOR, zerolinecolor=_GRID_COLOR,
        tickfont=dict(color=_TEXT_COLOR),
    )

    logger.info("性能图表完成: traces=%d 趋势traces=%d 公式数=%d",
                n_traces, len(trend_trace_indices), len(formulas))
    return fig


# ---------- 单元测试示例 ----------

if __name__ == '__main__':
    import logging as _lg
    _lg.basicConfig(level=_lg.INFO,
                    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')

    print("===== 测试1: _fit_poly 已知多项式 y=2x^2+3x+1 =====")
    x = np.array([0, 1, 2, 3, 4, 5], dtype=float)
    y = 2 * x ** 2 + 3 * x + 1  # 无噪声,应完美拟合
    fit = _fit_poly(x, y, 2)
    assert fit is not None
    assert abs(fit['coeffs'][0] - 2) < 1e-6, f"a={fit['coeffs'][0]}"
    assert abs(fit['coeffs'][1] - 3) < 1e-6
    assert abs(fit['coeffs'][2] - 1) < 1e-6
    assert abs(fit['r2'] - 1.0) < 1e-9, f"R2={fit['r2']}"
    print(f"  coeffs={np.round(fit['coeffs'],4)} R2={fit['r2']:.6f}")
    print("  [PASS] 系数和R2精确")

    print("\n===== 测试2: _fit_poly 加噪声 R2 下降但拟合 =====")
    rng = np.random.default_rng(42)
    y_noisy = 2 * x ** 2 + 3 * x + 1 + rng.normal(0, 2, len(x))
    fit2 = _fit_poly(x, y_noisy, 2)
    assert fit2 is not None
    assert 0.9 < fit2['r2'] <= 1.0, f"R2={fit2['r2']}"
    print(f"  R2={fit2['r2']:.4f}(噪声下仍较高)")
    print("  [PASS] 噪声数据拟合正常")

    print("\n===== 测试3: _fit_poly 样本不足返回 None =====")
    fit3 = _fit_poly(np.array([1.0, 2.0]), np.array([3.0, 4.0]), 2)
    assert fit3 is None
    print("  [PASS] 样本<degree+1 返回 None")

    print("\n===== 测试4: _poly_formula 公式渲染 =====")
    fml = _poly_formula(np.array([2.0, 3.0, 1.0]))
    print(f"  公式: {fml}")
    assert '2x' in fml and '3x' in fml
    fml2 = _poly_formula(np.array([1.5, -2.0, 0.0]))
    print(f"  公式(零系数项省略): {fml2}")
    assert '0' not in fml2.split('y = ')[1]  # 零系数项不出现
    print("  [PASS] 公式渲染正确")

    print("\n===== 测试5: create_performance_figure 完整渲染 =====")
    # mock aggregate_segments 输出:3个电流分组,每段有 run_time_at_mid 和电压均值
    groups_data = []
    for cur, base_v in [(95.0, 3.70), (100.0, 3.65), (150.0, 3.55)]:
        for i in range(8):
            groups_data.append({
                'current_target': cur,
                'run_time_at_mid': 100 + i * 50,
                'mid_time': pd.Timestamp('2026-08-22') + pd.Timedelta(hours=i),
                'duration': 200,
                'current_avg': cur,
                'FC_AvgCellVoltage_mean': base_v - 0.001 * i + 0.002 * i ** 2,
            })
    df_agg = pd.DataFrame(groups_data)
    # run_time 模式
    fig = create_performance_figure(df_agg, 'run_time',
                                     'FC_AvgCellVoltage_mean',
                                     group_col='current_target', degree=2)
    # 期望:3组 × (1散点 + 1CI + 1趋势线) = 9 traces
    print(f"  run_time模式 traces={len(fig.data)}(期望9)")
    assert len(fig.data) == 9, f"应9条trace,实际{len(fig.data)}"
    assert fig.data[0].type == 'scatter' and fig.data[0].mode == 'markers'
    print("  [PASS] run_time 模式散点+趋势线+CI 完整")

    # datetime 模式
    fig_dt = create_performance_figure(df_agg, 'datetime',
                                       'FC_AvgCellVoltage_mean',
                                       group_col='current_target', degree=2)
    assert len(fig_dt.data) == 9
    print(f"  datetime模式 traces={len(fig_dt.data)}")
    print("  [PASS] datetime 模式正常")

    print("\n===== 测试6: 无趋势线模式 =====")
    fig_no = create_performance_figure(df_agg, 'run_time',
                                      'FC_AvgCellVoltage_mean',
                                      group_col='current_target', show_trend=False)
    # 仅3个散点 trace
    assert len(fig_no.data) == 3, f"应3条散点trace,实际{len(fig_no.data)}"
    print(f"  无趋势线 traces={len(fig_no.data)}(仅散点)")
    print("  [PASS] show_trend=False 只画散点")

    print("\n===== 测试7: 空数据 =====")
    fig_empty = create_performance_figure(pd.DataFrame(), 'run_time',
                                          'FC_AvgCellVoltage_mean')
    assert len(fig_empty.data) == 0
    print("  [PASS] 空数据返回空状态图")

    print("\n===== 测试8: 无分组列(单组) =====")
    df_nog = df_agg.drop(columns=['current_target'])
    fig_sg = create_performance_figure(df_nog, 'run_time',
                                       'FC_AvgCellVoltage_mean',
                                       group_col='current_target')
    # 单组:1散点 + 1CI + 1趋势线 = 3
    assert len(fig_sg.data) == 3
    print(f"  无分组列 traces={len(fig_sg.data)}(单组)")
    print("  [PASS] 无分组列降级为单组")

    print("\n[OK] 全部测试通过")
