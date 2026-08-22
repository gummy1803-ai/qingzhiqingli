"""多车绝缘阻值对比模块。

在同一图表中对比多辆车的绝缘阻值趋势,包含衰减速率、报警触达预测、健康度。

核心函数:
    - create_vehicle_comparison: 多车对比图 + 结果列表
    - generate_comparison_table: 结果转 DataFrame(按健康度升序, 最差在上)

日志要点(用户重点要求):
    - _load_and_process: 车辆/时间/数据源行数/process_insulation_data 返回行数/有效点
    - predict_insulation_trend: R²/health/days(通过 predictor 模块日志)
    - create_vehicle_comparison: 车辆数/成功数/失败数 + 每车完成汇总
    - generate_comparison_table: 输入行数
"""
from __future__ import annotations

import logging
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from insulation.data_processor import process_insulation_data
from insulation.predictor import predict_insulation_trend

logger = logging.getLogger(__name__)

# 8 色离散色板
_VEHICLE_COLORS = [
    '#00D4FF', '#F5C842', '#2ED573', '#FF6B35',
    '#9467BD', '#E7E9EE', '#54A0FF', '#FF6B9D',
]

# 深色科技风
_BG_COLOR = 'rgba(0,0,0,0)'
_GRID_COLOR = 'rgba(255,255,255,0.08)'
_TEXT_COLOR = '#E7E9EE'
_PRIMARY_ALARM_COLOR = '#FF0000'    # 主报警线 红
_SECONDARY_ALARM_COLOR = '#FFD700'  # 次报警线 黄


def _get_data_dict() -> Dict[str, pd.DataFrame]:
    """从 streamlit session_state 获取 data dict(容错)。"""
    try:
        import streamlit as st
        data = st.session_state.get('data', {}) or {}
        if not data:
            data = st.session_state.get('insulation_raw_data', {}) or {}
        return data
    except Exception:
        return {}


def _load_and_process(vehicle_id: str, start_time, end_time) -> pd.DataFrame:
    """从全局 data 加载车辆数据 → 清洗聚合为绝缘时序。

    日志: 车辆/时间/数据源行数 → process_insulation_data 返回行数/有效点
    """
    logger.info("_load_and_process 开始: 车辆=%s, 时间=[%s, %s]",
                vehicle_id, start_time, end_time)

    data = _get_data_dict()
    if not data:
        logger.warning("data dict 为空(session_state 未设置 data)")
        return pd.DataFrame()

    if vehicle_id not in data:
        logger.warning("车辆 %s 无数据(可用车辆: %s)",
                       vehicle_id, list(data.keys()))
        return pd.DataFrame()

    raw = data[vehicle_id]
    logger.info("车辆 %s 数据源: %d 行, 列(前5)=%s",
                vehicle_id, len(raw), list(raw.columns)[:5])

    # 时间过滤
    if 'Timestamp' in raw.columns and start_time is not None and end_time is not None:
        raw = raw.copy()
        raw['Timestamp'] = pd.to_datetime(raw['Timestamp'], errors='coerce')
        mask = ((raw['Timestamp'] >= pd.Timestamp(start_time)) &
                (raw['Timestamp'] <= pd.Timestamp(end_time)))
        raw = raw.loc[mask]
        logger.info("车辆 %s 时间过滤后: %d 行", vehicle_id, len(raw))

    if len(raw) == 0:
        logger.warning("车辆 %s 过滤后无数据, 跳过", vehicle_id)
        return pd.DataFrame()

    # 清洗 + 聚合
    df_insul = process_insulation_data(raw)
    n_valid = int(df_insul['FC_VehicleIsolationR'].notna().sum()) \
        if 'FC_VehicleIsolationR' in df_insul.columns else 0
    logger.info("车辆 %s process_insulation_data 返回: %d 行, 有效点 %d",
                vehicle_id, len(df_insul), n_valid)
    return df_insul


