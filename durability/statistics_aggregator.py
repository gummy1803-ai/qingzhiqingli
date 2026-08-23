"""耐久数据统计聚合模块。

对 parse_durability_data 输出的增强 DataFrame,按 [cycle_id, power_point]
分组聚合,计算各信号的统计值(均值/中位数/最值),并评估每组的数据
稳定性和数据量,用于耐久循环间衰减趋势分析。

核心函数: aggregate_durability_stats
"""
from __future__ import annotations

import logging
from typing import List, Dict, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# 默认最小数据条数(少于此值视为"数据不足")
_DEFAULT_MIN_COUNT = 10
# 默认波动异常阈值(None=不检查;调用方可显式传入,如 0.005 表示 5mV)
_DEFAULT_VOL_THRESHOLD: Optional[float] = None

# 支持的聚合方法映射(numpy 容 NaN 实现)
_AGG_FUNCS = {
    'mean': np.nanmean,
    'median': np.nanmedian,
    'min': np.nanmin,
    'max': np.nanmax,
}


# ---------- 内部工具函数 ----------

def _normalize_signal_columns(
    df: pd.DataFrame,
    signal_columns: List[str],
) -> List[str]:
    """过滤出 df 中实际存在的信号列,缺失的列警告并跳过。"""
    valid = [c for c in signal_columns if c in df.columns]
    missing = [c for c in signal_columns if c not in df.columns]
    if missing:
        logger.warning("信号列缺失将被跳过: %s", missing)
    return valid


def _filter_stable(df: pd.DataFrame) -> pd.DataFrame:
    """过滤出稳定数据:is_stable=True 且 point_status != 'invalid'。

    缺少 is_stable 列时回退到全部数据(并 WARNING)。
    """
    has_is_stable = 'is_stable' in df.columns
    has_status = 'point_status' in df.columns
    if not has_is_stable:
        logger.warning("df 缺少 is_stable 列,无法过滤稳定段,将使用全部数据")
        return df.copy()

    mask = df['is_stable'].fillna(False).astype(bool)
    if has_status:
        invalid_mask = df['point_status'].fillna('') == 'invalid'
        before = int(mask.sum())
        mask = mask & (~invalid_mask)
        excluded = before - int(mask.sum())
        if excluded > 0:
            logger.info("排除 invalid 点: %d 个", excluded)
    return df[mask].copy()


# ---------- 主函数 ----------

