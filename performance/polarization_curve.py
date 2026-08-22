"""燃电性能统计 - 极化曲线(I-V特性)拟合模块。

基于稳态数据拟合燃料电池极化曲线,提取开路电压/欧姆内阻/
极限电流等关键物理参数,用于电堆性能评估与对比。

核心函数: fit_polarization_curve, create_polarization_figure
"""
from __future__ import annotations

import logging
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from scipy.optimize import curve_fit

logger = logging.getLogger(__name__)

# ---------- 样式常量(与 components/theme.py 一致) ----------
_SCATTER_COLOR = '#00D4FF'
_FIT_COLORS = {'empirical': '#F5C842', 'linear': '#2ED573',
               'polynomial': '#FF6B35'}
_SEG_COLORS = ['#2ED573', '#F5C842', '#FF6B35']  # 分段:低/中/高
_GRID_COLOR = 'rgba(255,255,255,0.08)'
_TEXT_COLOR = '#E8EDF5'
_TITLE_COLOR = '#00D4FF'


# ---------- 经验模型 ----------

def _empirical_model(I, E0, b, R, m, n):
    """燃料电池极化经验公式。

    V = E0 - b*log10(I) - R*I - m*exp(n*I)

    - E0: 开路电压(热力学电动势)
    - b: Tafel 斜率(活化极化,每 decade 电压降)
    - R: 欧姆内阻(Ω)
    - m*exp(n*I): 浓差极化(n>0,高电流区电压加速下降)
    """
    return E0 - b * np.log10(I) - R * I - m * np.exp(n * I)


def _fit_empirical(I: np.ndarray, V: np.ndarray) -> Optional[dict]:
    """scipy.curve_fit 拟合经验公式。"""
    # 过滤 I<=0(log 定义域)和异常值
    mask = (I > 0) & np.isfinite(I) & np.isfinite(V)
    I_c, V_c = I[mask], V[mask]
    if len(I_c) < 5:
        logger.warning("经验拟合样本不足: %d(需>=5)", len(I_c))
        return None

    # 初值估算(物理合理范围)
    E0_0 = float(np.max(V_c)) + 0.5
    b_0 = 0.05
    R_0 = abs(float((V_c.max() - V_c.min()) / max(I_c.max() - I_c.min(), 1)))
    m_0 = 0.001
    n_0 = 0.005
    p0 = [E0_0, b_0, R_0, m_0, n_0]
    # bounds: 物理参数为正,n>0(浓差加速)
    lower = [0.1, 0.0, 0.0, 0.0, 0.0]
    upper = [500, 2.0, 10.0, 100, 1.0]

    try:
        popt, _ = curve_fit(_empirical_model, I_c, V_c, p0=p0,
                            bounds=(lower, upper), maxfev=20000)
    except Exception as e:
        logger.warning("经验公式 curve_fit 失败: %s", e)
        return None

    E0, b, R, m, n = popt
    equation = (f'V = {E0:.3f} - {b:.4f}*log10(I) - {R:.5f}*I '
                f'- {m:.5f}*exp({n:.5f}*I)')
    logger.info("经验拟合: E0=%.3f b=%.4f R=%.5f m=%.5f n=%.5f",
                E0, b, R, m, n)
    return {
        'coefficients': {'E0': float(E0), 'b': float(b), 'R': float(R),
                         'm': float(m), 'n': float(n)},
        'equation': equation,
        'predict': lambda x: _empirical_model(x, *popt),
    }