def create_vehicle_comparison(
    vehicle_ids: List[str],
    start_time,
    end_time,
    alarm_values: List[float] = [350, 250],
) -> Tuple[go.Figure, List[Dict]]:
    """多车绝缘阻值对比图 + 结果列表。

    Args:
        vehicle_ids: 车辆 ID 列表(建议 ≥2)
        start_time: 起始时间
        end_time: 结束时间
        alarm_values: [主报警, 次报警] kΩ

    Returns:
        (fig, result): fig 为 plotly Figure;
        result 为每车汇总 [{vehicle_id, current, degradation_rate,
        forecast_350, forecast_250, health_score, color}]
    """
    logger.info("=== create_vehicle_comparison 开始 ===")
    logger.info("输入: 车辆数=%d, ids=%s, 时间=[%s, %s], alarm=%s",
                len(vehicle_ids), vehicle_ids, start_time, end_time, alarm_values)

    primary_alarm, secondary_alarm = alarm_values[0], alarm_values[-1]
    result: List[Dict] = []
    n_success = 0
    n_fail = 0

    fig = go.Figure()

    # 边界: 空车列表
    if not vehicle_ids:
        logger.warning("车辆列表为空, 返回空状态图")
        fig.update_layout(
            annotations=[dict(text="请选择至少一辆车进行对比",
                              xref="paper", yref="paper",
                              x=0.5, y=0.5, showarrow=False,
                              font=dict(color=_TEXT_COLOR, size=18))],
            paper_bgcolor=_BG_COLOR, plot_bgcolor=_BG_COLOR, height=600,
        )
        return fig, []

    # 边界: 单车提示
    if len(vehicle_ids) == 1:
        logger.warning("仅选择 1 辆车, 建议至少 2 辆以进行横向对比")

    # 逐车处理
    for idx, vid in enumerate(vehicle_ids):
        color = _VEHICLE_COLORS[idx % len(_VEHICLE_COLORS)]
        logger.info("--- 处理车辆 %s (idx=%d, color=%s) ---", vid, idx, color)

        # 加载 + 清洗
        df_insul = _load_and_process(vid, start_time, end_time)
        if len(df_insul) == 0:
            logger.warning("车辆 %s 无可用绝缘数据, 跳过", vid)
            n_fail += 1
            continue

        # 有效点
        valid = df_insul.dropna(subset=['FC_VehicleIsolationR'])
        if len(valid) == 0:
            logger.warning("车辆 %s 清洗后无有效点, 跳过", vid)
            n_fail += 1
            continue

        # 散点(每车一条线)
        fig.add_trace(go.Scatter(
            x=valid['timestamp'], y=valid['FC_VehicleIsolationR'],
            mode='markers', name=f'车辆 {vid}',
            marker=dict(color=color, size=6, opacity=0.75),
            hovertemplate=(f'车辆: {vid}<br>时间: %{{x}}<br>阻值: %{{y:.1f}} kΩ'
                           '<br>状态: %{{customdata}}<extra></extra>'),
            customdata=valid['FC_MainSts'].astype(str) if 'FC_MainSts' in valid.columns else '',
        ))

        # 趋势拟合
        prediction = predict_insulation_trend(
            df_insul, alarm_values=alarm_values, predict_days=30, poly_order=1
        )
        r_squared = prediction.get('r_squared', 0)
        health = prediction.get('health_score', 0)
        deg_rate = prediction.get('degradation_rate', 0)
        current = prediction.get('current_value')
        forecast_350 = prediction.get('alarm_crossings', {}).get(350, {})
        forecast_250 = prediction.get('alarm_crossings', {}).get(250, {})
        logger.info("车辆 %s 拟合汇总: R²=%.4f, health=%d, deg_rate=%.4f kΩ/天, current=%s",
                    vid, r_squared, health, deg_rate,
                    f"{current:.2f}" if current is not None else "N/A")
        logger.info("车辆 %s 报警预测: 350→%s, 250→%s",
                    vid,
                    f"{forecast_350.get('days'):.1f}天" if forecast_350.get('days') else "不触达",
                    f"{forecast_250.get('days'):.1f}天" if forecast_250.get('days') else "不触达")

        result.append({
            'vehicle_id': vid,
            'current': current,
            'degradation_rate': deg_rate,
            'forecast_350': {
                'days': forecast_350.get('days'),
                'date': forecast_350.get('date'),
            },
            'forecast_250': {
                'days': forecast_250.get('days'),
                'date': forecast_250.get('date'),
            },
            'health_score': health,
            'color': color,
        })
        n_success += 1
        logger.info("车辆 %s 处理完成 (累计 成功=%d / 失败=%d)",
                    vid, n_success, n_fail)

    # 报警线(统一 2 条, 不重复)
    for alarm, color, name in [
        (primary_alarm, _PRIMARY_ALARM_COLOR, f'主报警 {primary_alarm} kΩ'),
        (secondary_alarm, _SECONDARY_ALARM_COLOR, f'次报警 {secondary_alarm} kΩ'),
    ]:
        fig.add_hline(
            y=alarm, line_dash='dash', line_color=color, line_width=1.5,
            annotation_text=name, annotation_position='top right',
            annotation_font=dict(color=color, size=11),
        )

    fig.update_layout(
        title=dict(text='多车绝缘阻值趋势对比', font=dict(color='#00D4FF', size=18)),
        paper_bgcolor=_BG_COLOR, plot_bgcolor=_BG_COLOR,
        font=dict(color=_TEXT_COLOR),
        xaxis=dict(gridcolor=_GRID_COLOR, zeroline=False),
        yaxis=dict(title='绝缘阻值 (kΩ)', gridcolor=_GRID_COLOR),
        showlegend=True, height=600,
        margin=dict(l=60, r=30, t=60, b=50),
        legend=dict(orientation='h', yanchor='bottom', y=1.02,
                    xanchor='right', x=1),
    )

    logger.info("=== create_vehicle_comparison 结束: 车辆数=%d, 成功=%d, 失败=%d ===",
                len(vehicle_ids), n_success, n_fail)
    return fig, result


