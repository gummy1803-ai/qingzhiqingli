"""Python 版燃料电池模拟数据生成器(逻辑参考 src/mock/data.ts)。

生成 9 个信号的合理模拟数据:
  FC_CurrOut(电流) / FC_VoltOut(电压) / FC_NetPwrOut(净功率)
  FC_MinCellVoltage(最小单体电压) / FC_MinVoltageChannel(通道)
  FC_AvgCellVoltage / FC_AvgCellVoltDev / FC_VehicleIsolationR / FC_RunTime_Hours

@st.cache_data 装饰:参数(vehicle_id/start/end)变化时才重新生成,结果缓存。
"""
from __future__ import annotations

import zlib

import numpy as np
import pandas as pd
import streamlit as st


@st.cache_data(show_spinner=False, max_entries=8)
def generate_mock_data(
    vehicle_id: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    """生成指定车辆在 [start, end] 范围内的模拟数据(1 秒间隔)。

    Args:
        vehicle_id: 车辆 ID(影响随机种子,不同车数据不同)
        start/end: 时间范围(pandas Timestamp)

    Returns:
        DataFrame:含 Timestamp 列 + 9 个信号列;空范围返回空 DataFrame
    """
    # 1 秒间隔时间轴
    ts = pd.date_range(start=start, end=end, freq='s')
    n = len(ts)
    if n <= 0:
        return pd.DataFrame()

    # 稳定随机种子:按车辆 ID + 起始时间派生,可复现
    seed = zlib.crc32(str(vehicle_id).encode()) ^ int(pd.Timestamp(start).timestamp())
    seed = seed & 0xFFFFFFFF
    rng = np.random.default_rng(seed)

    # 启动阶段(前 60 秒低电流),之后运行阶段波动
    warmup = min(60, n)
    curr = np.empty(n)
    curr[:warmup] = rng.uniform(50, 80, warmup)              # 启动低电流
    curr[warmup:] = rng.uniform(150, 400, n - warmup)        # 运行波动

    volt = rng.uniform(280, 380, n)
    pwr = curr * volt / 1000.0 + rng.normal(0, 1.5, n)        # 净功率 = I*V/1000 + 噪声
    pwr = np.clip(pwr, 0, 150)

    avg_v = rng.uniform(3.3, 3.9, n)
    min_v = avg_v - rng.uniform(0.1, 0.4, n)
    # 约 1% 概率骤降到 2.0V(故障模拟)
    fault_mask = rng.random(n) < 0.01
    min_v[fault_mask] = 2.0

    chan = rng.integers(1, 121, n)                            # 通道 1-120
    dev = rng.uniform(-0.3, 0.3, n)                          # 离均差
    iso = rng.uniform(500, 2000, n)                          # 绝缘 kΩ
    iso_warn = rng.random(n) < 0.005
    iso[iso_warn] = 300                                      # 约 0.5% 掉到 300

    # 运行时间累计递增(0-10000h,每秒 +1/3600)
    base_h = rng.uniform(0, 9000)
    run_h = base_h + np.arange(n) / 3600.0

    df = pd.DataFrame({
        'Timestamp': ts,
        'FC_CurrOut': np.round(curr, 2),
        'FC_VoltOut': np.round(volt, 2),
        'FC_NetPwrOut': np.round(pwr, 2),
        'FC_MinCellVoltage': np.round(min_v, 3),
        'FC_MinVoltageChannel': chan,
        'FC_AvgCellVoltage': np.round(avg_v, 3),
        'FC_AvgCellVoltDev': np.round(dev, 3),
        'FC_VehicleIsolationR': np.round(iso, 1),
        'FC_RunTime_Hours': np.round(run_h, 4),
    })
    return df
