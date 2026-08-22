"""生成含异常的测试 CSV(用于数据质量分析模块的异常检测验证)。

特征:
- 不均匀采样间隔(1/2/3/5秒混合,体现真实抖动)
- 3 处 3σ 异常注入:电压骤降 / 绝缘掉低 / 电流飙升
- 3 处缺失值注入(不同列)
- 时间范围:2026-08-22 00:00 起 ~80 秒(落在默认筛选 1h 内)
"""
import numpy as np
import pandas as pd
from pathlib import Path

rng = np.random.default_rng(42)
n = 50
# 不均匀采样间隔(1/2/3/5秒混合,体现真实数据抖动)
gaps = [1, 1, 2, 1, 5, 1, 2, 3, 1, 1] * 5
gaps = gaps[: n - 1]
ts = [pd.Timestamp("2026-08-22 00:00:00")]
for g in gaps:
    ts.append(ts[-1] + pd.Timedelta(seconds=g))

curr = rng.uniform(150, 400, n)
volt = rng.uniform(280, 380, n)
df = pd.DataFrame({
    "Timestamp": ts,
    "FC_CurrOut": np.round(curr, 2),
    "FC_VoltOut": np.round(volt, 2),
    "FC_NetPwrOut": np.round(curr * volt / 1000, 2),
    "FC_MinCellVoltage": np.round(rng.uniform(3.3, 3.9, n), 3),
    "FC_MinVoltageChannel": rng.integers(1, 121, n),
    "FC_AvgCellVoltage": np.round(rng.uniform(3.3, 3.9, n), 3),
    "FC_AvgCellVoltDev": np.round(rng.uniform(-0.3, 0.3, n), 3),
    # 绝缘电阻:收窄正常分布到 1000-1500,使 3σ 下限为正数,从而让 300 异常可被检出
    "FC_VehicleIsolationR": np.round(rng.uniform(1000, 1500, n), 1),
    "FC_RunTime_Hours": np.round(np.cumsum(np.full(n, 1 / 3600)), 4),
})
# 注入 3σ 异常点(数据质量模块应检出)
df.loc[10, "FC_MinCellVoltage"] = 0.5      # 电压骤降(3σ 可检出)
df.loc[20, "FC_VehicleIsolationR"] = 300   # 绝缘掉低(正常分布收窄后,3σ 下限为正,可被检出)
df.loc[30, "FC_CurrOut"] = 2000            # 电流飙升(极端值,3σ 可检出)
# 注入缺失值(各列缺失率计算应检出)
df.loc[15, "FC_VoltOut"] = np.nan
df.loc[25, "FC_CurrOut"] = np.nan
df.loc[35, "FC_MinCellVoltage"] = np.nan

out = Path(__file__).parent / "fixtures" / "anomaly_test.csv"
out.parent.mkdir(parents=True, exist_ok=True)
df.to_csv(out, index=False)
print(f"生成: {out} ({len(df)} 行)")
print("缺失:", df.isna().sum().to_dict())
print("异常注入: 行10电压0.5 / 行20绝缘300 / 行30电流2000")
