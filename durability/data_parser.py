"""台架耐久数据解析模块。

将原始耐久时序数据解析为分层结构,识别:
1. 循环编号 (cycle_id): 6 个功率点为一组,构成一个完整循环
2. 功率点 (power_point): 当前所处的目标功率 (kW)
3. 持续时长 (point_duration): 当前功率点已持续秒数(从进入该段开始累计)
4. 稳定状态 (is_stable): 是否进入稳定段(功率波动 <5% 且去掉前后 10%)

支持两种模式:
- 模式1 (校验): 原始数据已含 cycle_id / power_point 列 → 校验合理性后补充稳定标记
- 模式2 (自动识别): 从 FC_NetPwrOut 时序中自动识别功率台阶与循环

核心函数: parse_durability_data
"""
from __future__ import annotations

import logging
from typing import List, Dict, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ---------- 常量 ----------
_DEFAULT_POWER_POINTS: List[float] = [33.0, 58.5, 117.0, 156.0, 175.5, 195.0]
_STABILITY_TOLERANCE = 0.05      # 功率波动 ±5% 视为稳态
_MIN_POINT_DURATION = 30.0       # 功率点至少持续 30 秒才视为有效
_TRANSITION_RATIO = 0.10         # 前/后 10% 视为过渡数据(不参与统计)
_CYCLE_SIZE = 6                  # 6 个功率点构成一个完整循环


# ---------- 内部工具函数 ----------

def _get_timestamps(df: pd.DataFrame) -> Optional[pd.Series]:
    """兼容查找时间戳列,返回 datetime Series 或 None。"""
    for c in ('Timestamp', 'timestamp'):
        if c in df.columns:
            return pd.to_datetime(df[c], errors='coerce')
    return None


def _find_runs(mask: np.ndarray) -> List[Tuple[int, int]]:
    """从布尔数组中提取所有连续 True 区间。

    向量化实现(等效 scipy.ndimage.label,但零额外依赖):
    在前后补 False,diff 找上升沿(+1=区间起点)和下降沿(-1=区间终点)。

    Returns:
        [(start, end), ...]  # end 为开区间索引,区间 = df[start:end]
    """
    if len(mask) == 0:
        return []
    padded = np.concatenate(([False], mask, [False])).astype(np.int8)
    diff = np.diff(padded)
    starts = np.where(diff == 1)[0]
    ends = np.where(diff == -1)[0]
    return [(int(s), int(e)) for s, e in zip(starts, ends)]


def _duration_seconds(
    ts: Optional[pd.Series],
    start: int,
    end: int,
) -> float:
    """计算区间 [start, end) 的持续时间(秒)。

    有时间戳:用首尾时间差;无时间戳:按点数近似(假设 1 秒/点)。
    """
    if ts is not None:
        sub = ts.iloc[start:end].dropna()
        if len(sub) >= 2:
            return float((sub.iloc[-1] - sub.iloc[0]).total_seconds())
        return float(end - start)  # 退化:按点数近似
    return float(end - start)


# ---------- 模式1: 校验已有列 ----------

def _validate_existing(
    df: pd.DataFrame,
    power_points: List[float],
    ts: Optional[pd.Series],
) -> pd.DataFrame:
    """模式1: 原始数据已含 cycle_id / power_point 列,执行校验。

    兼容列名 power_setpoint → 自动复制为 power_point。
    校验项:
    - cycle_id 中的 NaN 填为 -1(不完整)
    - power_point 不在任意目标功率 ±5% 容差内的点,标记为 NaN(过渡)
    """
    # 兼容 power_setpoint 列名
    if 'power_point' not in df.columns and 'power_setpoint' in df.columns:
        df['power_point'] = df['power_setpoint']
        logger.info("从 power_setpoint 列复制为 power_point (共 %d 行)",
                    len(df))

    cid_unique = sorted(df['cycle_id'].dropna().unique().tolist())
    pp_unique = sorted(df['power_point'].dropna().unique().tolist())
    logger.info("校验已有列: cycle_id 范围=%s, power_point 唯一值数=%d",
                cid_unique, len(pp_unique))

    # ---------- 校验 cycle_id NaN ----------
    cid_na = int(df['cycle_id'].isna().sum())
    if cid_na > 0:
        logger.warning("cycle_id 有 %d 个 NaN,填为 -1(不完整)", cid_na)
    df['cycle_id'] = df['cycle_id'].fillna(-1).astype(int)

    # ---------- 校验 power_point 是否在容差范围内 ----------
    if len(power_points) == 0:
        logger.warning("power_points 为空,跳过 power_point 容差校验")
    else:
        pp = df['power_point'].to_numpy(dtype=float)
        in_tol = np.zeros(len(pp), dtype=bool)
        for p in power_points:
            tol_abs = _STABILITY_TOLERANCE * abs(p)
            in_tol |= (np.abs(pp - p) <= tol_abs) & ~np.isnan(pp)
        invalid_cnt = int((~in_tol).sum())
        if invalid_cnt > 0:
            logger.warning("power_point 有 %d 个点不在目标功率 ±5%% 容差内,"
                            "将标记为 NaN(过渡数据)", invalid_cnt)
            df.loc[~in_tol, 'power_point'] = np.nan

    # 初始化 point_duration 和 is_stable 列(后续由 _mark_stability 填充)
    df['point_duration'] = 0.0
    df['is_stable'] = False

    return df


