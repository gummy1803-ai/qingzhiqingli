"""燃电性能统计及预测 - 筛选栏组件。

与 components/filter_bar.py 风格一致,但针对性能统计场景增加:
- 批量电流目标点配置(多行文本,每行"目标值±容差")
- 最短稳态持续时长(min_duration)
供 performance.steady_state_selector + segment_aggregator 流水线使用。

核心函数: render_performance_filter
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import List, Dict

import streamlit as st

logger = logging.getLogger(__name__)

# ---------- 常量(与 filter_bar.py 保持一致) ----------
_VEHICLES = ['212', '345']
_MAX_HOURS = 6  # 最大允许时间跨度(防数据量过大)
_DEFAULT_CURRENT_POINTS = '95±5\n100±5\n150±10'  # 燃电典型工况点
_DEFAULT_MIN_DURATION = 60  # 默认最短稳态持续 60s(mock 数据波动频繁,180s 过严)
_DEFAULT_WARMUP_SECONDS = 180  # 企业默认: 稳态段丢弃前 180s 过渡热机期

# Y 轴可选信号 + 对应显示名 + 单位 (与企业 9 字段 SIGNAL_MAP/ai_assistant 口径严格对齐)
_Y_SIGNAL_OPTIONS: list[dict] = [
    {'signal': 'FC_AvgCellVoltage',  'label': '平均单体电压',   'unit': 'mV',   'col_suffix': '_mean'},
    {'signal': 'FC_AvgCellVoltDev',  'label': '单体电压离均差', 'unit': 'mV',   'col_suffix': '_mean'},
    {'signal': 'FC_VARVoltage',      'label': '单体电压方差',   'unit': 'mV²',  'col_suffix': '_mean'},
    {'signal': 'FC_NetPwrOut',       'label': '系统净功率输出', 'unit': 'kW',   'col_suffix': '_mean'},
    {'signal': 'FC_VoltOut',         'label': '电堆输出电压',   'unit': 'V',    'col_suffix': '_mean'},
    {'signal': 'FC_MinCellVoltage',  'label': '最小单体电压',   'unit': 'mV',   'col_suffix': '_mean'},
]
_DEFAULT_Y_SIGNAL = _Y_SIGNAL_OPTIONS[0]['signal']
# X 轴模式
_X_MODES = [
    ('⏱ 累计运行时间 (h)', 'run_time'),
    ('📅 实际日期',         'datetime'),
]
_DEFAULT_X_MODE = 'run_time'
# 多项式阶数(趋势线阶数)
_DEGREE_OPTIONS = [1, 2, 3]
_DEFAULT_DEGREE = 2  # 默认二次

# 电流点行解析正则:支持 95±5 / 95+/-5 / 95 ±5 / 95 +-5
# (目标值) (分隔符 ± 或 +/-) (容差)
_LINE_RE = re.compile(r'(\d+(?:\.\d+)?)\s*(?:±|\+/-)\s*(\d+(?:\.\d+)?)')


def _parse_current_points(text: str) -> List[Dict]:
    """解析多行电流点文本为结构化列表。

    每行格式: "目标值±容差" (容差即波动范围),如:
        95±5
        100.5+/-2
        150 ± 10
    无法解析的行跳过并 WARNING。

    Returns:
        [{'target': 95.0, 'tolerance': 5.0}, ...]
    """
    points: List[Dict] = []
    if not text or not text.strip():
        return points
    for i, raw in enumerate(text.strip().splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        m = _LINE_RE.search(line)
        if not m:
            logger.warning("电流点第%d行无法解析(期望'目标±容差'): '%s',跳过", i, line)
            continue
        target = float(m.group(1))
        tol = float(m.group(2))
        points.append({'target': target, 'tolerance': tol})
        logger.info("电流点第%d行: target=%.2f tolerance=%.2f", i, target, tol)
    return points


def render_performance_filter() -> dict:
    """渲染燃电性能统计筛选栏,返回筛选配置。

    控件 key 均持久化到 st.session_state,刷新页面不丢失。
    任意控件变化即响应(Streamlit 天然响应式)。

    Returns:
        dict: {
            'vehicle_id': str,
            'start_time': datetime,
            'end_time': datetime,
            'current_points': list[dict],  # [{'target':float,'tolerance':float}, ...]
            'min_duration': int,           # 最短稳态持续秒数
            'warmup_seconds': int,         # 稳态段丢弃前 N 秒(企业 180s)
            'y_signal': str,               # Y 轴信号列(聚合后自动补 _mean)
            'y_label': str,                # Y 轴显示名
            'y_unit': str,                 # Y 轴单位
            'x_mode': str,                 # 'run_time' / 'datetime'
            'poly_degree': int,            # 趋势线多项式阶数 1/2/3
            'valid': bool,
        }
    """
    st.markdown("### 性能统计筛选条件")

    # 六列横向布局:车辆 / 起始时间 / 结束时间 / 电流目标点+持续时长 / 图表参数(X/Y/阶数) / 高级(warmup)
    col1, col2, col3, col4, col5, col6 = st.columns([1, 2, 2, 3, 2, 1.3])

    now = datetime.now()

    # ---------- 列1:车辆选择 ----------
    with col1:
        vehicle_id = st.selectbox(
            '🚗 车辆',
            _VEHICLES,
            index=0,
            key='perf_vehicle_selector',
        )

    # ---------- 列2:起始时间(日期 + 时间,支持秒) ----------
    with col2:
        start_date = st.date_input(
            '起始日期',
            value=now - timedelta(hours=1),
            key='perf_start_date',
        )
        start_time = st.time_input(
            '起始时间',
            value=(now.replace(minute=0, second=0) - timedelta(hours=1)).time(),
            key='perf_start_time',
            step=timedelta(minutes=1),
        )

    # ---------- 列3:结束时间(日期 + 时间,支持秒) ----------
    with col3:
        end_date = st.date_input(
            '结束日期',
            value=now,
            key='perf_end_date',
        )
        end_time = st.time_input(
            '结束时间',
            value=now.replace(second=0),
            key='perf_end_time',
            step=timedelta(minutes=1),
        )

    # ---------- 列4:电流目标点(核心) + 最短持续时长 ----------
    with col4:
        st.markdown('⚡ 电流目标点(每行: 目标值±容差)')
        points_text = st.text_area(
            '电流目标点',
            value=_DEFAULT_CURRENT_POINTS,
            height=90,
            key='perf_current_points',
            label_visibility='collapsed',
            help='每行一个工况点,格式如 95±5(电流95A允许±5A波动)。'
                 '支持 ± 或 +/- 分隔,可填多行实现批量分析。',
        )
        min_duration = st.number_input(
            '⏱️ 最短稳态持续(秒)',
            min_value=0,
            value=_DEFAULT_MIN_DURATION,
            step=30,
            key='perf_min_duration',
            help='稳态段需持续该秒数才视为有效,默认60s',
        )

    # ---------- 列5: 图表参数(Y轴/X轴/趋势线阶数) ----------
    with col5:
        y_labels_map = {o['signal']: o for o in _Y_SIGNAL_OPTIONS}
        y_signal = st.selectbox(
            '📊 Y 轴信号',
            options=list(y_labels_map.keys()),
            format_func=lambda s: f"{y_labels_map[s]['label']} ({y_labels_map[s]['unit']})",
            index=list(y_labels_map.keys()).index(_DEFAULT_Y_SIGNAL),
            key='perf_y_signal',
            help='性能趋势纵轴(默认平均单体电压)',
        )
        x_mode = st.radio(
            '🧭 X 轴',
            options=[v for _, v in _X_MODES],
            format_func=lambda v: next(disp for disp, val in _X_MODES if val == v),
            horizontal=True,
            index=next(i for i, (_, v) in enumerate(_X_MODES) if v == _DEFAULT_X_MODE),
            key='perf_x_mode',
        )
        poly_degree = st.selectbox(
            '📈 趋势线多项式',
            options=_DEGREE_OPTIONS,
            format_func=lambda d: f"{d}阶 ({'线性' if d==1 else '二次' if d==2 else '三次'})",
            index=_DEGREE_OPTIONS.index(_DEFAULT_DEGREE),
            key='perf_poly_degree',
            help='趋势拟合多项式阶数(企业推荐:2阶=二次抛物线)',
        )

    # ---------- 列6: warmup 过渡段丢弃 ----------
    with col6:
        warmup_seconds = st.number_input(
            '🔥 稳态丢弃前N秒',
            min_value=0,
            max_value=1800,
            value=_DEFAULT_WARMUP_SECONDS,
            step=30,
            key='perf_warmup',
            help='稳态段前 N 秒作为工况切换过渡期(热机),不参与统计。企业默认 180s',
        )
        st.caption(f"当前: 过渡丢弃前 {warmup_seconds}s")

    # ---------- 合并日期与时间 ----------
    start_datetime = datetime.combine(start_date, start_time)
    end_datetime = datetime.combine(end_date, end_time)

    # ---------- 解析电流目标点 ----------
    current_points = _parse_current_points(points_text)

    # ---------- 校验 ----------
    valid = True
    if end_datetime <= start_datetime:
        st.error('⚠️ 结束时间必须大于起始时间,请重新选择')
        valid = False
    if not current_points:
        st.error('⚠️ 请至少输入一个有效电流目标点(格式: 目标值±容差)')
        valid = False
    if (end_datetime - start_datetime) > timedelta(hours=_MAX_HOURS):
        st.warning(
            f'⚠️ 时间跨度超过 {_MAX_HOURS} 小时,数据量可能较大,'
            f'建议缩小范围以提升性能'
        )

    # ---------- 解析结果回显(让用户确认解析是否正确) ----------
    if current_points:
        preview = ' | '.join(
            f"{p['target']:.1f}±{p['tolerance']:.1f}A"
            for p in current_points
        )
        st.caption(f"✓ 已解析 {len(current_points)} 个电流点: {preview}")

    y_meta = y_labels_map.get(y_signal, {'label': y_signal, 'unit': ''})

    logger.info("性能筛选: vehicle=%s range=[%s, %s] points=%d min_dur=%ds warmup=%ds "
                "y=%s x_mode=%s degree=%d valid=%s",
                vehicle_id, start_datetime, end_datetime,
                len(current_points), min_duration, warmup_seconds,
                y_signal, x_mode, poly_degree, valid)

    # ---------- session_state 持久化 ----------
    st.session_state['performance_filter_state'] = {
        'vehicle_id': vehicle_id,
        'start_time': start_datetime,
        'end_time': end_datetime,
        'current_points': current_points,
        'min_duration': int(min_duration),
        'warmup_seconds': int(warmup_seconds),
        'y_signal': y_signal,
        'y_label': y_meta['label'],
        'y_unit': y_meta['unit'],
        'x_mode': x_mode,
        'poly_degree': int(poly_degree),
        'valid': valid,
    }

    return {
        'vehicle_id': vehicle_id,
        'start_time': start_datetime,
        'end_time': end_datetime,
        'current_points': current_points,
        'min_duration': int(min_duration),
        'warmup_seconds': int(warmup_seconds),
        'y_signal': y_signal,
        'y_label': y_meta['label'],
        'y_unit': y_meta['unit'],
        'x_mode': x_mode,
        'poly_degree': int(poly_degree),
        'valid': valid,
    }


# ---------- 单元测试示例(仅测试纯解析函数) ----------

if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    )

    print("===== 测试1: 标准多行解析 =====")
    pts = _parse_current_points("95±5\n100±5\n150±10")
    assert len(pts) == 3
    assert pts[0] == {'target': 95.0, 'tolerance': 5.0}
    assert pts[2] == {'target': 150.0, 'tolerance': 10.0}
    print(f"  解析 {len(pts)} 个点: {pts}")
    print("  [PASS] 标准格式正确")

    print("\n===== 测试2: 多种分隔符(± / +/-)和小数 =====")
    pts2 = _parse_current_points("95.5±2.5\n100+/-5\n200.0 ± 10")
    assert len(pts2) == 3
    assert pts2[0] == {'target': 95.5, 'tolerance': 2.5}
    assert pts2[1] == {'target': 100.0, 'tolerance': 5.0}
    assert pts2[2] == {'target': 200.0, 'tolerance': 10.0}
    print(f"  解析 {len(pts2)} 个点: {pts2}")
    print("  [PASS] 多分隔符+小数正确")

    print("\n===== 测试3: 容错(空行/无效行跳过) =====")
    pts3 = _parse_current_points("95±5\n\n无效行\n100±5\n  \n150±10")
    assert len(pts3) == 3, f"应跳过无效行得3个点,实际{len(pts3)}"
    print(f"  解析 {len(pts3)} 个点(跳过空行和无效行)")
    print("  [PASS] 容错正确")

    print("\n===== 测试4: 空输入 =====")
    pts4 = _parse_current_points("")
    assert len(pts4) == 0
    pts4b = _parse_current_points("   \n  \n")
    assert len(pts4b) == 0
    print("  [PASS] 空输入返回空列表")

    print("\n===== 测试5: 默认电流点 =====")
    pts5 = _parse_current_points(_DEFAULT_CURRENT_POINTS)
    assert len(pts5) == 3
    targets = [p['target'] for p in pts5]
    assert targets == [95.0, 100.0, 150.0]
    print(f"  默认工况点: {pts5}")
    print("  [PASS] 默认电流点正确")

    print("\n[OK] 全部测试通过")
