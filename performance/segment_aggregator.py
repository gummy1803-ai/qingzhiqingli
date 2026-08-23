"""燃电性能统计 - 段聚合模块。

对 find_steady_segments 筛选出的有效数据段,计算各信号的
平均值/标准差/变异系数,并标记含异常点的段,供耐久衰减曲线绘制。

核心函数: aggregate_segments
"""
from __future__ import annotations

import logging
from typing import List, Dict, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# 时间戳列名(与 utils/helpers.py 保持一致)
_TIMESTAMP_COL = 'Timestamp'
# 累计运行时间列(用于 X 轴定位耐久衰减)
_RUNTIME_COL = 'FC_RunTime_Hours'


def _ensure_anomaly_flag(sdf: pd.DataFrame) -> pd.DataFrame:
    """确保段数据带 is_anomaly 列;无则调用 detect_anomalies 标记。

    延迟 import 避免直接运行本脚本时顶部 import 失败。
    """
    if 'is_anomaly' in sdf.columns:
        return sdf
    try:
        from utils.helpers import detect_anomalies
        sdf = detect_anomalies(sdf)
        logger.info("段无 is_anomaly 列,已调用 detect_anomalies 标记")
    except Exception as e:
        logger.warning("detect_anomalies 调用失败,跳过异常标记: %s", e)
        sdf = sdf.copy()
        sdf['is_anomaly'] = False
    return sdf


