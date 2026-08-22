"""生成含 95A 连续稳态段 + 衰减趋势的测试 CSV。

用于验证 Tab8「性能统计预测」的稳态筛选 + 极化曲线 + 衰减分析模块:
- 95A 稳态段有两个(300-1800s, 2700-3600s),均 >= 60s,可被 find_steady_segments 检出
- 平均单体电压随累计运行时间线性衰减(-100 mV/1000h),analyze_degradation 能算出斜率/剩余寿命/健康度
- 150A 段(2100-2700s)作为第二电流点,可做多电流分组对比
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


def generate() -> pd.DataFrame:
    rng = np.random.default_rng(7)
    n = 3600  # 1 小时,每秒 1 条
    ts = pd.date_range("2026-08-22 00:00:00", periods=n, freq="1s")
    i = np.arange(n)

    # 累计运行时间:5000h 起,每秒 +0.5h(放大跨度到 1800h,让衰减可回归检测)
    run_time = 5000.0 + i * 0.5

    # 电流工况:启动→95A稳态→变载→150A稳态→95A稳态
    curr = np.zeros(n)
    curr[0:300] = np.linspace(50, 95, 300)           # 启动
    curr[300:1800] = 95 + rng.normal(0, 1.5, 1500)  # 95A 稳态段1(1500s)
    curr[1800:2100] = np.linspace(95, 150, 300)      # 变载
    curr[2100:2700] = 150 + rng.normal(0, 2.0, 600)  # 150A 稳态段(600s)
    curr[2700:3600] = 95 + rng.normal(0, 1.5, 900)   # 95A 稳态段2(900s)

    # 平均单体电压:3.8V 起,随 run_time 线性衰减 -150 mV/1000h
    # (跨度 1800h,前段 3.72V/后段 3.56V,健康度落黄色区间,剩余寿命可算)
    base_v = 3.8 - 0.00015 * (run_time - 5000.0)  # 衰减项
    avg_cell = base_v - (curr - 95) * 0.0008 + rng.normal(0, 0.008, n)
    min_cell = avg_cell - 0.25 + rng.normal(0, 0.005, n)  # 最小单体电压

    # 电堆输出电压:300V 基准,随电流反相
    volt_out = 300 - (curr - 95) * 0.5 + rng.normal(0, 0.5, n)

    # 净功率 = 电流*电压/1000 (kW)
    net_pwr = curr * volt_out / 1000.0

    # 绝缘电阻:1000-1500kΩ
    iso = 1000 + rng.uniform(0, 500, n)

    # 最小电压通道:1-120 整数
    ch = 1 + rng.integers(0, 120, n)

    df = pd.DataFrame({
        "Timestamp": ts,
        "FC_CurrOut": np.round(curr, 2),
        "FC_VoltOut": np.round(volt_out, 2),
        "FC_NetPwrOut": np.round(net_pwr, 2),
        "FC_AvgCellVoltage": np.round(avg_cell, 4),
        "FC_MinCellVoltage": np.round(min_cell, 4),
        "FC_MinVoltageChannel": ch,
        "FC_VehicleIsolationR": np.round(iso, 1),
        "FC_RunTime_Hours": np.round(run_time, 4),
    })
    return df


if __name__ == "__main__":
    df = generate()
    out = Path(__file__).parent / "fixtures" / "steady_95a_test.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    print(f"生成: {out} ({len(df)} 行)")

    # 自检:95A 稳态段是否可被 find_steady_segments 检出
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from performance.steady_state_selector import find_steady_segments
    segs = find_steady_segments(df, target_current=95, tolerance=5,
                                min_duration=60)
    print(f"95A±5 稳态段(min 60s): {len(segs)} 个")
    for s in segs:
        print(f"  [{s['start_idx']}:{s['end_idx']}] dur={s['duration']}s "
              f"avg_curr={s['mean_current']:.2f}A")

    # 自检:衰减分析
    from performance.segment_aggregator import aggregate_segments
    from performance.degradation_analyzer import analyze_degradation
    agg = aggregate_segments(segs, ["FC_AvgCellVoltage", "FC_VoltOut"],
                             exclude_anomaly=False)
    if len(agg) >= 2:
        deg = analyze_degradation(agg, "FC_AvgCellVoltage_mean",
                                  "run_time_at_mid", "current_target")
        for g in deg["groups"]:
            if g.get("skip"):
                continue
            print(f"衰减[{g['label']}]: {g['slope_mv_per_1000h']} mV/1000h "
                  f"R2={g['r_squared']} 剩余={g['remaining_life_hours']}h "
                  f"健康={g['health_score']}({g['health_status']})")
    print("自检完成")
