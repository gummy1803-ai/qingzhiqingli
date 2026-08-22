"""绝缘阻值趋势图表组件:散点+报警线+趋势预测+触碰标注(深色科技风)。

绘制 process_insulation_data 输出的绝缘时序散点(按运行/上电状态分色),
叠加主/次报警线、多项式趋势预测线及 95% CI 带、报警触达标注。

核心函数: create_insulation_figure
"""
from __future__ import annotations

import logging

import pandas as pd
import plotly.graph_objects as go

from insulation.predictor import predict_insulation_trend

logger = logging.getLogger(__name__)

# 深色科技风配色(与 state_analyzer / predictor 一致)
_BG = 'rgba(0,0,0,0)'
_GRID = 'rgba(255,255,255,0.08)'
_TEXT = '#E7E9EE'
_STATE4_COLOR = '#00D4FF'      # 运行态 - 青
_STATE8_COLOR = '#FF6B35'      # 上电态 - 橙
_PRIMARY_COLOR = '#FF0000'    # 主报警线 - 红
_SECONDARY_COLOR = '#FFD700'  # 次报警线 - 黄
_PRED_COLOR = '#2ED573'       # 趋势预测线 - 绿


def create_insulation_figure(
    df_insul: pd.DataFrame,
    primary_alarm: float,
    secondary_alarm: float,
    predict_days: int,
    poly_order: int,
) -> go.Figure:
    """绘制绝缘趋势图:实际散点(按状态分色)+ 报警线 + 预测 + 触碰标注。

    Args:
        df_insul: process_insulation_data 输出
        primary_alarm: 主报警线 kΩ
        secondary_alarm: 次报警线 kΩ
        predict_days: 预测天数
        poly_order: 多项式阶数

    Returns:
        plotly Figure
    """
    logger.info("=== create_insulation_figure 开始 ===")
    logger.info("输入: %d 行, 主报警=%s, 次报警=%s, 预测=%d天, 阶数=%d",
                len(df_insul) if df_insul is not None else 0,
                primary_alarm, secondary_alarm, predict_days, poly_order)

    fig = go.Figure()

    # 边界:空数据
    if df_insul is None or len(df_insul) == 0:
        logger.warning("输入为空, 返回空状态图")
        fig.update_layout(
            annotations=[dict(text="无数据", xref="paper", yref="paper",
                              x=0.5, y=0.5, showarrow=False,
                              font=dict(color=_TEXT, size=18))],
            paper_bgcolor=_BG, plot_bgcolor=_BG, height=600,
        )
        return fig

    # 有效点
    work = df_insul.dropna(subset=['FC_VehicleIsolationR']).copy()
    if len(work) == 0:
        logger.warning("无有效绝缘点(全 NaN), 返回空状态图")
        fig.update_layout(
            annotations=[dict(text="无有效绝缘数据", xref="paper", yref="paper",
                              x=0.5, y=0.5, showarrow=False,
                              font=dict(color=_TEXT, size=18))],
            paper_bgcolor=_BG, plot_bgcolor=_BG, height=600,
        )
        return fig

    # 实际散点(按状态分色)
    if 'FC_MainSts' in work.columns:
        s4 = work[work['FC_MainSts'] == 4]
        s8 = work[work['FC_MainSts'] == 8]
    else:
        s4, s8 = work, pd.DataFrame()
    if len(s4):
        fig.add_trace(go.Scatter(
            x=s4['timestamp'], y=s4['FC_VehicleIsolationR'],
            mode='markers', name='运行态(4)',
            marker=dict(color=_STATE4_COLOR, size=6, opacity=0.7),
            hovertemplate='时间: %{x}<br>阻值: %{y:.1f} kΩ<extra>运行态</extra>',
        ))
    if len(s8):
        fig.add_trace(go.Scatter(
            x=s8['timestamp'], y=s8['FC_VehicleIsolationR'],
            mode='markers', name='上电态(8)',
            marker=dict(color=_STATE8_COLOR, size=6, opacity=0.7),
            hovertemplate='时间: %{x}<br>阻值: %{y:.1f} kΩ<extra>上电态</extra>',
        ))
    logger.info("散点: 运行态 %d 点, 上电态 %d 点", len(s4), len(s8))

    # 趋势预测(内部调用 predict_insulation_trend)
    prediction = predict_insulation_trend(
        df_insul,
        alarm_values=[primary_alarm, secondary_alarm],
        predict_days=predict_days,
        poly_order=poly_order,
    )
    if prediction.get('fit_success'):
        preds = prediction.get('predictions', pd.DataFrame())
        if len(preds):
            # 预测线
            fig.add_trace(go.Scatter(
                x=preds['timestamp'], y=preds['value'],
                mode='lines', name='趋势预测',
                line=dict(color=_PRED_COLOR, width=2),
                hovertemplate='时间: %{x}<br>预测: %{y:.1f} kΩ<extra>预测</extra>',
            ))
            # 95% CI 带(upper 正序 + lower 逆序, fill toself)
            ts_fwd = list(preds['timestamp'])
            ts_rev = list(preds['timestamp'][::-1])
            upper_fwd = list(preds['upper'])
            lower_rev = list(preds['lower'][::-1])
            fig.add_trace(go.Scatter(
                x=ts_fwd + ts_rev,
                y=upper_fwd + lower_rev,
                fill='toself', fillcolor='rgba(46,213,115,0.12)',
                line=dict(color='rgba(0,0,0,0)'),
                name='95% CI', hoverinfo='skip',
            ))
            logger.info("预测线 + CI 带绘制: %d 个预测点", len(preds))

        # 报警触达标注
        for alarm, info in prediction.get('alarm_crossings', {}).items():
            days = info.get('days')
            date = info.get('date')
            if days is not None and date is not None:
                color = (_PRIMARY_COLOR if alarm == primary_alarm
                         else _SECONDARY_COLOR)
                fig.add_annotation(
                    x=date, y=alarm,
                    text=f"触碰{alarm}kΩ<br>{days:.0f}天",
                    showarrow=True, arrowhead=1, arrowcolor=color,
                    font=dict(color=color, size=10),
                    bgcolor='rgba(0,0,0,0.6)',
                )
                logger.info("触碰标注: %s kΩ @ %s (%.1f天)", alarm, date, days)
    else:
        logger.warning("趋势拟合失败, 仅显示散点")

    # 报警线(统一 2 条)
    for alarm, color, name in [
        (primary_alarm, _PRIMARY_COLOR, f'主报警 {primary_alarm}kΩ'),
        (secondary_alarm, _SECONDARY_COLOR, f'次报警 {secondary_alarm}kΩ'),
    ]:
        fig.add_hline(
            y=alarm, line_dash='dash', line_color=color, line_width=1.5,
            annotation_text=name, annotation_position='top right',
            annotation_font=dict(color=color, size=11),
        )

    fig.update_layout(
        title=dict(text='绝缘阻值趋势与报警预测',
                   font=dict(color='#00D4FF', size=18)),
        paper_bgcolor=_BG, plot_bgcolor=_BG,
        font=dict(color=_TEXT),
        xaxis=dict(gridcolor=_GRID, zeroline=False),
        yaxis=dict(title='绝缘阻值 (kΩ)', gridcolor=_GRID),
        showlegend=True, height=600,
        margin=dict(l=60, r=30, t=60, b=50),
        legend=dict(orientation='h', yanchor='bottom', y=1.02,
                    xanchor='right', x=1),
    )
    logger.info("=== create_insulation_figure 结束 ===")
    return fig