def generate_comparison_table(result: List[Dict]) -> pd.DataFrame:
    """结果转对比表格(按 health_score 升序, 最差在上)。

    列: vehicle_id / current / degradation_rate / forecast_350 / health_score
    """
    logger.info("generate_comparison_table: 输入 %d 行", len(result))
    if not result:
        df = pd.DataFrame(columns=['vehicle_id', 'current', 'degradation_rate',
                                   'forecast_350', 'health_score'])
        return df

    rows = []
    for r in result:
        f350 = r.get('forecast_350', {})
        days_350 = f350.get('days')
        rows.append({
            'vehicle_id': r['vehicle_id'],
            'current': round(r['current'], 1) if r.get('current') is not None else None,
            'degradation_rate': round(r['degradation_rate'], 4),
            'forecast_350': (f"{days_350:.1f}天" if days_350 is not None else "不触达"),
            'health_score': r['health_score'],
        })
    df = pd.DataFrame(rows).sort_values('health_score', ascending=True).reset_index(drop=True)
    logger.info("对比表格生成: %d 行, 按健康度升序(最差在上)", len(df))
    return df


# ---------- 单元测试 ----------

if __name__ == '__main__':
    import logging as _lg
    import sys as _sys
    import types as _types

    _lg.basicConfig(level=_lg.INFO,
                    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')

    rng = np.random.default_rng(42)

    def _make_vehicle_df(n=100, base=500, slope=-1.0, noise=10, start='2026-01-01'):
        """生成模拟车辆绝缘数据(线性衰减, 10分钟一个点)。"""
        ts = [pd.Timestamp(start) + pd.Timedelta(minutes=10 * i) for i in range(n)]
        days = np.array([i * 10 / 1440 for i in range(n)])
        vals = base + slope * days + rng.normal(0, noise, n)
        vals = np.maximum(vals, 0)
        return pd.DataFrame({
            'Timestamp': ts,
            'FC_VehicleIsolationR': vals,
            'FC_MainSts': 4,
        })

    # mock streamlit 模块, 注入支持 .get() 的 session_state
    class _MockSS:
        def __init__(self, data):
            self._d = data
        def get(self, key, default=None):
            return self._d.get(key, default)
        def __getattr__(self, name):
            if name.startswith('_'):
                raise AttributeError(name)
            return self._d.get(name)
        def __contains__(self, key):
            return key in self._d
        def __getitem__(self, key):
            return self._d[key]
    _st_mock = _types.ModuleType('streamlit')
    _st_mock.session_state = _MockSS({
        'data': {
            '212': _make_vehicle_df(n=100, base=500, slope=-1.0, noise=5),
            '345': _make_vehicle_df(n=100, base=450, slope=-0.5, noise=5),
        }
    })
    _sys.modules['streamlit'] = _st_mock

    print("\n===== 测试1: 双车对比(正常场景) =====")
    fig, result = create_vehicle_comparison(
        ['212', '345'],
        pd.Timestamp('2026-01-01'), pd.Timestamp('2026-01-31'),
        alarm_values=[350, 250],
    )
    assert len(result) == 2
    assert result[0]['vehicle_id'] in ('212', '345')
    assert result[1]['vehicle_id'] in ('212', '345')
    assert all('health_score' in r for r in result)
    print(f"  result 行数={len(result)}")
    for r in result:
        cur = f"{r['current']:.1f}" if r['current'] is not None else "N/A"
        print(f"  车辆 {r['vehicle_id']}: health={r['health_score']}, current={cur}, deg={r['degradation_rate']:.4f}")
    print(f"  fig trace 数={len(fig.data)}")
    assert len(fig.data) >= 2  # 至少 2 条车散点
    print("  [PASS] 双车对比正常")

    print("\n===== 测试2: 空车列表 =====")
    fig2, res2 = create_vehicle_comparison([], None, None)
    assert len(res2) == 0
    assert len(fig2.data) == 0
    print("  [PASS] 空车列表返回空状态图")

    print("\n===== 测试3: 单车提示(应正常处理) =====")
    fig3, res3 = create_vehicle_comparison(['212'], None, None)
    assert len(res3) == 1
    print("  [PASS] 单车正常处理(带提示日志)")

    print("\n===== 测试4: 车辆无数据(跳过) =====")
    fig4, res4 = create_vehicle_comparison(['212', '999'], None, None)
    assert len(res4) == 1  # 999 无数据跳过
    print("  [PASS] 无数据车辆跳过+warning")

    print("\n===== 测试5: 对比表格生成(按健康度升序) =====")
    # 双车表格
    fig5, res5 = create_vehicle_comparison(
        ['212', '345'], None, None, alarm_values=[350, 250])
    df_table = generate_comparison_table(res5)
    assert 'vehicle_id' in df_table.columns
    assert 'health_score' in df_table.columns
    assert len(df_table) == 2
    # 按健康度升序 → 第一行 health <= 第二行
    assert df_table['health_score'].iloc[0] <= df_table['health_score'].iloc[1]
    print(df_table.to_string(index=False))
    print("  [PASS] 表格生成 + 升序正确")

    print("\n===== 测试6: 空结果表格 =====")
    df_empty = generate_comparison_table([])
    assert len(df_empty) == 0
    print("  [PASS] 空结果表格正确")

    # 清理 mock
    del _sys.modules['streamlit']

    print("\n所有测试通过 [OK]")
