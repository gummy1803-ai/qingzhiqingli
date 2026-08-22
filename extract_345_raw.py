"""从原始 CSV 提取 345 车辆所有原始行,导出为单独文件方便发给采集方排查。

输出:
- reports/345_raw_export.csv (单文件,保留所有原始列)
- reports/345_raw_export_summary.txt (摘要:行数/列数/文件来源/字段空值率)
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.log_config import setup_logging
setup_logging(level=logging.INFO)

# ============================================================
# ✅ 全局 DB 降级机制: 启动就初始化, 后续若调用 DB 操作天然带保护
# ============================================================
from durability.database import (
    init_db as _db_init,
    print_console_db_status,
)
_db_init()

logger = logging.getLogger(__name__)

# 目标车辆目录 (支持命令行传入: `python extract_345_raw.py 555` 默认 345)
TARGET_CAR = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].strip() else "345"
CSV_DIR = ROOT / "企业资料包02_氢质氢离" / "02_整车数据处理" / TARGET_CAR
REPORT_DIR = ROOT / "reports"
REPORT_DIR.mkdir(exist_ok=True)

# 输出文件
OUT_CSV = REPORT_DIR / f"{TARGET_CAR}_raw_export.csv"
OUT_SUMMARY = REPORT_DIR / f"{TARGET_CAR}_raw_export_summary.txt"


def main() -> None:
    print("=" * 70)
    print(f"  提取车辆 {TARGET_CAR} 原始数据")
    print("=" * 70)
    print(f"  源目录: {CSV_DIR}")
    print(f"  用法  : python {Path(__file__).name} <车号>")

    # ✅ 控制台横幅: 展示 DB 后端状态
    print_console_db_status("Step 0 · DB 初始化状态")

    if not CSV_DIR.exists():
        print(f"  [错误] 目录不存在: {CSV_DIR}")
        available = []
        base = CSV_DIR.parent
        if base.exists():
            available = sorted(p.name for p in base.iterdir() if p.is_dir())
        if available:
            print(f"  [提示] 02_整车数据处理 下现有车辆目录: {', '.join(available)}")
            print(f"  [提示] 例如: python {Path(__file__).name} {available[0]}")
        return

    files = sorted(CSV_DIR.glob("*.csv"))
    print(f"  发现 CSV 文件: {len(files)} 个")

    # 拼接所有 CSV,保留所有原始列(不应用任何过滤/清洗)
    dfs = []
    for f in files:
        try:
            # 用 utf-8-sig 兼容 BOM,low_memory=False 避免类型推断警告
            df = pd.read_csv(f, encoding="utf-8-sig", low_memory=False)
            # 标注来源文件,方便采集方定位
            df["__source_file"] = f.name
            dfs.append(df)
            logger.info("  读 %s: %d 行 / %d 列", f.name, len(df), df.shape[1])
        except Exception as e:
            logger.error("  读 %s 失败: %s", f.name, e)

    if not dfs:
        print("  [错误] 没有成功读取的文件")
        return

    merged = pd.concat(dfs, ignore_index=True, sort=False)
    print(f"\n  合并完成: {len(merged):,} 行 / {merged.shape[1]} 列")

    # 关键字段空值/0 值率(便于采集方快速定位问题)
    print("\n  关键字段空值/0 值率:")
    key_fields = ["FC_HydCmPerHundred", "FC_HydCmInstts", "FC_VehicleSpd",
                  "FC_VehicleKM", "FC_NetPwrOut", "FC_CurrOut",
                  "FC_MinCellVoltage", "FC_MaxCellVoltage", "FC_ErrorCode"]
    summary_lines = []
    for col in key_fields:
        if col not in merged.columns:
            line = f"    {col:<25} : [字段不存在]"
        else:
            n = len(merged)
            zero = (merged[col].astype(str).str.strip() == "0").sum()
            empty = merged[col].isna().sum()
            line = (f"    {col:<25} : 0值={zero:>10,} ({zero/n:.1%})  "
                    f"NaN={empty:>8,} ({empty/n:.1%})  总={n:,}")
        print(line)
        summary_lines.append(line)

    # 导出 CSV(保留原始数据 + 来源标注)
    print(f"\n  导出 CSV: {OUT_CSV}")
    merged.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    size_kb = OUT_CSV.stat().st_size // 1024
    print(f"  体积: {size_kb:,} KB")

    # 写摘要文件
    with OUT_SUMMARY.open("w", encoding="utf-8") as f:
        f.write(f"车辆 {TARGET_CAR} 原始数据摘要\n")
        f.write("=" * 60 + "\n")
        f.write(f"源目录: {CSV_DIR}\n")
        f.write(f"CSV 文件数: {len(files)}\n")
        f.write(f"合并后总行数: {len(merged):,}\n")
        f.write(f"合并后总列数: {merged.shape[1]}\n")
        f.write(f"导出文件: {OUT_CSV}\n")
        f.write(f"导出体积: {size_kb:,} KB\n\n")
        f.write("关键字段空值/0 值率:\n")
        f.write("-" * 60 + "\n")
        for line in summary_lines:
            f.write(line + "\n")
        f.write("\n说明:\n")
        f.write("- 本文件保留所有原始数据未做任何清洗\n")
        f.write("- __source_file 列标注每行来自哪个原始 CSV 分片\n")
        f.write("- 重点关注 FC_HydCmPerHundred 字段:数据显示 100% 全 0\n")
        f.write("- 请采集方核实该字段的采集/上传流程\n")

    print(f"  摘要文件: {OUT_SUMMARY}")
    print("\n" + "=" * 70)
    print(f"  完成。请把以下两个文件发给采集方排查:")
    print(f"    1. {OUT_CSV}")
    print(f"    2. {OUT_SUMMARY}")
    print("=" * 70)

    # ✅ 结尾再次输出 DB 运行时状态
    print_console_db_status("提取结束 · DB 运行时状态")


if __name__ == "__main__":
    main()
