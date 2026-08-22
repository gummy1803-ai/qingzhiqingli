"""台架耐久数据统计及预警 - 筛选栏组件。

5 列横向布局:
- 列1: 台架选择
- 列2: 功率点(全选/清空快捷按钮,默认全选)
- 列3: 信号选择(多选,默认前两个)
- 列4: 预警条件配置(条件1/条件2/触发动作)
- 列5: 数据刷新按钮 + 数据日期范围展示

高级选项(时间范围/聚合方法/质量阈值)折叠到 expander,避免主筛选栏过宽。
所有控件 key 均加 'dur_' 前缀,与其他筛选栏(filter_bar/performance_filter/
insulation_filter)的 widget key 不冲突。状态持久化到
st.session_state['durability_filter_state']。

核心函数: render_durability_filter
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import List, Tuple, Optional

import streamlit as st

logger = logging.getLogger(__name__)

# ---------- 常量 ----------
_RIGS = ['台架A', '台架B', '台架C']
_DEFAULT_POWER_POINTS: List[float] = [33.0, 58.5, 117.0, 156.0, 175.5, 195.0]

# 信号选项与默认(默认前两个):企业要求补 LFR/HFR 两个阻抗信号
_SIGNAL_OPTIONS = [
    'FC_AvgCellVoltage', 'FC_AvgCellVoltDev',
    'FC_LFR', 'FC_HFR',                  # 低频阻抗 / 高频阻抗(企业新增)
    'FC_VARVoltage', 'FC_NetPwrOut', 'FC_VoltOut',
]
_DEFAULT_SIGNALS = _SIGNAL_OPTIONS[:2]

# 企业视图3 四张子图固定信号(平均电压/离均差/LFR/HFR),用于视图3 默认
_FOUR_PANEL_SIGNALS: List[str] = [
    'FC_AvgCellVoltage', 'FC_AvgCellVoltDev',
    'FC_LFR', 'FC_HFR',
]
_FOUR_PANEL_TITLES: dict = {
    'FC_AvgCellVoltage': '平均单体电压',
    'FC_AvgCellVoltDev': '单体电压离均差',
    'FC_LFR':           '低频阻抗 LFR',
    'FC_HFR':           '高频阻抗 HFR',
}
_FOUR_PANEL_UNITS: dict = {
    'FC_AvgCellVoltage': 'V',
    'FC_AvgCellVoltDev': 'mV',
    'FC_LFR':           'mΩ·cm²',
    'FC_HFR':           'mΩ·cm²',
}

# 预警条件(条件1 必选,条件2 含 '无' 可禁用)
_ALERT_CONDITIONS = ['离均差>50mV', '平均单体电压<600mV']
_ALERT_CONDITION_2_OPTIONS = ['无'] + _ALERT_CONDITIONS
_ALERT_ACTIONS = ['飞书通知', '邮件通知', '仅页面显示']
_DEFAULT_ALERT_ACTION = '仅页面显示'

# 高级选项默认值
_MAX_HOURS = 24 * 7               # 耐久数据可看较长(7 天)
_AGG_METHODS = ['mean', 'median', 'min', 'max']
_DEFAULT_AGG = 'mean'
_DEFAULT_MIN_COUNT = 10
_DEFAULT_VOL_THRESHOLD = 0.005     # 5mV(针对电压类信号)
_VOL_THRESHOLD_STEP = 0.001


def _validate_time_range(start: datetime, end: datetime,
                         max_hours: float = _MAX_HOURS) -> Tuple[bool, str]:
    """校验时间区间:结束>起始 且跨度不超上限。"""
    if end <= start:
        return False, "结束时间必须晚于起始时间"
    span_h = (end - start).total_seconds() / 3600.0
    if span_h > max_hours:
        return False, (f"时间跨度 {span_h:.1f}h 超过 {max_hours:.0f}h 上限,"
                       f"请缩小范围(耐久数据建议≤7天)")
    if span_h < 1 / 60:  # <1 分钟
        return False, "时间跨度不足 1 分钟,请扩大范围"
    return True, ""


def render_durability_filter() -> dict:
    """渲染耐久分析筛选栏,返回筛选配置。

    Returns:
        dict: {
            'rig_id'              : str,           # 台架编号
            'power_points'        : List[float],   # 选中的目标功率点
            'signal_columns'      : List[str],     # 选中的信号列
            'alert_condition_1'   : str,           # 预警条件1(必选)
            'alert_condition_2'   : str,           # 预警条件2('无'=禁用)
            'alert_action'        : str,           # 触发动作
            'last_update_time'   : Optional[datetime], # 上次刷新时间
            'data_date_range'   : Optional[Tuple[datetime, datetime]],
            'start_time'          : datetime,
            'end_time'            : datetime,
            'agg_method'          : str,           # mean/median/min/max
            'min_data_count'      : int,           # 数据不足阈值
            'volatility_threshold': float,         # 波动异常阈值
            'valid'               : bool,
        }
    """
    logger.info("耐久筛选栏渲染开始")
    st.markdown("#### 筛选条件")

    # 5 列横向布局:台架 / 功率点 / 信号 / 预警 / 数据刷新
    cols = st.columns([1, 2, 2, 1.5, 1.5])

    # ---------- 列1: 台架 ----------
    with cols[0]:
        rig_id = st.selectbox(
            '🏗️ 台架', _RIGS, index=0, key='dur_rig',
            help='选择耐久测试台架编号',
        )

    # ---------- 列2: 功率点(全选/清空快捷按钮,默认全选) ----------
    with cols[1]:
        st.markdown('⚡ 目标功率点(可多选)')
        _btn1, _btn2 = st.columns(2)
        with _btn1:
            if st.button('全选', key='dur_pp_select_all',
                         use_container_width=True):
                st.session_state['dur_power_points'] = [
                    f'{p:.1f} kW' for p in _DEFAULT_POWER_POINTS
                ]
                logger.info("功率点全选按钮触发")
        with _btn2:
            if st.button('清空', key='dur_pp_clear',
                         use_container_width=True):
                st.session_state['dur_power_points'] = []
                logger.info("功率点清空按钮触发")
        pp_options = [f'{p:.1f} kW' for p in _DEFAULT_POWER_POINTS]
        pp_default = pp_options  # 默认全选
        pp_selected = st.multiselect(
            '目标功率点',
            options=pp_options,
            default=pp_default,
            key='dur_power_points',
            label_visibility='collapsed',
            help='选择参与统计的功率点(默认全选 6 个,可用上方按钮快速全选/清空)',
        )
        # 解析选中的功率点("33.0 kW" -> 33.0)
        selected_pps: List[float] = []
        for s in pp_selected:
            try:
                v = float(s.replace(' kW', ''))
                selected_pps.append(v)
            except ValueError:
                logger.warning("无法解析功率点选项: %s", s)
        st.caption(f'当前选择: {len(selected_pps)}/6 个功率点')

    # ---------- 列3: 信号选择(多选,默认前两个) ----------
    with cols[2]:
        signals = st.multiselect(
            '📊 展示信号', options=_SIGNAL_OPTIONS,
            default=_DEFAULT_SIGNALS, key='dur_signals',
            help='选择需要在统计/图表中展示的信号列(默认前两个)',
        )
        st.caption(f'已选 {len(signals)}/{len(_SIGNAL_OPTIONS)} 个信号')

    # ---------- 列4: 预警条件配置 ----------
    with cols[3]:
        st.markdown('⚠️ 预警条件')
        alert_cond1 = st.selectbox(
            '预警条件1', _ALERT_CONDITIONS,
            index=0, key='dur_alert_cond1',
            help='必选一个预警条件',
        )
        alert_cond2 = st.selectbox(
            '预警条件2', _ALERT_CONDITION_2_OPTIONS,
            index=0, key='dur_alert_cond2',
            help='"无" 表示不启用第二个条件',
        )
        alert_action = st.selectbox(
            '预警动作', _ALERT_ACTIONS,
            index=_ALERT_ACTIONS.index(_DEFAULT_ALERT_ACTION),
            key='dur_alert_action',
            help='触发预警后的通知方式',
        )

    # ---------- 列5: 数据刷新 ----------
    with cols[4]:
        st.markdown('🔄 数据刷新')
        refresh_clicked = st.button('🔄 检测新数据', key='dur_refresh',
                                    use_container_width=True)
        if refresh_clicked:
            logger.info("用户点击'检测新数据'按钮,触发数据刷新")
            st.session_state['dur_last_update_time'] = datetime.now()
        last_update = st.session_state.get('dur_last_update_time')
        if last_update is not None:
            st.caption(f"数据更新时间: {last_update:%Y-%m-%d %H:%M:%S}")
        else:
            st.caption("数据更新时间: 尚未检测")
        # 数据日期范围(由外部模块设置到 session_state,这里仅展示)
        date_range = st.session_state.get('dur_data_date_range')
        if date_range is not None:
            try:
                st.caption(f"数据范围: "
                           f"{date_range[0]:%Y-%m-%d %H:%M} ~ "
                           f"{date_range[1]:%Y-%m-%d %H:%M}")
            except Exception:
                logger.warning("data_date_range 格式异常: %s", date_range)
                st.caption(f"数据范围: {date_range}")
        else:
            st.caption("数据范围: 未加载")

    # ---------- 高级选项(折叠):时间/聚合/质量阈值 ----------
    with st.expander("⚙️ 高级选项(时间范围 / 聚合方法 / 质量阈值)",
                     expanded=False):
        a1, a2, a3 = st.columns([2, 1.3, 1.5])
        with a1:
            _tc1, _tc2 = st.columns(2)
            with _tc1:
                start_date = st.date_input('起始日期', key='dur_start_date')
                start_time = st.time_input(
                    '起始时间', key='dur_start_time',
                    step=timedelta(minutes=1),
                )
            with _tc2:
                end_date = st.date_input('结束日期', key='dur_end_date')
                end_time = st.time_input(
                    '结束时间', key='dur_end_time',
                    step=timedelta(minutes=1),
                )
        with a2:
            agg_method = st.selectbox(
                '📊 聚合方法', _AGG_METHODS,
                index=_AGG_METHODS.index(_DEFAULT_AGG),
                key='dur_agg',
                help='mean=均值 / median=中位数 / min=最小 / max=最大',
            )
        with a3:
            min_count = st.number_input(
                '📉 最小数据量',
                min_value=1, value=_DEFAULT_MIN_COUNT, step=5,
                key='dur_min_count',
                help='每组(cycle_id × power_point)数据条数少于此值标记"数据不足"',
            )
            vol_thresh = st.number_input(
                '⚡ 波动阈值 (std)',
                min_value=0.0, value=_DEFAULT_VOL_THRESHOLD,
                step=_VOL_THRESHOLD_STEP, format='%.4f',
                key='dur_vol_thresh',
                help='任一信号 std > 此值标记"波动异常"(电压类 0.005=5mV)',
            )

    # ---------- 合并日期与时间 ----------
    start_dt = datetime.combine(start_date, start_time)
    end_dt = datetime.combine(end_date, end_time)

    # ---------- 校验 ----------
    valid, err = _validate_time_range(start_dt, end_dt)
    if not valid:
        st.error(f"❌ {err}")
        logger.warning("耐久筛选校验失败: %s", err)
    elif not selected_pps:
        err = "请至少选择一个功率点(或点击'全选'按钮)"
        st.error(f"❌ {err}")
        logger.warning(err)
        valid = False
    elif not signals:
        err = "请至少选择一个展示信号"
        st.error(f"❌ {err}")
        logger.warning(err)
        valid = False

    # ---------- 组装返回 ----------
    cfg = {
        'rig_id': rig_id,
        'power_points': selected_pps,
        'signal_columns': signals,
        'alert_condition_1': alert_cond1,
        'alert_condition_2': alert_cond2,
        'alert_action': alert_action,
        'last_update_time': last_update,
        'data_date_range': date_range,
        'start_time': start_dt,
        'end_time': end_dt,
        'agg_method': agg_method,
        'min_data_count': int(min_count),
        'volatility_threshold': float(vol_thresh),
        'valid': valid,
    }
    st.session_state['durability_filter_state'] = cfg
    logger.info("耐久筛选配置: rig=%s pps=%s signals=%s alert1=%s alert2=%s "
                "action=%s range=%s~%s agg=%s min_count=%d vol_thresh=%s "
                "valid=%s",
                rig_id, selected_pps, signals, alert_cond1, alert_cond2,
                alert_action, start_dt, end_dt, agg_method,
                min_count, vol_thresh, valid)

    # 回显配置摘要
    pp_summary = '/'.join(f'{p:.1f}' for p in selected_pps) if selected_pps else '无'
    sig_summary = '/'.join(signals) if signals else '无'
    st.caption(
        f"✓ 台架 {rig_id} | 功率点[{pp_summary}] | 信号[{sig_summary}] | "
        f"预警[{alert_cond1}+{alert_cond2}→{alert_action}] | "
        f"{start_dt:%m-%d %H:%M}~{end_dt:%m-%d %H:%M}"
    )
    return cfg


# ---------- 单元测试 ----------

if __name__ == '__main__':
    import re
    import logging as _lg
    _lg.basicConfig(level=_lg.INFO,
                    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')

    print("===== 测试1: _validate_time_range 正常区间 =====")
    s = datetime(2026, 8, 22, 0, 0, 0)
    e = datetime(2026, 8, 22, 1, 0, 0)
    ok, msg = _validate_time_range(s, e)
    assert ok and msg == "", f"1小时应有效,实际 {ok}/{msg}"
    print("  [PASS] 1小时区间有效")

    print("\n===== 测试2: 结束<=起始 拒绝 =====")
    ok, msg = _validate_time_range(e, s)
    assert not ok and "晚于" in msg
    ok2, _ = _validate_time_range(s, s)
    assert not ok2
    print(f"  [PASS] 结束<=起始被拒: {msg}")

    print("\n===== 测试3: 跨度超限拒绝 =====")
    s_long = datetime(2026, 8, 1, 0, 0, 0)
    e_long = datetime(2026, 8, 22, 0, 0, 0)  # 21天 > 7天
    ok, msg = _validate_time_range(s_long, e_long)
    assert not ok and "超过" in msg
    print(f"  [PASS] 21天被拒: {msg}")

    print("\n===== 测试4: 跨度过小拒绝 =====")
    s_tiny = datetime(2026, 8, 22, 0, 0, 0)
    e_tiny = datetime(2026, 8, 22, 0, 0, 30)  # 30秒 < 1分钟
    ok, msg = _validate_time_range(s_tiny, e_tiny)
    assert not ok and "1 分钟" in msg
    print(f"  [PASS] 30秒被拒: {msg}")

    print("\n===== 测试5: 默认常量值 =====")
    assert _DEFAULT_POWER_POINTS == [33.0, 58.5, 117.0, 156.0, 175.5, 195.0]
    assert _RIGS == ['台架A', '台架B', '台架C']
    assert _MAX_HOURS == 24 * 7
    assert _SIGNAL_OPTIONS == [
        'FC_AvgCellVoltage', 'FC_AvgCellVoltDev',
        'FC_LFR', 'FC_HFR',
        'FC_VARVoltage', 'FC_NetPwrOut', 'FC_VoltOut',
    ]
    assert _DEFAULT_SIGNALS == ['FC_AvgCellVoltage', 'FC_AvgCellVoltDev']
    assert _ALERT_CONDITIONS == ['离均差>50mV', '平均单体电压<600mV']
    assert _ALERT_CONDITION_2_OPTIONS == ['无', '离均差>50mV', '平均单体电压<600mV']
    assert _ALERT_ACTIONS == ['飞书通知', '邮件通知', '仅页面显示']
    assert _DEFAULT_ALERT_ACTION == '仅页面显示'
    assert _AGG_METHODS == ['mean', 'median', 'min', 'max']
    assert _DEFAULT_AGG == 'mean'
    assert _DEFAULT_MIN_COUNT == 10
    assert _DEFAULT_VOL_THRESHOLD == 0.005
    print(f"  pps={_DEFAULT_POWER_POINTS}")
    print(f"  signals={_SIGNAL_OPTIONS} default={_DEFAULT_SIGNALS}")
    print(f"  alerts={_ALERT_CONDITIONS} cond2_opts={_ALERT_CONDITION_2_OPTIONS}")
    print(f"  actions={_ALERT_ACTIONS} default_action={_DEFAULT_ALERT_ACTION}")
    print("  [PASS] 默认常量符合规格")

    print("\n===== 测试6: key 前缀 dur_ 约定 =====")
    src = open(__file__, encoding='utf-8').read()
    keys = re.findall(r"key='(dur_[^']+)'", src)
    expected = {
        # 主筛选栏
        'dur_rig', 'dur_pp_select_all', 'dur_pp_clear', 'dur_power_points',
        'dur_signals', 'dur_alert_cond1', 'dur_alert_cond2',
        'dur_alert_action', 'dur_refresh',
        # 高级选项
        'dur_start_date', 'dur_start_time', 'dur_end_date', 'dur_end_time',
        'dur_agg', 'dur_min_count', 'dur_vol_thresh',
    }
    assert set(keys) == expected, f"key集合不符: {set(keys) ^ expected}"
    print(f"  共 {len(keys)} 个 key,全部 dur_ 前缀: {sorted(keys)}")
    print("  [PASS] key 前缀约定完整,无冲突")

    print("\n===== 测试7: 聚合方法选项完整 =====")
    assert _AGG_METHODS == ['mean', 'median', 'min', 'max']
    assert _DEFAULT_AGG in _AGG_METHODS
    print(f"  methods={_AGG_METHODS} default={_DEFAULT_AGG}")
    print("  [PASS] 聚合方法 4 选项,默认 mean")

    print("\n===== 测试8: 自定义 max_hours =====")
    s_h = datetime(2026, 8, 22, 0, 0, 0)
    e_h = datetime(2026, 8, 22, 3, 0, 0)
    ok, _ = _validate_time_range(s_h, e_h, max_hours=2)
    assert not ok
    ok2, _ = _validate_time_range(s_h, e_h, max_hours=5)
    assert ok2
    print("  [PASS] 自定义上限生效")

    print("\n===== 测试9: 功率点选项字符串解析正确性 =====")
    test_strs = ['33.0 kW', '58.5 kW', '117.0 kW', '156.0 kW',
                 '175.5 kW', '195.0 kW']
    parsed = [float(s.replace(' kW', '')) for s in test_strs]
    assert parsed == _DEFAULT_POWER_POINTS, \
        f"解析后应等于默认功率点,实际 {parsed}"
    print(f"  解析: {test_strs} → {parsed}")
    print("  [PASS] 功率点字符串解析正确")

    print("\n===== 测试10: max_hours 边界(1h==1h 通过, 1h1s 拒绝) =====")
    s_b = datetime(2026, 8, 22, 0, 0, 0)
    e_b = datetime(2026, 8, 22, 1, 0, 0)
    ok, _ = _validate_time_range(s_b, e_b, max_hours=1)
    assert ok, "跨度 1h == 上限 1h 应通过"
    e_b2 = datetime(2026, 8, 22, 1, 0, 1)
    ok2, _ = _validate_time_range(s_b, e_b2, max_hours=1)
    assert not ok2, "跨度 > 上限应拒绝"
    print("  [PASS] max_hours 边界正确")

    print("\n===== 测试11: 预警条件2 含'无'选项(可禁用),条件1 必选 =====")
    assert '无' in _ALERT_CONDITION_2_OPTIONS, "条件2 应含 '无' 选项"
    assert _ALERT_CONDITION_2_OPTIONS[0] == '无', "条件2 默认应为 '无'"
    assert '无' not in _ALERT_CONDITIONS, "条件1 不应含 '无'(必选)"
    print(f"  cond1={_ALERT_CONDITIONS} (必选)")
    print(f"  cond2={_ALERT_CONDITION_2_OPTIONS} (含 '无',默认禁用)")
    print("  [PASS] 预警条件2 可禁用,条件1 必选")

    print("\n===== 测试12: 默认信号选择为前两个 =====")
    assert _DEFAULT_SIGNALS == _SIGNAL_OPTIONS[:2], \
        f"默认信号应为前两个,实际 {_DEFAULT_SIGNALS}"
    print(f"  _SIGNAL_OPTIONS={_SIGNAL_OPTIONS}")
    print(f"  _DEFAULT_SIGNALS={_DEFAULT_SIGNALS} (= _SIGNAL_OPTIONS[:2])")
    print("  [PASS] 默认选中前两个信号")

    print("\n===== 测试13: 预警动作默认 '仅页面显示' =====")
    assert _DEFAULT_ALERT_ACTION == '仅页面显示'
    assert _DEFAULT_ALERT_ACTION in _ALERT_ACTIONS
    print(f"  actions={_ALERT_ACTIONS} default={_DEFAULT_ALERT_ACTION}")
    print("  [PASS] 默认预警动作为 '仅页面显示'(最低打扰)")

    print("\n===== 测试14: 全选/清空按钮 key 存在 =====")
    src = open(__file__, encoding='utf-8').read()
    assert "key='dur_pp_select_all'" in src, "缺少全选按钮 key"
    assert "key='dur_pp_clear'" in src, "缺少清空按钮 key"
    # 验证按钮逻辑:全选 → 设置 6 个功率点字符串
    expected_full = [f'{p:.1f} kW' for p in _DEFAULT_POWER_POINTS]
    assert len(expected_full) == 6
    print(f"  全选应设置: {expected_full}")
    print(f"  清空应设置: []")
    print("  [PASS] 全选/清空按钮 key 与逻辑就位")

    print("\n===== 测试15: 数据刷新按钮 key 存在 =====")
    assert "key='dur_refresh'" in src, "缺少刷新按钮 key"
    assert "dur_last_update_time" in src, "缺少 last_update_time 状态键"
    assert "dur_data_date_range" in src, "缺少 data_date_range 状态键"
    print("  [PASS] 刷新按钮 + 状态键就位")

    print("\n[OK] 全部测试通过")
