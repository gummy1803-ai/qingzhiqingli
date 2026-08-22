"""燃电性能统计 - 衰减速率分析与剩余寿命预测模块。

基于稳态段聚合数据(aggregate_segments 输出),对每个电流分组
做线性回归,计算衰减速率/总衰减量/剩余寿命/健康度评分,
并用滚动窗口法判断衰减是否加速。

核心函数: analyze_degradation, create_degradation_figure
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import plotly.graph_objects as go

logger = logging.getLogger(__name__)

# ---------- 样式(与 theme.py / polarization_curve.py 一致) ----------
_SCATTER_COLORS = ['#00D4FF', '#F5C842', '#2ED573',
                   '#FF6B35', '#A78BFA', '#34D399']
_GRID_COLOR = 'rgba(255,255,255,0.08)'
_TEXT_COLOR = '#E8EDF5'
_TITLE_COLOR = '#00D4FF'
_HEALTH_COLORS = {'green': '#2ED573', 'yellow': '#F5C842', 'red': '#FF6B35'}


# ---------- 默认性能阈值 ----------
_DEFAULT_THRESHOLDS = {
    'FC_AvgCellVoltage': 3.0,   # V, 平均单体电压低于此值视为寿命终点
    'FC_MinCellVoltage': 2.5,   # V
    'FC_VoltOut': 200.0,        # V, 电堆输出电压
    'FC_NetPwrOut': 50.0,       # kW
}


def _to_numeric_series(s: pd.Series) -> np.ndarray:
    """安全转数值,datetime 转 timestamp 秒。"""
    if pd.api.types.is_datetime64_any_dtype(s):
        return s.astype('int64').to_numpy() / 1e9
    return pd.to_numeric(s, errors='coerce').to_numpy()


def _linear_fit(x: np.ndarray, y: np.ndarray) -> Optional[dict]:
    """一元线性回归 y = a*x + b,返回 a/b/R2。"""
    mask = np.isfinite(x) & np.isfinite(y)
    x_c, y_c = x[mask], y[mask]
    if len(x_c) < 2:
        return None
    if np.ptp(x_c) == 0:  # x 全相同,无法回归
        return None
    coeffs = np.polyfit(x_c, y_c, 1)  # [a, b]
    pred = np.poly1d(coeffs)(x_c)
    ss_res = float(np.sum((y_c - pred) ** 2))
    ss_tot = float(np.sum((y_c - np.mean(y_c)) ** 2))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0
    return {'slope': float(coeffs[0]), 'intercept': float(coeffs[1]),
            'r_squared': round(float(r2), 4)}


def _rolling_slopes(x: np.ndarray, y: np.ndarray,
                    window: int = 5) -> tuple[list, list]:
    """滚动窗口计算局部斜率,用于判断衰减是否加速。

    Returns:
        (window_center_x, local_slope) 两个等长列表
    """
    mask = np.isfinite(x) & np.isfinite(y)
    x_c, y_c = x[mask], y[mask]
    n = len(x_c)
    if n < window:
        return [], []
    centers, slopes = [], []
    for i in range(n - window + 1):
        xs, ys = x_c[i:i + window], y_c[i:i + window]
        if np.ptp(xs) == 0:
            continue
        a = float(np.polyfit(xs, ys, 1)[0])
        centers.append(float(np.mean(xs)))
        slopes.append(a)
    return centers, slopes


def _health_score(current: float, initial: float,
                  threshold: float) -> tuple[float, str]:
    """健康度评分 0-100,基于当前值距阈值的比例。

    score = (current - threshold) / (initial - threshold) * 100
    绿(>=80) 黄(60-80) 红(<60)
    """
    denom = initial - threshold
    if denom <= 0:  # 初始已低于阈值,无法评分
        return 0.0, 'red'
    score = (current - threshold) / denom * 100
    score = max(0.0, min(100.0, score))
    if score >= 80:
        status = 'green'
    elif score >= 60:
        status = 'yellow'
    else:
        status = 'red'
    return round(score, 1), status


# ---------- 主函数 ----------

def analyze_degradation(
    df_aggregated: pd.DataFrame,
    y_col: str,
    time_col: str = 'run_time_at_mid',
    group_col: str = 'current_target',
    threshold: Optional[float] = None,
    rolling_window: int = 5,
) -> Dict:
    """计算性能衰减速率与剩余寿命。

    Args:
        df_aggregated: aggregate_segments 输出,每行一个稳态段
        y_col: 性能指标列(如 'FC_AvgCellVoltage_mean')
        time_col: 时间列(如 'run_time_at_mid',单位 h)
        group_col: 分组列(如 'current_target')
        threshold: 寿命终点阈值;None 时按 y_col 用默认表推断
        rolling_window: 滚动斜率窗口(点数)

    Returns:
        dict: {
            'groups': [...],          # 各电流分组详细结果
            'summary_table': DataFrame,  # 对比表
            'threshold': float,       # 使用的阈值
            'y_col', 'time_col', 'group_col',
        }
    """
    logger.info("衰减分析: y_col=%s time=%s group=%s rows=%d",
                y_col, time_col, group_col, len(df_aggregated))

    result: Dict = {
        'groups': [], 'summary_table': pd.DataFrame(),
        'threshold': threshold, 'y_col': y_col,
        'time_col': time_col, 'group_col': group_col,
    }

    if df_aggregated is None or len(df_aggregated) == 0:
        logger.warning("衰减分析: 输入为空")
        return result
    if y_col not in df_aggregated.columns:
        logger.error("衰减分析: 缺列 %s", y_col)
        return result
    if time_col not in df_aggregated.columns:
        logger.error("衰减分析: 缺时间列 %s", time_col)
        return result

    # 阈值推断
    if threshold is None:
        # y_col 形如 FC_AvgCellVoltage_mean,去掉 _mean 后缀匹配默认表
        base = y_col.replace('_mean', '').replace('_avg', '')
        threshold = _DEFAULT_THRESHOLDS.get(base)
        if threshold is None:
            logger.warning("衰减分析: 无法推断 %s 的阈值,剩余寿命将不计算",
                           y_col)
    result['threshold'] = threshold

    groups: List[Dict] = []
    if group_col in df_aggregated.columns:
        group_vals = sorted(df_aggregated[group_col].dropna().unique())
    else:
        group_vals = [None]  # 无分组列,单组

    for g in group_vals:
        if g is None:
            sub = df_aggregated
            label = '全部'
        else:
            sub = df_aggregated[df_aggregated[group_col] == g]
            label = f'{g}A'

        t = _to_numeric_series(sub[time_col])
        y = pd.to_numeric(sub[y_col], errors='coerce').to_numpy()
        fit = _linear_fit(t, y)
        if fit is None:
            logger.warning("分组 %s: 样本不足或 x 无变化,跳过", label)
            groups.append({'current_target': g, 'label': label,
                           'skip': True})
            continue

        a, b, r2 = fit['slope'], fit['intercept'], fit['r_squared']
        # 初始/当前预测值(用拟合线外推,比原始点更稳)
        t_min, t_max = float(np.nanmin(t)), float(np.nanmax(t))
        initial_pred = b + a * t_min
        current_pred = b + a * t_max
        total_degradation = float(current_pred - initial_pred)

        # 衰减速率 mV/1000h: a(V/h) * 1e6(mV/V * h->1000h)
        # 即 a V/h = a*1000 mV/h = a*1000*1000 mV/1000h
        slope_mv_per_1000h = a * 1e6

        # 剩余寿命:到阈值的时间
        remaining_life = None
        if threshold is not None and a < 0 and current_pred > threshold:
            remaining_life = (threshold - current_pred) / a
            remaining_life = round(float(remaining_life), 1)

        # 健康度
        if threshold is not None and initial_pred > threshold:
            score, status = _health_score(current_pred, initial_pred,
                                          threshold)
        else:
            score, status = 0.0, 'red'

        # 滚动斜率(判断是否加速衰减)
        roll_x, roll_a = _rolling_slopes(t, y, rolling_window)
        is_accelerating = (len(roll_a) >= 2
                           and roll_a[-1] < roll_a[0] * 1.1
                           and roll_a[-1] < 0)

        grp = {
            'current_target': g, 'label': label,
            'slope_v_per_h': round(a, 8),
            'slope_mv_per_1000h': round(slope_mv_per_1000h, 2),
            'r_squared': r2,
            'intercept': round(b, 4),
            'initial_value': round(float(initial_pred), 4),
            'current_value': round(float(current_pred), 4),
            'total_degradation': round(total_degradation, 4),
            'remaining_life_hours': remaining_life,
            'health_score': score,
            'health_status': status,
            'n_points': int(len(t)),
            't_min': t_min, 't_max': t_max,
            'fit_x': t, 'fit_y': np.poly1d([a, b])(t),
            'rolling_x': roll_x, 'rolling_slope': roll_a,
            'is_accelerating': bool(is_accelerating),
            'slope_predict': lambda x, _a=a, _b=b: _a * x + _b,
        }
        groups.append(grp)
        logger.info("分组 %s: slope=%.2e V/h(%+.1f mV/1000h) R2=%.3f "
                    "initial=%.3f now=%.3f 剩余=%s h 健康=%s(%s)",
                    label, a, slope_mv_per_1000h, r2, initial_pred,
                    current_pred, remaining_life, score, status)

    # 汇总表
    valid_groups = [g for g in groups if not g.get('skip')]
    if valid_groups:
        result['summary_table'] = pd.DataFrame([{
            '电流点': g['label'],
            '衰减速率(mV/1000h)': g['slope_mv_per_1000h'],
            'R²': g['r_squared'],
            '初始值': g['initial_value'],
            '当前值': g['current_value'],
            '总衰减量': g['total_degradation'],
            '剩余寿命(h)': g['remaining_life_hours'],
            '健康度': g['health_score'],
            '健康状态': g['health_status'],
            '样本数': g['n_points'],
        } for g in valid_groups])
    result['groups'] = groups
    logger.info("衰减分析完成: %d 个分组(%d 有效)",
                len(groups), len(valid_groups))
    return result


# ---------- 绘图 ----------

def create_degradation_figure(
    result: Dict,
    df_aggregated: pd.DataFrame,
    y_col: str,
    time_col: str = 'run_time_at_mid',
    group_col: str = 'current_target',
    y_label: str = '',
) -> go.Figure:
    """衰减趋势图:散点 + 线性拟合 + 当前位置标注。"""
    fig = go.Figure()
    if not result.get('groups'):
        fig.update_layout(
            title='衰减速率分析(无数据)',
            plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
            font=dict(color=_TEXT_COLOR),
            annotations=[dict(text='无有效分组', showarrow=False,
                              x=0.5, y=0.5,
                              font=dict(size=16, color='#8892A8'))],
        )
        return fig

    threshold = result.get('threshold')
    y_label = y_label or y_col

    for i, g in enumerate(result['groups']):
        if g.get('skip'):
            continue
        color = _SCATTER_COLORS[i % len(_SCATTER_COLORS)]
        # 原始散点
        sub = (df_aggregated if g['current_target'] is None
               else df_aggregated[df_aggregated[group_col] == g['current_target']])
        t_raw = _to_numeric_series(sub[time_col])
        y_raw = pd.to_numeric(sub[y_col], errors='coerce').to_numpy()
        fig.add_trace(go.Scatter(
            x=t_raw, y=y_raw, mode='markers', name=f"{g['label']} 实测",
            marker=dict(color=color, size=9,
                        line=dict(width=1, color='rgba(255,255,255,0.4)')),
            hovertemplate=f"{g['label']}<br>t=%{{x:.1f}}h<br>{y_label}=%{{y:.3f}}"
                          f"<extra></extra>",
        ))
        # 线性拟合线(延长到 t_max + 10% 便于看趋势)
        t_min, t_max = g['t_min'], g['t_max']
        ext = t_max + (t_max - t_min) * 0.1 if t_max > t_min else t_max + 10
        xs = np.linspace(t_min, ext, 50)
        ys = g['slope_predict'](xs)
        fig.add_trace(go.Scatter(
            x=xs, y=ys, mode='lines', name=f"{g['label']} 拟合",
            line=dict(color=color, width=2, dash='dash'),
            hovertemplate=f"拟合 {g['label']}<br>%{{y:.3f}}<extra></extra>",
        ))
        # 当前位置标注(最新点)
        fig.add_trace(go.Scatter(
            x=[t_max], y=[g['current_value']], mode='markers+text',
            name=f"{g['label']} 当前",
            marker=dict(color=_HEALTH_COLORS[g['health_status']],
                        size=14, symbol='star',
                        line=dict(width=2, color='white')),
            text=[f"{g['health_score']}分"],
            textposition='top center',
            hovertemplate=f"{g['label']} 当前<br>{y_label}={g['current_value']}"
                          f"<br>健康={g['health_score']}({g['health_status']})"
                          f"<br>剩余={g['remaining_life_hours']}h<extra></extra>",
        ))

    # 阈值线
    if threshold is not None:
        fig.add_hline(y=threshold, line_dash='dot',
                      line=dict(color='#FF6B35', width=1.5),
                      annotation_text=f'寿命终点阈值 {threshold}',
                      annotation_position='top left',
                      annotation=dict(font=dict(color='#FF6B35', size=10)))

    fig.update_layout(
        title=dict(text='性能衰减趋势(按电流分组,含线性拟合)',
                   x=0.5, xanchor='center',
                   font=dict(color=_TITLE_COLOR, size=16)),
        plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
        font=dict(color=_TEXT_COLOR),
        hovermode='closest',
        legend=dict(orientation='h', yanchor='bottom', y=1.02),
        margin=dict(l=60, r=60, t=60, b=40), height=480,
    )
    fig.update_xaxes(title_text='累计运行时间 (h)',
                     gridcolor=_GRID_COLOR,
                     tickfont=dict(color=_TEXT_COLOR))
    fig.update_yaxes(title_text=y_label, gridcolor=_GRID_COLOR,
                     tickfont=dict(color=_TEXT_COLOR))
    return fig


# ---------- 单元测试 ----------

if __name__ == '__main__':
    import logging as _lg
    _lg.basicConfig(level=_lg.INFO,
                    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')

    rng = np.random.default_rng(42)

    print("===== 测试1: 已知衰减速率恢复 =====")
    # 电流95A,初始3.8V,衰减速率 -0.0001 V/h = -100 mV/1000h
    # 运行时间 0-2500h(当前值3.55V>阈值3.0,有剩余寿命,健康度落黄色区间)
    t1 = np.linspace(0, 2500, 20)
    y1 = 3.8 + (-0.0001) * t1 + rng.normal(0, 0.005, len(t1))
    df1 = pd.DataFrame({
        'run_time_at_mid': t1, 'FC_AvgCellVoltage_mean': y1,
        'current_target': [95.0] * len(t1),
    })
    r1 = analyze_degradation(df1, 'FC_AvgCellVoltage_mean')
    g1 = r1['groups'][0]
    print(f"  slope={g1['slope_v_per_h']} V/h "
          f"({g1['slope_mv_per_1000h']} mV/1000h)")
    print(f"  R2={g1['r_squared']} initial={g1['initial_value']} "
          f"now={g1['current_value']}")
    print(f"  剩余寿命={g1['remaining_life_hours']}h 健康={g1['health_score']}"
          f"({g1['health_status']})")
    assert r1['threshold'] == 3.0, f"阈值应3.0,实际{r1['threshold']}"
    assert g1['r_squared'] > 0.9, f"R2应>0.9,实际{g1['r_squared']}"
    assert abs(g1['slope_mv_per_1000h'] - (-100)) < 20, \
        f"速率应≈-100,实际{g1['slope_mv_per_1000h']}"
    assert g1['remaining_life_hours'] > 0, "应有剩余寿命"
    assert 60 <= g1['health_score'] <= 80, \
        f"健康度应在60-80(黄),实际{g1['health_score']}"
    print("  [PASS] 衰减速率/剩余寿命/健康度计算正确")

    print("\n===== 测试2: 多电流分组对比 =====")
    # 电流95: -100 mV/1000h;电流150: -200 mV/1000h(衰减更快)
    t95 = np.linspace(0, 8000, 15)
    t150 = np.linspace(0, 8000, 15)
    df2 = pd.DataFrame({
        'run_time_at_mid': np.concatenate([t95, t150]),
        'FC_AvgCellVoltage_mean': np.concatenate([
            3.8 - 0.0001 * t95 + rng.normal(0, 0.005, len(t95)),
            3.8 - 0.0002 * t150 + rng.normal(0, 0.005, len(t150)),
        ]),
        'current_target': np.concatenate([[95.0] * 15, [150.0] * 15]),
    })
    r2 = analyze_degradation(df2, 'FC_AvgCellVoltage_mean')
    print(f"  分组数={len(r2['groups'])} 汇总表行数={len(r2['summary_table'])}")
    for g in r2['groups']:
        if g.get('skip'):
            continue
        print(f"    {g['label']}: {g['slope_mv_per_1000h']} mV/1000h "
              f"R2={g['r_squared']} 健康={g['health_score']}")
    assert len(r2['groups']) == 2
    assert len(r2['summary_table']) == 2
    # 150A 衰减应更快(更负)
    g95 = next(g for g in r2['groups'] if g['label'] == '95.0A')
    g150 = next(g for g in r2['groups'] if g['label'] == '150.0A')
    assert g150['slope_mv_per_1000h'] < g95['slope_mv_per_1000h'], \
        "150A 应衰减更快"
    print("  [PASS] 多电流分组对比 + 汇总表")

    print("\n===== 测试3: 健康度三色标 =====")
    # 健康数据(几乎无衰减)→绿;严重衰减→红;中等→黄
    t_h = np.linspace(0, 5000, 10)
    df_green = pd.DataFrame({'run_time_at_mid': t_h,
                             'FC_AvgCellVoltage_mean': 3.8 - 0.00001 * t_h,
                             'current_target': [95.0] * 10})
    df_red = pd.DataFrame({'run_time_at_mid': t_h,
                          'FC_AvgCellVoltage_mean': 3.2 - 0.00008 * t_h,
                          'current_target': [95.0] * 10})
    rg = analyze_degradation(df_green, 'FC_AvgCellVoltage_mean')['groups'][0]
    rr = analyze_degradation(df_red, 'FC_AvgCellVoltage_mean')['groups'][0]
    print(f"  健康: {rg['health_score']}({rg['health_status']})")
    print(f"  严重: {rr['health_score']}({rr['health_status']})")
    assert rg['health_status'] == 'green', f"应绿,实际{rg['health_status']}"
    assert rr['health_status'] == 'red', f"应红,实际{rr['health_status']}"
    print("  [PASS] 健康度三色标正确")

    print("\n===== 测试4: 滚动斜率(加速衰减检测) =====")
    # 前半段衰减慢,后半段衰减快(加速)
    t_acc = np.linspace(0, 10000, 40)
    y_acc = np.piecewise(t_acc,
                         [t_acc < 5000, t_acc >= 5000],
                         [lambda t: 3.8 - 0.00005 * t,
                          lambda t: 3.8 - 0.00005 * 5000 - 0.0003 * (t - 5000)])
    y_acc += rng.normal(0, 0.002, len(t_acc))
    df4 = pd.DataFrame({'run_time_at_mid': t_acc,
                       'FC_AvgCellVoltage_mean': y_acc,
                       'current_target': [95.0] * len(t_acc)})
    r4 = analyze_degradation(df4, 'FC_AvgCellVoltage_mean')
    g4 = r4['groups'][0]
    print(f"  滚动斜率点数={len(g4['rolling_slope'])} "
          f"首={g4['rolling_slope'][0]:.2e} 尾={g4['rolling_slope'][-1]:.2e} "
          f"加速={g4['is_accelerating']}")
    assert len(g4['rolling_slope']) > 0, "应有滚动斜率"
    assert g4['rolling_slope'][-1] < g4['rolling_slope'][0], \
        "后段斜率应更负(加速衰减)"
    print("  [PASS] 滚动斜率 + 加速检测")

    print("\n===== 测试5: 边界容错 =====")
    # 空数据
    assert len(analyze_degradation(pd.DataFrame(), 'FC_AvgCellVoltage_mean')['groups']) == 0
    # 缺列
    miss = analyze_degradation(pd.DataFrame({'a': [1]}), 'FC_AvgCellVoltage_mean')
    assert len(miss['groups']) == 0
    # 单点(无法回归)
    single = analyze_degradation(
        pd.DataFrame({'run_time_at_mid': [100],
                      'FC_AvgCellVoltage_mean': [3.5],
                      'current_target': [95.0]}),
        'FC_AvgCellVoltage_mean')
    assert single['groups'][0].get('skip')
    print("  [PASS] 空数据/缺列/单点容错")

    print("\n===== 测试6: 绘图 =====")
    fig = create_degradation_figure(r2, df2, 'FC_AvgCellVoltage_mean',
                                    'run_time_at_mid', 'current_target',
                                    '平均单体电压 (V)')
    # 2组 × (1散点+1拟合+1当前) = 6 traces + 1阈值线
    assert len(fig.data) == 6, f"应6条trace,实际{len(fig.data)}"
    fig_empty = create_degradation_figure({'groups': []}, pd.DataFrame(), 'x')
    assert len(fig_empty.data) == 0
    print(f"  traces={len(fig.data)}(2组x3) + 阈值线")
    print("  [PASS] 绘图散点+拟合+当前标注")

    print("\n[OK] 全部测试通过")
