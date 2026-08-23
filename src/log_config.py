"""日志配置:统一管理全应用日志输出。

- 控制台输出 INFO 及以上级别
- 格式含时间 / 模块 / 级别 / 消息
- 通过 setup_logging() 在 app 入口调用一次即可
- 各模块只需 `import logging; logger = logging.getLogger(__name__)`
- 强制 UTF-8 编码,避免 Windows 控制台中文乱码

=== Windows 日志查看指南 ===
1. 实时查看控制台: 直接运行程序即可,已自动修复编码
2. 查看日志文件:
   - PowerShell: Get-Content logs/e2e_run.log -Encoding UTF8
   - CMD:        type logs/e2e_run.log  (需要 chcp 65001 先切编码)
   - Python:     python -c "print(open('logs/e2e_run.log', encoding='utf-8').read())"
"""
from __future__ import annotations

import io
import logging
import os
import sys
from pathlib import Path

_DEFAULT_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DEFAULT_DATEFMT = "%Y-%m-%d %H:%M:%S"
_configured = False
_encoding_fixed = False


def _force_utf8_console() -> tuple[bool, str]:
    """在 Windows 控制台强制 UTF-8 编码,避免中文乱码。

    Returns:
        (success, message): 编码修复是否成功,以及说明信息
    """
    global _encoding_fixed

    if sys.platform != "win32":
        _encoding_fixed = True
        return True, "非Windows平台,跳过编码修复"

    results = []

    # 方法1: 设置环境变量 (在Python 3.7+生效)
    try:
        os.environ["PYTHONIOENCODING"] = "utf-8"
        results.append("环境变量 PYTHONIOENCODING=utf-8 已设置")
    except Exception as e:
        results.append(f"环境变量设置失败: {e}")

    # 方法2: 调用 Windows API 切换控制台代码页
    api_ok = False
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32

        # SetConsoleOutputCP - 设置输出代码页
        result_out = kernel32.SetConsoleOutputCP(65001)
        # SetConsoleCP - 设置输入代码页
        result_in = kernel32.SetConsoleCP(65001)

        if result_out and result_in:
            api_ok = True
            results.append(f"API成功: SetConsoleOutputCP/CP => 65001 (UTF-8)")
        else:
            # GetLastError 获取错误码
            err = ctypes.get_last_error()
            results.append(f"API部分失败: Out={bool(result_out)}, In={bool(result_in)}, Err={err}")
    except Exception as e:
        results.append(f"API调用异常: {e}")

    # 方法3: 重新包装 stderr / stdout 为 UTF-8
    wrap_ok = False
    try:
        wrapped_count = 0
        for stream_name in ("stdout", "stderr"):
            stream = getattr(sys, stream_name, None)
            if stream is None:
                continue
            if hasattr(stream, "buffer") and not isinstance(stream, io.TextIOWrapper):
                # 检查是否已经是 UTF-8 编码
                if hasattr(stream, "encoding") and stream.encoding and stream.encoding.lower() == "utf-8":
                    continue  # 已经是 UTF-8,无需包装

                wrapped = io.TextIOWrapper(
                    stream.buffer,
                    encoding="utf-8",
                    errors="replace",
                    line_buffering=True,
                )
                setattr(sys, stream_name, wrapped)
                wrapped_count += 1

        if wrapped_count > 0:
            wrap_ok = True
            results.append(f"流包装成功: 包装了 {wrapped_count} 个流为 UTF-8")
        else:
            results.append("流无需包装 (已是UTF-8或无buffer)")
    except Exception as e:
        results.append(f"流包装异常: {e}")

    # 汇总结果
    _encoding_fixed = api_ok or wrap_ok or True  # 至少环境变量已设置
    summary = " | ".join(results)
    return _encoding_fixed, summary


