"""绝缘阻值状态分布分析模块。

分析整车原始时序数据中绝缘阻值在运行态(FC_MainSts=4)与上电非运行态(FC_MainSts=8)
的分布差异、状态切换事件、以及短时骤降异常,辅助判断绝缘老化模式。

核心函数:
    - analyze_state_distribution: 统计分析,返回汇总字典
    - create_state_distribution_figure: 箱线图 + jitter 散点(深色科技风)
"""
from __future__ import annotations

import logging
from typing import Dict, List

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

try:
    from scipy import stats as _scipy_stats
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False
    logger.warning("scipy 未安装, t 检验将跳过")

# 深色科技风颜色
_STATE4_COLOR = '#00D4FF'   # 运行态 - 青
_STATE8_COLOR = '#FF6B35'   # 上电态 - 橙
_BG_COLOR = 'rgba(0,0,0,0)'
_GRID_COLOR = 'rgba(255,255,255,0.08)'
_TEXT_COLOR = '#E7E9EE'

_DROP_WINDOW_SEC = 3600    # 骤降窗口: 1 小时
_DROP_THRESHOLD = 200.0   # 骤降阈值: 200 kΩ


def _stats_summary(series: np.ndarray) -> Dict:
    """统计摘要。"""
    if len(series) == 0:
        return {'count': 0, 'mean': None, 'std': None, 'min': None,
                'median': None, 'max': None}
    return {
        'count': int(len(series)),
        'mean': float(np.mean(series)),
        'std': float(np.std(series, ddof=1)) if len(series) > 1 else 0.0,
        'min': float(np.min(series)),
        'median': float(np.median(series)),
        'max': float(np.max(series)),
    }


def _empty_result() -> Dict:
    return {
        'state4_stats': _stats_summary(np.array([])),
        'state8_stats': _stats_summary(np.array([])),
        't_test': {'t_stat': None, 'p_value': None, 'significant': False, 'state4_lower': None},
        'state_transitions': [],
        'transition_summary': {'count': 0, '4_to_8': 0, '8_to_4': 0,
                               'state4_lower_in_transitions': None},
        'drop_events': [],
        'drop_summary': {'count': 0, 'total_drop': 0.0, 'avg_drop': 0.0, 'max_drop': 0.0},
        'n_total': 0,
        'n_state4': 0,
        'n_state8': 0,
    }


