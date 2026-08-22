"""AI 助手模块:基于说明书回答客户关于数据含义的问题。

设计要点:
- 云端 LLM(OpenAI 兼容接口,可接 OpenAI / 智谱 GLM / 通义千问等)
- 配置从 config/llm_config.ini 读取(模板见 .example.ini)
- 系统提示词内置数据说明书全文,确保 AI 基于说明书回答不编造
- 未配置 API key 时,自动降级为本地检索模式(基于说明书关键词匹配)
"""
from __future__ import annotations

import configparser
import json
import logging
from pathlib import Path

import requests

from src.log_config import get_logger

logger = get_logger(__name__)

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "llm_config.ini"
DICTIONARY_PATH = ROOT / "docs" / "DATA_DICTIONARY.md"

# 说明书缓存(避免每次调用都读盘)
_dictionary_cache: str | None = None


def load_dictionary() -> str:
    """加载数据说明书全文(用作 AI 系统提示词)。"""
    global _dictionary_cache
    if _dictionary_cache is None:
        if DICTIONARY_PATH.exists():
            _dictionary_cache = DICTIONARY_PATH.read_text(encoding="utf-8")
            logger.info("已加载说明书: %s (%d 字符)",
                        DICTIONARY_PATH, len(_dictionary_cache))
        else:
            _dictionary_cache = ""
            logger.warning("说明书不存在: %s", DICTIONARY_PATH)
    return _dictionary_cache


def load_llm_config(config_path: Path | str | None = None) -> dict | None:
    """读取 LLM 配置。返回 None 表示未配置(走本地检索模式)。"""
    cfg_path = Path(config_path) if config_path else CONFIG_PATH
    logger.info("[LLM 配置] 尝试读取: %s (存在=%s)",
                cfg_path, cfg_path.exists())
    if not cfg_path.exists():
        logger.warning("[LLM 配置] 配置文件不存在,走本地检索模式")
        return None
    try:
        cp = configparser.ConfigParser()
        cp.read(cfg_path, encoding="utf-8")
        logger.info("[LLM 配置] 配置文件读取成功, sections=%s",
                    cp.sections())
        if "llm" not in cp:
            logger.warning("[LLM 配置] 缺少 [llm] section")
            return None
        cfg = {
            "base_url": cp.get("llm", "base_url", fallback=""),
            "api_key": cp.get("llm", "api_key", fallback=""),
            "model": cp.get("llm", "model", fallback=""),
            "temperature": cp.getfloat("llm", "temperature", fallback=0.3),
            "max_tokens": cp.getint("llm", "max_tokens", fallback=1500),
        }
        # 不打印完整 api_key,只打印前后几位
        key_preview = (cfg["api_key"][:6] + "..." + cfg["api_key"][-4:]
                       if cfg["api_key"] else "(空)")
        logger.info("[LLM 配置] base_url=%s / model=%s / api_key=%s / "
                    "temp=%s / max_tokens=%s",
                    cfg["base_url"], cfg["model"], key_preview,
                    cfg["temperature"], cfg["max_tokens"])
        if not (cfg["base_url"] and cfg["api_key"] and cfg["model"]):
            logger.warning("[LLM 配置] 关键字段缺失,走本地检索模式")
            return None
        return cfg
    except Exception as e:
        logger.error("[LLM 配置] 读取异常: %s", e, exc_info=True)
        return None


def _build_system_prompt() -> str:
    """构造系统提示词,嵌入说明书全文。"""
    dictionary = load_dictionary()
    return (
        "你是燃料电池测试数据分析助手的 AI 客服。\n"
        "服务对象: 完全不懂代码、第一次接触燃料电池测试数据的客户。\n"
        "回答原则:\n"
        "1. 严格基于下方《数据说明书》回答,不得编造说明书未提及的字段或计算方式\n"
        "2. 提到任何数字必须带单位(尤其单片电压按 mV 理解)\n"
        "3. 解释计算方式要具体,如\"是过滤后 FC_XXX 字段的平均值\"\n"
        "4. 涉及具体故障码诊断、检修建议等业务判断,提示客户联系运维负责人\n"
        "5. 用通俗语言,避免技术黑话;必要时举例\n"
        "6. 回答简洁,200~500 字\n\n"
        f"=== 数据说明书开始 ===\n{dictionary}\n=== 数据说明书结束 ==="
    )


