"""飞书预警通知模块。

实现条件检测和飞书 Webhook 推送, 支持:
1. 遍历聚合数据, 检测预警条件(离均差>50mV / 平均单体电压<600mV)
2. 自动单位检测(mV/V), 避免单位混淆
3. 同一循环+功率点去重, 避免重复告警
4. 飞书 Webhook 富文本消息推送(含加粗/链接/@提及)
5. 测试模式(检测但不发送)
6. 重试机制(网络异常时自动重试)

核心函数: check_and_alert
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import List, Dict, Optional, Tuple, Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ---------- 常量 ----------
# 预警条件映射表(condition_str -> spec)
_CONDITION_SPECS: Dict[str, Dict[str, Any]] = {
    '离均差>50mV': {
        'signal': 'FC_AvgCellVoltDev',
        'operator': '>',
        'threshold_mV': 50.0,
        'label': '离均差',
        'emoji': '🔴',
    },
    '平均单体电压<600mV': {
        'signal': 'FC_AvgCellVoltage',
        'operator': '<',
        'threshold_mV': 600.0,
        'label': '平均单体电压',
        'emoji': '🔴',
    },
}

# 飞书 Webhook 配置
_DASHBOARD_URL = 'http://localhost:8501/?tab=durability'
_MAX_RETRIES = 3
_RETRY_DELAY = 1.0          # 秒(递增重试)
_REQUEST_TIMEOUT = 10       # 秒
_RATE_LIMIT_INTERVAL = 0.5  # 飞书限速: 5条/10s, 间隔 0.5s 安全

# 无效条件值(None / 'None' / '无' / 空字符串)
_INVALID_CONDITIONS = {None, 'None', '无', '', 'none', 'NULL'}


# ---------- 信号列解析 ----------

def _resolve_signal_column(df: pd.DataFrame, signal: str,
                           agg_method: str = 'mean') -> Optional[str]:
    """从聚合 DataFrame 解析信号对应列名。

    匹配优先级: {signal}_{agg_method} > {signal}_* (排除 _std) > signal 本身。
    """
    # 1. 精确匹配 {signal}_{agg_method}
    col = f'{signal}_{agg_method}'
    if col in df.columns:
        return col
    # 2. 匹配 {signal}_ 前缀(排除 _std 稳定性列)
    candidates = [c for c in df.columns
                  if c.startswith(f'{signal}_') and not c.endswith('_std')]
    if candidates:
        return candidates[0]
    # 3. 直接匹配
    if signal in df.columns:
        return signal
    return None


# ---------- 单位检测 ----------

def _detect_voltage_unit(values: np.ndarray) -> str:
    """自动检测电压数据单位: 'mV' or 'V'。

    启发式: 中位数 > 100 -> mV, 否则 -> V。
    (燃料电池单体电压: 正常 0.6-0.8V = 600-800mV)
    """
    clean = values[~np.isnan(values)]
    if len(clean) == 0:
        return 'mV'  # 默认
    median = float(np.nanmedian(clean))
    if median > 100:
        return 'mV'
    return 'V'


def _to_mV(value: float, unit: str) -> float:
    """将电压值统一转换为 mV。"""
    if pd.isna(value):
        return float('nan')
    if unit == 'V':
        return float(value) * 1000.0
    return float(value)


# ---------- 条件检测 ----------

def _is_valid_condition(cond: Optional[str]) -> bool:
    """判断条件是否有效(非 None/空/'无')。"""
    return cond is not None and str(cond).strip() not in _INVALID_CONDITIONS


def _detect_events(
    df: pd.DataFrame,
    condition: str,
    agg_method: str = 'mean',
) -> List[Dict]:
    """检测单个条件触发的所有事件。

    Returns:
        事件列表, 每个事件含:
        {timestamp, cycle_id, power_point, condition, signal,
         value(mV), value_original, unit, threshold(mV),
         operator, label, data_count, quality, message}
    """
    spec = _CONDITION_SPECS.get(condition)
    if spec is None:
        logger.warning("未知预警条件: %s", condition)
        return []

    signal = spec['signal']
    col_name = _resolve_signal_column(df, signal, agg_method)
    if col_name is None or col_name not in df.columns:
        logger.warning("条件 '%s' 的信号 %s 无对应列, 跳过", condition, signal)
        return []

    values = pd.to_numeric(df[col_name], errors='coerce')
    unit = _detect_voltage_unit(values.to_numpy())
    values_mV = values.apply(lambda v: _to_mV(v, unit))

    threshold = spec['threshold_mV']
    op = spec['operator']
    if op == '>':
        mask = values_mV > threshold
    elif op == '<':
        mask = values_mV < threshold
    else:
        logger.error("不支持的操作符: %s", op)
        return []

    events: List[Dict] = []
    for idx in mask[mask].index:
        row = df.loc[idx]
        v_mV = float(values_mV.loc[idx])
        v_orig = float(values.loc[idx])
        cycle = row.get('cycle_id', -1)
        power = row.get('power_point', 0.0)
        data_count = row.get('数据量', 0)
        quality = row.get('质量标记', '未知')

        message = (f"{spec['label']} {v_mV:.1f}mV {op} {threshold:.0f}mV"
                   f" (循环{cycle}, 功率{power:.1f}kW, 数据量{data_count}, {quality})")

        events.append({
            'timestamp': datetime.now(),
            'cycle_id': int(cycle) if not pd.isna(cycle) else -1,
            'power_point': float(power) if not pd.isna(power) else 0.0,
            'condition': condition,
            'signal': signal,
            'value': v_mV,
            'value_original': v_orig,
            'unit': unit,
            'threshold': threshold,
            'operator': op,
            'label': spec['label'],
            'data_count': int(data_count) if not pd.isna(data_count) else 0,
            'quality': str(quality),
            'message': message,
        })

    logger.info("条件 '%s' 检测完成: 触发 %d 个事件(单位=%s)",
                condition, len(events), unit)
    return events


def _deduplicate_events(events: List[Dict]) -> List[Dict]:
    """去重: 同一 (cycle_id, power_point, condition) 只保留第一个事件。"""
    seen = set()
    deduped: List[Dict] = []
    for e in events:
        key = (e['cycle_id'], e['power_point'], e['condition'])
        if key in seen:
            logger.info("去重: 跳过重复事件 (cycle=%d, pp=%.1f, cond=%s)",
                        e['cycle_id'], e['power_point'], e['condition'])
            continue
        seen.add(key)
        deduped.append(e)
    if len(deduped) < len(events):
        logger.info("去重: %d -> %d 个事件", len(events), len(deduped))
    return deduped


# ---------- 飞书消息格式化 ----------

def _build_feishu_payload(
    event: Dict,
    rig_id: str = '台架A',
    recipients: Optional[List[str]] = None,
) -> Dict:
    """构建飞书 Webhook 富文本消息 payload。

    格式遵循飞书自定义机器人 post 消息规范:
    msg_type=post, content.post.zh_cn.{title, content}
    content 为二维数组, 每个子数组代表一行。
    """
    ts_str = event['timestamp'].strftime('%Y-%m-%d %H:%M:%S')
    val_str = f"{event['value']:.1f}mV"
    thresh_str = f"{event['threshold']:.0f}mV"
    op = event['operator']
    label = event['label']

    # 富文本内容(二维数组, 每个子数组一行)
    content_lines: List[List[Dict]] = [
        # 第1行: 台架A - 循环 X 功率 YkW 触发预警:
        [
            {"tag": "text", "text": f"{rig_id} - 循环"},
            {"tag": "text", "text": str(event['cycle_id']),
             "style": ["bold"]},
            {"tag": "text", "text": " 功率 "},
            {"tag": "text", "text": f"{event['power_point']:.1f}kW",
             "style": ["bold"]},
            {"tag": "text", "text": " 触发预警:"},
        ],
        # 第2行: 条件详情
        [
            {"tag": "text", "text": f"🔴 {label}"},
            {"tag": "text", "text": val_str,
             "style": ["bold"]},
            {"tag": "text", "text": f" ({op} 阈值{thresh_str})"},
        ],
        # 第3行: 数据量与质量
        [
            {"tag": "text",
             "text": f"📊 数据量: {event['data_count']}, 质量: {event['quality']}"},
        ],
        # 第4行: 时间
        [
            {"tag": "text", "text": "⏰ "},
            {"tag": "text", "text": ts_str},
        ],
        # 第5行: 查看详情链接
        [
            {"tag": "a", "href": _DASHBOARD_URL, "text": "查看详情"},
        ],
    ]

    # 添加 @ 提及(飞书用户ID)
    if recipients:
        mention_line: List[Dict] = [
            {"tag": "text", "text": "👤 相关人员: "}
        ]
        for r in recipients:
            mention_line.append({
                "tag": "at",
                "user_id": r,
            })
            mention_line.append({"tag": "text", "text": " "})
        content_lines.append(mention_line)

    payload = {
        "msg_type": "post",
        "content": {
            "post": {
                "zh_cn": {
                    "title": "⚠️ 台架耐久测试异常预警",
                    "content": content_lines,
                }
            }
        }
    }
    return payload


# ---------- Webhook 发送 ----------

def _send_webhook(
    webhook_url: str,
    payload: Dict,
    timeout: int = _REQUEST_TIMEOUT,
    max_retries: int = _MAX_RETRIES,
) -> Tuple[bool, str]:
    """发送飞书 Webhook 消息(含重试机制)。

    优先使用 requests 库, 回退到 urllib(stdlib)。

    Returns:
        (success, message)
    """
    if not webhook_url or not webhook_url.startswith('http'):
        return False, f"无效的 webhook_url: {webhook_url}"

    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    headers = {'Content-Type': 'application/json; charset=utf-8'}

    for attempt in range(1, max_retries + 1):
        try:
            # 优先 requests
            try:
                import requests
                resp = requests.post(webhook_url, data=body,
                                     headers=headers, timeout=timeout)
                status = resp.status_code
                resp_text = resp.text
            except ImportError:
                # 回退到 urllib
                import urllib.request
                import urllib.error
                req = urllib.request.Request(
                    webhook_url, data=body, headers=headers, method='POST'
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    status = resp.status
                    resp_text = resp.read().decode('utf-8')

            # 飞书成功: HTTP 200 + {"code":0,"msg":"success"}
            if status == 200:
                try:
                    result = json.loads(resp_text)
                    feishu_code = result.get('code',
                                             result.get('StatusCode', -1))
                    if feishu_code == 0:
                        logger.info("飞书推送成功(attempt %d)", attempt)
                        return True, "发送成功"
                    msg = result.get('msg',
                                     result.get('ErrorMessage', resp_text))
                    logger.warning("飞书返回错误: %s", msg)
                    return False, f"飞书返回错误: {msg}"
                except json.JSONDecodeError:
                    logger.info("飞书推送成功(HTTP %d)", status)
                    return True, f"HTTP {status}"
            else:
                logger.warning("HTTP %d (attempt %d/%d): %s",
                               status, attempt, max_retries,
                               resp_text[:200])
                if attempt < max_retries:
                    time.sleep(_RETRY_DELAY * attempt)

        except Exception as e:
            logger.warning("发送异常(attempt %d/%d): %s",
                           attempt, max_retries, e)
            if attempt < max_retries:
                time.sleep(_RETRY_DELAY * attempt)

    return False, f"发送失败(重试{max_retries}次后仍失败)"


# ---------- 主函数 ----------

def check_and_alert(
    df: pd.DataFrame,
    condition_1: Optional[str],
    condition_2: Optional[str],
    webhook_url: str,
    recipients: List[str],
    rig_id: str = '台架A',
    agg_method: str = 'mean',
    test_mode: bool = False,
) -> List[Dict]:
    """检测预警条件并发送飞书通知。

    Args:
        df: 聚合后的 DataFrame(aggregate_durability_stats 输出), 需含
            cycle_id, power_point, {signal}_{agg_method}, 数据量, 质量标记
        condition_1: 预警条件1('离均差>50mV' 或 None)
        condition_2: 预警条件2('平均单体电压<600mV' 或 None/'无')
        webhook_url: 飞书 Webhook URL
        recipients: 飞书用户ID列表(用于 @ 提及)
        rig_id: 台架编号(用于消息标题)
        agg_method: 聚合方法列后缀(mean/median/min/max)
        test_mode: True=仅检测不发送飞书

    Returns:
        触发的事件列表:
        [{
            'timestamp': datetime,
            'cycle_id': int,
            'power_point': float,
            'condition': str,
            'value': float,        # mV 单位
            'threshold': float,    # mV 单位
            'message': str,
            'sent': bool,          # 是否成功发送飞书
            'send_error': str,     # 发送错误信息(成功时为空)
        }]
    """
    logger.info("预警检测开始: cond1=%s cond2=%s test=%s rows=%d",
                condition_1, condition_2, test_mode,
                len(df) if df is not None else 0)

    # ---------- 输入校验 ----------
    if df is None or len(df) == 0:
        logger.warning("聚合数据为空, 跳过检测")
        return []

    # ---------- 检测所有条件 ----------
    conditions: List[str] = []
    if _is_valid_condition(condition_1):
        conditions.append(condition_1)
    if _is_valid_condition(condition_2):
        conditions.append(condition_2)

    if not conditions:
        logger.info("无有效预警条件, 跳过检测")
        return []

    all_events: List[Dict] = []
    for cond in conditions:
        events = _detect_events(df, cond, agg_method)
        all_events.extend(events)

    # 去重: 同一 (cycle_id, power_point, condition) 只告警一次
    all_events = _deduplicate_events(all_events)

    if not all_events:
        logger.info("未触发任何预警条件")
        return []

    logger.info("共触发 %d 个预警事件(去重后)", len(all_events))

    # ---------- 发送飞书通知 ----------
    if test_mode:
        logger.info("测试模式: 不实际发送飞书通知")
        for e in all_events:
            e['sent'] = False
            e['send_error'] = '测试模式(未发送)'
    elif not webhook_url:
        logger.error("webhook_url 为空, 无法发送飞书通知")
        for e in all_events:
            e['sent'] = False
            e['send_error'] = 'webhook_url 未配置'
    else:
        sent_count = 0
        for e in all_events:
            payload = _build_feishu_payload(e, rig_id, recipients)
            success, msg = _send_webhook(webhook_url, payload)
            e['sent'] = success
            e['send_error'] = '' if success else msg
            if success:
                sent_count += 1
            # 飞书限速: 间隔发送
            time.sleep(_RATE_LIMIT_INTERVAL)
        logger.info("飞书推送完成: 成功 %d/%d", sent_count, len(all_events))

    return all_events


# ---------- Streamlit UI 封装 ----------

def render_feishu_alerter(
    df_agg: pd.DataFrame,
    filter_cfg: Optional[Dict] = None,
) -> None:
    """Streamlit UI: 飞书预警通知面板。

    从 st.secrets 读取 webhook_url, 提供测试模式开关,
    展示触发的事件列表。

    Args:
        df_agg: 聚合后的 DataFrame
        filter_cfg: durability_filter 返回的配置 dict
    """
    import streamlit as st

    st.markdown("### 🚨 飞书预警通知")

    # ---------- 读取 webhook_url(st.secrets 优先) ----------
    webhook_url = ''
    try:
        webhook_url = st.secrets.get('feishu', {}).get('webhook_url', '')
    except Exception:
        pass

    if not webhook_url:
        st.warning(
            "⚠ 未配置飞书 Webhook URL, 请在 .streamlit/secrets.toml 中设置:\n"
            "```\n[feishu]\nwebhook_url = 'https://open.feishu.cn/open-apis/bot/v2/hook/...'\n```"
        )
        webhook_url = st.text_input(
            '手动输入 Webhook URL', type='password',
            help='飞书自定义机器人 Webhook 地址',
        )

    # ---------- 测试模式 ----------
    test_mode = st.checkbox(
        '测试模式（不实际发送）', value=True,
        help='勾选后仅检测预警条件, 不实际发送飞书消息',
    )

    # ---------- 从 filter_cfg 获取条件 ----------
    if filter_cfg is None:
        filter_cfg = {}
    condition_1 = filter_cfg.get('alert_condition_1', '离均差>50mV')
    condition_2 = filter_cfg.get('alert_condition_2', '无')
    rig_id = filter_cfg.get('rig_id', '台架A')

    st.caption(f"当前预警条件: 条件1=[{condition_1}] 条件2=[{condition_2}]")

    # ---------- 飞书用户ID ----------
    recipients_str = st.text_input(
        '飞书用户ID(逗号分隔, 用于@提及)', '',
        help='输入飞书用户ID, 多个用逗号分隔。留空则不@提及。',
    )
    recipients = [r.strip() for r in recipients_str.split(',') if r.strip()]

    # ---------- 检测按钮 ----------
    if st.button('🔍 检测预警', use_container_width=True, type='primary'):
        with st.spinner('正在检测预警条件...'):
            events = check_and_alert(
                df_agg, condition_1, condition_2,
                webhook_url, recipients, rig_id,
                test_mode=test_mode,
            )

        if not events:
            st.success("✅ 未触发任何预警条件, 数据正常")
        else:
            st.warning(f"⚠ 触发 {len(events)} 个预警事件")
            for i, e in enumerate(events, 1):
                sent_icon = '✅' if e.get('sent') else '❌'
                with st.expander(
                    f"{sent_icon} 事件{i}: {e['message']}", expanded=True
                ):
                    c1, c2, c3 = st.columns(3)
                    c1.metric('循环', e['cycle_id'])
                    c2.metric('功率点', f"{e['power_point']:.1f}kW")
                    c3.metric('数值', f"{e['value']:.1f}mV")

                    c4, c5, c6 = st.columns(3)
                    c4.metric('阈值', f"{e['threshold']:.0f}mV")
                    c5.metric('条件', e['condition'])
                    c6.metric('数据量', e.get('data_count', 0))

                    st.caption(
                        f"⏰ {e['timestamp'].strftime('%Y-%m-%d %H:%M:%S')}"
                    )
                    if e.get('send_error'):
                        st.caption(f"发送状态: {e['send_error']}")


# ---------- 单元测试 ----------

def _make_test_agg_df(
    n_cycles: int = 3,
    powers: Optional[List[float]] = None,
    voltage_unit: str = 'mV',
) -> pd.DataFrame:
    """构造模拟聚合数据。

    Args:
        voltage_unit: 'mV' (FC_AvgCellVoltage~650) 或 'V' (FC_AvgCellVoltage~0.65)
    """
    if powers is None:
        powers = [33.0, 58.5, 117.0]
    np.random.seed(42)
    rows = []
    for c in range(n_cycles):
        for p in powers:
            if voltage_unit == 'mV':
                # 600mV 附近(部分<600触发预警)
                v_avg = 610 - c * 20 + np.random.randn() * 5
                # 50mV 附近(部分>50触发预警)
                v_dev = 45 + c * 5 + np.random.randn() * 3
            else:
                # V 单位
                v_avg = 0.61 - c * 0.02 + np.random.randn() * 0.005
                v_dev = 0.045 + c * 0.005 + np.random.randn() * 0.003
            rows.append({
                'cycle_id': c,
                'power_point': float(p),
                'FC_AvgCellVoltage_mean': round(float(v_avg), 4),
                'FC_AvgCellVoltage_std': round(float(np.random.rand() * 0.002), 4),
                'FC_AvgCellVoltDev_mean': round(float(v_dev), 4),
                'FC_AvgCellVoltDev_std': round(float(np.random.rand() * 0.003), 4),
                '数据量': 50 + int(np.random.rand() * 20),
                '质量标记': '正常',
            })
    return pd.DataFrame(rows)


if __name__ == '__main__':
    import sys
    import logging as _lg
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    _lg.basicConfig(level=_lg.INFO,
                    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')

    print("===== 测试1: 空数据返回空列表 =====")
    events = check_and_alert(pd.DataFrame(), '离均差>50mV', None,
                             'https://example.com/webhook', [])
    assert events == [], f"空数据应返回空列表, 实际{len(events)}"
    print("  [PASS] 空数据 -> []")

    print("\n===== 测试2: 无效条件(None/无)跳过 =====")
    df = _make_test_agg_df(voltage_unit='mV')
    events = check_and_alert(df, None, '无',
                             'https://example.com/webhook', [])
    assert events == [], f"无效应返回空, 实际{len(events)}"
    events2 = check_and_alert(df, 'None', '',
                              'https://example.com/webhook', [])
    assert events2 == [], "None/空字符串应跳过"
    print("  [PASS] None/无/空 -> []")

    print("\n===== 测试3: 离均差>50mV 检测(mV单位) =====")
    df_mv = _make_test_agg_df(n_cycles=3, voltage_unit='mV')
    # 构造部分 > 50mV 的离均差
    df_mv.loc[df_mv.index[::2], 'FC_AvgCellVoltDev_mean'] = 55.0  # 触发
    df_mv.loc[df_mv.index[1::2], 'FC_AvgCellVoltDev_mean'] = 45.0  # 不触发
    events = check_and_alert(df_mv, '离均差>50mV', None,
                             'https://example.com/webhook', [],
                             test_mode=True)
    assert len(events) > 0, "应有离均差>50mV事件触发"
    for e in events:
        assert e['condition'] == '离均差>50mV'
        assert e['value'] > 50.0, f"值应>50mV, 实际{e['value']}"
        assert e['threshold'] == 50.0
        assert e['operator'] == '>'
        assert not e['sent'], "测试模式应未发送"
        assert '测试模式' in e['send_error']
    print(f"  触发 {len(events)} 个离均差>50mV事件(测试模式)")
    print("  [PASS] 离均差检测+测试模式正确")

    print("\n===== 测试4: 平均单体电压<600mV 检测(mV单位) =====")
    df_mv2 = _make_test_agg_df(n_cycles=3, voltage_unit='mV')
    # 构造部分 < 600mV 的电压
    df_mv2.loc[df_mv2.index[::2], 'FC_AvgCellVoltage_mean'] = 580.0  # 触发
    df_mv2.loc[df_mv2.index[1::2], 'FC_AvgCellVoltage_mean'] = 620.0  # 不触发
    events = check_and_alert(df_mv2, None, '平均单体电压<600mV',
                             'https://example.com/webhook', [],
                             test_mode=True)
    assert len(events) > 0, "应有电压<600mV事件触发"
    for e in events:
        assert e['condition'] == '平均单体电压<600mV'
        assert e['value'] < 600.0, f"值应<600mV, 实际{e['value']}"
        assert e['threshold'] == 600.0
        assert e['operator'] == '<'
    print(f"  触发 {len(events)} 个电压<600mV事件")
    print("  [PASS] 电压检测正确(mV单位)")

    print("\n===== 测试5: V单位自动转换检测 =====")
    df_v = _make_test_agg_df(n_cycles=3, voltage_unit='V')
    # 构造 < 0.6V (= 600mV) 的电压
    df_v.loc[df_v.index[::2], 'FC_AvgCellVoltage_mean'] = 0.58  # = 580mV 触发
    df_v.loc[df_v.index[1::2], 'FC_AvgCellVoltage_mean'] = 0.62  # = 620mV 不触发
    events = check_and_alert(df_v, None, '平均单体电压<600mV',
                             'https://example.com/webhook', [],
                             test_mode=True)
    assert len(events) > 0, "V单位数据应正确转换并触发"
    for e in events:
        assert e['unit'] == 'V', f"应检测为V单位, 实际{e['unit']}"
        # value 字段应为 mV(转换后)
        assert e['value'] < 600.0, f"转换后mV值应<600, 实际{e['value']}"
        assert e['value_original'] < 1.0, "原始V值应<1"
    print(f"  V单位 -> 自动检测 -> 转换mV -> 触发{len(events)}个事件")
    print("  [PASS] V单位自动转换正确")

    print("\n===== 测试6: 去重(同循环+功率点+条件只告一次) =====")
    # 构造同一(cycle, power)有多行触发的数据
    df_dup = pd.DataFrame([
        {'cycle_id': 0, 'power_point': 33.0,
         'FC_AvgCellVoltDev_mean': 55.0, '数据量': 50, '质量标记': '正常'},
        {'cycle_id': 0, 'power_point': 33.0,  # 重复!
         'FC_AvgCellVoltDev_mean': 60.0, '数据量': 50, '质量标记': '正常'},
        {'cycle_id': 0, 'power_point': 58.5,
         'FC_AvgCellVoltDev_mean': 55.0, '数据量': 50, '质量标记': '正常'},
    ])
    events = check_and_alert(df_dup, '离均差>50mV', None,
                             'https://example.com/webhook', [],
                             test_mode=True)
    # 去重后应只剩 2 个(0,33) 和 (0,58.5)
    assert len(events) == 2, f"去重后应2个, 实际{len(events)}"
    keys = [(e['cycle_id'], e['power_point']) for e in events]
    assert (0, 33.0) in keys and (0, 58.5) in keys
    print(f"  3条触发 -> 去重 -> {len(events)}条")
    print("  [PASS] 去重逻辑正确")

    print("\n===== 测试7: 飞书payload格式正确 =====")
    event = {
        'timestamp': datetime(2026, 8, 22, 15, 30, 0),
        'cycle_id': 2,
        'power_point': 117.0,
        'condition': '离均差>50mV',
        'value': 52.0,
        'threshold': 50.0,
        'operator': '>',
        'label': '离均差',
        'data_count': 50,
        'quality': '正常',
    }
    payload = _build_feishu_payload(event, '台架A', ['user123'])
    assert payload['msg_type'] == 'post'
    post = payload['content']['post']['zh_cn']
    assert '台架耐久测试异常预警' in post['title']
    content = post['content']
    assert len(content) >= 5, "至少5行内容"
    # 第1行应含台架/循环/功率
    line1_texts = [t.get('text', '') for t in content[0] if t.get('tag') == 'text']
    assert any('台架A' in t for t in line1_texts)
    assert any('2' in t for t in line1_texts)  # 循环2
    assert any('117' in t for t in line1_texts)  # 功率117
    # 第2行应含离均差/52mV
    line2_texts = [t.get('text', '') for t in content[1] if t.get('tag') == 'text']
    assert any('离均差' in t for t in line2_texts)
    assert any('52' in t for t in line2_texts)
    # 第5行应含链接
    link_line = [t for t in content[4] if t.get('tag') == 'a']
    assert len(link_line) == 1
    assert link_line[0]['href'] == _DASHBOARD_URL
    # 最后一行应含@提及
    at_tags = [t for t in content[-1] if t.get('tag') == 'at']
    assert len(at_tags) == 1
    assert at_tags[0]['user_id'] == 'user123'
    print(f"  msg_type={payload['msg_type']}")
    print(f"  title={post['title']}")
    print(f"  content 行数: {len(content)}")
    print(f"  @提及: {at_tags}")
    print("  [PASS] 飞书payload格式正确")

    print("\n===== 测试8: 无效webhook_url =====")
    ok, msg = _send_webhook('', {'msg_type': 'text'})
    assert not ok, "空URL应失败"
    assert '无效' in msg
    ok2, msg2 = _send_webhook('not-a-url', {'msg_type': 'text'})
    assert not ok2, "非http URL应失败"
    assert '无效' in msg2
    print(f"  空 URL -> {msg}")
    print(f"  非法URL -> {msg2}")
    print("  [PASS] 无效URL被拒绝")

    print("\n===== 测试9: 缺失信号列容错 =====")
    df_missing = df_mv.drop(columns=['FC_AvgCellVoltDev_mean'])
    events = check_and_alert(df_missing, '离均差>50mV', None,
                             'https://example.com/webhook', [],
                             test_mode=True)
    assert events == [], "缺失信号列应返回空"
    print("  [PASS] 缺失信号列优雅跳过")

    print("\n===== 测试10: 双条件同时检测 =====")
    df_both = _make_test_agg_df(n_cycles=3, voltage_unit='mV')
    # 同时触发两个条件
    df_both['FC_AvgCellVoltDev_mean'] = 55.0  # 全部>50mV
    df_both['FC_AvgCellVoltage_mean'] = 580.0  # 全部<600mV
    events = check_and_alert(df_both, '离均差>50mV', '平均单体电压<600mV',
                             'https://example.com/webhook', [],
                             test_mode=True)
    # 应有 9 个cycle*power组合 * 2条件 = 18, 但去重后每个(cycle,power,cond)唯一
    cond1_count = sum(1 for e in events if e['condition'] == '离均差>50mV')
    cond2_count = sum(1 for e in events if e['condition'] == '平均单体电压<600mV')
    assert cond1_count > 0 and cond2_count > 0, "两个条件都应触发"
    assert cond1_count + cond2_count == len(events)
    print(f"  条件1触发: {cond1_count}, 条件2触发: {cond2_count}")
    print("  [PASS] 双条件同时检测正确")

    print("\n===== 测试11: 测试模式不发送 =====")
    events = check_and_alert(df_both, '离均差>50mV', None,
                             'https://example.com/webhook', [],
                             test_mode=True)
    assert all(not e['sent'] for e in events), "测试模式应全部未发送"
    assert all('测试模式' in e['send_error'] for e in events)
    print(f"  {len(events)} 个事件全部 sent=False")
    print("  [PASS] 测试模式不发送")

    print("\n===== 测试12: 事件返回字段完整 =====")
    events = check_and_alert(df_mv, '离均差>50mV', None,
                             'https://example.com/webhook', [],
                             test_mode=True)
    required = {'timestamp', 'cycle_id', 'power_point', 'condition',
                'value', 'threshold', 'message', 'sent', 'send_error'}
    for e in events:
        missing = required - set(e.keys())
        assert not missing, f"缺少字段: {missing}"
    print(f"  必需字段: {sorted(required)}")
    print("  [PASS] 事件返回字段完整")

    print("\n===== 测试13: _detect_voltage_unit 单位检测 =====")
    assert _detect_voltage_unit(np.array([650, 620, 580])) == 'mV'
    assert _detect_voltage_unit(np.array([0.65, 0.62, 0.58])) == 'V'
    assert _detect_voltage_unit(np.array([3600, 3500])) == 'mV'  # mV 大值
    assert _detect_voltage_unit(np.array([])) == 'mV'  # 空默认mV
    print("  [650,620,580] -> mV")
    print("  [0.65,0.62,0.58] -> V")
    print("  [] -> mV(默认)")
    print("  [PASS] 单位自动检测正确")

    print("\n===== 测试14: _to_mV 单位转换 =====")
    assert abs(_to_mV(0.6, 'V') - 600.0) < 0.001
    assert abs(_to_mV(600, 'mV') - 600.0) < 0.001
    assert pd.isna(_to_mV(float('nan'), 'V'))
    print("  0.6V -> 600mV")
    print("  600mV -> 600mV")
    print("  NaN -> NaN")
    print("  [PASS] 单位转换正确")

    print("\n===== 测试15: _is_valid_condition 条件校验 =====")
    assert _is_valid_condition('离均差>50mV') == True
    assert _is_valid_condition('平均单体电压<600mV') == True
    assert _is_valid_condition(None) == False
    assert _is_valid_condition('无') == False
    assert _is_valid_condition('None') == False
    assert _is_valid_condition('') == False
    assert _is_valid_condition('  ') == False
    print("  '离均差>50mV' -> True")
    print("  None/无/None/空 -> False")
    print("  [PASS] 条件有效性校验正确")

    print("\n===== 测试16: render_feishu_alerter 函数存在 =====")
    assert callable(render_feishu_alerter)
    import inspect
    sig = inspect.signature(render_feishu_alerter)
    params = list(sig.parameters.keys())
    assert params == ['df_agg', 'filter_cfg']
    print(f"  签名: {sig}")
    print("  [PASS] render_feishu_alerter 就绪(需Streamlit运行时测试)")

    print("\n===== 测试17: payload 不含 recipients 时无@提及 =====")
    payload = _build_feishu_payload(event, '台架A', None)
    at_tags = [t for line in payload['content']['post']['zh_cn']['content']
               for t in line if t.get('tag') == 'at']
    assert len(at_tags) == 0, "无recipients不应有@提及"
    print("  [PASS] 无recipients -> 无@提及")

    print("\n[OK] 全部测试通过")
