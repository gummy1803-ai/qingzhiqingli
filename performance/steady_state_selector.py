"""燃电性能统计 - 稳态工况筛选算法。

从原始时序数据中提取"电流恒定"的有效数据段,供后续性能指标
(单片电压一致性、功率效率、氢耗率等)在稳定工况下计算。

核心函数: find_steady_segments
"""
from __future__ import annotations

import logging
from typing import List, Dict, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# 相邻区间间隔小于该值(秒)则合并,认为短暂扰动(如传感器毛刺)
_MERGE_GAP_SECONDS = 5.0


# ---------- 内部工具函数 ----------

def _get_timestamps(df: pd.DataFrame) -> Optional[pd.Series]:
    """兼容查找时间戳列,返回 datetime Series 或 None。"""
    for c in ('Timestamp', 'timestamp'):
        if c in df.columns:
            return pd.to_datetime(df[c], errors='coerce')
    return None


def _find_runs(in_range: np.ndarray) -> List[Tuple[int, int]]:
    """从布尔数组中提取所有连续 True 区间。

    向量化实现(等效于 scipy.ndimage.label,但零额外依赖):
    在前后补 False,diff 找上升沿(+1=区间起点)和下降沿(-1=区间终点)。

    Returns:
        [(start, end), ...]  # end 为开区间索引,区间 = df[start:end]
    """
    if len(in_range) == 0:
        return []
    # 前后补 False,保证边界处的上升/下降沿都能被 diff 捕获
    padded = np.concatenate(([False], in_range, [False])).astype(np.int8)
    diff = np.diff(padded)
    starts = np.where(diff == 1)[0]   # False→True 上升沿
    ends = np.where(diff == -1)[0]    # True→False 下降沿
    return [(int(s), int(e)) for s, e in zip(starts, ends)]


def _merge_close_runs(
    runs: List[Tuple[int, int]],
    ts: Optional[pd.Series],
) -> List[Tuple[int, int]]:
    """合并间隔 < _MERGE_GAP_SECONDS 的相邻区间(视为短暂扰动)。

    有时间戳时按实际时间差判断;无时间戳时按索引差(假设 1 秒均匀采样)。
    """
    if not runs:
        return []
    merged: List[List[int]] = [list(runs[0])]
    for s, e in runs[1:]:
        prev_s, prev_e = merged[-1]
        if ts is not None:
            # 前区间最后一点 到 后区间第一点 的时间差
            t_prev = ts.iloc[prev_e - 1]
            t_curr = ts.iloc[s]
            gap = (t_curr - t_prev).total_seconds()
        else:
            # 无时间戳:按索引差近似(假设 1 秒/点)
            gap = float(s - (prev_e - 1))
        if gap < _MERGE_GAP_SECONDS:
            logger.info("合并区间: [%d:%d]+[%d:%d] gap=%.1fs < %.0fs",
                        prev_s, prev_e, s, e, gap, _MERGE_GAP_SECONDS)
            merged[-1][1] = e  # 扩展 end
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def _duration_seconds(
    ts: Optional[pd.Series],
    start: int,
    end: int,
) -> float:
    """计算区间[start:end)的持续时间(秒)。

    有时间戳:用首尾时间差;无时间戳:按点数近似(假设 1 秒/点)。
    """
    if ts is not None:
        sub = ts.iloc[start:end].dropna()
        if len(sub) >= 2:
            return float((sub.iloc[-1] - sub.iloc[0]).total_seconds())
        # 时间戳不足时回退到点数
        return float(end - start)
    return float(end - start)


# ---------- 主算法 ----------