def analyze_state_distribution(df: pd.DataFrame) -> Dict:
    """分析绝缘阻值在状态4(运行)vs状态8(上电)的分布。

    Args:
        df: 原始整车时序数据,含 Timestamp/FC_VehicleIsolationR/FC_MainSts

    Returns:
        dict: state4_stats/state8_stats/t_test/state_transitions/
        transition_summary/drop_events/drop_summary/n_total/n_state4/n_state8
    """
    logger.info("=== 状态分布分析开始 ===")
    if df is None or len(df) == 0:
        logger.warning("输入为空")
        return _empty_result()
    for col in ['Timestamp', 'FC_VehicleIsolationR', 'FC_MainSts']:
        if col not in df.columns:
            logger.error("缺列: %s", col)
            return _empty_result()

    work = df.copy()
    work['Timestamp'] = pd.to_datetime(work['Timestamp'], errors='coerce')
    work['FC_VehicleIsolationR'] = pd.to_numeric(work['FC_VehicleIsolationR'], errors='coerce')
    work['FC_MainSts'] = pd.to_numeric(work['FC_MainSts'], errors='coerce')
    work = work.dropna(subset=['Timestamp', 'FC_VehicleIsolationR', 'FC_MainSts'])
    n_total = len(work)
    logger.info("输入清洗后: %d 行", n_total)
    if n_total == 0:
        return _empty_result()

    # 功能1: 状态分布统计 + t 检验
    s4 = work.loc[work['FC_MainSts'] == 4, 'FC_VehicleIsolationR'].to_numpy()
    s8 = work.loc[work['FC_MainSts'] == 8, 'FC_VehicleIsolationR'].to_numpy()
    state4_stats = _stats_summary(s4)
    state8_stats = _stats_summary(s8)
    logger.info("状态4(运行): n=%d, mean=%s",
                state4_stats['count'],
                f"{state4_stats['mean']:.2f}" if state4_stats['mean'] is not None else "N/A")
    logger.info("状态8(上电): n=%d, mean=%s",
                state8_stats['count'],
                f"{state8_stats['mean']:.2f}" if state8_stats['mean'] is not None else "N/A")

    t_test = {'t_stat': None, 'p_value': None, 'significant': False, 'state4_lower': None}
    if _HAS_SCIPY and len(s4) >= 2 and len(s8) >= 2:
        t_stat, p_value = _scipy_stats.ttest_ind(s4, s8, equal_var=False)
        significant = bool(p_value < 0.05)
        state4_lower = bool(state4_stats['mean'] < state8_stats['mean'])
        t_test = {
            't_stat': float(t_stat),
            'p_value': float(p_value),
            'significant': significant,
            'state4_lower': state4_lower,
        }
        logger.info("t 检验: t=%.4f, p=%.6f, significant=%s, state4_lower=%s",
                    t_stat, p_value, significant, state4_lower)
    else:
        logger.warning("t 检验跳过(数据不足或 scipy 缺失)")

    # 功能2: 状态切换检测
    state_series = work['FC_MainSts'].to_numpy()
    ts_series = work['Timestamp'].to_numpy()
    val_series = work['FC_VehicleIsolationR'].to_numpy()
    diff = np.diff(state_series)
    change_idx = np.where(diff != 0)[0]
    transitions: List[Dict] = []
    for i in change_idx:
        from_state = int(state_series[i])
        to_state = int(state_series[i + 1])
        ts = pd.Timestamp(ts_series[i + 1])
        before_vals = val_series[max(0, i - 4):i + 1]
        after_vals = val_series[i + 1:i + 6]
        before_mean = float(np.mean(before_vals)) if len(before_vals) else None
        after_mean = float(np.mean(after_vals)) if len(after_vals) else None
        change = (after_mean - before_mean) if (before_mean is not None and after_mean is not None) else None
        transitions.append({
            'timestamp': ts,
            'from_state': from_state,
            'to_state': to_state,
            'before_mean': before_mean,
            'after_mean': after_mean,
            'change': change,
        })
    n_4to8 = sum(1 for t in transitions if t['from_state'] == 4 and t['to_state'] == 8)
    n_8to4 = sum(1 for t in transitions if t['from_state'] == 8 and t['to_state'] == 4)
    changes_valid = [t['change'] for t in transitions if t['change'] is not None]
    state4_lower_in_transitions = bool(np.mean(changes_valid) < 0) if changes_valid else None
    transition_summary = {
        'count': len(transitions),
        '4_to_8': n_4to8,
        '8_to_4': n_8to4,
        'state4_lower_in_transitions': state4_lower_in_transitions,
    }
    logger.info("状态切换: 共 %d 次 (4→8: %d, 8→4: %d)",
                len(transitions), n_4to8, n_8to4)

    # 功能3: 骤降事件扫描(1 小时窗口下降 > 200 kΩ)
    drop_events: List[Dict] = []
    n = len(work)
    i = 0
    last_event_end_idx = -1
    while i < n:
        # 找 i 之后 1 小时内的最远点 j_end
        j_end = i
        while j_end < n and (ts_series[j_end] - ts_series[i]).astype('timedelta64[s]').astype(float) <= _DROP_WINDOW_SEC:
            j_end += 1
        # 窗口 [i, j_end-1]
        if j_end - 1 > i:
            window_vals = val_series[i:j_end]
            min_idx = i + int(np.argmin(window_vals))
            drop = float(val_series[i]) - float(val_series[min_idx])
            if drop > _DROP_THRESHOLD and min_idx > last_event_end_idx:
                drop_events.append({
                    'start_time': pd.Timestamp(ts_series[i]),
                    'end_time': pd.Timestamp(ts_series[min_idx]),
                    'start_value': float(val_series[i]),
                    'end_value': float(val_series[min_idx]),
                    'drop': drop,
                    'state': int(state_series[i]),
                })
                last_event_end_idx = min_idx
                i = min_idx + 1
                continue
        i += 1
    drop_events.sort(key=lambda e: e['drop'], reverse=True)
    if drop_events:
        drops = [e['drop'] for e in drop_events]
        drop_summary = {
            'count': len(drop_events),
            'total_drop': float(np.sum(drops)),
            'avg_drop': float(np.mean(drops)),
            'max_drop': float(np.max(drops)),
        }
    else:
        drop_summary = {'count': 0, 'total_drop': 0.0, 'avg_drop': 0.0, 'max_drop': 0.0}
    logger.info("骤降扫描: %d 个事件(>%.0f kΩ/小时), 最大 drop=%.2f kΩ",
                len(drop_events), _DROP_THRESHOLD, drop_summary['max_drop'])

    result = {
        'state4_stats': state4_stats,
        'state8_stats': state8_stats,
        't_test': t_test,
        'state_transitions': transitions,
        'transition_summary': transition_summary,
        'drop_events': drop_events,
        'drop_summary': drop_summary,
        'n_total': n_total,
        'n_state4': int(len(s4)),
        'n_state8': int(len(s8)),
    }
    logger.info("=== 状态分布分析结束 (n=%d, n4=%d, n8=%d) ===",
                n_total, len(s4), len(s8))
    return result


