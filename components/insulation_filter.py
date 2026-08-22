"""绝缘阻值统计及预测 - 筛选栏组件。

5 列横向布局:车辆 / 时间范围 / 聚合间隔 / 报警阈值 / 预测参数。
所有控件 key 均加 'ins_' 前缀,与其他筛选栏(filter_bar/performance_filter)
的 widget key 不冲突。状态持久化到 st.session_state['insulation_filter_state']。

核心函数: render_insulation_filter
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Tuple

import streamlit as st

logger = logging.getLogger(__name__)

# ---------- 常量 ----------
_VEHICLES = ['212', '345']
_MAX_HOURS = 24 * 7  # 绝缘数据可看较长(7 天),不同于性能分析的 6h
_INTERVAL_OPTIONS = [5, 10, 15, 30]   # 聚合间隔(分钟)
_DEFAULT_INTERVAL = 10                 # 默认 10 分钟
_DEFAULT_PRIMARY = 350                 # 主报警线 kΩ
_DEFAULT_SECONDARY = 250               # 次报警线 kΩ
_DEFAULT_FORECAST_DAYS = 30           # 默认预测 30 天
_DEFAULT_DEGREE = 1                   # 默认线性拟合


def _validate_time_range(start: datetime, end: datetime,
                         max_hours: float = _MAX_HOURS) -> Tuple[bool, str]:
    """校验时间区间:结束>起始 且跨度不超上限。"""
    if end <= start:
        return False, "结束时间必须晚于起始时间"
    span_h = (end - start).total_seconds() / 3600.0
    if span_h > max_hours:
        return False, (f"时间跨度 {span_h:.1f}h 超过 {max_hours:.0f}h 上限,"
                       f"请缩小范围(绝缘数据建议≤7天)")
    if span_h < 1 / 60:  # <1 分钟
        return False, "时间跨度不足 1 分钟,请扩大范围"
    return True, ""


def render_insulation_filter() -> dict:
    """渲染绝缘分析筛选栏,返回筛选配置。

    Returns:
        dict: {
            'vehicle_id', 'start_time', 'end_time',
            'interval'(分钟), 'primary_threshold'(kΩ), 'secondary_threshold'(kΩ),
            'forecast_days', 'poly_degree', 'valid': bool,
        }
    """
    logger.info("绝缘筛选栏渲染开始")
    st.markdown("#### 筛选条件")

    # 5 列横向布局:车辆 / 时间 / 聚合间隔 / 报警阈值 / 预测参数
    cols = st.columns([1, 2, 1.3, 1.3, 1.3])

    # ---------- 列1: 车辆 ----------
    with cols[0]:
        vehicle_id = st.selectbox(
            '🚗 车辆', _VEHICLES, index=0, key='ins_vehicle',
            help='选择要分析的车辆编号',
        )

    # ---------- 列2: 时间范围(精确到秒) ----------
    with cols[1]:
        _tc1, _tc2 = st.columns(2)
        with _tc1:
            start_date = st.date_input('起始日期', key='ins_start_date')
            start_time = st.time_input(
                '起始时间', key='ins_start_time',
                step=timedelta(minutes=1),
            )
        with _tc2:
            end_date = st.date_input('结束日期', key='ins_end_date')
            end_time = st.time_input(
                '结束时间', key='ins_end_time',
                step=timedelta(minutes=1),
            )

    # ---------- 列3: 聚合间隔 ----------
    with cols[2]:
        interval = st.selectbox(
            '📊 聚合间隔', _INTERVAL_OPTIONS,
            index=_INTERVAL_OPTIONS.index(_DEFAULT_INTERVAL),
            key='ins_interval',
            help='每 N 分钟取一个最小绝缘值点(用于降采样+趋势分析)',
        )
        st.caption(f'当前: 每 {interval} 分钟 1 个点')

    # ---------- 列4: 报警阈值(主+次) ----------
    with cols[3]:
        primary_threshold = st.number_input(
            '🔴 主报警线 (kΩ)', value=_DEFAULT_PRIMARY, step=50,
            key='ins_primary',
            help='低于此值视为高危(绝缘严重下降)',
        )
        secondary_threshold = st.number_input(
            '🟡 次报警线 (kΩ)', value=_DEFAULT_SECONDARY, step=50,
            key='ins_secondary',
            help='低于此值视为预警(需关注)',
        )

    # ---------- 列5: 预测参数 ----------
    with cols[4]:
        forecast_days = st.number_input(
            '🔮 预测时长 (天)', value=_DEFAULT_FORECAST_DAYS, step=7,
            key='ins_forecast_days', min_value=1, max_value=365,
            help='向前预测多少天的绝缘趋势',
        )
        poly_degree = st.selectbox(
            '拟合阶数', [1, 2, 3], index=_DEFAULT_DEGREE - 1,
            key='ins_degree',
            help='1=线性(匀速衰减) 2=二次(加速) 3=三次(复杂)',
        )

    # ---------- 合并日期与时间 ----------
    start_dt = datetime.combine(start_date, start_time)
    end_dt = datetime.combine(end_date, end_time)

    # ---------- 校验 ----------
    valid, err = _validate_time_range(start_dt, end_dt)
    if not valid:
        st.error(f"❌ {err}")
        logger.warning("绝缘筛选校验失败: %s", err)
    elif primary_threshold <= secondary_threshold:
        st.warning("⚠ 主报警线应高于次报警线(主=高危阈值,次=预警阈值)")
        logger.warning("报警线异常: 主(%s) <= 次(%s)",
                        primary_threshold, secondary_threshold)

    # 报警线合理性:主>次
    alarm_valid = primary_threshold > secondary_threshold
    final_valid = valid and alarm_valid

    # ---------- 组装返回 ----------
    cfg = {
        'vehicle_id': vehicle_id,
        'start_time': start_dt,
        'end_time': end_dt,
        'interval': int(interval),
        'primary_threshold': float(primary_threshold),
        'secondary_threshold': float(secondary_threshold),
        'forecast_days': int(forecast_days),
        'poly_degree': int(poly_degree),
        'valid': final_valid,
    }
    st.session_state['insulation_filter_state'] = cfg
    logger.info("绝缘筛选配置: vehicle=%s range=%s~%s interval=%dmin "
                "报警=%s/%s 预测=%dd 阶数=%d valid=%s",
                vehicle_id, start_dt, end_dt, interval,
                primary_threshold, secondary_threshold,
                forecast_days, poly_degree, final_valid)

    # 回显配置摘要
    st.caption(
        f"✓ 车辆 {vehicle_id} | {start_dt:%m-%d %H:%M}~{end_dt:%m-%d %H:%M} | "
        f"{interval}min 聚合 | 报警 {primary_threshold}/{secondary_threshold} kΩ | "
        f"预测 {forecast_days}d ({poly_degree}阶)"
    )
    return cfg


# ---------- 单元测试 ----------

if __name__ == '__main__':
    import logging as _lg
    _lg.basicConfig(level=_lg.INFO,
                    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')

    print("===== 测试1: _validate_time_range 正常区间 =====")
    s = datetime(2026, 8, 22, 0, 0, 0)
    e = datetime(2026, 8, 22, 1, 0, 0)
    ok, msg = _validate_time_range(s, e)
    assert ok and msg == "", f"1小时应有效,实际{ok}/{msg}"
    print("  [PASS] 1小时区间有效")

    print("\n===== 测试2: 结束<=起始 拒绝 =====")
    ok, msg = _validate_time_range(e, s)  # end<start
    assert not ok and "晚于" in msg
    ok2, msg2 = _validate_time_range(s, s)  # end==start
    assert not ok2
    print(f"  end<start: {msg}")
    print("  [PASS] 结束<=起始被拒")

    print("\n===== 测试3: 跨度超限拒绝 =====")
    s_long = datetime(2026, 8, 1, 0, 0, 0)
    e_long = datetime(2026, 8, 22, 0, 0, 0)  # 21天 > 7天
    ok, msg = _validate_time_range(s_long, e_long)
    assert not ok and "超过" in msg
    print(f"  21天: {msg}")
    print("  [PASS] 超7天上限被拒")

    print("\n===== 测试4: 跨度过小拒绝 =====")
    s_tiny = datetime(2026, 8, 22, 0, 0, 0)
    e_tiny = datetime(2026, 8, 22, 0, 0, 30)  # 30秒 < 1分钟
    ok, msg = _validate_time_range(s_tiny, e_tiny)
    assert not ok and "1 分钟" in msg
    print(f"  30秒: {msg}")
    print("  [PASS] 不足1分钟被拒")

    print("\n===== 测试5: 自定义 max_hours =====")
    s_h = datetime(2026, 8, 22, 0, 0, 0)
    e_h = datetime(2026, 8, 22, 3, 0, 0)  # 3小时
    ok, _ = _validate_time_range(s_h, e_h, max_hours=2)  # 上限2h
    assert not ok  # 3h > 2h
    ok2, _ = _validate_time_range(s_h, e_h, max_hours=5)  # 上限5h
    assert ok2  # 3h < 5h
    print("  [PASS] 自定义上限生效")

    print("\n===== 测试6: 默认常量值 =====")
    assert _DEFAULT_INTERVAL == 10
    assert _DEFAULT_PRIMARY == 350
    assert _DEFAULT_SECONDARY == 250
    assert _DEFAULT_FORECAST_DAYS == 30
    assert _DEFAULT_DEGREE == 1
    assert _INTERVAL_OPTIONS == [5, 10, 15, 30]
    assert _MAX_HOURS == 24 * 7
    assert 10 in _INTERVAL_OPTIONS  # 默认值必须在选项里
    print(f"  interval={_DEFAULT_INTERVAL}min primary={_DEFAULT_PRIMARY}kΩ "
          f"secondary={_DEFAULT_SECONDARY}kΩ forecast={_DEFAULT_FORECAST_DAYS}d "
          f"degree={_DEFAULT_DEGREE} max_hours={_MAX_HOURS}h")
    print("  [PASS] 默认常量符合规格")

    print("\n===== 测试7: key 前缀 ins_ 约定 =====")
    # 验证模块源码里所有 widget key 都带 ins_ 前缀(避免与其他筛选栏冲突)
    import re
    src = open(__file__, encoding='utf-8').read()
    keys = re.findall(r"key='(ins_[^']+)'", src)
    expected = {'ins_vehicle', 'ins_start_date', 'ins_start_time',
                'ins_end_date', 'ins_end_time', 'ins_interval',
                'ins_primary', 'ins_secondary', 'ins_forecast_days',
                'ins_degree'}
    assert set(keys) == expected, f"key集合不符: {set(keys) ^ expected}"
    print(f"  共 {len(keys)} 个 key,全部 ins_ 前缀: {sorted(keys)}")
    print("  [PASS] key 前缀约定完整,无冲突")

    print("\n[OK] 全部测试通过")