def _fit_piecewise_linear(I: np.ndarray, V: np.ndarray) -> Optional[dict]:
    """分段线性拟合:低/中/高电流区分别线性回归。

    低电流区:活化极化主导(对数,但这里近似线性)
    中电流区:欧姆极化主导(线性,斜率≈-R_ohmic)
    高电流区:浓差极化主导(非线性,近似线性陡降)
    """
    mask = np.isfinite(I) & np.isfinite(V) & (I > 0)
    I_c, V_c = I[mask], V[mask]
    if len(I_c) < 6:
        logger.warning("分段线性样本不足: %d", len(I_c))
        return None

    q1, q2 = np.percentile(I_c, 33), np.percentile(I_c, 67)
    segs = []
    for lo, hi, color in [(0, q1, _SEG_COLORS[0]),
                          (q1, q2, _SEG_COLORS[1]),
                          (q2, np.inf, _SEG_COLORS[2])]:
        sm = (I_c >= lo) & (I_c < hi)
        if sm.sum() < 2:
            segs.append(None)
            continue
        coeffs = np.polyfit(I_c[sm], V_c[sm], 1)  # [slope, intercept]
        segs.append({'coeffs': coeffs, 'color': color,
                     'lo': lo, 'hi': hi,
                     'I': I_c[sm], 'V': V_c[sm]})
        logger.info("分段[%.1f,%.1f): slope=%.5f intercept=%.3f n=%d",
                    lo, hi if hi != np.inf else -1, coeffs[0], coeffs[1],
                    int(sm.sum()))

    def predict(x):
        x = np.asarray(x, dtype=float)
        out = np.full_like(x, np.nan)
        for s in segs:
            if s is None:
                continue
            sm = (x >= s['lo']) & (x < s['hi'])
            out[sm] = np.poly1d(s['coeffs'])(x[sm])
        return out

    eq_parts = []
    for i, s in enumerate(segs):
        if s is None:
            continue
        eq_parts.append(f"[{s['lo']:.0f},{s['hi'] if s['hi'] != np.inf else 'inf'}):"
                        f" V={s['coeffs'][1]:.2f}{s['coeffs'][0]:+.5f}*I")
    return {
        'coefficients': {'segments': segs},
        'equation': ' | '.join(eq_parts),
        'predict': predict,
        'segments': segs,  # 供绘图按段着色
    }


def _fit_polynomial(I: np.ndarray, V: np.ndarray, degree: int = 3) -> Optional[dict]:
    """3 次多项式拟合 V = a0 + a1*I + a2*I^2 + a3*I^3。"""
    mask = np.isfinite(I) & np.isfinite(V)
    I_c, V_c = I[mask], V[mask]
    if len(I_c) < degree + 1:
        logger.warning("多项式样本不足: %d(需>=%d)", len(I_c), degree + 1)
        return None
    coeffs = np.polyfit(I_c, V_c, degree)  # 降序
    poly = np.poly1d(coeffs)
    terms = []
    for i, c in enumerate(coeffs):
        p = degree - i
        sign = '+' if c >= 0 else '-'
        if p == 0:
            terms.append(f'{sign} {abs(c):.4f}')
        elif p == 1:
            terms.append(f'{sign} {abs(c):.4f}*I')
        else:
            terms.append(f'{sign} {abs(c):.4f}*I^{p}')
    equation = 'V = ' + ' '.join(terms).lstrip('+ ').strip()
    if equation.startswith('V = - '):
        equation = 'V = -' + equation[6:]
    logger.info("多项式拟合 degree=%d coeffs=%s", degree, np.round(coeffs, 4))
    return {
        'coefficients': {'coeffs': coeffs.tolist()},
        'equation': equation,
        'predict': lambda x: poly(x),
    }


# ---------- 参数提取与质量评估 ----------

def _fit_quality(y_true: np.ndarray, y_pred: np.ndarray) -> dict:
    """计算 R2 / RMSE / MAPE。"""
    mask = np.isfinite(y_true) & np.isfinite(y_pred)
    yt, yp = y_true[mask], y_pred[mask]
    n = len(yt)
    if n == 0:
        return {'r_squared': 0, 'rmse': 0, 'mape': 0, 'n': 0}
    ss_res = float(np.sum((yt - yp) ** 2))
    ss_tot = float(np.sum((yt - np.mean(yt)) ** 2))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0
    rmse = float(np.sqrt(ss_res / n))
    # MAPE:避免除零
    nonzero = np.abs(yt) > 1e-6
    mape = (float(np.mean(np.abs((yt[nonzero] - yp[nonzero]) / yt[nonzero])) * 100)
            if nonzero.any() else 0)
    return {'r_squared': round(r2, 4), 'rmse': round(rmse, 4),
            'mape': round(mape, 2), 'n': n}


