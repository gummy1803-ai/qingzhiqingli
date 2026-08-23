"""分支管理 UI 组件: 提供分支管理的 Streamlit 界面。

功能:
1. 分支列表展示与操作
2. 文件结构差异可视化
3. 重复文件检测报告
4. 文件完整性校验报告
5. 分支合并与冲突解决界面
"""

import streamlit as st
import pandas as pd
from datetime import datetime
from pathlib import Path
from .branch_manager import BranchManager, get_branch_manager


def render_branch_management_page():
    """渲染分支管理主页面。"""
    st.title("🌿 分支管理")
    
    bm = get_branch_manager()
    branches = bm.list_branches()
    active_branch = bm.get_active_branch()
    
    # 顶部状态栏
    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        st.info(f"**当前分支**: `{active_branch}`")
    with col2:
        st.metric("分支总数", len(branches))
    with col3:
        total_files = sum(b.get("file_count", 0) for b in branches)
        st.metric("文件总数", total_files)
    
    # 操作面板
    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "📋 分支列表", "📁 文件结构", "🔍 检测与校验", 
        "🔀 分支对比", "📦 合并与冲突"
    ])
    
    with tab1:
        _render_branch_list_tab(bm, branches, active_branch)
    
    with tab2:
        _render_file_structure_tab(bm, active_branch)
    
    with tab3:
        _render_detection_tab(bm, active_branch)
    
    with tab4:
        _render_comparison_tab(bm, branches, active_branch)
    
    with tab5:
        _render_merge_tab(bm, branches, active_branch)


def _render_branch_list_tab(bm: BranchManager, branches: list, active_branch: str):
    """渲染分支列表标签页。"""
    # 创建新分支
    with st.expander("➕ 创建新分支", expanded=False):
        with st.form("create_branch_form"):
            new_branch_name = st.text_input("分支名称", placeholder="例如: feature-branch")
            new_branch_desc = st.text_area("分支描述", placeholder="描述此分支的用途")
            source_branch = st.selectbox(
                "来源分支",
                options=["(空分支)"] + [b["name"] for b in branches],
                help="选择从哪个分支复制文件"
            )
            
            col1, col2 = st.columns(2)
            with col1:
                if st.form_submit_button("创建空分支", use_container_width=True):
                    success, msg = bm.create_branch(new_branch_name, new_branch_desc)
                    if success:
                        st.success(msg)
                        st.rerun()
                    else:
                        st.error(msg)
            with col2:
                if st.form_submit_button("从来源分支创建", use_container_width=True):
                    src = source_branch if source_branch != "(空分支)" else None
                    success, msg = bm.create_branch(new_branch_name, new_branch_desc, src)
                    if success:
                        st.success(msg)
                        st.rerun()
                    else:
                        st.error(msg)
    
    # 分支操作区
    st.subheader("分支操作")
    cols = st.columns(min(3, len(branches) + 1))
    
    for i, branch in enumerate(branches):
        with cols[i]:
            is_active = branch.get("is_active", False)
            border_color = "#00D4FF" if is_active else "#6B7894"
            
            with st.container():
                st.markdown(f"""
                <div style="
                    border: 2px solid {border_color};
                    border-radius: 8px;
                    padding: 12px;
                    margin: 4px 0;
                    background: rgba(0,0,0,0.3);
                ">
                    <div style="font-weight: 700; color: {border_color};">
                        {'🟢 ' if is_active else '⚪ '}{branch['name']}
                    </div>
                    <div style="font-size: 0.85rem; color: #888; margin-top: 4px;">
                        {branch.get('description', '')}
                    </div>
                    <div style="font-size: 0.75rem; color: #6B7894; margin-top: 8px;">
                        📁 {branch.get('file_count', 0)} 文件 | 
                        📏 {_format_size(branch.get('total_size', 0))} |
                        📅 {branch.get('created_at', '')[:10]}
                    </div>
                </div>
                """, unsafe_allow_html=True)
                
                # 操作按钮
                btn_col1, btn_col2, btn_col3 = st.columns(3)
                with btn_col1:
                    if st.button("切换", key=f"switch_{branch['name']}", use_container_width=True):
                        success, msg = bm.switch_branch(branch['name'])
                        if success:
                            st.success(msg)
                            st.rerun()
                        else:
                            st.error(msg)
                with btn_col2:
                    if st.button("重命名", key=f"rename_{branch['name']}", use_container_width=True):
                        _show_rename_dialog(bm, branch['name'])
                with btn_col3:
                    if st.button("删除", key=f"delete_{branch['name']}", use_container_width=True,
                                disabled=(branch['name'] == "main")):
                        _show_delete_dialog(bm, branch['name'])


