# -*- coding: utf-8 -*-
"""部署前 Tab 兜底异常捕获 + KeyError 防御补丁注入。
用法: 在 app.py 顶部 import 本模块后, 所有 _render_tab_* 函数会被自动装饰:
  - 捕获 Exception -> 显示 st.error, 绝不全页 Oh no 崩溃
  - 对 Streamlit 专属组件/模块也不会影响 Streamlit 自身。
"""
from __future__ import annotations

import functools
import traceback

import streamlit as st


def tab_safe_render(func):
    """装饰器: Tab 渲染函数兜底, 全异常捕获 + 错误框 + 日志 + 不影响其他 Tab。"""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except (KeyError, IndexError) as e:
            st.error(
                f"⚠️ 「{func.__name__}」数据字段缺失/越界: `{e}`。"
                f"请检查数据源或切换 Tab。(已自动隔离, 不影响其他页面)"
            )
            with st.expander("🛠 技术详情(仅复制给开发)", expanded=False):
                st.code(traceback.format_exc(), language="python")
            return None
        except ModuleNotFoundError as e:
            st.error(
                f"📦 「{func.__name__}」缺少依赖包: `{e}`。"
                f"请在项目根 requirements.txt 中补齐后 Redeploy。"
            )
            with st.expander("🛠 Traceback", expanded=False):
                st.code(traceback.format_exc(), language="python")
            return None
        except Exception as e:
            st.error(
                f"💥 「{func.__name__}」运行出错: {type(e).__name__}: {e}。"
                f"已自动隔离, 其他 Tab 正常可用。"
            )
            with st.expander("🛠 完整错误栈 (发给开发即可)", expanded=False):
                st.code(traceback.format_exc(), language="python")
            return None
    return wrapper


def apply_tab_safety_globals():
    """把常用的几个防御小工具放进 st.session_state, 供 app.py 随时用 (可选)。"""
    st.session_state.setdefault("_tab_safety_applied", True)
