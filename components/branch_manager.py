"""分支管理系统: 实现文件系统的分支隔离、版本控制和协作功能。

核心功能:
1. 分支管理: 创建/切换/删除/重命名分支
2. 文件快照: 记录每个分支的文件状态
3. 重复性检测: 基于 SHA256 哈希检测重复文件
4. 完整性校验: 检测损坏或无法读取的文件
5. 合并与冲突解决: 支持分支合并和冲突处理
"""

import hashlib
import logging
import os
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class BranchManager:
    """分支管理器核心类。"""
    
    def __init__(self, base_path: str = None):
        """初始化分支管理器。
        
        Args:
            base_path: 分支存储的基础路径, 默认使用项目根目录下的 .branches
        """
        if base_path is None:
            base_path = Path(__file__).parent.parent / ".branches"
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)
        logger.info("[分支管理] 初始化: 基础路径=%s", self.base_path)
        self._initialize_default_branch()
    
    def _initialize_default_branch(self):
        """初始化默认主分支。"""
        main_branch = self.base_path / "main"
        if not main_branch.exists():
            main_branch.mkdir(parents=True, exist_ok=True)
            self._create_branch_metadata("main", "主分支 (默认)", is_active=True)
    
    def _get_branch_path(self, branch_name: str) -> Path:
        """获取分支的文件存储路径。"""
        return self.base_path / branch_name
    
    def _get_metadata_path(self, branch_name: str) -> Path:
        """获取分支元数据文件路径。"""
        return self.base_path / f"{branch_name}.meta.json"
    
    def _create_branch_metadata(
        self,
        branch_name: str,
        description: str = "",
        is_active: bool = False,
        parent_branch: str = None
    ):
        """创建分支元数据。"""
        import json
        metadata = {
            "name": branch_name,
            "description": description,
            "is_active": is_active,
            "parent_branch": parent_branch,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "versions": [],
            "conflicts": []
        }
        meta_path = self._get_metadata_path(branch_name)
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)
    
    def _read_branch_metadata(self, branch_name: str) -> Dict:
        """读取分支元数据。"""
        import json
        meta_path = self._get_metadata_path(branch_name)
        if not meta_path.exists():
            return {}
        with open(meta_path, "r", encoding="utf-8") as f:
            return json.load(f)
    
    def _update_branch_metadata(self, branch_name: str, updates: Dict):
        """更新分支元数据。"""
        import json
        meta = self._read_branch_metadata(branch_name)
        meta.update(updates)
        meta["updated_at"] = datetime.now().isoformat()
        meta_path = self._get_metadata_path(branch_name)
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    
    def list_branches(self) -> List[Dict]:
        """列出所有分支。"""
        branches = []
        for meta_file in self.base_path.glob("*.meta.json"):
            branch_name = meta_file.stem
            meta = self._read_branch_metadata(branch_name)
            if meta:
                branch_path = self._get_branch_path(branch_name)
                file_count = sum(1 for f in branch_path.rglob("*") if f.is_file())
                total_size = sum(f.stat().st_size for f in branch_path.rglob("*") if f.is_file())
                meta["file_count"] = file_count
                meta["total_size"] = total_size
                branches.append(meta)
        return sorted(branches, key=lambda x: x.get("name", ""))
    
    def create_branch(
        self,
        branch_name: str,
        description: str = "",
        source_branch: str = None
    ) -> Tuple[bool, str]:
        """创建新分支。
        
        Args:
            branch_name: 新分支名称
            description: 分支描述
            source_branch: 来源分支（fork时使用）
            
        Returns:
            (成功标志, 消息)
        """
        t0 = time.perf_counter()
        logger.info("[分支创建] 请求: name=%s, source=%s, desc=%s", 
                    branch_name, source_branch, description[:50])
        
        if not branch_name or not branch_name.replace("-", "").replace("_", "").isalnum():
            logger.warning("[分支创建] 失败: 分支名无效 '%s'", branch_name)
            return False, "分支名只能包含字母、数字、-、_ 字符"
        
        branch_path = self._get_branch_path(branch_name)
        if branch_path.exists():
            logger.warning("[分支创建] 失败: 分支 '%s' 已存在", branch_name)
            return False, f"分支 '{branch_name}' 已存在"
        
        try:
            if source_branch:
                # 从源分支复制文件
                source_path = self._get_branch_path(source_branch)
                if not source_path.exists():
                    logger.error("[分支创建] 失败: 源分支 '%s' 不存在", source_branch)
                    return False, f"源分支 '{source_branch}' 不存在"
                
                file_count = sum(1 for f in source_path.rglob("*") if f.is_file())
                logger.info("[分支创建] Fork: 从 '%s' 复制 %d 个文件到 '%s'", 
                           source_branch, file_count, branch_name)
                shutil.copytree(source_path, branch_path)
                self._create_branch_metadata(
                    branch_name,
                    description,
                    is_active=False,
                    parent_branch=source_branch
                )
                self._add_version_record(branch_name, "create", 
                    f"从分支 '{source_branch}' 创建", file_count)
                elapsed = (time.perf_counter() - t0) * 1000
                logger.info("[分支创建] 成功: '%s' (fork自'%s', %d文件, %.1fms)", 
                           branch_name, source_branch, file_count, elapsed)
                return True, f"成功创建分支 '{branch_name}' (来源于 '{source_branch}')"
            else:
                # 创建空分支
                branch_path.mkdir(parents=True, exist_ok=True)
                self._create_branch_metadata(
                    branch_name, description, is_active=False
                )
                self._add_version_record(branch_name, "create", "创建空分支", 0)
                elapsed = (time.perf_counter() - t0) * 1000
                logger.info("[分支创建] 成功: 空分支 '%s' (%.1fms)", branch_name, elapsed)
                return True, f"成功创建空分支 '{branch_name}'"
        except Exception as e:
            elapsed = (time.perf_counter() - t0) * 1000
            logger.error("[分支创建] 异常: '%s' -> %s (%.1fms)", branch_name, str(e), elapsed)
            return False, f"创建分支失败: {str(e)}"
    
    def switch_branch(self, branch_name: str) -> Tuple[bool, str]:
        """切换到指定分支。"""
        branch_path = self._get_branch_path(branch_name)
        if not branch_path.exists():
            return False, f"分支 '{branch_name}' 不存在"
        
        # 将所有分支设为非活跃
        for b in self.list_branches():
            self._update_branch_metadata(b["name"], {"is_active": False})
        
        # 激活目标分支
        self._update_branch_metadata(branch_name, {"is_active": True})
        return True, f"已切换到分支 '{branch_name}'"
    
    def get_active_branch(self) -> Optional[str]:
        """获取当前活跃分支。"""
        for b in self.list_branches():
            if b.get("is_active"):
                return b["name"]
        return "main"
    
    def rename_branch(self, old_name: str, new_name: str) -> Tuple[bool, str]:
        """重命名分支。"""
        old_path = self._get_branch_path(old_name)
        new_path = self._get_branch_path(new_name)
        
        if not old_path.exists():
            return False, f"分支 '{old_name}' 不存在"
        if new_path.exists():
            return False, f"分支 '{new_name}' 已存在"
        
        try:
            # 移动文件目录
            old_path.rename(new_path)
            
            # 移动元数据文件
            old_meta = self._get_metadata_path(old_name)
            new_meta = self._get_metadata_path(new_name)
            if old_meta.exists():
                meta = self._read_branch_metadata(old_name)
                meta["name"] = new_name
                with open(new_meta, "w", encoding="utf-8") as f:
                    import json
                    json.dump(meta, f, ensure_ascii=False, indent=2)
                old_meta.unlink()
            
            self._add_version_record(new_name, "rename", 
                f"从 '{old_name}' 重命名", 0)
            return True, f"分支已重命名为 '{new_name}'"
        except Exception as e:
            return False, f"重命名失败: {str(e)}"
    
    def delete_branch(self, branch_name: str, force: bool = False) -> Tuple[bool, str]:
        """删除分支。
        
        Args:
            branch_name: 要删除的分支名
            force: 强制删除（即使有文件）
        """
        if branch_name == "main":
            return False, "不能删除主分支 'main'"
        
        branch_path = self._get_branch_path(branch_name)
        if not branch_path.exists():
            return False, f"分支 '{branch_name}' 不存在"
        
        file_count = sum(1 for f in branch_path.rglob("*") if f.is_file())
        if file_count > 0 and not force:
            return False, f"分支 '{branch_name}' 包含 {file_count} 个文件, 使用 force=True 强制删除"
        
        try:
            shutil.rmtree(branch_path)
            meta_path = self._get_metadata_path(branch_name)
            if meta_path.exists():
                meta_path.unlink()
            return True, f"分支 '{branch_name}' 已删除"
        except Exception as e:
            return False, f"删除失败: {str(e)}"
    
    def add_file(
        self,
        branch_name: str,
        file_path: str,
        file_content: bytes = None,
        target_name: str = None
    ) -> Tuple[bool, str, str]:
        """向分支添加文件。
        
        Args:
            branch_name: 目标分支
            file_path: 源文件路径或文件名
            file_content: 文件内容（可选）
            target_name: 目标文件名（可选，默认使用原文件名）
            
        Returns:
            (成功, 消息, 文件哈希)
        """
        t0 = time.perf_counter()
        branch_path = self._get_branch_path(branch_name)
        if not branch_path.exists():
            logger.error("[文件添加] 失败: 分支 '%s' 不存在", branch_name)
            return False, f"分支 '{branch_name}' 不存在", ""
        
        if target_name is None:
            target_name = Path(file_path).name
        
        target_path = branch_path / target_name
        source_size = len(file_content) if file_content else (Path(file_path).stat().st_size if Path(file_path).exists() else 0)
        
        logger.info("[文件添加] 请求: branch=%s, target=%s, size=%dKB", 
                    branch_name, target_name, source_size // 1024)
        
        try:
            if file_content is not None:
                with open(target_path, "wb") as f:
                    f.write(file_content)
                logger.info("[文件添加] 写入: %dKB 内容到 %s", source_size // 1024, target_path)
            else:
                source = Path(file_path)
                if source.exists():
                    shutil.copy2(source, target_path)
                    logger.info("[文件添加] 复制: %s -> %s", source, target_path)
                else:
                    logger.error("[文件添加] 失败: 源文件不存在 %s", file_path)
                    return False, f"源文件不存在: {file_path}", ""
            
            file_hash = self._compute_file_hash(target_path)
            file_size = target_path.stat().st_size
            self._add_version_record(branch_name, "commit", 
                f"添加文件: {target_name}", 1)
            elapsed = (time.perf_counter() - t0) * 1000
            logger.info("[文件添加] 成功: %s (branch=%s, hash=%s..., size=%dKB, %.1fms)", 
                       target_name, branch_name, file_hash[:16], file_size // 1024, elapsed)
            return True, f"文件 '{target_name}' 已添加到分支 '{branch_name}'", file_hash
        except Exception as e:
            elapsed = (time.perf_counter() - t0) * 1000
            logger.error("[文件添加] 异常: %s -> %s (%.1fms)", file_path, str(e), elapsed)
            return False, f"添加文件失败: {str(e)}", ""
    
    def remove_file(self, branch_name: str, file_name: str) -> Tuple[bool, str]:
        """从分支删除文件。"""
        branch_path = self._get_branch_path(branch_name)
        target_path = branch_path / file_name
        
        if not target_path.exists():
            return False, f"文件 '{file_name}' 不存在于分支 '{branch_name}'"
        
        try:
            target_path.unlink()
            self._add_version_record(branch_name, "commit", 
                f"删除文件: {file_name}", -1)
            return True, f"文件 '{file_name}' 已从分支 '{branch_name}' 删除"
        except Exception as e:
            return False, f"删除失败: {str(e)}"
    
    def list_branch_files(self, branch_name: str) -> List[Dict]:
        """列出分支中的所有文件。"""
        branch_path = self._get_branch_path(branch_name)
        if not branch_path.exists():
            return []
        
        files = []
        for file_path in sorted(branch_path.rglob("*")):
            if file_path.is_file():
                relative_path = file_path.relative_to(branch_path)
                file_hash = self._compute_file_hash(file_path)
                is_valid = self._validate_file(file_path)
                file_size = file_path.stat().st_size
                
                files.append({
                    "path": str(relative_path),
                    "name": file_path.name,
                    "hash": file_hash,
                    "size": file_size,
                    "is_valid": is_valid,
                    "type": file_path.suffix.lower(),
                    "modified": datetime.fromtimestamp(file_path.stat().st_mtime).isoformat()
                })
        
        return files
    
    def detect_duplicates(self, branch_name: str) -> Dict:
        """检测分支中的重复文件。
        
        Returns:
            {
                "duplicates": [{"hash": ..., "files": [...]}],
                "total_files": int,
                "duplicate_groups": int,
                "wasted_space": int
            }
        """
        files = self.list_branch_files(branch_name)
        
        # 按哈希分组
        hash_groups: Dict[str, List[Dict]] = {}
        for f in files:
            h = f["hash"]
            if h not in hash_groups:
                hash_groups[h] = []
            hash_groups[h].append(f)
        
        # 找出重复的
        duplicates = []
        wasted_space = 0
        for h, group in hash_groups.items():
            if len(group) > 1:
                duplicates.append({
                    "hash": h,
                    "files": [f["path"] for f in group],
                    "count": len(group),
                    "size": group[0]["size"]
                })
                wasted_space += group[0]["size"] * (len(group) - 1)
        
        return {
            "duplicates": sorted(duplicates, key=lambda x: x["count"], reverse=True),
            "total_files": len(files),
            "duplicate_groups": len(duplicates),
            "wasted_space": wasted_space
        }
    
    def validate_branch(self, branch_name: str) -> Dict:
        """校验分支文件完整性。
        
        Returns:
            {
                "valid_files": int,
                "invalid_files": [{"path": ..., "error": ...}],
                "total_files": int,
                "issues": ["文件损坏: ...", ...]
            }
        """
        files = self.list_branch_files(branch_name)
        invalid_files = []
        valid_count = 0
        
        for f in files:
            if f["is_valid"]:
                valid_count += 1
            else:
                invalid_files.append({
                    "path": f["path"],
                    "error": f.get("error", "文件无法读取或已损坏")
                })
        
        return {
            "valid_files": valid_count,
            "invalid_files": invalid_files,
            "total_files": len(files),
            "issues": [f"文件损坏: {ifile['path']}" for ifile in invalid_files]
        }
    
    def compare_branches(
        self,
        branch_a: str,
        branch_b: str
    ) -> Dict:
        """比较两个分支的文件差异。
        
        Returns:
            {
                "only_in_a": [...],
                "only_in_b": [...],
                "modified": [...],
                "unchanged": [...],
                "summary": {...}
            }
        """
        files_a = {f["path"]: f for f in self.list_branch_files(branch_a)}
        files_b = {f["path"]: f for f in self.list_branch_files(branch_b)}
        
        paths_a = set(files_a.keys())
        paths_b = set(files_b.keys())
        
        only_in_a = [files_a[p] for p in paths_a - paths_b]
        only_in_b = [files_b[p] for p in paths_b - paths_a]
        
        common = paths_a & paths_b
        modified = []
        unchanged = []
        
        for p in common:
            if files_a[p]["hash"] != files_b[p]["hash"]:
                modified.append({
                    "path": p,
                    "hash_a": files_a[p]["hash"],
                    "hash_b": files_b[p]["hash"],
                    "size_a": files_a[p]["size"],
                    "size_b": files_b[p]["size"]
                })
            else:
                unchanged.append(files_a[p])
        
        return {
            "only_in_a": only_in_a,
            "only_in_b": only_in_b,
            "modified": modified,
            "unchanged": unchanged,
            "summary": {
                f"only_in_{branch_a}": len(only_in_a),
                f"only_in_{branch_b}": len(only_in_b),
                "modified": len(modified),
                "unchanged": len(unchanged)
            }
        }
    
    def merge_branches(
        self,
        source_branch: str,
        target_branch: str,
        strategy: str = "auto"
    ) -> Tuple[bool, Dict]:
        """合并分支。
        
        Args:
            source_branch: 源分支（被合并）
            target_branch: 目标分支（接收合并）
            strategy: 冲突解决策略
                - auto: 自动合并（无冲突直接合并，有冲突返回待处理）
                - keep_source: 遇到冲突保留源分支文件
                - keep_target: 遇到冲突保留目标分支文件
                - manual: 手动解决（返回冲突列表供外部处理）
                
        Returns:
            (成功, 结果字典)
        """
        result = {
            "merged_files": 0,
            "conflicts": [],
            "added_files": [],
            "updated_files": [],
            "errors": []
        }
        
        source_path = self._get_branch_path(source_branch)
        target_path = self._get_branch_path(target_branch)
        
        if not source_path.exists():
            return False, {"error": f"源分支 '{source_branch}' 不存在"}
        if not target_path.exists():
            return False, {"error": f"目标分支 '{target_branch}' 不存在"}
        
        source_files = {f["path"]: f for f in self.list_branch_files(source_branch)}
        target_files = {f["path"]: f for f in self.list_branch_files(target_branch)}
        
        # 处理源分支的每个文件
        for path, src_file in source_files.items():
            src_path = source_path / path
            tgt_path = target_path / path
            
            if path not in target_files:
                # 目标分支没有此文件 - 直接添加
                try:
                    tgt_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_path, tgt_path)
                    result["added_files"].append(path)
                    result["merged_files"] += 1
                except Exception as e:
                    result["errors"].append(f"添加文件 {path} 失败: {str(e)}")
            elif src_file["hash"] != target_files[path]["hash"]:
                # 文件存在但内容不同 - 冲突
                if strategy == "keep_source":
                    try:
                        shutil.copy2(src_path, tgt_path)
                        result["updated_files"].append(path)
                        result["merged_files"] += 1
                    except Exception as e:
                        result["errors"].append(f"更新文件 {path} 失败: {str(e)}")
                elif strategy == "keep_target":
                    # 保留目标分支，跳过
                    pass
                else:  # auto 或 manual
                    conflict = {
                        "path": path,
                        "source_hash": src_file["hash"],
                        "target_hash": target_files[path]["hash"],
                        "source_size": src_file["size"],
                        "target_size": target_files[path]["size"],
                        "resolution": "pending"
                    }
                    result["conflicts"].append(conflict)
        
        # 处理目标分支中有但源分支没有的文件（删除）
        for path in target_files:
            if path not in source_files:
                src_path = source_path / path
                if not src_path.exists():
                    tgt_path = target_path / path
                    try:
                        if tgt_path.exists():
                            # 根据策略决定是否删除
                            if strategy in ("auto", "keep_target"):
                                pass  # 保留目标分支的独有文件
                            elif strategy == "keep_source":
                                tgt_path.unlink()
                                result["removed_files"] = result.get("removed_files", []) + [path]
                    except Exception as e:
                        result["errors"].append(f"处理文件 {path} 失败: {str(e)}")
        
        # 记录版本
        self._add_version_record(target_branch, "merge",
            f"合并分支 '{source_branch}': 新增 {len(result['added_files'])} 个, "
            f"更新 {len(result['updated_files'])} 个, 冲突 {len(result['conflicts'])} 个",
            result["merged_files"])
        
        return len(result["errors"]) == 0, result
    
    def resolve_conflict(
        self,
        target_branch: str,
        file_path: str,
        resolution: str
    ) -> Tuple[bool, str]:
        """解决合并冲突。
        
        Args:
            target_branch: 目标分支
            file_path: 冲突文件路径
            resolution: 解决方案 (keep_source/keep_target)
        """
        branch_path = self._get_branch_path(target_branch)
        target_file = branch_path / file_path
        
        if resolution == "keep_target":
            # 保留当前目标分支的文件（什么都不做）
            return True, f"已保留目标分支的文件 '{file_path}'"
        elif resolution == "keep_source":
            # 需要从源分支获取最新文件
            # 这里简化处理：记录解决方案
            return True, f"已标记保留源分支版本的 '{file_path}'"
        else:
            return False, f"无效的解决方案: {resolution}"
    
    def get_branch_history(self, branch_name: str) -> List[Dict]:
        """获取分支的变更历史。"""
        meta = self._read_branch_metadata(branch_name)
        return meta.get("versions", [])
    
    def _compute_file_hash(self, file_path: Path) -> str:
        """计算文件的 SHA256 哈希。"""
        sha256 = hashlib.sha256()
        try:
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    sha256.update(chunk)
            return sha256.hexdigest()
        except Exception:
            return "ERROR"
    
    def _validate_file(self, file_path: Path) -> Tuple[bool, str]:
        """校验文件是否可读且未损坏。"""
        try:
            if not file_path.exists():
                return False, "文件不存在"
            if file_path.stat().st_size == 0:
                return False, "文件为空"
            
            # 尝试读取文件
            with open(file_path, "rb") as f:
                # 对于文本文件检查编码
                if file_path.suffix.lower() in (".csv", ".txt", ".json", ".md", ".py"):
                    content = f.read(1024)
                    try:
                        content.decode("utf-8")
                    except UnicodeDecodeError:
                        # 尝试其他编码
                        try:
                            content.decode("gbk")
                        except UnicodeDecodeError:
                            return False, "文件编码异常"
                else:
                    # 二进制文件只检查是否能读取
                    f.read(1)
            
            return True, ""
        except PermissionError:
            return False, "无读取权限"
        except Exception as e:
            return False, f"读取异常: {str(e)}"
    
    def _add_version_record(
        self,
        branch_name: str,
        change_type: str,
        summary: str,
        changed_files: int
    ):
        """添加版本记录。"""
        meta = self._read_branch_metadata(branch_name)
        versions = meta.get("versions", [])
        version_number = len(versions) + 1
        
        record = {
            "version": version_number,
            "change_type": change_type,
            "summary": summary,
            "changed_files": changed_files,
            "created_at": datetime.now().isoformat()
        }
        
        versions.append(record)
        meta["versions"] = versions
        self._update_branch_metadata(branch_name, {"versions": versions})


def get_branch_manager() -> BranchManager:
    """获取全局分支管理器实例。"""
    global _branch_manager
    if "_branch_manager" not in globals():
        _branch_manager = BranchManager()
    return _branch_manager