def find_steady_segments(
    df: pd.DataFrame,
    target_current: float,
    tolerance: float,
    min_duration: int,
    current_col: str = 'FC_CurrOut',
) -> List[Dict]:
    """从时序数据中提取电流恒定的有效数据段。

    Args:
        df: 原始时序数据,需含电流列(默认 FC_CurrOut)
        target_current: 电流目标值,如 95
        tolerance: 允许波动范围,如 5 (即 [90, 100] 均视为稳态)
        min_duration: 最短持续时间(秒),短于此值的段被丢弃
        current_col: 电流列名

    Returns:
        所有有效数据段列表,每个元素:
        {
            'start_idx': int,            # 起始索引(df.iloc 下标)
            'end_idx': int,              # 结束索引(开区间)
            'duration': int,             # 持续秒数
            'mean_current': float,       # 该段实际平均电流
            'segment_data': pd.DataFrame # 该段完整数据
        }

    算法步骤:
        1. 向量化标记每个点是否在 [target±tol] 范围内
        2. numpy diff 提取连续 True 区间(等效 scipy.ndimage.label)
        3. 合并间隔 < 5s 的相邻区间(短暂扰动)
        4. 过滤 duration < min_duration 的区间,计算每段统计
    """
    logger.info("稳态筛选开始: target=%.2f tol=%.2f min_dur=%ds rows=%d",
                target_current, tolerance, min_duration, len(df))

    # ---------- 输入校验 ----------
    if current_col not in df.columns:
        logger.error("电流列不存在: %s (现有列: %s)",
                     current_col, list(df.columns))
        return []

    cur = df[current_col].to_numpy(dtype=float)
    if len(cur) == 0:
        logger.warning("数据为空,无法筛选稳态段")
        return []

    # ---------- 边界:目标电流超出数据范围 ----------
    cmin, cmax = float(np.nanmin(cur)), float(np.nanmax(cur))
    if target_current + tolerance < cmin or target_current - tolerance > cmax:
        logger.warning("目标电流 %.2f 超出数据范围 [%.2f, %.2f]±%.2f,无稳态段",
                       target_current, cmin, cmax, tolerance)
        return []

    ts = _get_timestamps(df)

    # ---------- 步骤1:向量化标记目标范围内的点 ----------
    in_range = np.abs(cur - target_current) <= tolerance
    in_range = np.nan_to_num(in_range, nan=False).astype(bool)
    in_cnt = int(in_range.sum())
    logger.info("目标范围内点数: %d / %d (%.1f%%)",
                in_cnt, len(in_range), 100 * in_cnt / max(len(in_range), 1))

    # ---------- 步骤2:提取连续 True 区间 ----------
    runs = _find_runs(in_range)
    logger.info("初始连续区间: %d 个", len(runs))
    if not runs:
        logger.warning("无任何点落在目标范围内,返回空列表")
        return []

    # ---------- 步骤3:合并间隔 < 5s 的相邻区间 ----------
    merged = _merge_close_runs(runs, ts)
    logger.info("合并后区间: %d 个 (合并阈值 %.0fs)", len(merged), _MERGE_GAP_SECONDS)

    # ---------- 步骤4:过滤过短区间,计算每段统计 ----------
    results: List[Dict] = []
    for start, end in merged:
        duration = _duration_seconds(ts, start, end)
        if duration < min_duration:
            logger.info("丢弃区间 [%d:%d] dur=%.0fs < min_dur=%ds",
                        start, end, duration, min_duration)
            continue
        seg = df.iloc[start:end]
        mean_cur = float(seg[current_col].astype(float).mean())
        results.append({
            'start_idx': int(start),
            'end_idx': int(end),
            'duration': int(round(duration)),
            'mean_current': round(mean_cur, 4),
            'segment_data': seg,
        })

    logger.info("有效稳态段: %d 个 (满足 >= %ds)", len(results), min_duration)
    for r in results:
        logger.info("  段[idx %d:%d] dur=%ds mean=%.2fA rows=%d",
                    r['start_idx'], r['end_idx'], r['duration'],
                    r['mean_current'], len(r['segment_data']))
    return results


# ---------- 单元测试示例 ----------

