"""飞书固定人员对接管理模块。

纯 App ID + Secret 模式: 无需 Webhook, 通过飞书 Open API 直接给指定人员发消息。

功能:
1. 人员注册: 记录姓名/手机号/open_id/app_id/app_secret
2. 密钥验证: 通过飞书 API 验证 app_id+app_secret 有效性
3. 人员管理: 列表/删除/启用禁用
4. 预警推送: 通过飞书 Open API 向已验证人员发送预警通知(含详情)

存储:
- **统一走 durability.database 层** (MySQL + 自动降级 SQLite), 享受降级保护
- 兼容旧版 JSON 文件: 启动期若发现 data/feishu_contacts.json, 会自动迁移到数据库,
  迁移成功后 JSON 会被重命名为 .migrated 备份文件, 避免重复导入。
"""
from __future__ import annotations

import json
import logging
import threading
import time
import hashlib
from datetime import datetime
from typing import List, Dict, Optional, Tuple
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

# ---------- 常量 ----------
# 兼容旧版 JSON 文件路径 (迁移用)
_OLD_CONTACTS_FILE = Path(__file__).resolve().parent.parent / "data" / "feishu_contacts.json"
_REQUEST_TIMEOUT = 10
_MAX_RETRIES = 2

# 飞书 API 端点
_FEISHU_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
_FEISHU_MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages"

# 密钥自动巡检缓存: (cache_key) -> (result_dict, timestamp_ms)
# cache_key = 所有 (app_id, app_secret_md5[:12], enabled_ids_str) 的 digest
# TTL 5 分钟, Streamlit rerun 不再重复打外网
_CREDS_CACHE_LOCK = threading.RLock()
_CREDS_CACHE: Dict[str, Tuple[Dict, float]] = {}
_CREDS_CACHE_TTL_SECONDS = 5 * 60

# ---------- 迁移: 旧版 JSON → 数据库 (一次性) ----------

