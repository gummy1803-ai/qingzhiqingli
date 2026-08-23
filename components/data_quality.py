"""数据质量分析模块:Mock 与真实数据差异对比(加分功能)。

在「燃电运行看板」Tab 统计卡片下方以折叠面板呈现:
- 三大核心指标 st.metric:数据完整率 / 异常率(3σ) / 采样均匀度
- 真实数据:采样间隔分布直方图 + 各列缺失比例 + 3σ 异常值计数
- Mock 数据:完整率 100% 提示 + 工况曲线参数设定值
- 对比模式:同时加载两组数据,上下堆叠对比质量指标(窄屏友好)
"""
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from utils.helpers import SIGNAL_MAP

logger = logging.getLogger(__name__)


# ---------- 辅助函数 ----------

def _ts_col(df: pd.DataFrame) -> Optional[str]:
    """兼容查找时间戳列(大写 Timestamp / 小写 timestamp)。"""
    for c in ('Timestamp', 'timestamp'):
        if c in df.columns:
            return c
    return None


def _missing_ratio(df: pd.DataFrame) -> pd.Series:
    """各列缺失值百分比。"""
    miss = (df.isna().mean() * 100).round(2)
    logger.info("缺失率计算: %d 列, 平均缺失=%.2f%%, 行数=%d",
                len(miss), float(miss.mean()), len(df))
    # 逐列打印具体缺失位置,便于排查数据采集问题
    for col in df.columns:
        na_mask = df[col].isna()
        na_cnt = int(na_mask.sum())
        if na_cnt == 0:
            continue
        na_idx = df.index[na_mask].tolist()
        logger.warning("缺失[%s]: %d 个 (位置行号=%s)",
                       col, na_cnt, na_idx[:20])
    return miss


def _anomaly_3sigma(df: pd.DataFrame) -> dict:
    """各数值列超出 mean±3σ 的样本数。

    详细日志输出每个异常点的:行号 / 原始值 / μ / σ / 阈值 / 偏离方向。
    """
    res: dict = {}
    for col in df.select_dtypes(include='number').columns:
        s = df[col].dropna()
        if len(s) == 0:
            continue
        mu, sd = float(s.mean()), float(s.std())
        if pd.isna(sd) or sd == 0:
            res[col] = 0
            logger.info("3σ跳过[%s]: σ=0 或 NaN,无法判定异常", col)
            continue
        lo, hi = mu - 3 * sd, mu + 3 * sd
        mask = (s < lo) | (s > hi)
        cnt = int(mask.sum())
        res[col] = cnt
        if cnt:
            logger.info("3σ检测[%s]: μ=%.4f σ=%.4f 阈值=[%.4f, %.4f] 异常数=%d",
                        col, mu, sd, lo, hi, cnt)
            # 逐点打印每个异常的具体位置、数值、偏离方向与距离
            for idx, val in s[mask].items():
                if val < lo:
                    direction = f"低于下界(μ-3σ={lo:.4f}), 偏离={lo - val:.4f}"
                else:
                    direction = f"高于上界(μ+3σ={hi:.4f}), 偏离={val - hi:.4f}"
                logger.warning("  异常点[%s] 行#%s 值=%.4f %s",
                               col, idx, val, direction)
        else:
            logger.info("3σ检测[%s]: μ=%.4f σ=%.4f 阈值=[%.4f, %.4f] 无异常",
                        col, mu, sd, lo, hi)
    logger.info("3σ检测完成: %d 数值列, 异常总数=%d",
                len(res), sum(res.values()))
    return res


def _anomaly_details(df: pd.DataFrame) -> pd.DataFrame:
    """返回每个 3σ 异常点的明细:列/行号/原始值/μ/σ/下界/上界/偏离方向/偏离量。

    用于在面板里直接展示具体数值和计算过程,便于排查。
    """
    rows: list[dict] = []
    for col in df.select_dtypes(include='number').columns:
        s = df[col].dropna()
        if len(s) == 0:
            continue
        mu, sd = float(s.mean()), float(s.std())
        if pd.isna(sd) or sd == 0:
            continue
        lo, hi = mu - 3 * sd, mu + 3 * sd
        mask = (s < lo) | (s > hi)
        for idx, val in s[mask].items():
            if val < lo:
                direction, delta = '低于下界(μ-3σ)', lo - val
            else:
                direction, delta = '高于上界(μ+3σ)', val - hi
            rows.append({
                '信号': SIGNAL_MAP.get(col, col),
                '列名': col,
                '行号': idx,
                '原始值': round(float(val), 4),
                'μ': round(mu, 4),
                'σ': round(sd, 4),
                '下界(μ-3σ)': round(lo, 4),
                '上界(μ+3σ)': round(hi, 4),
                '偏离方向': direction,
                '偏离量': round(float(delta), 4),
            })
    logger.info("异常明细生成: 共 %d 个异常点", len(rows))
    return pd.DataFrame(rows)