def _render_file_structure_tab(bm: BranchManager, active_branch: str):
    """渲染文件结构标签页。"""
    import logging as _ulgg
    _logger = _ulgg.getLogger(__name__)

    st.subheader(f"📁 分支文件结构: `{active_branch}`")

    # ------ 一致性状态条（DB快照 vs 文件系统）------
    try:
        from durability.database import db_get_branch_snapshot_status  # 延迟导入
        snap = db_get_branch_snapshot_status(active_branch)
        if snap and snap.get("ok"):
            by_st = snap.get("by_status", {}) or {}
            status_colors = {
                "new": ("🆕 新文件", "🟢"),
                "unchanged": ("✅ 未变", "🔵"),
                "modified": ("✏️ 修改", "🟡"),
                "deleted": ("🗑️ 已删", "🔴"),
            }
            badges = []
            total_size = snap.get("total_size", 0) or 0
            for s, (label, _icon) in status_colors.items():
                if by_st.get(s, 0):
                    badges.append(f"{by_st[s]} {label}")
            st.caption(
                f"🔗 DB一致性快照 | 已注册 {snap.get('total', 0)} 个文件, "
                f"总大小 {_format_size(total_size)} | "
                + (" | ".join(badges) if badges else "暂无快照记录（上传文件后自动写入）")
            )
    except Exception as _snap_err:
        st.caption(f"🔗 DB快照暂不可用（首次导入时自动同步）: {str(_snap_err)[:40]}")

    # ------ 文件夹管理面板 ------
    with st.expander("📂 文件夹管理（新建/删除/重命名）", expanded=False):
        folders = bm.list_folders(active_branch)
        folder_paths = [""] + [f["path"] for f in folders]
        if folders:
            st.markdown(f"**当前文件夹（共 {len(folders)} 个）**")
            df_folders = pd.DataFrame([{
                "📁 路径": f["path"], "父目录": f["parent"] or "/(根)",
                "📄 文件数": f["file_count"], "总大小": _format_size(f["size"]),
            } for f in folders])
            st.dataframe(df_folders, use_container_width=True, hide_index=True, height=160)
        else:
            st.info("暂无文件夹（导入带子路径的文件时会自动创建）")

        fm_col1, fm_col2, fm_col3 = st.columns(3)
        with fm_col1:
            st.markdown("**➕ 新建文件夹**")
            new_folder = st.text_input("新文件夹路径（可多级: raw/车212/第一批）",
                                       key="new_folder_input", placeholder="例如: raw/车212")
            if st.button("✅ 确认新建", key="create_folder_btn", use_container_width=True):
                _logger.info("[UI-文件夹创建] 点击确认: branch=%s, path=%s", active_branch, new_folder)
                ok, msg = bm.create_folder(active_branch, new_folder)
                if ok:
                    st.success(msg); st.rerun()
                else:
                    st.error(msg)

        with fm_col2:
            st.markdown("**✏️ 重命名文件夹**")
            old_folder = st.selectbox("选择旧路径", folder_paths,
                                      key="rename_folder_old",
                                      format_func=lambda x: "(根)" if x == "" else x)
            new_folder_name = st.text_input("新路径", key="rename_folder_new",
                                            placeholder="例如: raw/车212_v2")
            if st.button("✅ 确认重命名", key="rename_folder_btn",
                         use_container_width=True, disabled=(not old_folder)):
                _logger.info("[UI-文件夹重命名] 点击确认: branch=%s, old=%s, new=%s",
                             active_branch, old_folder, new_folder_name)
                ok, msg = bm.rename_folder(active_branch, old_folder, new_folder_name)
                if ok:
                    st.success(msg); st.rerun()
                else:
                    st.error(msg)

        with fm_col3:
            st.markdown("**🗑️ 删除文件夹**")
            del_folder = st.selectbox("选择要删除的文件夹", folder_paths,
                                      key="delete_folder_sel",
                                      format_func=lambda x: "(根)" if x == "" else x)
            force_folder = st.checkbox("强制删除（文件夹非空也删）", key="force_folder_del")
            if st.button("🗑️ 执行删除", key="delete_folder_btn", type="primary",
                         use_container_width=True, disabled=(not del_folder)):
                _logger.info("[UI-文件夹删除] 点击确认: branch=%s, path=%s, force=%s",
                             active_branch, del_folder, force_folder)
                ok, msg = bm.delete_folder(active_branch, del_folder, force=force_folder)
                if ok:
                    st.success(msg); st.rerun()
                else:
                    st.error(msg)

    # ------ 上传文件（支持选目标文件夹） ------
    with st.expander("📤 上传文件到分支（支持指定文件夹）", expanded=True):
        folders = bm.list_folders(active_branch)
        folder_options = [("（根目录·直接上传）", "")] + [
            (f"📁 {f['path']} ({f['file_count']}个文件)", f["path"]) for f in folders
        ]
        display_map = {label: value for label, value in folder_options}
        tgt_label = st.selectbox(
            "导入到哪个文件夹？",
            options=[label for label, _ in folder_options],
            key="upload_target_folder_sel",
            help="选择'（根目录）'直接放在分支下; 也可以先在上面面板新建 raw/车212 这样的子目录"
        )
        target_folder_norm = display_map.get(tgt_label, "")
        st.caption(f"→ 最终存放位置: `.branches/{active_branch}/{target_folder_norm or '<根>'}/`")

        uploaded_file = st.file_uploader(
            "选择要添加的文件（CSV / DOCX / XLSX 会自动解析并写入数据库）",
            accept_multiple_files=True,
            key="branch_file_upload_v2",
        )

        if uploaded_file:
            any_success = False
            for file in uploaded_file:
                content = file.read()
                # 统一用 add_file_to_folder: 会同步写 branch_file_snapshots + vehicle_data_files
                success, msg, _ = bm.add_file_to_folder(
                    active_branch,
                    file_path=file.name,
                    file_content=content,
                    target_folder=target_folder_norm,
                    upload_user=st.session_state.get("_user_hint", "ui_streamlit"),
                )
                if success:
                    any_success = True
                    st.success(f"✅ {msg}")
                else:
                    st.error(f"❌ {msg}")
            if any_success:
                st.rerun()

    # ------ 文件列表 + 操作按钮（每行：预览 / 移动 / 删除） ------
    files = bm.list_branch_files(active_branch)

    if files:
        # 统计信息
        total_size = sum(f["size"] for f in files)
        valid_count = sum(1 for f in files if f["is_valid"])
        invalid_count = sum(1 for f in files if not f["is_valid"])

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("文件总数", len(files))
        col2.metric("文件总大小", _format_size(total_size))
        col3.metric("有效文件", valid_count)
        col4.metric("损坏文件", invalid_count)

        # 按文件夹分组, 方便用户选择过滤
        all_parents = sorted({
            str(Path(f["path"]).parent).replace("\\", "/")
            for f in files
        })
        all_parents = [p for p in all_parents if p != "."]
        scope = ["📂 全部文件"] + [f"📁 {p}" for p in all_parents]
        scope_sel = st.selectbox("按文件夹筛选:", scope, key="scope_filter")
        if scope_sel != scope[0]:
            parent_need = scope_sel.replace("📁 ", "")
            files = [f for f in files
                     if str(Path(f["path"]).parent).replace("\\", "/") == parent_need]

        # 文件表格
        df = pd.DataFrame([{
            "📁 路径": f["path"],
            "📄 文件名": f["name"],
            "💾 大小": _format_size(f["size"]),
            "✅ 状态": "✅ 有效" if f["is_valid"] else "❌ 损坏",
            "🔐 哈希": f["hash"][:16] + "...",
            "📅 修改时间": f["modified"][:19]
        } for f in files])

        st.dataframe(df, use_container_width=True, hide_index=True, height=240)

        # ===== 动作面板: 预览 / 移动 / 重命名 / 删除 =====
        st.markdown("### 🛠️ 文件操作（单文件）")
        path_options = [f["path"] for f in files]
        if not path_options:
            st.info("当前筛选条件下无文件")
            return

        act_tab1, act_tab2, act_tab3, act_tab4 = st.tabs([
            "👁️ 打开预览", "➡️ 移动位置", "✏️ 重命名", "🗑️ 删除",
        ])

        # -- Tab A: 预览 --
        with act_tab1:
            prev_sel = st.selectbox("选择要预览的文件", path_options, key="preview_sel")
            if st.button("👁️ 打开预览", key="do_preview", use_container_width=True):
                _logger.info("[UI-文件预览] 点击: branch=%s, path=%s", active_branch, prev_sel)
                with st.spinner("正在读取文件..."):
                    res = bm.open_file_preview(active_branch, prev_sel, max_rows=200)
                if not res.get("ok"):
                    st.error(res.get("error") or "预览失败")
                else:
                    kind = res.get("kind", "binary")
                    size = res.get("size", 0)
                    st.info(f"📦 文件类型: **{kind.upper()}** | 大小: {_format_size(size)} | "
                            f"行数: {res.get('rows', 0)} | 列数: {res.get('cols', 0)}")
                    if res.get("kind") in ("csv", "excel"):
                        st.dataframe(res.get("dataframe"), use_container_width=True,
                                     height=400, hide_index=False)
                    elif res.get("kind") == "docx":
                        if res.get("dataframe") is not None:
                            st.markdown("#### 表格内容（取首张表前100行）")
                            st.dataframe(res.get("dataframe"), use_container_width=True, height=400)
                        if res.get("text"):
                            with st.expander(f"文档正文（前200段）", expanded=False):
                                st.text(res.get("text"))
                    elif res.get("kind") == "image":
                        try:
                            st.image(res.get("raw_path"), caption=prev_sel)
                        except Exception:
                            st.info("图片类型请下载后本地打开查看")
                    elif res.get("kind") == "text":
                        st.text_area("文本内容", value=res.get("text", ""), height=400)
                    else:
                        with open(res.get("raw_path"), "rb") as fh:
                            st.download_button("⬇️ 下载此二进制文件",
                                               data=fh.read(), file_name=Path(prev_sel).name)
                    # 也给一个"数据一键跳到上传历史"按钮
                    st.caption("提示: 如果是业务 CSV/DOCX/XLSX，已自动同步到 [📁 上传历史] Tab 查看")

        # -- Tab B: 移动（跨目录） --
        with act_tab2:
            move_src = st.selectbox("选择要移动的文件", path_options, key="move_src_sel")
            move_folder_opts = ["（根目录）"] + [f["path"] for f in folders]
            move_tgt_label = st.selectbox("移动到哪个文件夹？", move_folder_opts, key="move_tgt_folder")
            move_tgt_folder = "" if move_tgt_label == "（根目录）" else move_tgt_label
            # 目标文件名：默认不变,可改
            move_new_name = st.text_input("新文件名（不改就留空）",
                                          value=Path(move_src).name, key="move_new_name")
            if Path(move_new_name).name != move_new_name:
                st.warning("⚠️ 新文件名不能带路径，用上方'移动到哪个文件夹'选择目录")
            combined_new = (Path(move_tgt_folder) / Path(move_new_name).name).as_posix() \
                if move_tgt_folder else Path(move_new_name).name
            st.caption(f"最终目标: `.branches/{active_branch}/{combined_new}`")
            if st.button("➡️ 执行移动", key="do_move", type="primary", use_container_width=True):
                if move_src == combined_new:
                    st.info("源与目标路径相同，跳过")
                else:
                    _logger.info("[UI-文件移动] 点击确认: branch=%s, src=%s, dst=%s",
                                 active_branch, move_src, combined_new)
                    ok, msg = bm.move_file(active_branch, move_src, combined_new)
                    if ok:
                        st.success(msg); st.rerun()
                    else:
                        st.error(msg)

        # -- Tab C: 重命名文件（已存在·优化版） --
        with act_tab3:
            file_to_rename = st.selectbox(
                "选择要重命名的文件",
                options=path_options,
                key="file_rename_select_v2",
            )
            default_ext = Path(file_to_rename).suffix if file_to_rename else ""
            new_name_val = st.text_input(
                "新名字（含扩展名）",
                value=Path(file_to_rename).name if file_to_rename else "",
                key="file_rename_new_name_v2",
                help="只改文件名（保持同目录）；要换目录请用 '➡️ 移动位置' Tab"
            )
            # 拼接成相对路径（保持原目录）
            src_parent = str(Path(file_to_rename).parent).replace("\\", "/") if file_to_rename else ""
            if src_parent == ".":
                src_parent = ""
            combined_rename = (Path(src_parent) / new_name_val).as_posix() if src_parent else new_name_val
            if st.button("✅ 确认重命名", type="primary",
                         key="confirm_rename_file_btn_v2", use_container_width=True,
                         disabled=(not file_to_rename)):
                if not new_name_val or new_name_val.strip() == "":
                    st.error("请输入新文件名")
                elif Path(new_name_val).name != new_name_val.strip():
                    st.error("新名字不能包含路径，移动目录请用 '➡️ 移动位置' Tab")
                else:
                    _logger.info("[UI-文件重命名] 点击确认: branch=%s, old=%s, new=%s",
                                active_branch, file_to_rename, combined_rename)
                    success, msg = bm.rename_file(active_branch, file_to_rename, combined_rename)
                    if success:
                        st.success(msg); st.rerun()
                    else:
                        st.error(msg)

        # -- Tab D: 删除文件（已存在·加二次确认） --
        with act_tab4:
            file_to_delete = st.selectbox(
                "选择要删除的文件",
                options=path_options,
                key="file_delete_select_v2"
            )
            confirm_del = st.checkbox(
                f"⚠️ 确认删除 `{file_to_delete}`？此操作不可恢复（.branches 物理删除 + DB快照同步标记为已删除）",
                key="confirm_delete_checkbox_v2")
            if st.button("🗑️ 执行删除", type="primary", key="delete_file_btn_v2",
                         use_container_width=True, disabled=(not confirm_del or not file_to_delete)):
                _logger.info("[UI-文件删除] 点击确认删除: branch=%s, file=%s",
                            active_branch, file_to_delete)
                success, msg = bm.remove_file(active_branch, file_to_delete)
                if success:
                    st.success(msg); st.rerun()
                else:
                    st.error(msg)
    else:
        st.info("📂 此分支为空，请上传文件")


