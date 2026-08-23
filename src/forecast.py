"""趋势预测模块:基于历史数据预测 7 个关键指标走势。

预测目标:
1. 单片电压压差(劣化趋势)
2. 累计氢耗(续能估计)
3. 故障码频率(故障率趋势)
4. 净功率衰减(电堆衰减)
5. 绝缘电阻趋势(预测触达报警线时间)
6. 平均单体电压衰减(电堆健康度)
7. 离均差趋势(单体一致性劣化)

实现方式: 用 numpy + sklearn 轻量实现,不依赖 statsmodels:
- 线性回归(长期趋势)
- 移动平均(短期平滑)
- 滑动窗口计数(故障频率)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from src.log_config import get_logger
from src.metrics import _safe_num

logger = get_logger(__name__)


@dataclass
class ForecastResult:
    """单个指标的预测结果。"""
    metric_name: str  # 指标名
    history_x: np.ndarray  # 历史时间(数值化,小时)
    history_y: np.ndarray  # 历史值
    future_x: np.ndarray  # 预测时间(数值化,小时)
    future_y: np.ndarray  # 预测值
    confidence_low: np.ndarray  # 95% 置信下界
    confidence_high: np.ndarray  # 95% 置信上界
    slope: float  # 趋势斜率(单位/小时)
    r2: float  # 拟合优度
    interpretation: str  # 趋势解读文字
    extra: dict = field(default_factory=dict)  # 额外信息


def _to_hours(ts: pd.Series) -> np.ndarray:
    """把 Timestamp 序列转为以小时为单位的数值(从首时刻起算)。"""
    ts = pd.to_datetime(ts, errors="coerce").dropna()
    if len(ts) == 0:
        return np.array([])
    deltas = (ts - ts.iloc[0]).dt.total_seconds() / 3600
    return deltas.to_numpy()


def _linear_fit(x: np.ndarray, y: np.ndarray) -> tuple[float, float, float]:
    """简单线性回归 y = a*x + b,返回 (a, b, r2)。"""
    if len(x) < 2:
        return 0.0, float(y.mean()) if len(y) else 0.0, 0.0
    a, b = np.polyfit(x, y, 1)
    y_pred = a * x + b
    ss_res = np.sum((y - y_pred) ** 2)
    ss_tot = np.sum((y - y.mean()) ** 2)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return float(a), float(b), float(r2)


def _interpret_trend(slope_per_hour: float, slope_per_day: float,
                     metric_name: str, ascending_bad: bool = True) -> str:
    """生成趋势解读文字。

    Args:
        slope_per_hour: 每小时变化量
        slope_per_day: 每天变化量(便于理解)
        metric_name: 指标名
        ascending_bad: True=指标变大算劣化(如压差),False=指标变小算劣化(如功率)
    """
    if abs(slope_per_day) < 1e-6:
        return f"{metric_name}基本稳定,无明显趋势"

    direction = "上升" if slope_per_day > 0 else "下降"
    bad = (slope_per_day > 0) if ascending_bad else (slope_per_day < 0)
    severity = "劣化" if bad else "改善"

    return (f"{metric_name}呈{direction}趋势(每天变化 {slope_per_day:+.4f}),"
            f"属{severity}趋势。线性拟合 R²较高时可信,需结合采样窗口判断")


def forecast_cell_diff(df: pd.DataFrame, future_hours: float) -> ForecastResult | None:
    """预测单片电压压差走势。"""
    logger.info("[预测:压差] 输入 rows=%d, future_h=%s", len(df), future_hours)
    if "FC_MaxCellVoltage" not in df.columns or "FC_MinCellVoltage" not in df.columns:
        logger.warning("[预测:压差] 缺列 FC_MaxCellVoltage/FC_MinCellVoltage, 跳过")
        return None
    if "Timestamp" not in df.columns:
        logger.warning("[预测:压差] 缺列 Timestamp, 跳过")
        return None

    mx = _safe_num(df["FC_MaxCellVoltage"])
    mn = _safe_num(df["FC_MinCellVoltage"])
    ok = (mx > 0) & (mx < 2000) & (mn > 0) & (mn < 2000)
    diff = (mx - mn)[ok]
    diff = diff[(diff > 0) & (diff < 50)].dropna()
    ts = df["Timestamp"][ok][diff.index]
    logger.info("[预测:压差] 过滤后: 原始=%d → 有效=%d (范围 %.1f~%.1f mV)",
                len(mx), len(diff), diff.min() if len(diff) else 0,
                diff.max() if len(diff) else 0)

    if len(diff) < 10:
        logger.warning("[预测:压差] 过滤后样本不足(%d<10), 无法预测", len(diff))
        return None

    # 按小时分箱,取每小时压差均值,降低噪声
    hours = _to_hours(ts)
    if len(hours) == 0:
        return None
    df_bin = pd.DataFrame({"h": hours, "v": diff.to_numpy()})
    df_bin = df_bin.groupby(pd.cut(df_bin["h"], bins=np.arange(
        0, df_bin["h"].max() + 1, 1.0))).mean().dropna()
    x = df_bin["h"].to_numpy()
    y = df_bin["v"].to_numpy()

    if len(x) < 3:
        logger.warning("[预测:压差] 分箱后样本不足(%d<3)", len(x))
        return None

    a, b, r2 = _linear_fit(x, y)
    future_x = np.linspace(x.max(), x.max() + future_hours, 50)
    future_y = a * future_x + b
    resid_std = float(np.std(y - (a * x + b))) if len(y) > 2 else 1.0
    conf_low = future_y - 1.96 * resid_std
    conf_high = future_y + 1.96 * resid_std
    logger.info("[预测:压差] 拟合: a=%.6f/h b=%.2f r²=%.3f bins=%d y_range=%.1f~%.1f",
                a, b, r2, len(x), y.min(), y.max())

    return ForecastResult(
        metric_name="单片电压压差(mV)",
        history_x=x, history_y=y,
        future_x=future_x, future_y=future_y,
        confidence_low=conf_low, confidence_high=conf_high,
        slope=a, r2=r2,
        interpretation=_interpret_trend(
            a, a * 24, "压差", ascending_bad=True),
        extra={"resid_std": resid_std, "bins": len(x)},
    )


def forecast_hydrogen_cumulative(df: pd.DataFrame,
                                  future_hours: float) -> ForecastResult | None:
    """预测累计氢耗走势(基于瞬时氢耗积分)。"""
    logger.info("[预测:氢耗] 输入 rows=%d, future_h=%s", len(df), future_hours)
    if "FC_HydCmInstts" not in df.columns or "Timestamp" not in df.columns:
        logger.warning("[预测:氢耗] 缺列 FC_HydCmInstts/Timestamp, 跳过")
        return None

    hi = _safe_num(df["FC_HydCmInstts"])
    hi_raw = len(hi)
    hi = hi[hi >= 0].dropna()
    ts = df["Timestamp"].loc[hi.index]
    logger.info("[预测:氢耗] 过滤后: 原始=%d → 有效=%d (mean=%.2f)",
                hi_raw, len(hi), hi.mean() if len(hi) else 0)

    if len(hi) < 10:
        logger.warning("[预测:氢耗] 样本不足(%d<10)", len(hi))
        return None

    hours = _to_hours(ts)
    if len(hours) == 0:
        return None

    if len(hours) > 1:
        dt = np.diff(hours, prepend=0)
    else:
        dt = np.array([1.0])
    cum_h2 = np.cumsum(hi.to_numpy() * dt)

    df_bin = pd.DataFrame({"h": hours, "v": cum_h2})
    df_bin = df_bin.groupby(pd.cut(df_bin["h"], bins=np.arange(
        0, df_bin["h"].max() + 1, 1.0))).max().dropna()
    x = df_bin["h"].to_numpy()
    y = df_bin["v"].to_numpy()

    if len(x) < 3:
        logger.warning("[预测:氢耗] 分箱后样本不足(%d<3)", len(x))
        return None

    a, b, r2 = _linear_fit(x, y)
    future_x = np.linspace(x.max(), x.max() + future_hours, 50)
    future_y = a * future_x + b
    resid_std = float(np.std(y - (a * x + b))) if len(y) > 2 else 1.0
    logger.info("[预测:氢耗] 拟合: a=%.6f/h b=%.2f r²=%.3f bins=%d cum_range=%.1f~%.1f",
                a, b, r2, len(x), y.min(), y.max())

    return ForecastResult(
        metric_name="累计氢耗(kg)",
        history_x=x, history_y=y,
        future_x=future_x, future_y=future_y,
        confidence_low=future_y - 1.96 * resid_std,
        confidence_high=future_y + 1.96 * resid_std,
        slope=a, r2=r2,
        interpretation=_interpret_trend(
            a, a * 24, "累计氢耗", ascending_bad=False),
        extra={"resid_std": resid_std, "bins": len(x)},
    )


def forecast_fault_frequency(df: pd.DataFrame,
                              future_hours: float) -> ForecastResult | None:
    """预测故障码出现频率(每小时故障数)。"""
    logger.info("[预测:故障] 输入 rows=%d, future_h=%s", len(df), future_hours)
    if "FC_ErrorCode" not in df.columns or "Timestamp" not in df.columns:
        logger.warning("[预测:故障] 缺列 FC_ErrorCode/Timestamp, 跳过")
        return None

    ec = _safe_num(df["FC_ErrorCode"])
    mask = ec > 0
    ts_fault = df["Timestamp"].loc[mask[mask.fillna(False)].index]
    logger.info("[预测:故障] 故障样本: 总行=%d → 故障行=%d", len(df), len(ts_fault))

    if len(ts_fault) < 5:
        logger.warning("[预测:故障] 故障样本不足(%d<5)", len(ts_fault))
        return None

    hours = _to_hours(ts_fault)
    if len(hours) == 0:
        return None

    bins = np.arange(0, max(hours.max(), 1) + 2, 1.0)
    hist_counts, _ = np.histogram(hours, bins=bins)
    nonzero_mask = hist_counts > 0
    logger.info("[预测:故障] 分箱: 总小时=%d, 有故障小时=%d, max频次=%d",
                len(hist_counts), nonzero_mask.sum(),
                hist_counts.max() if len(hist_counts) else 0)
    if nonzero_mask.sum() < 3:
        logger.warning("[预测:故障] 分布过稀疏(只有 %d 小时有故障), 无法拟合",
                        nonzero_mask.sum())
        return None
    bin_centers = (bins[:-1] + bins[1:]) / 2
    x = bin_centers[nonzero_mask]
    y = hist_counts[nonzero_mask]

    if len(x) < 3:
        return None

    a, b, r2 = _linear_fit(x, y)
    future_x = np.linspace(x.max(), x.max() + future_hours, 50)
    future_y = np.maximum(a * future_x + b, 0)
    resid_std = float(np.std(y - (a * x + b))) if len(y) > 2 else 1.0
    logger.info("[预测:故障] 拟合: a=%.6f/h b=%.2f r²=%.3f bins=%d y_range=%.0f~%.0f",
                a, b, r2, len(x), y.min(), y.max())

    return ForecastResult(
        metric_name="故障码频率(次/小时)",
        history_x=x, history_y=y,
        future_x=future_x, future_y=future_y,
        confidence_low=np.maximum(future_y - 1.96 * resid_std, 0),
        confidence_high=future_y + 1.96 * resid_std,
        slope=a, r2=r2,
        interpretation=_interpret_trend(
            a, a * 24, "故障频率", ascending_bad=True),
        extra={"resid_std": resid_std, "bins": len(x)},
    )


def forecast_net_power(df: pd.DataFrame,
                        future_hours: float) -> ForecastResult | None:
    """预测净输出功率走势(判断电堆衰减)。"""
    logger.info("[预测:净功率] 输入 rows=%d, future_h=%s", len(df), future_hours)
    if "FC_NetPwrOut" not in df.columns or "Timestamp" not in df.columns:
        logger.warning("[预测:净功率] 缺列 FC_NetPwrOut/Timestamp, 跳过")
        return None

    p = _safe_num(df["FC_NetPwrOut"])
    p_raw = len(p)
    p = p[(p > 0) & (p < 100000)].dropna()
    ts = df["Timestamp"].loc[p.index]
    logger.info("[预测:净功率] 过滤后: 原始=%d → 有效=%d (mean=%.2f kW)",
                p_raw, len(p), p.mean() if len(p) else 0)

    if len(p) < 10:
        logger.warning("[预测:净功率] 样本不足(%d<10)", len(p))
        return None

    hours = _to_hours(ts)
    if len(hours) == 0:
        return None

    df_bin = pd.DataFrame({"h": hours, "v": p.to_numpy()})
    df_bin = df_bin.groupby(pd.cut(df_bin["h"], bins=np.arange(
        0, df_bin["h"].max() + 1, 1.0))).mean().dropna()
    x = df_bin["h"].to_numpy()
    y = df_bin["v"].to_numpy()

    if len(x) < 3:
        logger.warning("[预测:净功率] 分箱后样本不足(%d<3)", len(x))
        return None

    a, b, r2 = _linear_fit(x, y)
    future_x = np.linspace(x.max(), x.max() + future_hours, 50)
    future_y = np.maximum(a * future_x + b, 0)
    resid_std = float(np.std(y - (a * x + b))) if len(y) > 2 else 1.0
    logger.info("[预测:净功率] 拟合: a=%.6f/h b=%.2f r²=%.3f bins=%d y_range=%.1f~%.1f",
                a, b, r2, len(x), y.min(), y.max())

    return ForecastResult(
        metric_name="净输出功率(kW)",
        history_x=x, history_y=y,
        future_x=future_x, future_y=future_y,
        confidence_low=np.maximum(future_y - 1.96 * resid_std, 0),
        confidence_high=future_y + 1.96 * resid_std,
        slope=a, r2=r2,
        interpretation=_interpret_trend(
            a, a * 24, "净功率", ascending_bad=False),
        extra={"resid_std": resid_std, "bins": len(x)},
    )


def forecast_insulation(df: pd.DataFrame,
                        future_hours: float) -> ForecastResult | None:
    """预测绝缘电阻趋势,计算触达 350kΩ/250kΩ 报警线的时间。"""
    logger.info("[预测:绝缘] 输入 rows=%d, future_h=%s", len(df), future_hours)
    if "FC_VehicleIsolationR" not in df.columns or "Timestamp" not in df.columns:
        logger.warning("[预测:绝缘] 缺列 FC_VehicleIsolationR/Timestamp, 跳过")
        return None

    r = _safe_num(df["FC_VehicleIsolationR"])
    r_raw = len(r)
    # 过滤坏值: 65535(传感器故障) / >=9999(溢出) / <=0(无效)
    r = r[(r > 0) & (r != 65535) & (r < 9999)].dropna()
    ts = df["Timestamp"].loc[r.index]
    logger.info("[预测:绝缘] 过滤后: 原始=%d → 有效=%d (mean=%.1f kΩ)",
                r_raw, len(r), r.mean() if len(r) else 0)

    if len(r) < 10:
        logger.warning("[预测:绝缘] 样本不足(%d<10)", len(r))
        return None

    hours = _to_hours(ts)
    if len(hours) == 0:
        return None

    df_bin = pd.DataFrame({"h": hours, "v": r.to_numpy()})
    # 每10分钟取最小值(企业规则)
    df_bin = df_bin.groupby(pd.cut(df_bin["h"], bins=np.arange(
        0, df_bin["h"].max() + 1, 1 / 6.0))).min().dropna()
    x = df_bin["h"].to_numpy()
    y = df_bin["v"].to_numpy()

    if len(x) < 3:
        logger.warning("[预测:绝缘] 分箱后样本不足(%d<3)", len(x))
        return None

    a, b, r2 = _linear_fit(x, y)
    future_x = np.linspace(x.max(), x.max() + future_hours, 50)
    future_y = np.maximum(a * future_x + b, 0)
    resid_std = float(np.std(y - (a * x + b))) if len(y) > 2 else 1.0

    # 计算触达报警线时间
    alarm_info = ""
    t_350 = t_250 = None
    if a < 0:  # 下降趋势
        t_350 = (350 - b) / a if a != 0 else float("inf")
        t_250 = (250 - b) / a if a != 0 else float("inf")
        if t_350 > x.max():
            alarm_info = f"预测约 {t_350:.1f}h 后触达 350kΩ 一级报警线"
        elif t_250 > x.max():
            alarm_info = f"已低于350kΩ,预测约 {t_250:.1f}h 后触达 250kΩ 二级报警线"
        else:
            alarm_info = "已低于250kΩ二级报警线,需立即检修"
    else:
        alarm_info = "绝缘电阻稳定或上升趋势,暂无触达报警线风险"
    logger.info("[预测:绝缘] 拟合: a=%.6f/h b=%.2f r²=%.3f bins=%d y_range=%.1f~%.1f | %s",
                a, b, r2, len(x), y.min(), y.max(), alarm_info)

    interpretation = _interpret_trend(
        a, a * 24, "绝缘电阻", ascending_bad=False) + " | " + alarm_info

    return ForecastResult(
        metric_name="绝缘电阻(kΩ)",
        history_x=x, history_y=y,
        future_x=future_x, future_y=future_y,
        confidence_low=np.maximum(future_y - 1.96 * resid_std, 0),
        confidence_high=future_y + 1.96 * resid_std,
        slope=a, r2=r2,
        interpretation=interpretation,
        extra={"resid_std": resid_std, "alarm_350h": t_350,
               "alarm_250h": t_250},
    )


def forecast_avg_cell_voltage(df: pd.DataFrame,
                              future_hours: float) -> ForecastResult | None:
    """预测平均单体电压衰减趋势(电堆健康度核心指标)。"""
    logger.info("[预测:均压] 输入 rows=%d, future_h=%s", len(df), future_hours)
    if "FC_AvgCellVoltage" not in df.columns or "Timestamp" not in df.columns:
        logger.warning("[预测:均压] 缺列 FC_AvgCellVoltage/Timestamp, 跳过")
        return None

    v = _safe_num(df["FC_AvgCellVoltage"])
    v_raw = len(v)
    # 过滤: mV 有效范围 0-2000,排除65535
    v = v[(v > 0) & (v != 65535) & (v < 2000)].dropna()
    ts = df["Timestamp"].loc[v.index]
    logger.info("[预测:均压] 过滤后: 原始=%d → 有效=%d (mean=%.1f mV)",
                v_raw, len(v), v.mean() if len(v) else 0)

    if len(v) < 10:
        logger.warning("[预测:均压] 样本不足(%d<10)", len(v))
        return None

    hours = _to_hours(ts)
    if len(hours) == 0:
        return None

    df_bin = pd.DataFrame({"h": hours, "v": v.to_numpy()})
    df_bin = df_bin.groupby(pd.cut(df_bin["h"], bins=np.arange(
        0, df_bin["h"].max() + 1, 1.0))).mean().dropna()
    x = df_bin["h"].to_numpy()
    y = df_bin["v"].to_numpy()

    if len(x) < 3:
        logger.warning("[预测:均压] 分箱后样本不足(%d<3)", len(x))
        return None

    a, b, r2 = _linear_fit(x, y)
    future_x = np.linspace(x.max(), x.max() + future_hours, 50)
    future_y = np.maximum(a * future_x + b, 0)
    resid_std = float(np.std(y - (a * x + b))) if len(y) > 2 else 1.0

    # 600mV 预警线
    alarm_info = ""
    t_600 = None
    if a < 0:
        t_600 = (600 - b) / a if a != 0 else float("inf")
        if t_600 > x.max():
            alarm_info = f"预测约 {t_600:.1f}h 后触达 600mV 预警线"
        else:
            alarm_info = "已低于600mV预警线"
    else:
        alarm_info = "平均单体电压稳定或上升,暂无触达预警线风险"
    logger.info("[预测:均压] 拟合: a=%.6f/h b=%.2f r²=%.3f bins=%d y_range=%.1f~%.1f | %s",
                a, b, r2, len(x), y.min(), y.max(), alarm_info)

    interpretation = _interpret_trend(
        a, a * 24, "平均单体电压", ascending_bad=False) + " | " + alarm_info

    return ForecastResult(
        metric_name="平均单体电压(mV)",
        history_x=x, history_y=y,
        future_x=future_x, future_y=future_y,
        confidence_low=np.maximum(future_y - 1.96 * resid_std, 0),
        confidence_high=future_y + 1.96 * resid_std,
        slope=a, r2=r2,
        interpretation=interpretation,
        extra={"resid_std": resid_std, "alarm_600h": t_600},
    )


def forecast_cell_deviation(df: pd.DataFrame,
                            future_hours: float) -> ForecastResult | None:
    """预测离均差趋势(单体一致性劣化,>50mV 触发预警)。"""
    logger.info("[预测:离均差] 输入 rows=%d, future_h=%s", len(df), future_hours)
    if "FC_AvgCellVoltDev" not in df.columns or "Timestamp" not in df.columns:
        logger.warning("[预测:离均差] 缺列 FC_AvgCellVoltDev/Timestamp, 跳过")
        return None

    d = _safe_num(df["FC_AvgCellVoltDev"])
    d_raw = len(d)
    # 过滤: mV 有效范围 0-200,排除65535
    d = d[(d >= 0) & (d != 65535) & (d < 200)].dropna()
    ts = df["Timestamp"].loc[d.index]
    logger.info("[预测:离均差] 过滤后: 原始=%d → 有效=%d (mean=%.1f mV)",
                d_raw, len(d), d.mean() if len(d) else 0)

    if len(d) < 10:
        logger.warning("[预测:离均差] 样本不足(%d<10)", len(d))
        return None

    hours = _to_hours(ts)
    if len(hours) == 0:
        return None

    df_bin = pd.DataFrame({"h": hours, "v": d.to_numpy()})
    df_bin = df_bin.groupby(pd.cut(df_bin["h"], bins=np.arange(
        0, df_bin["h"].max() + 1, 1.0))).mean().dropna()
    x = df_bin["h"].to_numpy()
    y = df_bin["v"].to_numpy()

    if len(x) < 3:
        logger.warning("[预测:离均差] 分箱后样本不足(%d<3)", len(x))
        return None

    a, b, r2 = _linear_fit(x, y)
    future_x = np.linspace(x.max(), x.max() + future_hours, 50)
    future_y = np.maximum(a * future_x + b, 0)
    resid_std = float(np.std(y - (a * x + b))) if len(y) > 2 else 1.0

    # 50mV 预警线
    alarm_info = ""
    t_50 = None
    if a > 0:  # 上升趋势(劣化)
        t_50 = (50 - b) / a if a != 0 else float("inf")
        if t_50 > x.max():
            alarm_info = f"预测约 {t_50:.1f}h 后触达 50mV 预警线"
        else:
            alarm_info = "已超过50mV预警线"
    else:
        alarm_info = "离均差稳定或下降,一致性良好"
    logger.info("[预测:离均差] 拟合: a=%.6f/h b=%.2f r²=%.3f bins=%d y_range=%.1f~%.1f | %s",
                a, b, r2, len(x), y.min(), y.max(), alarm_info)

    interpretation = _interpret_trend(
        a, a * 24, "离均差", ascending_bad=True) + " | " + alarm_info

    return ForecastResult(
        metric_name="离均差(mV)",
        history_x=x, history_y=y,
        future_x=future_x, future_y=future_y,
        confidence_low=np.maximum(future_y - 1.96 * resid_std, 0),
        confidence_high=future_y + 1.96 * resid_std,
        slope=a, r2=r2,
        interpretation=interpretation,
        extra={"resid_std": resid_std, "alarm_50h": t_50},
    )


def forecast_all(df: pd.DataFrame, future_hours: float) -> list[ForecastResult]:
    """对一个车辆 DataFrame 做全部 7 个指标预测。"""
    results = []
    for fn in (forecast_cell_diff, forecast_hydrogen_cumulative,
               forecast_fault_frequency, forecast_net_power,
               forecast_insulation, forecast_avg_cell_voltage,
               forecast_cell_deviation):
        try:
            r = fn(df, future_hours)
            if r is not None:
                results.append(r)
        except Exception as e:
            logger.error("预测 %s 失败: %s", fn.__name__, e, exc_info=True)
    logger.info("趋势预测完成: 成功 %d / 7", len(results))
    return results


def fig_forecast(r: ForecastResult):
    """把单个预测结果画成 Plotly 图(历史 + 预测 + 置信带)。"""
    import plotly.graph_objects as go

    fig = go.Figure()
    # 历史
    fig.add_trace(go.Scatter(
        x=r.history_x, y=r.history_y,
        mode="markers", name="历史",
        marker=dict(size=4, color="#1f77b4"),
    ))
    # 预测
    fig.add_trace(go.Scatter(
        x=r.future_x, y=r.future_y,
        mode="lines", name="预测",
        line=dict(color="#ff7f0e", width=2, dash="dash"),
    ))
    # 置信带
    fig.add_trace(go.Scatter(
        x=list(r.future_x) + list(r.future_x[::-1]),
        y=list(r.confidence_high) + list(r.confidence_low[::-1]),
        fill="toself", fillcolor="rgba(255,127,14,0.15)",
        line=dict(color="rgba(0,0,0,0)"),
        name="95% 置信区间", showlegend=False,
    ))
    fig.update_layout(
        title=f"{r.metric_name} 趋势预测 (R²={r.r2:.3f})",
        xaxis_title="时间(小时)",
        yaxis_title=r.metric_name,
        template="plotly_white",
        height=350,
        margin=dict(l=40, r=20, t=50, b=40),
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )
    return fig