def create_state_distribution_figure(df: pd.DataFrame):
    """绘制状态4 vs 状态8 的箱线图 + jitter 散点(深色科技风)。"""
    import plotly.graph_objects as go

    logger.info("绘制状态分布图开始")
    res = analyze_state_distribution(df)
    if res['n_total'] == 0:
        fig = go.Figure()
        fig.update_layout(
            annotations=[dict(text="无数据", xref="paper", yref="paper",
                              x=0.5, y=0.5, showarrow=False,
                              font=dict(color=_TEXT_COLOR, size=18))],
            paper_bgcolor=_BG_COLOR, plot_bgcolor=_BG_COLOR, height=600,
        )
        return fig

    work = df.copy()
    work['FC_VehicleIsolationR'] = pd.to_numeric(work['FC_VehicleIsolationR'], errors='coerce')
    work['FC_MainSts'] = pd.to_numeric(work['FC_MainSts'], errors='coerce')
    work = work.dropna(subset=['FC_VehicleIsolationR', 'FC_MainSts'])
    s4 = work.loc[work['FC_MainSts'] == 4, 'FC_VehicleIsolationR']
    s8 = work.loc[work['FC_MainSts'] == 8, 'FC_VehicleIsolationR']

    fig = go.Figure()
    if len(s4):
        fig.add_trace(go.Box(
            y=s4, name='运行态(4)',
            boxpoints=False, marker_color=_STATE4_COLOR,
            line_color=_STATE4_COLOR,
            fillcolor='rgba(0,212,255,0.15)',
        ))
        rng = np.random.default_rng(0)
        jitter = rng.uniform(-0.3, 0.3, len(s4))
        fig.add_trace(go.Scatter(
            x=[0 + j for j in jitter], y=s4,
            mode='markers', name='运行态散点',
            marker=dict(color=_STATE4_COLOR, size=5, opacity=0.6),
            hovertemplate='阻值: %{y:.1f} kΩ<extra>运行态</extra>',
        ))
    if len(s8):
        fig.add_trace(go.Box(
            y=s8, name='上电态(8)',
            boxpoints=False, marker_color=_STATE8_COLOR,
            line_color=_STATE8_COLOR,
            fillcolor='rgba(255,107,53,0.15)',
        ))
        rng = np.random.default_rng(1)
        jitter = rng.uniform(-0.3, 0.3, len(s8))
        fig.add_trace(go.Scatter(
            x=[1 + j for j in jitter], y=s8,
            mode='markers', name='上电态散点',
            marker=dict(color=_STATE8_COLOR, size=5, opacity=0.6),
            hovertemplate='阻值: %{y:.1f} kΩ<extra>上电态</extra>',
        ))

    fig.update_layout(
        title=dict(text='绝缘阻值状态分布对比', font=dict(color='#00D4FF', size=18)),
        paper_bgcolor=_BG_COLOR, plot_bgcolor=_BG_COLOR,
        font=dict(color=_TEXT_COLOR),
        xaxis=dict(gridcolor=_GRID_COLOR, zeroline=False),
        yaxis=dict(title='绝缘阻值 (kΩ)', gridcolor=_GRID_COLOR),
        showlegend=True, height=600,
        margin=dict(l=60, r=30, t=60, b=50),
    )
    logger.info("绘制状态分布图完成 (n4=%d, n8=%d)", len(s4), len(s8))
    return fig


