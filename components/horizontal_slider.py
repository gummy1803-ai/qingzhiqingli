"""水平滑块包装器: 将 with 块内的 Streamlit 控件包进「可水平拖动 + 两端箭头提示」的容器。

适用场景:
- 燃电运行看板的「选项栏」(车辆 / 起始 / 结束 / 信号 四列) 在窄屏下横向空间不足,
  某些选项控件被遮挡或发生换行 → 用本包装器将选项栏固定为单行可横向滚动,
  用户可拖动空白区域 / 点箭头 / 触屏滑动查看被遮挡内容。

特性 (对齐用户需求):
  ✅ 平滑拖动体验: 鼠标按住非交互区域 (空白/标题文本) 直接左右拖拽,
     触屏端原生横滑,滚动带 smooth 动画。
  ✅ 清晰的视觉指示:
      - 溢出时左右两侧自动出现 ◀ ▶ 圆形半透明按钮;
      - 滚到两端 → 对应方向按钮弱化 (25% 不透明) 以示到头;
      - 内容两侧 mask 渐变模糊提示「还有更多」。
      - 底部迷你进度条: 80px 宽小滑块显示已读位置 / 剩余量。
  ✅ 未溢出自动隐藏: 当内容宽度 < 容器 (大屏足以完整显示),
     隐藏 ◀ ▶ 按钮 / 渐变 / 进度条,不制造视觉干扰。
  ✅ 响应式: 任何尺寸都生效 (ResizeObserver 实时监测容器与内容尺寸变化,
     Streamlit rerun / window resize / 内部 childList 变动均自动重新计算)。
  ✅ 无依赖: 纯原生 CSS + JavaScript,不需要任何前端库。
"""
from __future__ import annotations

import contextlib
import hashlib
from typing import Iterator

import streamlit as st
import streamlit.components.v1 as components

# ---------- 会话内单例: 避免重复注入 CSS ----------
_CSS_INJECTED_KEY: str = "_hslider_css_injected_v1"

