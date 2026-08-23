"""LetterGlitch 组件 - 故障风格字母动画。

纯 HTML/JS 版本,通过 st.components.v1.html 嵌入 Streamlit。
来源:React Bits LetterGlitch 组件移植。
"""
from __future__ import annotations

import json
from pathlib import Path

import streamlit as st


# 默认字符集和颜色配置
DEFAULT_CHARACTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ!@#$&*()-_+=/[]{};:<>.,0123456789"
DEFAULT_COLORS = ['#2b4539', '#61dca3', '#61b3dc']


def _build_glitch_html(
    glitch_colors: list[str] | None = None,
    glitch_speed: int = 50,
    center_vignette: bool = True,
    outer_vignette: bool = False,
    smooth: bool = True,
    characters: str = DEFAULT_CHARACTERS,
    height: int = 180,
) -> str:
    """构建 LetterGlitch HTML 源码。

    Args:
        glitch_colors: 字母故障颜色数组
        glitch_speed: 字符打乱速度(ms)
        center_vignette: 是否开启中心径向渐变
        outer_vignette: 是否开启外部径向渐变
        smooth: 是否开启平滑过渡
        characters: 字符集
        height: 画布高度(px)
    """
    colors = glitch_colors or DEFAULT_COLORS

    return f"""
<div id="letter-glitch-container" style="
    position: relative;
    width: 100%;
    height: {height}px;
    background: #000;
    overflow: hidden;
    border-radius: 8px;
">
    <canvas id="glitch-canvas" style="
        display: block;
        width: 100%;
        height: 100%;
    "></canvas>
    {"<div id='outer-vignette' style='position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;background:radial-gradient(circle,rgba(0,0,0,0) 60%,rgba(0,0,0,1) 100%)'></div>" if outer_vignette else ""}
    {"<div id='center-vignette' style='position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;background:radial-gradient(circle,rgba(0,0,0,0.8) 0%,rgba(0,0,0,0) 60%)'></div>" if center_vignette else ""}
</div>

<script>
(function() {{
    const glitchColors = {json.dumps(colors)};
    const glitchSpeed = {glitch_speed};
    const smooth = {str(smooth).lower()};
    const lettersAndSymbols = "{characters}".split('');

    const fontSize = 16;
    const charWidth = 10;
    const charHeight = 20;

    const canvas = document.getElementById('glitch-canvas');
    const container = document.getElementById('letter-glitch-container');
    if (!canvas || !container) return;

    const ctx = canvas.getContext('2d');
    const letters = [];
    let grid = {{ columns: 0, rows: 0 }};
    let lastGlitchTime = Date.now();
    let animationId = null;

    function getRandomChar() {{
        return lettersAndSymbols[Math.floor(Math.random() * lettersAndSymbols.length)];
    }}

    function getRandomColor() {{
        return glitchColors[Math.floor(Math.random() * glitchColors.length)];
    }}

    function hexToRgb(hex) {{
        const shorthandRegex = /^#?([a-f\\d])([a-f\\d])([a-f\\d])$/i;
        hex = hex.replace(shorthandRegex, (m, r, g, b) => {{
            return r + r + g + g + b + b;
        }});
        const result = /^#?([a-f\\d]{{2}})([a-f\\d]{{2}})([a-f\\d]{{2}})$/i.exec(hex);
        return result ? {{
            r: parseInt(result[1], 16),
            g: parseInt(result[2], 16),
            b: parseInt(result[3], 16)
        }} : null;
    }}

    function interpolateColor(start, end, factor) {{
        const result = {{
            r: Math.round(start.r + (end.r - start.r) * factor),
            g: Math.round(start.g + (end.g - start.g) * factor),
            b: Math.round(start.b + (end.b - start.b) * factor)
        }};
        return `rgb(${{result.r}}, ${{result.g}}, ${{result.b}})`;
    }}

    function calculateGrid(width, height) {{
        const columns = Math.ceil(width / charWidth);
        const rows = Math.ceil(height / charHeight);
        return {{ columns, rows }};
    }}

    function initializeLetters(columns, rows) {{
        grid = {{ columns, rows }};
        const totalLetters = columns * rows;
        letters.length = 0;
        for (let i = 0; i < totalLetters; i++) {{
            letters.push({{
                char: getRandomChar(),
                color: getRandomColor(),
                targetColor: getRandomColor(),
                colorProgress: 1
            }});
        }}
    }}

    function drawLetters() {{
        const rect = canvas.getBoundingClientRect();
        ctx.clearRect(0, 0, rect.width, rect.height);
        ctx.font = `${{fontSize}}px monospace`;
        ctx.textBaseline = 'top';

        letters.forEach((letter, index) => {{
            const x = (index % grid.columns) * charWidth;
            const y = Math.floor(index / grid.columns) * charHeight;
            ctx.fillStyle = letter.color;
            ctx.fillText(letter.char, x, y);
        }});
    }}

    function updateLetters() {{
        const updateCount = Math.max(1, Math.floor(letters.length * 0.05));
        for (let i = 0; i < updateCount; i++) {{
            const index = Math.floor(Math.random() * letters.length);
            if (!letters[index]) continue;

            letters[index].char = getRandomChar();
            letters[index].targetColor = getRandomColor();

            if (!smooth) {{
                letters[index].color = letters[index].targetColor;
                letters[index].colorProgress = 1;
            }} else {{
                letters[index].colorProgress = 0;
            }}
        }}
    }}

    function handleSmoothTransitions() {{
        let needsRedraw = false;
        letters.forEach(letter => {{
            if (letter.colorProgress < 1) {{
                letter.colorProgress += 0.05;
                if (letter.colorProgress > 1) letter.colorProgress = 1;

                const startRgb = hexToRgb(letter.color);
                const endRgb = hexToRgb(letter.targetColor);
                if (startRgb && endRgb) {{
                    letter.color = interpolateColor(startRgb, endRgb, letter.colorProgress);
                    needsRedraw = true;
                }}
            }}
        }});
        if (needsRedraw) drawLetters();
    }}

    function resizeCanvas() {{
        const dpr = window.devicePixelRatio || 1;
        const rect = container.getBoundingClientRect();
        canvas.width = rect.width * dpr;
        canvas.height = rect.height * dpr;
        canvas.style.width = `${{rect.width}}px`;
        canvas.style.height = `${{rect.height}}px`;
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        const {{ columns, rows }} = calculateGrid(rect.width, rect.height);
        initializeLetters(columns, rows);
        drawLetters();
    }}

    function animate() {{
        const now = Date.now();
        if (now - lastGlitchTime >= glitchSpeed) {{
            updateLetters();
            drawLetters();
            lastGlitchTime = now;
        }}
        if (smooth) handleSmoothTransitions();
        animationId = requestAnimationFrame(animate);
    }}

    // 初始化
    setTimeout(() => {{
        resizeCanvas();
        animate();
    }}, 50);

    // 窗口大小变化时重新调整
    let resizeTimeout;
    window.addEventListener('resize', () => {{
        clearTimeout(resizeTimeout);
        resizeTimeout = setTimeout(() => {{
            if (animationId) cancelAnimationFrame(animationId);
            resizeCanvas();
            animate();
        }}, 100);
    }});
}})();
</script>
"""


def render_letter_glitch(
    glitch_colors: list[str] | None = None,
    glitch_speed: int = 50,
    center_vignette: bool = True,
    outer_vignette: bool = False,
    smooth: bool = True,
    height: int = 180,
) -> None:
    """渲染 LetterGlitch 故障风格字母动画。

    Args:
        glitch_colors: 字母故障颜色,默认深色科技配色
        glitch_speed: 字符打乱速度(ms),越小越快
        center_vignette: 是否开启中心径向渐变(聚焦效果)
        outer_vignette: 是否开启外部径向渐变(暗角效果)
        smooth: 是否开启颜色平滑过渡
        height: 画布高度(px)
    """
    try:
        from streamlit.components.v1 import html as st_html
    except ImportError:
        st.warning("需要安装 streamlit.components 才能使用 LetterGlitch 组件")
        return

    html_code = _build_glitch_html(
        glitch_colors=glitch_colors,
        glitch_speed=glitch_speed,
        center_vignette=center_vignette,
        outer_vignette=outer_vignette,
        smooth=smooth,
        height=height,
    )
    st_html(html_code, height=height)
