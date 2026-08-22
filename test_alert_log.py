"""独立测试脚本：预警历史记录面板效果预览。

构造模拟预警事件数据，调用 render_alert_log 渲染查看效果。
运行: streamlit run test_alert_log.py --server.port 8503
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

# 项目根加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd
import streamlit as st

from components.durability_alert_log import (
    render_alert_log,
    _make_event_id,
    _STATUS_PENDING,
    _STATUS_CONFIRMED,
    _STATUS_IGNORED,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)

# ============================================================
# ✅ 全局 DB 降级机制: 启动就初始化, render_alert_log 内 DB 操作天然带保护
# ============================================================
from durability.database import (
    init_db as _db_init,
    render_streamlit_db_status,
    print_console_db_status,
)
_db_init()
print_console_db_status("test_alert_log 启动 · DB 初始化状态")

# 应用深色主题
try:
    from components.theme import apply_custom_css
    apply_custom_css()
except Exception:
    pass


def make_mock_events(n: int = 20) -> list[dict]:
    """构造模拟预警事件数据。

    - 5个循环 × 6个功率点的一部分组合
    - 两种条件: 离均差>50mV / 平均单体电压<600mV
    - 部分已推送成功，部分失败（测试模式、webhook未配置等）
    """
    rng = np.random.default_rng(42)
    powers = [33.0, 58.5, 117.0, 156.0, 175.5, 195.0]
    conditions = ['离均差>50mV', '平均单体电压<600mV']

    events = []
    base_ts = datetime(2026, 8, 22, 8, 0, 0)

    for i in range(n):
        cycle = int(rng.integers(0, 6))
        power = float(powers[int(rng.integers(0, len(powers)))])
        cond = conditions[int(rng.integers(0, 2))]

        if cond == '离均差>50mV':
            # 50mV~75mV 触发
            value = float(rng.uniform(51.0, 75.0))
            threshold = 50.0
            signal = 'FC_AvgCellVoltDev'
            unit = 'mV'
            op = '>'
        else:
            # 400mV~599mV 触发（注意可能是V单位）
            use_mV = rng.random() > 0.5
            if use_mV:
                value = float(rng.uniform(400.0, 599.0))
                unit = 'mV'
            else:
                value = float(rng.uniform(0.4, 0.599))
                unit = 'V'
            threshold = 600.0
            signal = 'FC_AvgCellVoltage'
            op = '<'

        # 推送状态: 60%成功, 30%测试模式未发, 10%失败
        r = rng.random()
        if r < 0.6:
            sent = True
            send_error = ''
        elif r < 0.9:
            sent = False
            send_error = '测试模式(未发送)'
        else:
            sent = False
            send_error = 'webhook_url 未配置'

        events.append({
            'timestamp': base_ts + timedelta(minutes=int(i * 17)),
            'cycle_id': cycle,
            'power_point': power,
            'condition': cond,
            'value': value,
            'threshold': threshold,
            'signal': signal,
            'unit': unit,
            'operator': op,
            'label': '离均差' if cond.startswith('离均差') else '平均单体电压',
            'data_count': int(rng.integers(15, 120)),
            'quality': str(rng.choice(['正常', '波动异常', '数据不足'])),
            'message': f"{cond}: {value}{unit} {op} {threshold}mV",
            'sent': sent,
            'send_error': send_error,
        })

    return events


def init_session_state(events: list[dict]) -> None:
    """初始化 session_state: 将部分事件标记为已确认/已忽略，模拟真实场景。"""
    if 'dur_alert_status_initialized' not in st.session_state:
        rng = np.random.default_rng(123)
        status_map: dict[str, str] = {}
        for i, e in enumerate(events):
            r = rng.random()
            eid = _make_event_id(e)
            if r < 0.25:
                status_map[eid] = _STATUS_CONFIRMED
            elif r < 0.35:
                status_map[eid] = _STATUS_IGNORED
            # 其余默认 pending(不需要手动设置)
        st.session_state['dur_alert_status'] = status_map
        st.session_state['dur_alert_status_initialized'] = True


def confirm_callback(event: dict) -> None:
    """确认回调：打印日志 + toast 通知。"""
    cycle = event.get('cycle_id', '-')
    power = event.get('power_point', 0)
    cond = event.get('condition', '-')
    msg = f"已确认预警: 循环{cycle} | {power:.1f}kW | {cond}"
    logging.getLogger(__name__).info(msg)
    st.toast(f"✅ {msg}", icon='🎉')


def main() -> None:
    st.set_page_config(
        page_title="预警历史面板 - 效果预览",
        page_icon="📋",
        layout="wide",
    )

    try:
        from components.theme import apply_custom_css
        apply_custom_css()
    except Exception:
        pass

    st.title("📋 预警历史记录面板 - 效果预览")
    st.caption("测试 components/durability_alert_log.py 渲染效果（20条模拟事件）")

    # ✅ 侧边栏底部: DB 状态卡片 + 降级醒目警告
    with st.sidebar:
        render_streamlit_db_status(st.sidebar)

    # 构造模拟数据
    events = make_mock_events(20)

    # 初始化状态（部分已确认/已忽略）
    init_session_state(events)

    # 信息卡片：说明
    with st.expander("ℹ 关于本测试页", expanded=True):
        st.markdown("""
        - **20条模拟预警事件**：循环 0-5 × 6 个功率点的随机组合
        - **两种预警条件**：`离均差>50mV`（51~75mV）/ `平均单体电压<600mV`（400~599mV 或 0.4~0.599V）
        - **状态分布**：约 65% 待确认 / 25% 已确认 / 10% 已忽略（基于 session_state 初始化）
        - **推送状态**：60% 已推送成功 / 30% 测试模式未发 / 10% webhook未配置失败
        - **操作**：点击 ⚠️待确认 事件下方的 **✅确认** 或 **⏭️忽略** 按钮可切换状态
        """)

    # 渲染预警历史面板
    render_alert_log(events, on_confirm=confirm_callback)

    # 底部：查看原始事件列表
    with st.expander("🔧 调试：查看原始事件字典结构", expanded=False):
        st.json(events[:3])


if __name__ == '__main__':
    main()
