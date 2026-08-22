"""预警历史记录面板组件。

展示历史预警记录, 支持筛选和确认操作:
1. 顶部统计行(总预警/待确认/已确认/高频功率点)
2. 表格展示(st.dataframe): 时间/循环/功率/条件/数值/阈值/状态/推送
3. 状态管理: 待确认(橙)/已确认(绿)/已忽略(灰)
4. 飞书推送状态: 推送结果/推送时间
5. 确认/忽略操作按钮

状态持久化到 st.session_state['dur_alert_status']。
事件 ID = cycle_id + power_point + condition + timestamp。

核心函数: render_alert_log
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime
from typing import Dict, List, Optional, Callable, Any

import pandas as pd
import streamlit as st

logger = logging.getLogger(__name__)

# ---------- 常量 ----------
_STATUS_PENDING = 'pending'
_STATUS_CONFIRMED = 'confirmed'
_STATUS_IGNORED = 'ignored'
_STATUS_MAP = {
    _STATUS_PENDING: '⚠️ 待确认',
    _STATUS_CONFIRMED: '✅ 已确认',
    _STATUS_IGNORED: '⏭️ 已忽略',
}
_STATUS_COLORS = {
    _STATUS_PENDING: 'orange',
    _STATUS_CONFIRMED: 'green',
    _STATUS_IGNORED: 'gray',
}


# ---------- 内部工具函数 ----------

def _make_event_id(event: Dict) -> str:
    """生成事件唯一 ID: cycle_power_condition_timestamp。"""
    cycle = event.get('cycle_id', -1)
    power = event.get('power_point', 0.0)
    cond = event.get('condition', 'unknown')
    ts = event.get('timestamp')
    ts_str = ts.strftime('%Y%m%d%H%M%S') if isinstance(ts, datetime) else str(ts)
    return f"{cycle}_{power:.1f}_{cond}_{ts_str}"


def _get_event_status(event: Dict, event_id: str) -> str:
    """从 session_state 获取事件状态(默认 pending)。"""
    status_map = st.session_state.get('dur_alert_status', {})
    return status_map.get(event_id, _STATUS_PENDING)


def _set_event_status(event_id: str, status: str) -> None:
    """设置事件状态到 session_state。"""
    if 'dur_alert_status' not in st.session_state:
        st.session_state['dur_alert_status'] = {}
    prev = st.session_state['dur_alert_status'].get(event_id, _STATUS_PENDING)
    st.session_state['dur_alert_status'][event_id] = status
    logger.info("[状态变更] event_id=%s | %s -> %s | session_state 共 %d 条",
                event_id, prev, status,
                len(st.session_state['dur_alert_status']))


def _compute_stats(events: List[Dict],
                   status_map: Dict[str, str]) -> Dict[str, Any]:
    """计算统计数据: 总数/待确认/已确认/高频功率点。

    纯函数, 可单独测试。
    """
    total = len(events)
    pending = sum(1 for e in events
                  if status_map.get(_make_event_id(e), _STATUS_PENDING)
                  == _STATUS_PENDING)
    confirmed = sum(1 for e in events
                    if status_map.get(_make_event_id(e), _STATUS_PENDING)
                    == _STATUS_CONFIRMED)
    ignored = sum(1 for e in events
                  if status_map.get(_make_event_id(e), _STATUS_PENDING)
                  == _STATUS_IGNORED)

    # 高频功率点统计(按触发次数排序)
    power_counter = Counter()
    for e in events:
        pp = e.get('power_point', 0)
        power_counter[f"{pp:.1f}kW"] += 1
    top_power = None
    top_power_count = 0
    if power_counter:
        top_power, top_power_count = power_counter.most_common(1)[0]

    return {
        'total': total,
        'pending': pending,
        'confirmed': confirmed,
        'ignored': ignored,
        'top_power': top_power,
        'top_power_count': top_power_count,
        'power_distribution': dict(power_counter),
    }


def _events_to_dataframe(
    events: List[Dict],
    status_map: Dict[str, str],
) -> pd.DataFrame:
    """将事件列表转为 DataFrame(用于 st.dataframe 展示)。

    纯函数, 可单独测试。
    """
    if not events:
        return pd.DataFrame(columns=[
            '时间', '循环', '功率', '条件', '数值', '阈值',
            '状态', '飞书推送', '推送时间', '推送结果',
        ])

    rows = []
    for e in events:
        eid = _make_event_id(e)
        status = status_map.get(eid, _STATUS_PENDING)
        status_label = _STATUS_MAP.get(status, _STATUS_MAP[_STATUS_PENDING])

        ts = e.get('timestamp')
        ts_str = (ts.strftime('%m-%d %H:%M') if isinstance(ts, datetime)
                  else str(ts))

        sent = e.get('sent', False)
        push_label = '✅ 已推送' if sent else '❌ 未推送'
        push_time = ''
        if sent and e.get('timestamp'):
            push_time = ts.strftime('%H:%M:%S')
        push_result = '成功' if sent else (e.get('send_error', '未发送') or '未发送')

        rows.append({
            '时间': ts_str,
            '循环': e.get('cycle_id', '-'),
            '功率': f"{e.get('power_point', 0):.1f}kW",
            '条件': e.get('condition', '-'),
            '数值': f"{e.get('value', 0):.1f}mV",
            '阈值': f"{e.get('threshold', 0):.0f}mV",
            '状态': status_label,
            '飞书推送': push_label,
            '推送时间': push_time,
            '推送结果': push_result,
        })

    return pd.DataFrame(rows)


def _filter_events(
    events: List[Dict],
    status_map: Dict[str, str],
    status_filter: str = '全部',
    condition_filter: str = '全部',
) -> List[Dict]:
    """按状态/条件筛选事件。

    纯函数, 可单独测试。
    """
    filtered = list(events)

    if status_filter != '全部':
        status_key = {'待确认': _STATUS_PENDING,
                      '已确认': _STATUS_CONFIRMED,
                      '已忽略': _STATUS_IGNORED}.get(status_filter)
        if status_key:
            filtered = [e for e in filtered
                        if status_map.get(_make_event_id(e), _STATUS_PENDING)
                        == status_key]

    if condition_filter != '全部':
        filtered = [e for e in filtered
                    if e.get('condition', '') == condition_filter]

    return filtered


# ---------- 主函数 ----------

def render_alert_log(
    alert_events: List[Dict],
    on_confirm: Optional[Callable] = None,
) -> None:
    """渲染预警历史记录面板。

    Args:
        alert_events: 预警事件列表(check_and_alert 返回值), 每个含
            timestamp, cycle_id, power_point, condition, value,
            threshold, sent, send_error 等
        on_confirm: 确认回调函数, 接收 event dict, 执行后续处理
    """
    logger.info("预警历史面板渲染: events=%d", len(alert_events))

    st.markdown("### 📋 预警历史记录")

    # ---------- 初始化状态 ----------
    if 'dur_alert_status' not in st.session_state:
        st.session_state['dur_alert_status'] = {}
    status_map = st.session_state['dur_alert_status']

    # ---------- 空数据 ----------
    if not alert_events:
        st.info("ℹ 暂无预警记录")
        return

    # ---------- 顶部统计行 ----------
    stats = _compute_stats(alert_events, status_map)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("📊 总预警数", stats['total'])
    c2.metric("⚠️ 待确认", stats['pending'])
    c3.metric("✅ 已确认", stats['confirmed'])
    if stats['top_power']:
        c4.metric("🔥 高频功率点",
                   f"{stats['top_power']}",
                   f"{stats['top_power_count']} 次")
    else:
        c4.metric("🔥 高频功率点", "无")

    # ---------- 筛选栏 ----------
    with st.expander("🔍 筛选", expanded=False):
        fc1, fc2 = st.columns(2)
        with fc1:
            status_filter = st.selectbox(
                '状态筛选',
                ['全部', '待确认', '已确认', '已忽略'],
                key='dur_alert_status_filter',
            )
        with fc2:
            conditions = ['全部'] + list(dict.fromkeys(
                e.get('condition', '') for e in alert_events
                if e.get('condition')
            ))
            condition_filter = st.selectbox(
                '条件筛选', conditions,
                key='dur_alert_condition_filter',
            )

    # ---------- 筛选事件 ----------
    filtered_events = _filter_events(
        alert_events, status_map, status_filter, condition_filter,
    )
    st.caption(f"显示 {len(filtered_events)}/{len(alert_events)} 条记录")

    # ---------- 表格展示 ----------
    df = _events_to_dataframe(filtered_events, status_map)
    if len(df) > 0:
        # 状态列着色(通过 styling)
        def _color_status(val):
            if '待确认' in str(val):
                return 'background-color: rgba(255,165,0,0.15)'
            elif '已确认' in str(val):
                return 'background-color: rgba(0,128,0,0.15)'
            elif '已忽略' in str(val):
                return 'background-color: rgba(128,128,128,0.15)'
            return ''

        styled = df.style.map(_color_status, subset=['状态'])
        st.dataframe(styled, use_container_width=True,
                     hide_index=True, height=min(400, 38 * len(df) + 40))
    else:
        st.info("无匹配记录")

    # ---------- 确认操作区 ----------
    st.markdown("#### 🔧 确认操作")
    pending_events = _filter_events(
        alert_events, status_map, '待确认', condition_filter,
    )

    if not pending_events:
        st.success("✅ 无待确认的预警")
    else:
        st.caption(f"共 {len(pending_events)} 条待确认预警")
        for i, event in enumerate(pending_events):
            eid = _make_event_id(event)
            ts = event.get('timestamp')
            ts_str = (ts.strftime('%m-%d %H:%M:%S')
                      if isinstance(ts, datetime) else str(ts))
            val = event.get('value', 0)
            thresh = event.get('threshold', 0)

            with st.expander(
                f"⚠️ 循环{event.get('cycle_id', '-')} | "
                f"{event.get('power_point', 0):.1f}kW | "
                f"{event.get('condition', '-')} | "
                f"{val:.1f}mV (阈值{thresh:.0f}mV) | {ts_str}",
                expanded=(i == 0),
            ):
                # 事件详情
                dc1, dc2, dc3, dc4 = st.columns(4)
                dc1.metric("循环", event.get('cycle_id', '-'))
                dc2.metric("功率", f"{event.get('power_point', 0):.1f}kW")
                dc3.metric("数值", f"{val:.1f}mV")
                dc4.metric("阈值", f"{thresh:.0f}mV")

                # 飞书推送状态
                sent = event.get('sent', False)
                if sent:
                    st.caption(f"✅ 飞书已推送 | 结果: "
                               f"{event.get('send_error', '成功') or '成功'}")
                else:
                    st.caption(f"❌ 飞书未推送 | "
                               f"{event.get('send_error', '未发送')}")

                # 数据量与质量
                st.caption(f"📊 数据量: {event.get('data_count', '-')} | "
                           f"质量: {event.get('quality', '-')}")

                # 确认/忽略按钮
                bc1, bc2, bc3 = st.columns([1, 1, 2])
                with bc1:
                    if st.button('✅ 确认', key=f'confirm_{eid}',
                                 use_container_width=True, type='primary'):
                        logger.info("[确认操作] 开始 | event_id=%s | 循环%d | %.1fkW | %s | 值=%.2f%s",
                                    eid, event.get('cycle_id', -1),
                                    event.get('power_point', 0.0),
                                    event.get('condition', '-'),
                                    val, event.get('unit', ''))
                        _set_event_status(eid, _STATUS_CONFIRMED)
                        if on_confirm:
                            try:
                                on_confirm(event)
                                logger.info("[确认回调] 成功 | event_id=%s | 回调完成",
                                            eid)
                            except Exception as e:
                                logger.error("[确认回调] 失败 | event_id=%s | 错误=%s",
                                             eid, e, exc_info=True)
                                st.error(f"确认回调异常: {e}")
                        else:
                            logger.info("[确认回调] 未配置 on_confirm, 跳过 | event_id=%s",
                                        eid)
                        logger.info("[确认操作] 完成 | event_id=%s | 新状态=confirmed",
                                    eid)
                        st.success("✅ 已确认")
                        st.rerun()
                with bc2:
                    if st.button('⏭️ 忽略', key=f'ignore_{eid}',
                                 use_container_width=True):
                        logger.info("[忽略操作] 开始 | event_id=%s | 循环%d | %.1fkW | %s",
                                    eid, event.get('cycle_id', -1),
                                    event.get('power_point', 0.0),
                                    event.get('condition', '-'))
                        _set_event_status(eid, _STATUS_IGNORED)
                        logger.info("[忽略操作] 完成 | event_id=%s | 新状态=ignored",
                                    eid)
                        st.info("已忽略")
                        st.rerun()
                with bc3:
                    st.caption(f"事件ID: {eid}")


# ---------- 单元测试 ----------

def _make_test_event(
    cycle: int = 2,
    power: float = 117.0,
    condition: str = '离均差>50mV',
    value: float = 52.0,
    threshold: float = 50.0,
    sent: bool = True,
    send_error: str = '',
) -> Dict:
    """构造测试预警事件。"""
    return {
        'timestamp': datetime(2026, 8, 22, 15, 30, 0),
        'cycle_id': cycle,
        'power_point': power,
        'condition': condition,
        'value': value,
        'threshold': threshold,
        'signal': 'FC_AvgCellVoltDev',
        'unit': 'mV',
        'operator': '>',
        'label': '离均差',
        'data_count': 50,
        'quality': '正常',
        'message': f'离均差 {value}mV > {threshold}mV',
        'sent': sent,
        'send_error': send_error,
    }


if __name__ == '__main__':
    import sys
    import logging as _lg
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    _lg.basicConfig(level=_lg.INFO,
                    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')

    print("===== 测试1: _make_event_id 唯一性 =====")
    e1 = _make_test_event(cycle=2, power=117.0)
    e2 = _make_test_event(cycle=2, power=117.0)
    e3 = _make_test_event(cycle=3, power=117.0)
    eid1 = _make_event_id(e1)
    eid2 = _make_event_id(e2)
    eid3 = _make_event_id(e3)
    assert eid1 == eid2, "相同事件应生成相同ID"
    assert eid1 != eid3, "不同事件应生成不同ID"
    assert '117.0' in eid1 and '离均差' in eid1
    print(f"  e1 ID: {eid1}")
    print(f"  e3 ID: {eid3}")
    print("  [PASS] 事件ID生成正确")

    print("\n===== 测试2: _compute_stats 统计计算 =====")
    events = [
        _make_test_event(cycle=0, power=33.0),
        _make_test_event(cycle=1, power=117.0),
        _make_test_event(cycle=2, power=117.0),
        _make_test_event(cycle=3, power=117.0),
        _make_test_event(cycle=4, power=195.0),
    ]
    # 所有事件默认 pending
    stats = _compute_stats(events, {})
    assert stats['total'] == 5
    assert stats['pending'] == 5
    assert stats['confirmed'] == 0
    assert stats['ignored'] == 0
    assert stats['top_power'] == '117.0kW', f"高频应为117kW, 实际{stats['top_power']}"
    assert stats['top_power_count'] == 3, f"应3次, 实际{stats['top_power_count']}"
    print(f"  总数={stats['total']} 待确认={stats['pending']} "
          f"已确认={stats['confirmed']} 已忽略={stats['ignored']}")
    print(f"  高频: {stats['top_power']} ({stats['top_power_count']}次)")
    print(f"  功率分布: {stats['power_distribution']}")
    print("  [PASS] 统计计算正确")

    print("\n===== 测试3: _compute_stats 含已确认/已忽略 =====")
    # 标记部分事件为 confirmed/ignored
    status_map = {}
    for i, e in enumerate(events):
        eid = _make_event_id(e)
        if i == 0:
            status_map[eid] = _STATUS_CONFIRMED
        elif i == 1:
            status_map[eid] = _STATUS_IGNORED
    stats = _compute_stats(events, status_map)
    assert stats['pending'] == 3
    assert stats['confirmed'] == 1
    assert stats['ignored'] == 1
    print(f"  待确认={stats['pending']} 已确认={stats['confirmed']} "
          f"已忽略={stats['ignored']}")
    print("  [PASS] 状态统计正确")

    print("\n===== 测试4: _events_to_dataframe 列完整 =====")
    df = _events_to_dataframe(events, {})
    expected_cols = ['时间', '循环', '功率', '条件', '数值', '阈值',
                     '状态', '飞书推送', '推送时间', '推送结果']
    assert list(df.columns) == expected_cols, \
        f"列不符: {list(df.columns)}"
    assert len(df) == 5
    # 检查第一行内容
    assert df.iloc[0]['循环'] == 0
    assert '33.0kW' in df.iloc[0]['功率']
    assert '离均差' in df.iloc[0]['条件']
    assert '待确认' in df.iloc[0]['状态']
    print(f"  行数: {len(df)}, 列: {list(df.columns)}")
    print(f"  第1行: {df.iloc[0].to_dict()}")
    print("  [PASS] DataFrame 格式正确")

    print("\n===== 测试5: _events_to_dataframe 空列表 =====")
    df_empty = _events_to_dataframe([], {})
    assert len(df_empty) == 0
    assert len(df_empty.columns) == 10
    print(f"  空表列数: {len(df_empty.columns)}")
    print("  [PASS] 空列表返回带列名的空DataFrame")

    print("\n===== 测试6: _filter_events 状态筛选 =====")
    status_map = {
        _make_event_id(events[0]): _STATUS_CONFIRMED,
        _make_event_id(events[1]): _STATUS_IGNORED,
    }
    # 筛选待确认
    pending = _filter_events(events, status_map, '待确认', '全部')
    assert len(pending) == 3, f"待确认应3条, 实际{len(pending)}"
    # 筛选已确认
    confirmed = _filter_events(events, status_map, '已确认', '全部')
    assert len(confirmed) == 1
    # 筛选已忽略
    ignored = _filter_events(events, status_map, '已忽略', '全部')
    assert len(ignored) == 1
    # 全部
    all_ev = _filter_events(events, status_map, '全部', '全部')
    assert len(all_ev) == 5
    print(f"  待确认={len(pending)} 已确认={len(confirmed)} "
          f"已忽略={len(ignored)} 全部={len(all_ev)}")
    print("  [PASS] 状态筛选正确")

    print("\n===== 测试7: _filter_events 条件筛选 =====")
    events_mixed = [
        _make_test_event(cycle=0, condition='离均差>50mV'),
        _make_test_event(cycle=1, condition='平均单体电压<600mV'),
        _make_test_event(cycle=2, condition='离均差>50mV'),
    ]
    filtered = _filter_events(events_mixed, {}, '全部', '离均差>50mV')
    assert len(filtered) == 2, f"离均差应2条, 实际{len(filtered)}"
    filtered2 = _filter_events(events_mixed, {}, '全部', '平均单体电压<600mV')
    assert len(filtered2) == 1
    print(f"  离均差>50mV: {len(filtered)}条")
    print(f"  平均单体电压<600mV: {len(filtered2)}条")
    print("  [PASS] 条件筛选正确")

    print("\n===== 测试8: 飞书推送状态显示 =====")
    e_sent = _make_test_event(sent=True, send_error='')
    e_not_sent = _make_test_event(sent=False, send_error='测试模式(未发送)')
    df_push = _events_to_dataframe([e_sent, e_not_sent], {})
    assert '已推送' in df_push.iloc[0]['飞书推送']
    assert '未推送' in df_push.iloc[1]['飞书推送']
    assert df_push.iloc[0]['推送结果'] == '成功'
    assert '未发送' in df_push.iloc[1]['推送结果'] or '测试' in df_push.iloc[1]['推送结果']
    print(f"  已推送: {df_push.iloc[0]['飞书推送']} / {df_push.iloc[0]['推送结果']}")
    print(f"  未推送: {df_push.iloc[1]['飞书推送']} / {df_push.iloc[1]['推送结果']}")
    print("  [PASS] 飞书推送状态正确显示")

    print("\n===== 测试9: 状态标签映射 =====")
    assert _STATUS_MAP[_STATUS_PENDING] == '⚠️ 待确认'
    assert _STATUS_MAP[_STATUS_CONFIRMED] == '✅ 已确认'
    assert _STATUS_MAP[_STATUS_IGNORED] == '⏭️ 已忽略'
    assert _STATUS_COLORS[_STATUS_PENDING] == 'orange'
    assert _STATUS_COLORS[_STATUS_CONFIRMED] == 'green'
    assert _STATUS_COLORS[_STATUS_IGNORED] == 'gray'
    print(f"  状态映射: {_STATUS_MAP}")
    print(f"  颜色映射: {_STATUS_COLORS}")
    print("  [PASS] 状态标签/颜色映射正确")

    print("\n===== 测试10: _make_event_id 含不同时间戳 =====")
    e_t1 = _make_test_event()
    e_t1['timestamp'] = datetime(2026, 8, 22, 15, 30, 0)
    e_t2 = _make_test_event()
    e_t2['timestamp'] = datetime(2026, 8, 22, 16, 0, 0)
    assert _make_event_id(e_t1) != _make_event_id(e_t2), "不同时间应不同ID"
    print(f"  15:30 ID: {_make_event_id(e_t1)}")
    print(f"  16:00 ID: {_make_event_id(e_t2)}")
    print("  [PASS] 时间戳区分事件ID")

    print("\n===== 测试11: _compute_stats 空事件 =====")
    stats_empty = _compute_stats([], {})
    assert stats_empty['total'] == 0
    assert stats_empty['pending'] == 0
    assert stats_empty['top_power'] is None
    assert stats_empty['top_power_count'] == 0
    print(f"  空统计: {stats_empty}")
    print("  [PASS] 空事件统计正确")

    print("\n===== 测试12: 高频功率点计算 =====")
    events_power = [
        _make_test_event(power=33.0),
        _make_test_event(power=117.0),
        _make_test_event(power=117.0),
        _make_test_event(power=117.0),
        _make_test_event(power=195.0),
        _make_test_event(power=195.0),
    ]
    stats = _compute_stats(events_power, {})
    assert stats['top_power'] == '117.0kW'
    assert stats['top_power_count'] == 3
    assert stats['power_distribution']['117.0kW'] == 3
    assert stats['power_distribution']['195.0kW'] == 2
    assert stats['power_distribution']['33.0kW'] == 1
    print(f"  分布: {stats['power_distribution']}")
    print(f"  高频: {stats['top_power']} ({stats['top_power_count']}次)")
    print("  [PASS] 高频功率点正确")

    print("\n===== 测试13: render_alert_log 函数签名 =====")
    assert callable(render_alert_log)
    import inspect
    sig = inspect.signature(render_alert_log)
    params = list(sig.parameters.keys())
    assert params == ['alert_events', 'on_confirm']
    print(f"  签名: {sig}")
    print("  [PASS] render_alert_log 就绪(需Streamlit运行时测试)")

    print("\n===== 测试14: _filter_events 组合筛选 =====")
    events_combo = [
        _make_test_event(cycle=0, power=33.0, condition='离均差>50mV'),
        _make_test_event(cycle=1, power=117.0, condition='离均差>50mV'),
        _make_test_event(cycle=2, power=117.0, condition='平均单体电压<600mV'),
    ]
    sm = {_make_event_id(events_combo[0]): _STATUS_CONFIRMED}
    # 筛选: 已确认 + 离均差>50mV -> 只剩 cycle0
    filtered = _filter_events(events_combo, sm, '已确认', '离均差>50mV')
    assert len(filtered) == 1
    assert filtered[0]['cycle_id'] == 0
    print(f"  已确认+离均差: {len(filtered)}条 (cycle={filtered[0]['cycle_id']})")
    print("  [PASS] 组合筛选正确")

    print("\n===== 测试15: 推送时间为空(未推送) =====")
    e_no_push = _make_test_event(sent=False, send_error='webhook_url 未配置')
    df_np = _events_to_dataframe([e_no_push], {})
    assert df_np.iloc[0]['推送时间'] == ''
    assert '未推送' in df_np.iloc[0]['飞书推送']
    assert '未配置' in df_np.iloc[0]['推送结果']
    print(f"  推送时间: '{df_np.iloc[0]['推送时间']}'")
    print(f"  推送结果: {df_np.iloc[0]['推送结果']}")
    print("  [PASS] 未推送时推送时间为空")

    print("\n[OK] 全部测试通过")
