"""绝缘阻值趋势预测模块:多项式拟合 + 报警触达时间预测。

输入 process_insulation_data 的输出(10分钟窗口最小值序列),
拟合绝缘阻值随时间的衰减趋势,预测触达报警线(350/250 kΩ)的时间。

核心函数: predict_insulation_trend
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Dict, List

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def predict_insulation_trend(
    df: pd.DataFrame,
    alarm_values: List[float] = [350, 250],
    predict_days: int = 30,
    poly_order: int = 1,
) -> Dict:
    """拟合绝缘阻值趋势并预测报警触达时间。

    Args:
        df: process_insulation_data 的输出,需含 timestamp/FC_VehicleIsolationR
        alarm_values: 报警阈值列表(默认[350, 250] kΩ)
        predict_days: 向前预测天数(默认30)
        poly_order: 多项式阶数(默认1=线性)

    Returns:
        dict: fit_coefficients/r_squared/rmse/predictions(DataFrame)/
        alarm_crossings/degradation_rate/health_score/current_value/
        n_points/fit_success
    """
    logger.info("=== 绝缘趋势预测开始 ===")
    logger.info("输入: %d 行, alarm_values=%s, predict_days=%d, poly_order=%d",
                len(df) if df is not None else 0, alarm_values, predict_days, poly_order)

    def _empty_result(reason: str) -> Dict:
        logger.warning("拟合失败: %s", reason)
        return {
            'fit_coefficients': [],
            'r_squared': 0.0,
            'rmse': 0.0,
            'predictions': pd.DataFrame(columns=['timestamp', 'value', 'lower', 'upper', 'band']),
            'alarm_crossings': {a: {'days': None, 'date': None, 'confidence': 'low'} for a in alarm_values},
            'degradation_rate': 0.0,
            'health_score': 0,
            'current_value': None,
            'n_points': 0,
            'fit_success': False,
        }

    # 边界1: 空数据
    if df is None or len(df) == 0:
        return _empty_result("输入数据为空")
    # 边界2: 缺列
    for col in ['timestamp', 'FC_VehicleIsolationR']:
        if col not in df.columns:
            return _empty_result(f"缺列: {col}")

    # 步骤1: 提取 X(相对天数)/Y(绝缘值)
    work = df.copy()
    work['timestamp'] = pd.to_datetime(work['timestamp'], errors='coerce')
    work = work.dropna(subset=['timestamp', 'FC_VehicleIsolationR'])
    n_points = len(work)
    logger.info("步骤1 数据提取: 有效点 %d (清洗前 %d)", n_points, len(df))
    if n_points < 2:
        return _empty_result(f"有效点不足({n_points} < 2)")

    # X = 距首点的相对天数(浮点, 10分钟粒度也能体现变化, 避免 toordinal 同天塌缩)
    t0 = pd.Timestamp(work['timestamp'].iloc[0])
    ts_list = [pd.Timestamp(t) for t in work['timestamp']]
    X = np.array([(t - t0).total_seconds() / 86400.0 for t in ts_list], dtype=float)
    Y = work['FC_VehicleIsolationR'].to_numpy(dtype=float)
    logger.info("  X 范围: [0, %.4f] 天, Y 范围: [%.2f, %.2f] kΩ",
                float(X.max()), float(Y.min()), float(Y.max()))

    # 边界3: 点数 < order+1
    if n_points < poly_order + 1:
        return _empty_result(f"点数 {n_points} 不足以拟合 {poly_order} 阶多项式(需 ≥ {poly_order + 1})")

    # 步骤2: polyfit(高阶在前), cov 降级保护
    has_cov = False
    cov = None
    try:
        with np.errstate(all='ignore'):
            coeff, cov = np.polyfit(X, Y, poly_order, cov=True)
        has_cov = True
    except (np.linalg.LinAlgError, ValueError) as e:
        logger.warning("polyfit cov=True 失败, 降级无 cov: %s", e)
        try:
            with np.errstate(all='ignore'):
                coeff = np.polyfit(X, Y, poly_order)
        except (np.linalg.LinAlgError, ValueError) as e2:
            return _empty_result(f"polyfit 求解失败: {e2}")
    logger.info("步骤2 polyfit: order=%d, coefficients=%s, has_cov=%s",
                poly_order, [round(float(c), 6) for c in coeff], has_cov)

    # 步骤3: R² / RMSE
    Y_pred = np.polyval(coeff, X)
    residuals = Y - Y_pred
    ss_res = float(np.sum(residuals ** 2))
    ss_tot = float(np.sum((Y - Y.mean()) ** 2))
    r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    rmse = float(np.sqrt(np.mean(residuals ** 2))) if n_points > 0 else 0.0
    logger.info("步骤3 拟合质量: R²=%.4f, RMSE=%.4f kΩ, SS_res=%.4f, SS_tot=%.4f",
                r_squared, rmse, ss_res, ss_tot)

    # 步骤4: 预测序列(延伸到 X[-1] + predict_days, 100 点)
    X_pred = np.linspace(float(X[0]), float(X[-1]) + predict_days, 100)
    Y_pred_future = np.polyval(coeff, X_pred)
    logger.info("步骤4 预测生成: predict_days=%d, %d 个点, 范围 [%.4f, %.4f] 天",
                predict_days, len(X_pred), float(X_pred[0]), float(X_pred[-1]))

    # 步骤5: 95% CI(协方差矩阵 × 设计矩阵 → 标准误差)
    if has_cov and cov is not None:
        try:
            X_pred_design = np.vander(X_pred, N=poly_order + 1)
            var_pred = np.sum((X_pred_design @ cov) * X_pred_design, axis=1)
            se_pred = np.sqrt(np.maximum(var_pred, 0.0))
            upper = Y_pred_future + 1.96 * se_pred
            lower = Y_pred_future - 1.96 * se_pred
            logger.info("步骤5 95%%CI: 均值 σ_pred=%.4f, CI 宽度均值=%.4f kΩ",
                        float(np.mean(se_pred)), float(np.mean(upper - lower)))
        except Exception as e:
            logger.warning("步骤5 CI 计算失败, 改用固定宽度: %s", e)
            sigma = float(np.std(residuals)) if len(residuals) > 0 else 0.0
            upper = Y_pred_future + 1.96 * sigma
            lower = Y_pred_future - 1.96 * sigma
    else:
        sigma = float(np.std(residuals)) if len(residuals) > 0 else 0.0
        upper = Y_pred_future + 1.96 * sigma
        lower = Y_pred_future - 1.96 * sigma
        logger.info("步骤5 95%%CI(固定宽度): σ=%.4f", sigma)

    # 还原到真实日期(t0 + 相对天数)
    ts_pred = [t0 + pd.Timedelta(days=float(x)) for x in X_pred]
    band = np.where((Y_pred_future >= np.asarray(lower, dtype=float)) &
                    (Y_pred_future <= np.asarray(upper, dtype=float)),
                    'in', 'out')
    predictions = pd.DataFrame({
        'timestamp': ts_pred,
        'value': Y_pred_future,
        'lower': lower,
        'upper': upper,
        'band': band,
    })

    # 步骤6: 报警触达时间(多项式 = alarm 的根)
    alarm_crossings: Dict = {}
    X_last = float(X[-1])
    for alarm in alarm_values:
        logger.info("步骤6 报警 %s kΩ 求根开始", alarm)
        # 求 poly(x) - alarm = 0 → 末项(常数)减 alarm
        coeff_shifted = coeff.copy()
        coeff_shifted[-1] = coeff_shifted[-1] - alarm
        roots = np.roots(coeff_shifted)
        logger.info("  所有根: %s",
                    [round(float(r.real), 4) + round(float(r.imag), 4) * 1j for r in roots])
        # 选实根(|imag|<1e-6)且 > X_last 的最近根
        real_roots = [float(r.real) for r in roots
                      if abs(float(r.imag)) < 1e-6 and float(r.real) > X_last]
        if real_roots:
            chosen = min(real_roots)
            days_to_alarm = chosen - X_last
            alarm_date = (t0 + pd.Timedelta(days=float(chosen))).date()
            if r_squared > 0.8:
                conf = 'high'
            elif r_squared > 0.5:
                conf = 'medium'
            else:
                conf = 'low'
            alarm_crossings[alarm] = {
                'days': float(days_to_alarm),
                'date': alarm_date,
                'confidence': conf,
            }
            logger.info("  选中根: x=%.4f (距今 %.2f 天), 日期 %s, 置信度 %s",
                        chosen, days_to_alarm, alarm_date, conf)
        else:
            alarm_crossings[alarm] = {'days': None, 'date': None, 'confidence': 'low'}
            logger.info("  无满足条件的实根(>X_last), 报警 %s kΩ 不会触达", alarm)

    # 步骤7: 整体置信度
    if r_squared > 0.8:
        confidence = 'high'
    elif r_squared > 0.5:
        confidence = 'medium'
    else:
        confidence = 'low'
    logger.info("步骤7 整体置信度: %s (R²=%.4f)", confidence, r_squared)

    # 步骤8: 健康度 score=min(100, (current-250)/(350-250)*100), <250 为 0
    current_value = float(Y[-1])
    if current_value >= 250:
        health_score = min(100, int(round((current_value - 250) / (350 - 250) * 100)))
    else:
        health_score = 0
    logger.info("步骤8 健康度: current=%.2f kΩ, health_score=%d/100",
                current_value, health_score)

    # 衰减速率: 一阶系数(线性斜率) kΩ/天
    if poly_order >= 1 and len(coeff) >= 2:
        degradation_rate = float(coeff[-2])
    else:
        degradation_rate = 0.0
    logger.info("衰减速率: %.4f kΩ/天", degradation_rate)

    result = {
        'fit_coefficients': [float(c) for c in coeff],
        'r_squared': float(r_squared),
        'rmse': float(rmse),
        'predictions': predictions,
        'alarm_crossings': alarm_crossings,
        'degradation_rate': degradation_rate,
        'health_score': health_score,
        'current_value': current_value,
        'n_points': n_points,
        'fit_success': True,
    }
    logger.info("=== 绝缘趋势预测结束 (R²=%.4f, health=%d, n=%d) ===",
                r_squared, health_score, n_points)
    return result


# ---------- 单元测试 ----------

if __name__ == '__main__':
    import logging as _lg
    _lg.basicConfig(level=_lg.INFO,
                    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')

    rng = np.random.default_rng(42)

    def _make_insul_df(n=50, base=500, slope=-2.0, noise=10, start='2026-01-01'):
        """生成模拟绝缘数据(线性衰减, 10分钟一个点)。"""
        ts = [pd.Timestamp(start) + pd.Timedelta(hours=1 * i) for i in range(n)]
        days_offset = np.array([i / 24.0 for i in range(n)])  # 1小时=1/24天
        values = base + slope * days_offset + rng.normal(0, noise, n)
        values = np.maximum(values, 0)
        return pd.DataFrame({
            'timestamp': ts,
            'FC_VehicleIsolationR': values,
            'FC_MainSts': 4,
            'is_running': True,
        })

    print("\n===== 测试1: 线性衰减拟合 (50点, slope=-2 kΩ/天) =====")
    df1 = _make_insul_df(n=200, base=500, slope=-2.0, noise=0.5)
    r1 = predict_insulation_trend(df1, alarm_values=[350, 250], predict_days=30, poly_order=1)
    assert r1['fit_success']
    assert r1['n_points'] == 200
    print(f"  coeff={r1['fit_coefficients']}, R^2={r1['r_squared']:.4f}, RMSE={r1['rmse']:.4f}")
    print(f"  deg_rate={r1['degradation_rate']:.4f}, health={r1['health_score']}, current={r1['current_value']:.2f}")
    assert r1['r_squared'] > 0.95, f"R^2 应 >0.95, 实际 {r1['r_squared']}"
    assert abs(r1['degradation_rate'] - (-2.0)) < 0.5, f"斜率应≈-2, 实际 {r1['degradation_rate']}"
    assert r1['health_score'] == 100, f"当前≈500 应=100, 实际 {r1['health_score']}"
    print("  [PASS] 线性拟合 + R^2/斜率/健康度正确")

    print("\n===== 测试2: 报警触达预测 (350 kΩ) =====")
    df2 = _make_insul_df(n=50, base=500, slope=-2.0, noise=0.1, start='2026-01-01')
    r2 = predict_insulation_trend(df2, alarm_values=[350], predict_days=200, poly_order=1)
    cross = r2['alarm_crossings'][350]
    print(f"  350kΩ: days={cross['days']}, date={cross['date']}, conf={cross['confidence']}")
    assert cross['days'] is not None
    # 50点×10分钟≈0.35天, 衰减0.7kΩ, 当前≈499.3, 触达350需(499.3-350)/2≈74.6天
    assert 70 < cross['days'] < 80, f"触达应在~75天, 实际 {cross['days']}"
    assert cross['confidence'] == 'high'
    print("  [PASS] 350 kΩ 触达时间预测正确")

    print("\n===== 测试3: 边界-空数据 =====")
    r3 = predict_insulation_trend(pd.DataFrame())
    assert not r3['fit_success']
    assert r3['health_score'] == 0
    print("  [PASS] 空数据返回 fit_success=False")

    print("\n===== 测试4: 边界-缺列 =====")
    r4 = predict_insulation_trend(pd.DataFrame({'ts': [], 'val': []}))
    assert not r4['fit_success']
    print("  [PASS] 缺列返回 fit_success=False")

    print("\n===== 测试5: 边界-点数不足 (n<order+1) =====")
    df5 = _make_insul_df(n=1, base=500, slope=-2.0)
    r5 = predict_insulation_trend(df5, poly_order=2)
    assert not r5['fit_success']
    print("  [PASS] 点数不足返回 fit_success=False")

    print("\n===== 测试6: 二阶多项式拟合 =====")
    n = 200
    ts = [pd.Timestamp('2026-01-01') + pd.Timedelta(hours=1 * i) for i in range(n)]
    days = np.array([i / 24.0 for i in range(n)])
    y = 600 - 3 * days - 0.1 * days ** 2 + rng.normal(0, 1, n)
    df6 = pd.DataFrame({
        'timestamp': ts,
        'FC_VehicleIsolationR': y,
        'FC_MainSts': 4,
        'is_running': True,
    })
    r6 = predict_insulation_trend(df6, predict_days=30, poly_order=2)
    print(f"  二阶 R^2={r6['r_squared']:.4f}, coeff={r6['fit_coefficients']}")
    assert r6['fit_success']
    assert r6['r_squared'] > 0.9
    print("  [PASS] 二阶拟合成功")

    print("\n所有测试通过 [OK]")
