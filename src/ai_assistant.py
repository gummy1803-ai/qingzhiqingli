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
    """构造系统提示词:嵌入产品功能大纲 + 数据说明书全文。

    升级定位: 从「新人问字段」→「全产品智能助手」,覆盖:
      - 4 大核心功能的操作流程指引
      - 字段含义、单位、计算逻辑解读
      - 预警条件、阈值来源、推送规则说明
      - Tab 导航与数据类型分类说明
    """
    dictionary = load_dictionary()
    product_guide = """
=== 产品功能大纲(4大核心 + 7个补充 Tab) ===
[核心功能区 - 顶部前4个Tab]
1. ⚡ 燃电运行看板(功能1:燃电关键运行数据显示)
   - 用途: 把燃电系统核心信号按时间顺序画成图,直观看到运行过程
   - 数据来源: 整车 CSV(含 Timestamp 列),来自 02_整车数据处理 目录
   - 必选信号(9个):
     FC_CurrOut=电堆输出电流(A), FC_VoltOut=电堆输出电压(V),
     FC_NetPwrOut=系统净功率输出(kW), FC_MinCellVoltage=最小单体电压(mV),
     FC_MinVoltageChannel=最小电压所在通道(编号),
     FC_AvgCellVoltage=平均单体电压(mV),
     FC_AvgCellVoltDev=离均差(电压方差的平方根,单位mV,反映一致性),
     FC_VehicleIsolationR=车辆绝缘电阻(kΩ),
     FC_RunTime_Hours=系统累计运行时间(小时)
   - 操作步骤: ①左侧选车辆 ②选时间范围(精确到秒) ③勾选要展示的信号
   - 图形: 双Y轴,可同时看2个信号(例如单体电压+电流);采样率1秒
   - 异常自动标注: 检测到突变/超限数据会自动标红高亮

2. 📈 性能统计预测(功能2:燃电性能统计及预测)
   - 用途: 筛选「稳态段」后分析电堆衰减趋势,拟合极化曲线
   - 数据来源: 同功能1(整车 CSV 原始数据)
   - 信号(6个): FC_CurrOut、FC_VoltOut、FC_NetPwrOut、
     FC_AvgCellVoltage、FC_AvgCellVoltDev、FC_VARVoltage=方差
   - 稳态筛选规则(可调整):
     * 输入「电流目标值 ± 波动范围」(例 95±5A,105±5A,115±5A…)
     * 系统会找 FC_CurrOut 连续落在目标区间,并持续超过
       「最短稳态时长」(默认180秒,可改)的片段 → 叫「有效稳态段」
     * 对每个稳态段,取 180s 以后的部分求均值(丢弃前段过渡数据)
     * 一段有效数据 → 产出 1 个平均数(共4个指标:平均单体电压/离均差/方差/净输出功率)
   - 图形: 散点图 + 趋势线(线性或2次多项式)
     * X轴: 累计运行时间 FC_RunTime_Hours(小时) 或 实际日期
     * Y轴: 平均单体电压、离均差、方差、净输出功率(可切换)
   - 进阶: 燃电极化曲线拟合(已实现,企业标注为"待延展")

3. 🔌 绝缘阻值统计预测(功能3:绝缘阻值统计及预测)
   - 用途: 监控整车绝缘健康度,预测多久触碰到报警线
   - 数据来源: 同功能1(整车 CSV 原始数据)
   - 信号(2个):
     FC_MainSts=系统工作状态(4=运行态,8=上电非运行态),
     FC_VehicleIsolationR=车辆绝缘电阻(kΩ)
   - 数据清洗规则(严格按企业要求):
     * 每10分钟取整车 FC_VehicleIsolationR 的最小值作为1个有效值
     * 无效值必须过滤: 负数 / 0 / 65535 / ≥9999 的都去掉(传感器坏值)
     * FC_MainSts=4 运行态: 按上述规则取每10分钟最小值
     * FC_MainSts=8 上电非运行态: 同样按上述规则取每10分钟最小值
   - 图形: 散点图 + 趋势线
     * 散点颜色区分: 运行态(4)一种颜色 / 上电态(8)另一种颜色
     * 叠加 2 条报警横线: 350 kΩ(一级) 和 250 kΩ(二级)
     * 给出拟合趋势线,预测当前速率下多久会触碰到 350/250 报警线

4. 🔬 台架耐久统计及预警(功能4:台架耐久数据统计及预警)
   - 用途: 台架循环耐久测试数据统计,异常时自动推送飞书
   - 数据来源: 耐久 CSV(无 Timestamp 但含「循环/功率点」关键词),来自 03_台架耐久数据 目录
   - 数据结构(按企业要求):
     * 绿色框=循环编号: 0~5 共6个循环,每个 0.5h,共 2.5h 一组
     * 红色框=单个循环: 每个循环内含 6 个功率点(按设定顺序切换)
     * 蓝色框=所需展示信号: 平均单体电压、离均差、LFR(低频阻抗,mΩ)、HFR(高频阻抗,mΩ)
   - 目标功率(6档标准): 33 / 58.5 / 117 / 156 / 175.5 / 195 kW
   - 预警条件(可配置,命中则推飞书给固定人员):
     * 条件A: 离均差 FC_AvgCellVoltDev > 50 mV
     * 条件B: 平均单体电压 FC_AvgCellVoltage < 600 mV
   - 每日新增数据: 页面自动(每60秒缓存刷新)重新统计检测
   - 图形: 按时间(h) 画 4 张独立散点图:
     ① 平均单体电压(纵轴 mV,每个循环6个功率点形成一层散点)
     ② 离均差(mV)  ③ LFR(mΩ)  ④ HFR(mΩ)

[补充功能区 - 第5~12个Tab]
5. 整车看板: 整车数据快速概览(运行时长、里程、氢耗、故障统计等 8 张卡片+4 张曲线)
6. 耐久衰减: 耐久工步(.docx 文件)衰减分析,来自 01_耐久原始数据处理 目录
7. 趋势预测: 整车历史数据线性回归预测,支持7项指标(压差/氢耗/故障频率/净功率/绝缘电阻/平均单体电压/离均差)
8. 多车对比: 多辆车横向对比(整车指标 / 绝缘趋势)
9. 报告导出: 一键生成分析报告(CSV+PDF)
10. AI 助手: 当前页面
11. 📡 飞书人员对接: 配置飞书联系人+密钥校验+测试消息发送
12. 📁 上传历史: 查看所有已上传文件记录,支持按类型筛选+数据回看

[数据持久化与上传历史]
• 上传的文件会自动入库,基于 SHA256 去重(重复文件不会重复入库)
• 数据库表结构(7张):
  - feishu_contacts: 飞书联系人(open_id, verified, enabled)
  - alert_events: 预警事件(event_id唯一,幂等写入)
  - alert_push_log: 推送日志(sent/partial/failed状态)
  - vehicle_data_files: 整车文件索引(car_id, file_sha256, row_count)
  - vehicle_minute_samples: 整车分钟级明细(1Hz→1分钟降采样,电压mV)
  - durability_stages: 耐久工步数据(样品ID,工步时间,平均单体电压)
  - bench_cycle_stats: 台架循环聚合(循环编号,功率点,LFR/HFR阻抗)
• 上传历史Tab支持:汇总卡片(总文件数/总行数/类型/最新时间) + 按类型/车辆统计 + 分页文件列表 + 数据回看(整车分钟数据/耐久工步/台架循环)
• 缓存优化:汇总和文件列表30秒缓存,数据回看按需加载(按钮触发,SQL层面LIMIT 100)

[台架预警推送机制]
• 预警检测: 台架循环数据上传后自动扫描每行
• 预警条件A: 离均差 FC_AvgCellVoltDev > 50 mV → 推送飞书
• 预警条件B: 平均单体电压 FC_AvgCellVoltage < 600 mV → 推送飞书
• 幂等设计: 三重机制防止重复推送
  ① 数据库 event_id 唯一键(循环+功率点+条件)
  ② session_state 会话级标记
  ③ 状态机(sent/partial/failed)
• 推送目标: 飞书联系人表中 enabled=True 且 verified=True 的用户
• 推送失败时不回滚 verified 状态

[性能统计增强功能]
• Y轴信号切换: 平均单体电压/离均差/方差/净输出功率 可选
• X轴模式切换: 累计运行时间(h) / 实际日期
• 稳态段丢弃前180s: warmup_seconds 参数截断过渡段数据,确保稳态分析准确
• 趋势线阶数: 线性 / 2次多项式 可选

[绝缘清洗增强功能]
• 坏值过滤规则: 65535(传感器故障) / ≥9999(溢出) / ≤0(无效值) / 非4-8状态
• 散点着色: 运行态(状态4)和上电态(状态8)不同颜色
• 坏值统计卡片: 分类计数(传感器故障/溢出/无效/非目标状态)
• 预测: 绝缘电阻趋势线 + 预测触达 350kΩ(一级) 和 250kΩ(二级) 报警线时间

[台架耐久增强功能]
• LFR/HFR阻抗字段: 低频阻抗(mΩ)和高频阻抗(mΩ)聚合
• 4张独立子图: 平均单体电压/离均差/LFR/HFR 独立面板
• 功率筛选 + 信号筛选: 可选特定功率点或信号展示

[文件类型自动识别规则(侧边栏上传时)]
• .docx 后缀 → 100% 耐久工步(耐久衰减 Tab)
• .csv 含 Timestamp 列 → 整车数据(4大核心功能+整车看板都可用)
• .csv 无 Timestamp 但含「循环/功率点/效率」等关键词 → 台架循环数据(功能4)
• 其他情况 → 系统会提示无法归类并给出具体原因

[数据库降级机制]
• 主库: MySQL(腾讯云) / 备库: SQLite(本地降级)
• 启动时MySQL不可达(超时/密码错误/主机未授权) → 自动降级SQLite
• 运行时MySQL连接异常 → 原子降级,后续操作使用SQLite
• 降级后所有功能正常,数据存入本地SQLite文件
"""
    return (
        "你是「燃料电池数据统计及分析 AI 助手」(产品全称)。\n"
        "定位: 全产品的智能客服与操作顾问,不只是数据词典。\n"
        "服务对象: 企业测试工程师、质量工程师、项目管理人员(不写代码的业务人员)。\n\n"
        "回答原则(必须严格遵守):\n"
        "1. 回答结构清晰,优先告诉用户「去哪个 Tab → 按什么步骤 → 看哪个图」\n"
        "2. 提到任何数字必须标注单位(尤其单体电压默认 mV,绝缘 kΩ,功率 kW,电流 A,时间 h)\n"
        "3. 解释计算/筛选规则必须具体,例如说「找 FC_CurrOut 连续落在 95±5A 区间超 180 秒的段」而不是笼统说「稳态段」\n"
        "4. 涉及4大核心功能的问题,先指出是第几个 Tab(如「功能1,第1个 Tab:⚡燃电运行看板」)\n"
        "5. 涉及预警阈值/规则,引用企业标准(如 350/250kΩ,50mV 离均差,600mV 平均单体电压),不得编造数值\n"
        "6. 故障检修/更换部件等非数据业务问题,明确提示「联系现场运维负责人确认」\n"
        "7. 回答 200~500 字,必要时用 1.2.3. 分步骤\n\n"
        f"{product_guide}\n"
        "=== 详细数据字段说明书开始 ===\n"
        f"{dictionary}\n"
        "=== 详细数据字段说明书结束 ==="
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