def aggregate_durability_stats(
    df: pd.DataFrame,
    signal_columns: List[str],
    agg_method: str = 'mean',
    min_data_count: int = _DEFAULT_MIN_COUNT,
    volatility_threshold: Optional[float] = _DEFAULT_VOL_THRESHOLD,
) -> pd.DataFrame:
    """按 [cycle_id, power_point] 分组聚合信号统计值。

    Args:
        df: parse_durability_data 返回的增强 DataFrame,需含 cycle_id/power_point/
            is_stable/point_status 列
        signal_columns: 需统计的信号列,如 ['FC_AvgCellVoltage','FC_NetPwrOut']
        agg_method: 聚合方法,可选 'mean'/'median'/'min'/'max'
        min_data_count: 最小数据条数阈值,少于此值标记 '数据不足'
        volatility_threshold: 任一信号 std > 此值则标记 '波动异常';
                              None=不检查波动异常

    Returns:
        DataFrame,每行一个 (cycle_id, power_point) 组合,列:
        - cycle_id                 : 循环编号
        - power_point              : 功率点 (kW)
        - <signal>_<agg_method>    : 主统计值
        - <signal>_std             : 标准差(稳定性评分)
        - 数据量                   : 该组有效数据条数
        - 质量标记                 : '正常' / '数据不足' / '波动异常'
    """
    logger.info("=== 耐久聚合开始 rows=%d signals=%s agg=%s "
                "min_count=%d vol_thresh=%s ===",
                len(df), signal_columns, agg_method,
                min_data_count, volatility_threshold)

    # ---------- 输入校验 ----------
    if agg_method not in _AGG_FUNCS:
        logger.error("不支持的 agg_method: %s (支持: %s)",
                     agg_method, list(_AGG_FUNCS.keys()))
        raise ValueError(f"不支持的 agg_method: {agg_method}"
                         f" (支持: {list(_AGG_FUNCS.keys())})")

    empty_cols = ['cycle_id', 'power_point', '数据量', '质量标记']
    if len(df) == 0:
        logger.warning("输入数据为空,返回空 DataFrame")
        return pd.DataFrame(columns=empty_cols)

    required = ['cycle_id', 'power_point']
    missing = [c for c in required if c not in df.columns]
    if missing:
        logger.error("df 缺少必要列: %s (需要 cycle_id/power_point)", missing)
        raise ValueError(f"df 缺少必要列: {missing}")

    # ---------- 过滤稳定数据 ----------
    df_stable = _filter_stable(df)
    logger.info("稳定数据过滤: %d -> %d (剔除 %d 非稳定/invalid)",
                len(df), len(df_stable), len(df) - len(df_stable))

    # 过滤缺失 cycle_id/power_point,以及 cycle_id=-1(不完整循环/过渡)
    before = len(df_stable)
    df_stable = df_stable.dropna(subset=['cycle_id', 'power_point'])
    n_neg = int((df_stable['cycle_id'] == -1).sum())
    if n_neg > 0:
        logger.info("排除 cycle_id=-1(不完整循环/过渡数据): %d 行", n_neg)
    df_stable = df_stable[df_stable['cycle_id'] >= 0]
    logger.info("cycle_id 校验: %d -> %d (剔除 NaN/负值 %d)",
                before, len(df_stable), before - len(df_stable))

    if len(df_stable) == 0:
        logger.warning("过滤后无有效数据,返回空 DataFrame")
        return pd.DataFrame(columns=empty_cols)

    # ---------- 校验信号列 ----------
    valid_signals = _normalize_signal_columns(df_stable, signal_columns)
    if not valid_signals:
        logger.error("df 中无任何指定信号列: %s", signal_columns)
        raise ValueError("无有效信号列可统计")

    # ---------- 分组聚合 ----------
    groups = df_stable.groupby(['cycle_id', 'power_point'], dropna=False)
    n_groups = len(groups)
    logger.info("分组数: %d (cycle_id x power_point 组合)", n_groups)

    agg_func = _AGG_FUNCS[agg_method]
    rows: List[Dict] = []
    for (cid, pp), g in groups:
        row: Dict = {
            'cycle_id': int(cid),
            'power_point': float(pp),
            '数据量': int(len(g)),
        }

        # ---------- 质量标记 ----------
        quality = '正常'
        if len(g) < min_data_count:
            quality = '数据不足'

        # ---------- 各信号统计 ----------
        is_volatile = False
        for col in valid_signals:
            s = pd.to_numeric(g[col], errors='coerce').dropna()
            if len(s) == 0:
                row[f'{col}_{agg_method}'] = None
                row[f'{col}_std'] = None
                logger.warning("组 (cid=%s, pp=%s) 信号 %s 全部 NaN",
                               cid, pp, col)
                continue
            try:
                m = float(agg_func(s.to_numpy()))
            except Exception as e:
                logger.warning("组 (cid=%s, pp=%s) 信号 %s %s 失败: %s",
                               cid, pp, col, agg_method, e)
                m = None
            sd = float(s.std()) if len(s) > 1 else 0.0
            row[f'{col}_{agg_method}'] = round(m, 4) if m is not None else None
            row[f'{col}_std'] = round(sd, 4)
            # 检查波动
            if (volatility_threshold is not None
                    and sd > volatility_threshold):
                is_volatile = True
                logger.info("  组 (cid=%s, pp=%s) 信号 %s std=%.4f > 阈值 %s",
                            cid, pp, col, sd, volatility_threshold)

        # 波动异常优先级低于数据不足(数据不足更严重)
        if is_volatile and quality != '数据不足':
            quality = '波动异常'
        row['质量标记'] = quality
        rows.append(row)
        logger.info("  组 (cid=%s, pp=%s): n=%d quality=%s%s",
                    cid, pp, len(g), quality,
                    f" [波动={is_volatile}]" if is_volatile else "")

    df_out = pd.DataFrame(rows)
    n_insuf = int((df_out['质量标记'] == '数据不足').sum())
    n_vola = int((df_out['质量标记'] == '波动异常').sum())
    n_normal = int((df_out['质量标记'] == '正常').sum())
    logger.info("=== 耐久聚合结束: 输出 %d 组 (正常=%d 数据不足=%d 波动异常=%d) ===",
                len(df_out), n_normal, n_insuf, n_vola)
    return df_out


# ---------- 单元测试 ----------