def _render_detection_tab(bm: BranchManager, active_branch: str):
    """渲染检测与校验标签页。"""
    st.subheader(f"🔍 检测与校验: `{active_branch}`")
    
    col1, col2 = st.columns(2)
    
    # 重复文件检测
    with col1:
        st.markdown("### 🔄 重复文件检测")
        if st.button("🔍 开始检测重复文件", use_container_width=True):
            with st.spinner("正在检测重复文件..."):
                result = bm.detect_duplicates(active_branch)
            
            st.success(f"检测完成! 共 {result['total_files']} 个文件, "
                      f"发现 {result['duplicate_groups']} 组重复文件, "
                      f"浪费空间 {_format_size(result['wasted_space'])}")
            
            if result["duplicates"]:
                with st.expander(f"📋 重复文件详情 (点击展开)", expanded=True):
                    for dup in result["duplicates"]:
                        st.markdown(f"""
                        <div style="
                            border: 1px solid #444;
                            border-radius: 8px;
                            padding: 12px;
                            margin: 8px 0;
                            background: rgba(0,0,0,0.2);
                        ">
                            <div style="color: #FFD93D; font-weight: 600;">
                                🔁 重复组 ({dup['count']} 个文件)
                            </div>
                            <div style="font-size: 0.85rem; color: #888; margin: 4px 0;">
                                哈希: `{dup['hash'][:32]}...`
                            </div>
                            <div style="font-size: 0.85rem; color: #888;">
                                单个大小: {_format_size(dup['size'])} | 
                                可节省: {_format_size(dup['size'] * (dup['count'] - 1))}
                            </div>
                            <div style="margin-top: 8px;">
                                {"<br>".join([f"  - {f}" for f in dup['files']])}
                            </div>
                        </div>
                        """, unsafe_allow_html=True)
            else:
                st.success("✅ 未发现重复文件!")
    
    # 文件完整性校验
    with col2:
        st.markdown("### ✅ 文件完整性校验")
        if st.button("🔍 开始校验文件完整性", use_container_width=True):
            with st.spinner("正在校验文件..."):
                result = bm.validate_branch(active_branch)
            
            if result["invalid_files"]:
                st.error(f"⚠️ 发现 {len(result['invalid_files'])} 个问题文件!")
                st.markdown("**问题详情:**")
                for inv in result["invalid_files"]:
                    st.warning(f"  - `{inv['path']}`: {inv['error']}")
            else:
                st.success(f"✅ 所有 {result['total_files']} 个文件校验通过!")
            
            with st.expander("📊 校验汇总", expanded=False):
                st.markdown(f"""
                - **文件总数:** {result['total_files']}
                - **有效文件:** {result['valid_files']} ✅
                - **问题文件:** {len(result['invalid_files'])} ❌
                - **问题列表:**
                  {result['issues'] if result['issues'] else '无'}
                """)


