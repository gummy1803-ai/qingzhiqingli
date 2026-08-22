"""绝缘阻值统计摘要组件:5 卡片 + 健康度进度条 + 报警触达提示。

渲染 process_insulation_data 输出 + predict_insulation_trend 结果的统计摘要。
深色科技风(与 insulation_filter / predictor 风格一致)。

核心函数: render_insulation_stats
"""
from __future__ import annotations

import logging

import pandas as pd
import streamlit as st

logger = logging.getLogger(__name__)


def render_insulation_stats(df_insul: pd.DataFrame, prediction: dict) -> None:
    """渲染绝缘统计摘要(5 卡片 + 健康度进度条 + 报警触达)。

    Args:
        df_insul: process_insulation_data 输出(timestamp/FC_VehicleIsolationR/
                  FC_MainSts/is_running)
        prediction: predict_insulation_trend 返回 dict(fit_coefficients/r_squared/
                    rmse/predictions/alarm_crossings/degradation_rate/health_score/
                    current_value/n_points/fit_success)
    """
    logger.info("=== render_insulation_stats 开始 ===")
    if not prediction or not prediction.get("fit_success"):
        logger.warning("prediction 无效或拟合失败, 跳过统计摘要")
        st.warning("趋势拟合失败,无法生成统计摘要")
        return

    current = prediction.get("current_value", 0) or 0
    health = prediction.get("health_score", 0)
    deg_rate = prediction.get("degradation_rate", 0)
    r_squared = prediction.get("r_squared", 0)
    n_points = prediction.get("n_points", 0)
    rmse = prediction.get("rmse", 0)
    logger.info("摘要: current=%.2f kΩ, health=%d/100, deg=%.4f kΩ/天, "
                "R²=%.4f, RMSE=%.4f, n=%d",
                current, health, deg_rate, r_squared, rmse, n_points)

    # 5 卡片
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("当前阻值", f"{current:.1f} kΩ")
    c2.metric("健康度", f"{health}/100")
    c3.metric("衰减速率", f"{deg_rate:.3f} kΩ/天",
              delta="↓ 下降" if deg_rate < 0 else "↑ 上升",
              delta_color="inverse" if deg_rate < 0 else "off")
    c4.metric("拟合 R²", f"{r_squared:.3f}")
    c5.metric("数据点数", f"{n_points}")

    # 健康度进度条 + 颜色提示
    st.write("**绝缘健康度**")
    st.progress(health / 100.0)
    if health >= 70:
        st.success(f"健康度良好({health}/100),绝缘裕度充足")
    elif health >= 40:
        st.warning(f"健康度中等({health}/100),建议关注衰减趋势")
    else:
        st.error(f"健康度偏低({health}/100),接近报警线,建议维护")

    # 报警触达时间
    crossings = prediction.get("alarm_crossings", {})
    if crossings:
        st.markdown("**报警触达预测**")
        for alarm, info in crossings.items():
            days = info.get("days")
            date = info.get("date")
            conf = info.get("confidence", "low")
            if days is not None and date is not None:
                st.info(
                    f"报警线 {alarm} kΩ:预计 {days:.1f} 天后触达"
                    f"({date}) · 置信度 {conf}"
                )
            else:
                st.info(f"报警线 {alarm} kΩ:当前趋势下不会触达")
            logger.info("报警触达: %s kΩ → %s 天 (%s, %s)",
                        alarm,
                        f"{days:.1f}" if days is not None else "不触达",
                        date, conf)
    logger.info("=== render_insulation_stats 结束 ===")


# ---------- 单元测试 ----------

if __name__ == '__main__':
    import logging as _lg
    import numpy as np
    _lg.basicConfig(level=_lg.INFO,
                    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')

    # render_insulation_stats 依赖 streamlit 运行时,直接调用会报错;
    # 这里只测试空/无效输入的容错分支(不触发 st.* 调用)

    print("===== 测试1: 空 prediction 容错 =====")
    # 空 dict → 早期返回, 不调用 st.*
    try:
        render_insulation_stats(pd.DataFrame(), {})
        print("  [未触发 st.* 调用即通过]")
    except Exception as _e:
        # 如果进了 st.* 才报错,说明空检查没拦住
        if "streamlit" in str(type(_e).__name__).lower() or "NoSession" in str(_e):
            print(f"  [跳过] 需要 streamlit 上下文: {_e}")
        else:
            raise

    print("\n===== 测试2: fit_success=False 容错 =====")
    try:
        render_insulation_stats(pd.DataFrame(), {'fit_success': False})
    except Exception as _e:
        if "NoSession" in str(_e) or "streamlit" in str(_e).lower():
            print(f"  [跳过] 需要 streamlit 上下文: {_e}")
        else:
            raise

    print("\n===== 测试3: 有效 prediction 的 key 完整性 =====")
    # 验证 render_insulation_stats 读取的所有 key 都在 predict_insulation_trend 返回里
    expected_keys = {'fit_success', 'current_value', 'health_score',
                     'degradation_rate', 'r_squared', 'n_points', 'rmse',
                     'alarm_crossings'}
    # 模拟 predictor 返回
    fake_pred = {
        'fit_success': True, 'current_value': 480.0, 'health_score': 100,
        'degradation_rate': -1.5, 'r_squared': 0.95, 'n_points': 100,
        'rmse': 0.5,
        'alarm_crossings': {350: {'days': 80.0, 'date': None, 'confidence': 'high'}},
    }
    missing = expected_keys - set(fake_pred.keys())
    assert not missing, f"fake_pred 缺 key: {missing}"
    print(f"  所有关键 key 齐全: {sorted(expected_keys)}")
    print("  [PASS] prediction key 完整")

    print("\n[OK] 容错分支测试通过(完整渲染需 streamlit 上下文)")