# ---------- 单元测试 ----------

if __name__ == '__main__':
    import logging as _lg
    import numpy as np
    _lg.basicConfig(level=_lg.INFO,
                    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')

    rng = np.random.default_rng(42)

    def _make_df(n=100, base=500, slope=-2.0, noise=0.5, start='2026-01-01'):
        """生成模拟绝缘数据(1小时间隔, 线性衰减)。"""
        ts = [pd.Timestamp(start) + pd.Timedelta(hours=1 * i) for i in range(n)]
        days = np.array([i / 24.0 for i in range(n)])
        vals = base + slope * days + rng.normal(0, noise, n)
        vals = np.maximum(vals, 0)
        return pd.DataFrame({
            'timestamp': ts,
            'FC_VehicleIsolationR': vals,
            'FC_MainSts': 4,
            'is_running': True,
        })

    print("===== 测试1: 正常数据绘图(含预测+CI+报警线) =====")
    df1 = _make_df(n=100, base=500, slope=-2.0, noise=0.5)
    fig1 = create_insulation_figure(df1, 350, 250, predict_days=30, poly_order=1)
    assert fig1 is not None
    # 至少:运行态散点 + 预测线 + CI带 = 3 条 trace
    assert len(fig1.data) >= 3, f"trace 数应≥3, 实际 {len(fig1.data)}"
    print(f"  trace 数={len(fig1.data)}")
    # 应有报警线(hline → layout.shapes)
    assert len(fig1.layout.shapes) >= 2 or any(
        'hline' in str(fig1.layout) for _ in [0])
    print("  [PASS] 正常绘图 + 预测 + CI + 报警线")

    print("\n===== 测试2: 双状态(4/8 交替) =====")
    df2 = _make_df(n=100, base=500, slope=-1.0, noise=0.5)
    # 一半改状态8
    df2.loc[df2.index[:50], 'FC_MainSts'] = 8
    fig2 = create_insulation_figure(df2, 350, 250, 30, 1)
    # 运行态 + 上电态 + 预测 + CI = 4 条
    assert len(fig2.data) >= 4
    print(f"  trace 数={len(fig2.data)}")
    print("  [PASS] 双状态散点分色")

    print("\n===== 测试3: 空数据容错 =====")
    fig3 = create_insulation_figure(pd.DataFrame(), 350, 250, 30, 1)
    assert fig3 is not None
    assert len(fig3.data) == 0
    assert len(fig3.layout.annotations) >= 1  # "无数据" 标注
    print("  [PASS] 空数据返回空状态图")

    print("\n===== 测试4: 全 NaN 绝缘值容错 =====")
    df4 = _make_df(n=50)
    df4['FC_VehicleIsolationR'] = np.nan
    fig4 = create_insulation_figure(df4, 350, 250, 30, 1)
    assert len(fig4.data) == 0
    print("  [PASS] 全 NaN 返回空状态图")

    print("\n===== 测试5: 拟合失败(点数不足)仍能画散点 =====")
    # 1 个有效点, poly_order=1 需要 ≥2 点 → 拟合失败, 但散点应画出
    df5 = _make_df(n=1, base=500, slope=0, noise=0)
    fig5 = create_insulation_figure(df5, 350, 250, 30, 1)
    assert len(fig5.data) >= 1  # 至少散点
    print(f"  trace 数={len(fig5.data)}(仅散点, 预测跳过)")
    print("  [PASS] 拟合失败时仍画散点")

    print("\n[OK] 全部测试通过")