def _render_comparison_tab(bm: BranchManager, branches: list, active_branch: str):
    """渲染分支对比标签页。"""
    st.subheader("🔀 分支对比")
    
    if len(branches) < 2:
        st.info("需要至少 2 个分支才能进行对比。请先创建新分支。")
        return
    
    col1, col2 = st.columns(2)
    with col1:
        branch_a = st.selectbox(
            "分支 A",
            options=[b["name"] for b in branches],
            key="compare_branch_a"
        )
    with col2:
        branch_b = st.selectbox(
            "分支 B",
            options=[b["name"] for b in branches if b["name"] != branch_a],
            key="compare_branch_b"
        )
    
    if branch_a and branch_b and st.button("🔍 开始对比", type="primary", use_container_width=True):
        with st.spinner("正在对比分支..."):
            comparison = bm.compare_branches(branch_a, branch_b)
        
        summary = comparison["summary"]
        
        # 差异可视化
        col1, col2, col3, col4 = st.columns(4)
        col1.metric(f"仅在 {branch_a}", summary.get(f"only_in_{branch_a}", 0))
        col2.metric(f"仅在 {branch_b}", summary.get(f"only_in_{branch_b}", 0))
        col3.metric("内容差异", summary.get("modified", 0))
        col4.metric("完全相同", summary.get("unchanged", 0))
        
        # 详细差异
        with st.expander("📋 差异详情", expanded=False):
            if comparison["only_in_a"]:
                st.markdown(f"### 📂 仅在 `{branch_a}` 中的文件")
                for f in comparison["only_in_a"]:
                    st.markdown(f"  - `{f['path']}` ({_format_size(f['size'])})")
            
            if comparison["only_in_b"]:
                st.markdown(f"### 📂 仅在 `{branch_b}` 中的文件")
                for f in comparison["only_in_b"]:
                    st.markdown(f"  - `{f['path']}` ({_format_size(f['size'])})")
            
            if comparison["modified"]:
                st.markdown("### 🔄 内容不同的文件")
                for m in comparison["modified"]:
                    st.markdown(f"  - `{m['path']}`")
                    st.caption(f"    {branch_a}: {m['hash_a'][:16]}... ({_format_size(m['size_a'])})")
                    st.caption(f"    {branch_b}: {m['hash_b'][:16]}... ({_format_size(m['size_b'])})")