def _local_retrieve_answer(question: str) -> str:
    """本地检索模式:基于说明书行级匹配返回相关内容 + 上下文。

    无 API key 时的降级方案,只能回答说明书已覆盖的问题。
    改进: 按行匹配(覆盖表格行),并返回匹配行前后 ±3 行作为上下文。
    """
    dictionary = load_dictionary()
    if not dictionary:
        return ("AI 助手未配置: 请创建 config/llm_config.ini 填入 API key, "
                "或联系运维添加 docs/DATA_DICTIONARY.md")

    # 提取关键词(去标点 + 2-gram 切分,应对中文无空格分词)
    cleaned = question
    for ch in "?,。,;:!、？，。；：！":
        cleaned = cleaned.replace(ch, " ")
    # 按空格切得到词块
    word_chunks = [w for w in cleaned.split() if w]
    keywords: list[str] = []
    for chunk in word_chunks:
        if len(chunk) <= 3:
            keywords.append(chunk)
        else:
            # 长词块切 2-gram + 3-gram,让"百公里氢耗"里能匹配上"氢耗"
            for n in (2, 3):
                for i in range(len(chunk) - n + 1):
                    keywords.append(chunk[i:i + n])
    if not keywords:
        return ("请用更具体的关键词提问,例如 \"压差\"、\"百公里氢耗\"、\"345\"")

    lines = dictionary.split("\n")
    scored = []
    for i, line in enumerate(lines):
        # 跳过纯分隔线
        if line.strip().startswith("---") or not line.strip():
            continue
        score = sum(1 for kw in keywords if kw in line)
        if score > 0:
            scored.append((score, i, line))
    scored.sort(key=lambda x: (-x[0], x[1]))

    if not scored:
        return ("本地检索模式未找到匹配内容,该问题可能超出说明书范围。\n"
                "建议: 1) 查看完整说明书 docs/DATA_DICTIONARY.md\n"
                "2) 配置 LLM API key 后可获得更智能的回答")

    # 取 Top3 匹配,每个加 ±3 行上下文
    snippets = []
    seen_lines = set()
    for score, idx, _ in scored[:3]:
        lo = max(0, idx - 3)
        hi = min(len(lines), idx + 4)
        ctx_lines = lines[lo:hi]
        # 避免重复段落
        key = (lo, hi)
        if key in seen_lines:
            continue
        seen_lines.add(key)
        snippets.append("\n".join(ctx_lines))

    return ("[本地检索模式 - 说明书相关段落]\n\n"
            + "\n\n---\n\n".join(snippets)
            + "\n\n如需更智能的回答,请配置 config/llm_config.ini")


def ask(question: str, context: dict | None = None,
        config_path: Path | str | None = None) -> str:
    """回答客户问题。

    Args:
        question: 客户问题
        context: 可选上下文(如当前查看的车辆、指标值),会拼接到问题前
        config_path: LLM 配置文件路径

    Returns:
        str: AI 回答文本
    """
    import time
    t0 = time.time()
    logger.info("[AI 请求] 收到问题(长度=%d): %s",
                len(question), question[:80])

    cfg = load_llm_config(config_path)
    if cfg is None:
        logger.info("[AI 请求] LLM 未配置,走本地检索模式 (耗时=%.2fs)",
                    time.time() - t0)
        return _local_retrieve_answer(question)

    # 构造完整问题(可附加上下文)
    full_question = question
    if context:
        ctx_str = json.dumps(context, ensure_ascii=False, indent=2)
        full_question = f"当前数据上下文:\n{ctx_str}\n\n客户问题: {question}"
    logger.info("[AI 请求] 完整问题长度=%d / 含上下文=%s",
                len(full_question), bool(context))

    system_prompt = _build_system_prompt()
    logger.info("[AI 请求] 系统提示词长度=%d", len(system_prompt))

    try:
        # OpenAI 兼容接口:base_url 写完整到版本路径(如 .../v1 或 .../v4)
        # 代码只补 /chat/completions,避免 /v1/v1 重复
        url = f"{cfg['base_url'].rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {cfg['api_key']}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": cfg["model"],
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": full_question},
            ],
            "temperature": cfg["temperature"],
            "max_tokens": cfg["max_tokens"],
        }
        payload_size = len(json.dumps(payload, ensure_ascii=False))
        logger.info("[AI 请求] HTTP POST %s / model=%s / payload=%d bytes",
                    url, cfg["model"], payload_size)
        t1 = time.time()
        resp = requests.post(url, headers=headers, json=payload, timeout=60)
        t2 = time.time()
        logger.info("[AI 请求] HTTP 响应: status=%s / 耗时=%.2fs / "
                    "resp_size=%d bytes",
                    resp.status_code, t2 - t1, len(resp.content))
        resp.raise_for_status()
        data = resp.json()
        answer = (data.get("choices", [{}])[0]
                  .get("message", {}).get("content", "")).strip()
        if not answer:
            logger.warning("[AI 请求] LLM 返回空内容 (耗时=%.2fs)",
                           time.time() - t0)
            return "AI 未返回有效内容,请稍后重试"
        logger.info("[AI 请求] 回答完成: %d 字符 / 总耗时=%.2fs",
                    len(answer), time.time() - t0)
        return answer

    except requests.exceptions.Timeout:
        logger.error("[AI 请求] 调用超时 (耗时=%.2fs)", time.time() - t0)
        return "AI 响应超时,请稍后重试或换用本地检索模式"
    except requests.exceptions.HTTPError as e:
        logger.error("[AI 请求] HTTP 错误: %s / 响应体=%s (耗时=%.2fs)",
                     e, e.response.text[:200] if e.response else "",
                     time.time() - t0, exc_info=True)
        return f"AI 调用失败(HTTP {e.response.status_code if e.response else '?'}),请检查 API key"
    except Exception as e:
        logger.error("[AI 请求] 调用异常: %s (耗时=%.2fs)",
                     e, time.time() - t0, exc_info=True)
        return f"AI 调用异常: {e}"
