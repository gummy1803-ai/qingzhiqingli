"""生成模拟电池测试数据,覆盖7项预测维度。

生成策略:
- 12 小时数据,1Hz 采样 = 43,200 行
- 包含 7 项预测所需字段:
  1. FC_MaxCellVoltage / FC_MinCellVoltage → 压差预测(渐增劣化)
  2. FC_HydCmInstts → 氢耗预测(稳定消耗)
  3. FC_ErrorCode → 故障频率预测(渐增故障)
  4. FC_NetPwrOut → 净功率预测(渐降衰减)
  5. FC_VehicleIsolationR → 绝缘电阻预测(渐降触达350kΩ)
  6. FC_AvgCellVoltage → 平均单体电压预测(渐降触达600mV)
  7. FC_AvgCellVoltDev → 离均差预测(渐增触达50mV)
- 故意加入坏值: 65535, 9999, 0, -1 用于验证清洗逻辑
- 故障码: 前6小时稀疏,后6小时密集(验证趋势)

输出: tests/mock_forecast_7dims.csv
"""
from __future__ import annotations

import os
import sys
import numpy as np
import pandas as pd

# 项目根目录
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def generate() -> pd.DataFrame:
    """生成 12 小时 1Hz 模拟数据,覆盖 7 项预测维度。"""
    total_seconds = 12 * 3600  # 12 小时
    n = total_seconds  # 1Hz = 43200 行
    base_time = pd.Timestamp("2026-01-01 08:00:00")
    ts = pd.date_range(base_time, periods=n, freq="1s")

    # 小时数 (0 ~ 12)
    hours = np.linspace(0, 12, n)

    # 1. 压差: 从 5mV 线性增长到 35mV (劣化趋势)
    base_diff = 5 + 2.5 * hours  # 5 → 35 mV
    noise = np.random.normal(0, 1.5, n)
    diff = np.maximum(base_diff + noise, 0.1)

    # FC_MaxCellVoltage: 基准 750 + diff/2
    max_v = 750 + diff / 2 + np.random.normal(0, 5, n)
    # FC_MinCellVoltage: 基准 750 - diff/2
    min_v = 750 - diff / 2 + np.random.normal(0, 5, n)

    # 2. 瞬时氢耗: 稳定 0.5 kg/h + 噪声
    hyd_inst = 0.5 + np.random.normal(0, 0.05, n)

    # 3. 故障码: 前 6h 稀疏(每小时 2-3 次), 后 6h 密集(渐增到 10-15 次)
    error_code = np.zeros(n, dtype=int)
    for h in range(12):
        seg_mask = (hours >= h) & (hours < h + 1)
        seg_len = seg_mask.sum()
        if h < 6:
            # 前 6h: 每小时 2-3 次故障
            n_faults = np.random.randint(2, 4)
        else:
            # 后 6h: 故障次数线性增长
            n_faults = int(4 + (h - 6) * 2)  # 4,6,8,10,12,14
        fault_indices = np.random.choice(seg_len, min(n_faults, seg_len), replace=False)
        error_code[np.where(seg_mask)[0][fault_indices]] = np.random.randint(100, 999, len(fault_indices))

    # 4. 净功率: 从 80 kW 线性衰减到 65 kW
    base_power = 80 - 1.25 * hours  # 80 → 65 kW
    noise_p = np.random.normal(0, 2, n)
    power = np.maximum(base_power + noise_p, 0)

    # 5. 绝缘电阻: 从 500 kΩ 线性下降到 380 kΩ (接近 350 报警线)
    base_iso = 500 - 10 * hours  # 500 → 380 kΩ
    noise_i = np.random.normal(0, 8, n)
    iso = np.maximum(base_iso + noise_i, 0)

    # 6. 平均单体电压: 从 750 mV 线性下降到 680 mV
    base_avg = 750 - 5.8 * hours  # 750 → 680 mV
    noise_a = np.random.normal(0, 3, n)
    avg_v = np.maximum(base_avg + noise_a, 0)

    # 7. 离均差: 从 10 mV 线性增长到 45 mV (接近 50 预警线)
    base_dev = 10 + 2.9 * hours  # 10 → 45 mV
    noise_d = np.random.normal(0, 1, n)
    dev = np.maximum(base_dev + noise_d, 0)

    # 其他字段
    curr_out = 200 + np.random.normal(0, 10, n)
    volt_out = 400 + np.random.normal(0, 5, n)
    main_sts = np.where(np.random.random(n) > 0.05, 4, 8)  # 95% 运行态

    df = pd.DataFrame({
        "Timestamp": ts,
        "FC_CurrOut": curr_out,
        "FC_VoltOut": volt_out,
        "FC_NetPwrOut": power,
        "FC_MaxCellVoltage": max_v.round(1),
        "FC_MinCellVoltage": min_v.round(1),
        "FC_MinVoltageChannel": np.random.randint(1, 200, n),
        "FC_AvgCellVoltage": avg_v.round(1),
        "FC_AvgCellVoltDev": dev.round(1),
        "FC_VehicleIsolationR": iso.round(1),
        "FC_HydCmInstts": hyd_inst,
        "FC_HydCmPerHundred": 0.8 + np.random.normal(0, 0.02, n),
        "FC_ErrorCode": error_code,
        "FC_MainSts": main_sts,
        "FC_RunTime_Hours": hours,
    })

    # 注入坏值 (用于验证清洗逻辑)
    n_bad = 200
    bad_indices = np.random.choice(n, n_bad, replace=False)

    # 绝缘电阻: 注入 50 个 65535 (传感器故障)
    iso_bad = np.random.choice(bad_indices, 50, replace=False)
    df.loc[iso_bad, "FC_VehicleIsolationR"] = 65535

    # 绝缘电阻: 注入 30 个 9999 (溢出)
    iso_overflow = np.random.choice(
        np.setdiff1d(bad_indices, iso_bad), 30, replace=False)
    df.loc[iso_overflow, "FC_VehicleIsolationR"] = 9999

    # 绝缘电阻: 注入 20 个 0 (无效值)
    iso_zero = np.random.choice(
        np.setdiff1d(bad_indices, np.concatenate([iso_bad, iso_overflow])),
        20, replace=False)
    df.loc[iso_zero, "FC_VehicleIsolationR"] = 0

    # 平均单体电压: 注入 30 个 65535
    avg_bad = np.random.choice(bad_indices, 30, replace=False)
    df.loc[avg_bad, "FC_AvgCellVoltage"] = 65535

    # 离均差: 注入 20 个 65535
    dev_bad = np.random.choice(
        np.setdiff1d(bad_indices, avg_bad), 20, replace=False)
    df.loc[dev_bad, "FC_AvgCellVoltDev"] = 65535

    # 净功率: 注入 30 个 0 (停机)
    pwr_bad = np.random.choice(
        np.setdiff1d(bad_indices, np.concatenate([avg_bad, dev_bad])),
        30, replace=False)
    df.loc[pwr_bad, "FC_NetPwrOut"] = 0

    return df