def _render_merge_tab(bm: BranchManager, branches: list, active_branch: str):
    """渲染合并与冲突标签页。"""
    st.subheader("📦 分支合并")
    
    if len(branches) < 2:
        st.info("需要至少 2 个分支才能进行合并。")
        return
    
    col1, col2 = st.columns(2)
    with col1:
        source_branch = st.selectbox(
            "📥 源分支 (要合并的)",
            options=[b["name"] for b in branches if b["name"] != active_branch],
            key="merge_source"
        )
    with col2:
        target_branch = st.selectbox(
            "📤 目标分支 (接收合并的)",
            options=[b["name"] for b in branches],
            key="merge_target"
        )
    
    strategy = st.selectbox(
        "🎯 冲突解决策略",
        options=[
            ("auto", "自动处理 (无冲突直接合并)"),
            ("keep_source", "保留源分支文件"),
            ("keep_target", "保留目标分支文件")
        ],
        format_func=lambda x: x[1] if isinstance(x, tuple) else x,
        key="merge_strategy"
    )
    if isinstance(strategy, tuple):
        strategy = strategy[0]
    
    if st.button("🔀 执行合并", type="primary", use_container_width=True):
        with st.spinner(f"正在合并 '{source_branch}' → '{target_branch}'..."):
            success, result = bm.merge_branches(source_branch, target_branch, strategy)
        
        if success:
            st.success(f"✅ 合并完成!")
        else:
            st.error(f"❌ 合并失败: {result.get('error', '未知错误')}")
        
        # 显示结果
        with st.expander("📊 合并详情", expanded=True):
            st.markdown(f"""
            ### 合并结果
            - **成功合并文件:** {result['merged_files']} 个
            - **新增文件:** {len(result.get('added_files', []))} 个
            - **更新文件:** {len(result.get('updated_files', []))} 个
            - **冲突文件:** {len(result.get('conflicts', []))} 个
            - **错误:** {len(result.get('errors', []))} 个
            """)
            
            if result.get('errors'):
                st.error("**错误详情:**")
                for err in result['errors']:
                    st.warning(f"  - {err}")
            
            if result.get('conflicts'):
                st.warning("**⚠️ 存在冲突需要解决:**")
                for i, conflict in enumerate(result['conflicts']):
                    with st.expander(f"冲突 {i+1}: {conflict['path']}", expanded=False):
                        st.markdown(f"""
                        - **源分支哈希:** `{conflict['source_hash'][:20]}...`
                        - **目标分支哈希:** `{conflict['target_hash'][:20]}...`
                        - **源分支大小:** {_format_size(conflict['source_size'])}
                        - **目标分支大小:** {_format_size(conflict['target_size'])}
                        """)
                        
                        resolution = st.radio(
                            "解决方案",
                            options=["keep_target", "keep_source"],
                            format_func=lambda x: "保留目标分支" if x == "keep_target" else "保留源分支",
                            key=f"conflict_{i}"
                        )
                        
                        if st.button("应用解决方案", key=f"resolve_{i}"):
                            r_success, r_msg = bm.resolve_conflict(
                                target_branch,
                                conflict['path'],
                                resolution
                            )
                            if r_success:
                                st.success(r_msg)
                            else:
                                st.error(r_msg)
    
    # 合并历史
    st.markdown("---")
    st.markdown("### 📜 分支历史")
    history_branch = st.selectbox(
        "查看分支历史",
        options=[b["name"] for b in branches],
        key="history_branch"
    )
    
    history = bm.get_branch_history(history_branch)
    if history:
        for record in reversed(history[-20:]):  # 最近20条
            emoji = {"create": "🆕", "commit": "📝", "merge": "🔀", 
                    "rename": "✏️", "delete": "🗑️"}.get(record["change_type"], "📌")
            st.markdown(f"""
            <div style="
                border-left: 3px solid #00D4FF;
                padding: 8px 12px;
                margin: 4px 0;
                background: rgba(0,0,0,0.2);
            ">
                <span style="color: #00D4FF;">{emoji} v{record['version']}</span>
                <span style="color: #888; margin-left: 12px;">{record['created_at'][:16]}</span>
                <div style="color: #ccc; margin-top: 4px;">{record['summary']}</div>
            </div>
            """, unsafe_allow_html=True)
    else:
        st.info("暂无历史记录")


