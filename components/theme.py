"""工业科技感暗色主题 CSS 注入。

通过 st.markdown 注入全局样式,覆盖 Streamlit 默认浅色主题:
- 主背景 #0B0E17 / 卡片半透明 + 科技蓝边框 / 标题 #00D4FF
- stMetric 数值高亮 + hover 发光(box-shadow 0 0 20px rgba(0,212,255,0.1))
- 下拉/多选/日期/时间输入框透明背景 + 白色文字
- Plotly 图表背景透明与主题融合
- 深色滚动条 / Tab 高亮
仅用 !important 覆盖视觉层,不触碰交互逻辑,不破坏 Streamlit 原有功能。
"""
from __future__ import annotations

import streamlit as st

_CSS = """
<style>
/* ===== 全局背景 ===== */
.stApp, section[data-testid="stMain"] {
    background-color: #0B0E17 !important;
    color: #E8EDF5;
}
section[data-testid="stSidebar"], div[data-testid="stSidebar"] {
    background-color: #0B0E17 !important;
}

/* ===== 标题颜色 ===== */
h1, h2, h3, h4 {
    color: #00D4FF !important;
}
.stSubheader {
    color: #00D4FF !important;
}
/* 副标题 / 说明文字 */
.stCaption, .stCaption p, .stMarkdown p {
    color: #8892A8 !important;
}

/* ===== stMetric 统计卡片 ===== */
div[data-testid="stMetric"] {
    background-color: rgba(0, 212, 255, 0.04) !important;
    border: 1px solid rgba(0, 212, 255, 0.15) !important;
    border-radius: 12px !important;
    padding: 14px 16px !important;
    transition: box-shadow 0.25s ease, border-color 0.25s ease !important;
}
div[data-testid="stMetric"]:hover {
    border-color: rgba(0, 212, 255, 0.5) !important;
    box-shadow: 0 0 20px rgba(0, 212, 255, 0.1) !important;
}
div[data-testid="stMetric"] label p {
    color: #8892A8 !important;
    font-size: 0.85rem !important;
}
div[data-testid="stMetric"] div[data-testid="stMetricValue"] {
    color: #00D4FF !important;
    font-weight: 700 !important;
}

/* ===== 下拉框 / 多选框 ===== */
.stSelectbox, .stMultiSelect {
    background: transparent !important;
}
div[data-baseweb="select"] > div,
div[data-baseweb="multi-select"] > div {
    background-color: rgba(255, 255, 255, 0.03) !important;
    border: 1px solid rgba(0, 212, 255, 0.15) !important;
    border-radius: 8px !important;
}
div[data-baseweb="select"] *,
div[data-baseweb="multi-select"] * {
    color: #FFFFFF !important;
}
/* 下拉弹出菜单 */
[data-baseweb="menu"] {
    background-color: #11151F !important;
}
[data-baseweb="menu"] li {
    color: #E8EDF5 !important;
}
[data-baseweb="menu"] li:hover {
    background-color: rgba(0, 212, 255, 0.12) !important;
}

/* ===== 日期 / 时间 输入框 ===== */
.stDateInput input, .stTimeInput input {
    background-color: transparent !important;
    color: #FFFFFF !important;
    border: 1px solid rgba(0, 212, 255, 0.15) !important;
    border-radius: 8px !important;
}
.stDateInput input::placeholder, .stTimeInput input::placeholder {
    color: #8892A8 !important;
}

/* ===== Tabs 标签 ===== */
.stTabs [data-baseweb="tab"] {
    color: #8892A8 !important;
}
.stTabs [data-baseweb="tab"][aria-selected="true"] {
    color: #00D4FF !important;
    border-bottom: 2px solid #00D4FF !important;
}

/* ===== Plotly 图表背景透明(与主题融合) ===== */
.js-plotly-plot .plotly,
.js-plotly-plot .plot-container,
.js-plotly-plot .svg-container,
.stPlotly {
    background-color: transparent !important;
}

/* ===== 滚动条 ===== */
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-track { background: #0B0E17; }
::-webkit-scrollbar-thumb {
    background: rgba(0, 212, 255, 0.3);
    border-radius: 4px;
}
::-webkit-scrollbar-thumb:hover { background: rgba(0, 212, 255, 0.5); }
</style>
"""


def apply_custom_css() -> None:
    """注入工业科技感暗色主题 CSS(在 app.py 中调用一次即可)。"""
    st.markdown(_CSS, unsafe_allow_html=True)
