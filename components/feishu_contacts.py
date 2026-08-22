"""飞书固定人员对接系统 UI 组件。

提供三个子页面:
1. 新增对接 - 输入飞书人员信息并验证密钥
2. 人员管理 - 展示所有已对接人员,支持启用/禁用/删除
3. 预警测试 - 模拟预警事件并推送到已验证人员
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd
import streamlit as st

logger = logging.getLogger(__name__)


def _render_new_contact_form() -> None:
    """渲染新增对接人员表单。"""
    st.subheader("新增飞书人员对接")
    st.caption("输入飞书应用凭证,验证通过后即可接收预警通知")

    with st.form("feishu_contact_form", clear_on_submit=True):
        col1, col2 = st.columns(2)
        with col1:
            name = st.text_input("姓名 *", placeholder="如: 张工", key="fc_name")
            phone = st.text_input("手机号", placeholder="如: 13800001111", key="fc_phone")
        with col2:
            app_id = st.text_input(
                "飞书 App ID *", placeholder="cli_xxxx", key="fc_app_id",
                help="飞书开放平台 → 应用详情 → App ID",
            )
            app_secret = st.text_input(
                "飞书 App Secret *", type="password", placeholder="xxxxxx",
                key="fc_app_secret",
                help="飞书开放平台 → 应用详情 → App Secret",
            )

        open_id = st.text_input(
            "飞书 open_id *",
            placeholder="ou_xxxxxxxxxxxx",
            key="fc_open_id",
            help="飞书用户的 open_id (ou_ 开头)。获取方式: 飞书开放平台 → 应用 → 通讯录 → 获取用户 ID",
        )

        col_a, col_b = st.columns([1, 2])
        with col_a:
            verify_btn = st.form_submit_button("🔍 验证密钥", type="secondary")
        with col_b:
            submit_btn = st.form_submit_button("💾 保存对接", type="primary")

    # 验证密钥(不提交表单)
    if verify_btn:
        if not app_id or not app_secret:
            st.error("请填写 App ID 和 App Secret")
        else:
            from durability.feishu_contacts import verify_credentials
            with st.spinner("正在验证飞书密钥..."):
                ok, msg = verify_credentials(app_id, app_secret)
            if ok:
                st.session_state["fc_verified"] = True
                st.session_state["fc_verified_app_id"] = app_id
                st.success("✅ 验证成功! 密钥有效,可保存对接")
            else:
                st.session_state["fc_verified"] = False
                st.error(f"❌ 验证失败: {msg}")

    # 保存对接
    if submit_btn:
        _handle_save(name, phone, open_id, app_id, app_secret)


def _handle_save(
    name: str, phone: str, open_id: str,
    app_id: str, app_secret: str,
) -> None:
    """处理保存对接表单提交。"""
    # 检查必填项
    missing = []
    if not name:
        missing.append("姓名")
    if not open_id:
        missing.append("open_id")
    if not app_id:
        missing.append("App ID")
    if not app_secret:
        missing.append("App Secret")
    if missing:
        st.error(f"请填写必填项: {', '.join(missing)}")
        return

    # 检查密钥验证状态
    verified = st.session_state.get("fc_verified", False)
    verified_app_id = st.session_state.get("fc_verified_app_id", "")
    if not verified or verified_app_id != app_id:
        st.warning("⚠️ 当前密钥未通过验证, 请先点击「验证密钥」按钮")
        return

    from durability.feishu_contacts import add_contact, send_test_message
    ok, msg = add_contact(
        name=name, open_id=open_id, phone=phone,
        app_id=app_id, app_secret=app_secret, verified=True,
    )
    if ok:
        st.success(f"✅ 对接成功! [{name}] 已添加为飞书预警接收人")
        # 记录本次新增的联系人 ID 到 session_state, 下面显示测试消息按钮用
        st.session_state["fc_last_added_id"] = msg
        st.session_state["fc_last_added_name"] = name
        st.session_state["fc_verified"] = False
        logger.info("飞书人员对接成功: name=%s id=%s", name, msg)
    else:
        st.error(f"❌ 保存失败: {msg}")
        st.session_state.pop("fc_last_added_id", None)

    # ============== 新增: 保存成功后显示「📤 发送测试消息 (自动验证)」按钮
    last_added_id = st.session_state.get("fc_last_added_id", "")
    last_added_name = st.session_state.get("fc_last_added_name", "")
    if last_added_id and last_added_name:
        st.markdown("---")
        c_send1, c_send2 = st.columns([1, 2])
        with c_send1:
            if st.button(
                f"📤 给 [{last_added_name}] 发送测试消息 (送达后自动 verified=True)",
                type="primary",
                key="send_test_newly_added",
            ):
                with st.spinner(f"正在向 {last_added_name} 发送飞书测试消息..."):
                    test_ok, test_msg = send_test_message(last_added_id)
                if test_ok:
                    st.success(f"✅ {test_msg}")
                    # 界面立刻反映 verified=True
                    st.session_state.pop("fc_last_added_id", None)
                else:
                    st.error(f"❌ 发送失败: {test_msg}")
                    st.caption(
                        "常见原因:\n"
                        "  ① App ID / Secret 错误 → 去飞书管理后台重新核对\n"
                        "  ② open_id 不是 `ou_` 开头或拼写错误\n"
                        "  ③ 当前应用没给该用户所在的企微/飞书组织权限"
                    )
        with c_send2:
            st.caption(
                "点击该按钮会:\n"
                "  ① 用该联系人的 App ID / Secret 走一遍真实的飞书 Open API;\n"
                "  ② 如果消息成功送达到该 open_id, 自动把 verified 标记为 True;\n"
                "  ③ 后续预警事件推送时会自动通知此人。"
            )


def _render_contacts_management() -> None:
    """渲染已对接人员管理页面。"""
    from durability.feishu_contacts import (
        list_contacts, remove_contact, toggle_contact, send_test_message,
    )

    st.subheader("已对接飞书人员管理")
    contacts = list_contacts()

    if not contacts:
        st.info("暂无已对接的飞书人员。请前往「新增对接」页面添加。")
        return

    # 统计卡片
    col1, col2, col3 = st.columns(3)
    total = len(contacts)
    verified_cnt = sum(1 for c in contacts if c.get("verified"))
    enabled_cnt = sum(1 for c in contacts if c.get("enabled", True))
    col1.metric("总对接人数", total)
    col2.metric("已验证", verified_cnt)
    col3.metric("已启用", enabled_cnt)

    st.markdown("---")

    # 表格展示
    df = pd.DataFrame(contacts)
    display_cols = {
        "name": "姓名", "phone": "手机号", "open_id": "Open ID",
        "app_id": "App ID", "app_secret_masked": "App Secret",
        "verified": "验证状态", "enabled": "启用状态",
        "created_at": "对接时间", "last_alert": "最后预警",
    }
    df_display = df.rename(columns=display_cols)
    keep_cols = list(display_cols.values())
    df_display = df_display[[c for c in keep_cols if c in df_display.columns]]

    # 格式化布尔列
    if "验证状态" in df_display.columns:
        df_display["验证状态"] = df_display["验证状态"].map({True: "✅ 已验证", False: "❌ 未验证"})
    if "启用状态" in df_display.columns:
        df_display["启用状态"] = df_display["启用状态"].map({True: "✅ 启用", False: "⏸️ 禁用"})

    # open_id 截断显示
    if "Open ID" in df_display.columns:
        df_display["Open ID"] = df_display["Open ID"].apply(
            lambda x: x[:12] + "..." if isinstance(x, str) and len(x) > 12 else x
        )

    st.dataframe(df_display, use_container_width=True, hide_index=True)

    # 操作区
    st.markdown("---")
    st.subheader("操作")
    for c in contacts:
        with st.expander(
            f"{'✅' if c.get('verified') else '❌'} {c['name']} | "
            f"{c.get('phone', '无手机')} | {c.get('app_id', '')}",
            expanded=False,
        ):
            cid = c["id"]
            col_e, col_d, col_test = st.columns([1, 1, 1])
            with col_e:
                is_enabled = c.get("enabled", True)
                if st.button(
                    "⏸️ 禁用" if is_enabled else "▶️ 启用",
                    key=f"toggle_{cid}",
                ):
                    ok, msg = toggle_contact(cid, not is_enabled)
                    if ok:
                        st.rerun()
            with col_d:
                if st.button("🗑️ 删除", key=f"del_{cid}", type="secondary"):
                    ok, msg = remove_contact(cid)
                    if ok:
                        st.rerun()
            with col_test:
                _test_label = (
                    "📤 重测验证" if c.get("verified") else "📤 发送测试消息并激活"
                )
                if st.button(_test_label, key=f"test_{cid}", type="primary"):
                    with st.spinner(f"向 {c.get('name','')} 发送测试消息..."):
                        t_ok, t_msg = send_test_message(cid)
                    if t_ok:
                        st.success(f"✅ {t_msg}")
                        st.rerun()
                    else:
                        st.error(f"❌ {t_msg}")

            last_alert = c.get("last_alert", "")
            if last_alert:
                st.caption(f"最后成功推送: {last_alert}")
            else:
                st.caption("尚未接收过预警/测试消息")


def _render_alert_test() -> None:
    """渲染预警测试推送页面。"""
    from durability.feishu_contacts import (
        get_verified_contacts, send_alert_to_contacts,
    )

    st.subheader("预警推送测试")
    st.caption("模拟预警事件,向已验证的飞书人员推送通知")

    contacts = get_verified_contacts()
    if not contacts:
        st.warning("暂无已验证且启用的飞书人员, 无法推送预警")
        return

    st.info(f"当前可推送人员: {len(contacts)} 人 - "
            f"{', '.join(c['name'] for c in contacts)}")

    # 模拟预警事件参数
    col1, col2, col3 = st.columns(3)
    with col1:
        cycle_id = st.number_input("循环编号", 0, 10, 0, key="at_cycle")
        power_point = st.selectbox(
            "功率点 (kW)", [33.0, 58.5, 117.0, 156.0, 175.5, 195.0],
            key="at_power", index=4,
        )
    with col2:
        condition = st.selectbox(
            "预警条件", ["离均差>50mV", "平均单体电压<600mV"],
            key="at_cond",
        )
        if condition == "离均差>50mV":
            value = st.number_input("离均差值 (mV)", 51.0, 100.0, 55.0, key="at_val")
            threshold = 50.0
            operator = ">"
            label = "离均差"
        else:
            value = st.number_input("平均电压 (mV)", 400.0, 599.0, 540.0, key="at_val")
            threshold = 600.0
            operator = "<"
            label = "平均单体电压"
    with col3:
        data_count = st.number_input("数据量", 10, 500, 100, key="at_cnt")
        quality = st.selectbox("数据质量", ["正常", "波动异常", "数据不足"], key="at_qual")

    if st.button("🚀 发送测试预警", type="primary"):
        event = {
            "timestamp": datetime.now(),
            "cycle_id": int(cycle_id),
            "power_point": float(power_point),
            "condition": condition,
            "value": float(value),
            "threshold": threshold,
            "operator": operator,
            "label": label,
            "data_count": int(data_count),
            "quality": quality,
            "message": f"{condition}: {value:.1f}mV {operator} {threshold:.0f}mV",
            "sent": False,
            "send_error": "",
        }

        with st.spinner("正在推送预警通知..."):
            results = send_alert_to_contacts(event, contacts)

        if not results:
            st.error("推送失败: 无可推送人员")
            return

        # 展示推送结果
        st.markdown("---")
        st.subheader("推送结果")
        for r in results:
            if r["success"]:
                st.success(f"✅ {r['name']}: 发送成功")
            else:
                st.error(f"❌ {r['name']}: {r['message']}")


def render_feishu_contacts() -> None:
    """渲染飞书人员对接系统主页面(含三个子页面)。"""
    st.subheader("📡 飞书固定人员对接系统")
    st.caption("管理飞书预警接收人,验证密钥,自动推送预警通知")

    # ===== 数据源 + 迁移说明卡片 (全局统一, 与 durability.database 打通) =====
    from durability.database import (
        get_db_backend_info,
    )
    from durability.feishu_contacts import (
        last_migration_status,
        detect_all_credentials_status,
        credentials_status_text,
        list_contacts as _list_feishu,
    )
    try:
        info = get_db_backend_info()
        backend = info.get("backend", "")
        migrate_cnt, migrate_msg = last_migration_status()
        with st.container(border=True):
            c_db, c_mig, c_creds = st.columns([2, 3, 3])
            with c_db:
                if "MySQL" in backend:
                    st.success(
                        f"**🗄️ 联系人存储后端: MySQL (腾讯云)**\n\n"
                        f"Host: `{info.get('host','')}:{info.get('port','')}`  \n"
                        f"DB: `{info.get('database','')}`  User: `{info.get('user','')}`\n\n"
                        f"外网中断时自动降级到本地 SQLite, 所有联系人都不会丢。"
                    )
                else:
                    note = info.get("note", "")
                    if note:
                        st.warning(
                            f"**⚠️  当前: SQLite (本地降级)**\n\n"
                            f"{note}  \n"
                            f"文件: `{info.get('path','')}`"
                        )
                    else:
                        st.info(
                            f"**🗄️ 联系人存储后端: SQLite (本地)**\n\n"
                            f".env 未配置 MySQL 或启动阶段已降级。  \n"
                            f"文件: `{info.get('path','')}`"
                        )
            with c_mig:
                if migrate_cnt > 0:
                    st.success(f"**✅ 旧 JSON 联系人已自动迁移**\n\n{migrate_msg}")
                elif migrate_msg and "未发现" not in migrate_msg and "无需迁移" not in migrate_msg:
                    st.info(f"**迁移日志**: {migrate_msg}")
                else:
                    st.info(
                        "**数据格式说明**\n\n"
                        "所有联系人现在统一存入数据库 (feishu_contacts 表)。\n"
                        "旧版 `data/feishu_contacts.json` 如果首次启动还存在, 会自动迁库并打 `.migrated` 备份。"
                    )
            # ==== 🔑 预检: 自动检测所有联系人密钥是否过期 (卡片第三列) ====
            with c_creds:
                # 只在真正有联系人时检测, 避免 Streamlit 每轮 rerun 都空跑
                _creds_result = None
                try:
                    _all_n = len(_list_feishu())
                    if _all_n > 0:
                        _creds_result = detect_all_credentials_status(
                            skip_disabled=True, use_cache=True,
                        )
                        sm = _creds_result.get("summary", {})
                        hit = _creds_result.get("cache_hit")
                        age = _creds_result.get("checked_seconds_ago") or 0
                        total_elapsed_ms = _creds_result.get("total_elapsed_ms", 0) or 0
                        # 标题 + 4 色分块
                        if sm.get("invalid", 0) > 0:
                            st.error(
                                f"**🔑 密钥预检: 发现 {sm.get('invalid')} 个失效密钥**  \n"
                                f"*(已缓存 {age:.0f}s 前, 刷新结果请按 F5 / 改 secret)*  \n"
                                f"去重 App 组={_creds_result.get('app_groups')}  "
                                f"总耗时 {total_elapsed_ms:.0f}ms"
                            )
                        elif sm.get("timeout", 0) or sm.get("network_err", 0):
                            st.warning(
                                f"**🔑 密钥预检: 网络/超时异常 ({sm.get('timeout',0)} 超时 / "
                                f"{sm.get('network_err',0)} 网关错误)**  \n"
                                f"请检查本机是否能访问 open.feishu.cn。"
                            )
                        else:
                            st.success(
                                f"**🔑 密钥预检: 全部有效**  \n"
                                f"(命中缓存 {age:.0f}s 前) 去重 App 组={_creds_result.get('app_groups')}  "
                                f"总耗时 {total_elapsed_ms:.0f}ms"
                            )
                        # 每个启用联系人 1 行
                        lines = []
                        per_c = _creds_result.get("per_contact", {})
                        for c in _list_feishu():
                            entry = per_c.get(c.get("id"), {})
                            status_text = credentials_status_text(
                                entry.get("status", ""), entry.get("code")
                            )
                            icon = "✅" if c.get("enabled") and c.get("verified") else (
                                "⛔" if not c.get("enabled") else "🔲"
                            )
                            lines.append(
                                f"- {icon} **{c.get('name','')}** `{c.get('app_id','') or '-'}`: "
                                f"{status_text}"
                            )
                        with st.expander("🔍 查看每个联系人的密钥状态明细", expanded=False):
                            st.markdown("\n".join(lines) or "_(空)_")
                    else:
                        st.info(
                            "**🔑 密钥预检**\n\n"
                            "当前还没有对接的飞书联系人。请在『📝 新增对接』填入 AppID/Secret 后, "
                            "这里会自动检测所有密钥的有效期。"
                        )
                except Exception as _creds_e:
                    st.warning(f"🔑 密钥预检失败 (不影响页面): {_creds_e}")
    except Exception as _e:
        logger.warning("渲染联系人后端说明失败: %s", _e, exc_info=True)

    # 子页面 Tab
    sub_tab1, sub_tab2, sub_tab3 = st.tabs([
        "📝 新增对接", "👥 人员管理", "🚨 预警测试",
    ])

    with sub_tab1:
        _render_new_contact_form()

    with sub_tab2:
        _render_contacts_management()

    with sub_tab3:
        _render_alert_test()