# ---------- 模式2: 自动识别 ----------

def _match_nearest_power(
    pwr: np.ndarray,
    power_points: List[float],
    tol: float = _STABILITY_TOLERANCE,
) -> Tuple[np.ndarray, np.ndarray]:
    """对每个功率样本,匹配最近的功率点(若在容差内)。

    Args:
        pwr: 净功率数组 (kW)
        power_points: 候选目标功率点列表
        tol: 相对容差(默认 5%)

    Returns:
        matched_pp: 每个样本对应的功率点(NaN 表示无匹配,即过渡数据)
        in_tol: 是否在某个功率点容差范围内
    """
    pwr = np.asarray(pwr, dtype=float)
    n = len(pwr)
    if n == 0 or not power_points:
        return (np.full(n, np.nan), np.zeros(n, dtype=bool))

    pp_arr = np.array(power_points, dtype=float)
    # 距离矩阵 (n_samples, n_points)
    diff = np.abs(pwr[:, None] - pp_arr[None, :])
    nearest_idx = np.argmin(diff, axis=1)
    nearest_pp = pp_arr[nearest_idx]
    # 容差按相对值:abs(diff) <= 5% * 目标功率
    tol_abs = tol * np.abs(pp_arr[nearest_idx])
    in_tol = diff[np.arange(n), nearest_idx] <= tol_abs

    matched = np.where(in_tol, nearest_pp, np.nan)
    return matched, in_tol


def _find_pp_runs(
    matched_pp: np.ndarray,
    in_tol: np.ndarray,
) -> List[Tuple[int, int]]:
    """按匹配到的功率点切分连续段(忽略过渡段)。

    与 _find_runs(二值掩码) 不同,本函数考虑功率点的具体值变化:
    当 matched_pp 从 P1 跳到 P2 时(即使两者都在容差内),
    视为段切换。过渡段(in_tol=False)被丢弃。

    算法(两步法,避免 -inf 运算产生 NaN RuntimeWarning):
    1. 用 _find_runs(in_tol) 提取每个 in_tol=True 的子段
    2. 在每个子段内,按 matched_pp 值变化进一步切分

    Args:
        matched_pp: 每个样本匹配到的功率点(NaN 表示过渡)
        in_tol: 是否在容差范围内

    Returns:
        [(start, end), ...]  # 每段内功率点恒定
    """
    n = len(matched_pp)
    if n == 0:
        return []
    # 步骤1: 用 _find_runs 找出每个 in_tol=True 的连续段(自动跳过过渡段)
    base_runs = _find_runs(in_tol)
    # 步骤2: 在每个子段内,按 matched_pp 值变化进一步切分
    final_runs: List[Tuple[int, int]] = []
    for s, e in base_runs:
        seg_pp = matched_pp[s:e]
        if len(seg_pp) == 0:
            continue
        if len(seg_pp) == 1:
            final_runs.append((s, e))
            continue
        # 在段内找功率点变化点(理论上 seg_pp 应全部非 NaN,但用 nan_to_num 防御)
        diff = np.diff(seg_pp)
        # 处理 NaN(若有):视为变化点
        nan_change = np.isnan(seg_pp[1:]) | np.isnan(seg_pp[:-1])
        val_change = np.abs(np.nan_to_num(diff, nan=0.0)) > 1e-9
        change_mask = nan_change | val_change
        change_idx = np.where(change_mask)[0] + 1
        boundaries = np.concatenate(([0], change_idx, [len(seg_pp)]))
        for i in range(len(boundaries) - 1):
            ss = s + int(boundaries[i])
            ee = s + int(boundaries[i + 1])
            final_runs.append((ss, ee))
    return final_runs