# ---------- 全局 CSS ----------
# 注意: Streamlit 主题色与深色背景,按钮用 slate-800 半透明 + 玻璃拟态;
# 进度条渐变用企业蓝紫渐变 (sky-400 → indigo-500), 与 theme.py 一致。
_CSS: str = r"""
<style>
/* ============================================================
   Horizontal Slider Wrapper (燃电看板 · 选项栏滑块)
   ============================================================ */

/* 根容器: 相对定位, 给绝对定位的箭头提供参照 */
.hs-root {
    position: relative;
    width: 100%;
    box-sizing: border-box;
    padding: 0;
    margin: 2px 0 10px 0;
}

/* 滚动轨道: 水平可滑动, 隐藏原生滚动条 */
.hs-track {
    position: relative;
    overflow-x: auto;
    overflow-y: hidden;
    scroll-behavior: smooth;
    -webkit-overflow-scrolling: touch;     /* iOS 惯性滚动 */
    scrollbar-width: none;                 /* Firefox: 隐藏滚动条 */
    cursor: grab;
    width: 100%;
    box-sizing: border-box;
}
.hs-track::-webkit-scrollbar { display: none; }  /* Chrome / Safari / Edge: 隐藏滚动条 */
.hs-track.hs-dragging { cursor: grabbing; }

/* 轨道内的「真正内容容器」— 我们把 Streamlit 原来的 columns/widgets 放进这里,
   保证它不换行, 保持最小内容宽度 (由子元素决定)。
   为了避免 Streamlit columns 小屏断点触发自动堆叠 (flex-wrap), 我们强制 nowrap +
   保持子元素 flex-basis 自动收缩。 */
.hs-track > .hs-inner {
    display: flex;
    flex-flow: row nowrap;
    align-items: flex-start;
    gap: 0.5rem;
    min-width: 100%;
    width: max-content;      /* 关键: 按子元素真实宽度撑开, 触发水平滚动 */
}

/* 防止 Streamlit 原生 columns 的「小屏堆叠」覆盖我们的 nowrap */
.hs-track > .hs-inner > [data-testid="stHorizontalBlock"] {
    flex-wrap: nowrap !important;
    width: max-content !important;
    min-width: 100%;
    gap: 0.5rem;
}
.hs-track > .hs-inner > [data-testid="stHorizontalBlock"] > [data-testid="column"] {
    flex: 0 0 auto;        /* 关键: 每列都按自己的内容宽度, 不收缩换行 */
    min-width: 220px;      /* 最小 220px, 保证 selectbox/date_input 可读 */
    width: auto;
}

/* ---------- 两侧渐变遮罩 (提示: 还有更多) ---------- */
.hs-track.hs-has-left {
    -webkit-mask-image: linear-gradient(to right, transparent 0, #000 48px);
            mask-image: linear-gradient(to right, transparent 0, #000 48px);
}
.hs-track.hs-has-right {
    -webkit-mask-image: linear-gradient(to left, transparent 0, #000 48px);
            mask-image: linear-gradient(to left, transparent 0, #000 48px);
}
.hs-track.hs-has-both {
    -webkit-mask-image:
        linear-gradient(to right, transparent 0, #000 48px calc(100% - 48px), transparent 100%);
            mask-image:
        linear-gradient(to right, transparent 0, #000 48px calc(100% - 48px), transparent 100%);
}

/* ---------- 左 / 右箭头按钮 ---------- */
.hs-btn {
    position: absolute;
    top: 50%;
    transform: translateY(-50%);
    z-index: 20;
    width: 36px;
    height: 36px;
    border-radius: 9999px;
    border: 1px solid rgba(148, 163, 184, 0.25);
    background: rgba(15, 23, 42, 0.72);
    color: #e2e8f0;
    font-size: 16px;
    line-height: 1;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    box-shadow: 0 4px 14px rgba(0, 0, 0, 0.35);
    backdrop-filter: blur(6px);
    -webkit-backdrop-filter: blur(6px);
    opacity: 0;
    pointer-events: none;
    transition: opacity 0.2s ease, background 0.15s ease, transform 0.1s ease;
}
.hs-btn:hover { background: rgba(51, 65, 85, 0.94); color: #fff; }
.hs-btn:active { transform: translateY(-50%) scale(0.92); }
.hs-btn.hs-left  { left: -14px; }
.hs-btn.hs-right { right: -14px; }

/* 溢出时显示箭头 */
.hs-root.hs-overflow .hs-btn {
    opacity: 1;
    pointer-events: auto;
}
/* 滚到最左 → 左箭头半透明, 示意无法再向左 */
.hs-root.hs-at-start .hs-btn.hs-left   { opacity: 0.25; pointer-events: none; }
.hs-root.hs-at-end   .hs-btn.hs-right  { opacity: 0.25; pointer-events: none; }

/* ---------- 底部迷你进度指示条 (仅溢出时显示) ---------- */
.hs-indicator {
    position: relative;
    margin: 4px auto 0;
    height: 3px;
    width: 96px;
    border-radius: 999px;
    background: rgba(148, 163, 184, 0.18);
    overflow: hidden;
    display: none;
}
.hs-root.hs-overflow .hs-indicator { display: block; }
.hs-indicator-fill {
    position: absolute;
    top: 0;
    left: 0;
    height: 100%;
    border-radius: 999px;
    background: linear-gradient(90deg, #38bdf8 0%, #6366f1 100%);
    transition: width 0.12s linear, left 0.12s linear;
    min-width: 24px;
}

/* ---------- 响应式断点: 移动端按钮内边一点,不超出卡片边缘 ---------- */
@media (max-width: 768px) {
    .hs-btn.hs-left  { left: -2px; }
    .hs-btn.hs-right { right: -2px; }
    .hs-track.hs-has-left {
        -webkit-mask-image: linear-gradient(to right, transparent 0, #000 28px);
                mask-image: linear-gradient(to right, transparent 0, #000 28px);
    }
    .hs-track.hs-has-right {
        -webkit-mask-image: linear-gradient(to left, transparent 0, #000 28px);
                mask-image: linear-gradient(to left, transparent 0, #000 28px);
    }
    .hs-track.hs-has-both {
        -webkit-mask-image:
            linear-gradient(to right, transparent 0, #000 28px calc(100% - 28px), transparent 100%);
                mask-image:
            linear-gradient(to right, transparent 0, #000 28px calc(100% - 28px), transparent 100%);
    }
}
</style>
"""

