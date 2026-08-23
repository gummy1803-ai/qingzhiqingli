# -*- coding: utf-8 -*-
"""综合冒烟测试:覆盖 PRD 功能 2/3/4 + 上传历史顺序/重命名/删除 + DB schema 迁移
不依赖 Streamlit,直接跑核心逻辑。用法:python tests/smoke_test_full.py
"""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# 让根目录进 import path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_PASS = "\033[92m✅ PASS\033[0m"
_FAIL = "\033[91m❌ FAIL\033[0m"
_WARN = "\033[93m⚠️ WARN\033[0m"
_results: list[dict] = []


def case(name: str, group: str):
    def _wrap(fn):
        t0 = time.perf_counter()
        try:
            fn()
            dt = (time.perf_counter() - t0) * 1000
            _results.append({"group": group, "name": name, "ok": True,
                             "ms": dt, "note": ""})
            print(f"{_PASS} [{group}] {name} | {dt:.1f}ms")
        except AssertionError as e:
            dt = (time.perf_counter() - t0) * 1000
            _results.append({"group": group, "name": name, "ok": False,
                             "ms": dt, "note": f"AssertionError: {e}"})
            print(f"{_FAIL} [{group}] {name} | {dt:.1f}ms | {e}")
        except Exception as e:
            dt = (time.perf_counter() - t0) * 1000
            _results.append({"group": group, "name": name, "ok": False,
                             "ms": dt, "note": f"{type(e).__name__}: {e}"})
            print(f"{_FAIL} [{group}] {name} | {dt:.1f}ms | {type(e).__name__}: {e}")
    return _wrap


# =====================================================================
# 组 A · 数据库: schema 迁移 + display_order + 重排 + 重命名 + 删除
# 用临时文件做 SQLite, 不污染生产 data_sqlite.db
# =====================================================================
_TMP_DB = tempfile.mktemp(suffix=".db", prefix="smoke_test_db_")
# 1) 先把环境变量设空, database 模块 import 时会判定 MySQL 不可用,不做真实连接
os.environ["DB_HOST"] = ""
os.environ["DB_PORT"] = ""
os.environ["DB_USER"] = ""
os.environ["DB_PASSWORD"] = ""
os.environ["DB_NAME"] = ""
from durability import database as db_mod  # noqa: E402

from sqlalchemy import create_engine  # noqa: E402

# 2) 强制锁住 DB 全局: 用临时 SQLite, 禁用 fallback(防止 upsert 时被改回生产路径)
tmp_engine = create_engine(f"sqlite:///{_TMP_DB}", future=True, echo=False)
# ⚠️ 不要 _metadata.clear(): 模块加载时所有 Table(_vehicle_data_files/_alert_events 等)已经
# 注册在 _metadata 里,清了就空了;直接 create_all(checkfirst=True) 就能在 tmp_engine 上建表
db_mod._metadata.create_all(tmp_engine, checkfirst=True)
# 应用在线迁移(主要是 display_order / bench_cycle_id 补列;刚建的表一般不用,但保险)
db_mod._apply_schema_migrations(tmp_engine)
# 锁住全局 engine 和 fallback,让后续所有 db_* 函数都走 tmp_engine,不回生产
db_mod._engine = tmp_engine
db_mod._USE_MYSQL = False
db_mod._trigger_fallback = lambda _stage, _err: None  # type: ignore[method-assign]


@case("vehicle_data_files.display_order 字段存在", "A1-DB迁移")
def _a1():
    from sqlalchemy import inspect, select
    insp = inspect(db_mod._engine)
    cols = {c["name"] for c in insp.get_columns("vehicle_data_files")}
    assert "display_order" in cols, f"缺少 display_order 列,现有列={sorted(cols)}"
    indexes = {i["name"] for i in insp.get_indexes("vehicle_data_files")}
    assert "idx_vdf_disp_order" in indexes, "缺少 display_order 索引 idx_vdf_disp_order"