if __name__ == '__main__':
    import sys
    from pathlib import Path
    # 直接运行脚本时,把项目根加入 sys.path,以便 import 跨包模块
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    from durability.data_parser import parse_durability_data
    from src.log_config import setup_logging
    setup_logging(level=logging.INFO)

    rng = np.random.default_rng(42)

    # ---------- 公共测试数据:1 完整循环 / 6 功率点 / 每段 300s ----------
    n_per = 300
    pps = [33.0, 58.5, 117.0, 156.0, 175.5, 195.0]
    n_total = n_per * len(pps)
    ts_arr = pd.date_range('2026-08-22 00:00:00',
                            periods=n_total, freq='1s')
    pwr_arr = np.concatenate([np.full(n_per, p) for p in pps])
    # 电压:每段稳定,微小噪声 ±1mV
    volt = np.concatenate([
        np.full(n_per, 3.6) + rng.normal(0, 0.001, n_per) for _ in pps
    ])
    df_test = pd.DataFrame({
        'Timestamp': ts_arr,
        'FC_NetPwrOut': pwr_arr,
        'FC_AvgCellVoltage': volt,
        'FC_VoltOut': np.full(n_total, 320.0),
    })
    parsed = parse_durability_data(df_test)
    sigs = ['FC_AvgCellVoltage', 'FC_VoltOut']

    print("\n===== 测试1: 正常聚合 (1 循环 / 6 功率点) =====")
    agg = aggregate_durability_stats(parsed, sigs, agg_method='mean')
    print(agg[['cycle_id', 'power_point', '数据量', '质量标记',
              'FC_AvgCellVoltage_mean', 'FC_VoltOut_mean']].to_string(index=False))
    assert len(agg) == 6, f"应输出 6 行,实际 {len(agg)}"
    assert (agg['质量标记'] == '正常').all(), "所有组应正常"
    assert (agg['数据量'] >= 10).all(), "每组数据量应 >= 10"
    # 每段 300 点,去前后 10% (30+30) 后剩 240 点
    assert (agg['数据量'] == 240).all(), \
        f"每组数据量应=240,实际 {agg['数据量'].tolist()}"
    print(f"  [PASS] 6 行输出,全部 '正常',每组数据量=240")

    print("\n===== 测试2: 不同 agg_method =====")
    for method in ['mean', 'median', 'min', 'max']:
        a = aggregate_durability_stats(parsed, ['FC_VoltOut'],
                                         agg_method=method)
        # FC_VoltOut 恒定 320,各种 method 应该都接近 320
        val = a['FC_VoltOut_' + method].iloc[0]
        assert abs(val - 320.0) < 0.1, \
            f"{method} 应≈320,实际 {val}"
        print(f"  {method}: 第一组 FC_VoltOut_{method}={val}")
    print(f"  [PASS] mean/median/min/max 全部正常")

    print("\n===== 测试3: 过渡段数据被排除 =====")
    # 6 个功率点 + 5 个 30s 过渡段(45kW 不在任何功率点容差内)
    n_per3 = 100
    n_trans3 = 30
    pwr3 = np.concatenate([
        np.full(n_per3, 33.0), np.full(n_trans3, 45.0),
        np.full(n_per3, 58.5), np.full(n_trans3, 45.0),
        np.full(n_per3, 117.0), np.full(n_trans3, 45.0),
        np.full(n_per3, 156.0), np.full(n_trans3, 45.0),
        np.full(n_per3, 175.5), np.full(n_trans3, 45.0),
        np.full(n_per3, 195.0),
    ])
    ts3 = pd.date_range('2026-08-22 00:00:00', periods=len(pwr3), freq='1s')
    volt3 = np.full(len(pwr3), 3.6)
    df3 = pd.DataFrame({
        'Timestamp': ts3, 'FC_NetPwrOut': pwr3, 'FC_AvgCellVoltage': volt3,
    })
    parsed3 = parse_durability_data(df3)
    trans_total = int((parsed3['point_status'] == 'transition').sum())
    print(f"  过渡点总数: {trans_total}")
    assert trans_total > 0, "应有过渡点"
    agg3 = aggregate_durability_stats(parsed3, ['FC_AvgCellVoltage'])
    assert len(agg3) == 6, f"应输出 6 行,实际 {len(agg3)}"
    # 每段 100 点,去前后 10% (10+10) 后剩 80 点稳定数据
    # 过渡段的 30 个点不应该被聚合
    assert (agg3['数据量'] == 80).all(), \
        f"每组数据量应=80,实际 {agg3['数据量'].tolist()}"
    print(f"  [PASS] 过渡段被排除,每组数据量=80 (100-前后 10%*2)")

    print("\n===== 测试4: 数据不足标记 (min_data_count 阈值) =====")
    # 每段 50 点 → stable 后 40 点 < min_data_count=50 → '数据不足'
    n_per4 = 50
    ts4 = pd.date_range('2026-08-22 00:00:00',
                         periods=n_per4 * 6, freq='1s')
    pwr4 = np.concatenate([np.full(n_per4, p) for p in pps])
    volt4 = np.full(n_per4 * 6, 3.6)
    df4 = pd.DataFrame({
        'Timestamp': ts4, 'FC_NetPwrOut': pwr4, 'FC_AvgCellVoltage': volt4,
    })
    parsed4 = parse_durability_data(df4)
    agg4 = aggregate_durability_stats(parsed4, ['FC_AvgCellVoltage'],
                                       min_data_count=50)
    assert len(agg4) == 6, f"应输出 6 行,实际 {len(agg4)}"
    assert (agg4['数据量'] == 40).all(), \
        f"每组数据量应=40,实际 {agg4['数据量'].tolist()}"
    assert (agg4['质量标记'] == '数据不足').all(), \
        f"40 < 50 应标 '数据不足',实际 {agg4['质量标记'].tolist()}"
    print(f"  [PASS] 每组 40 < 50 → 全部标 '数据不足'")

    print("\n===== 测试5: 波动异常标记 (volatility_threshold) =====")
    # 构造信号 std=0.05 > 阈值 0.01 → '波动异常'
    pwr5 = np.concatenate([np.full(n_per, p) for p in pps])
    volt5 = np.concatenate([
        np.full(n_per, 3.6) + rng.normal(0, 0.05, n_per) for _ in pps
    ])
    df5 = pd.DataFrame({
        'Timestamp': ts_arr, 'FC_NetPwrOut': pwr5,
        'FC_AvgCellVoltage': volt5,
    })
    parsed5 = parse_durability_data(df5)
    agg5 = aggregate_durability_stats(parsed5, ['FC_AvgCellVoltage'],
                                       volatility_threshold=0.01)
    assert (agg5['质量标记'] == '波动异常').all(), \
        f"std>0.01 应标 '波动异常',实际 {agg5['质量标记'].tolist()}"
    print(f"  [PASS] std>阈值 0.01 → 全部标 '波动异常'")

    print("\n===== 测试6: 空输入返回空 DataFrame =====")
    empty = aggregate_durability_stats(pd.DataFrame(), sigs)
    assert len(empty) == 0
    for col in ['cycle_id', 'power_point', '数据量', '质量标记']:
        assert col in empty.columns, f"缺少列 {col}"
    print(f"  [PASS] 空输入返回含必要列的空 DataFrame")

    print("\n===== 测试7: 不支持的 agg_method 报错 =====")
    try:
        aggregate_durability_stats(parsed, sigs, agg_method='invalid')
        assert False, "应抛 ValueError"
    except ValueError as e:
        assert 'agg_method' in str(e)
        print(f"  [PASS] 抛 ValueError: {e}")

    print("\n===== 测试8: 缺信号列容错 =====")
    df8 = parsed.drop(columns=['FC_VoltOut'])
    agg8 = aggregate_durability_stats(df8, ['FC_VoltOut', 'FC_AvgCellVoltage'])
    assert 'FC_VoltOut_mean' not in agg8.columns, "缺失列不应出现在输出"
    assert 'FC_AvgCellVoltage_mean' in agg8.columns, "存在的信号应正常输出"
    print(f"  [PASS] 缺失信号列被跳过,其他列正常输出")

    print("\n===== 测试9: 数据不足 + 波动异常同时出现(优先级) =====")
    # 构造:每段 200 点, stable 后 160 < min_count=200, 但 std=0.05 > 0.01
    n_per9 = 200
    pwr9 = np.concatenate([np.full(n_per9, p) for p in pps])
    volt9 = np.concatenate([
        np.full(n_per9, 3.6) + rng.normal(0, 0.05, n_per9) for _ in pps
    ])
    ts9 = pd.date_range('2026-08-22 00:00:00', periods=n_per9 * 6, freq='1s')
    df9 = pd.DataFrame({
        'Timestamp': ts9, 'FC_NetPwrOut': pwr9,
        'FC_AvgCellVoltage': volt9,
    })
    parsed9 = parse_durability_data(df9)
    agg9 = aggregate_durability_stats(parsed9, ['FC_AvgCellVoltage'],
                                       min_data_count=200,
                                       volatility_threshold=0.01)
    # 数据不足优先级高于波动异常
    assert (agg9['质量标记'] == '数据不足').all(), \
        f"应标 '数据不足' (优先级高于波动异常),实际 {agg9['质量标记'].tolist()}"
    print(f"  [PASS] 数据不足优先级 > 波动异常 (160<200 且 std>0.01)")

    print("\n[OK] 全部测试通过")
