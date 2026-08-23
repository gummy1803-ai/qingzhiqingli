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
    st.subheader(f"📁 分支文件结构: `{active_branch}`")
    
    # 添加文件区
    with st.expander("📤 上传文件到分支", expanded=False):
        uploaded_file = st.file_uploader(
            "选择要添加的文件",
            accept_multiple_files=True,
            key="branch_file_upload"
        )
        
        if uploaded_file:
            for file in uploaded_file:
                content = file.read()
                success, msg, _ = bm.add_file(
                    active_branch,
                    file.name,
                    content
                )
                if success:
                    st.success(f"✅ {msg}")
                else:
                    st.error(f"❌ {msg}")
            st.rerun()
    
    # 文件列表
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
        
        # 文件表格
        df = pd.DataFrame([{
            "📁 路径": f["path"],
            "📄 文件名": f["name"],
            "💾 大小": _format_size(f["size"]),
            "✅ 状态": "✅ 有效" if f["is_valid"] else "❌ 损坏",
            "🔐 哈希": f["hash"][:16] + "...",
            "📅 修改时间": f["modified"][:19]
        } for f in files])
        
        st.dataframe(df, use_container_width=True, hide_index=True, height=300)
        
        # 文件操作
        with st.expander("✏️ 重命名文件", expanded=False):
            file_to_rename = st.selectbox(
                "选择要重命名的文件",
                options=[f["path"] for f in files],
                key="file_rename_select",
                help="支持修改文件名，或用 'subdir/newname.ext' 移动到同级子目录"
            )
            default_ext = Path(file_to_rename).suffix if file_to_rename else ""
            default_stem = Path(file_to_rename).stem if file_to_rename else ""
            new_name_val = st.text_input(
                "新名字（含扩展名）",
                value=file_to_rename if file_to_rename else "",
                key="file_rename_new_name",
                placeholder=f"例如: 新文件名{default_ext}  或  subdir/新文件名{default_ext}"
            )
            col_a, col_b = st.columns([1, 1])
            with col_a:
                if st.button("✅ 确认重命名", type="primary", key="confirm_rename_file_btn", use_container_width=True):
                    if not new_name_val or new_name_val.strip() == "":
                        st.error("请输入新文件名")
                    else:
                        import logging as _lgg
                        _ulgg = _lgg.getLogger(__name__)
                        _ulgg.info("[UI-文件重命名] 用户点击确认: branch=%s, old=%s, new=%s",
                                    active_branch, file_to_rename, new_name_val)
                        success, msg = bm.rename_file(active_branch, file_to_rename, new_name_val.strip())
                        if success:
                            st.success(msg)
                            st.rerun()
                        else:
                            st.error(msg)
            with col_b:
                if st.button("↩️ 还原成原名", key="reset_rename_name_btn", use_container_width=True):
                    st.info(f"已还原为: `{file_to_rename}`")
                    st.rerun()

        with st.expander("🗑️ 删除文件", expanded=False):
            file_to_delete = st.selectbox(
                "选择要删除的文件",
                options=[f["path"] for f in files],
                key="file_delete_select"
            )
            col_c, col_d = st.columns([1, 1])
            with col_c:
                confirm_del = st.checkbox(f"⚠️ 确认删除 `{file_to_delete}`？此操作不可恢复", key="confirm_delete_checkbox")
            with col_d:
                if st.button("🗑️ 执行删除", type="primary", key="delete_file_btn",
                             use_container_width=True, disabled=not confirm_del):
                    import logging as _lgg2
                    _ulgg2 = _lgg2.getLogger(__name__)
                    _ulgg2.info("[UI-文件删除] 用户点击确认删除: branch=%s, file=%s",
                                active_branch, file_to_delete)
                    success, msg = bm.remove_file(active_branch, file_to_delete)
                    if success:
                        st.success(msg)
                        st.rerun()
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