def aggregate_segments(
    segments: List[Dict],
    signal_columns: List[str],
    exclude_anomaly: bool = True,
    warmup_seconds: int = 180,
) -> pd.DataFrame:
    """对有效数据段计算各信号统计量。

    Args:
        segments: find_steady_segments 返回的段列表,每段含
                  {start_idx, end_idx, duration, mean_current, segment_data}
        signal_columns: 需统计的信号列名,如 ['FC_AvgCellVoltage','FC_NetPwrOut']
        exclude_anomaly: True 则剔除含异常点(detect_anomalies 标记)的段
        warmup_seconds: 稳态段前 N 秒作为过渡热机期丢弃,仅用 N 秒之后的数据求统计
                       (企业要求 180s; 若段总时长 < warmup 则仍保留该段,使用全部数据,
                       并在日志里 WARNING)

    Returns:
        DataFrame,每行一个有效段,列:
        - segment_id: 段编号(对应原 segments 列表索引,剔除后跳号可追溯)
        - start_time / end_time / mid_time: 起止/中点时间(基于 warmup 后部分)
        - duration: 持续秒数(基于 warmup 后部分)
        - current_avg: 平均电流(基于 warmup 后部分)
        - warmup_dropped: 丢弃的热机时长(秒,实际丢弃量;若段过短为 0)
        - run_time_at_mid: 段中点对应的累计运行时间(FC_RunTime_Hours)
        - is_anomaly_segment: 该段是否含异常点
        - anomaly_count: 段内异常点数
        - <signal>_mean / <signal>_std / <signal>_cv: 各信号均值/标准差/变异系数
    """
    logger.info("聚合开始: segments=%d signals=%s exclude_anomaly=%s warmup=%ds",
                len(segments), signal_columns, exclude_anomaly, warmup_seconds)

    if not segments:
        logger.warning("无有效段,返回空 DataFrame")
        return pd.DataFrame()

    rows: List[Dict] = []
    skipped = 0
    for i, seg in enumerate(segments):
        sdf = seg.get('segment_data')
        if sdf is None or len(sdf) == 0:
            logger.warning("段%d 数据为空,跳过", i)
            continue

        # ---------- 丢弃稳态段前 warmup_seconds 热机过渡期 ----------
        ts_warmup = (pd.to_datetime(sdf[_TIMESTAMP_COL], errors='coerce')
                     if _TIMESTAMP_COL in sdf.columns else None)
        if warmup_seconds > 0:
            if ts_warmup is not None and ts_warmup.notna().sum() >= 2:
                elapsed = (ts_warmup - ts_warmup.iloc[0]).dt.total_seconds()
                mask_after = (elapsed >= warmup_seconds)
                drop_n = int((~mask_after).sum())
                actual_drop_s = (ts_warmup.iloc[min(drop_n, len(sdf)-1)] - ts_warmup.iloc[0]).total_seconds() \
                    if drop_n < len(sdf) else (ts_warmup.iloc[-1] - ts_warmup.iloc[0]).total_seconds() if len(ts_warmup) >= 2 else 0
                sdf_warm = sdf.loc[mask_after].copy() if mask_after.any() else sdf.copy()
                warmup_dropped_s = float(actual_drop_s) if mask_after.any() else 0.0
            else:
                # 无时间戳 -> 按索引近似(1 点≈1 秒),截断前 warmup_seconds 行
                if len(sdf) > warmup_seconds:
                    sdf_warm = sdf.iloc[warmup_seconds:].copy()
                    warmup_dropped_s = float(warmup_seconds)
                else:
                    sdf_warm = sdf.copy()
                    warmup_dropped_s = 0.0
            if len(sdf_warm) < max(3, len(sdf) // 4):
                logger.warning(
                    "段%d warmup=%ds 截断后仅剩 %d 行(原 %d),跳过该段",
                    i, warmup_seconds, len(sdf_warm), len(sdf))
                skipped += 1
                continue
            if 0 < warmup_dropped_s < warmup_seconds * 0.6:
                logger.warning(
                    "段%d 总时长不足 warmup(%ds),实际仅丢弃 %.0fs(段过短,未丢弃)",
                    i, warmup_seconds, warmup_dropped_s)
                warmup_dropped_s = 0.0
        else:
            sdf_warm = sdf.copy()
            warmup_dropped_s = 0.0

        # ---------- 异常段检测(基于 warmup 后子段) ----------
        sdf_warm = _ensure_anomaly_flag(sdf_warm)
        anom_cnt = int(sdf_warm['is_anomaly'].sum()) if 'is_anomaly' in sdf_warm.columns else 0
        has_anomaly = anom_cnt > 0

        if exclude_anomaly and has_anomaly:
            logger.info("段%d 含 %d 个异常点,剔除(exclude_anomaly=True)",
                        i, anom_cnt)
            skipped += 1
            continue

        # ---------- 时间与运行时间定位(warmup 后子段) ----------
        ts = (sdf_warm[_TIMESTAMP_COL] if _TIMESTAMP_COL in sdf_warm.columns
              else None)
        mid = len(sdf_warm) // 2
        start_time = ts.iloc[0] if ts is not None else None
        end_time = ts.iloc[-1] if ts is not None else None
        mid_time = ts.iloc[mid] if ts is not None else None

        # duration / mean_current: 用 warmup 后子段重新算,更准确
        if ts is not None and len(ts.dropna()) >= 2:
            sub_dur = float((ts.dropna().iloc[-1] - ts.dropna().iloc[0]).total_seconds())
        else:
            sub_dur = float(len(sdf_warm))
        cur_col_local = 'FC_CurrOut' if 'FC_CurrOut' in sdf_warm.columns else None
        sub_cur_avg = (float(pd.to_numeric(sdf_warm[cur_col_local], errors='coerce').mean())
                       if cur_col_local is not None else seg.get('mean_current'))

        run_time_mid: Optional[float] = None
        if _RUNTIME_COL in sdf_warm.columns:
            rt = pd.to_numeric(sdf_warm[_RUNTIME_COL], errors='coerce')
            run_time_mid = (float(rt.iloc[mid])
                            if not pd.isna(rt.iloc[mid]) else None)

        # ---------- 组装行 ----------
        row: Dict = {
            'segment_id': i,
            'start_time': start_time,
            'end_time': end_time,
            'mid_time': mid_time,
            'duration': int(round(sub_dur)),
            'current_avg': round(sub_cur_avg, 4) if sub_cur_avg is not None else seg.get('mean_current'),
            'warmup_dropped_s': round(warmup_dropped_s, 1),
            'run_time_at_mid': (round(run_time_mid, 4)
                                if run_time_mid is not None else None),
            'is_anomaly_segment': has_anomaly,
            'anomaly_count': anom_cnt,
        }

        # ---------- 各信号统计:mean/std/cv ----------
        for col in signal_columns:
            if col in sdf_warm.columns:
                s = pd.to_numeric(sdf_warm[col], errors='coerce').dropna()
                if len(s):
                    m = float(s.mean())
                    sd = float(s.std()) if len(s) > 1 else 0.0
                    cv = (sd / m) if m != 0 else None
                    row[f'{col}_mean'] = round(m, 4)
                    row[f'{col}_std'] = round(sd, 4)
                    row[f'{col}_cv'] = round(cv, 4) if cv is not None else None
                else:
                    row[f'{col}_mean'] = None
                    row[f'{col}_std'] = None
                    row[f'{col}_cv'] = None
            else:
                # 兼容: 若 col == 'FC_VARVoltage'(方差) 但原信号里没有,就就地从
                # FC_AvgCellVoltage 的段内方差(=std^2) 近似生成
                if col == 'FC_VARVoltage' and 'FC_AvgCellVoltage' in sdf_warm.columns:
                    s_v = pd.to_numeric(sdf_warm['FC_AvgCellVoltage'], errors='coerce').dropna()
                    if len(s_v) > 1:
                        var_v = float(s_v.var(ddof=1))  # 样本方差
                        row[f'{col}_mean'] = round(var_v, 6)
                        row[f'{col}_std'] = round(s_v.std(), 6)
                        row[f'{col}_cv'] = round((s_v.std() / float(s_v.mean())), 6) if float(s_v.mean()) != 0 else None
                    else:
                        row[f'{col}_mean'] = None
                        row[f'{col}_std'] = None
                        row[f'{col}_cv'] = None
                else:
                    logger.warning("段%d 缺少信号列 %s,该列置空", i, col)
                    row[f'{col}_mean'] = None
                    row[f'{col}_std'] = None
                    row[f'{col}_cv'] = None

        rows.append(row)
        logger.info("段%d 聚合完成: dur=%ds cur_avg=%.2fA warmup_drop=%.0fs anomaly=%s(%d) rt_mid=%s",
                    i, row['duration'], row['current_avg'] if row['current_avg'] is not None else 0,
                    warmup_dropped_s,
                    has_anomaly, anom_cnt, run_time_mid)

    df_out = pd.DataFrame(rows)
    logger.info("聚合完成: 输出 %d 段(剔除 %d 段),列数=%d, warmup默认=%ds",
                len(df_out), skipped, len(df_out.columns), warmup_seconds)
    return df_out


# ---------- 单元测试示例 ----------

if __name__ == '__main__':
    import sys
    from pathlib import Path
    # 直接运行脚本时,把项目根加入 sys.path,以便 import 同包/跨包模块
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    from performance.steady_state_selector import find_steady_segments
    from src.log_config import setup_logging
    setup_logging(level=logging.INFO)

    # 构造测试数据(1 秒采样, 600 点):
    #   0-399s 电流=95(稳态,中间200-201扰动会被合并成1段)
    #   400-599s 电流=60(偏离)
    #   索引100 注入电压骤降(<3.0) -> 段内含异常点
    n = 600
    ts_arr = pd.date_range('2026-08-22 00:00:00', periods=n, freq='1s')
    cur_arr = np.full(n, 95.0)
    cur_arr[200:202] = 200.0   # 短暂扰动
    cur_arr[400:600] = 60.0    # 后段偏离
    minv = np.full(n, 3.6)
    minv[100] = 2.5            # 段内注入异常点(电压骤降 < 3.0)
    df_test = pd.DataFrame({
        'Timestamp': ts_arr,
        'FC_CurrOut': cur_arr,
        'FC_VoltOut': np.full(n, 320.0),
        'FC_MinCellVoltage': minv,
        'FC_AvgCellVoltage': np.full(n, 3.6),
        'FC_NetPwrOut': np.round(cur_arr * 320 / 1000, 2),
        'FC_RunTime_Hours': np.cumsum(np.full(n, 1 / 3600)),
    })

    segs = find_steady_segments(df_test, target_current=95,
                                tolerance=5, min_duration=180)
    sigs = ['FC_VoltOut', 'FC_MinCellVoltage', 'FC_AvgCellVoltage', 'FC_NetPwrOut']

    print("\n===== 测试1: 保留异常段 (exclude_anomaly=False) =====")
    agg = aggregate_segments(segs, sigs, exclude_anomaly=False)
    print(agg[['segment_id', 'duration', 'current_avg',
               'is_anomaly_segment', 'anomaly_count',
               'FC_MinCellVoltage_mean', 'FC_VoltOut_mean']].to_string(index=False))
    assert len(agg) == 1, f"应输出1段,实际{len(agg)}"
    assert bool(agg.iloc[0]['is_anomaly_segment']), "段含异常点应标记为异常段"
    assert int(agg.iloc[0]['anomaly_count']) == 1
    # 段[0:400] 中点 idx=200, run_time 取该点实际累计运行时间(聚合输出4位小数,容差1e-3)
    expected_rt = float(df_test['FC_RunTime_Hours'].iloc[200])
    assert abs(float(agg.iloc[0]['run_time_at_mid']) - expected_rt) < 1e-3, \
        f"run_time_at_mid={agg.iloc[0]['run_time_at_mid']} 期望{expected_rt}"
    print("  [PASS] 异常段标记+中点运行时间正确")

    print("\n===== 测试2: 剔除异常段 (exclude_anomaly=True) =====")
    agg2 = aggregate_segments(segs, sigs, exclude_anomaly=True)
    print(f"剔除后剩余段数: {len(agg2)}")
    assert len(agg2) == 0, f"含异常的段应被剔除,实际剩{len(agg2)}"
    print("  [PASS] 异常段被正确剔除")

    print("\n===== 测试3: 无异常的稳态段(正常聚合) =====")
    minv_clean = np.full(n, 3.6)  # 不注入异常
    df_clean = df_test.copy()
    df_clean['FC_MinCellVoltage'] = minv_clean
    segs_clean = find_steady_segments(df_clean, 95, 5, 180)
    agg3 = aggregate_segments(segs_clean, sigs, exclude_anomaly=True)
    print(agg3[['segment_id', 'duration', 'current_avg',
               'is_anomaly_segment', 'FC_MinCellVoltage_cv']].to_string(index=False))
    assert len(agg3) == 1
    assert not bool(agg3.iloc[0]['is_anomaly_segment'])
    # 电压恒定3.6, std=0 -> cv=0
    assert float(agg3.iloc[0]['FC_MinCellVoltage_cv']) == 0.0
    print("  [PASS] 正常段聚合+变异系数(cv=0)正确")

    print("\n===== 测试4: 空段输入 =====")
    empty = aggregate_segments([], sigs, exclude_anomaly=True)
    assert len(empty) == 0
    print("  [PASS] 空输入返回空DataFrame")

    print("\n===== 测试5: 缺少信号列(容错) =====")
    df_miss = df_clean.drop(columns=['FC_NetPwrOut'])
    segs_miss = find_steady_segments(df_miss, 95, 5, 180)
    agg5 = aggregate_segments(segs_miss, ['FC_NetPwrOut', 'FC_VoltOut'],
                              exclude_anomaly=True)
    assert len(agg5) == 1
    assert agg5.iloc[0]['FC_NetPwrOut_mean'] is None  # 缺列置空
    assert agg5.iloc[0]['FC_VoltOut_mean'] is not None
    print("  [PASS] 缺失信号列置空,不影响其他列")

    print("\n[OK] 全部测试通过")
