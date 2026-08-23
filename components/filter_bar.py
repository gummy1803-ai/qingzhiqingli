"""顶部筛选栏组件:车辆选择 / 时间范围 / 信号多选。

三列(实为四列 [1,2,2,3])横向布局,Streamlit 天然响应式:
任意控件变化即触发重跑,无需"查询"按钮。
所有状态通过 st.session_state 持久化,刷新页面不丢失。

窄屏增强: 选项栏外层包了水平滑块, 在任何分辨率下都可通过
  - 鼠标按住空白区域拖动
  - 点两侧 ◀ ▶ 按钮
  - Shift + 滚轮
  - 触屏横向滑动
查看被遮挡的选项内容;不溢出时滑块自动隐藏,不制造视觉干扰。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import streamlit as st

from utils.helpers import SIGNAL_MAP
from components.horizontal_slider import horizontal_slider

# ---------- 常量 ----------
_VEHICLES = ['212', '345']
_DEFAULT_SIGNALS = ['FC_AvgCellVoltage', 'FC_CurrOut']
_MAX_HOURS = 6  # 最大允许时间跨度(防数据量过大)


def render_filter_bar() -> dict:
    """渲染顶部筛选栏,返回当前筛选状态。

    控件 key 均持久化到 st.session_state,刷新页面不丢失。
    任意控件变化即响应(Streamlit 天然响应式,无需查询按钮)。

    Returns:
        dict: {
            'vehicle_id': str,
            'start_time': datetime,
            'end_time': datetime,
            'selected_signals': list[str],
        }
    """
    st.markdown("### 筛选条件")

    now = datetime.now()

    # ============================================================
    # 燃电运行看板选项栏 → 外层包水平滑块
    #   宽屏: 内容不溢出,滑块自动隐藏,完全不影响现有观感
    #   窄屏: 出现 ◀ ▶ 箭头 + 渐变遮罩 + 底部滑块, 可拖/点箭头/滚轮查看被遮挡选项
    # ============================================================
    with horizontal_slider("fc_filter_options"):
        # 四列横向布局:车辆 / 起始时间 / 结束时间 / 信号多选
        col1, col2, col3, col4 = st.columns([1, 2, 2, 3])

        # ---------- 列1:车辆选择 ----------
        with col1:
            vehicle_id = st.selectbox(
                '🚗 车辆',
                _VEHICLES,
                index=0,
                key='vehicle_selector',
            )

        # ---------- 列2:起始时间(日期 + 时间,支持秒) ----------
        with col2:
            start_date = st.date_input(
                '起始日期',
                value=now - timedelta(hours=1),
                key='start_date',
            )
            start_time = st.time_input(
                '起始时间',
                # 取整到分钟再减1小时,转 time 对象
                value=(now.replace(minute=0, second=0) - timedelta(hours=1)).time(),
                key='start_time',
                step=timedelta(minutes=1),
            )

        # ---------- 列3:结束时间(日期 + 时间,支持秒) ----------
        with col3:
            end_date = st.date_input(
                '结束日期',
                value=now,
                key='end_date',
            )
            end_time = st.time_input(
                '结束时间',
                value=now.replace(second=0),
                key='end_time',
                step=timedelta(minutes=1),
            )

        # ---------- 列4:信号多选 ----------
        with col4:
            selected_signals = st.multiselect(
                '📊 选择信号',
                options=list(SIGNAL_MAP.keys()),
                format_func=lambda x: SIGNAL_MAP[x],  # 下拉显示中文名
                default=_DEFAULT_SIGNALS,
                key='signal_selector',
                placeholder='请选择要展示的信号（最多选5个）',
            )

    # ---------- 合并日期与时间 ----------
    start_datetime = datetime.combine(start_date, start_time)
    end_datetime = datetime.combine(end_date, end_time)

    # ---------- 校验 ----------
    valid = True
    if end_datetime <= start_datetime:
        st.error('⚠️ 结束时间必须大于起始时间,请重新选择')
        valid = False

    # 最大跨度限制:超出仅警告,不阻断(用户可坚持查询)
    if (end_datetime - start_datetime) > timedelta(hours=_MAX_HOURS):
        st.warning(
            f'⚠️ 时间跨度超过 {_MAX_HOURS} 小时,数据量可能较大,'
            f'建议缩小范围以提升性能'
        )

    # ---------- session_state 持久化(跨组件状态共享) ----------
    st.session_state['filter_state'] = {
        'vehicle_id': vehicle_id,
        'start_time': start_datetime,
        'end_time': end_datetime,
        'selected_signals': selected_signals,
        'valid': valid,
    }

    return {
        'vehicle_id': vehicle_id,
        'start_time': start_datetime,
        'end_time': end_datetime,
        'selected_signals': selected_signals,
    }