def _interval_stats(df: pd.DataFrame):
    """采样间隔统计:返回 (mean, std, cv, diffs_series) 或 None。"""
    tc = _ts_col(df)
    if tc is None:
        logger.warning("采样间隔跳过: 未找到时间戳列(Timestamp/timestamp)")
        return None
    ts = pd.to_datetime(df[tc], errors='coerce').dropna().sort_values()
    if len(ts) < 2:
        logger.warning("采样间隔跳过: 有效时间戳不足2个 (有效=%d)", len(ts))
        return None
    diffs = ts.diff().dropna().dt.total_seconds()
    mean = diffs.mean()
    std = diffs.std()
    cv = (std / mean) if mean else 0  # 变异系数,越小越均匀
    logger.info("采样间隔: mean=%.2fs std=%.2fs cv=%.4f 样本数=%d",
                mean, std, cv, len(diffs))
    return mean, std, cv, diffs


def _fig_interval_hist(diffs: pd.Series) -> go.Figure:
    """采样间隔分布直方图(体现真实数据不均匀性)。"""
    fig = go.Figure(go.Histogram(
        x=diffs.values, nbinsx=40,
        marker_color='#00D4FF', opacity=0.85,
    ))
    fig.update_layout(
        paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
        font=dict(color='#E8EDF5'), height=280,
        margin=dict(l=40, r=20, t=20, b=40),
        xaxis=dict(title='间隔(秒)',
                   gridcolor='rgba(255,255,255,0.08)', color='#E8EDF5'),
        yaxis=dict(title='频次',
                   gridcolor='rgba(255,255,255,0.08)', color='#E8EDF5'),
    )
    return fig


# Mock 工况参数设定(与 utils/mock_data.py 生成逻辑一致)
# 单位严格遵循企业 9 字段口径:最小/平均单体电压为 mV,离均差为 mV,绝缘为 kΩ
_MOCK_PARAMS = {
    'FC_CurrOut':            '50-80(启动) / 150-400(运行) A',
    'FC_VoltOut':            '280-380 V',
    'FC_NetPwrOut':          '= I×V/1000 + 噪声, clip 0-150 kW',
    'FC_MinCellVoltage':     'avg-100~400 mV, 1% 骤降至 2000 mV (企业口径 mV)',
    'FC_MinVoltageChannel':  '1-120 整数',
    'FC_AvgCellVoltage':     '3300-3900 mV (企业口径 mV)',
    'FC_AvgCellVoltDev':     '-300 ~ +300 mV (企业口径 mV)',
    'FC_VehicleIsolationR':  '500-2000, 0.5% 掉至 300 kΩ',
    'FC_RunTime_Hours':      'base + 每秒累计递增 h',
}


def _quality_metrics(df: pd.DataFrame, label: str) -> None:
    """渲染单组数据的核心三指标 + describe(对比模式上下堆叠共用)。"""
    if df is None or len(df) == 0:
        logger.warning("质量指标跳过[%s]: df为空", label)
        st.warning(f'{label}: 无数据')
        return
    logger.info("质量指标计算开始[%s]: rows=%d cols=%d", label, len(df), len(df.columns))
    st.markdown(f"**{label}** · {len(df):,} 行")

    # 完整率 = 1 - 平均缺失率
    miss = _missing_ratio(df)
    completeness = 100 - (miss.mean() if len(miss) else 0)

    # 异常率(3σ) = 异常数 / (行数×数值列数)
    anom = _anomaly_3sigma(df)
    total_anom = sum(anom.values())
    denom = len(df) * max(len(anom), 1)
    anom_rate = (total_anom / denom * 100) if denom else 0

    # 采样均匀度 = max(0, 100 - cv×100),cv 越小越均匀
    ival = _interval_stats(df)
    uniformity = max(0, 100 - (ival[2] * 100)) if ival else 0

    logger.info("质量指标[%s]: 完整率=%.2f%% 异常率=%.4f%% (异常%d/分母%d) 采样均匀度=%.2f%%",
                label, completeness, anom_rate, total_anom, denom, uniformity)

    m1, m2, m3 = st.columns(3)
    m1.metric('数据完整率', f'{completeness:.1f}%')
    m2.metric('异常率(3σ)', f'{anom_rate:.2f}%')
    m3.metric('采样均匀度', f'{uniformity:.1f}%')

    st.markdown('**详细统计 (describe)**')
    st.dataframe(df.describe(), use_container_width=True)


