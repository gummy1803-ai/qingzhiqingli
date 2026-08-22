"""端到端流程:用真实测试数据生成一份最终报告。

运行: python run_e2e.py
输出: reports/测试报告_<车辆>.html

⚠️  全局 DB 降级机制:
- 启动时自动从 .env 加载腾讯云 MySQL 配置并调用 init_db()
- MySQL 不可达 (外网断开/超时/密码错) 会自动降级到本地 SQLite (data/app.db)
- 降级时会在日志和控制台打印醒目的 [DB 降级] 横幅, 便于排查
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.log_config import setup_logging
# 同时输出到文件,便于排查;控制台只 INFO,文件 DEBUG
setup_logging(level=logging.INFO, log_file=str(ROOT / "logs" / "e2e_run.log"))

# ============================================================
# ✅ 全局生效: 启动阶段就初始化数据库 + 降级机制
# 这里 import database 会触发:
#   1. _load_db_config() 从 .env 加载 MySQL 配置
#   2. 模块级构建 SQLAlchemy Engine
# main() 开头再调用 init_db() 建库建表 + 打印后端横幅
# ============================================================
from durability import database as db_module
from durability.database import (
    init_db as db_init,
    get_db_backend_info,
    print_console_db_status,  # ✅ 统一横幅辅助函数
)

from src.data_loader import load_durability_docx, load_durability_metadata, load_vehicle_csvs
from src.data_quality import classify_risk
from src.metrics import (
    _safe_num,
    cell_voltage_consistency,
    fault_time_series,
    h2_system,
    power_summary,
    vehicle_overview,
    vehicle_speed_profile,
)
from src.report import build_report_html

logger = logging.getLogger(__name__)

_FALLBACK_BANNER = "═" * 70

DATA_ROOT = ROOT / "企业资料包02_氢质氢离"
CSV_BASE = DATA_ROOT / "02_整车数据处理"
DOCX_BASE = DATA_ROOT / "01_耐久原始数据处理"
REPORT_DIR = ROOT / "reports"
REPORT_DIR.mkdir(exist_ok=True)


def banner(title: str) -> None:
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70)


def _precheck_vehicle_inventory() -> list[dict]:
    """Step 0.5 · 新车目录预检: 扫一下 02_整车数据处理 下全部车辆 + 0 占比摘要。

    只读取 FC_HydCmPerHundred 列 (1 col), 不加载所有信号, 速度快, 用于
    主程序启动时给用户一眼看到"我加的新车有没有被识别 + 数据是否健康"。
    """
    print("\n" + "─" * 70)
    print("  Step 0.5 预检 · 整车目录自动识别 (新增车型 ← 这里自动看到)")
    print("─" * 70)
    if not CSV_BASE.exists():
        print(f"  [警告] 目录不存在: {CSV_BASE}")
        return []
    car_dirs = sorted([d for d in CSV_BASE.iterdir() if d.is_dir()])
    print(f"  扫描目录: {CSV_BASE}")
    print(f"  自动识别车辆数: {len(car_dirs)}")
    header = f"  {'车辆':<8}{'CSV数':>6}{'总行数':>10}{'0行数':>10}{'0占比':>9}{'风险':>8}  状态"
    print("  " + "-" * (len(header) - 2))
    print(header)
    results = []
    high_risk = 0
    all_zero = 0
    for car_dir in car_dirs:
        files = sorted(car_dir.glob("*.csv"))
        total_rows = 0
        zero_rows = 0
        nan_rows = 0
        sentinel_rows = 0
        nonzero_rows = 0
        for f in files:
            try:
                df_col = pd.read_csv(f, usecols=["FC_HydCmPerHundred"],
                                     dtype={"FC_HydCmPerHundred": "object"})
            except Exception:
                # 某些 CSV 可能还没这个字段, 跳过即可, 不中断主流程
                continue
            col = _safe_num(df_col["FC_HydCmPerHundred"])
            total_rows += len(col)
            zero_rows += int((col == 0).sum())
            sentinel_rows += int(col.isin({65535, -1, 999, 99}).sum())
            nan_rows += int(col.isna().sum())
        if total_rows:
            nonzero_rows = max(total_rows - zero_rows - sentinel_rows - nan_rows, 0)
            zero_ratio = zero_rows / total_rows
            risk = classify_risk(zero_ratio, nonzero_rows)
            if nonzero_rows == 0:
                status = "全部为0" if zero_ratio >= 1 else "无有效值(全0+哨兵+NaN)"
            else:
                status = "正常" if risk not in ("中危", "高危") else "需复核"
            if risk == "高危":
                high_risk += 1
            if status in ("全部为0", "无有效值(全0+哨兵+NaN)"):
                all_zero += 1
            print(f"  {car_dir.name:<8}{len(files):>6}{total_rows:>10,}{zero_rows:>10,}"
                  f"{zero_ratio:>8.1%}{risk:>8}  {status}")
        else:
            status = "无数据/无匹配字段"
            print(f"  {car_dir.name:<8}{len(files):>6}{'-':>10}{'-':>10}{'-':>9}{'-':>8}  {status}")
            zero_ratio = 0.0
            risk = "无数据"
        results.append(dict(car=car_dir.name, files=len(files),
                            total_rows=total_rows, zero_rows=zero_rows,
                            zero_ratio=zero_ratio, risk_level=risk, status=status))
    print(f"\n  汇总: 共 {len(results)} 车 | 高危 {high_risk} 辆 | 全部无有效值 {all_zero} 辆")
    logger.info("[主程序预检·车辆] 共 %d 车, 高危 %d 辆, 无有效值 %d 辆 | 明细=%s",
                len(results), high_risk, all_zero,
                [(r["car"], r["files"], r["risk_level"]) for r in results])
    return results


def _precheck_feishu_contacts() -> list[dict]:
    """Step 0.6 预检 · 飞书对接人清单 + 已验证/禁用状态 + 🔑密钥过期自动检测。

    保证主程序入口也能一眼看到"现在哪些人会收到预警 + 谁的密钥已经过期(code=10003等)"。
    密钥检测对 (app_id, secret) 自动去重, 5 分钟内同组只打一次飞书接口, 不烧限流配额。
    """
    print("\n" + "─" * 70)
    print("  Step 0.6 预检 · 飞书对接人清单 + 🔑密钥过期自动检测")
    print("─" * 70)
    try:
        from durability.feishu_contacts import (
            list_contacts,
            detect_all_credentials_status,
            credentials_status_text,
        )
        from durability.database import get_db_backend_info
    except Exception as e:
        print(f"  [警告] 导入 feishu_contacts 失败: {e}")
        return []
    db_info = get_db_backend_info()
    all_cs = list_contacts()
    total = len(all_cs)
    verified_n = sum(1 for c in all_cs if c.get("verified"))
    enabled_n = sum(1 for c in all_cs if c.get("enabled"))
    verified_and_enabled = sum(1 for c in all_cs if c.get("verified") and c.get("enabled"))
    print(f"  存储后端: {db_info['backend']}  {db_info.get('host_or_path','')}")
    print(f"  总联系人 {total} 人 | 已启用 {enabled_n} 人 | 已验证(可推预警) {verified_n} 人 | "
          f"启用且已验证 {verified_and_enabled} 人")

    # ---- 🔑 新增: 密钥自动检测 (跳过禁用的, 走 5 分钟缓存, 预检只测启用的) ----
    creds_result = None
    try:
        if total > 0:
            print("  🔑 正在检测所有联系人密钥 (同 AppID+Secret 只测一次)...")
            creds_result = detect_all_credentials_status(skip_disabled=True, use_cache=True)
            sm = creds_result.get("summary", {})
            hit = creds_result.get("cache_hit")
            age = creds_result.get("checked_seconds_ago") or 0
            print(
                f"  🔑 检测完成 {'(命中缓存 {} 前)'.format(f'{age:.0f}s') if hit else ''} | "
                f"去重 App 组={creds_result.get('app_groups')} 总耗时={creds_result.get('total_elapsed_ms',0):.0f}ms"
            )
            print(
                f"     有效={sm.get('valid',0)}  失效/过期={sm.get('invalid',0)}  "
                f"超时={sm.get('timeout',0)}  网络错={sm.get('network_err',0)}  "
                f"跳过禁用={sm.get('skipped_disabled',0)}"
            )
            if sm.get("invalid"):
                print(
                    "     ⚠️  存在 {} 个失效密钥 (通常 code=10003 = App Secret 错误或已重置, "
                    "请到飞书管理后台 → 凭证与基础信息 重新复制 Secret 粘贴)".format(sm["invalid"])
                )
    except Exception as e:
        # 检测失败只降级为告警, 不影响主程序
        print(f"  ⚠️  密钥自动检测失败 (不影响主流程): {e}")
        creds_result = None

    if total == 0:
        print("  (空) 暂无联系人, 请在 Streamlit 页面『🚨飞书对接预警』Tab 新增并发送测试消息")
        return []
    header = f"  {'姓名':<10}{'启用':>4}{'验证':>4}  {'app_id':<14}  🔑密钥状态"
    print("  " + "-" * (len(header) + 10))
    print(header)
    per_c = (creds_result or {}).get("per_contact", {})
    for c in all_cs:
        entry = per_c.get(c.get("id"), {})
        text = credentials_status_text(entry.get("status",""), entry.get("code"))
        elapsed = entry.get("elapsed_ms", 0)
        detail = f"{text} ({elapsed:.0f}ms)" if elapsed > 0 else text
        oid = c.get("open_id") or ""
        _ = oid  # 这里只展示密钥状态, 不再打印脱敏 open_id (让表格更宽更可读)
        print(f"  {str(c.get('name','')):<10}"
              f"{'✅' if c.get('enabled') else '⛔':>4}"
              f"{'✅' if c.get('verified') else '🔲':>4}  "
              f"{c.get('app_id','') or '-':<14}  {detail}")
    logger.info("[主程序预检·飞书] 存储=%s 总=%d 已启用=%d 已验证=%d 启用且已验证=%d | 姓名=%s",
                db_info["backend"], total, enabled_n, verified_n, verified_and_enabled,
                [c.get("name") for c in all_cs])
    if creds_result is not None:
        logger.info("[主程序预检·飞书·密钥] cache_hit=%s age_s=%s app_groups=%d 总耗时=%.0fms | summary=%s",
                    creds_result.get("cache_hit"), creds_result.get("checked_seconds_ago"),
                    creds_result.get("app_groups"), creds_result.get("total_elapsed_ms", 0),
                    creds_result.get("summary"))
    return all_cs


def main() -> None:
    t_all = time.perf_counter()
    banner("端到端流程开始:使用真实测试数据生成最终报告")

    # ============== Step 0: 初始化数据库 (全局生效降级机制) ==============
    banner("Step 0: 初始化数据库 (MySQL → 自动降级 SQLite)")
    t0_db = time.perf_counter()
    db_init()
    # ✅ 统一横幅格式 (与其他入口文件完全一致)
    print_console_db_status("主程序 Step 0 · DB 初始化状态")
    print(f"  DB 初始化耗时: {(time.perf_counter() - t0_db)*1000:.0f}ms")
    db_info = get_db_backend_info()
    logger.info(
        "[主程序入口] DB 初始化完成 | 后端=%s | info=%s | 耗时=%.0fms",
        db_info["backend"], db_info, (time.perf_counter() - t0_db) * 1000,
    )

    # ============== Step 0.5 / 0.6: 新增车型 + 飞书对接预检 (全局可见) ==============
    banner("Step 0.5 / 0.6 预检: 自动识别车型 + 飞书对接人 (全局集成)")
    _precheck_vehicle_inventory()
    _precheck_feishu_contacts()

    # ============== Step 1: 加载整车 CSV (支持任意新车型目录) ==============
    banner("Step 1: 加载整车 CSV (自动扫描 02_整车数据处理 下全部车型目录)")
    car_data: dict[str, pd.DataFrame] = {}
    for car_dir in sorted(CSV_BASE.iterdir()):
        if not car_dir.is_dir():
            continue
        files = sorted(car_dir.glob("*.csv"))
        logger.info(">> 车辆 %s: %d 个 CSV 分片", car_dir.name, len(files))
        t0 = time.perf_counter()
        df = load_vehicle_csvs([str(f) for f in files])
        logger.info(">> %s 加载完成: %d 行 / 耗时 %.2fs",
                    car_dir.name, len(df), time.perf_counter() - t0)
        print(f"  → 车辆 {car_dir.name}: {len(df):,} 行")
        if len(df):
            car_data[car_dir.name] = df

    if not car_data:
        logger.error("未加载到任何车辆数据,终止")
        print("[错误] 未加载到任何车辆数据")
        return

    # ============== Step 2: 加载耐久 docx ==============
    banner("Step 2: 加载耐久 docx(01_耐久原始数据处理)")
    docx_files = sorted(DOCX_BASE.glob("*.docx"))
    print(f"  → 发现 {len(docx_files)} 个 docx 文件")
    t0 = time.perf_counter()
    dur_df = load_durability_docx([str(f) for f in docx_files])
    logger.info("耐久 docx 加载完成: %d 行 / 耗时 %.2fs",
                len(dur_df), time.perf_counter() - t0)
    print(f"  → 耐久长表: {len(dur_df):,} 行 / {dur_df['stage'].nunique() if len(dur_df) else 0} 个阶段")

    # 元数据
    meta_df = load_durability_metadata([str(f) for f in docx_files])
    print(f"  → 耐久元数据: {len(meta_df)} 份 docx")

    # ============== Step 3: 计算指标(对每辆车)==============
    banner("Step 3: 计算指标(整车运行概览/单片一致性/功率/氢系统)")

    # 用第一辆车(212)作为主报告对象
    rep_car = list(car_data.keys())[0]
    rep_df = car_data[rep_car]
    print(f"  → 报告主车辆: {rep_car} ({len(rep_df):,} 行)")

    t0 = time.perf_counter()
    overview = vehicle_overview(rep_df)
    cell_c = cell_voltage_consistency(rep_df)
    power = power_summary(rep_df)
    h2 = h2_system(rep_df)
    logger.info("指标计算耗时 %.2fs", time.perf_counter() - t0)

    # 关键指标摘要打印
    print("\n  --- 关键指标摘要 ---")
    for k in ["运行时长(h)", "行驶里程(km)", "平均车速(km/h)",
              "百公里氢耗均值(kg)", "瞬时氢耗均值(kg/h)",
              "启动次数", "故障码种类", "故障总数", "采样点数"]:
        if k in overview:
            print(f"    {k}: {overview[k]}")
    if "最弱通道Top5" in cell_c:
        print(f"    最弱通道Top5: {cell_c['最弱通道Top5']}")

    # ============== Step 4: 故障时间序列(辅助验证)==============
    banner("Step 4: 故障时间序列提取")
    faults = fault_time_series(rep_df)
    print(f"  → 故障记录: {len(faults):,} 条")
    if len(faults):
        print(f"  → 前 5 条故障:")
        print(faults.head().to_string(index=False))

    # ============== Step 5 & 6: 为所有已加载车辆生成 HTML 报告 ==============
    # 兼容旧行为: 第一辆车仍然写 测试报告_<首车>.html (方便老路径/文档引用)
    # 额外所有车都写 reports/<车号>_<YYYYMMDD_HHMM>_report.html (带时间戳, 多次运行不覆盖)
    banner(f"Step 5: 生成 HTML 报告 (共 {len(car_data)} 辆车)")
    ts_str = time.strftime("%Y%m%d_%H%M")
    t_start_reports = time.perf_counter()
    all_reports: list[tuple[str, Path]] = []

    for idx, car in enumerate(car_data.keys(), start=1):
        df_car = car_data[car]
        print(f"\n  [{idx}/{len(car_data)}] 车辆 {car}: {len(df_car):,} 行")
        t0 = time.perf_counter()
        ov_car = vehicle_overview(df_car)
        cc_car = cell_voltage_consistency(df_car)
        pw_car = power_summary(df_car)
        h2_car = h2_system(df_car)
        html = build_report_html(
            vehicle=car,
            df=df_car,
            overview=ov_car,
            cell_consist=cc_car,
            power=pw_car,
            h2=h2_car,
        )
        # 主输出路径: 带时间戳, 不覆盖
        out_ts = REPORT_DIR / f"{car}_{ts_str}_report.html"
        out_ts.write_text(html, encoding="utf-8")
        all_reports.append((car, out_ts))
        # 首车额外写一份 旧名字 (兼容之前的链接/文档)
        if idx == 1:
            out_legacy = REPORT_DIR / f"测试报告_{car}.html"
            out_legacy.write_text(html, encoding="utf-8")
            print(f"    → 主报告(兼容旧路径): {out_legacy}")
        # 如果只有两辆车, 第二辆车也写 测试报告_<car>.html (和历史 Step 6 输出一致)
        elif idx == 2 and len(car_data) == 2:
            out_legacy_cmp = REPORT_DIR / f"测试报告_{car}.html"
            out_legacy_cmp.write_text(html, encoding="utf-8")
            print(f"    → 对比报告(兼容旧路径): {out_legacy_cmp}")
        dt_ms = (time.perf_counter() - t0) * 1000
        print(f"    → {out_ts.name}: {len(html)//1024} KB / 耗时 {dt_ms:.0f} ms")
        # 关键指标简要 (避免每次都翻 html)
        for k in ["运行时长(h)", "行驶里程(km)", "百公里氢耗均值(kg)", "故障总数", "采样点数"]:
            if k in ov_car:
                print(f"       {k}: {ov_car[k]}")

    print(f"\n  全部 {len(all_reports)} 份报告耗时: {time.perf_counter()-t_start_reports:.1f}s")

    # ============== 总结 ==============
    banner("端到端流程完成")
    print(f"  总耗时: {time.perf_counter() - t_all:.2f}s")
    print(f"  日志文件: {ROOT / 'logs' / 'e2e_run.log'}")
    print(f"  报告目录: {REPORT_DIR}")
    print(f"  在浏览器打开上述 .html 文件即可查看,Ctrl+P 可打印为 PDF")

    # ✅ 统一横幅: 运行时 DB 状态 (期间是否降级一眼可见)
    print_console_db_status("主程序结束 · DB 运行时状态")
    if db_module._fallback_triggered:
        # 如果确实发生过降级, 再加一条高亮提醒 (日志里也有 [DB 降级] 横幅)
        print(f"  ⚠️  运行期间发生过 MySQL→SQLite 降级 (请检查 logs/e2e_run.log)")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    main()