def _auto_identify(
    df: pd.DataFrame,
    power_points: List[float],
    ts: Optional[pd.Series],
) -> pd.DataFrame:
    """模式2: 从功率时序自动识别循环编号和功率点。

    算法步骤:
    2a. 对每个样本匹配最近目标功率点(±5% 容差)
    2b. 提取每个功率点的连续段(向量化 _find_runs)
    2c. 过滤过短段(< 30s 标 invalid)
    2d. 每 6 个有效段为一循环,余数标 cycle_id=-1
    """
    pwr = df['FC_NetPwrOut'].to_numpy(dtype=float)
    n = len(pwr)
    logger.info("自动识别: n=%d, 候选功率点=%s", n, power_points)

    # ---------- 步骤2a: 匹配最近功率点 ----------
    matched_pp, in_tol = _match_nearest_power(pwr, power_points)
    logger.info("容差内匹配点数: %d / %d (%.1f%%)",
                int(in_tol.sum()), n, 100 * in_tol.sum() / max(n, 1))

    # ---------- 步骤2b: 按功率点变化切分连续段 ----------
    # 注意:不能用 _find_runs(in_tol),否则 6 个阶跃切换的功率点会被
    # 合并成一个大段(因为相邻段都 in_tol=True)。必须按 matched_pp 值变化切分。
    runs = _find_pp_runs(matched_pp, in_tol)
    logger.info("功率点连续段: %d 个", len(runs))

    # ---------- 步骤2c: 区分有效段和过短段 ----------
    valid_segments: List[Dict] = []  # 进入循环编号的有效段
    short_segments: List[Dict] = []  # 过短段(仅写 power_point 供 invalid 标记)
    for start, end in runs:
        dur = _duration_seconds(ts, start, end)
        seg_pp = matched_pp[start:end]
        vals = seg_pp[~np.isnan(seg_pp)]
        if len(vals) == 0:
            logger.warning("段 [%d:%d] 全部 NaN,跳过", start, end)
            continue
        # 中位数更稳健(应对零星偏差)
        rep_pp = float(np.median(vals))
        if dur < _MIN_POINT_DURATION:
            short_segments.append({
                'start': start, 'end': end,
                'duration': dur, 'power_point': rep_pp,
            })
            logger.info("段 [%d:%d] dur=%.1fs < %.0fs,标记为待 invalid",
                        start, end, dur, _MIN_POINT_DURATION)
            continue
        valid_segments.append({
            'start': start, 'end': end,
            'duration': dur, 'power_point': rep_pp,
        })

    logger.info("有效功率段: %d 个 (满足 >= %.0fs);过短段: %d 个",
                len(valid_segments), _MIN_POINT_DURATION, len(short_segments))

    # ---------- 步骤2d: 每 _CYCLE_SIZE 个段为一循环 ----------
    cycle_id_arr = np.full(n, -1, dtype=int)
    pp_arr = np.full(n, np.nan, dtype=float)

    n_cycles = len(valid_segments) // _CYCLE_SIZE
    remainder = len(valid_segments) % _CYCLE_SIZE
    logger.info("完整循环数: %d (剩余 %d 段标为 cycle_id=-1)",
                n_cycles, remainder)

    for i, seg in enumerate(valid_segments):
        cid = i // _CYCLE_SIZE
        if cid >= n_cycles:
            cid = -1  # 不完整循环
        cycle_id_arr[seg['start']:seg['end']] = cid
        pp_arr[seg['start']:seg['end']] = seg['power_point']
        logger.info("  有效段[%d] [%d:%d] dur=%.1fs pp=%.1f cid=%d",
                    i, seg['start'], seg['end'], seg['duration'],
                    seg['power_point'], cid)

    # 过短段:写 power_point(供 invalid 标记),cycle_id 保持 -1
    for j, seg in enumerate(short_segments):
        pp_arr[seg['start']:seg['end']] = seg['power_point']
        logger.info("  过短段[%d] [%d:%d] dur=%.1fs pp=%.1f (cid=-1,将标 invalid)",
                    j, seg['start'], seg['end'], seg['duration'],
                    seg['power_point'])

    df = df.copy()
    df['cycle_id'] = cycle_id_arr
    df['power_point'] = pp_arr
    df['point_duration'] = 0.0
    df['is_stable'] = False

    return df