def render_data_quality(
    df: pd.DataFrame,
    use_mock: bool,
    vehicle_id: str,
    start_dt,
    end_dt,
    real_df: Optional[pd.DataFrame] = None,
    mock_df: Optional[pd.DataFrame] = None,
) -> None:
    """渲染数据质量分析折叠面板。

    Args:
        df: 当前数据源 DataFrame
        use_mock: 当前是否为模拟数据
        vehicle_id/start_dt/end_dt: 当前筛选条件(仅用于上下文)
        real_df/mock_df: 对比模式时另一组数据(可选;None 则该侧无数据)
    """
    with st.expander('📊 数据质量分析', expanded=False):
        logger.info('数据质量分析入口: use_mock=%s vehicle=%s rows=%d',
                    use_mock, vehicle_id, len(df) if df is not None else 0)
        if df is None or len(df) == 0:
            logger.warning("数据质量分析终止: 当前无数据 (df=%s rows=%d)",
                           type(df).__name__, len(df) if df is not None else 0)
            st.warning('当前无数据,无法分析')
            return

        # 对比模式开关(勾选后,app.py 预加载另一组数据传入)
        do_compare = st.checkbox(
            '🔄 同时加载 Mock + 真实数据对比', value=False,
            key='dq_compare',
            help='同时加载两组数据(同车辆同时段),上下堆叠对比质量指标(窄屏友好)',
        )

        if not do_compare:
            # ---------- 单数据源详细分析 ----------
            label = '模拟数据(mock)' if use_mock else '真实数据'
            logger.info("分支: 单数据源分析 label=%s", label)
            _quality_metrics(df, label)
            st.markdown('---')

            if use_mock:
                # Mock:完整率 100% + 工况参数设定
                logger.info("分支: Mock 工况参数展示")
                st.success(
                    '当前为模拟数据,数据完整率 100%,无缺失值,'
                    '采样严格 1 秒均匀间隔'
                )
                st.markdown('**模拟参数设定(工况曲线)**')
                params = pd.DataFrame({
                    '信号': [SIGNAL_MAP.get(k, k) for k in _MOCK_PARAMS],
                    '设定值': list(_MOCK_PARAMS.values()),
                })
                st.dataframe(params, use_container_width=True, hide_index=True)
            else:
                # 真实数据:间隔直方图 + 缺失 + 3σ
                logger.info("分支: 真实数据详细分析(间隔/缺失/3σ)")
                ival = _interval_stats(df)
                if ival:
                    mean_i, std_i, cv_i, diffs = ival
                    logger.info("渲染采样间隔直方图: mean=%.2f std=%.2f cv=%.3f",
                                mean_i, std_i, cv_i)
                    st.markdown(
                        f"**采样间隔**:均值 {mean_i:.2f}s · 标准差 {std_i:.2f}s · "
                        f"变异系数 {cv_i:.3f} (越小越均匀)"
                    )
                    st.plotly_chart(_fig_interval_hist(diffs),
                                    use_container_width=True)
                else:
                    logger.warning("无采样间隔数据,跳过直方图渲染")

                st.markdown('**各列缺失值比例(%)**')
                st.dataframe(_missing_ratio(df).to_frame('缺失%'),
                             use_container_width=True)

                # 缺失值明细:逐行列出具体缺失位置
                miss_rows = []
                for col in df.columns:
                    na_mask = df[col].isna()
                    if na_mask.any():
                        for idx in df.index[na_mask]:
                            miss_rows.append({
                                '列名': col,
                                '信号': SIGNAL_MAP.get(col, col),
                                '行号': idx,
                            })
                if miss_rows:
                    logger.info("缺失值明细: 共 %d 条", len(miss_rows))
                    st.markdown('**缺失值明细(具体位置)**')
                    st.dataframe(pd.DataFrame(miss_rows),
                                 use_container_width=True, hide_index=True)

                st.markdown('**各列异常值数量(超出 mean±3σ)**')
                anom = _anomaly_3sigma(df)
                st.dataframe(pd.Series(anom).to_frame('3σ异常数'),
                             use_container_width=True)

                # 异常点明细:具体数值 + 计算过程(μ/σ/阈值/偏离)
                details = _anomaly_details(df)
                if len(details):
                    logger.info("异常点明细: 共 %d 条,渲染明细表", len(details))
                    st.markdown('**异常点明细(数值与计算过程)**')
                    st.dataframe(details, use_container_width=True,
                                 hide_index=True)
                else:
                    logger.info("无3σ异常点,显示提示")
                    st.info('未检测到 3σ 异常点')
        else:
            # ---------- 对比模式:上下堆叠(窄屏友好) ----------
            logger.info("分支: 对比模式(上下堆叠) mock_rows=%d real_rows=%d",
                        len(mock_df) if mock_df is not None else 0,
                        len(real_df) if real_df is not None else 0)
            st.info('对比模式:上方 Mock,下方真实数据(同车辆同时段)')
            _quality_metrics(mock_df, '模拟数据(mock)')
            st.markdown('---')
            _quality_metrics(real_df, '真实数据')
            st.caption('提示:Mock 采样严格 1s 均匀,真实数据常含缺失/抖动,'
                       '变异系数与缺失率差异即技术深度所在')
