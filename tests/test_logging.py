"""日志测试脚本:运行所有 mock 数据,触发 WARNING/ERROR 分支。

运行: python tests/test_logging.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

# 项目根加入 sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.log_config import setup_logging
# 切到 DEBUG 才能看到所有日志细节
setup_logging(level=logging.DEBUG)

from src.data_loader import load_vehicle_csvs, mark_invalid
from src.metrics import (
    cell_voltage_consistency,
    h2_system,
    power_summary,
    vehicle_overview,
)

TESTS_DIR = ROOT / "tests"


def banner(title: str) -> None:
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70)


def main() -> None:
    # ===== 用例 1: 文件名不匹配规则 =====
    banner("用例 1: 文件名不符合规则")
    bad_name_file = TESTS_DIR / "random_file_with_bad_name.csv"
    df = load_vehicle_csvs([str(bad_name_file)])
    print(f"  → 返回 DataFrame 行数: {len(df)} (应为 0)")

    # ===== 用例 2: 文件名 OK 但缺 Timestamp 列 =====
    banner("用例 2: 缺 Timestamp 列")
    no_ts_file = TESTS_DIR / "900002_202607071800_202607072359_CH0_20260807_225246.csv"
    df = load_vehicle_csvs([str(no_ts_file)])
    print(f"  → 返回 DataFrame 行数: {len(df)} (应为 0)")

    # ===== 用例 3: 含 Timestamp 空、哨兵值、负值、字符串的混合文件 =====
    banner("用例 3: 混合异常数据(空 Timestamp / 65535 / 负值 / 字符串)")
    mixed_file = TESTS_DIR / "900001_202607071800_202607072359_CH0_20260807_225246.csv"
    df = load_vehicle_csvs([str(mixed_file)])
    print(f"  → 返回 DataFrame 行数: {len(df)} (应为 5,因 1 行空 Timestamp 被剔除)")

    if len(df) == 0:
        print("  → 数据为空,后续指标计算测试跳过")
        return

    # ===== 用例 4: 异常值打标(mark_invalid 触发 WARNING)=====
    banner("用例 4: 异常值打标 mark_invalid")
    flagged = mark_invalid(df)
    n_invalid_cols = [c for c in flagged.columns if c.startswith("__") and c.endswith("_invalid")]
    print(f"  → 标记的列数: {len(n_invalid_cols)}")
    for c in n_invalid_cols:
        n = int(flagged[c].sum())
        if n:
            print(f"    {c}: {n} 条异常")

    # ===== 用例 5: 指标计算(触发 WARNING: 列过滤后无数据等)=====
    banner("用例 5: 指标计算 vehicle_overview")
    ov = vehicle_overview(df)
    print(f"  → 产出指标: {len(ov)} 个")
    for k, v in list(ov.items())[:5]:
        print(f"    {k}: {v}")

    banner("用例 6: 单片电压一致性 cell_voltage_consistency")
    cvc = cell_voltage_consistency(df)
    print(f"  → 产出键: {list(cvc.keys())}")

    banner("用例 7: 功率与效率 power_summary")
    pw = power_summary(df)
    print(f"  → 产出键: {list(pw.keys())}")

    banner("用例 8: 氢系统状态 h2_system")
    h2 = h2_system(df)
    print(f"  → 产出键: {list(h2.keys())}")

    # ===== 用例 9: 路径不存在(触发 ERROR 文件读取失败)=====
    banner("用例 9: 路径不存在")
    df = load_vehicle_csvs(["/no/such/path/file.csv"])
    print(f"  → 返回 DataFrame 行数: {len(df)} (应为 0)")

    print("\n" + "=" * 70)
    print("  所有日志测试用例已执行,请检查上方 [WARNING] / [ERROR] 输出")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    main()