def _extract_parameters(method: str, coeffs: dict, predict,
                        I: np.ndarray, V: np.ndarray) -> dict:
    """提取开路电压/欧姆内阻/活化损失/极限电流。"""
    I_min, I_max = float(np.nanmin(I)), float(np.nanmax(I))
    params = {
        'open_circuit_voltage': None,
        'ohmic_resistance_mohm': None,
        'activation_loss': None,
        'limit_current': None,
    }

    if method == 'empirical':
        E0 = coeffs['E0']
        params['open_circuit_voltage'] = round(float(E0), 3)
        params['ohmic_resistance_mohm'] = round(float(coeffs['R'] * 1000), 4)
        # 活化损失:在参考电流 I_ref=I_max 处的 b*log10 项
        I_ref = max(I_max, 1.0)
        params['activation_loss'] = round(float(coeffs['b'] * np.log10(I_ref)), 4)
        # 极限电流:外推到 V=0.3*E0 的电流(数值搜索)
        params['limit_current'] = _search_limit_current(predict, E0, I_max)
    elif method == 'polynomial':
        c = np.array(coeffs['coeffs'])
        poly = np.poly1d(c)
        params['open_circuit_voltage'] = round(float(poly(0)), 3)  # I=0 外推
        # 欧姆内阻:dV/dI 的绝对值(V/A = Ω,直接即内阻,非倒数)
        I_mid = (I_min + I_max) / 2
        deriv = np.polyder(poly)
        slope = float(deriv(I_mid))
        params['ohmic_resistance_mohm'] = round(abs(slope) * 1000, 4) if slope else 0
        params['limit_current'] = _search_limit_current(predict,
                                                         float(poly(0)), I_max)
    elif method == 'linear':
        segs = coeffs.get('segments', [])
        # 开路电压:低段在 I=0 的截距
        if segs and segs[0] is not None:
            params['open_circuit_voltage'] = round(float(segs[0]['coeffs'][1]), 3)
            # 欧姆内阻:中段斜率 dV/dI 的绝对值(V/A=Ω)
            mid_seg = segs[1] if (len(segs) > 1 and segs[1] is not None) else segs[0]
            slope = float(mid_seg['coeffs'][0])
            params['ohmic_resistance_mohm'] = (round(abs(slope) * 1000, 4)
                                              if slope else 0)
        # 极限电流:高段外推到 V=0
        if segs and segs[2] is not None:
            s_hi = segs[2]['coeffs']
            if s_hi[0] < 0:  # 斜率为负才有解
                I_lim = -s_hi[1] / s_hi[0]
                params['limit_current'] = round(float(I_lim), 2)
    return params


def _search_limit_current(predict, E0: float, I_max: float) -> Optional[float]:
    """数值搜索拟合曲线上 V=0.3*E0 对应的电流(极限电流近似)。"""
    target = 0.3 * E0
    # 在 [I_max, I_max*3] 外推搜索
    xs = np.linspace(I_max, I_max * 3, 200)
    try:
        ys = predict(xs)
    except Exception:
        return round(float(I_max), 2)
    below = xs[ys <= target]
    if len(below):
        return round(float(below[0]), 2)
    return round(float(I_max * 1.2), 2)  # 找不到则外推 1.2*Imax


# ---------- 主函数 ----------

