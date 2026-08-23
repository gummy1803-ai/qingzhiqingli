"""Streamlit 顶部 Tab 栏 · 横向滑块增强。

用户反馈截图里最右侧的「整车 / 耐久 / 台架 ...」Tab 被「封锁」了（小屏/窄窗口看不到也点不到）。
本模块对页面上所有 `st.tabs(...)` 渲染出来的 Tab 栏一次性套上「横向拖动 + 两侧箭头 + 溢出隐藏」能力:

实现原则 (对齐经验 #1470453: 不能用 JS 移动/重排 Streamlit 原生组件 DOM, 否则会破坏事件绑定):
  ✅ 只注入 CSS, 给 [data-testid="stTabs"] [role="tablist"] 加 overflow + flex 不换行
  ✅ 只在 tablist 的**父容器 (stTabs 根 div)** **同级**插入 ◀ ▶ 按钮 & 底部指示条, 绝不搬运 Tab DOM
  ✅ 鼠标拖动 / 滚轮 / 触屏 只挂事件监听, 不改变 DOM 结构 → Tab 点击切换/下划线高亮 100% 保持 Streamlit 原生行为

特性:
  1) 平滑拖动: 按住 tablist 空白区可左右拖 (4px 阈值, 避免误判点击为拖动)
  2) 两端箭头: 溢出时 ◀ ▶ 圆钮玻璃拟态; 滚到两端对应方向自动弱化(25% 透明度)
  3) 未溢出隐藏: 内容宽度 < 容器时, 箭头/指示条/mask 全部隐藏, 不干扰现有 UI
  4) 响应式: ResizeObserver + MutationObserver + window.resize 三重监听, Streamlit rerun /
     手动拉窄窗口 / 动态重排 Tab 都能自动重新计算
  5) 全局生效: 调用 `enable_tab_slider()` 一次, 页面全部 st.tabs (主页面 13 个 Tab + 未来嵌套 tab) 都生效
"""
from __future__ import annotations

import streamlit as st

# ---------- 会话内单例锁: CSS/JS 只注入一次 ----------
_INJECTED_KEY: str = "_tab_slider_injected_v1"