def _show_rename_dialog(bm: BranchManager, old_name: str):
    """显示重命名对话框。"""
    new_name = st.text_input("新分支名", key=f"rename_input_{old_name}")
    if st.button("确认重命名", key=f"confirm_rename_{old_name}"):
        if new_name:
            success, msg = bm.rename_branch(old_name, new_name)
            if success:
                st.success(msg)
                st.rerun()
            else:
                st.error(msg)
        else:
            st.warning("请输入新分支名")


def _show_delete_dialog(bm: BranchManager, branch_name: str):
    """显示删除对话框。"""
    count = sum(1 for f in bm.list_branch_files(branch_name) if f["is_valid"])
    st.warning(f"⚠️ 确认删除分支 '{branch_name}'? (包含 {count} 个文件)")
    
    col1, col2 = st.columns(2)
    with col1:
        if st.button("取消", key=f"cancel_delete_{branch_name}"):
            pass
    with col2:
        if st.button("确认删除", key=f"confirm_delete_{branch_name}", type="primary"):
            success, msg = bm.delete_branch(branch_name, force=True)
            if success:
                st.success(msg)
                st.rerun()
            else:
                st.error(msg)


def _format_size(size_bytes: int) -> str:
    """格式化文件大小显示。"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


# ====================================================================
# 侧边栏专用：分支文件结构（紧凑单列，适配 ~300px 侧边栏宽）
# ====================================================================

def render_sidebar_file_structure():
    """侧边栏版的文件结构面板。

    设计原则（参考侧边栏布局安全经验）:
    1. 全部单列流式布局，不用 columns 多列或固定高度/绝对定位，避免破坏侧边栏 DOM 流
    2. 只暴露核心操作：切分支 → 新建文件夹 → 上传到指定目录 → 文件列表+快捷操作
    3. 详细编辑（文件夹重命名/文件移动预览）仍跳转完整 Tab 进行
    """
    import logging as _lgg
    _logger = _lgg.getLogger(__name__)
    bm = get_branch_manager()
    branches = bm.list_branches()
    names = [b["name"] for b in branches]
    active = bm.get_active_branch()
    if active not in names and names:
        active = names[0]

    # ---- 标题 ----
    st.markdown("""
    <div style="padding: 2px 0 6px 0; border-bottom: 1px solid rgba(0,212,255,0.12); margin-bottom: 10px;">
        <div style="font-size: 0.95rem; font-weight: 700; color: #00D4FF;">📁 分支文件结构</div>
    </div>
    """, unsafe_allow_html=True)

    # ---- 1. 分支切换 ----
    try:
        idx_cur = names.index(active)
    except ValueError:
        idx_cur = 0
    new_sel = st.selectbox("当前分支", options=names, index=idx_cur,
                           key="sidebar_branch_sel", label_visibility="visible")
    if new_sel and new_sel != active:
        ok, msg = bm.switch_branch(new_sel)
        if ok:
            st.success(f"已切换至 `{new_sel}`"); st.rerun()
        else:
            st.error(msg)
    active = new_sel or active

    # ---- 2. 新建文件夹（单行压缩） ----
    with st.expander("➕ 新建 / 管理文件夹", expanded=False):
        new_fp = st.text_input("新文件夹路径 (可多级)", key="sb_new_fp",
                               placeholder="例: raw/车212/第一批", label_visibility="collapsed")
        if st.button("✅ 新建文件夹", key="sb_mkdir_btn", use_container_width=True):
            if not new_fp:
                st.warning("路径不能为空")
            else:
                _logger.info("[侧边栏-新建文件夹] branch=%s, path=%s", active, new_fp)
                ok, msg = bm.create_folder(active, new_fp)
                if ok:
                    st.success(msg); st.rerun()
                else:
                    st.error(msg)

        st.caption("重命名/删除文件夹: 请打开「🌿 分支管理」→「📁 文件结构」")

    # ---- 3. 上传文件到指定目录 ----
    with st.expander("📤 上传文件到分支", expanded=True):
        folders = bm.list_folders(active)
        folder_opts = [("（根目录）", "")] + [(f"📁 {f['path']}", f["path"]) for f in folders]
        display_map = {label: value for label, value in folder_opts}
        tgt_lbl = st.selectbox("目标文件夹", options=[x[0] for x in folder_opts],
                               key="sb_upload_target", label_visibility="visible")
        target_folder_norm = display_map.get(tgt_lbl, "")
        st.caption(f"位置: `.branches/{active}/{target_folder_norm or '<根>'}`")

        uploaded = st.file_uploader(
            "选择文件",
            accept_multiple_files=True,
            key="sb_upload_files",
            label_visibility="collapsed",
        )
        if uploaded:
            any_ok = False
            for f in uploaded:
                ok, msg, _h = bm.add_file_to_folder(
                    active,
                    file_path=f.name,
                    file_content=f.read(),
                    target_folder=target_folder_norm,
                    upload_user="sidebar_ui",
                )
                if ok:
                    any_ok = True
                    st.success(f"✅ {Path(f.name).name}")
                else:
                    st.error(f"❌ {Path(f.name).name}: {msg}")
            if any_ok:
                st.rerun()

    # ---- 4. 文件列表（紧凑） ----
    files = bm.list_branch_files(active)
    total_kb = sum(f["size"] for f in files) // 1024
    st.caption(f"共 {len(files)} 个文件 · 约 {total_kb} KB")

    if files:
        # 按目录过滤
        parents = sorted({
            str(Path(f["path"]).parent).replace("\\", "/")
            for f in files
        })
        parents = [p for p in parents if p != "."]
        filter_options = ["📂 全部"] + [f"📁 {p}" for p in parents]
        sel_scope = st.selectbox("按目录筛选", options=filter_options,
                                 key="sb_file_scope", label_visibility="visible")
        if sel_scope != "📂 全部":
            need = sel_scope.replace("📁 ", "")
            files = [f for f in files
                     if str(Path(f["path"]).parent).replace("\\", "/") == need]

        # 预览文件总数截断，防止侧边栏过长 (最多 40 条)
        truncated = files[:40]
        if len(files) > len(truncated):
            st.caption(f"仅显示前 {len(truncated)} 个，完整列表请到「🌿 分支管理」Tab")

        for f in truncated:
            path = f["path"]
            size_s = _format_size(f["size"])
            valid_s = "✅" if f["is_valid"] else "⚠️"
            with st.container():
                r1, r2 = st.columns([4, 1])
                with r1:
                    st.markdown(
                        f"<div style='font-size:0.78rem; word-break:break-all;'>"
                        f"{valid_s} {path}</div>"
                        f"<div style='font-size:0.68rem;color:#6B7894;'>{size_s} · "
                        f"修改 {f['modified'][:16].replace('T',' ')}</div>",
                        unsafe_allow_html=True,
                    )
                with r2:
                    op = st.selectbox(
                        "操作", ["", "👁️预览", "✏️重命名", "🗑️删除"],
                        key=f"sb_op_{f['hash'][:12]}",
                        label_visibility="collapsed",
                    )
                if op == "👁️预览":
                    _logger.info("[侧边栏-预览] branch=%s, path=%s", active, path)
                    with st.spinner("读取中..."):
                        prev = bm.open_file_preview(active, path, max_rows=50)
                    if not prev.get("ok"):
                        st.error(prev.get("error") or "预览失败")
                    else:
                        kind = prev.get("kind", "")
                        with st.expander(f"预览 · {Path(path).name} ({kind.upper()})", expanded=False):
                            if prev.get("dataframe") is not None:
                                st.dataframe(prev["dataframe"], use_container_width=True, height=220)
                            elif prev.get("kind") == "image":
                                try:
                                    st.image(prev.get("raw_path"))
                                except Exception:
                                    st.info("图片类型需本地打开")
                            elif prev.get("text"):
                                st.text(prev["text"][:3000])
                            else:
                                try:
                                    with open(prev.get("raw_path"), "rb") as fh:
                                        st.download_button("⬇️ 下载", data=fh.read(),
                                                           file_name=Path(path).name,
                                                           key=f"sb_dl_{f['hash'][:10]}")
                                except Exception as dl_e:
                                    st.error(str(dl_e))
                elif op == "✏️重命名":
                    cur_parent = str(Path(path).parent).replace("\\", "/")
                    if cur_parent == ".":
                        cur_parent = ""
                    new_name = st.text_input("新文件名(带扩展名)", value=Path(path).name,
                                             key=f"sb_rn_{f['hash'][:12]}")
                    if st.button("✅ 确认重命名", key=f"sb_rnbtn_{f['hash'][:12]}",
                                 use_container_width=True):
                        combined = (Path(cur_parent) / new_name).as_posix() if cur_parent else new_name
                        if Path(new_name).name != new_name:
                            st.error("不能包含路径")
                        else:
                            _logger.info("[侧边栏-重命名] branch=%s, old=%s, new=%s",
                                         active, path, combined)
                            ok, msg = bm.rename_file(active, path, combined)
                            if ok:
                                st.success(msg); st.rerun()
                            else:
                                st.error(msg)
                elif op == "🗑️删除":
                    confirm = st.checkbox(f"确认删除 `{Path(path).name}`?",
                                          key=f"sb_delchk_{f['hash'][:12]}")
                    if st.button("🗑️ 执行删除", key=f"sb_delbtn_{f['hash'][:12]}",
                                 type="primary", use_container_width=True, disabled=not confirm):
                        _logger.info("[侧边栏-删除] branch=%s, path=%s", active, path)
                        ok, msg = bm.remove_file(active, path)
                        if ok:
                            st.success(msg); st.rerun()
                        else:
                            st.error(msg)
                st.divider() if False else None  # placeholder (保持代码可扩展)
    else:
        st.info("📭 分支为空，先上传文件。")

    st.caption("更多功能(文件夹重命名/移动文件/一致性校验): 打开 Tab → 🌿 分支管理 → 📁 文件结构")

