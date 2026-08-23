"""企业级主题 CSS 注入。

设计理念:
- 深色科技感底色 (#0B0E17) + 科技蓝主色 (#00D4FF)
- 卡片式布局,圆角 + 微光边框 + hover 发光
- 统一的字体层级和间距
- 精致的表格、按钮、输入框样式
- Tab 高亮 + 滚动条美化
通过 st.markdown 注入,不破坏 Streamlit 功能。
"""
from __future__ import annotations

import streamlit as st

_CSS = """
<style>
/* ===== 字体导入 ===== */
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

/* ===== 全局背景 & 字体 ===== */
.stApp, section[data-testid="stMain"] {
    background: linear-gradient(135deg, #0B0E17 0%, #0F1320 50%, #0B0E17 100%) !important;
    color: #E8EDF5 !important;
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif !important;
}
section[data-testid="stSidebar"], div[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #0D111C 0%, #0B0E17 100%) !important;
    border-right: 1px solid rgba(0, 212, 255, 0.08) !important;
}

/* ===== 顶部标题栏 ===== */
.stApp > header, [data-testid="stHeader"] {
    background: rgba(11, 14, 23, 0.85) !important;
    backdrop-filter: blur(12px) !important;
    border-bottom: 1px solid rgba(0, 212, 255, 0.1) !important;
}

/* ===== 标题层级 ===== */
h1 {
    color: #00D4FF !important;
    font-weight: 700 !important;
    letter-spacing: -0.02em !important;
    font-size: 1.75rem !important;
}
h2 {
    color: #00D4FF !important;
    font-weight: 600 !important;
    letter-spacing: -0.01em !important;
    font-size: 1.4rem !important;
    border-bottom: 1px solid rgba(0, 212, 255, 0.12) !important;
    padding-bottom: 8px !important;
}
h3, h4 {
    color: #7DD3FC !important;
    font-weight: 600 !important;
}
.stSubheader, .stSubheader p {
    color: #7DD3FC !important;
}
.stCaption, .stCaption p {
    color: #6B7894 !important;
    font-size: 0.8rem !important;
}
.stMarkdown p {
    color: #C4CCDB !important;
    line-height: 1.6 !important;
}
.stMarkdown strong {
    color: #E8EDF5 !important;
}

/* ===== stMetric 统计卡片 ===== */
div[data-testid="stMetric"] {
    background: linear-gradient(135deg, rgba(0, 212, 255, 0.04) 0%, rgba(0, 212, 255, 0.01) 100%) !important;
    border: 1px solid rgba(0, 212, 255, 0.12) !important;
    border-radius: 10px !important;
    padding: 16px 18px !important;
    transition: all 0.3s ease !important;
}
div[data-testid="stMetric"]:hover {
    border-color: rgba(0, 212, 255, 0.35) !important;
    box-shadow: 0 4px 20px rgba(0, 212, 255, 0.08), 0 0 0 1px rgba(0, 212, 255, 0.1) !important;
    transform: translateY(-1px) !important;
}
div[data-testid="stMetric"] label p {
    color: #6B7894 !important;
    font-size: 0.78rem !important;
    font-weight: 500 !important;
    text-transform: uppercase !important;
    letter-spacing: 0.05em !important;
}
div[data-testid="stMetric"] div[data-testid="stMetricValue"] {
    color: #00D4FF !important;
    font-weight: 700 !important;
    font-size: 1.5rem !important;
}

/* ===== 容器/卡片 (st.container border) ===== */
div[data-testid="stVerticalBlockBorderWrapper"] {
    background: rgba(255, 255, 255, 0.02) !important;
    border: 1px solid rgba(0, 212, 255, 0.1) !important;
    border-radius: 12px !important;
    padding: 20px !important;
}

/* ===== 下拉框 / 多选框 ===== */
.stSelectbox, .stMultiSelect {
    background: transparent !important;
}
div[data-baseweb="select"] > div,
div[data-baseweb="multi-select"] > div {
    background-color: rgba(255, 255, 255, 0.03) !important;
    border: 1px solid rgba(0, 212, 255, 0.12) !important;
    border-radius: 8px !important;
    transition: border-color 0.2s ease !important;
}
div[data-baseweb="select"] > div:hover,
div[data-baseweb="multi-select"] > div:hover {
    border-color: rgba(0, 212, 255, 0.3) !important;
}
div[data-baseweb="select"] *,
div[data-baseweb="multi-select"] * {
    color: #E8EDF5 !important;
}
/* 下拉弹出菜单 */
[data-baseweb="menu"] {
    background-color: #11151F !important;
    border: 1px solid rgba(0, 212, 255, 0.15) !important;
    border-radius: 10px !important;
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4) !important;
}
[data-baseweb="menu"] li {
    color: #C4CCDB !important;
    border-radius: 6px !important;
    transition: background 0.15s ease !important;
}
[data-baseweb="menu"] li:hover {
    background-color: rgba(0, 212, 255, 0.1) !important;
    color: #00D4FF !important;
}

/* ===== 日期 / 时间 输入框 ===== */
.stDateInput input, .stTimeInput input,
.stTextInput input, .stNumberInput input {
    background-color: rgba(255, 255, 255, 0.03) !important;
    color: #E8EDF5 !important;
    border: 1px solid rgba(0, 212, 255, 0.12) !important;
    border-radius: 8px !important;
    transition: border-color 0.2s ease !important;
}
.stDateInput input:focus, .stTimeInput input:focus,
.stTextInput input:focus, .stNumberInput input:focus {
    border-color: rgba(0, 212, 255, 0.4) !important;
    box-shadow: 0 0 0 3px rgba(0, 212, 255, 0.08) !important;
}
.stDateInput input::placeholder, .stTimeInput input::placeholder,
.stTextInput input::placeholder {
    color: #6B7894 !important;
}

/* ===== 按钮 ===== */
.stButton > button, .stFormSubmitButton > button {
    background: linear-gradient(135deg, rgba(0, 212, 255, 0.15) 0%, rgba(0, 212, 255, 0.05) 100%) !important;
    color: #00D4FF !important;
    border: 1px solid rgba(0, 212, 255, 0.25) !important;
    border-radius: 8px !important;
    font-weight: 500 !important;
    transition: all 0.25s ease !important;
}
.stButton > button:hover {
    background: linear-gradient(135deg, rgba(0, 212, 255, 0.25) 0%, rgba(0, 212, 255, 0.1) 100%) !important;
    border-color: rgba(0, 212, 255, 0.5) !important;
    box-shadow: 0 2px 12px rgba(0, 212, 255, 0.12) !important;
}
.stButton > button[kind="primary"] {
    background: linear-gradient(135deg, #0096C7 0%, #00D4FF 100%) !important;
    color: #0B0E17 !important;
    border: none !important;
    font-weight: 600 !important;
}
.stButton > button[kind="primary"]:hover {
    box-shadow: 0 4px 16px rgba(0, 212, 255, 0.25) !important;
}

/* ===== Tabs 标签 ===== */
.stTabs [data-baseweb="tab-list"] {
    gap: 2px !important;
    border-bottom: 1px solid rgba(0, 212, 255, 0.1) !important;
}
.stTabs [data-baseweb="tab"] {
    color: #6B7894 !important;
    font-weight: 500 !important;
    font-size: 0.85rem !important;
    padding: 8px 16px !important;
    border-radius: 8px 8px 0 0 !important;
    transition: all 0.2s ease !important;
}
.stTabs [data-baseweb="tab"]:hover {
    color: #C4CCDB !important;
    background: rgba(0, 212, 255, 0.04) !important;
}
.stTabs [data-baseweb="tab"][aria-selected="true"] {
    color: #00D4FF !important;
    background: rgba(0, 212, 255, 0.06) !important;
    border-bottom: 2px solid #00D4FF !important;
}
.stTabs [data-baseweb="tab-border"] {
    display: none !important;
}
.stTabs [data-baseweb="tab-highlight"] {
    background-color: #00D4FF !important;
    height: 2px !important;
}

/* ===== 表格 (DataFrame) ===== */
.stDataFrame, .stTable {
    border: 1px solid rgba(0, 212, 255, 0.08) !important;
    border-radius: 10px !important;
    overflow: hidden !important;
}
.stDataFrame table {
    border-radius: 10px !important;
}
.stDataFrame th {
    background-color: rgba(0, 212, 255, 0.08) !important;
    color: #7DD3FC !important;
    font-weight: 600 !important;
    font-size: 0.82rem !important;
    text-transform: uppercase !important;
    letter-spacing: 0.03em !important;
}
.stDataFrame td {
    color: #C4CCDB !important;
    font-size: 0.85rem !important;
}
.stDataFrame tr:hover td {
    background-color: rgba(0, 212, 255, 0.04) !important;
}

/* ===== Expander 折叠面板 ===== */
.streamlit-expander {
    background: rgba(255, 255, 255, 0.02) !important;
    border: 1px solid rgba(0, 212, 255, 0.08) !important;
    border-radius: 10px !important;
}
.streamlit-expander > summary {
    color: #7DD3FC !important;
    font-weight: 500 !important;
}

/* ===== Alert/Info/Success/Error 框 ===== */
div[data-testid="stAlert"] {
    border-radius: 10px !important;
    border: 1px solid !important;
}
div[data-testid="stAlert"] p {
    color: #C4CCDB !important;
}

/* === Info 蓝色 === */
div[data-testid="stAlertContainerInfo"] {
    background: rgba(0, 212, 255, 0.06) !important;
    border-color: rgba(0, 212, 255, 0.2) !important;
}
/* === Success 绿色 === */
div[data-testid="stAlertContainerSuccess"] {
    background: rgba(34, 197, 94, 0.06) !important;
    border-color: rgba(34, 197, 94, 0.2) !important;
}
/* === Warning 黄色 === */
div[data-testid="stAlertContainerWarning"] {
    background: rgba(251, 191, 36, 0.06) !important;
    border-color: rgba(251, 191, 36, 0.2) !important;
}
/* === Error 红色 === */
div[data-testid="stAlertContainerError"] {
    background: rgba(239, 68, 68, 0.06) !important;
    border-color: rgba(239, 68, 68, 0.2) !important;
}

/* ===== Divider 分割线 ===== */
hr, .stDivider {
    border-color: rgba(0, 212, 255, 0.08) !important;
    margin: 20px 0 !important;
}

/* ===== Plotly 图表背景透明 ===== */
.js-plotly-plot .plotly,
.js-plotly-plot .plot-container,
.js-plotly-plot .svg-container,
.stPlotly {
    background-color: transparent !important;
}
.js-plotly-plot .modebar {
    background-color: transparent !important;
}

/* ===== 滚动条 ===== */
::-webkit-scrollbar {
    width: 6px !important;
    height: 6px !important;
}
::-webkit-scrollbar-track {
    background: transparent !important;
}
::-webkit-scrollbar-thumb {
    background: rgba(0, 212, 255, 0.2) !important;
    border-radius: 3px !important;
}
::-webkit-scrollbar-thumb:hover {
    background: rgba(0, 212, 255, 0.4) !important;
}

/* ===== Spinner 加载动画 ===== */
.stSpinner > div {
    border-top-color: #00D4FF !important;
}

/* ===== Sidebar 侧边栏 ===== */
section[data-testid="stSidebar"] .stMarkdown h3 {
    color: #00D4FF !important;
    font-size: 0.9rem !important;
    text-transform: uppercase !important;
    letter-spacing: 0.05em !important;
}
section[data-testid="stSidebar"] .stSelectbox label p,
section[data-testid="stSidebar"] .stMultiSelect label p {
    color: #8892A8 !important;
    font-size: 0.8rem !important;
}

/* ===== Radio / Checkbox ===== */
.stRadio label p, .stCheckbox label p {
    color: #C4CCDB !important;
    font-size: 0.85rem !important;
}
.stRadio label p:hover {
    color: #00D4FF !important;
}

/* ===== JSON 展示 ===== */
.stJson {
    background: rgba(255, 255, 255, 0.02) !important;
    border: 1px solid rgba(0, 212, 255, 0.08) !important;
    border-radius: 8px !important;
}

/* ===== Code 代码块 ===== */
.stCode {
    background: rgba(0, 0, 0, 0.3) !important;
    border: 1px solid rgba(0, 212, 255, 0.08) !important;
    border-radius: 8px !important;
}

/* ===== 隐藏 Streamlit 默认元素 ===== */
#MainMenu { visibility: hidden !important; }
footer { visibility: hidden !important; }
#stDecoration { display: none !important; }
</style>
"""


def apply_custom_css() -> None:
    """注入企业级深色科技主题 CSS(在 app.py 中调用一次即可)。"""
    st.markdown(_CSS, unsafe_allow_html=True)