def _migrate_json_to_db_if_needed() -> Tuple[int, str]:
    """启动期执行一次 JSON → DB 迁移。

    Returns:
        (migrated_count, message)
        migrated_count 为 0 代表没文件 / 没需要迁的 / 迁移失败
    """
    if not _OLD_CONTACTS_FILE.exists():
        return 0, "未发现旧版 JSON 文件, 跳过迁移"
    try:
        with open(_OLD_CONTACTS_FILE, "r", encoding="utf-8") as f:
            old_contacts = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error("读取旧联系人 JSON 失败, 跳过迁移: %s", e)
        return 0, f"旧 JSON 读取失败: {e}"

    if not old_contacts:
        # 空文件直接打备份标
        _bak = _OLD_CONTACTS_FILE.with_name(
            f"feishu_contacts.json.migrated.empty_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        )
        try:
            _OLD_CONTACTS_FILE.rename(_bak)
        except Exception:
            pass
        return 0, "旧 JSON 为空, 无需迁移"

    # 引入在这里做, 避免模块级相互 import 环
    from durability.database import (
        init_db, db_add_contact, db_list_contacts,
    )
    try:
        init_db()
    except Exception as e:
        logger.warning("迁移前 DB init 失败, 跳过: %s", e)
        return 0, f"DB 初始化失败: {e}"

    existing = db_list_contacts()
    existing_open_ids = {c.get("open_id", "") for c in existing if c.get("open_id")}
    existing_app_ids = {c.get("app_id", "") for c in existing if c.get("app_id")}

    migrated = 0
    skipped = 0
    failures = 0
    for c in old_contacts:
        oid = str(c.get("open_id", "")).strip()
        aid = str(c.get("app_id", "")).strip()
        if not oid or not aid:
            skipped += 1
            continue
        if oid in existing_open_ids or aid in existing_app_ids:
            # 已经迁过一遍了 (或旧文件本身重复), 不再写
            skipped += 1
            continue
        ok, _msg = db_add_contact(
            name=str(c.get("name", "未命名")),
            open_id=oid,
            phone=str(c.get("phone", "")),
            app_id=aid,
            app_secret=str(c.get("app_secret", "")),
            verified=bool(c.get("verified", False)),
        )
        if ok:
            migrated += 1
            existing_open_ids.add(oid)
            existing_app_ids.add(aid)
        else:
            failures += 1

    # 备份 JSON, 防止重复迁移
    _bak = _OLD_CONTACTS_FILE.with_name(
        f"feishu_contacts.json.migrated_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    )
    try:
        _OLD_CONTACTS_FILE.rename(_bak)
        logger.info(
            "飞书联系人 JSON→DB 迁移完成: 迁移 %d 条, 跳过 %d 条, 失败 %d 条 | 备份 %s",
            migrated, skipped, failures, _bak.name,
        )
    except Exception as e:
        logger.error("旧 JSON 文件备份失败: %s", e)
        return migrated, f"迁移 {migrated} 条, 但备份 JSON 失败: {e}"

    return migrated, f"迁移 {migrated} 条, 跳过 {skipped} 条, 失败 {failures} 条"


# 模块加载就尝试迁移 (保证任何入口只要 import 了这个模块, 旧 JSON 就自动迁好)
try:
    _MIGRATE_RESULT = _migrate_json_to_db_if_needed()
    if _MIGRATE_RESULT[0] > 0:
        logger.warning(
            "[飞书联系人自动迁移] %s | 备份文件位于 data/feishu_contacts.json.migrated_*",
            _MIGRATE_RESULT[1],
        )
except Exception as _mig_err:
    logger.exception("迁移旧 JSON 时发生异常, 忽略继续运行: %s", _mig_err)
    _MIGRATE_RESULT = (0, f"迁移异常: {_mig_err}")


def last_migration_status() -> Tuple[int, str]:
    """给 UI 展示用: 返回本次进程启动期的迁移结果。"""
    return _MIGRATE_RESULT


# ---------- 密钥验证 ----------

def verify_credentials(
    app_id: str,
    app_secret: str,
) -> Tuple[bool, str]:
    """验证飞书 app_id + app_secret, 返回 (success, token_or_error)。"""
    if not app_id or not app_secret:
        return False, "app_id 和 app_secret 不能为空"

    payload = {"app_id": app_id, "app_secret": app_secret}
    headers = {"Content-Type": "application/json; charset=utf-8"}

    for attempt in range(1, _MAX_RETRIES + 1):
        t0 = time.perf_counter()
        try:
            logger.info(
                "[飞书密钥] 请求 token (第%d/%d次) | app_id=%s timeout=%ds url=%s",
                attempt, _MAX_RETRIES, app_id, _REQUEST_TIMEOUT, _FEISHU_TOKEN_URL,
            )
            resp = requests.post(
                _FEISHU_TOKEN_URL, json=payload,
                headers=headers, timeout=_REQUEST_TIMEOUT,
            )
            dt_ms = (time.perf_counter() - t0) * 1000
            try:
                data = resp.json()
            except ValueError:
                logger.error(
                    "[飞书密钥] 响应非 JSON | HTTP=%s 耗时=%.0fms body_前100=%s",
                    resp.status_code, dt_ms, resp.text[:100],
                )
                return False, f"返回非 JSON (HTTP {resp.status_code})"

            if resp.status_code == 200 and data.get("code") == 0:
                token = data.get("tenant_access_token", "")
                expire = data.get("expire", -1)
                logger.info(
                    "[飞书密钥] ✅ 验证成功 | app_id=%s HTTP=200 耗时=%.0fms "
                    "token_len=%d expire_s=%s 重试次数=%d",
                    app_id, dt_ms, len(token), expire, attempt - 1,
                )
                return True, token
            else:
                msg = data.get("msg", f"HTTP {resp.status_code}")
                code = data.get("code", "<missing>")
                logger.warning(
                    "[飞书密钥] ❌ 验证失败 | app_id=%s HTTP=%s code=%s msg=%s 耗时=%.0fms 重试次数=%d",
                    app_id, resp.status_code, code, msg, dt_ms, attempt - 1,
                )
                return False, msg

        except requests.Timeout:
            dt_ms = (time.perf_counter() - t0) * 1000
            logger.warning(
                "[飞书密钥] ⏱️ 请求超时 (尝试 %d/%d) | app_id=%s 耗时=%.0fms 已超 %ds",
                attempt, _MAX_RETRIES, app_id, dt_ms, _REQUEST_TIMEOUT,
            )
            if attempt == _MAX_RETRIES:
                return False, "请求超时, 请检查网络连接"
        except Exception as e:
            logger.error("飞书验证异常: %s", e)
            return False, f"验证异常: {e}"

    return False, "验证失败(重试耗尽)"


# ---------- 人员管理 (存储层统一走 durability.database) ----------

def add_contact(
    name: str,
    open_id: str,
    phone: str,
    app_id: str,
    app_secret: str,
    verified: bool = False,
) -> Tuple[bool, str]:
    """添加飞书联系人 (写入 DB, 自动享受 MySQL→SQLite 降级保护)。

    兼容性:
    - 保持原 JSON 版返回 (bool, id_or_error_message) 契约不变
    - 额外对 app_id / open_id 做重复检查, 行为与旧 JSON 版一致
    """
    from durability.database import db_add_contact, db_list_contacts

    if not name or not name.strip():
        return False, "姓名不能为空"
    if not open_id or not open_id.strip():
        return False, "飞书 open_id 不能为空"
    if not app_id or not app_secret:
        return False, "App ID 和 App Secret 不能为空"

    # Python 层做 app_id / open_id 唯一检查 (DB 表没建唯一索引, 与旧 JSON 版逻辑一致)
    try:
        existing = db_list_contacts()
    except Exception:
        existing = []
    for c in existing:
        if c.get("app_id") == app_id:
            return False, f"App ID 已被 [{c.get('name', '?')}] 使用, 不能重复绑定"
        if c.get("open_id") == open_id:
            return False, f"open_id 已在 [{c.get('name', '?')}] 名下存在"

    ok, msg_or_id = db_add_contact(
        name=name, open_id=open_id, phone=phone,
        app_id=app_id, app_secret=app_secret, verified=verified,
    )
    if ok:
        logger.info(
            "新增联系人(DB): name=%s open_id=%s verified=%s id=%s",
            name, (open_id[:8] + "..." if len(open_id) > 8 else open_id), verified, msg_or_id,
        )
    return ok, msg_or_id


def remove_contact(contact_id: str) -> Tuple[bool, str]:
    from durability.database import db_remove_contact
    ok, msg = db_remove_contact(contact_id)
    if ok:
        logger.info("删除联系人(DB): id=%s", contact_id)
    return ok, msg


def toggle_contact(contact_id: str, enabled: bool) -> Tuple[bool, str]:
    from durability.database import db_toggle_contact
    ok, msg = db_toggle_contact(contact_id, enabled)
    if ok:
        logger.info("切换联系人启用(DB): id=%s enabled=%s", contact_id, enabled)
    return ok, msg


def list_contacts() -> List[Dict]:
    """返回所有联系人(隐藏 app_secret, 带 app_secret_masked 打码字段)。

    注: 直接复用 durability.database.db_list_contacts() 的结果结构,
        与原 JSON 版返回契约完全一致 (字段名/打码规则相同)。
    """
    from durability.database import db_list_contacts
    try:
        rows = db_list_contacts()
    except Exception as e:
        logger.error("查询联系人列表失败: %s", e)
        return []
    return rows


def get_verified_contacts() -> List[Dict]:
    """返回所有已验证且启用的联系人 (含完整 app_secret, 用于发消息)。

    与原 JSON 版返回契约一致: 包含 app_secret 字段 (发消息时要用)。
    durability.database.db_get_verified_contacts() 恰好不做 pop, 可直接返回。
    """
    from durability.database import db_get_verified_contacts
    try:
        return db_get_verified_contacts()
    except Exception as e:
        logger.error("查询已验证联系人失败: %s", e)
        return []


# ---------- 预警推送 (飞书 Open API) ----------

def _build_alert_text(event: Dict, contact: Dict, rig_id: str) -> str:
    """构建台架耐久预警飞书文本(结构化+企业阈值+Tab跳转提示)。"""
    ts = event.get("timestamp", datetime.now())
    ts_str = ts.strftime("%Y-%m-%d %H:%M:%S") if isinstance(ts, datetime) else str(ts)
    cyc = event.get("cycle_id", "?")
    pp = float(event.get("power_point", 0))
    label = event.get("label", "")
    operator = event.get("operator", ">")
    value = float(event.get("value", 0))
    threshold = float(event.get("threshold", 0))
    unit = event.get("unit", "mV")
    data_count = int(event.get("data_count", 0))
    quality = event.get("quality", "正常")
    name = contact.get("name", "老师")

    # 企业标准阈值说明(根据不同条件给业务人员判断指导)
    _std_note = ""
    cond_key = event.get("condition", "")
    if "离均差" in cond_key:
        _std_note = (
            "【企业标准】离均差反映单片电压一致性。>50mV 属于严重偏差段，建议：\n"
            "  ① 现场检查该循环的冷却液温度是否波动\n"
            "  ② 查看供气(氢气/空气)压力/流量是否稳定\n"
            "  ③ 拉该 6 个功率点的 LFR/HFR 曲线确认阻抗是否同步劣化"
        )
    elif "平均单体电压" in cond_key:
        _std_note = (
            "【企业标准】平均单体电压<600mV 已进入显著衰减区。建议：\n"
            "  ① 核对该台架的启停记录(是否频繁冷启动)\n"
            "  ② 对比同一功率档位前 3 个循环的电压，计算衰减速率\n"
            "  ③ 叠加极化曲线看高电流区是否塌陷(>150kW 掉压严重?)"
        )
    else:
        _std_note = "【通用】请登录系统对照原始曲线复核该数据段。"

    # 台架编号: 默认取 rig_id, 用户也可以在 expander 里改
    lines = [
        f"🚨 氢质氢离 · 台架耐久预警通知（功能4 · 第4个 Tab）",
        f"────────────────────────",
        f"【台架】 {rig_id}      【循环编号】 {cyc} (每组0.5h)",
        f"【功率点】 {pp:.1f} kW  (6档标准: 33 / 58.5 / 117 / 156 / 175.5 / 195)",
        f"【触发条件】 {label} {value:.1f}{unit} {operator} 阈值 {threshold:.0f}{unit}",
        f"【偏差幅度】 {abs(value - threshold):.1f}{unit} "
        f"({'超' if operator == '>' else '低于'}阈值 {abs(value - threshold) / max(threshold, 1e-6) * 100:.1f}%)",
        f"【样本量】 {data_count} 个数据点   【质量标记】 {quality}",
        f"【发生时间】 {ts_str}",
        f"────────────────────────",
        _std_note,
        f"────────────────────────",
        f"接收人：{name}",
        f"登录【🔬 台架耐久统计及预警】(第 4 个 Tab)查看原始散点图：",
        f"  Web 版: https://qingzhiqingli-2vsualb39bebgck2jgw2bh.streamlit.app/",
        f"  本地版: http://localhost:8501/",
    ]
    return "\n".join(lines)


def _send_feishu_message(
    app_id: str,
    app_secret: str,
    open_id: str,
    text_content: str,
) -> Tuple[bool, str]:
    """通过飞书 Open API 给指定 open_id 用户发送消息。

    步骤:
    1. 获取 tenant_access_token (verify_credentials, 已带详细日志)
    2. 调用 /im/v1/messages 发送文本消息 (本函数内加日志)
    """
    oid_mask = (open_id[:8] + "...") if isinstance(open_id, str) and len(open_id) > 8 else str(open_id)
    content_len = len(text_content)

    # 1. 获取 token
    t0_verify = time.perf_counter()
    ok, token_or_err = verify_credentials(app_id, app_secret)
    dt_verify_ms = (time.perf_counter() - t0_verify) * 1000
    if not ok:
        logger.error(
            "[飞书发消息] 中止: 密钥获取失败 | app_id=%s open_id_mask=%s 耗时=%.0fms 错误=%s",
            app_id, oid_mask, dt_verify_ms, token_or_err,
        )
        return False, f"密钥验证失败: {token_or_err}"

    token = token_or_err

    # 2. 发送消息
    payload = {
        "receive_id": open_id,
        "msg_type": "text",
        "content": json.dumps({"text": text_content}, ensure_ascii=False),
    }
    headers = {
        "Authorization": f"Bearer {token[:8]}...",  # 日志不要打印完整 token
        "Content-Type": "application/json; charset=utf-8",
    }
    t0_send = time.perf_counter()
    try:
        logger.info(
            "[飞书发消息] HTTP POST 开始 | app_id=%s open_id_mask=%s "
            "content_len=%d 字符 url=%s receive_id_type=open_id",
            app_id, oid_mask, content_len, _FEISHU_MESSAGE_URL,
        )
        resp = requests.post(
            f"{_FEISHU_MESSAGE_URL}?receive_id_type=open_id",
            json=payload, headers=headers, timeout=_REQUEST_TIMEOUT,
        )
        dt_send_ms = (time.perf_counter() - t0_send) * 1000
        try:
            data = resp.json()
        except ValueError:
            logger.error(
                "[飞书发消息] 响应非 JSON | HTTP=%s 耗时=%.0fms body_前120=%s",
                resp.status_code, dt_send_ms, resp.text[:120],
            )
            return False, f"返回非 JSON (HTTP {resp.status_code})"

        if resp.status_code == 200 and data.get("code") == 0:
            msg_id = str(data.get("data", {}).get("message_id", ""))
            logger.info(
                "[飞书发消息] ✅ 发送成功 | app_id=%s open_id_mask=%s HTTP=200 "
                "耗时=%.0fms 总耗时(含token)=%.0fms message_id=%s 内容长度=%d字符",
                app_id, oid_mask, dt_send_ms, dt_verify_ms + dt_send_ms,
                msg_id, content_len,
            )
            return True, "发送成功"
        else:
            msg = data.get("msg", f"HTTP {resp.status_code}")
            code = data.get("code", "<missing>")
            logger.warning(
                "[飞书发消息] ❌ 发送失败 | app_id=%s open_id_mask=%s HTTP=%s "
                "code=%s msg=%s 耗时=%.0fms",
                app_id, oid_mask, resp.status_code, code, msg, dt_send_ms,
            )
            return False, msg

    except requests.Timeout:
        dt_send_ms = (time.perf_counter() - t0_send) * 1000
        logger.error(
            "[飞书发消息] ⏱️ HTTP 超时 | app_id=%s open_id_mask=%s 耗时=%.0fms "
            "已超过 timeout=%ds",
            app_id, oid_mask, dt_send_ms, _REQUEST_TIMEOUT,
        )
        return False, f"请求超时 (> {_REQUEST_TIMEOUT}s), 请检查外网是否可达飞书"
    except Exception as e:
        dt_send_ms = (time.perf_counter() - t0_send) * 1000
        logger.exception(
            "[飞书发消息] 💥 发送异常 | app_id=%s open_id_mask=%s 耗时=%.0fms err=%r",
            app_id, oid_mask, dt_send_ms, e,
        )
        return False, f"发送异常: {e}"


def send_alert_to_contacts(
    event: Dict,
    contacts: Optional[List[Dict]] = None,
    rig_id: str = "台架A",
) -> List[Dict]:
    """向已验证的飞书人员推送预警通知(通过 Open API 直接发消息给个人)。

    关键节点全部带结构化日志,便于排查发送失败卡在哪一步:
      [台架预警发送] 入口 → 联系人过滤 → 逐人发送前 → 发送后 → 结束汇总

    Args:
        event: 预警事件 dict
        contacts: 联系人列表; None=自动获取已验证人员
        rig_id: 台架编号

    Returns:
        推送结果列表, 每项 {contact_id, name, success, message}
    """
    t_total = time.perf_counter()
    ev_short = (
        f"cycle={event.get('cycle_id','?')} "
        f"pp={event.get('power_point',0):.1f}kW "
        f"cond={event.get('condition','?')}"
    )
    logger.info("[台架预警发送] ========== 开始 ========== | %s | rig_id=%s", ev_short, rig_id)

    if contacts is None:
        t_fetch = time.perf_counter()
        contacts = get_verified_contacts()
        dt_fetch_ms = (time.perf_counter() - t_fetch) * 1000
        logger.info(
            "[台架预警发送] 步骤1 联系人拉取: DB查询 %d 位已验证联系人 | 耗时=%.0fms",
            len(contacts), dt_fetch_ms,
        )
    else:
        logger.info(
            "[台架预警发送] 步骤1 联系人拉取: 调用方已传入 contacts=%d 人(跳过DB拉取)",
            len(contacts),
        )

    if not contacts:
        logger.warning(
            "[台架预警发送] ⛔ 提前结束: 0 位已验证联系人 → 请先去「飞书人员对接」Tab"
            "新增联系人并点「发送测试消息」把 verified 打勾。 | %s", ev_short,
        )
        return []

    # 过滤: 必须 enabled=True + open_id 非空 + app_id 非空 + app_secret 非空
    # (用户可能把某个联系人临时禁用、或者 open_id 没填)
    valid_contacts: List[Dict] = []
    invalid_notes: List[str] = []
    for c in contacts:
        cid = str(c.get("id", ""))[:10]
        name = str(c.get("name", "?"))
        enabled = bool(c.get("enabled", False))
        oid_ok = bool(c.get("open_id") and str(c.get("open_id", "")).strip())
        app_ok = bool(c.get("app_id") and str(c.get("app_id", "")).strip())
        sec_ok = bool(c.get("app_secret") and len(str(c.get("app_secret", ""))) >= 8)
        if enabled and oid_ok and app_ok and sec_ok:
            valid_contacts.append(c)
        else:
            reasons = []
            if not enabled:
                reasons.append("enabled=False")
            if not oid_ok:
                reasons.append("open_id空")
            if not app_ok:
                reasons.append("app_id空")
            if not sec_ok:
                reasons.append("app_secret<8位或空")
            invalid_notes.append(f"[{cid}] {name}: " + "/".join(reasons))
    logger.info(
        "[台架预警发送] 步骤2 联系人有效性过滤: 共%d 位, 有效%d 位, 跳过%d 位",
        len(contacts), len(valid_contacts), len(invalid_notes),
    )
    if invalid_notes:
        for note in invalid_notes:
            logger.warning("[台架预警发送] 跳过(配置不合规): %s", note)

    if not valid_contacts:
        logger.warning("[台架预警发送] ⛔ 提前结束: 0 位有效联系人(全部被跳过)。 | %s", ev_short)
        return []

    results: List[Dict] = []
    ok_cnt = 0
    fail_cnt = 0
    for idx, c in enumerate(valid_contacts, 1):
        name = str(c.get("name", "?"))
        oid = str(c.get("open_id", ""))
        oid_mask = oid[:8] + "..." if len(oid) > 8 else oid
        app_id = str(c.get("app_id", ""))
        app_id_mask = app_id[:8] + "..." if len(app_id) > 8 else app_id
        sec_len = len(str(c.get("app_secret", "")))
        logger.info(
            "[台架预警发送] 步骤3 开始发送 第%d/%d位: 联系人=%s | app_id=%s | "
            "open_id_mask=%s | app_secret_len=%d | %s",
            idx, len(valid_contacts), name, app_id_mask, oid_mask, sec_len, ev_short,
        )

        t_build = time.perf_counter()
        try:
            text = _build_alert_text(event, c, rig_id)
        except Exception as e:
            logger.exception("[台架预警发送] 步骤3.1 文本构建异常: 联系人=%s err=%r", name, e)
            results.append({
                "contact_id": c.get("id", ""), "name": name,
                "success": False, "message": f"构建文本异常: {e}",
            })
            fail_cnt += 1
            continue
        dt_build_ms = (time.perf_counter() - t_build) * 1000
        logger.info(
            "[台架预警发送] 步骤3.1 文本构建完成: len=%d字符 | 耗时=%.0fms | 联系人=%s",
            len(text), dt_build_ms, name,
        )

        t_send = time.perf_counter()
        success, msg = _send_feishu_message(
            c.get("app_id", ""),
            c.get("app_secret", ""),
            c.get("open_id", ""),
            text,
        )
        dt_send_ms = (time.perf_counter() - t_send) * 1000
        results.append({
            "contact_id": c.get("id", ""),
            "name": name,
            "success": success,
            "message": msg,
        })

        if success:
            ok_cnt += 1
            try:
                _update_last_alert(c.get("id", ""))
            except Exception as e:
                logger.warning(
                    "[台架预警发送] 步骤3.3 更新 last_alert 失败(不影响发送结果) "
                    "id=%s err=%s", c.get("id", ""), e,
                )
            logger.info(
                "[台架预警发送] ✅ 第%d/%d位发送成功: %s | 耗时=%.0fms | %s",
                idx, len(valid_contacts), name, dt_send_ms, ev_short,
            )
        else:
            fail_cnt += 1
            logger.warning(
                "[台架预警发送] ❌ 第%d/%d位发送失败: %s | 耗时=%.0fms | 原因=%s | %s",
                idx, len(valid_contacts), name, dt_send_ms, msg, ev_short,
            )

        # 飞书限速 0.5s/人(最后一位不加 sleep 省时间)
        if idx < len(valid_contacts):
            time.sleep(0.5)

    dt_total_ms = (time.perf_counter() - t_total) * 1000
    logger.info(
        "[台架预警发送] ========== 结束 ========== | %s | rig_id=%s | "
        "有效联系人=%d位 成功=%d 失败=%d | 总耗时=%.0fms | 平均=%.0fms/人",
        ev_short, rig_id, len(valid_contacts), ok_cnt, fail_cnt,
        dt_total_ms, dt_total_ms / max(len(valid_contacts), 1),
    )

    return results


def _update_last_alert(contact_id: str) -> None:
    """推送成功后更新联系人 last_alert 时间 (写 DB)。"""
    from durability.database import db_update_last_alert
    try:
        db_update_last_alert(contact_id)
    except Exception as e:
        logger.warning("更新 last_alert 失败 id=%s err=%s", contact_id, e)


# ---------- 测试消息 + 自动打标 verified=True ----------

def _build_test_message_text(contact: Dict) -> str:
    name = contact.get("name", "您")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cid = contact.get("id", "")
    backend_info: Dict[str, str] = {"backend": "未知"}
    try:
        from durability.database import get_db_backend_info
        backend_info = get_db_backend_info()
    except Exception:
        pass
    return (
        f"✅ 氢质氢离数据分析助手 · 飞书对接测试消息\n\n"
        f"Hi {name},\n\n"
        f"这条是你点击「📤 发送测试消息」按钮后由系统发出的测试推送。\n"
        f"如果你能看到本条消息，说明:\n"
        f"  ① 你的 App ID / App Secret 配置正确\n"
        f"  ② 你的 open_id ({str(contact.get('open_id',''))[:8]}...) 正确\n"
        f"  ③ 飞书 Open API 链路 (获取 tenant_access_token → 发送 text 消息) 100% 可用\n\n"
        f"系统已自动把该联系人标记为 「已验证 verified=True」，\n"
        f"后续有预警事件就会推送到你这里。\n\n"
        f"─── 调试信息 ───\n"
        f"联系人 ID : {cid}\n"
        f"存储后端  : {backend_info.get('backend', '?')}\n"
        f"发送时间  : {now}\n"
    )


def send_test_message(contact_id: str) -> Tuple[bool, str]:
    """给指定联系人发送一条测试消息; 成功后自动把 verified 设为 True。

    Returns:
        (success, message)
    """
    from durability.database import db_get_contact_raw, db_set_contact_verified

    contact_id = str(contact_id or "").strip()
    logger.info(
        "[测试消息] ========== 开始 ========== | id=%s",
        contact_id or "<空>",
    )
    t0_total = time.perf_counter()
    if not contact_id:
        logger.error("[测试消息] ❌ 入参为空 contact_id, 拒绝")
        return False, "联系人 ID 不能为空"

    t0_fetch = time.perf_counter()
    c = db_get_contact_raw(contact_id)
    dt_fetch_ms = (time.perf_counter() - t0_fetch) * 1000
    if not c:
        logger.error(
            "[测试消息] ❌ 联系人不存在 | id=%s DB 耗时=%.0fms",
            contact_id, dt_fetch_ms,
        )
        return False, f"联系人 {contact_id} 不存在 / 读不到详情"

    cid = c.get("id", "")
    name = str(c.get("name") or "")
    app_id = str(c.get("app_id") or "")
    open_id = str(c.get("open_id") or "")
    enabled = bool(c.get("enabled", True))
    verified_old = bool(c.get("verified"))

    logger.info(
        "[测试消息] 联系人信息 | id=%s name=%s app_id=%s "
        "open_id_mask=%s enabled=%s verified(旧)=%s DB 耗时=%.0fms",
        cid, name, app_id, (open_id[:8] + "...") if len(open_id) > 8 else open_id,
        enabled, verified_old, dt_fetch_ms,
    )

    if not enabled:
        logger.warning(
            "[测试消息] ❌ 联系人已禁用 | id=%s name=%s 拒绝发送",
            cid, name,
        )
        return False, f"联系人 {name} 已被禁用, 请先启用再发送测试消息"

    sec = str(c.get("app_secret") or "")
    if not app_id or not sec or not open_id:
        logger.error(
            "[测试消息] ❌ 联系人字段缺失 | id=%s app_id_empty=%s "
            "app_secret_empty=%s open_id_empty=%s",
            cid, not app_id, not sec, not open_id,
        )
        return False, "联系人缺少 app_id / app_secret / open_id, 无法发送"

    text = _build_test_message_text(c)

    t0_send = time.perf_counter()
    ok, msg = _send_feishu_message(app_id, sec, open_id, text)
    dt_send_ms = (time.perf_counter() - t0_send) * 1000

    if not ok:
        dt_total_ms = (time.perf_counter() - t0_total) * 1000
        logger.warning(
            "[测试消息] ❌ 飞书发送失败, verified 保持 %s 不变 | "
            "id=%s name=%s send耗时=%.0fms 总耗时=%.0fms err=%s",
            verified_old, cid, name, dt_send_ms, dt_total_ms, msg,
        )
        return False, f"飞书接口返回错误: {msg}"

    # ---------- 发送成功: 自动 verified=True + last_alert ----------
    t0_db = time.perf_counter()
    _v_ok, _v_msg = db_set_contact_verified(contact_id, True)
    try:
        _update_last_alert(contact_id)
    except Exception as _e:
        logger.warning("[测试消息] 更新 last_alert 失败 id=%s err=%s", contact_id, _e)
    dt_db_ms = (time.perf_counter() - t0_db) * 1000
    dt_total_ms = (time.perf_counter() - t0_total) * 1000

    if not _v_ok:
        logger.error(
            "[测试消息] ⚠️ 发送成功,但 DB 更新 verified 失败 | "
            "id=%s name=%s db_set_verified结果=%s DB耗时=%.0fms 总耗时=%.0fms",
            cid, name, _v_msg, dt_db_ms, dt_total_ms,
        )
        return True, f"测试消息已送达, 但 auto verified 设置失败: {_v_msg}"

    logger.info(
        "[测试消息] ✅ 全流程成功 | id=%s name=%s "
        "verified: %s → True 发送耗时=%.0fms DB写回耗时=%.0fms 总耗时=%.0fms",
        cid, name, verified_old, dt_send_ms, dt_db_ms, dt_total_ms,
    )
    return True, (
        f"测试消息已送达 {name}, 已自动标记为已验证 (verified=True)。"
        f" 后续预警事件将按此配置推送。"
    )


# ---------- 密钥过期自动检测 ----------

def detect_all_credentials_status(
    skip_disabled: bool = True,
    use_cache: bool = True,
) -> Dict:
    """对所有已启用联系人执行一次密钥有效期巡检 (自动去重同 app_id+secret)。

    ⚠️  本函数会对外网发起 HTTP (飞书 tenant_access_token 接口),
    每个**不同的 (app_id, app_secret)** 只测一次 (即使被 N 个联系人共用);
    结果会缓存 5 分钟, 避免 Streamlit 每轮 rerun 重复打飞书接口。

    Args:
        skip_disabled: True=只检测启用联系人, False=全量 (禁用的也测, 但仍标记为 skip)
        use_cache: 是否优先命中缓存 (预检场景保持 True, 点『重新检测』时传 False)

    Returns:
        {
          "cache_hit": bool,
          "checked_seconds_ago": float|None,
          "total": int,
          "skipped_disabled": int,
          "app_groups": int,            # 实际请求飞书的去重组数
          "total_elapsed_ms": float,
          "per_contact": {
            cid: {
              "name", "app_id", "enabled", "verified",
              "status": "valid" | "invalid" | "timeout" | "network_err" | "unknown" | "skipped_disabled",
              "code": int|str|None,        # 飞书返回错误码, 10003=密钥不对/过期
              "msg": str,                  # 人类可读
              "elapsed_ms": float,         # 本组密钥检测耗时 (分摊到组内所有联系人)
            }
          },
          "summary": { "valid": n, "invalid": n, "timeout": n, "network_err": n, "skipped_disabled": n }
        }
    """
    from durability.database import db_get_contact_raw, get_db_backend_info

    t_all_0 = time.perf_counter()
    db_info = get_db_backend_info()

    all_contacts = list_contacts()
    total = len(all_contacts)
    enabled_cids = [c["id"] for c in all_contacts if c.get("enabled")]
    disabled_cids = [c["id"] for c in all_contacts if not c.get("enabled")]

    # 1. 先做缓存 key 计算
    cache_source_parts = [f"db={db_info.get('backend','')}-{db_info.get('host_or_path','')}", f"total={total}"]
    for c in sorted(all_contacts, key=lambda x: x.get("id", "")):
        sec = c.get("app_secret") or ""  # list_contacts 返回的是空串(脱敏), 后面替换
        cache_source_parts.append(
            f"cid={c.get('id')}|app={c.get('app_id','')}|en={int(bool(c.get('enabled')))}|v={int(bool(c.get('verified')))}"
        )
    # 把每个 enabled cid 的真实 secret 用 md5(前 12 位) 拼入 cache_key —— 只有 secret 变了才会失效缓存
    for cid in enabled_cids:
        raw = db_get_contact_raw(cid)
        sec = str((raw or {}).get("app_secret") or "")
        md = hashlib.md5(sec.encode("utf-8")).hexdigest()[:12]
        cache_source_parts.append(f"secret_digest_{cid}={md}")
    cache_key = hashlib.sha256("||".join(cache_source_parts).encode("utf-8")).hexdigest()

    # 2. 命中即返回
    if use_cache:
        with _CREDS_CACHE_LOCK:
            cached = _CREDS_CACHE.get(cache_key)
            if cached:
                payload, ts = cached
                age_s = (time.perf_counter() - ts)
                if age_s < _CREDS_CACHE_TTL_SECONDS:
                    payload["cache_hit"] = True
                    payload["checked_seconds_ago"] = round(age_s, 1)
                    logger.info(
                        "[密钥巡检] 命中缓存 (%s 前) | 总联系人=%d app_groups=%d valid=%s invalid=%s",
                        f"{age_s:.0f}s", payload["total"], payload.get("app_groups"),
                        payload["summary"].get("valid"), payload["summary"].get("invalid"),
                    )
                    return payload
                # 过期清掉, 后面重测
                _CREDS_CACHE.pop(cache_key, None)

    # 3. 取 enabled 联系人的 raw (含 secret)
    enabled_raw: Dict[str, Dict] = {}
    for cid in enabled_cids:
        r = db_get_contact_raw(cid)
        if r and r.get("enabled"):
            enabled_raw[cid] = r

    # 4. 按 (app_id, app_secret) 去重 (同一个自建应用只测一次)
    groups: Dict[Tuple[str, str], List[str]] = {}  # (app_id, secret) -> [cid...]
    for cid, raw in enabled_raw.items():
        aid = str(raw.get("app_id") or "")
        sec = str(raw.get("app_secret") or "")
        if not aid or not sec:
            continue
        groups.setdefault((aid, sec), []).append(cid)

    app_groups = len(groups)
    per_contact: Dict[str, Dict] = {}
    summary = {"valid": 0, "invalid": 0, "timeout": 0, "network_err": 0,
               "unknown": 0, "skipped_disabled": len(disabled_cids) if skip_disabled else 0}

    # 5. 给 disabled 的先写 skipped_disabled / 或者如果 skip_disabled=False 也测 (不发消息, 只测密钥)
    for c in all_contacts:
        cid = c["id"]
        if not c.get("enabled") and skip_disabled:
            per_contact[cid] = dict(
                name=c.get("name",""), app_id=c.get("app_id",""),
                enabled=False, verified=bool(c.get("verified")),
                status="skipped_disabled", code=None,
                msg="已禁用, 跳过密钥检测", elapsed_ms=0.0,
            )

    # 6. 每个 app_group 只请求一次 verify_credentials
    logger.info(
        "[密钥巡检] 开始 | 总联系人=%d 启用=%d (需检测) 禁用=%d app去重组=%s 存储后端=%s | cache_hit=false",
        total, len(enabled_cids), len(disabled_cids), app_groups, db_info.get("backend"),
    )

    for (app_id, app_secret), cids_in_group in groups.items():
        # 只对第一个 cid 取 name 用于日志 (其他 name 可能不同, 但密钥相同)
        first_name = ""
        for c in all_contacts:
            if c["id"] == cids_in_group[0]:
                first_name = c.get("name","")
                break
        t0 = time.perf_counter()
        ok, token_or_err = verify_credentials(app_id, app_secret)
        dt_ms = (time.perf_counter() - t0) * 1000

        # 解析 verify_credentials 返回的错误码: 返回 (False, "密钥验证失败: xxx") 之前在日志里有 code
        # 但 verify_credentials 不单独返回 code, 我们就用一次额外的 parse: token_or_err 形如
        # "app_id 和 app_secret 不能为空" / "请求超时..." / "返回非 JSON (HTTP ..)" / "invalid param"
        status = "unknown"
        code: Optional[str] = None
        msg = token_or_err if isinstance(token_or_err, str) else ""
        if ok:
            status = "valid"
            code = None
            msg = "密钥有效 (tenant_access_token 获取成功)"
        else:
            # 从 verify_credentials 日志的已知错误模式解析状态分类
            if "超时" in msg or "timeout" in msg.lower():
                status = "timeout"
                code = "TIMEOUT"
            elif "非 JSON" in msg or "HTTP" in msg:
                status = "network_err"
                code = "BAD_HTTP"
            elif "不能为空" in msg:
                status = "invalid"
                code = "EMPTY_CREDS"
            else:
                status = "invalid"
                # 对典型 invalid param (=10003) 用 code=10003 做显式标记, 其他 code 暂时存原文
                code = "10003" if "invalid param" in msg else msg[:20]

        # verify_credentials 自己的日志里已经有 HTTP code & msg, 这里补充 [密钥巡检] 摘要日志
        logger.info(
            "[密钥巡检] app_group 结果 | app_id=%s 覆盖人数=%d (例如:%s) status=%s code=%s 耗时=%.0fms msg=%s",
            app_id, len(cids_in_group), first_name, status, code, dt_ms, msg,
        )

        # 分摊到组内每个联系人
        for cid in cids_in_group:
            c_meta = next((x for x in all_contacts if x["id"] == cid), {})
            entry = dict(
                name=c_meta.get("name",""), app_id=app_id,
                enabled=True, verified=bool(c_meta.get("verified")),
                status=status, code=code, msg=msg,
                elapsed_ms=round(dt_ms, 1),
            )
            per_contact[cid] = entry
            if status in summary:
                summary[status] += 1
            else:
                summary["unknown"] += 1

    # 7. 对 skip_disabled=False 的情况, 禁用联系人也单独测一次
    if not skip_disabled:
        for cid in disabled_cids:
            raw = db_get_contact_raw(cid)
            if not raw:
                c_meta = next((x for x in all_contacts if x["id"] == cid), {})
                per_contact[cid] = dict(
                    name=c_meta.get("name",""), app_id=c_meta.get("app_id",""),
                    enabled=False, verified=bool(c_meta.get("verified")),
                    status="unknown", code=None, msg="读不到详情", elapsed_ms=0,
                )
                summary["unknown"] += 1
                continue
            aid = str(raw.get("app_id") or "")
            sec = str(raw.get("app_secret") or "")
            if not aid or not sec:
                per_contact[cid] = dict(
                    name=raw.get("name",""), app_id=aid, enabled=False,
                    verified=bool(raw.get("verified")), status="invalid",
                    code="EMPTY_CREDS", msg="空密钥", elapsed_ms=0,
                )
                summary["invalid"] += 1
                continue
            t0 = time.perf_counter()
            ok, token_or_err = verify_credentials(aid, sec)
            dt_ms = (time.perf_counter() - t0) * 1000
            if ok:
                st = "valid"; cd = None; m_ = "密钥有效"
            elif "超时" in str(token_or_err):
                st = "timeout"; cd = "TIMEOUT"; m_ = str(token_or_err)
            else:
                st = "invalid"
                cd = "10003" if "invalid param" in str(token_or_err) else None
                m_ = str(token_or_err)
            per_contact[cid] = dict(
                name=raw.get("name",""), app_id=aid, enabled=False,
                verified=bool(raw.get("verified")), status=st, code=cd, msg=m_,
                elapsed_ms=round(dt_ms, 1),
            )
            summary[st] = summary.get(st, 0) + 1

    total_elapsed_ms = (time.perf_counter() - t_all_0) * 1000

    # 8. 组装结果并写入缓存
    result = dict(
        cache_hit=False,
        checked_seconds_ago=0.0,
        total=total,
        skipped_disabled=summary.get("skipped_disabled", 0),
        app_groups=app_groups,
        total_elapsed_ms=round(total_elapsed_ms, 1),
        per_contact=per_contact,
        summary=summary,
    )
    with _CREDS_CACHE_LOCK:
        _CREDS_CACHE[cache_key] = (result, time.perf_counter())
    logger.info(
        "[密钥巡检] ✅ 完成 | 总=%d 去重请求=%d 总耗时=%.0fms | 有效=%d 失效=%s 超时=%d 网络错=%d 跳过禁用=%d",
        total, app_groups, total_elapsed_ms,
        summary.get("valid", 0), summary.get("invalid", 0),
        summary.get("timeout", 0), summary.get("network_err", 0),
        summary.get("skipped_disabled", 0),
    )
    return result


def credentials_status_text(status: str, code=None) -> str:
    """把 detect_all_credentials_status 的机器状态 转成 1 行人类可读文本, 用于控制台/表格展示。"""
    s = status or ""
    if s == "valid":
        return "✅ 密钥有效"
    if s == "invalid":
        tag = f" (code={code})" if code else ""
        if code == "10003" or (isinstance(code, int) and code == 10003):
            return f"❌ 已过期/错误密钥{tag} → 请在飞书管理后台重置 App Secret"
        if code == "EMPTY_CREDS":
            return "❌ 空 AppID/Secret, 请先在『📝 新增对接』填入"
        return f"⚠️ 验证失败{tag}"
    if s == "timeout":
        return "⏱️ 外网超时, 检查是否能访问 open.feishu.cn (可能需要走代理/VPN)"
    if s == "network_err":
        return "💥 飞书网关响应异常 (非 JSON / HTTP 非 200), 查 status_code / 证书"
    if s == "skipped_disabled":
        return "⏸️ 已禁用, 跳过检测"
    return f"❓ 未知状态: {s}"


# ---------- 单元测试 ----------

if __name__ == "__main__":
    import logging as _lg
    _lg.basicConfig(level=_lg.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    print("\n===== 测试1: 添加联系人 =====")
    ok, msg = add_contact(
        name="测试员A", open_id="ou_test1234567890",
        phone="13800001111",
        app_id="cli_test1", app_secret="sec_test1", verified=False,
    )
    assert ok, f"添加失败: {msg}"
    print(f"  [PASS] 添加成功: id={msg}")

    print("\n===== 测试2: 重复 app_id(应失败) =====")
    ok2, msg2 = add_contact(
        name="测试员B", open_id="ou_test9876543210",
        phone="13800002222",
        app_id="cli_test1", app_secret="sec_test2",
    )
    assert not ok2, "重复 app_id 应失败"
    print(f"  [PASS] 重复检测: {msg2}")

    print("\n===== 测试3: 列表(隐藏 secret) =====")
    contacts = list_contacts()
    assert len(contacts) >= 1
    c0 = contacts[0]
    assert "app_secret" not in c0, "不应暴露完整 secret"
    assert "app_secret_masked" in c0, "应有 masked secret"
    assert "open_id" in c0, "应有 open_id"
    print(f"  [PASS] 列表 {len(contacts)} 人, open_id={c0['open_id']}, secret={c0['app_secret_masked']}")

    print("\n===== 测试4: 删除联系人 =====")
    ok4, msg4 = remove_contact(c0["id"])
    assert ok4, f"删除失败: {msg4}"
    assert len(list_contacts()) == len(contacts) - 1
    print(f"  [PASS] 删除成功")

    print("\n===== 测试5: 验证假密钥(应失败) =====")
    ok5, msg5 = verify_credentials("fake_id", "fake_secret")
    print(f"  [INFO] 验证结果: ok={ok5} msg={msg5}")

    print("\n===== 测试6: 向空联系人推送(应返回空) =====")
    results = send_alert_to_contacts({"value": 55.0, "threshold": 50.0,
                                       "label": "离均差", "operator": ">",
                                       "cycle_id": 0, "power_point": 175.5,
                                       "data_count": 100, "quality": "正常",
                                       "timestamp": datetime.now()})
    assert len(results) == 0
    print(f"  [PASS] 空联系人返回空结果")

    print("\n===== 全部测试通过 =====")
