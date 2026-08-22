"""绝缘阻值数据处理模块:清洗 + 状态区分 + 重采样聚合。

输入整车时序数据,输出每 N 分钟(默认10分钟)按运行状态区分的
最小绝缘阻值,用于绝缘老化趋势分析。

状态约定(FC_MainSts):
    4 = 运行态(is_running=True)
    8 = 上电非运行态(is_running=False)

核心函数: process_insulation_data
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ---------- 清洗常量 ----------
_SENSOR_FAULT = 65535        # 传感器故障默认值
_OVERFLOW = 9999             # 溢出值阈值(>= 此值视为无效)
_VALID_STATES = (4, 8)       # 只保留 运行态(4) / 上电非运行态(8)


def _clean_insulation(df: pd.DataFrame,
                      value_col: str = 'FC_VehicleIsolationR',
                      state_col: str = 'FC_MainSts') -> pd.DataFrame:
    """清洗绝缘数据:剔除无效值 + 只保留有效状态。

    规则:
        1. FC_VehicleIsolationR <= 0 剔除
        2. FC_VehicleIsolationR == 65535(传感器故障) 剔除
        3. FC_VehicleIsolationR >= 9999(溢出) 剔除(企业要求的坏值过滤)
        4. FC_MainSts 只保留 4 或 8

    附加: 返回 DataFrame.attrs 记录清洗统计(供上层展示坏值摘要):
        {'raw_rows', 'kept_rows', 'bad_le0', 'bad_65535', 'bad_ge9999', 'bad_state'}
    """
    n0 = len(df)
    stats: dict = {'raw_rows': n0, 'kept_rows': 0, 'bad_le0': 0, 'bad_65535': 0,
                   'bad_ge9999': 0, 'bad_state': 0}
    if value_col not in df.columns or state_col not in df.columns:
        logger.error("清洗: 缺列 %s/%s (现有列: %s)",
                     value_col, state_col, list(df.columns))
        out = df.iloc[0:0].copy()
        out.attrs.update(stats)
        return out

    v = pd.to_numeric(df[value_col], errors='coerce')
    s = pd.to_numeric(df[state_col], errors='coerce')

    # 细分坏值计数(企业要求: 65535 / >=9999 两类坏值单独追踪)
    m_le0 = (v <= 0) | v.isna()
    m_65535 = (v == _SENSOR_FAULT)
    m_ge9999 = (v >= _OVERFLOW)
    stats['bad_le0'] = int(m_le0.sum())
    stats['bad_65535'] = int(m_65535.sum())
    stats['bad_ge9999'] = int(m_ge9999.sum())

    # 规则1-3: 值有效性
    mask_valid = (~m_le0) & (~m_65535) & (~m_ge9999)
    n_invalid = int((~mask_valid).sum())
    # 规则4: 状态有效性
    mask_state = s.isin(_VALID_STATES)
    n_bad_state = int(mask_valid.sum() - (mask_valid & mask_state).sum())
    stats['bad_state'] = n_bad_state

    mask = mask_valid & mask_state
    out = df.loc[mask].copy()
    stats['kept_rows'] = len(out)
    out.attrs.update(stats)
    logger.info(
        "清洗: 输入 %d 行 -> 保留 %d 行 "
        "(剔除: <=0/NaN %d, ==65535 %d, >=9999 %d, 非状态4/8 %d)",
        n0, len(out), stats['bad_le0'], stats['bad_65535'],
        stats['bad_ge9999'], stats['bad_state'])
    return out


def process_insulation_data(
    df: pd.DataFrame,
    interval_minutes: int = 10,
    timestamp_col: str = 'Timestamp',
    value_col: str = 'FC_VehicleIsolationR',
    state_col: str = 'FC_MainSts',
) -> pd.DataFrame:
    """清洗绝缘数据并按时间窗口+状态聚合最小值。

    Args:
        df: 原始整车时序数据,需含 Timestamp/FC_VehicleIsolationR/FC_MainSts
        interval_minutes: 聚合窗口分钟数(默认10)
        timestamp_col: 时间戳列名
        value_col: 绝缘阻值列名
        state_col: 主状态列名

    Returns:
        DataFrame, 每个窗口×每个有效状态一行:
        - timestamp: 窗口起始时间(每 interval 分钟一个点)
        - FC_VehicleIsolationR: 该窗口该状态的最小绝缘值(无数据记 NaN,不填充)
        - FC_MainSts: 状态(4 或 8)
        - is_running: True=运行态(状态4), False=上电非运行态(状态8)
        DataFrame.attrs.clean_stats = _clean_insulation 的清洗统计(坏值计数)
    """
    logger.info("绝缘处理: 输入 %d 行, 窗口=%d 分钟", len(df), interval_minutes)
    default_cols = ['timestamp', value_col, state_col, 'is_running']
    if df is None or len(df) == 0:
        logger.warning("绝缘处理: 输入为空")
        empty = pd.DataFrame(columns=default_cols)
        empty.attrs['clean_stats'] = {'raw_rows': 0, 'kept_rows': 0,
            'bad_le0': 0, 'bad_65535': 0, 'bad_ge9999': 0, 'bad_state': 0}
        return empty

    # 1. 清洗
    clean = _clean_insulation(df, value_col, state_col)
    clean_stats = dict(clean.attrs)
    if len(clean) == 0:
        logger.warning("绝缘处理: 清洗后无有效数据")
        empty = pd.DataFrame(columns=default_cols)
        empty.attrs['clean_stats'] = clean_stats
        return empty

    # 2. 时间戳转 datetime
    clean = clean.copy()
    clean[timestamp_col] = pd.to_datetime(clean[timestamp_col],
                                           errors='coerce')
    clean = clean.dropna(subset=[timestamp_col])
    # 数值化(清洗时已转,但 copy 后重新确保)
    clean[value_col] = pd.to_numeric(clean[value_col], errors='coerce')
    clean[state_col] = pd.to_numeric(clean[state_col], errors='coerce')

    # 3. 按 窗口×状态 分组取最小值
    freq = f'{interval_minutes}min'
    grouper = pd.Grouper(key=timestamp_col, freq=freq)
    agg = (clean.groupby([grouper, state_col])[value_col]
           .min()
           .rename('FC_VehicleIsolationR'))
    # agg 是 Series, MultiIndex (timestamp, FC_MainSts)

    # 4. 构建完整 (窗口×{4,8}) 笛卡尔积,缺失记 NaN(不填充,保留空白)
    t_min = clean[timestamp_col].min().floor(f'{interval_minutes}min')
    t_max = clean[timestamp_col].max().floor(f'{interval_minutes}min')
    all_windows = pd.date_range(start=t_min, end=t_max, freq=freq)
    full_idx = pd.MultiIndex.from_product([all_windows, list(_VALID_STATES)],
                                          names=[timestamp_col, state_col])
    agg = agg.reindex(full_idx)
    logger.info("聚合: %d 个窗口 x %d 状态 = %d 组合, "
                "其中绝缘有效 %d, 缺失(NaN) %d",
                len(all_windows), len(_VALID_STATES), len(full_idx),
                int(agg.notna().sum()), int(agg.isna().sum()))

    # 5. 整理输出
    out = agg.reset_index()
    out = out.rename(columns={timestamp_col: 'timestamp'})
    out['is_running'] = out[state_col] == 4  # 4=运行态 True, 8=False
    out = out[['timestamp', 'FC_VehicleIsolationR', state_col, 'is_running']]
    out = out.sort_values(['timestamp', state_col]).reset_index(drop=True)
    # 透传清洗统计 attrs(坏值计数摘要,供绝缘Tab的指标卡片展示)
    out.attrs['clean_stats'] = clean_stats
    logger.info("绝缘处理完成: 输出 %d 行 (窗口数=%d), 清洗统计=%s",
                len(out), len(all_windows), clean_stats)
    return out


# ---------- 单元测试 ----------

if __name__ == '__main__':
    import logging as _lg
    _lg.basicConfig(level=_lg.INFO,
                    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')

    rng = np.random.default_rng(42)

    def _make_df(rows):
        """rows: list of (minute_offset, value, state)"""
        ts = [pd.Timestamp('2026-08-22 00:00:00') + pd.Timedelta(minutes=m)
              for m, _, _ in rows]
        vals = [v for _, v, _ in rows]
        sts = [s for _, _, s in rows]
        return pd.DataFrame({'Timestamp': ts,
                             'FC_VehicleIsolationR': vals,
                             'FC_MainSts': sts})

    print("===== 测试1: 清洗规则(剔 0/65535/9999+ /非4/8状态) =====")
    rows = []
    # 0-9分钟,每分钟1条,状态4/8交替,正常值 500-2000
    for m in range(10):
        rows.append((m, 1000 + rng.integers(0, 500), 4 if m % 2 == 0 else 8))
    # 注入无效值
    rows.append((10, 0, 4))           # <=0 剔除
    rows.append((11, 65535, 4))       # 传感器故障 剔除
    rows.append((12, 9999, 8))        # >=9999 剔除
    rows.append((13, 1500, 2))        # 非状态4/8 剔除
    rows.append((13, 1500, 7))        # 非状态4/8 剔除
    df1 = _make_df(rows)
    r1 = process_insulation_data(df1, interval_minutes=10)
    # 0-9分钟有效(10条),13状态4/8但同一窗口; 10-13无效/非状态剔除
    # 窗口: 00:00(0-9分钟), 10:00(10-19), 状态4/8各一行
    print(f"  输出行数={len(r1)}")
    print(r1.to_string(index=False))
    # 13分钟状态4/8的1500在 10:00 窗口(10-19分钟区间)
    # 检查无 0/65535/9999 出现
    assert (r1['FC_VehicleIsolationR'] > 0).all() or r1['FC_VehicleIsolationR'].isna().any()
    assert not (r1['FC_VehicleIsolationR'] == 65535).any()
    assert not (r1['FC_VehicleIsolationR'] >= 9999).any()
    # 状态只含4/8
    assert set(r1['FC_MainSts'].dropna().unique()).issubset({4, 8})
    print("  [PASS] 清洗剔除无效值+非4/8状态")

    print("\n===== 测试2: 10分钟窗口取最小值 =====")
    # 0-9分钟状态4,值递减(2000->100),min应=100
    rows2 = [(m, 2000 - m * 190, 4) for m in range(10)]
    # 10-19分钟状态4,值递增(100->2000),min应=100
    rows2 += [(10 + m, 100 + m * 190, 4) for m in range(10)]
    df2 = _make_df(rows2)
    r2 = process_insulation_data(df2, interval_minutes=10)
    print(r2.to_string(index=False))
    win0 = r2[r2['timestamp'] == pd.Timestamp('2026-08-22 00:00:00')]
    win1 = r2[r2['timestamp'] == pd.Timestamp('2026-08-22 00:10:00')]
    # 00:00 窗口(0-9分)状态4 min=100 (2000-9*190=290? 等下 m=0..9: 2000,1810,...,290, min=290)
    # 重新算: 2000-0*190=2000, 2000-9*190=290, min=290
    assert abs(win0['FC_VehicleIsolationR'].iloc[0] - 290) < 1, \
        f"窗口0 min应=290,实际{win0['FC_VehicleIsolationR'].iloc[0]}"
    # 00:10 窗口(10-19分)状态4 min=100 (100+0*190=100)
    assert abs(win1['FC_VehicleIsolationR'].iloc[0] - 100) < 1, \
        f"窗口1 min应=100,实际{win1['FC_VehicleIsolationR'].iloc[0]}"
    print("  [PASS] 10分钟窗口取最小值")

    print("\n===== 测试3: 双状态区分(同窗口4和8各一行) =====")
    rows3 = []
    for m in range(10):
        rows3.append((m, 800 + m * 10, 4))   # 状态4 递增
        rows3.append((m, 1200 - m * 5, 8))  # 状态8 递减
    df3 = _make_df(rows3)
    r3 = process_insulation_data(df3, interval_minutes=10)
    win = r3[r3['timestamp'] == pd.Timestamp('2026-08-22 00:00:00')]
    print(win.to_string(index=False))
    # 同窗口应有2行(状态4和8)
    assert len(win) == 2, f"同窗口应有2行,实际{len(win)}"
    s4 = win[win['FC_MainSts'] == 4]
    s8 = win[win['FC_MainSts'] == 8]
    assert len(s4) == 1 and len(s8) == 1
    # 状态4 min=800, 状态8 min=1200-9*5=1155
    assert abs(s4['FC_VehicleIsolationR'].iloc[0] - 800) < 1
    assert abs(s8['FC_VehicleIsolationR'].iloc[0] - 1155) < 1
    # is_running: 4=True, 8=False
    assert s4['is_running'].iloc[0] == True
    assert s8['is_running'].iloc[0] == False
    print("  [PASS] 双状态分别取最小 + is_running 标记正确")

    print("\n===== 测试4: 缺失NaN(某窗口某状态无数据,不填充) =====")
    # 0-9分钟只有状态4,无状态8 -> 00:00窗口状态8应为NaN
    rows4 = [(m, 1000 + m, 4) for m in range(10)]
    # 10-19分钟只有状态8
    rows4 += [(10 + m, 2000 - m, 8) for m in range(10)]
    df4 = _make_df(rows4)
    r4 = process_insulation_data(df4, interval_minutes=10)
    print(r4.to_string(index=False))
    win0 = r4[r4['timestamp'] == pd.Timestamp('2026-08-22 00:00:00')]
    # 00:00窗口: 状态4有值, 状态8应NaN
    s8_na = win0[win0['FC_MainSts'] == 8]
    assert len(s8_na) == 1, "状态8行应存在(缺失NaN)"
    assert pd.isna(s8_na['FC_VehicleIsolationR'].iloc[0]), \
        "00:00窗口状态8无数据应为NaN"
    # 00:10窗口: 状态8有值, 状态4应NaN
    win1 = r4[r4['timestamp'] == pd.Timestamp('2026-08-22 00:10:00')]
    s4_na = win1[win1['FC_MainSts'] == 4]
    assert pd.isna(s4_na['FC_VehicleIsolationR'].iloc[0]), \
        "00:10窗口状态4无数据应为NaN"
    print("  [PASS] 缺失状态记NaN,不填充保留空白")

    print("\n===== 测试5: 边界容错(空/缺列) =====")
    assert len(process_insulation_data(pd.DataFrame())) == 0
    miss = process_insulation_data(
        pd.DataFrame({'Timestamp': [pd.Timestamp('2026-08-22')],
                      'FC_VehicleIsolationR': [1000]}))  # 缺 FC_MainSts
    assert len(miss) == 0
    # 全部无效值
    allbad = _make_df([(0, 65535, 4), (1, 0, 8), (2, 9999, 4)])
    assert len(process_insulation_data(allbad)) == 0
    print("  [PASS] 空数据/缺列/全无效值容错")

    print("\n===== 测试6: 自定义窗口(5分钟) =====")
    rows6 = [(m, 1000 + m * 10, 4) for m in range(20)]
    df6 = _make_df(rows6)
    r6 = process_insulation_data(df6, interval_minutes=5)
    # 20分钟 / 5分钟 = 4个窗口
    n_win = r6['timestamp'].nunique()
    print(f"  5分钟窗口: {n_win} 个 (20分钟数据)")
    assert n_win == 4, f"应有4个窗口,实际{n_win}"
    print("  [PASS] 自定义窗口大小")

    print("\n[OK] 全部测试通过")