# ---------- 步骤3: 稳定性标记 ----------

def _mark_stability(
    df: pd.DataFrame,
    ts: Optional[pd.Series],
) -> pd.DataFrame:
    """在每个功率点段内,标 is_stable=True 仅中间 80%(去掉前 10% 和后 10%)。

    边界:
    - 段长 < 3 点 → 整段标稳定(无法去头尾)
    - 去头尾后为空 → 仅标中点
    """
    if 'power_point' not in df.columns or len(df) == 0:
        return df

    pp = df['power_point'].to_numpy(dtype=float)
    is_stable = np.zeros(len(df), dtype=bool)

    # 用 _find_pp_runs 按 power_point 值切分
    # (避免 6 个无过渡的功率点段被 _find_runs 合并为 1 个大段)
    has_pp = ~np.isnan(pp)
    runs = _find_pp_runs(pp, has_pp)
    logger.info("稳定性标记: %d 个功率点段", len(runs))

    for start, end in runs:
        seg_len = end - start
        if seg_len < 3:
            is_stable[start:end] = True
            continue
        trim = max(1, int(seg_len * _TRANSITION_RATIO))
        stable_start = start + trim
        stable_end = end - trim
        if stable_end <= stable_start:
            mid = (start + end) // 2
            is_stable[mid] = True
        else:
            is_stable[stable_start:stable_end] = True

    df['is_stable'] = is_stable
    stable_cnt = int(is_stable.sum())
    logger.info("稳定点数: %d / %d (%.1f%%)",
                stable_cnt, len(df),
                100 * stable_cnt / max(len(df), 1))

    # ---------- 计算 point_duration ----------
    df = _compute_point_duration(df, ts)

    return df


def _compute_point_duration(
    df: pd.DataFrame,
    ts: Optional[pd.Series],
) -> pd.DataFrame:
    """计算 point_duration: 当前功率点已持续秒数(从进入该段开始累计)。"""
    if 'power_point' not in df.columns or len(df) == 0:
        return df

    pp = df['power_point'].to_numpy(dtype=float)
    n = len(pp)
    dur = np.zeros(n, dtype=float)

    # 用 _find_pp_runs 按 power_point 值切分(与 _mark_stability 保持一致)
    has_pp = ~np.isnan(pp)
    runs = _find_pp_runs(pp, has_pp)

    for start, end in runs:
        if ts is not None:
            sub_ts = ts.iloc[start:end]
            t0 = sub_ts.iloc[0]
            if pd.isna(t0):
                # 退化:无时间戳起点,按点数
                for i in range(start, end):
                    dur[i] = float(i - start)
            else:
                for i in range(start, end):
                    ti = ts.iloc[i]
                    if pd.notna(ti):
                        dur[i] = float((ti - t0).total_seconds())
                    else:
                        dur[i] = float(i - start)
        else:
            for i in range(start, end):
                dur[i] = float(i - start)

    df['point_duration'] = dur
    return df


# ---------- 边界处理: 过渡和无效标记 ----------

