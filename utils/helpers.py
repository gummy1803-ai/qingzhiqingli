"""信号中文映射与数据处理工具函数。

提供:
- SIGNAL_MAP: 信号英文列名 → 中文显示名
- resample_data: 按间隔重采样(数值列均值/通道号 last)
- filter_by_time: 时间范围过滤(不足 10 条自动扩展前后各 5 秒)
- detect_anomalies: 异常点标记(最小单体电压 < 3.0V 或 绝缘电阻 < 500kΩ)
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

# ---------- 信号中文映射 ----------

SIGNAL_MAP: dict[str, str] = {
    'FC_CurrOut': '电堆输出电流 (A)',
    'FC_VoltOut': '电堆输出电压 (V)',
    'FC_NetPwrOut': '系统净功率 (kW)',
    'FC_MinCellVoltage': '最小单体电压 (V)',
    'FC_MinVoltageChannel': '最小电压通道',
    'FC_AvgCellVoltage': '平均单体电压 (V)',
    'FC_AvgCellVoltDev': '离均差 (V)',
    'FC_VehicleIsolationR': '绝缘电阻 (kΩ)',
    'FC_RunTime_Hours': '运行时间 (h)',
}

# 通道号列(重采样时用 last 聚合,而非均值)
_CHANNEL_COL = 'FC_MinVoltageChannel'
# 时间戳列名
_TIMESTAMP_COL = 'Timestamp'
# pandas 2.2+ 弃用大写频率别名,这里做旧->新兼容映射(秒/分/时)
_FREQ_ALIAS: dict[str, str] = {'S': 's', 'T': 'min', 'H': 'h'}


def _modernize_freq(interval: str) -> str:
    """将旧式大写频率别名转为 pandas 2.2+ 新式(如 '1S'->'1s','1H'->'1h')。

    未知别名原样返回,交由 pandas 自行解析。
    """
    import re
    m = re.match(r'^(\d*)([A-Za-z]+)$', interval.strip())
    if not m:
        return interval
    n, u = m.groups()
    if u in _FREQ_ALIAS:
        return (n or '1') + _FREQ_ALIAS[u]
    return interval


def resample_data(df: pd.DataFrame, interval: str = '1S') -> pd.DataFrame:
    """按指定间隔重采样:数值列均值聚合,通道号 last 聚合。

    Args:
        df: 含 Timestamp 列的整车数据
        interval: pandas 频率字符串,默认 '1S'(1 秒)

    Returns:
        重采样后的 DataFrame(Timestamp 为索引,删除全空区间);
        空输入或缺 Timestamp 列返回空 DataFrame。
    """
    if df is None or len(df) == 0 or _TIMESTAMP_COL not in df.columns:
        return pd.DataFrame()

    work = df.copy()
    work[_TIMESTAMP_COL] = pd.to_datetime(work[_TIMESTAMP_COL], errors='coerce')
    work = work.dropna(subset=[_TIMESTAMP_COL]).set_index(_TIMESTAMP_COL)

    # 按列类型选择聚合方式:通道号 last,数值列 mean,其余列 last(保留末值)
    agg_map: dict[str, str] = {}
    for col in work.columns:
        if col == _CHANNEL_COL:
            agg_map[col] = 'last'
        elif pd.api.types.is_numeric_dtype(work[col]):
            agg_map[col] = 'mean'
        else:
            agg_map[col] = 'last'

    resampled = work.resample(_modernize_freq(interval)).agg(agg_map)
    return resampled.dropna(how='all')


def filter_by_time(
    df: pd.DataFrame,
    start: datetime,
    end: datetime,
) -> pd.DataFrame:
    """根据时间范围过滤数据;不足 10 条则前后各扩展 5 秒。

    边界扩展用于避免筛选区间过窄时无数据显示。

    Args:
        df: 含 Timestamp 列的整车数据
        start: 起始时间
        end: 结束时间

    Returns:
        过滤后的 DataFrame(重置索引);空输入返回空 DataFrame。
    """
    if df is None or len(df) == 0 or _TIMESTAMP_COL not in df.columns:
        return pd.DataFrame()

    work = df.copy()
    work[_TIMESTAMP_COL] = pd.to_datetime(work[_TIMESTAMP_COL], errors='coerce')
    work = work.dropna(subset=[_TIMESTAMP_COL])

    s = pd.Timestamp(start)
    e = pd.Timestamp(end)
    sub = work[(work[_TIMESTAMP_COL] >= s) & (work[_TIMESTAMP_COL] <= e)]

    # 数据不足 10 条,前后各扩展 5 秒,避免边界无数据显示
    if len(sub) < 10:
        s2 = s - pd.Timedelta(seconds=5)
        e2 = e + pd.Timedelta(seconds=5)
        sub = work[(work[_TIMESTAMP_COL] >= s2) & (work[_TIMESTAMP_COL] <= e2)]

    return sub.reset_index(drop=True)


def detect_anomalies(df: pd.DataFrame) -> pd.DataFrame:
    """标记异常点:最小单体电压 < 3.0V 或 绝缘电阻 < 500kΩ。

    Args:
        df: 整车数据(含 FC_MinCellVoltage / FC_VehicleIsolationR 列)

    Returns:
        添加 'is_anomaly' (bool) 列后的 DataFrame。
    """
    out = df.copy()
    if len(out) == 0:
        out['is_anomaly'] = pd.Series(dtype=bool)
        return out

    cond = pd.Series(False, index=out.index)
    if 'FC_MinCellVoltage' in out.columns:
        v = pd.to_numeric(out['FC_MinCellVoltage'], errors='coerce')
        cond = cond | (v < 3.0)
    if 'FC_VehicleIsolationR' in out.columns:
        r = pd.to_numeric(out['FC_VehicleIsolationR'], errors='coerce')
        cond = cond | (r < 500)

    out['is_anomaly'] = cond.fillna(False).astype(bool)
    return out
