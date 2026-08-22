"""指标计算模块:运行时长/里程/氢耗/单片电压一致性/故障码统计等。"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from src.log_config import get_logger

logger = get_logger(__name__)


def _safe_num(series: pd.Series) -> pd.Series:
    """安全数值化:把列强制转 float,无法转换的变 NaN。

    用于在过滤之前防御字符串/混合类型列导致的 TypeError。
    """
    if pd.api.types.is_numeric_dtype(series):
        return series
    return pd.to_numeric(series, errors="coerce")


def vehicle_overview(df: pd.DataFrame) -> dict:
    """整车运行概览指标。

    输入:load_vehicle_csvs 输出的 DataFrame
    输出:运行时长、行驶里程、平均车速、总氢耗、百公里氢耗、启动次数、故障码种类
    """
    logger.info("计算 vehicle_overview,输入 %d 行", len(df))
    res: dict = {}

    # 时间跨度(秒)
    if "Timestamp" in df.columns and len(df):
        span_s = (df["Timestamp"].iloc[-1] - df["Timestamp"].iloc[0]).total_seconds()
        res["运行时长(h)"] = round(span_s / 3600, 2)
        res["采样点数"] = len(df)
        res["起止时间"] = f"{df['Timestamp'].iloc[0]} → {df['Timestamp'].iloc[-1]}"
    else:
        logger.warning("缺少 Timestamp 列或空数据,跳过时间跨度计算")

    # 运行时长字段(累计,与上面跨度可能不同)
    if "FC_RunTime_Hours" in df.columns:
        res["累计运行小时"] = float(df["FC_RunTime_Hours"].iloc[-1])
    if "FC_RunTime_Min" in df.columns:
        res["累计运行分钟"] = float(df["FC_RunTime_Min"].iloc[-1])

    # 里程差
    if "FC_VehicleKM" in df.columns:
        v = _safe_num(df["FC_VehicleKM"])
        v = v[~v.isin([65535, -1, 999, 99]) & (v >= 0)].dropna()
        if len(v):
            res["里程末值(km)"] = float(v.iloc[-1])
            res["里程初值(km)"] = float(v.iloc[0])
            res["行驶里程(km)"] = round(float(v.iloc[-1] - v.iloc[0]), 2)
        else:
            logger.warning("FC_VehicleKM 全部为异常值,无里程指标")
    else:
        logger.debug("无 FC_VehicleKM 列")

    # 平均车速
    if "FC_VehicleSpd" in df.columns:
        s = _safe_num(df["FC_VehicleSpd"])
        s = s[(s >= 0) & (s < 200)].dropna()
        res["平均车速(km/h)"] = round(float(s.mean()), 2) if len(s) else 0.0
        res["最高车速(km/h)"] = round(float(s.max()), 2) if len(s) else 0.0

    # 启动次数
    if "FC_StartTimes" in df.columns:
        st = _safe_num(df["FC_StartTimes"])
        st = st[st >= 0].dropna()
        if len(st):
            res["启动次数"] = int(st.iloc[-1])

    # 百公里氢耗(取字段统计)
    if "FC_HydCmPerHundred" in df.columns:
        h = _safe_num(df["FC_HydCmPerHundred"])
        h = h[(h > 0) & (h < 100)].dropna()
        if len(h):
            res["百公里氢耗均值(kg)"] = round(float(h.mean()), 2)
            res["百公里氢耗峰值(kg)"] = round(float(h.max()), 2)

    # 瞬时氢耗均值
    if "FC_HydCmInstts" in df.columns:
        hi = _safe_num(df["FC_HydCmInstts"])
        hi = hi[hi >= 0].dropna()
        if len(hi):
            res["瞬时氢耗均值(kg/h)"] = round(float(hi.mean()), 2)

    # 故障码统计
    if "FC_ErrorCode" in df.columns:
        ec = _safe_num(df["FC_ErrorCode"])
        ec = ec[ec > 0].dropna()
        if len(ec):
            top = ec.value_counts().head(10)
            res["故障码Top10"] = top.to_dict()
            res["故障总数"] = int(ec.shape[0])
            res["故障码种类"] = int(ec.nunique())
            logger.info("  故障统计: 总数=%d / 种类=%d / Top1=%s",
                        res["故障总数"], res["故障码种类"],
                        list(top.items())[0] if len(top) else "无")
        else:
            logger.info("  无故障码(ErrorCode 全为 0)")

    logger.info("vehicle_overview 完成,产出 %d 个指标", len(res))
    return res


def cell_voltage_consistency(df: pd.DataFrame) -> dict:
    """单片电压一致性:最大/最小/平均电压、压差分布。"""
    logger.info("计算 cell_voltage_consistency,输入 %d 行", len(df))
    res = {}
    cols = ["FC_MinCellVoltage", "FC_MaxCellVoltage", "FC_AvgCellVoltage"]
    for c in cols:
        if c in df.columns:
            v = _safe_num(df[c])
            # 数据单位疑似 mV(单片正常 600~900),保留 (0, 2000) 范围
            v = v[(v > 0) & (v < 2000)].dropna()
            if len(v):
                res[c] = {
                    "mean": round(float(v.mean()), 2),
                    "min": round(float(v.min()), 2),
                    "max": round(float(v.max()), 2),
                }
                logger.debug("  %s: mean=%.2f min=%.2f max=%.2f (n=%d)",
                             c, res[c]["mean"], res[c]["min"], res[c]["max"], len(v))
            else:
                logger.warning("  %s 在过滤后无数据", c)
    # 压差(最大 - 最小):先对两端做范围过滤,防御负值/异常值制造假高压差
    # 数据单位疑似 mV,与单电压统计保持一致用 (0, 2000) 范围
    if "FC_MaxCellVoltage" in df.columns and "FC_MinCellVoltage" in df.columns:
        mx = _safe_num(df["FC_MaxCellVoltage"])
        mn = _safe_num(df["FC_MinCellVoltage"])
        # 两端都要求在 (0, 2000) 范围内
        ok = (mx > 0) & (mx < 2000) & (mn > 0) & (mn < 2000)
        diff = (mx - mn)[ok]
        # 单片压差物理上不可能 >50mV,超过即视为采集异常
        diff = diff[(diff > 0) & (diff < 50)].dropna()
        if len(diff):
            res["cell_diff"] = {
                "mean": round(float(diff.mean()), 2),
                "max": round(float(diff.max()), 2),
            }
            logger.info("  压差: mean=%.2f max=%.2f (过滤前 %d 行 → 后 %d 行)",
                        res["cell_diff"]["mean"], res["cell_diff"]["max"],
                        int(ok.sum()), len(diff))

    # 最弱通道定位
    if "FC_MinVoltageChannel" in df.columns:
        ch = _safe_num(df["FC_MinVoltageChannel"])
        ch = ch[ch >= 0].dropna()
        if len(ch):
            top = ch.value_counts().head(5)
            res["最弱通道Top5"] = top.to_dict()
            top1 = next(iter(top.items()), None)
            logger.info("  最弱通道 Top1: %s (%d 次)",
                        top1[0] if top1 else "无",
                        int(top1[1]) if top1 else 0)

    return res


def power_summary(df: pd.DataFrame) -> dict:
    """功率与效率:输出功率/净功率/总电压/总电流。"""
    logger.info("计算 power_summary,输入 %d 行", len(df))
    res = {}
    for c in ["FC_NetPwrOut", "FC_CurrOut", "FC_VoltOut", "TotalVoltage"]:
        if c in df.columns:
            v = _safe_num(df[c])
            v = v[(v > 0) & (v < 100000)].dropna()
            if len(v):
                res[c] = {
                    "mean": round(float(v.mean()), 2),
                    "max": round(float(v.max()), 2),
                }
                logger.debug("  %s: mean=%.2f max=%.2f", c, res[c]["mean"], res[c]["max"])
    return res


def h2_system(df: pd.DataFrame) -> dict:
    """氢系统状态:高压/中压/SOC 末值。"""
    logger.info("计算 h2_system,输入 %d 行", len(df))
    res = {}
    for c in ["FC_HSSHighPreu", "FC_HSSMidPre", "FC_HSSH2SOC"]:
        if c in df.columns:
            v = _safe_num(df[c])
            v = v[v >= 0].dropna()
            if len(v):
                res[c] = {
                    "first": round(float(v.iloc[0]), 2),
                    "last": round(float(v.iloc[-1]), 2),
                    "min": round(float(v.min()), 2),
                    "max": round(float(v.max()), 2),
                }
                logger.debug("  %s: first=%.2f last=%.2f", c,
                             res[c]["first"], res[c]["last"])
    return res


def fault_time_series(df: pd.DataFrame) -> pd.DataFrame:
    """提取所有故障发生时刻(ErrorCode > 0),用于时间轴。"""
    logger.info("提取 fault_time_series,输入 %d 行", len(df))
    if "FC_ErrorCode" not in df.columns:
        logger.warning("无 FC_ErrorCode 列,返回空")
        return pd.DataFrame()
    ec = _safe_num(df["FC_ErrorCode"])
    mask = ec > 0
    # 按可用列取子集,防御缺失 Timestamp / FC_SysFltRnk 列导致 KeyError
    keep_cols = ["Timestamp", "FC_ErrorCode", "FC_SysFltRnk"]
    keep_cols = [c for c in keep_cols if c in df.columns]
    sub = df.loc[mask[mask.fillna(False)].index, keep_cols].copy()
    logger.info("  故障记录 %d 条", len(sub))
    return sub


def vehicle_speed_profile(df: pd.DataFrame) -> pd.DataFrame:
    """返回车速与瞬时氢耗时间序列(过滤异常),用于曲线绘制。"""
    logger.info("提取 vehicle_speed_profile,输入 %d 行", len(df))
    cols = ["Timestamp"]
    keep = ["FC_VehicleSpd", "FC_HydCmInstts", "FC_VehicleKM"]
    cols += [c for c in keep if c in df.columns]
    sub = df[cols].copy()
    # 简单异常剔除(只过滤明显无效)
    for c in keep:
        if c in sub.columns:
            v = _safe_num(sub[c])
            sub[c] = v.where((v >= 0) & (v < 100000))
    logger.debug("  返回 %d 行 / %d 列", len(sub), len(sub.columns))
    return sub