# ---------- 全局 CSS ----------
_CSS: str = r"""
<style>
/* ============================================================
   Tabs 横向滚动 —— 覆盖 Streamlit 原生 flex-wrap 换行
   ============================================================ */

/* Streamlit 主 tabs 容器: 变成 position:relative, 为同级插入的绝对定位箭头做参照 */
div[data-testid="stTabs"] {
    position: relative !important;
    width: 100% !important;
}

/* 原生 tablist: 强制 nowrap + 可横向滚动 + 隐藏滚动条 */
div[data-testid="stTabs"] div[role="tablist"] {
    flex-wrap: nowrap !important;         /* 关键: 禁止换行 (Streamlit 默认窄屏会 wrap) */
    overflow-x: auto !important;
    overflow-y: hidden !important;
    scroll-behavior: smooth;
    -webkit-overflow-scrolling: touch;
    scrollbar-width: none;                /* Firefox */
    gap: 0 !important;                    /* Streamlit 原生 gap 在 nowrap 下不必要 */
    padding-right: 12px;                  /* 给右侧留一点呼吸区, 避免最后一个 Tab 贴边 */
    cursor: grab;
    width: 100% !important;
    box-sizing: border-box;
}
div[data-testid="stTabs"] div[role="tablist"]::-webkit-scrollbar {
    display: none;                        /* Chrome/Safari/Edge: 隐藏滚动条 */
}
div[data-testid="stTabs"] div[role="tablist"].ts-dragging {
    cursor: grabbing;
}

/* 单个 Tab 按钮: 禁止收缩, 保持最小可读宽度 */
div[data-testid="stTabs"] div[role="tablist"] > button[role="tab"],
div[data-testid="stTabs"] div[role="tablist"] > div[data-baseweb="tab"] {
    flex: 0 0 auto !important;            /* 关键: 不收缩不换行 */
    min-width: max-content;
    padding-left: 0.9rem !important;
    padding-right: 0.9rem !important;
    white-space: nowrap;
}

/* 两侧 mask 渐变提示 (仍有更多内容时): 给 tablist 直接套 mask-image */
div[data-testid="stTabs"] div[role="tablist"].ts-has-right {
    -webkit-mask-image: linear-gradient(to left, transparent 0, #000 48px);
            mask-image: linear-gradient(to left, transparent 0, #000 48px);
}
div[data-testid="stTabs"] div[role="tablist"].ts-has-left {
    -webkit-mask-image: linear-gradient(to right, transparent 0, #000 48px);
            mask-image: linear-gradient(to right, transparent 0, #000 48px);
}
div[data-testid="stTabs"] div[role="tablist"].ts-has-both {
    -webkit-mask-image:
        linear-gradient(to right, transparent 0, #000 48px calc(100% - 48px), transparent 100%);
            mask-image:
        linear-gradient(to right, transparent 0, #000 48px calc(100% - 48px), transparent 100%);
}

/* ---------- 左右箭头按钮 (绝对定位到 stTabs 容器的左右两侧外面) ---------- */
.ts-btn {
    position: absolute;
    top: 14px;
    transform: translateY(0);
    z-index: 9999;          /* 最高, 不被 Streamlit header/tab bar 盖掉 */
    width: 34px;
    height: 34px;
    border-radius: 9999px;
    border: 1px solid rgba(148, 163, 184, 0.30);
    background: rgba(15, 23, 42, 0.80);
    color: #e2e8f0;
    font-size: 15px;
    line-height: 1;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.45);
    backdrop-filter: blur(6px);
    -webkit-backdrop-filter: blur(6px);
    opacity: 0;
    pointer-events: none;
    transition: opacity 0.2s ease, background 0.15s ease, transform 0.1s ease;
}
.ts-btn:hover { background: rgba(51, 65, 85, 0.96); color: #fff; }
.ts-btn:active { transform: scale(0.92); }
.ts-btn.ts-left  { left: -14px;  }
.ts-btn.ts-right { right: -14px; }

/* 溢出时显示 */
div[data-testid="stTabs"].ts-overflow > .ts-btn {
    opacity: 1;
    pointer-events: auto;
}
/* 到两端时: 对应方向按钮弱化 */
div[data-testid="stTabs"].ts-at-start .ts-btn.ts-left   { opacity: 0.25; pointer-events: none; }
div[data-testid="stTabs"].ts-at-end   .ts-btn.ts-right  { opacity: 0.25; pointer-events: none; }

/* ---------- 底部迷你进度指示条 (溢出时显示) ---------- */
div[data-testid="stTabs"] > .ts-indicator {
    position: relative;
    margin: 4px auto 0;
    height: 3px;
    width: 120px;
    border-radius: 999px;
    background: rgba(148, 163, 184, 0.18);
    overflow: hidden;
    display: none;
}
div[data-testid="stTabs"].ts-overflow > .ts-indicator { display: block; }
div[data-testid="stTabs"] > .ts-indicator > .ts-indicator-fill {
    position: absolute;
    top: 0;
    left: 0;
    height: 100%;
    border-radius: 999px;
    background: linear-gradient(90deg, #38bdf8 0%, #6366f1 100%);
    min-width: 28px;
    transition: width 0.12s linear, left 0.12s linear;
}

/* ---------- 移动端适配: 按钮往里收, 避免超出卡片边界 ---------- */
@media (max-width: 768px) {
    .ts-btn.ts-left  { left: -2px;  top: 12px; }
    .ts-btn.ts-right { right: -2px; top: 12px; }
    div[data-testid="stTabs"] div[role="tablist"] { padding-right: 8px; }
    div[data-testid="stTabs"] div[role="tablist"].ts-has-right {
        -webkit-mask-image: linear-gradient(to left, transparent 0, #000 28px);
                mask-image: linear-gradient(to left, transparent 0, #000 28px);
    }
    div[data-testid="stTabs"] div[role="tablist"].ts-has-left {
        -webkit-mask-image: linear-gradient(to right, transparent 0, #000 28px);
                mask-image: linear-gradient(to right, transparent 0, #000 28px);
    }
    div[data-testid="stTabs"] div[role="tablist"].ts-has-both {
        -webkit-mask-image:
            linear-gradient(to right, transparent 0, #000 28px calc(100% - 28px), transparent 100%);
                mask-image:
            linear-gradient(to right, transparent 0, #000 28px calc(100% - 28px), transparent 100%);
    }
}
</style>
"""