def _mark_transitions_and_invalid(
    df: pd.DataFrame,
    ts: Optional[pd.Series],
) -> pd.DataFrame:
    """标记功率切换过渡点和无效段。

    通过列 'point_status' 输出:
    - 'stable'    : 稳定段(is_stable=True)
    - 'transition': 功率点之间的切换瞬间(NaN 区间或段边界)
    - 'invalid'   : 持续时间不足 30s 的功率点段(整段标 invalid)
    """
    n = len(df)
    if n == 0:
        df['point_status'] = pd.Series(dtype=object)
        return df

    # 默认所有点为 transition
    status = np.full(n, 'transition', dtype=object)

    # 稳定段标 'stable'
    if 'is_stable' in df.columns:
        status[df['is_stable'].to_numpy()] = 'stable'

    # 找出每个功率点段,过短的标 'invalid'(覆盖 stable 标记)
    if 'power_point' in df.columns:
        pp = df['power_point'].to_numpy(dtype=float)
        has_pp = ~np.isnan(pp)
        # 用 _find_pp_runs 按 power_point 值切分(与 _mark_stability 一致)
        runs = _find_pp_runs(pp, has_pp)
        for start, end in runs:
            dur = _duration_seconds(ts, start, end)
            if dur < _MIN_POINT_DURATION:
                status[start:end] = 'invalid'
                logger.info("标记 invalid 段 [%d:%d] dur=%.1fs < %.0fs",
                            start, end, dur, _MIN_POINT_DURATION)

    df['point_status'] = status
    stable_cnt = int((status == 'stable').sum())
    trans_cnt = int((status == 'transition').sum())
    inval_cnt = int((status == 'invalid').sum())
    logger.info("边界处理完成: stable=%d transition=%d invalid=%d",
                stable_cnt, trans_cnt, inval_cnt)

    return df


# ---------- 主函数 ----------

def parse_durability_data(
    df: pd.DataFrame,
    power_points: List[float] = None,
) -> pd.DataFrame:
    """解析原始耐久数据,识别循环编号和功率点,构建分层数据结构。

    Args:
        df: 原始耐久时序数据,需含 FC_NetPwrOut 列(净功率输出,kW)
        power_points: 6 个目标功率点(kW),默认 [33, 58.5, 117, 156, 175.5, 195]

    Returns:
        增强后的 DataFrame,新增列:
        - cycle_id      : 循环编号 0..N(-1 表示不完整循环或过渡数据)
        - power_point   : 当前所处的功率点(kW),过渡数据为 NaN
        - point_duration: 当前功率点已持续秒数(从进入该段开始累计)
        - is_stable     : 是否进入稳定状态(功率波动 <5% 且去掉前后 10%)
        - point_status  : 'stable' / 'transition' / 'invalid' (辅助列)

    解析模式:
        1. 若 df 已含 cycle_id 和 power_point 列 → 校验模式(校验合理性)
        2. 否则 → 自动识别(从功率时序匹配最近功率点,每 6 段为一循环)
    """
    if power_points is None:
        power_points = list(_DEFAULT_POWER_POINTS)

    logger.info("=== 耐久数据解析开始 rows=%d power_points=%s ===",
                len(df), power_points)

    # ---------- 输入校验 ----------
    if len(df) == 0:
        logger.warning("输入数据为空,返回空 DataFrame(含空新列)")
        result = df.copy()
        result['cycle_id'] = pd.Series(dtype=int)
        result['power_point'] = pd.Series(dtype=float)
        result['point_duration'] = pd.Series(dtype=float)
        result['is_stable'] = pd.Series(dtype=bool)
        result['point_status'] = pd.Series(dtype=object)
        return result

    if 'FC_NetPwrOut' not in df.columns:
        logger.error("缺少 FC_NetPwrOut 列,无法自动识别功率点")
        raise ValueError("FC_NetPwrOut 列必需(净功率输出, kW)")

    ts = _get_timestamps(df)
    if ts is None:
        logger.warning("未找到 Timestamp/timestamp 列,"
                        "point_duration 将按点数近似(假设 1s/点)")

    # ---------- 模式选择 ----------
    if 'cycle_id' in df.columns and (
        'power_point' in df.columns or 'power_setpoint' in df.columns
    ):
        logger.info("模式1: 已有 cycle_id/power_point 列,执行校验")
        result = _validate_existing(df.copy(), power_points, ts)
    else:
        logger.info("模式2: 自动识别循环和功率点")
        result = _auto_identify(df.copy(), power_points, ts)

    # ---------- 步骤3: 稳定性标记 ----------
    result = _mark_stability(result, ts)

    # ---------- 边界处理 ----------
    result = _mark_transitions_and_invalid(result, ts)

    # ---------- 完成日志 ----------
    cycle_cnt = int(result.loc[result['cycle_id'] >= 0, 'cycle_id'].nunique())
    stable_cnt = int(result['is_stable'].sum())
    logger.info("=== 耐久数据解析结束: 完整循环数=%d, 稳定点数=%d/%d (%.1f%%) ===",
                cycle_cnt, stable_cnt, len(result),
                100 * stable_cnt / max(len(result), 1))

    return result