# ---------- 初始化 JS (每个实例一份, 用唯一 KEY 区分) ----------
_JS_TEMPLATE: str = r"""
<script data-hslider-key="__KEY__">
(function () {
    "use strict";
    const KEY = "__KEY__";

    // ---- 找到挂载点 DOM (由 with 块的 md 渲染出来) ----
    const mountId = "hs-mount-" + KEY;
    const mount = document.getElementById(mountId);
    if (!mount) {
        console.warn("[hslider] 找不到挂载点:", mountId);
        return;
    }
    // 防重复初始化
    if (mount.dataset.hsReady === "1") return;
    mount.dataset.hsReady = "1";

    // ============================================================
    // 构建 DOM:
    //   mount
    //   └── .hs-root
    //       ├── .hs-track
    //       │   └── .hs-inner (把 mount 下原有的 widget block 挪进这里)
    //       ├── button.hs-btn.hs-left  (◀)
    //       ├── button.hs-btn.hs-right (▶)
    //       └── .hs-indicator (底部迷你滑块)
    //           └── .hs-indicator-fill
    // ============================================================
    const root = document.createElement("div");
    root.className = "hs-root";
    root.id = "hs-root-" + KEY;

    const track = document.createElement("div");
    track.className = "hs-track";

    const inner = document.createElement("div");
    inner.className = "hs-inner";
    // mount.children 是 Streamlit 刚才在 with 块里给我们放的 widgets div
    //   → 逐个挪到 inner 里 (保持顺序)
    while (mount.firstChild) {
        // Streamlit 常把 <style>/空 text/注释 也当 child, 直接保留原样
        inner.appendChild(mount.firstChild);
    }
    track.appendChild(inner);
    root.appendChild(track);

    const btnL = document.createElement("button");
    btnL.type = "button";
    btnL.className = "hs-btn hs-left";
    btnL.title = "向左滑动 (Shift+滚轮也可)";
    btnL.setAttribute("aria-label", "向左滑动选项栏");
    btnL.textContent = "\u25C0";  // ◀

    const btnR = document.createElement("button");
    btnR.type = "button";
    btnR.className = "hs-btn hs-right";
    btnR.title = "向右滑动 (Shift+滚轮也可)";
    btnR.setAttribute("aria-label", "向右滑动选项栏");
    btnR.textContent = "\u25B6";  // ▶

    root.appendChild(btnL);
    root.appendChild(btnR);

    const indicator = document.createElement("div");
    indicator.className = "hs-indicator";
    indicator.setAttribute("aria-hidden", "true");
    const indicatorFill = document.createElement("div");
    indicatorFill.className = "hs-indicator-fill";
    indicator.appendChild(indicatorFill);
    root.appendChild(indicator);

    mount.appendChild(root);

    // ============================================================
    // 状态更新: 判断是否溢出、是否到两端、更新 mask/按钮/进度条
    // ============================================================
    function updateState() {
        const sw = track.scrollWidth;
        const cw = track.clientWidth;
        const sl = track.scrollLeft;
        const overflow = (sw - cw) > 2;   // 留 2px 容差, 避免四舍五入抖动
        const atStart = sl <= 1;
        const atEnd   = (sl + cw) >= (sw - 1);

        root.classList.toggle("hs-overflow", overflow);
        root.classList.toggle("hs-at-start", atStart);
        root.classList.toggle("hs-at-end",   atEnd);

        // mask 渐变
        track.classList.remove("hs-has-left", "hs-has-right", "hs-has-both");
        if (overflow) {
            if (atStart) track.classList.add("hs-has-right");
            else if (atEnd) track.classList.add("hs-has-left");
            else track.classList.add("hs-has-both");
        }

        // 底部进度条
        if (overflow) {
            const viewRatio = Math.max(0, Math.min(1, cw / sw));
            const fillW = viewRatio * 100;           // % 相对 indicator
            indicatorFill.style.width = fillW.toFixed(1) + "%";
            const maxScroll = Math.max(0, sw - cw);
            const pct = maxScroll > 0 ? (sl / maxScroll) : 0;
            const maxFillLeft = 100 - fillW;         // % 相对 indicator
            indicatorFill.style.left = (pct * maxFillLeft).toFixed(1) + "%";
        }
    }

    // ============================================================
    // 交互: 箭头点击 + 滚轮 (Shift+滚轮 → 横向) + 鼠标拖动 + 触屏
    // ============================================================
    function pageStep() {
        // 一次滚动 = 0.8 倍可见宽度, 最少 180px
        return Math.max(180, Math.floor(track.clientWidth * 0.8));
    }
    btnL.addEventListener("click", function () {
        track.scrollBy({ left: -pageStep(), behavior: "smooth" });
    });
    btnR.addEventListener("click", function () {
        track.scrollBy({ left:  pageStep(), behavior: "smooth" });
    });

    // Shift + 纵向滚轮 = 横向滚动
    track.addEventListener("wheel", function (e) {
        if (Math.abs(e.deltaX) > Math.abs(e.deltaY) || e.shiftKey) {
            e.preventDefault();
            const dx = (e.deltaX !== 0) ? e.deltaX : e.deltaY;
            track.scrollLeft += dx;
        }
    }, { passive: false });

    // ---- 鼠标按住拖动 (左键, 且目标不是交互元素) ----
    let isDown = false;
    let startX = 0;
    let startScrollLeft = 0;
    const DRAG_THRESHOLD = 4;   // 移动 4px 以上才算 drag, 避免点击被误认为拖动
    let dragged = false;

    function isInteractiveNode(node) {
        if (!node) return false;
        if (!(node instanceof Element)) return false;
        return !!node.closest(
            "button, input, select, textarea, label, option, [role='button'], "
            + "[role='option'], [role='listbox'], a"
        );
    }

    track.addEventListener("mousedown", function (e) {
        if (e.button !== 0) return;                    // 只响应左键
        if (isInteractiveNode(e.target)) return;      // 不拦截交互控件
        isDown = true;
        dragged = false;
        startX = e.pageX;
        startScrollLeft = track.scrollLeft;
        track.classList.add("hs-dragging");
        try { document.body.style.userSelect = "none"; } catch (_) {}
    });
    window.addEventListener("mousemove", function (e) {
        if (!isDown) return;
        const dx = e.pageX - startX;
        if (!dragged && Math.abs(dx) < DRAG_THRESHOLD) return;
        dragged = true;
        track.scrollLeft = startScrollLeft - dx;
    });
    function endDrag() {
        if (!isDown) return;
        isDown = false;
        track.classList.remove("hs-dragging");
        try { document.body.style.userSelect = ""; } catch (_) {}
    }
    window.addEventListener("mouseup", endDrag);
    window.addEventListener("mouseleave", endDrag);

    // 拖拽过程中避免误点: 若拖了 > threshold, 就吞掉一次 click 事件
    track.addEventListener("click", function (e) {
        if (dragged) {
            e.stopPropagation();
            e.preventDefault();
            dragged = false;
            return false;
        }
    }, true);

    // ============================================================
    // 响应式 + 自动重新计算:
    //   - ResizeObserver(track) → 容器/子元素尺寸变了
    //   - window.resize
    //   - MutationObserver(inner.childList) → Streamlit rerun 重建了内部 DOM
    // ============================================================
    if (window.ResizeObserver) {
        const ro = new ResizeObserver(function () { updateState(); });
        ro.observe(track);
        ro.observe(inner);
    }
    window.addEventListener("resize", updateState, { passive: true });
    if (window.MutationObserver) {
        const mo = new MutationObserver(function () { updateState(); });
        mo.observe(inner, { childList: true, subtree: true, attributes: true });
    }

    // 初始化 (分 2 次, 应对 Streamlit 渲染异步)
    requestAnimationFrame(updateState);
    setTimeout(updateState, 250);
})();
</script>
"""


