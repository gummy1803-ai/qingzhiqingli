"""统计卡片组件:在图表上方展示数据摘要与各信号快速指标。

- 第一行:4 个核心 st.metric(记录数/时间跨度/信号数/异常告警)
- 第二行:对每个选中信号展示 最大/最小/平均 三个 st.metric
- 叠加自定义 CSS:卡片深色背景 + 科技蓝边框发光 + 悬停增强
"""
from __future__ import annotations

from typing import List

import pandas as pd
import streamlit as st

from utils.helpers import SIGNAL_MAP


def _ts_col(df: pd.DataFrame) -> str | None:
    """兼容查找时间戳列(项目约定大写 Timestamp,兼容用户小写 timestamp)。"""
    for c in ('Timestamp', 'timestamp'):
        if c in df.columns:
            return c
    return None


def _inject_css() -> None:
    """注入卡片发光样式(每次会话仅注入一次,避免重复)。"""
    if st.session_state.get('_stats_css_injected'):
        return
    st.markdown(
        """
        <style>
        /* 统计卡片:深色背景 + 科技蓝边框发光 */
        div[data-testid="stMetric"] {
            background: rgba(0, 212, 255, 0.04);
            border: 1px solid rgba(0, 212, 255, 0.25);
            border-radius: 10px;
            padding: 14px 16px !important;
            box-shadow: 0 0 12px rgba(0, 212, 255, 0.12);
            transition: box-shadow 0.2s ease, border-color 0.2s ease;
        }
        div[data-testid="stMetric"]:hover {
            border-color: rgba(0, 212, 255, 0.55);
            box-shadow: 0 0 18px rgba(0, 212, 255, 0.32);
        }
        /* 标签文字 */
        div[data-testid="stMetric"] label p {
            font-size: 0.82rem !important;
            color: #8FA3B8 !important;
            margin-bottom: 4px !important;
        }
        /* 数值文字 */
        div[data-testid="stMetric"] div[data-testid="stMetricValue"] {
            font-size: 1.35rem !important;
            font-weight: 700 !important;
            color: #E8EDF5 !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.session_state['_stats_css_injected'] = True


def _fmt(v: float) -> str:
    """按数值量级格式化(大值保留1位,小值多保留几位)。"""
    if v is None or pd.isna(v):
        return '—'
    a = abs(v)
    if a >= 1000:
        return f'{v:,.1f}'
    if a >= 10:
        return f'{v:.2f}'
    if a >= 1:
        return f'{v:.3f}'
    return f'{v:.4f}'


def render_stats(df: pd.DataFrame, selected_signals: List[str]) -> None:
    """使用 st.metric 渲染统计卡片。

    Args:
        df: 整车数据(含时间戳列;可选 is_anomaly 列)
        selected_signals: 当前选中的信号列表(为空则跳过第二行快速指标)
    """
    _inject_css()

    # ---------- 空数据兜底 ----------
    if df is None or len(df) == 0:
        st.info('暂无数据,无法生成统计摘要')
        return

    # ---------- 第一行:4 个核心指标 ----------
    col1, col2, col3, col4 = st.columns(4)

    # 卡片1:数据记录数
    with col1:
        st.metric('📝 数据记录', f'{len(df)} 条')

    # 卡片2:时间跨度(秒)
    with col2:
        tc = _ts_col(df)
        span_txt = '— 秒'
        if tc is not None:
            ts = pd.to_datetime(df[tc], errors='coerce').dropna()
            if len(ts) >= 2:
                span = (ts.max() - ts.min()).total_seconds()
                span_txt = f'{span:.0f} 秒'
        st.metric('⏱️ 时间跨度', span_txt)

    # 卡片3:选中信号数
    with col3:
        st.metric('📊 展示信号', f'{len(selected_signals)} 个')

    # 卡片4:异常事件(inverse 配色:数字越大越红/越警示)
    with col4:
        anom_count = int(df['is_anomaly'].sum()) if 'is_anomaly' in df.columns else 0
        st.metric(
            '⚠️ 异常告警',
            f'{anom_count} 次',
            delta=str(anom_count) if anom_count > 0 else None,
            delta_color='inverse',
        )

    # ---------- 第二行:选中信号的快速指标(最大/最小/平均) ----------
    if selected_signals:
        st.markdown("#### 信号快速指标")
        # 每个信号一行3列(最大/最小/平均);信号较多时纵向堆叠
        any_shown = False
        for sig in selected_signals:
            if sig not in df.columns:
                continue
            v = pd.to_numeric(df[sig], errors='coerce').dropna()
            if len(v) == 0:
                continue
            label = SIGNAL_MAP.get(sig, sig)
            mx, mn, av = v.max(), v.min(), v.mean()
            c1, c2, c3 = st.columns(3)
            c1.metric(f'{label} · 最大', _fmt(mx))
            c2.metric(f'{label} · 最小', _fmt(mn))
            c3.metric(f'{label} · 平均', _fmt(av))
            any_shown = True
        if not any_shown:
            st.caption('选中信号在当前数据中无可用数值')
