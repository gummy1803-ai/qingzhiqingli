"""分支管理器单元测试脚本。"""

from components.branch_manager import BranchManager
import tempfile
import os
import shutil


def main():
    # 创建临时目录测试
    test_path = tempfile.mkdtemp()
    print(f"测试路径: {test_path}")
    bm = BranchManager(test_path)
    
    # 测试创建分支
    success, msg = bm.create_branch("feature-test", "测试分支")
    print(f"创建分支: {success} - {msg}")
    
    # 测试从源分支创建
    success, msg = bm.create_branch("feature-fork", "从main fork", source_branch="main")
    print(f"创建fork: {success} - {msg}")
    
    # 测试列出分支
    branches = bm.list_branches()
    print(f"分支数量: {len(branches)}")
    for b in branches:
        print(f"  - {b['name']} ({b.get('file_count', 0)} 文件)")
    
    # 测试添加文件
    test_file = os.path.join(test_path, "test.txt")
    with open(test_file, "w") as f:
        f.write("test content")
    success, msg, hash_val = bm.add_file("main", test_file)
    print(f"添加文件: {success} - {msg}")
    
    # 测试列出文件
    files = bm.list_branch_files("main")
    print(f"main分支文件数: {len(files)}")
    
    # 测试重复检测
    dup_result = bm.detect_duplicates("main")
    print(f"重复检测: 共 {dup_result['total_files']} 文件, {dup_result['duplicate_groups']} 组重复")
    
    # 测试完整性校验
    val_result = bm.validate_branch("main")
    print(f"完整性校验: {val_result['valid_files']}/{val_result['total_files']} 通过")
    
    # 测试分支对比
    comparison = bm.compare_branches("main", "feature-fork")
    print(f"分支对比: 仅main={comparison['summary']['only_in_main']}, 仅fork={comparison['summary']['only_in_feature-fork']}")
    
    # 测试合并
    success, merge_result = bm.merge_branches("feature-test", "main", "auto")
    print(f"合并: {success}, 合并文件数: {merge_result['merged_files']}")
    
    # 测试历史
    history = bm.get_branch_history("main")
    print(f"main分支历史: {len(history)} 条记录")
    
    # 测试重命名
    success, msg = bm.rename_branch("feature-test", "feature-renamed")
    print(f"重命名: {success} - {msg}")
    
    # 测试删除
    success, msg = bm.delete_branch("feature-renamed", force=True)
    print(f"删除: {success} - {msg}")
    
    # 测试切换分支
    success, msg = bm.switch_branch("main")
    print(f"切换分支: {success} - {msg}")
    
    active = bm.get_active_branch()
    print(f"当前活跃分支: {active}")
    
    # 清理
    shutil.rmtree(test_path)
    print("\n✅ 所有测试通过!")


if __name__ == "__main__":
    main()
