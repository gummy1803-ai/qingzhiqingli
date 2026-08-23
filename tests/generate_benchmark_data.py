"""生成整车 + 台架 mock 数据集用于本地验证。

生成两个 CSV:
1. mock_vehicle_steady_test.csv — 整车数据,含多段稳态电流 + 过渡段 + 异常段
2. mock_bench_cycle_test.csv — 台架循环数据,6 循环 × 6 功率点,部分触发预警

运行: python tests/generate_benchmark_data.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

# 确保项目根在 sys.path
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

OUT_DIR = os.path.join(_ROOT, "tests", "fixtures")
os.makedirs(OUT_DIR, exist_ok=True)


def generate_vehicle_mock() -> str:
    """生成整车 mock CSV,包含多段稳态电流和异常段。

    时间轴: 2026-08-20 08:00 ~ 12:00 (4 小时, 1 秒间隔 = 14400 行)

    电流工况设计:
      段A (0-600s): 95A ± 5A 稳态段 (10分钟,足够>180s min_duration)
      过渡 (600-720s): 85→95A 过渡段 (2分钟)
      段B (720-1500s): 105A ± 5A 稳态段 (13分钟)
      异常 (1500-1560s): 骤降到 50A 然后恢复 (1分钟故障)
      段C (1560-2400s): 115A ± 5A 稳态段 (14分钟)
      过渡 (2400-2520s): 105→115A 过渡段 (2分钟)
      段D (2520-3600s): 95A ± 5A 稳态段 (18分钟)
      过渡 (3600-3720s): 115→95A 过渡段 (2分钟)
      段E (3720-5000s): 105A ± 5A 稳态段 (21分钟)
      过渡 (5000-5120s): 95→105A 过渡段 (2分钟)
      段F (5120-7200s): 115A ± 5A 稳态段 (35分钟)
      段G (7200-8400s): 95A ± 5A 稳态段 (20分钟)
      段H (8400-10800s): 105A ± 5A 稳态段 (40分钟)
      段I (10800-14400s): 115A ± 5A 稳态段 (60分钟)
    """
    print("=" * 60)
    print("生成整车 mock 数据...")
    print("=" * 60)

    start = pd.Timestamp("2026-08-20 08:00:00")
    n_total = 14400  # 4小时 × 3600秒
    ts = pd.date_range(start=start, periods=n_total, freq="s")

    rng = np.random.default_rng(42)

    # ---- 电流段定义 ----
    # (start_sec, end_sec, target, tolerance, label)
    segments = [
        (0, 600, 95, 5, "steady_95A_1"),
        (600, 720, None, None, "transition_85_95A"),
        (720, 1500, 105, 5, "steady_105A_1"),
        (1500, 1560, None, None, "fault_dip_50A"),
        (1560, 2400, 115, 5, "steady_115A_1"),
        (2400, 2520, None, None, "transition_105_115A"),
        (2520, 3600, 95, 5, "steady_95A_2"),
        (3600, 3720, None, None, "transition_115_95A"),
        (3720, 5000, 105, 5, "steady_105A_2"),
        (5000, 5120, None, None, "transition_95_105A"),
        (5120, 7200, 115, 5, "steady_115A_2"),
        (7200, 8400, 95, 5, "steady_95A_3"),
        (8400, 10800, 105, 5, "steady_105A_3"),
        (10800, 14400, 115, 5, "steady_115A_3"),
    ]

    curr = np.zeros(n_total)
    segment_labels = [""] * n_total

    for seg_start, seg_end, target, tol, label in segments:
        seg_len = seg_end - seg_start
        if target is not None:
            # 稳态段: 围绕 target ± tol 波动
            curr[seg_start:seg_end] = rng.normal(target, tol / 3, seg_len)
            segment_labels[seg_start:seg_end] = [label] * seg_len
        else:
            # 过渡段/故障段: 线性插值
            if label == "fault_dip_50A":
                # 故障段: 骤降到 50A 然后恢复
                curr[seg_start:seg_end] = 50 + rng.normal(0, 2, seg_len)
                segment_labels[seg_start:seg_end] = [label] * seg_len
            elif label.startswith("transition"):
                parts = label.split("_")
                # 解析过渡起点电流
                if "85_95" in label:
                    begin, end = 85, 95
                elif "105_115" in label:
                    begin, end = 105, 115
                elif "115_95" in label:
                    begin, end = 115, 95
                elif "95_105" in label:
                    begin, end = 95, 105
                else:
                    begin, end = 90, 110
                curr[seg_start:seg_end] = np.linspace(begin, end, seg_len) + rng.normal(0, 1, seg_len)
                segment_labels[seg_start:seg_end] = [label] * seg_len

    # ---- 其他信号基于电流派生 ----
    # 电压: 280-380V,与电流弱相关
    volt = 360 - curr * 0.35 + rng.normal(0, 2, n_total)
    volt = np.clip(volt, 260, 400)

    # 净功率 = 电流 × 电压 / 1000
    pwr = curr * volt / 1000.0 + rng.normal(0, 0.5, n_total)
    pwr = np.clip(pwr, 0, 150)

    # 平均单体电压 (V): 3.3-3.9V,与电压弱相关
    avg_v = (volt / 96.0) + rng.normal(0, 0.03, n_total)
    avg_v = np.clip(avg_v, 2.8, 4.2)

    # 最小单体电压: 比平均低 0.1-0.3V
    min_v = avg_v - rng.uniform(0.05, 0.35, n_total)
    min_v = np.clip(min_v, 2.5, 4.2)
    # 故障段: 模拟电压骤降
    fault_mask = np.array([l == "fault_dip_50A" for l in segment_labels])
    min_v[fault_mask] = 2.0 + rng.normal(0, 0.1, fault_mask.sum())

    # 最小电压通道: 1-120
    chan = rng.integers(1, 121, n_total)

    # 离均差 (V): -0.3 ~ 0.3
    dev = rng.normal(0, 0.1, n_total)
    dev = np.clip(dev, -0.5, 0.5)

    # 方差 (mV²): 基于电流波动
    var = np.abs(dev) * 1000 + rng.normal(5, 2, n_total)
    var = np.clip(var, 0, 500)

    # 绝缘电阻 (kΩ): 500-2000
    iso = rng.uniform(500, 2000, n_total)
    # 偶尔掉到 300 (预警)
    iso_warn_idx = rng.choice(n_total, size=20, replace=False)
    iso[iso_warn_idx] = 300 + rng.normal(0, 10, 20)

    # 主状态: 大部分运行态(4),偶尔上电(8)
    sts = np.full(n_total, 4)
    sts_switch = rng.choice(n_total, size=30, replace=False)
    sts[sts_switch] = 8

    # 运行时间累计
    base_h = 5000.0
    run_h = base_h + np.arange(n_total) / 3600.0

    # ---- 构建 DataFrame ----
    df = pd.DataFrame({
        "Timestamp": ts,
        "FC_CurrOut": np.round(curr, 2),
        "FC_VoltOut": np.round(volt, 2),
        "FC_NetPwrOut": np.round(pwr, 2),
        "FC_MinCellVoltage": np.round(min_v, 3),
        "FC_MinVoltageChannel": chan,
        "FC_AvgCellVoltage": np.round(avg_v, 3),
        "FC_AvgCellVoltDev": np.round(dev, 3),
        "FC_VARVoltage": np.round(var, 2),
        "FC_VehicleIsolationR": np.round(iso, 1),
        "FC_MainSts": sts,
        "FC_RunTime_Hours": np.round(run_h, 4),
        "_segment": segment_labels,
    })

    out_path = os.path.join(OUT_DIR, "mock_vehicle_steady_test.csv")
    df.to_csv(out_path, index=False)

    # ---- 统计摘要 ----
    steady_segments = [l for l in segment_labels if l.startswith("steady")]
    from collections import Counter
    seg_counts = Counter(steady_segments)
    print(f"  输出文件: {out_path}")
    print(f"  总行数: {len(df):,}")
    print(f"  时间范围: {ts[0]} ~ {ts[-1]}")
    print(f"  稳态段数: {len(steady_segments)} 段")
    for seg_name, cnt in sorted(seg_counts.items()):
        print(f"    {seg_name}: {cnt}s ({cnt/60:.1f}min)")
    print(f"  电流范围: {curr.min():.1f}A ~ {curr.max():.1f}A")
    print(f"  故障段: {int(fault_mask.sum())} 行")
    print(f"  预警绝缘点: {int((iso < 350).sum())} 行")
    print(f"  平均单体电压范围: {avg_v.min():.3f}V ~ {avg_v.max():.3f}V")

    return out_path


def generate_bench_mock() -> str:
    """生成台架循环 mock CSV,6 循环 × 6 功率点。

    功率点: [33, 58.5, 117, 156, 175.5, 195] kW
    每个功率点持续 30 秒,循环间有 15 秒过渡段。
    总时长: 6 循环 × (6 点 × 30s + 5 过渡 × 15s) = 6 × (180 + 75) = 1530s

    预警触发设计:
      - cycle 3 的 117kW 功率点: 离均差 55mV (触发 >50mV 预警)
      - cycle 5 的 156kW 功率点: 平均单体电压 580mV (触发 <600mV 预警)
      - cycle 6 的 195kW 功率点: 两个条件同时触发
    """
    print()
    print("=" * 60)
    print("生成台架循环 mock 数据...")
    print("=" * 60)

    power_points = [33.0, 58.5, 117.0, 156.0, 175.5, 195.0]
    n_cycles = 6
    duration_per_point = 30  # 秒
    transition_duration = 15  # 过渡秒

    rng = np.random.default_rng(123)

    rows = []
    start_time = pd.Timestamp("2026-08-20 08:00:00")

    alert_trigger = {
        # (cycle_idx, power_idx, type): 预警设计
        (2, 2, "deviation"),      # cycle 3, 117kW 点: 离均差异常
        (4, 3, "low_voltage"),    # cycle 5, 156kW 点: 电压过低
        (5, 5, "both"),           # cycle 6, 195kW 点: 双预警
    }

    row_idx = 0
    for cycle_idx in range(n_cycles):
        cycle_label = f"cycle_{cycle_idx + 1}"
        print(f"  生成 {cycle_label} ...")

        for pp_idx, pp_kw in enumerate(power_points):
            point_label = f"pp_{pp_idx + 1}_{pp_kw}kW"
            transition_label = f"trans_{pp_idx + 1}_{pp_kw}kW"

            # ---- 判断该功率点是否触发预警 ----
            alert_type = None
            for (ac, ap, at) in alert_trigger:
                if ac == cycle_idx and ap == pp_idx:
                    alert_type = at
                    break

            # ---- 稳态段: duration_per_point 秒 ----
            for sec in range(duration_per_point):
                ts = start_time + pd.Timedelta(seconds=row_idx)

                # 功率围绕目标值 ±1.5% 波动
                pwr_noise = rng.normal(0, pp_kw * 0.005)
                net_pwr = pp_kw + pwr_noise

                # 电流: 功率 / 电压 * 1000
                voltage = 360 + rng.normal(0, 1.5)
                current = pp_kw * 1000 / voltage + rng.normal(0, 0.5)

                # 平均单体电压: 正常 780-820mV
                avg_cell = 790 + rng.normal(0, 5)

                # 离均差: 正常 ±10mV
                deviation = rng.normal(0, 4)

                # 阻抗
                lfr = 85 + rng.normal(0, 1)
                hfr = 51 + rng.normal(0, 0.5)

                # ---- 预警数据注入 ----
                if alert_type == "deviation":
                    # 离均差 > 50mV
                    deviation = rng.normal(55, 3)
                    avg_cell = 790 + rng.normal(0, 5)  # 电压正常,仅离均差异常
                elif alert_type == "low_voltage":
                    # 平均单体电压 < 600mV
                    avg_cell = rng.normal(580, 3)
                    deviation = rng.normal(5, 1)  # 离均差正常
                elif alert_type == "both":
                    # 双预警: 离均差 > 50mV 且电压 < 600mV
                    deviation = rng.normal(60, 3)
                    avg_cell = rng.normal(550, 3)

                # 绝缘
                iso = 1500 + rng.normal(0, 50)
                main_sts = 4

                rows.append({
                    "Timestamp": ts,
                    "FC_NetPwrOut": round(net_pwr, 2),
                    "FC_VoltOut": round(voltage, 1),
                    "FC_CurrOut": round(current, 2),
                    "FC_AvgCellVoltage": round(avg_cell, 1),
                    "FC_AvgCellVoltDev": round(deviation, 2),
                    "FC_LFR": round(lfr, 2),
                    "FC_HFR": round(hfr, 2),
                    "FC_VehicleIsolationR": round(iso, 1),
                    "FC_MainSts": main_sts,
                    "cycle_id": cycle_idx,
                    "power_point": pp_kw,
                    "_label": point_label,
                    "_alert_type": alert_type or "normal",
                })
                row_idx += 1

            # ---- 过渡段: transition_duration 秒 ----
            if pp_idx < len(power_points) - 1:
                next_pp = power_points[pp_idx + 1]
                for sec in range(transition_duration):
                    ts = start_time + pd.Timedelta(seconds=row_idx)
                    alpha = (sec + 1) / (transition_duration + 1)
                    interp_pwr = pp_kw + alpha * (next_pp - pp_kw)
                    voltage = 360 + rng.normal(0, 1.5)
                    current = interp_pwr * 1000 / voltage + rng.normal(0, 1)

                    rows.append({
                        "Timestamp": ts,
                        "FC_NetPwrOut": round(interp_pwr, 2),
                        "FC_VoltOut": round(voltage, 1),
                        "FC_CurrOut": round(current, 2),
                        "FC_AvgCellVoltage": round(790 + rng.normal(0, 8), 1),
                        "FC_AvgCellVoltDev": round(rng.normal(0, 6), 2),
                        "FC_LFR": round(85 + rng.normal(0, 2), 2),
                        "FC_HFR": round(51 + rng.normal(0, 1), 2),
                        "FC_VehicleIsolationR": round(1500 + rng.normal(0, 80), 1),
                        "FC_MainSts": 4,
                        "cycle_id": -1,  # 过渡段不归属任何循环
                        "power_point": np.nan,
                        "_label": transition_label,
                        "_alert_type": "transition",
                    })
                    row_idx += 1

    df = pd.DataFrame(rows)
    out_path = os.path.join(OUT_DIR, "mock_bench_cycle_test.csv")
    df.to_csv(out_path, index=False)

    # ---- 统计摘要 ----
    print(f"  输出文件: {out_path}")
    print(f"  总行数: {len(df):,}")
    print(f"  循环数: {n_cycles}")
    print(f"  功率点数/循环: {len(power_points)}")

    stable_mask = df["cycle_id"] >= 0
    stable_df = df[stable_mask]
    print(f"  稳态段行数: {len(stable_df):,}")

    # ---- 预警点统计 ----
    print("\n  🚨 预警点统计:")
    for (ac, ap, at) in alert_trigger:
        pp_kw = power_points[ap]
        cid_rows = stable_df[
            (stable_df["cycle_id"] == ac) & (stable_df["power_point"] == pp_kw)
        ]
        if len(cid_rows) > 0:
            avg_dev = cid_rows["FC_AvgCellVoltDev"].mean()
            avg_cv = cid_rows["FC_AvgCellVoltage"].mean()
            print(f"    cycle {ac+1} / {pp_kw}kW: "
                  f"离均差均值={avg_dev:.1f}mV "
                  f"电压均值={avg_cv:.1f}mV "
                  f"(预警类型={at})")

    return out_path


def validate_vehicle_data(path: str) -> None:
    """验证整车 mock 数据格式和内容。"""
    print()
    print("=" * 60)
    print("验证整车 mock 数据...")
    print("=" * 60)

    df = pd.read_csv(path)
    print(f"  文件: {path}")
    print(f"  行数: {len(df):,}")
    print(f"  列数: {len(df.columns)}")
    print(f"  列名: {list(df.columns)}")

    # 检查必需列
    required = ["Timestamp", "FC_CurrOut", "FC_VoltOut", "FC_NetPwrOut",
                "FC_AvgCellVoltage", "FC_AvgCellVoltDev", "FC_MainSts"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        print(f"  ⚠ 缺失列: {missing}")
    else:
        print("  ✅ 所有必需列完整")

    # 检查稳态段
    if "_segment" in df.columns:
        from collections import Counter
        seg_counts = Counter(df["_segment"])
        steady = {k: v for k, v in seg_counts.items() if k.startswith("steady")}
        print(f"  稳态段: {len(steady)} 段")
        for name, cnt in sorted(steady.items()):
            if cnt >= 180:
                print(f"    ✅ {name}: {cnt}s ≥ 180s (有效稳态段)")
            else:
                print(f"    ⚠ {name}: {cnt}s < 180s (过短)")

    print(f"  电流范围: {df['FC_CurrOut'].min():.1f} ~ {df['FC_CurrOut'].max():.1f} A")
    print(f"  电压范围: {df['FC_VoltOut'].min():.1f} ~ {df['FC_VoltOut'].max():.1f} V")
    print(f"  平均单体电压: {df['FC_AvgCellVoltage'].min():.3f} ~ {df['FC_AvgCellVoltage'].max():.3f} V")


def validate_bench_data(path: str) -> None:
    """验证台架 mock 数据格式和预警点。"""
    print()
    print("=" * 60)
    print("验证台架 mock 数据...")
    print("=" * 60)

    df = pd.read_csv(path)
    print(f"  文件: {path}")
    print(f"  行数: {len(df):,}")

    # 必需列
    required = ["Timestamp", "FC_NetPwrOut", "FC_AvgCellVoltage",
                "FC_AvgCellVoltDev", "cycle_id", "power_point"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        print(f"  ⚠ 缺失列: {missing}")
    else:
        print("  ✅ 所有必需列完整")

    # 检查周期
    stable = df[df["cycle_id"] >= 0]
    n_cycles = stable["cycle_id"].nunique()
    print(f"  循环数: {n_cycles}")
    pp_counts = stable.groupby("cycle_id")["power_point"].nunique()
    print(f"  每循环功率点数: {pp_counts.to_dict()}")

    # 预警点验证
    print("\n  🚨 预警验证 (阈值: 离均差>50mV / 电压<600mV):")
    stable_copy = stable.copy()
    stable_copy["FC_AvgCellVoltage"] = stable_copy["FC_AvgCellVoltage"]  # 已在 mV

    for (_, row) in stable.groupby(["cycle_id", "power_point"]):
        avg_dev = row["FC_AvgCellVoltDev"].mean()
        avg_cv = row["FC_AvgCellVoltage"].mean()
        cid = int(row["cycle_id"].iloc[0])
        pp = float(row["power_point"].iloc[0])

        hits = []
        if avg_dev > 50:
            hits.append(f"离均差={avg_dev:.1f}mV > 50mV")
        if avg_cv < 600:
            hits.append(f"电压={avg_cv:.1f}mV < 600mV")

        label = row["_alert_type"].iloc[0] if "_alert_type" in row.columns else "?"
        if hits:
            print(f"    ✅ cycle {cid+1} / {pp}kW: {', '.join(hits)} (预期={label})")
        elif label == "normal":
            pass  # 正常点不打印
        else:
            print(f"    ℹ cycle {cid+1} / {pp}kW: 无预警 (离均差={avg_dev:.1f}mV, 电压={avg_cv:.1f}mV)")

    print()
    print("=" * 60)
    print("✅ 数据生成完成!")
    print("使用方法:")
    print("  1. 启动 Streamlit: streamlit run app.py")
    print("  2. 上传生成的 CSV 文件到侧边栏")
    print("  3. 或直接访问 tests/fixtures 目录")
    print("  4. 性能统计 Tab 会自动读取稳态段")
    print("  5. 台架耐久 Tab 会自动触发预警")


if __name__ == "__main__":
    vehicle_path = generate_vehicle_mock()
    bench_path = generate_bench_mock()

    validate_vehicle_data(vehicle_path)
    validate_bench_data(bench_path)