# ---------- 初始化 JS (全局一份, MutationObserver 发现新增 stTabs 自动套滑块) ----------
_JS: str = r"""
<script>
(function () {
    "use strict";
    // 防重复注入 (Streamlit 每 rerun 都会再写一次 script, 但我们只初始化一次 MutationObserver)
    if (window.__TAB_SLIDER_INSTALLED__ === true) return;
    window.__TAB_SLIDER_INSTALLED__ = true;

    // ============================================================
    // 对单个 <div data-testid="stTabs"> 容器: 附加按钮 + 指示条 + 事件 + 更新
    // ============================================================
    function install(root) {
        if (!root || root.dataset.tsInstalled === "1") return;
        root.dataset.tsInstalled = "1";

        const tablist = root.querySelector('div[role="tablist"]');
        if (!tablist) {
            // tablist 还没渲染出来, Streamlit 可能会稍后补
            const mo = new MutationObserver(function () {
                const t = root.querySelector('div[role="tablist"]');
                if (t) { mo.disconnect(); install(root); }
            });
            mo.observe(root, { childList: true, subtree: true });
            return;
        }

        // ---- 1) 同级插入 ◀ ▶ 按钮 & 底部指示条 (都挂到 root 上) ----
        const btnL = document.createElement("button");
        btnL.type = "button";
        btnL.className = "ts-btn ts-left";
        btnL.title = "向左滑动 (Shift+滚轮 / 按住空白处拖动)";
        btnL.setAttribute("aria-label", "向左滑动 Tab 栏");
        btnL.textContent = "\u25C0";
        root.appendChild(btnL);

        const btnR = document.createElement("button");
        btnR.type = "button";
        btnR.className = "ts-btn ts-right";
        btnR.title = "向右滑动 (Shift+滚轮 / 按住空白处拖动)";
        btnR.setAttribute("aria-label", "向右滑动 Tab 栏");
        btnR.textContent = "\u25B6";
        root.appendChild(btnR);

        const indicator = document.createElement("div");
        indicator.className = "ts-indicator";
        indicator.setAttribute("aria-hidden", "true");
        const fill = document.createElement("div");
        fill.className = "ts-indicator-fill";
        indicator.appendChild(fill);
        root.appendChild(indicator);

        // ---- 2) 状态更新函数 ----
        function updateState() {
            const sw = tablist.scrollWidth;
            const cw = tablist.clientWidth;
            const sl = tablist.scrollLeft;
            const overflow = (sw - cw) > 2;
            const atStart = sl <= 1;
            const atEnd   = (sl + cw) >= (sw - 1);

            root.classList.toggle("ts-overflow", overflow);
            root.classList.toggle("ts-at-start", atStart);
            root.classList.toggle("ts-at-end", atEnd);

            tablist.classList.remove("ts-has-left", "ts-has-right", "ts-has-both");
            if (overflow) {
                if (atStart) tablist.classList.add("ts-has-right");
                else if (atEnd) tablist.classList.add("ts-has-left");
                else tablist.classList.add("ts-has-both");
            }

            // 底部进度条
            if (overflow) {
                const viewRatio = Math.max(0, Math.min(1, cw / sw));
                const fillW = viewRatio * 100;
                fill.style.width = fillW.toFixed(1) + "%";
                const maxScroll = Math.max(0, sw - cw);
                const pct = maxScroll > 0 ? (sl / maxScroll) : 0;
                const maxFillLeft = Math.max(0, 100 - fillW);
                fill.style.left = (pct * maxFillLeft).toFixed(1) + "%";
            }
        }

        // ---- 3) 交互: 按钮 ----
        function pageStep() { return Math.max(180, Math.floor(tablist.clientWidth * 0.8)); }
        btnL.addEventListener("click", function () {
            tablist.scrollBy({ left: -pageStep(), behavior: "smooth" });
        });
        btnR.addEventListener("click", function () {
            tablist.scrollBy({ left:  pageStep(), behavior: "smooth" });
        });

        // ---- 4) 交互: Shift + 滚轮 = 横向 ----
        tablist.addEventListener("wheel", function (e) {
            if (Math.abs(e.deltaX) > Math.abs(e.deltaY) || e.shiftKey) {
                e.preventDefault();
                const dx = (e.deltaX !== 0) ? e.deltaX : e.deltaY;
                tablist.scrollLeft += dx;
            }
        }, { passive: false });

        // ---- 5) 交互: 鼠标按住拖动 (不搬 DOM, 只加监听) ----
        let isDown = false;
        let startX = 0;
        let startScrollLeft = 0;
        const THRESHOLD = 4;
        let dragged = false;
        function isInteractive(node) {
            if (!node || !(node instanceof Element)) return false;
            return !!node.closest("button, [role='tab'], a, input, select, textarea, label");
        }
        tablist.addEventListener("mousedown", function (e) {
            if (e.button !== 0) return;
            if (isInteractive(e.target)) return;
            isDown = true;
            dragged = false;
            startX = e.pageX;
            startScrollLeft = tablist.scrollLeft;
            tablist.classList.add("ts-dragging");
            try { document.body.style.userSelect = "none"; } catch (_) {}
        });
        window.addEventListener("mousemove", function (e) {
            if (!isDown) return;
            const dx = e.pageX - startX;
            if (!dragged && Math.abs(dx) < THRESHOLD) return;
            dragged = true;
            tablist.scrollLeft = startScrollLeft - dx;
        });
        function endDrag() {
            if (!isDown) return;
            isDown = false;
            tablist.classList.remove("ts-dragging");
            try { document.body.style.userSelect = ""; } catch (_) {}
        }
        window.addEventListener("mouseup", endDrag);
        window.addEventListener("mouseleave", endDrag);
        tablist.addEventListener("click", function (e) {
            if (dragged) { e.stopPropagation(); e.preventDefault(); dragged = false; return false; }
        }, true);

        // ---- 6) 响应式观察: 容器/子元素尺寸变化 & Streamlit rerun DOM 变化 ----
        if (window.ResizeObserver) {
            const ro = new ResizeObserver(function () { updateState(); });
            ro.observe(tablist);
            ro.observe(root);
        }
        window.addEventListener("resize", updateState, { passive: true });
        tablist.addEventListener("scroll", updateState, { passive: true });
        if (window.MutationObserver) {
            new MutationObserver(updateState).observe(tablist, {
                childList: true, subtree: true, attributes: true,
            });
        }

        // 初始化 (2 次, 应对 Streamlit 异步 DOM 搭建)
        requestAnimationFrame(updateState);
        setTimeout(updateState, 300);
    }

    // ============================================================
    // 扫描页面已有 stTabs + MutationObserver 监听新插入的 stTabs
    // ============================================================
    function scanAll() {
        document.querySelectorAll('div[data-testid="stTabs"]').forEach(install);
    }
    requestAnimationFrame(scanAll);
    setTimeout(scanAll, 500);

    if (window.MutationObserver) {
        new MutationObserver(function (muts) {
            let need = false;
            for (const m of muts) {
                for (const n of m.addedNodes) {
                    if (n instanceof Element) {
                        if (n.matches && n.matches('div[data-testid="stTabs"]')) { need = true; }
                        if (n.querySelector && n.querySelector('div[data-testid="stTabs"]')) need = true;
                    }
                }
            }
            if (need) scanAll();
        }).observe(document.documentElement, { childList: true, subtree: true });
    }
})();
</script>
"""


def enable_tab_slider() -> None:
    """调用一次: 对页面全部 st.tabs(...) 顶部 Tab 栏启用「横向拖动 + 箭头 + 溢出隐藏」。

    - 幂等: 多次调用不会重复注入;
    - 全局: 之后用 MutationObserver 动态发现新的 stTabs 也会自动套滑块;
    - 零侵入: 不改动 Streamlit 原生 Tab DOM 结构与事件绑定, 只加 CSS/同级按钮/事件监听。

    用法: 在 `app.py` 顶部 (set_page_config 之后, `st.tabs(...)` 调用之前任意位置) 写一行:
        from components.tab_slider import enable_tab_slider
        enable_tab_slider()
    """
    if st.session_state.get(_INJECTED_KEY):
        return
    # CSS + JS 都用 st.markdown 注入到主页面 (components.html 是 iframe, 到不了主 DOM)
    st.markdown(_CSS, unsafe_allow_html=True)
    st.markdown(_JS, unsafe_allow_html=True)
    st.session_state[_INJECTED_KEY] = True