# ---------- 单元测试 ----------

if __name__ == '__main__':
    import logging as _lg
    _lg.basicConfig(level=_lg.INFO,
                    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')

    print("\n===== 测试1: 自动识别完整循环 (1 循环 / 6 功率点) =====")
    n_per = 300  # 5 分钟/段(300s)
    pps = [33.0, 58.5, 117.0, 156.0, 175.5, 195.0]
    n_total = n_per * len(pps)
    ts_arr = pd.date_range('2026-08-22 00:00:00', periods=n_total, freq='1s')
    pwr_arr = np.concatenate([np.full(n_per, p) for p in pps])
    rng = np.random.default_rng(42)
    pwr_arr = pwr_arr * (1 + rng.normal(0, 0.01, n_total))  # ±1% 噪声
    df_test = pd.DataFrame({
        'Timestamp': ts_arr,
        'FC_NetPwrOut': pwr_arr,
        'FC_CurrOut': np.full(n_total, 100.0),
    })
    result = parse_durability_data(df_test)
    assert result['cycle_id'].nunique() == 1, \
        f"应识别为 1 个循环,实际 {result['cycle_id'].nunique()}"
    assert (result['cycle_id'] == 0).all(), "所有点应属于 cycle_id=0"
    assert result['power_point'].nunique() == 6, \
        f"应有 6 个功率点,实际 {result['power_point'].nunique()}"
    stable_ratio = result['is_stable'].sum() / len(result)
    assert 0.70 < stable_ratio < 0.90, \
        f"稳定点占比应约 80%,实际 {stable_ratio:.2f}"
    print(f"  [PASS] 1 循环 / 6 功率点 / 稳定占比={stable_ratio:.2f}")

    print("\n===== 测试2: 不完整循环标记为 -1 (11 段) =====")
    n_seg2 = 11  # 6+5 段(第 2 个循环不完整)
    n_per2 = 100
    pps2 = ([33.0, 58.5, 117.0, 156.0, 175.5, 195.0] * 2)[:n_seg2]
    n_total2 = n_per2 * n_seg2
    ts2 = pd.date_range('2026-08-22 00:00:00', periods=n_total2, freq='1s')
    pwr2 = np.concatenate([np.full(n_per2, p) for p in pps2])
    df2 = pd.DataFrame({'Timestamp': ts2, 'FC_NetPwrOut': pwr2})
    r2 = parse_durability_data(df2)
    cid_vals = sorted(r2['cycle_id'].unique().tolist())
    assert 0 in cid_vals, f"应有 cycle_id=0,实际 {cid_vals}"
    assert -1 in cid_vals, f"应有 cycle_id=-1(不完整),实际 {cid_vals}"
    print(f"  [PASS] cycle_id 唯一值: {cid_vals}")

    print("\n===== 测试3: 校验模式(已有 cycle_id/power_point 列) =====")
    pwr_clean = np.concatenate([np.full(n_per, p) for p in pps])
    df3 = pd.DataFrame({
        'Timestamp': ts_arr,
        'FC_NetPwrOut': pwr_clean,
        'cycle_id': np.zeros(n_total, dtype=int),
        'power_point': pwr_clean,
    })
    r3 = parse_durability_data(df3)
    assert (r3['cycle_id'] == 0).all(), "cycle_id 应保持为 0"
    assert r3['power_point'].nunique() == 6
    stable_ratio3 = r3['is_stable'].sum() / len(r3)
    assert 0.70 < stable_ratio3 < 0.90
    print(f"  [PASS] 校验模式: cycle_id=0, 稳定占比={stable_ratio3:.2f}")

    print("\n===== 测试4: 功率点持续 < 30s 标 invalid =====")
    n_short = 20  # 20s < 30s
    ts4 = pd.date_range('2026-08-22 00:00:00', periods=n_short, freq='1s')
    pwr4 = np.full(n_short, 33.0)
    df4 = pd.DataFrame({'Timestamp': ts4, 'FC_NetPwrOut': pwr4})
    r4 = parse_durability_data(df4)
    assert (r4['point_status'] == 'invalid').all(), \
        "20s 段应全部标 invalid"
    print(f"  [PASS] 20s 段全部标 invalid")

    print("\n===== 测试5: 过渡数据标 transition =====")
    n_per5 = 100
    n_trans = 50
    ts5 = pd.date_range('2026-08-22 00:00:00',
                         periods=n_per5 * 2 + n_trans, freq='1s')
    pwr5 = np.concatenate([
        np.full(n_per5, 33.0),
        np.full(n_trans, 45.0),  # 不在任何功率点 5% 容差内
        np.full(n_per5, 58.5),
    ])
    df5 = pd.DataFrame({'Timestamp': ts5, 'FC_NetPwrOut': pwr5})
    r5 = parse_durability_data(df5)
    # 45kW 过渡段(索引 n_per5:n_per5+n_trans) 应全部为 transition 且 power_point=NaN
    trans_seg = r5.iloc[n_per5:n_per5 + n_trans]
    assert (trans_seg['point_status'] == 'transition').all(), \
        "45kW 过渡段应全部标 transition"
    assert trans_seg['power_point'].isna().all(), \
        "45kW 过渡段 power_point 应为 NaN"
    print(f"  [PASS] 45kW 过渡段({n_trans}点)全部 transition 且 power_point=NaN")

    print("\n===== 测试6: 空输入返回空 DataFrame =====")
    r6 = parse_durability_data(pd.DataFrame())
    assert len(r6) == 0
    for col in ('cycle_id', 'power_point', 'point_duration',
                'is_stable', 'point_status'):
        assert col in r6.columns, f"缺少列 {col}"
    print(f"  [PASS] 空输入返回含 5 个空新列的 DataFrame")

    print("\n===== 测试7: 缺少 FC_NetPwrOut 列报错 =====")
    try:
        parse_durability_data(pd.DataFrame({'Timestamp': [1, 2]}))
        assert False, "应抛 ValueError"
    except ValueError as e:
        assert 'FC_NetPwrOut' in str(e)
        print(f"  [PASS] 抛 ValueError: {e}")

    print("\n===== 测试8: 无时间戳列(按点数近似) =====")
    df8 = pd.DataFrame({'FC_NetPwrOut': np.full(60, 33.0)})
    r8 = parse_durability_data(df8)
    assert r8['point_duration'].iloc[-1] == 59.0, \
        f"无时间戳最后一点 dur 应=59,实际 {r8['point_duration'].iloc[-1]}"
    print(f"  [PASS] 无时间戳: 最后一点 point_duration="
          f"{r8['point_duration'].iloc[-1]}")

    print("\n===== 测试9: 兼容 power_setpoint 列名 =====")
    df9 = pd.DataFrame({
        'Timestamp': ts_arr,
        'FC_NetPwrOut': pwr_clean,
        'cycle_id': np.zeros(n_total, dtype=int),
        'power_setpoint': pwr_clean,  # 用 setpoint 命名
    })
    r9 = parse_durability_data(df9)
    assert 'power_point' in r9.columns
    assert r9['power_point'].nunique() == 6
    print(f"  [PASS] power_setpoint 自动复制为 power_point")

    print("\n===== 测试10: 多循环(3 完整循环) =====")
    n_per10 = 60
    pps10 = pps * 3  # 3 个完整循环
    n_total10 = n_per10 * len(pps10)
    ts10 = pd.date_range('2026-08-22 00:00:00',
                          periods=n_total10, freq='1s')
    pwr10 = np.concatenate([np.full(n_per10, p) for p in pps10])
    df10 = pd.DataFrame({'Timestamp': ts10, 'FC_NetPwrOut': pwr10})
    r10 = parse_durability_data(df10)
    cid_vals10 = sorted(set(r10['cycle_id'].unique().tolist()))
    assert cid_vals10 == [0, 1, 2], \
        f"3 完整循环应有 cycle_id 0/1/2,实际 {cid_vals10}"
    print(f"  [PASS] 3 完整循环: cycle_id={cid_vals10}")

    print("\n[OK] 全部测试通过")