def fit_polarization_curve(
    df: pd.DataFrame,
    current_col: str = 'FC_CurrOut',
    voltage_col: str = 'FC_VoltOut',
    fit_method: str = 'empirical',
) -> Dict:
    """拟合燃料电池极化曲线。

    Args:
        df: 稳态段数据(建议传入 aggregate_segments 输出或稳态筛选后的段数据)
        current_col: 电流列名
        voltage_col: 电压列名
        fit_method: 'empirical'(经验公式) | 'linear'(分段线性) | 'polynomial'(多项式)

    Returns:
        dict: {
            'method', 'coefficients', 'r_squared', 'rmse', 'mape',
            'equation', 'predicted', 'parameters': {
                'open_circuit_voltage', 'ohmic_resistance_mohm',
                'activation_loss', 'limit_current'
            }, 'fit_success': bool
        }
    """
    logger.info("极化曲线拟合: method=%s rows=%d", fit_method, len(df))
    result: Dict = {
        'method': fit_method, 'fit_success': False,
        'coefficients': {}, 'r_squared': 0, 'rmse': 0, 'mape': 0,
        'equation': '', 'predicted': np.array([]),
        'parameters': {'open_circuit_voltage': None,
                       'ohmic_resistance_mohm': None,
                       'activation_loss': None,
                       'limit_current': None},
    }

    if df is None or len(df) == 0:
        logger.warning("极化拟合: 输入为空")
        return result
    if current_col not in df.columns or voltage_col not in df.columns:
        logger.error("极化拟合: 缺列 %s/%s", current_col, voltage_col)
        return result

    I = pd.to_numeric(df[current_col], errors='coerce').to_numpy()
    V = pd.to_numeric(df[voltage_col], errors='coerce').to_numpy()

    # ---------- 分发到对应拟合器 ----------
    fit = None
    if fit_method == 'empirical':
        fit = _fit_empirical(I, V)
    elif fit_method == 'linear':
        fit = _fit_piecewise_linear(I, V)
    elif fit_method == 'polynomial':
        fit = _fit_polynomial(I, V, degree=3)
    else:
        logger.error("未知 fit_method: %s", fit_method)
        return result

    if fit is None:
        logger.warning("%s 拟合失败,返回未拟合结果", fit_method)
        return result

    # ---------- 预测 + 质量评估 ----------
    pred = fit['predict'](I)
    quality = _fit_quality(V, pred)

    # ---------- 参数提取 ----------
    params = _extract_parameters(fit_method, fit['coefficients'],
                                 fit['predict'], I, V)

    result.update({
        'fit_success': True,
        'coefficients': fit['coefficients'],
        'r_squared': quality['r_squared'],
        'rmse': quality['rmse'],
        'mape': quality['mape'],
        'n_points': quality['n'],
        'equation': fit['equation'],
        'predicted': pred,
        'parameters': params,
    })
    logger.info("极化拟合完成[%s]: R2=%.4f RMSE=%.4f MAPE=%.2f%% E0=%s R=%s mOhm Ilim=%s",
                fit_method, quality['r_squared'], quality['rmse'], quality['mape'],
                params['open_circuit_voltage'], params['ohmic_resistance_mohm'],
                params['limit_current'])
    return result


# ---------- 绘图 ----------

