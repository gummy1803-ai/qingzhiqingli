"""数据质量扫描模块:扫描 DataFrame,检测关键字段全 0 等异常,生成质量简报。

被以下场景复用:
- scan_hyd_zero.py: 命令行扫描历史 CSV
- app.py: 上传新数据时即时扫描 + 邮件报警
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

from src.log_config import get_logger
from src.metrics import _safe_num

logger = get_logger(__name__)

# 哨兵值(参考 data_loader.INVALID_SENTINELS)
SENTINELS = {65535, -1, 999, 99}

# 阈值:0 行占比超过此比例即认为"主要异常"
ZERO_RATIO_THRESHOLD = 0.5

# 默认扫描的关键字段(可扩展)
DEFAULT_CHECK_FIELDS = [
    "FC_HydCmPerHundred", "FC_HydCmInstts", "FC_VehicleSpd",
    "FC_VehicleKM", "FC_NetPwrOut", "FC_CurrOut",
    "FC_MinCellVoltage", "FC_MaxCellVoltage", "FC_ErrorCode",
]


def classify_risk(zero_ratio: float, total_rows: int) -> str:
    """根据 0 占比判定风险等级。

    - 高危: 全 0 比例 > 50% (含全部为 0)
    - 中危: 0 < 全 0 比例 <= 50%
    - 低危: 全 0 比例 == 0
    - 无数据: total_rows == 0
    """
    if total_rows == 0:
        return "无数据"
    if zero_ratio > ZERO_RATIO_THRESHOLD:
        return "高危"
    if zero_ratio > 0:
        return "中危"
    return "低危"


def scan_field(col: pd.Series, field_name: str) -> dict:
    """扫描单列字段的 0 值/哨兵值/NaN 情况。"""
    num = _safe_num(col)
    n = len(num)
    if n == 0:
        return {"field": field_name, "total": 0, "zero": 0, "sentinel": 0,
                "nan": 0, "nonzero": 0, "zero_ratio": 0, "risk_level": "无数据"}

    zero = int((num == 0).sum())
    sentinel = int(num.isin(SENTINELS).sum())
    nan = int(num.isna().sum())
    nonzero = n - zero - sentinel - nan
    zero_ratio = round(zero / n, 4) if n else 0
    return {
        "field": field_name,
        "total": n,
        "zero": zero,
        "sentinel": sentinel,
        "nan": nan,
        "nonzero": nonzero,
        "zero_ratio": zero_ratio,
        "risk_level": classify_risk(zero_ratio, n),
    }


def scan_df(df: pd.DataFrame, vehicle: str = "未知车辆",
            fields: list[str] | None = None) -> dict:
    """扫描一个 DataFrame 的关键字段质量。

    Args:
        df: 已加载的车辆数据
        vehicle: 车辆标识(用于简报)
        fields: 要扫描的字段列表,默认 DEFAULT_CHECK_FIELDS

    Returns:
        dict: {
            "vehicle": str,
            "total_rows": int,
            "scanned_at": str,
            "fields": [scan_field 返回的 dict, ...],
            "high_risk_fields": [str, ...],
            "overall_risk": "高危" | "中危" | "低危",
        }
    """
    fields = fields or DEFAULT_CHECK_FIELDS
    scanned_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logger.info("质量扫描开始: 车辆=%s / 输入 %d 行 / 检查 %d 个字段",
                vehicle, len(df), len(fields))

    field_results = []
    high_risk = []
    for f in fields:
        if f not in df.columns:
            logger.debug("  字段 %s 不存在,跳过", f)
            continue
        r = scan_field(df[f], f)
        field_results.append(r)
        if r["risk_level"] == "高危":
            high_risk.append(f)
            logger.warning("  字段 %s 高危: 0占比=%.1f%%",
                           f, r["zero_ratio"] * 100)

    # 整体风险: 任一字段高危即整体高危
    if high_risk:
        overall = "高危"
    elif any(r["risk_level"] == "中危" for r in field_results):
        overall = "中危"
    else:
        overall = "低危"

    logger.info("质量扫描完成: 整体=%s / 高危字段=%d 个 / 总扫描=%d 个",
                overall, len(high_risk), len(field_results))

    return {
        "vehicle": vehicle,
        "total_rows": len(df),
        "scanned_at": scanned_at,
        "fields": field_results,
        "high_risk_fields": high_risk,
        "overall_risk": overall,
    }


def generate_brief(result: dict) -> str:
    """把扫描结果生成纯文本质量简报。"""
    lines = []
    lines.append("=" * 60)
    lines.append("  数据质量简报")
    lines.append("=" * 60)
    lines.append(f"  车辆: {result['vehicle']}")
    lines.append(f"  总行数: {result['total_rows']:,}")
    lines.append(f"  扫描时间: {result['scanned_at']}")
    lines.append(f"  整体风险: {result['overall_risk']}")
    lines.append("")
    lines.append(f"  {'字段':<25}{'总行':>10}{'0值':>10}{'哨兵':>8}"
                 f"{'NaN':>8}{'0占比':>10}{'风险':>8}")
    lines.append("  " + "-" * 80)
    for f in result["fields"]:
        lines.append(f"  {f['field']:<25}{f['total']:>10,}{f['zero']:>10,}"
                     f"{f['sentinel']:>8,}{f['nan']:>8,}"
                     f"{f['zero_ratio']:>10.1%}{f['risk_level']:>8}")
    lines.append("")
    if result["high_risk_fields"]:
        lines.append("  ⚠ 高危字段: " + ", ".join(result["high_risk_fields"]))
        lines.append("  建议立即排查对应采集设备/上传流程")
    else:
        lines.append("  ✓ 未发现高危字段")
    lines.append("=" * 60)
    return "\n".join(lines)


def save_brief(brief: str, out_dir: Path | str = "reports",
               vehicle: str = "unknown") -> Path:
    """把简报保存到文件,返回路径。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"quality_brief_{vehicle}_{ts}.txt"
    out_path.write_text(brief, encoding="utf-8")
    logger.info("质量简报已保存: %s", out_path)
    return out_path