if __name__ == '__main__':
    import time

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    )

    # 构造测试数据(1 秒均匀采样, 600 个点):
    #   0-199s   电流=95  (稳态段A, 200s >= 180s, 保留)
    #   200-201s 电流=200 (短暂扰动, 与前后段间隔 < 5s, 应被合并进段AB)
    #   202-399s 电流=95  (稳态段B, 与A合并后 dur≈399s, 保留)
    #   400-409s 电流=60  (偏离带, 把段AB 和 段C 隔开, 间隔 > 5s 不合并)
    #   410-449s 电流=95  (稳态段C, 40s < 180s, 应被过滤)
    #   450-599s 电流=60  (偏离目标, 非稳态)
    n = 600
    ts_arr = pd.date_range('2026-08-22 00:00:00', periods=n, freq='1s')
    cur_arr = np.full(n, 95.0)
    cur_arr[200:202] = 200.0   # 短暂扰动(2s)
    cur_arr[400:410] = 60.0    # 偏离带(隔开段AB与段C)
    cur_arr[450:600] = 60.0    # 后段偏离目标
    df_test = pd.DataFrame({
        'Timestamp': ts_arr,
        'FC_CurrOut': cur_arr,
        'FC_VoltOut': np.full(n, 320.0),
    })

    print("\n===== 测试1: 常规筛选 (target=95, tol=5, min=180s) =====")
    segs = find_steady_segments(df_test, target_current=95,
                               tolerance=5, min_duration=180)
    print(f"找到 {len(segs)} 个稳态段")
    for s in segs:
        print(f"  [idx {s['start_idx']}:{s['end_idx']}] "
              f"dur={s['duration']}s mean={s['mean_current']}A "
              f"rows={len(s['segment_data'])}")
    # 期望:1 段(段A+扰动+段B 合并为 [0:400]),段C(40s)被过滤
    assert len(segs) == 1, f"应合并为1段(段C被过滤),实际{len(segs)}"
    assert segs[0]['start_idx'] == 0
    assert segs[0]['end_idx'] == 400, f"end_idx应为400,实际{segs[0]['end_idx']}"
    assert segs[0]['duration'] >= 180
    print("  [PASS] 合并+过滤逻辑正确")

    print("\n===== 测试2: 目标电流超出数据范围 (target=9999) =====")
    empty = find_steady_segments(df_test, target_current=9999,
                                tolerance=5, min_duration=180)
    assert len(empty) == 0
    print("  [PASS] 超范围返回空列表")

    print("\n===== 测试3: 边界稳态(数据起止处于稳态) =====")
    # 全程稳态,首尾都在范围内
    df_edge = pd.DataFrame({'Timestamp': ts_arr,
                            'FC_CurrOut': np.full(n, 95.0)})
    segs_edge = find_steady_segments(df_edge, 95, 5, 180)
    assert len(segs_edge) == 1
    assert segs_edge[0]['start_idx'] == 0
    assert segs_edge[0]['end_idx'] == n
    print(f"  [PASS] 首尾稳态保留: [0:{n}] dur={segs_edge[0]['duration']}s")

    print("\n===== 测试4: 性能测试 (10万+ 条数据 < 1s) =====")
    n_big = 100_000
    ts_big = pd.date_range('2026-08-22 00:00:00', periods=n_big, freq='1s')
    cur_big = np.full(n_big, 95.0)
    cur_big[50000:50002] = 200  # 中间一次扰动
    df_big = pd.DataFrame({'Timestamp': ts_big, 'FC_CurrOut': cur_big})
    t0 = time.perf_counter()
    segs_big = find_steady_segments(df_big, 95, 5, 180)
    elapsed = time.perf_counter() - t0
    print(f"  10万条数据耗时: {elapsed*1000:.1f}ms, 找到 {len(segs_big)} 段")
    assert elapsed < 1.0, f"性能不达标: {elapsed:.2f}s >= 1s"
    print("  [PASS] 10万条 < 1s")

    print("\n===== 测试5: 无时间戳列(按索引近似) =====")
    df_no_ts = pd.DataFrame({'FC_CurrOut': cur_arr})
    segs_no_ts = find_steady_segments(df_no_ts, 95, 5, 180)
    assert len(segs_no_ts) == 1
    print(f"  [PASS] 无时间戳也能工作: dur={segs_no_ts[0]['duration']}s")

    print("\n[OK] 全部测试通过")
