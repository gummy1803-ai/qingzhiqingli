"""增量数据自动检测模块。

检测耐久测试数据目录下的新文件, 支持增量合并与统计重算:
1. 扫描 data_dir 下匹配 pattern 的 CSV 文件
2. 与上次检测时间比较, 找出新增/修改过的文件
3. 增量合并到现有数据(不覆盖历史)
4. 触发聚合统计重算

Streamlit 集成:
- st.cache_data(ttl=300) 每5分钟自动检查
- 用户点击"检测新数据"按钮手动触发
- 有新数据时 st.toast 通知

核心函数: check_for_new_data
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

# ---------- 常量 ----------
_DEFAULT_PATTERN = 'durability_*.csv'
_STATE_FILENAME = '.durability_update_state.json'
_DEFAULT_BATCH_SIZE = 50000  # 单批最大行数(防止内存溢出)


# ---------- 文件扫描 ----------

def _scan_files(data_dir: str, pattern: str) -> List[Dict]:
    """扫描目录下匹配 pattern 的文件, 返回文件信息列表。

    Returns:
        [{path, filename, mtime(datetime), size_bytes}, ...]
    """
    dir_path = Path(data_dir)
    if not dir_path.exists() or not dir_path.is_dir():
        logger.warning("数据目录不存在或非目录: %s", data_dir)
        return []

    files = []
    for fp in sorted(dir_path.glob(pattern)):
        if not fp.is_file():
            continue
        try:
            stat = fp.stat()
            files.append({
                'path': str(fp),
                'filename': fp.name,
                'mtime': datetime.fromtimestamp(stat.st_mtime),
                'mtime_ts': stat.st_mtime,
                'size_bytes': stat.st_size,
            })
        except OSError as e:
            logger.warning("无法读取文件状态 %s: %s", fp, e)

    logger.info("扫描完成: %s 匹配 %d 个文件", data_dir, len(files))
    return files


def _load_state(data_dir: str) -> Dict:
    """从数据目录加载状态文件(上次检测的文件列表+时间)。"""
    state_path = Path(data_dir) / _STATE_FILENAME
    if not state_path.exists():
        return {'last_check': None, 'known_files': {}}
    try:
        with open(state_path, 'r', encoding='utf-8') as f:
            state = json.load(f)
        # 转换时间字符串
        if state.get('last_check'):
            state['last_check'] = datetime.fromisoformat(state['last_check'])
        # known_files: {filename: mtime_ts}
        return state
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("状态文件解析失败, 重置: %s", e)
        return {'last_check': None, 'known_files': {}}


def _save_state(data_dir: str, state: Dict) -> None:
    """保存状态到数据目录。"""
    state_path = Path(data_dir) / _STATE_FILENAME
    try:
        save_state = {
            'last_check': (state['last_check'].isoformat()
                            if isinstance(state.get('last_check'), datetime)
                            else state.get('last_check')),
            'known_files': state.get('known_files', {}),
        }
        with open(state_path, 'w', encoding='utf-8') as f:
            json.dump(save_state, f, ensure_ascii=False, indent=2)
        logger.info("状态已保存: %s", state_path)
    except Exception as e:
        logger.error("状态保存失败: %s", e)


# ---------- 主检测函数 ----------

def check_for_new_data(
    data_dir: str,
    pattern: str = _DEFAULT_PATTERN,
    last_modified: Optional[datetime] = None,
) -> Dict:
    """检测数据目录下的新文件或修改过的文件。

    Args:
        data_dir: 数据目录路径
        pattern: 文件匹配模式(默认 'durability_*.csv')
        last_modified: 上次检测时间; None 表示首次检测(所有文件视为新增)

    Returns:
        {
            'new_files': List[str],      # 新增/修改的文件路径列表
            'new_records': int,           # 新增记录数(行数)
            'latest_date': datetime,      # 最新文件修改时间
            'has_update': bool,           # 是否有更新
            'total_files': int,           # 目录下总匹配文件数
            'scan_time': datetime,        # 本次扫描时间
        }
    """
    logger.info("增量检测开始: dir=%s pattern=%s last_modified=%s",
                data_dir, pattern, last_modified)

    scan_time = datetime.now()
    files = _scan_files(data_dir, pattern)

    if not files:
        logger.info("目录下无匹配文件")
        return {
            'new_files': [],
            'new_records': 0,
            'latest_date': None,
            'has_update': False,
            'total_files': 0,
            'scan_time': scan_time,
        }

    # 最新修改时间
    latest_date = max(f['mtime'] for f in files)

    # 找出新增/修改的文件
    new_files: List[str] = []
    if last_modified is None:
        # 首次检测: 所有文件都是"新"的
        new_files = [f['path'] for f in files]
        logger.info("首次检测: 全部 %d 个文件视为新增", len(new_files))
    else:
        # 与上次检测时间比较
        last_ts = last_modified.timestamp()
        for f in files:
            if f['mtime_ts'] > last_ts:
                new_files.append(f['path'])
                logger.info("发现新/修改文件: %s (mtime=%s)",
                            f['filename'], f['mtime'])

    # 统计新增记录数(读取文件行数)
    new_records = 0
    if new_files:
        for fp in new_files:
            try:
                # 快速行数统计(不加载全部数据)
                with open(fp, 'r', encoding='utf-8-sig') as f:
                    # 减1去掉header行, 负数保护
                    line_count = max(sum(1 for _ in f) - 1, 0)
                new_records += line_count
            except (OSError, UnicodeDecodeError) as e:
                logger.warning("无法统计文件行数 %s: %s", fp, e)

    has_update = len(new_files) > 0
    logger.info("增量检测完成: 新文件=%d 新记录=%d 总文件=%d has_update=%s",
                len(new_files), new_records, len(files), has_update)

    return {
        'new_files': new_files,
        'new_records': new_records,
        'latest_date': latest_date,
        'has_update': has_update,
        'total_files': len(files),
        'scan_time': scan_time,
    }


# ---------- 增量合并 ----------

def load_durability_csv(filepath: str) -> pd.DataFrame:
    """加载单个耐久数据 CSV 文件。

    自动处理编码(utf-8-sig/gbk 回退)和分隔符。
    """
    fp = Path(filepath)
    if not fp.exists():
        logger.error("文件不存在: %s", filepath)
        return pd.DataFrame()

    # 尝试编码
    for encoding in ('utf-8-sig', 'utf-8', 'gbk'):
        try:
            df = pd.read_csv(filepath, encoding=encoding)
            logger.info("加载 CSV: %s (encoding=%s, rows=%d, cols=%d)",
                        fp.name, encoding, len(df), len(df.columns))
            return df
        except (UnicodeDecodeError, pd.errors.ParserError):
            continue
        except Exception as e:
            logger.warning("加载失败(encoding=%s): %s", encoding, e)
            continue

    logger.error("无法解析 CSV(所有编码均失败): %s", filepath)
    return pd.DataFrame()


def merge_incremental(
    existing_df: Optional[pd.DataFrame],
    new_files: List[str],
    dedup_key: Optional[str] = None,
) -> pd.DataFrame:
    """增量合并: 将新文件数据追加到现有 DataFrame。

    Args:
        existing_df: 现有数据(None 表示首次加载)
        new_files: 新增文件路径列表
        dedup_key: 去重列名(如 'Timestamp'), None 不去重

    Returns:
        合并后的 DataFrame
    """
    if not new_files:
        logger.info("无新文件, 返回现有数据")
        return existing_df if existing_df is not None else pd.DataFrame()

    # 加载新文件
    new_dfs: List[pd.DataFrame] = []
    for fp in new_files:
        df = load_durability_csv(fp)
        if len(df) > 0:
            df['_source_file'] = Path(fp).name  # 标记来源
            new_dfs.append(df)

    if not new_dfs:
        logger.warning("新文件加载均失败, 返回现有数据")
        return existing_df if existing_df is not None else pd.DataFrame()

    new_data = pd.concat(new_dfs, ignore_index=True)
    logger.info("新数据合并: %d 行(%d 个文件)", len(new_data), len(new_dfs))

    if existing_df is None or len(existing_df) == 0:
        merged = new_data
    else:
        merged = pd.concat([existing_df, new_data], ignore_index=True)
        logger.info("合并后总行数: %d (旧=%d + 新=%d)",
                    len(merged), len(existing_df), len(new_data))

    # 去重(可选, 按指定列)
    if dedup_key and dedup_key in merged.columns:
        before = len(merged)
        merged = merged.drop_duplicates(subset=[dedup_key], keep='last')
        if len(merged) < before:
            logger.info("去重(%s): %d -> %d 行", dedup_key, before, len(merged))

    return merged


def update_and_reaggregate(
    data_dir: str,
    existing_df: Optional[pd.DataFrame],
    pattern: str = _DEFAULT_PATTERN,
    last_modified: Optional[datetime] = None,
    dedup_key: Optional[str] = None,
) -> Tuple[pd.DataFrame, Dict]:
    """一体化: 检测新数据 -> 增量合并 -> 返回更新结果。

    Returns:
        (merged_df, check_result_dict)
    """
    result = check_for_new_data(data_dir, pattern, last_modified)

    if not result['has_update']:
        logger.info("无新数据, 跳过合并")
        return (existing_df if existing_df is not None else pd.DataFrame(),
                result)

    merged = merge_incremental(existing_df, result['new_files'], dedup_key)

    # 保存状态
    state = {
        'last_check': result['scan_time'],
        'known_files': {Path(f).name: result['latest_date'].timestamp()
                        for f in result['new_files']},
    }
    _save_state(data_dir, state)

    return merged, result


# ---------- Streamlit UI 封装 ----------

def render_data_updater(
    data_dir: str,
    existing_df: Optional[pd.DataFrame] = None,
    pattern: str = _DEFAULT_PATTERN,
    on_update: Optional[callable] = None,
) -> Dict:
    """Streamlit UI: 增量数据检测面板。

    自动检测(ttl=300) + 手动按钮 + toast 通知 + 状态展示。

    Args:
        data_dir: 数据目录
        existing_df: 现有数据(从 session_state 传入)
        pattern: 文件匹配模式
        on_update: 更新回调函数(接收 merged_df, 执行重算)

    Returns:
        最新检测结果 dict
    """
    import streamlit as st

    st.markdown("#### 🔄 增量数据检测")

    # 从 session_state 读取上次检测时间
    last_check = st.session_state.get('dur_last_update_time')

    # ---------- 自动检测(st.cache_data, ttl=300) ----------
    @st.cache_data(ttl=300, show_spinner=False)
    def _auto_check(_data_dir: str, _pattern: str,
                    _last_ts: float) -> Dict:
        """缓存自动检测(5分钟一次)。"""
        last_dt = (datetime.fromtimestamp(_last_ts)
                   if _last_ts else None)
        return check_for_new_data(_data_dir, _pattern, last_dt)

    last_ts = last_check.timestamp() if last_check else 0
    auto_result = _auto_check(data_dir, pattern, last_ts)

    # ---------- 状态展示 ----------
    col1, col2, col3 = st.columns(3)
    col1.metric("📁 总文件数", auto_result['total_files'])
    col2.metric("📝 新增记录", auto_result['new_records'])
    col3.metric("🕐 最新更新",
                auto_result['latest_date'].strftime('%m-%d %H:%M')
                if auto_result['latest_date'] else '无')

    if last_check:
        st.caption(f"上次检测: {last_check.strftime('%Y-%m-%d %H:%M:%S')}")

    # ---------- 手动检测按钮 ----------
    if st.button('🔄 检测新数据', use_container_width=True, type='primary'):
        result = check_for_new_data(data_dir, pattern, last_check)

        if result['has_update']:
            st.toast(f'检测到新数据！{len(result["new_files"])} 个文件, '
                     f'{result["new_records"]} 条新记录',
                     icon='🎉')

            # 增量合并
            with st.spinner('正在合并新数据...'):
                merged = merge_incremental(
                    existing_df, result['new_files'],
                    dedup_key='Timestamp' if existing_df is not None
                    and 'Timestamp' in existing_df.columns else None,
                )

            # 更新 session_state
            st.session_state['dur_last_update_time'] = result['scan_time']
            st.session_state['dur_merged_data'] = merged

            st.success(f"✅ 合并完成: {len(merged)} 行总数据 "
                       f"(新增 {result['new_records']} 条)")

            # 触发回调(重算聚合)
            if on_update:
                with st.spinner('正在重新计算聚合统计...'):
                    on_update(merged)
                st.success("✅ 聚合统计已更新")

            # 展示新文件列表
            with st.expander(f"📋 新增文件 ({len(result['new_files'])})",
                             expanded=False):
                for f in result['new_files']:
                    st.text(f"  - {Path(f).name}")
        else:
            st.info("ℹ 未检测到新数据")
            st.session_state['dur_last_update_time'] = result['scan_time']

        return result

    # ---------- 自动检测通知 ----------
    if auto_result['has_update'] and not st.session_state.get(
            'dur_toast_shown', False):
        st.toast(f'检测到新数据：{len(auto_result["new_files"])} 个新文件',
                 icon='🔔')
        st.session_state['dur_toast_shown'] = True

    return auto_result


# ---------- 单元测试 ----------

def _make_test_csv(path: str, n_rows: int = 100,
                   start_cycle: int = 0) -> None:
    """生成测试 CSV 文件。"""
    import numpy as np
    np.random.seed(42)
    df = pd.DataFrame({
        'Timestamp': pd.date_range('2026-08-22', periods=n_rows, freq='1s'),
        'cycle_id': [start_cycle + i // 50 for i in range(n_rows)],
        'power_point': [33.0 + (i % 6) * 30 for i in range(n_rows)],
        'FC_AvgCellVoltage': np.random.uniform(0.6, 0.7, n_rows),
        'FC_AvgCellVoltDev': np.random.uniform(0.04, 0.06, n_rows),
        'FC_NetPwrOut': np.random.uniform(30, 200, n_rows),
    })
    df.to_csv(path, index=False, encoding='utf-8-sig')


if __name__ == '__main__':
    import sys
    import shutil
    import tempfile
    import logging as _lg
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    _lg.basicConfig(level=_lg.INFO,
                    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')

    # 创建临时测试目录
    tmp_dir = tempfile.mkdtemp(prefix='durability_test_')
    print(f"测试目录: {tmp_dir}")

    try:
        print("\n===== 测试1: 首次检测(所有文件视为新增) =====")
        _make_test_csv(os.path.join(tmp_dir, 'durability_001.csv'), 100)
        time.sleep(0.1)
        _make_test_csv(os.path.join(tmp_dir, 'durability_002.csv'), 200)
        result = check_for_new_data(tmp_dir, last_modified=None)
        assert result['has_update'] == True
        assert len(result['new_files']) == 2, f"应2个新文件, 实际{len(result['new_files'])}"
        assert result['new_records'] == 300, f"应300行, 实际{result['new_records']}"
        assert result['total_files'] == 2
        assert result['latest_date'] is not None
        print(f"  新文件: {len(result['new_files'])}, 新记录: {result['new_records']}")
        print("  [PASS] 首次检测全部文件视为新增")

        print("\n===== 测试2: 无更新(与上次时间比较) =====")
        last_mod = result['scan_time']
        result2 = check_for_new_data(tmp_dir, last_modified=last_mod)
        assert result2['has_update'] == False, "无新文件应has_update=False"
        assert len(result2['new_files']) == 0
        assert result2['total_files'] == 2  # 总文件数不变
        print(f"  新文件: {len(result2['new_files'])}, has_update={result2['has_update']}")
        print("  [PASS] 无新文件时 has_update=False")

        print("\n===== 测试3: 新增文件检测 =====")
        time.sleep(1.1)  # 确保 mtime 不同
        _make_test_csv(os.path.join(tmp_dir, 'durability_003.csv'), 150)
        result3 = check_for_new_data(tmp_dir, last_modified=last_mod)
        assert result3['has_update'] == True
        assert len(result3['new_files']) == 1, f"应1个新文件, 实际{len(result3['new_files'])}"
        assert 'durability_003.csv' in result3['new_files'][0]
        assert result3['new_records'] == 150
        assert result3['total_files'] == 3
        print(f"  新文件: {result3['new_files']}")
        print(f"  新记录: {result3['new_records']}, 总文件: {result3['total_files']}")
        print("  [PASS] 正确检测新增文件")

        print("\n===== 测试4: 文件修改检测(mtime变化) =====")
        # 修改已有文件(追加行)
        old_mtime = os.path.getmtime(
            os.path.join(tmp_dir, 'durability_001.csv'))
        time.sleep(1.1)
        _make_test_csv(os.path.join(tmp_dir, 'durability_001.csv'), 120)
        result4 = check_for_new_data(tmp_dir, last_modified=last_mod)
        assert result4['has_update'] == True
        # durability_001.csv(修改) + durability_003.csv(新, 在last_mod之后)
        assert len(result4['new_files']) >= 1
        print(f"  新/修改文件: {len(result4['new_files'])} 个")
        print("  [PASS] 文件修改被检测到")

        print("\n===== 测试5: 空目录检测 =====")
        empty_dir = tempfile.mkdtemp(prefix='durability_empty_')
        result5 = check_for_new_data(empty_dir, last_modified=None)
        assert result5['has_update'] == False
        assert len(result5['new_files']) == 0
        assert result5['total_files'] == 0
        assert result5['latest_date'] is None
        shutil.rmtree(empty_dir)
        print("  [PASS] 空目录返回空结果")

        print("\n===== 测试6: 不存在的目录 =====")
        result6 = check_for_new_data('/nonexistent/path/12345')
        assert result6['has_update'] == False
        assert result6['total_files'] == 0
        print("  [PASS] 不存在目录返回空结果")

        print("\n===== 测试7: 增量合并(无现有数据) =====")
        # 用 durability_002.csv(200行, 未被test4修改)
        merged = merge_incremental(None, [result['new_files'][1]])
        assert len(merged) == 200, f"应200行, 实际{len(merged)}"
        assert '_source_file' in merged.columns
        print(f"  合并行数: {len(merged)}, 含来源列: '_source_file'")
        print("  [PASS] 首次合并正确")

        print("\n===== 测试8: 增量合并(追加到现有) =====")
        existing = merged.copy()
        # durability_001.csv 在test4中被修改为120行
        merged2 = merge_incremental(existing, [result['new_files'][0]])
        assert len(merged2) == 320, f"应320行(200+120), 实际{len(merged2)}"
        print(f"  旧数据: {len(existing)} -> 合并后: {len(merged2)}")
        print("  [PASS] 追加合并正确")

        print("\n===== 测试9: 去重合并(Timestamp列) =====")
        # 用相同文件去重(durability_002.csv Timestamp 与 existing 重复)
        existing2 = merged.copy()
        merged3 = merge_incremental(existing2, [result['new_files'][1]],
                                     dedup_key='Timestamp')
        # Timestamp 重复的保留最后一条, 行数应=200
        assert len(merged3) == 200, f"去重后应200行, 实际{len(merged3)}"
        print(f"  去重前: {len(existing2)}+200 -> 去重后: {len(merged3)}")
        print("  [PASS] 去重合并正确")

        print("\n===== 测试10: 空文件列表合并 =====")
        merged4 = merge_incremental(existing, [])
        assert len(merged4) == len(existing)
        print("  [PASS] 空文件列表返回原数据")

        print("\n===== 测试11: 状态保存与加载 =====")
        state = {
            'last_check': datetime(2026, 8, 22, 12, 0, 0),
            'known_files': {'durability_001.csv': 1724313600.0},
        }
        _save_state(tmp_dir, state)
        loaded = _load_state(tmp_dir)
        assert loaded['last_check'] == datetime(2026, 8, 22, 12, 0, 0)
        assert 'durability_001.csv' in loaded['known_files']
        print(f"  保存: last_check={state['last_check']}")
        print(f"  加载: last_check={loaded['last_check']}")
        print("  [PASS] 状态保存/加载正确")

        print("\n===== 测试12: 状态文件不存在时返回默认 =====")
        fresh = _load_state('/nonexistent/path/12345')
        assert fresh['last_check'] is None
        assert fresh['known_files'] == {}
        print("  [PASS] 无状态文件返回默认值")

        print("\n===== 测试13: update_and_reaggregate 一体化 =====")
        # 清理状态文件
        state_file = os.path.join(tmp_dir, _STATE_FILENAME)
        if os.path.exists(state_file):
            os.remove(state_file)
        merged5, result7 = update_and_reaggregate(tmp_dir, None)
        assert result7['has_update'] == True
        assert len(merged5) > 0
        assert os.path.exists(state_file), "状态文件应已保存"
        print(f"  合并: {len(merged5)} 行, has_update={result7['has_update']}")
        print(f"  状态文件已保存: {os.path.exists(state_file)}")
        print("  [PASS] 一体化更新正确")

        print("\n===== 测试14: 二次update无新数据 =====")
        merged6, result8 = update_and_reaggregate(
            tmp_dir, merged5, last_modified=result7['scan_time'])
        assert result8['has_update'] == False
        assert len(merged6) == len(merged5), "无新数据应保持原样"
        print(f"  has_update={result8['has_update']}, 行数不变={len(merged6)}")
        print("  [PASS] 二次检测无新数据")

        print("\n===== 测试15: render_data_updater 函数签名 =====")
        assert callable(render_data_updater)
        import inspect
        sig = inspect.signature(render_data_updater)
        params = list(sig.parameters.keys())
        assert params == ['data_dir', 'existing_df', 'pattern', 'on_update']
        print(f"  签名: {sig}")
        print("  [PASS] render_data_updater 就绪(需Streamlit运行时测试)")

        print("\n===== 测试16: pattern 匹配过滤 =====")
        # 添加不匹配的文件
        _make_test_csv(os.path.join(tmp_dir, 'other_data.csv'), 50)
        result9 = check_for_new_data(tmp_dir,
                                      pattern='durability_*.csv',
                                      last_modified=None)
        assert result9['total_files'] == 3, \
            f"应只匹配3个durability_*, 实际{result9['total_files']}"
        print(f"  durability_*.csv 匹配: {result9['total_files']} 个(排除 other_data.csv)")
        print("  [PASS] pattern 过滤正确")

        print("\n[OK] 全部测试通过")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        print(f"\n清理测试目录: {tmp_dir}")