def main() -> None:
    np.random.seed(42)
    df = generate()

    out_dir = os.path.join(_ROOT, "tests")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "mock_forecast_7dims.csv")
    df.to_csv(out_path, index=False, encoding="utf-8-sig")

    print(f"[生成] {out_path}")
    print(f"[行数] {len(df):,}")
    print(f"[时间] {df['Timestamp'].iloc[0]} ~ {df['Timestamp'].iloc[-1]}")
    print(f"[时长] {(df['Timestamp'].iloc[-1] - df['Timestamp'].iloc[0]).total_seconds() / 3600:.1f} 小时")
    print()
    print("=== 7 项预测维度数据摘要 ===")

    # 1. 压差
    diff = df["FC_MaxCellVoltage"] - df["FC_MinCellVoltage"]
    print(f"1. 压差:   {diff.min():.1f} ~ {diff.max():.1f} mV (趋势: {diff.iloc[:100].mean():.1f} → {diff.iloc[-100:].mean():.1f})")

    # 2. 氢耗
    print(f"2. 氢耗:   mean={df['FC_HydCmInstts'].mean():.3f} kg/h (稳定消耗)")

    # 3. 故障
    faults = (df["FC_ErrorCode"] > 0).sum()
    print(f"3. 故障:   {faults} 次故障码 (前6h: {(df['FC_ErrorCode'].iloc[:21600] > 0).sum()}, 后6h: {(df['FC_ErrorCode'].iloc[21600:] > 0).sum()})")

    # 4. 净功率
    print(f"4. 净功率: {df['FC_NetPwrOut'].iloc[:100].mean():.1f} → {df['FC_NetPwrOut'].iloc[-100:].mean():.1f} kW (衰减)")

    # 5. 绝缘
    iso_valid = df["FC_VehicleIsolationR"][(df["FC_VehicleIsolationR"] > 0) & (df["FC_VehicleIsolationR"] != 65535) & (df["FC_VehicleIsolationR"] < 9999)]
    print(f"5. 绝缘:   {iso_valid.iloc[:100].mean():.1f} → {iso_valid.iloc[-100:].mean():.1f} kΩ (含 {len(df) - len(iso_valid)} 个坏值)")

    # 6. 均压
    avg_valid = df["FC_AvgCellVoltage"][(df["FC_AvgCellVoltage"] > 0) & (df["FC_AvgCellVoltage"] != 65535)]
    print(f"6. 均压:   {avg_valid.iloc[:100].mean():.1f} → {avg_valid.iloc[-100:].mean():.1f} mV (衰减, 含 {len(df) - len(avg_valid)} 个坏值)")

    # 7. 离均差
    dev_valid = df["FC_AvgCellVoltDev"][(df["FC_AvgCellVoltDev"] >= 0) & (df["FC_AvgCellVoltDev"] != 65535)]
    print(f"7. 离均差: {dev_valid.iloc[:100].mean():.1f} → {dev_valid.iloc[-100:].mean():.1f} mV (增长, 含 {len(df) - len(dev_valid)} 个坏值)")

    print()
    print(f"=== 坏值注入统计 ===")
    print(f"绝缘 65535: {(df['FC_VehicleIsolationR'] == 65535).sum()}")
    print(f"绝缘 9999:  {(df['FC_VehicleIsolationR'] == 9999).sum()}")
    print(f"绝缘 0:     {(df['FC_VehicleIsolationR'] == 0).sum()}")
    print(f"均压 65535: {(df['FC_AvgCellVoltage'] == 65535).sum()}")
    print(f"离均差65535:{(df['FC_AvgCellVoltDev'] == 65535).sum()}")
    print(f"净功率 0:   {(df['FC_NetPwrOut'] == 0).sum()}")

    print()
    print("=== 本地验证步骤 ===")
    print("1. 启动 Streamlit: streamlit run app.py")
    print("2. 侧边栏上传 tests/mock_forecast_7dims.csv (整车数据)")
    print("3. 进入「趋势预测」Tab,选车辆,点「开始预测」")
    print("4. 检查日志: Get-Content logs/e2e_run.log -Encoding UTF8 | Select-String '预测'")


if __name__ == "__main__":
    main()