def _safe_key(key: str) -> str:
    """把用户给的 key 转成合法 HTML id / data key (ASCII 字母/数字/下划线/短横, 长度<64)。"""
    if not key:
        key = "unnamed"
    h = hashlib.md5(key.encode("utf-8")).hexdigest()[:8]
    # 只保留 ASCII 安全字符, 防止中文/全角符号破坏 HTML id
    cleaned = "".join(
        ch for ch in key
        if ch.isascii() and (ch.isalnum() or ch in "-_")
    )
    cleaned = (cleaned or "slider")[:48]
    return f"{cleaned}-{h}"


def _inject_css_once() -> None:
    """全局 CSS 只注入一次。"""
    # 注意: 必须用 st.markdown 而非 components.html,
    # components.html 放在 iframe 里, CSS 到不了主页面。
    if not st.session_state.get(_CSS_INJECTED_KEY):
        st.markdown(_CSS, unsafe_allow_html=True)
        st.session_state[_CSS_INJECTED_KEY] = True


@contextlib.contextmanager
def horizontal_slider(key: str) -> Iterator[None]:
    """将 `with` 代码块内的 Streamlit 控件包进「可横向拖动 + 两端箭头」的滑块容器。

    Args:
        key: 同一页面多次使用时传不同唯一标识 (字母/数字/下划线/短横)。
             会被规范化并附加 8 位哈希, 不会撞 DOM id。

    示例:
        from components.horizontal_slider import horizontal_slider

        with horizontal_slider("fc_filter_options"):
            c1, c2, c3, c4 = st.columns([1, 2, 2, 3])
            with c1:
                st.selectbox("车辆", ...)
            with c2:
                st.date_input(...)
            ...
    """
    _inject_css_once()
    safe_key = _safe_key(key)
    mount_id = f"hs-mount-{safe_key}"
    init_js = _JS_TEMPLATE.replace("__KEY__", safe_key)

    # ① 打开挂载点 div (注意: Streamlit 的 st.markdown 会自己包一层 <div class="stMarkdown">,
    #   但 JS 初始化时只抓 mount_id 里面的「所有 firstChild」, 顺序仍然对)
    st.markdown(
        f'<div id="{mount_id}" style="width:100%;box-sizing:border-box;">',
        unsafe_allow_html=True,
    )
    try:
        yield  # 用户的 with 代码块在这里执行, widget 会作为 mount 的 children 被插入
    finally:
        # ② 关闭挂载点
        st.markdown("</div>", unsafe_allow_html=True)
        # ③ 注入 JS 初始化脚本 (components.html 在 iframe 里, 但 script 会
        #   用 window.parent 找不到。所以这里用 st.markdown 把 <script> 写到主页面。)
        st.markdown(init_js, unsafe_allow_html=True)
