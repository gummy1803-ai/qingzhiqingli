"""扫描所有历史 CSV,统计关键字段全 0 的车辆数量 + 风险等级。

复用 src/data_quality.py 模块的核心扫描逻辑,本脚本只负责:
- 遍历 02_整车数据处理 下所有车辆目录
- 读 CSV 文件并合并
- 调用 scan_df 扫描
- 打印控制台表格 + 保存 CSV 结果文件

用法:
    python scan_hyd_zero.py
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.log_config import setup_logging
setup_logging(level=logging.WARNING)

# ============================================================
# ✅ 全局 DB 降级机制: 启动就初始化, 后续若调用 DB 操作天然带保护
# ============================================================
from durability.database import (
    init_db as _db_init,
    print_console_db_status,
)
_db_init()

from src.data_quality import classify_risk
from src.metrics import _safe_num

CSV_BASE = ROOT / "企业资料包02_氢质氢离" / "02_整车数据处理"
REPORT_DIR = ROOT / "reports"
REPORT_DIR.mkdir(exist_ok=True)


def _scan_vehicle_full(car_dir: Path, files: list[Path]) -> dict:
    """实际读取所有 CSV 并扫描 FC_HydCmPerHundred 字段。"""
    total_rows = 0
    zero_rows = 0
    sentinel_rows = 0
    nan_rows = 0
    nonzero_sample: list[float] = []

    for f in files:
        try:
            df = pd.read_csv(f, usecols=["FC_HydCmPerHundred"],
                             dtype={"FC_HydCmPerHundred": "object"})
        except Exception as e:
            logging.warning("读取 %s 失败: %s", f.name, e)
            continue
        col = _safe_num(df["FC_HydCmPerHundred"])
        total_rows += len(col)
        zero_rows += int((col == 0).sum())
        sentinel_rows += int(col.isin({65535, -1, 999, 99}).sum())
        nan_rows += int(col.isna().sum())
        valid = col[(col != 0) & (~col.isin({65535, -1, 999, 99})) & (~col.isna())]
        if len(nonzero_sample) < 5:
            for v in valid.head(5 - len(nonzero_sample)):
                nonzero_sample.append(float(v))

    # 状态判定
    if total_rows == 0:
        status = "空数据"
    elif zero_rows == total_rows:
        status = "全部为0"
    elif (zero_rows + nan_rows + sentinel_rows) == total_rows and zero_rows > 0:
        status = "无有效值(全0+哨兵+NaN)"
    elif zero_rows / total_rows > 0.5:
        status = "主要异常(>50%为0)"
    else:
        status = "正常"

    zero_ratio = round(zero_rows / total_rows, 4) if total_rows else 0
    nonzero_rows = total_rows - zero_rows - sentinel_rows - nan_rows
    return {
        "car": car_dir.name,
        "files": len(files),
        "total_rows": total_rows,
        "zero_rows": zero_rows,
        "sentinel_rows": sentinel_rows,
        "nan_rows": nan_rows,
        "nonzero_rows": nonzero_rows,
        "zero_ratio": zero_ratio,
        "status": status,
        "risk_level": classify_risk(zero_ratio, total_rows),
        "nonzero_sample": nonzero_sample,
    }


def save_results_csv(results: list[dict], out_path: Path | None = None) -> Path:
    """把扫描结果保存为 CSV 文件,包含风险等级列。"""
    if out_path is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = REPORT_DIR / f"quality_scan_{ts}.csv"

    rows = []
    for r in results:
        rows.append({
            "车辆": r.get("car", ""),
            "文件数": r.get("files", 0),
            "总行数": r.get("total_rows", 0),
            "0行数": r.get("zero_rows", 0),
            "哨兵值行数": r.get("sentinel_rows", 0),
            "NaN行数": r.get("nan_rows", 0),
            "非0有效行数": r.get("nonzero_rows", 0),
            "0占比": r.get("zero_ratio", 0),
            "状态": r.get("status", ""),
            "风险等级": r.get("risk_level", ""),
            "非0样本(前5)": ";".join(str(x) for x in r.get("nonzero_sample", [])),
        })
    df = pd.DataFrame(rows)
    df.to_csv(out_path, index=False, encoding="utf-8-sig")
    return out_path


def main() -> None:
    print("=" * 80)
    print("  FC_HydCmPerHundred 字段全 0 车辆扫描")
    print("=" * 80)
    print(f"  扫描目录: {CSV_BASE}\n")

    # ✅ 控制台横幅: 展示 DB 后端状态
    print_console_db_status("Step 0 · DB 初始化状态")

    if not CSV_BASE.exists():
        print(f"  [错误] 目录不存在: {CSV_BASE}")
        return

    car_dirs = sorted([d for d in CSV_BASE.iterdir() if d.is_dir()])
    print(f"  发现 {len(car_dirs)} 个车辆目录\n")
    print(f"{'车辆':<10}{'文件数':>8}{'总行数':>12}{'0行数':>12}"
          f"{'哨兵值':>10}{'NaN':>10}{'非0行':>10}{'0占比':>10}"
          f"{'风险等级':>10}  状态")
    print("-" * 110)

    results = []
    high_risk_count = 0
    all_zero_count = 0
    for car_dir in car_dirs:
        r = _scan_vehicle_full(car_dir, sorted(car_dir.glob("*.csv")))
        results.append(r)
        if r["risk_level"] == "高危":
            high_risk_count += 1
        if r["status"] in ("全部为0", "无有效值(全0+哨兵+NaN)"):
            all_zero_count += 1
        if r.get("total_rows", 0):
            print(f"{r['car']:<10}{r['files']:>8}{r['total_rows']:>12,}"
                  f"{r['zero_rows']:>12,}{r['sentinel_rows']:>10,}"
                  f"{r['nan_rows']:>10,}{r['nonzero_rows']:>10,}"
                  f"{r['zero_ratio']:>10.2%}{r['risk_level']:>10}  {r['status']}")
        else:
            print(f"{r['car']:<10}{r.get('files', 0):>8}"
                  f"{'-':>12}{'-':>12}{'-':>10}{'-':>10}{'-':>10}"
                  f"{'-':>10}{'-':>10}  {r['status']}")

    csv_path = save_results_csv(results)
    print(f"\n  → 结果已保存: {csv_path}")

    print("\n" + "=" * 80)
    print("  扫描结果汇总")
    print("=" * 80)
    total_cars = len(results)
    cars_with_data = sum(1 for r in results if r.get("total_rows", 0) > 0)
    print(f"  车辆总数: {total_cars}")
    print(f"  有数据车辆: {cars_with_data}")
    print(f"  全 0 车辆: {all_zero_count}  "
          f"(占比 {all_zero_count / cars_with_data * 100:.1f}% of 有数据)" if cars_with_data else
          f"  全 0 车辆: 0")
    print(f"  高危车辆: {high_risk_count}  (0占比 > 50%)")

    from collections import Counter
    risk_cnt = Counter(r["risk_level"] for r in results)
    print(f"\n  风险等级分类:")
    for risk, cnt in risk_cnt.most_common():
        print(f"    {risk}: {cnt} 辆")

    print("\n" + "-" * 80)
    if high_risk_count == 0:
        print("  结论:【低风险】 没有发现高危车辆(全 0 比例 <= 50%)")
    elif high_risk_count == cars_with_data:
        print("  结论:【普遍问题】 所有有数据车辆都是高危,采集流程存在系统性故障")
    elif high_risk_count > cars_with_data / 2:
        print("  结论:【普遍问题】 超半数车辆为高危,采集流程存在系统性故障")
    else:
        print(f"  结论:【部分问题】 {high_risk_count}/{cars_with_data} 辆车为高危,"
              f"需排查对应车辆的采集设备/上传流程")
    print("-" * 80)

    # ✅ 结尾再次输出 DB 运行时状态 (确认期间是否发生降级)
    print_console_db_status("扫描结束 · DB 运行时状态")


if __name__ == "__main__":
    main()