# ---------- 单元测试 ----------

if __name__ == '__main__':
    import logging as _lg
    _lg.basicConfig(level=_lg.INFO,
                    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')

    rng = np.random.default_rng(42)

    def _make_raw(n=200, base4=800, base8=1000, switch_every=50):
        """生成原始时序数据(4/8 交替)。"""
        ts = [pd.Timestamp('2026-01-01') + pd.Timedelta(seconds=10 * i) for i in range(n)]
        states = [4 if (i // switch_every) % 2 == 0 else 8 for i in range(n)]
        vals = []
        for i, s in enumerate(states):
            base = base4 if s == 4 else base8
            vals.append(base + rng.normal(0, 30))
        return pd.DataFrame({
            'Timestamp': ts,
            'FC_VehicleIsolationR': vals,
            'FC_MainSts': states,
        })

    print("\n===== 测试1: 状态分布统计 + t 检验 =====")
    df1 = _make_raw(n=200, base4=800, base8=1000, switch_every=50)
    r1 = analyze_state_distribution(df1)
    assert r1['n_total'] == 200
    assert r1['n_state4'] + r1['n_state8'] == 200
    assert r1['state4_stats']['mean'] < r1['state8_stats']['mean']
    assert r1['t_test']['significant']
    assert r1['t_test']['state4_lower'] == True
    print(f"  state4 mean={r1['state4_stats']['mean']:.2f}, state8 mean={r1['state8_stats']['mean']:.2f}")
    print(f"  t={r1['t_test']['t_stat']:.4f}, p={r1['t_test']['p_value']:.6f}")
    print("  [PASS] 状态分布 + t 检验正确")

    print("\n===== 测试2: 状态切换检测 =====")
    df2 = _make_raw(n=200, base4=800, base8=1000, switch_every=50)
    r2 = analyze_state_distribution(df2)
    # 200点每50切换 → 切换次数≥3 (50→8, 100→4, 150→8)
    assert r2['transition_summary']['count'] >= 3
    assert r2['transition_summary']['4_to_8'] >= 1
    assert r2['transition_summary']['8_to_4'] >= 1
    print(f"  切换={r2['transition_summary']['count']}, 4→8={r2['transition_summary']['4_to_8']}, 8→4={r2['transition_summary']['8_to_4']}")
    print("  [PASS] 状态切换检测正确")

    print("\n===== 测试3: 骤降事件检测 =====")
    df3 = _make_raw(n=200, base4=1000, base8=1200, switch_every=200)  # 全状态4
    # 100-200 点注入下降(1000→500, 500kΩ drop over 1000秒≈17分钟)
    for i in range(100, 200):
        df3.loc[i, 'FC_VehicleIsolationR'] = 1000 - (i - 100) * 5
    r3 = analyze_state_distribution(df3)
    print(f"  骤降事件数={r3['drop_summary']['count']}, max_drop={r3['drop_summary']['max_drop']:.2f}")
    assert r3['drop_summary']['count'] >= 1
    assert r3['drop_summary']['max_drop'] > 200
    print("  [PASS] 骤降事件检测正确")

    print("\n===== 测试4: 边界-空数据/缺列 =====")
    assert analyze_state_distribution(pd.DataFrame())['n_total'] == 0
    assert analyze_state_distribution(pd.DataFrame({'Timestamp': [], 'X': []}))['n_total'] == 0
    print("  [PASS] 空数据/缺列处理正确")

    print("\n===== 测试5: 图表生成 =====")
    df5 = _make_raw(n=100)
    fig = create_state_distribution_figure(df5)
    assert fig is not None
    assert len(fig.data) >= 2
    print(f"  trace 数={len(fig.data)}")
    print("  [PASS] 图表生成成功")

    print("\n所有测试通过 [OK]")