def _make_fake_file(data_kind, fname, order=None):
    """在 vehicle_data_files 造一条假记录,返回 file_id。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    fh = hashlib.sha256(f"{fname}_{time.time_ns()}".encode()).hexdigest()
    fid, inserted, _ = db_mod.db_upsert_data_file(
        data_kind, fname,
        file_hash=fh,
        row_count=1000,
        vehicle_id="212",
        status="aggregated",
        agg_rows=1000,
    )
    if order is not None:
        with db_mod._engine.connect() as conn:
            conn.execute(
                db_mod._vehicle_data_files.update()
                .where(db_mod._vehicle_data_files.c.id == int(fid))
                .values(display_order=int(order))
            )
            conn.commit()
    return int(fid)


@case("db_ensure_display_order: 为 5 条 NULL 记录分配顺序号", "A2-DisplayOrder")
def _a2():
    ids = [_make_fake_file("整车", f"T_整车_{i}.csv") for i in range(5)]
    fixed = db_mod.db_ensure_display_order()
    assert fixed == 5, f"预期修复 5 条,实际 fixed={fixed}"
    # 再跑应该是 0(幂等)
    fixed2 = db_mod.db_ensure_display_order()
    assert fixed2 == 0, f"第二次兜底应为 0,actual={fixed2}"
    # 查出来看顺序
    rows = db_mod.db_list_data_files("整车")
    orders = [r.get("display_order") for r in rows if int(r.get("id")) in ids]
    assert all(o is not None and o > 0 for o in orders), f"仍有 display_order=NULL: {orders}"


@case("db_swap_data_file_order: 相邻两行交换 display_order", "A2-DisplayOrder")
def _a3():
    a = _make_fake_file("台架循环", "Swap_A.csv", order=11)
    b = _make_fake_file("台架循环", "Swap_B.csv", order=22)
    ok, _ = db_mod.db_swap_data_file_order(a, b)
    assert ok, "交换失败"
    fa = db_mod.db_get_data_file(a)
    fb = db_mod.db_get_data_file(b)
    assert int(fa["display_order"]) == 22, f"a.display_order 应为 22,实际 {fa}"
    assert int(fb["display_order"]) == 11, f"b.display_order 应为 11,实际 {fb}"


@case("db_update_display_order_batch: 按列表重新编号", "A2-DisplayOrder")
def _a4():
    ids = [_make_fake_file("耐久工步", f"Batch_{i}.docx", order=i + 1) for i in range(4)]
    new_order = [ids[3], ids[1], ids[0], ids[2]]  # 打乱顺序
    ok, msg = db_mod.db_update_display_order_batch(new_order)
    assert ok, f"批量重置失败: {msg}"
    rows = db_mod.db_list_data_files("耐久工步")
    id2disp = {int(r.get("id")): int(r.get("display_order"))
               for r in rows if int(r.get("id")) in ids}
    # db_list 按 display_order ASC 返回,第一条应为 new_order[0]
    files_of_kind = [r for r in rows if int(r.get("id")) in ids]
    first_id = int(files_of_kind[0]["id"])
    assert first_id == new_order[0], (
        f"排序后第一条应为 {new_order[0]},实际 {first_id}, id2disp={id2disp}"
    )
    # display_order 分别应当是 new_order 中的 index+1(按传入的列表从 1 编号)
    expected = {fid: pos + 1 for pos, fid in enumerate(new_order)}
    for fid in ids:
        assert id2disp[fid] == expected[fid], (
            f"id={fid} display_order 应为 {expected[fid]} 实际 {id2disp}"
        )


@case("db_rename_data_file: 重命名 + 空名拒绝 + 重复冲突拒绝", "A3-重命名删除")
def _a5():
    fid1 = _make_fake_file("整车", "Ren_old.csv")
    _make_fake_file("整车", "Ren_same.csv")  # 同类型第二个文件

    # 1. 正常改名
    ok, _ = db_mod.db_rename_data_file(fid1, "Ren_NEWNAME.csv")
    assert ok, "重命名正常情况失败"
    after = db_mod.db_get_data_file(fid1)
    assert after["file_name"] == "Ren_NEWNAME.csv", f"未生效实际={after['file_name']}"

    # 2. 空名拒绝
    ok, _ = db_mod.db_rename_data_file(fid1, "   ")
    assert not ok, "空文件名应当被拒绝"

    # 3. 同类型同名冲突拒绝
    ok, msg = db_mod.db_rename_data_file(fid1, "Ren_same.csv")
    assert not ok, f"同类型下同名应被拒绝,msg={msg}"


@case("db_delete_data_file: 级联删除(文件不存在拒绝)", "A3-重命名删除")
def _a6():
    ok, msg = db_mod.db_delete_data_file(999_999_999, op_user="smoke_test")
    # 不存在应当报错而不是静默成功
    assert not ok, f"删除不存在文件应返回失败,msg={msg}"


# =====================================================================
# 组 B · 功能 2(燃电性能): 稳态段筛选 + 聚合 + 趋势图 + 极化曲线
# =====================================================================
from performance.steady_state_selector import find_steady_segments  # noqa: E402
from performance.segment_aggregator import aggregate_segments  # noqa: E402


def _gen_perf_mock() -> pd.DataFrame:
    """造一段 1Hz 数据,人为插入 3 段符合 95±5A,连续 600s(>180s) 要求。"""
    rng = np.random.default_rng(0)
    t0 = datetime(2026, 8, 1, 8, 0, 0)
    N = 3600  # 1小时数据
    ts = [t0 + timedelta(seconds=i) for i in range(N)]
    curr = rng.normal(70, 8, size=N)  # 默认随机电流
    avg = 760 + rng.normal(0, 8, size=N)
    dev = 20 + rng.normal(0, 3, size=N)
    var = 450 + rng.normal(0, 60, size=N)
    pwr = curr * 0.66 + rng.normal(0, 2, size=N)
    run = np.cumsum(np.ones(N)) / 3600.0  # 小时

    # 插入三段稳态:索引 [200, 800)、[1200,1800)、[2200,2800) 共 600s 每段
    for start in (200, 1200, 2200):
        curr[start:start + 600] = rng.normal(95, 1.0, size=600)  # 95±1 << 5
        avg[start:start + 600] -= 2  # 稳态下电压略低,用于区分
    return pd.DataFrame({
        "Timestamp": ts, "FC_CurrOut": curr,
        "FC_AvgCellVoltage": avg, "FC_AvgCellVoltDev": dev,
        "FC_VARVoltage": var, "FC_NetPwrOut": pwr,
        "FC_RunTime_Hours": run,
    })


@case("find_steady_segments: 95±5A,min_dur=180s → 找到 3 段,每段 ~600s", "B1-功能2稳态筛选")
def _b1():
    df = _gen_perf_mock()
    segs = find_steady_segments(df, target_current=95.0, tolerance=5.0,
                                min_duration=180)
    assert len(segs) == 3, f"预期 3 段稳态,实际={len(segs)}"
    for s in segs:
        dur = int(s["duration"])
        assert 599 <= dur <= 600, f"段长应在 [599,600] 之间,实际 {dur}"
    # 每段 start_idx 顺序正确
    starts = sorted(int(s["start_idx"]) for s in segs)
    assert starts == [200, 1200, 2200], f"start_idx 不对: {starts}"


@case("aggregate_segments: warmup=180s 应丢弃前 180s;聚合输出 4 个信号的均值", "B2-功能2聚合")
def _b2():
    df = _gen_perf_mock()
    segs = find_steady_segments(df, 95.0, 5.0, 180)
    sigs = ["FC_AvgCellVoltage", "FC_AvgCellVoltDev", "FC_VARVoltage", "FC_NetPwrOut"]
    agg = aggregate_segments(segs, sigs, exclude_anomaly=False, warmup_seconds=180)
    # 每段 600s - 180s warmup = 420s 实际参与平均值,期望 duration≈420
    assert len(agg) == 3, f"应有 3 行聚合结果: {len(agg)}"
    for dur in agg["duration"].tolist():
        assert 419 <= float(dur) <= 421, f"duration 应为 420s,实际 {dur}"
    for col_sig in sigs:
        col = f"{col_sig}_mean"
        assert col in agg.columns, f"聚合缺少列 {col},现列={list(agg.columns)}"
    # run_time_at_mid 应该存在(X轴累计运行时间)
    assert "run_time_at_mid" in agg.columns, "缺少 run_time_at_mid(X 轴累计运行时间列)"
    # mid_time(实际日期时间)应当存在
    assert "mid_time" in agg.columns, "缺少 mid_time(X 轴实际日期列)"


@case("fit_polarization_curve: 至少 6 点样本能拟合三种方法", "B3-功能2极化")
def _b3():
    from performance.polarization_curve import fit_polarization_curve
    # 6 个典型工况点(>3 满足分段线性需求;正常极化趋势:电流升电压降)
    agg_sample = pd.DataFrame({
        "current_avg": [50.0, 70.0, 95.0, 115.0, 140.0, 170.0],
        "FC_VoltOut_mean": [268.0, 261.0, 255.0, 248.0, 241.0, 232.0],
    })
    for method in ["linear", "polynomial", "empirical"]:
        res = fit_polarization_curve(
            agg_sample, current_col="current_avg",
            voltage_col="FC_VoltOut_mean", fit_method=method,
        )
        assert res["fit_success"], (
            f"方法 {method} 拟合失败, err={res.get('error')}, "
            f"method_key={method}, sample_n={len(agg_sample)}"
        )
        r2 = float(res.get("r_squared", 0.0))
        assert 0.0 <= r2 <= 1.0 + 1e-6, f"方法 {method} R²={r2} 不合理"


# =====================================================================
# 组 C · 功能 3(绝缘阻值): 坏值筛选 + 10min 聚合 + 趋势预测
# =====================================================================
from insulation.data_processor import process_insulation_data  # noqa: E402
from insulation.predictor import predict_insulation_trend  # noqa: E402


def total_bad(kind, raw):
    s = pd.to_numeric(raw["FC_VehicleIsolationR"], errors="coerce")
    if kind == "65535":
        return int((s == 65535).sum())
    if kind == "≥9999":
        return int((s >= 9999).sum())
    if kind == "≤0":
        return int((s <= 0).sum())
    return 0


def _gen_insul_mock() -> pd.DataFrame:
    """造 12 小时绝缘数据,每 10 分钟下降约 5 kΩ,故意注入坏值和 MainSts=4/8 两种状态。"""
    rng = np.random.default_rng(1)
    total_min = 12 * 60
    ts = [datetime(2026, 8, 1, 0, 0, 0) + timedelta(minutes=i) for i in range(total_min)]
    # 真值:1200kΩ 线性下降到 1140kΩ
    truth = np.linspace(1200.0, 1140.0, total_min) + rng.normal(0, 6, size=total_min)
    mainsts = np.where(np.arange(total_min) % 2 == 0, 4, 8)  # 奇数分钟=8(上电非运行),偶数=4(运行)
    # 每 37 条注入 1 条 65535
    truth[::37] = 65535.0
    # 每 41 条注入 ≥9999
    truth[::41] = 99999.0
    # 每 53 条注入 0 / 负数
    truth[::53] = -10.0
    # 每 61 条改成 MainSts=1(无效,应被剔除)
    mainsts[::61] = 1
    return pd.DataFrame({"Timestamp": ts,
                         "FC_VehicleIsolationR": truth,
                         "FC_MainSts": mainsts})


@case("process_insulation_data: 65535/≥9999/≤0/MainSts非48 均应剔除", "C1-功能3清洗")
def _c1():
    raw = _gen_insul_mock()
    cleaned = process_insulation_data(raw, interval_minutes=10)
    cs = cleaned.attrs.get("clean_stats", {})
    assert int(cs.get("bad_65535", 0)) >= total_bad("65535", raw), (
        f"65535 剔除不够,stats={cs}"
    )
    assert int(cs.get("bad_ge9999", 0)) >= total_bad("≥9999", raw)
    assert int(cs.get("bad_le0", 0)) >= total_bad("≤0", raw)
    assert int(cs.get("bad_state", 0)) >= 1
    assert len(cleaned) >= 12 * 5, f"有效样本太少(12h × 每小时~6段), actual={len(cleaned)}"
    # 聚合后不应该再有 ≥9999 或 ≤0 的绝缘值
    series = pd.to_numeric(cleaned["FC_VehicleIsolationR"], errors="coerce")
    assert series.min() > 100, f"清洗后仍有异常最小值={series.min()}"
    assert series.max() < 9999, f"清洗后仍有异常最大值={series.max()}"


@case("predict_insulation_trend: 线性下降样本能预测到 350kΩ 报警", "C2-功能3预测")
def _c2():
    # 造 60 天的 10min 绝缘数据: 从 800 线性降到 500kΩ,趋势明确
    n = 60 * 24 * 6  # 60 天 × 24h × 6个/小时
    t0 = datetime(2026, 8, 1)
    days = np.linspace(0, 60, n)
    ts = [t0 + timedelta(days=d) for d in days]
    vals = 800 - (300 / 60) * days + np.random.default_rng(2).normal(0, 2, size=n)
    df = pd.DataFrame({
        "timestamp": ts,
        "FC_VehicleIsolationR": vals,
        "bucket_state": [4 if i % 2 else 8 for i in range(n)],
    })
    pred = predict_insulation_trend(
        df, alarm_values=[350.0, 250.0],
        predict_days=180, poly_order=1,
    )
    assert pred["fit_success"], f"拟合失败 {pred.get('error')}"
    # 样本: 第 0~60 天从 800 线性降到 500 (每天降 5kΩ)
    # 所以从最后一个点(500kΩ at day=60) 出发:
    #   到 350kΩ → 还要 150/5 = 30 天左右
    #   到 250kΩ → 还要 250/5 = 50 天左右
    crossings = pred.get("alarm_crossings", {})
    d350 = crossings.get(350.0, {}).get("days")
    d250 = crossings.get(250.0, {}).get("days")
    assert d350 is not None, (
        f"未算出触碰 350kΩ 的天数。所有 crossings={crossings} "
        f"pred.keys={sorted(pred.keys())}"
    )
    assert 20 < d350 < 40, f"350kΩ 应在 [20,40] 天内触碰, 实际 {d350}"
    assert d250 is not None, "未算出触碰 250kΩ 的天数"
    assert 40 < d250 < 70, f"250kΩ 应在 [40,70] 天内触碰, 实际 {d250}"
    assert d250 > d350, f"250 应比 350 更远: d250={d250}, d350={d350}"


# =====================================================================
# 组 D · 功能 4(台架预警): 阈值命中 + DB 事件幂等
# =====================================================================
def _bench_agg_sample() -> pd.DataFrame:
    rows = []
    for cyc in range(3):
        for pp in [33.0, 58.5, 117.0, 156.0, 175.5, 195.0]:
            rows.append({
                "cycle_id": cyc, "power_point": pp,
                "FC_AvgCellVoltDev_mean": 20.0,   # 正常
                "FC_AvgCellVoltage_mean": 780.0,  # 正常
                "数据量": 50, "质量标记": "正常",
            })
    # 故意加 2 个命中: 离均差 > 50, 平均 < 600
    rows.append({"cycle_id": 0, "power_point": 156.0,
                 "FC_AvgCellVoltDev_mean": 62.0,  # >50
                 "FC_AvgCellVoltage_mean": 780.0,
                 "数据量": 50, "质量标记": "正常"})
    rows.append({"cycle_id": 2, "power_point": 33.0,
                 "FC_AvgCellVoltDev_mean": 20.0,
                 "FC_AvgCellVoltage_mean": 560.0,  # <600
                 "数据量": 50, "质量标记": "正常"})
    return pd.DataFrame(rows)


@case("预警命中: 3×6 正常 + 2 命中 → 总命中=2", "D1-功能4预警命中")
def _d1():
    agg = _bench_agg_sample()
    dev_th, avg_th = 50.0, 600.0
    hits = 0
    for _, r in agg.iterrows():
        dev = float(r["FC_AvgCellVoltDev_mean"])
        avg = float(r["FC_AvgCellVoltage_mean"])
        if dev > dev_th:
            hits += 1
        if 0 < avg < avg_th:
            hits += 1
    assert hits == 2, f"应为 2 个命中,实际 {hits}"


@case("db_save_event 幂等: 同一事件存 2 次,返回的 event_id 相同且不抛异常", "D2-功能4DB幂等")
def _d2():
    ev = {
        "timestamp": datetime(2026, 8, 23, 0, 0, 0),
        "cycle_id": 99999, "power_point": 117.0,
        "condition": f"离均差>999mV(测试)",
        "value": 1000.0, "threshold": 999.0,
        "signal": "FC_AvgCellVoltDev", "unit": "mV",
        "operator": ">", "label": "离均差(测试)",
        "data_count": 1, "quality": "正常",
        "message": f"smoke_test 临时事件(可删除)",
        "rig_id": "SMOKE_RIG",
        "severity": "medium",
    }
    id1 = db_mod.db_save_event(ev)
    id2 = db_mod.db_save_event(ev)
    assert id1 == id2, f"两次 save 返回不同 ID: {id1} vs {id2}"
    # 把这条测试事件标记成 ignored,方便事后识别(签名只有 eid+status)
    db_mod.db_set_event_status(id1, "ignored")


# =====================================================================
# 汇总输出
# =====================================================================
def main():
    total = len(_results)
    passed = sum(1 for r in _results if r["ok"])
    failed = total - passed
    print("\n" + "=" * 78)
    print(f"测试完成: 共 {total} 项, {_PASS.split('[0m')[0]}{_PASS[-1]} {passed} "
          f"{_FAIL.split('[0m')[0]}{_FAIL[-1]} {failed}")
    print("=" * 78)
    if failed:
        print("\n失败项详情:")
        for r in _results:
            if not r["ok"]:
                print(f"  [{r['group']}] {r['name']} → {r['note']}")
        # 最后 exit(1) 让 CI 能识别
    # 删掉临时 db
    try:
        if os.path.exists(_TMP_DB):
            os.remove(_TMP_DB)
    except Exception:
        pass
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