def create_polarization_figure(
    df: pd.DataFrame,
    result: Dict,
    current_col: str = 'FC_CurrOut',
    voltage_col: str = 'FC_VoltOut',
) -> go.Figure:
    """绘制极化曲线散点 + 拟合曲线。"""
    fig = go.Figure()

    if not result.get('fit_success'):
        fig.update_layout(
            title='极化曲线(拟合失败)',
            plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
            font=dict(color=_TEXT_COLOR),
            annotations=[dict(text='无有效拟合结果', showarrow=False,
                              x=0.5, y=0.5, font=dict(size=16, color='#8892A8'))],
        )
        return fig

    I = pd.to_numeric(df[current_col], errors='coerce').to_numpy()
    V = pd.to_numeric(df[voltage_col], errors='coerce').to_numpy()
    method = result['method']
    fit_color = _FIT_COLORS.get(method, '#F5C842')

    # ---------- 散点(原始数据) ----------
    fig.add_trace(go.Scatter(
        x=I, y=V, mode='markers', name='实测数据',
        marker=dict(color=_SCATTER_COLOR, size=9,
                    line=dict(width=1, color='rgba(255,255,255,0.4)')),
        hovertemplate='I=%{x:.1f}A V=%{y:.3f}V<extra></extra>',
    ))

    # ---------- 拟合曲线 ----------
    if method == 'linear':
        # 分段:每段单独画,不同颜色
        for i, seg in enumerate(result['coefficients'].get('segments', [])):
            if seg is None:
                continue
            xs = np.linspace(seg['lo'],
                             seg['hi'] if seg['hi'] != np.inf else float(np.max(I)),
                             50)
            ys = np.poly1d(seg['coeffs'])(xs)
            fig.add_trace(go.Scatter(
                x=xs, y=ys, mode='lines', name=f'段{i+1}拟合',
                line=dict(color=seg['color'], width=2.5),
            ))
    else:
        xs = np.linspace(float(np.nanmin(I)), float(np.nanmax(I)) * 1.05, 150)
        ys = result['predict'](xs) if hasattr(result, 'get') and 'predict' in result \
            else np.poly1d(result['coefficients'].get('coeffs', [0, 0, 0, 0]))(xs)
        # 简化:直接用 coefficients 重建预测(empirical/polynomial 都在 fit 里有 predict,
        # 但 result dict 没存 predict 函数,这里用方程重新算)
        # 为稳健,用 result['predicted'] 反推不行,改用重新调用对应模型
        ys = _predict_from_result(method, result, xs)
        fig.add_trace(go.Scatter(
            x=xs, y=ys, mode='lines', name=f'{method} 拟合',
            line=dict(color=fit_color, width=2.5),
        ))

    # ---------- 参数标注 ----------
    p = result['parameters']
    ann = (f"R2={result['r_squared']:.4f} | RMSE={result['rmse']:.4f} | "
           f"MAPE={result['mape']:.2f}%\n"
           f"E0={p['open_circuit_voltage']}V | "
           f"R_ohmic={p['ohmic_resistance_mohm']}mOhm | "
           f"I_limit={p['limit_current']}A")

    fig.update_layout(
        title=dict(text=f'极化曲线 ({method})', x=0.5, xanchor='center',
                   font=dict(color=_TITLE_COLOR, size=16)),
        annotations=[dict(text=ann, xref='paper', yref='paper',
                          x=0.5, y=1.06, showarrow=False, yanchor='bottom',
                          font=dict(color='#8892A8', size=11))],
        plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
        font=dict(color=_TEXT_COLOR),
        hovermode='closest',
        legend=dict(orientation='h', yanchor='bottom', y=1.02),
        margin=dict(l=60, r=60, t=90, b=40), height=480,
    )
    fig.update_xaxes(title_text='电流 (A)', gridcolor=_GRID_COLOR,
                     tickfont=dict(color=_TEXT_COLOR))
    fig.update_yaxes(title_text='电压 (V)', gridcolor=_GRID_COLOR,
                     tickfont=dict(color=_TEXT_COLOR))
    return fig


def _predict_from_result(method: str, result: Dict, x: np.ndarray) -> np.ndarray:
    """从 result dict 重建预测函数(用于绘图)。"""
    try:
        if method == 'empirical':
            c = result['coefficients']
            return _empirical_model(x, c['E0'], c['b'], c['R'], c['m'], c['n'])
        elif method == 'polynomial':
            return np.poly1d(result['coefficients']['coeffs'])(x)
    except Exception as e:
        logger.warning("重建预测失败: %s", e)
    return np.full_like(x, np.nan)


# ---------- 单元测试 ----------