def setup_logging(level: int = logging.INFO, log_file: str | None = None) -> None:
    """初始化全局日志配置。仅在应用入口调用一次。

    Args:
        level: 控制台输出级别,默认 INFO
        log_file: 可选,同时写入文件路径。None 则只输出到 stderr
    """
    global _configured
    if _configured:
        return

    # 先修复控制台编码 (UTF-8),再创建 handler
    enc_ok, enc_msg = _force_utf8_console()

    root = logging.getLogger()
    root.setLevel(level)

    # 控制台 handler(显式使用 UTF-8 Stream,避免 Windows 乱码)
    if not any(isinstance(h, logging.StreamHandler) and h.stream is sys.stderr
               for h in root.handlers):
        sh = logging.StreamHandler(sys.stderr)
        sh.setLevel(level)
        sh.setFormatter(logging.Formatter(_DEFAULT_FORMAT, _DEFAULT_DATEFMT))
        root.addHandler(sh)

    # 文件 handler(UTF-8 写入,无乱码)
    file_log_msg = ""
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(str(log_path), encoding="utf-8")
        fh.setLevel(level)
        fh.setFormatter(logging.Formatter(_DEFAULT_FORMAT, _DEFAULT_DATEFMT))
        root.addHandler(fh)
        file_log_msg = f", 文件日志={log_path}"

    _configured = True

    # 输出编码修复状态 (使用 print 确保日志已可输出)
    try:
        status = "✅" if enc_ok else "⚠️"
        print(f"\n{status} [日志初始化] 编码修复: {enc_msg}{file_log_msg}")
        if log_file:
            print(f"   查看日志文件: Get-Content {log_file} -Encoding UTF8\n")
    except Exception:
        pass

    # 用 logger 再记录一次
    try:
        root_logger = logging.getLogger(__name__)
        root_logger.info(f"[日志初始化] 编码修复状态: {enc_msg}{file_log_msg}")
        if sys.platform == "win32" and log_file:
            root_logger.info(f"[日志提示] 查看日志文件请使用: Get-Content {log_file} -Encoding UTF8")
    except Exception:
        pass


def get_logger(name: str) -> logging.Logger:
    """便捷获取 logger,顺便确保已 setup。"""
    if not _configured:
        setup_logging()
    return logging.getLogger(name)


def view_log_file(log_file: str, lines: int = 50, keyword: str | None = None) -> str:
    """查看日志文件内容(UTF-8 编码),方便排查问题。

    Args:
        log_file: 日志文件路径
        lines: 显示最后 N 行 (默认 50)
        keyword: 可选,只显示包含该关键词的行

    Returns:
        日志内容字符串
    """
    log_path = Path(log_file)
    if not log_path.exists():
        return f"[错误] 日志文件不存在: {log_file}"

    try:
        with open(log_path, "r", encoding="utf-8") as f:
            all_lines = f.readlines()

        if keyword:
            filtered = [l for l in all_lines if keyword in l]
            if not filtered:
                return f"[提示] 未找到包含 '{keyword}' 的日志行"
            output_lines = filtered[-lines:]
        else:
            output_lines = all_lines[-lines:]

        result = f"=== 日志文件: {log_file} (共 {len(all_lines)} 行,显示最后 {len(output_lines)} 行) ===\n"
        if keyword:
            result += f"[筛选关键词: {keyword}]\n"
        result += "".join(output_lines)
        return result
    except UnicodeDecodeError:
        # 尝试 GBK 解码 (某些老日志文件)
        try:
            with open(log_path, "r", encoding="gbk") as f:
                all_lines = f.readlines()
            output_lines = all_lines[-lines:]
            result = f"=== 日志文件 (GBK解码): {log_file} ===\n"
            result += "".join(output_lines)
            return result
        except Exception as e:
            return f"[错误] 读取日志文件失败: {e}"
    except Exception as e:
        return f"[错误] 读取日志文件失败: {e}"


def tail_log(log_file: str, keyword: str | None = None) -> None:
    """实时查看日志文件 (类似 tail -f)。

    在 PowerShell 中运行: python -c "from src.log_config import tail_log; tail_log('logs/e2e_run.log')"

    Args:
        log_file: 日志文件路径
        keyword: 可选,只显示包含该关键词的行
    """
    import time

    log_path = Path(log_file)
    if not log_path.exists():
        print(f"[错误] 日志文件不存在: {log_file}")
        return

    print(f"开始实时查看日志: {log_file}")
    print("按 Ctrl+C 退出\n")

    last_pos = 0
    try:
        while True:
            try:
                with open(log_path, "r", encoding="utf-8") as f:
                    f.seek(last_pos)
                    new_content = f.read()
                    if new_content:
                        for line in new_content.splitlines():
                            if keyword is None or keyword in line:
                                print(line)
                    last_pos = f.tell()
            except UnicodeDecodeError:
                # 文件可能正在写入,稍后重试
                pass
            except Exception as e:
                print(f"[错误] 读取日志失败: {e}")
                break

            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n已退出日志查看")