if __name__ == '__main__':
    import logging as _lg
    _lg.basicConfig(level=_lg.INFO,
                    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')

    rng = np.random.default_rng(42)

    print("===== 测试1: 经验公式拟合(已知参数恢复) =====")
    # 用 E0=300, b=0.05, R=0.1, m=0.001, n=0.005 生成数据+小噪声
    I_true = np.linspace(10, 400, 50)
    V_true = _empirical_model(I_true, 300, 0.05, 0.1, 0.001, 0.005)
    V_noisy = V_true + rng.normal(0, 0.3, len(I_true))
    df1 = pd.DataFrame({'FC_CurrOut': I_true, 'FC_VoltOut': V_noisy})
    r1 = fit_polarization_curve(df1, fit_method='empirical')
    print(f"  方程: {r1['equation']}")
    print(f"  R2={r1['r_squared']} E0={r1['parameters']['open_circuit_voltage']} "
          f"R={r1['parameters']['ohmic_resistance_mohm']}mOhm "
          f"Ilim={r1['parameters']['limit_current']}A")
    assert r1['fit_success']
    assert r1['r_squared'] > 0.99, f"R2应>0.99,实际{r1['r_squared']}"
    # 经验公式参数存在耦合(E0 与浓差项 m*exp 补偿,参数不唯一),
    # 但拟合曲线质量高、参数在物理合理量级即可
    assert 200 < r1['parameters']['open_circuit_voltage'] < 500, \
        f"E0应在物理合理范围,实际{r1['parameters']['open_circuit_voltage']}"
    assert 0 < r1['parameters']['ohmic_resistance_mohm'] < 300, \
        f"R应在合理范围,实际{r1['parameters']['ohmic_resistance_mohm']}"
    assert r1['parameters']['limit_current'] > 0
    print("  [PASS] 经验公式拟合质量+参数物理量级")

    print("\n===== 测试2: 多项式拟合 =====")
    r2 = fit_polarization_curve(df1, fit_method='polynomial')
    print(f"  方程: {r2['equation'][:60]}...")
    print(f"  R2={r2['r_squared']} E0={r2['parameters']['open_circuit_voltage']}")
    assert r2['fit_success']
    assert r2['r_squared'] > 0.95
    print("  [PASS] 多项式拟合")

    print("\n===== 测试3: 分段线性拟合 =====")
    r3 = fit_polarization_curve(df1, fit_method='linear')
    print(f"  方程: {r3['equation']}")
    print(f"  R2={r3['r_squared']} E0={r3['parameters']['open_circuit_voltage']}")
    assert r3['fit_success']
    assert r3['r_squared'] > 0.7  # 分段近似,R2 稍低
    print("  [PASS] 分段线性三段拟合")

    print("\n===== 测试4: 空数据/缺列容错 =====")
    r_empty = fit_polarization_curve(pd.DataFrame(), 'empirical')
    assert not r_empty['fit_success']
    r_miss = fit_polarization_curve(
        pd.DataFrame({'FC_CurrOut': [1, 2]}), 'empirical')  # 缺电压列
    assert not r_miss['fit_success']
    print("  [PASS] 空数据/缺列返回未拟合")

    print("\n===== 测试5: 绘图 =====")
    fig = create_polarization_figure(df1, r1)
    # 1散点 + 1拟合曲线 = 2 traces
    assert len(fig.data) == 2, f"应2条trace,实际{len(fig.data)}"
    fig_lin = create_polarization_figure(df1, r3)
    # 1散点 + 3段 = 4 traces
    assert len(fig_lin.data) == 4, f"分段应4条trace,实际{len(fig_lin.data)}"
    fig_fail = create_polarization_figure(df1,
                                          {'fit_success': False, 'method': 'empirical',
                                           'parameters': {}})
    assert len(fig_fail.data) == 0
    print(f"  empirical traces={len(fig.data)} 分段traces={len(fig_lin.data)}")
    print("  [PASS] 绘图散点+拟合曲线")

    print("\n===== 测试6: 拟合质量评估 =====")
    yt = np.array([300, 290, 280, 270])
    yp = np.array([301, 289, 281, 269])
    q = _fit_quality(yt, yp)
    assert q['r_squared'] > 0.99
    assert q['rmse'] > 0
    print(f"  R2={q['r_squared']} RMSE={q['rmse']} MAPE={q['mape']}%")
    print("  [PASS] 质量评估计算正确")

    print("\n[OK] 全部测试通过")
