"""周报生成：选题打分 → 深度解读 → 按模板渲染 → 归档原文。

对应原先手工跑的 `gen_weekly_17.py` 那套脚本，但数据源换成 MP 自己的库
（公众号缓存 + 「其他来源」外部目录），模型走应用里已配置的 AI 模型。

**模板是唯一真相**（2026-09 重做）：旧脚本用正则抠 `<div class="article-card">` 那块，
卡片 HTML 在模板与 Python 里各写一份 —— 用户改模板里的卡片，输出不会变。
现在改用 Jinja2，循环与条件都写在模板里，改什么输出就是什么。
配套三重安全网：未定义变量会被**列名警告**（不是静默空白）、语法错误带行号、
预览模式用缓存数据渲染不花 AI 调用。

**AI 提示词可人工编辑**：四段（打分/深度解读/核心洞察/其他摘要）都能改，
默认值就是旧脚本里的原文。但**输出 JSON 格式由代码固定拼接、不可编辑**
（`FIXED_OUTPUT`）—— 否则用户改标准时删掉「只输出 JSON」，解析就崩了。
这与 `ai_filter` 的「原则 + FIXED_OUTPUT_REQUIREMENTS」是同一套设计。

**缓存随提示词自动失效**：缓存键 = `sha256(该段完整提示词)[:8] + ":" + 文章键`。
改哪段只有哪段失效，改回来还能命中旧缓存 —— 比手工版本号精确。
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import html as html_mod
import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

# 外部条目的正文规则与 AI 内容筛选共用同一份实现（2026-09 由两处逐字相同的
# 副本合并而来）。external_sources 是叶子模块、不反向依赖 core，无循环风险。
from mp_harvest.core.ai_filter import ModelCallError
from mp_harvest.core.external_sources import read_external_body

# ── 领域与业务标签（与旧脚本一致，模板注释里也有同一份规范）──────────

DOMAINS = [
    "AI芯片架构与推理优化",
    "FPGA/可编程计算与架构",
    "芯片互联与存储架构",
    "处理器安全与可信架构",
    "半导体制造与先进封装",
]

BUSINESS_TAGS = ["公共", "数通", "传送", "接入", "芯片硬件"]

TAG_COLORS = {
    "公共": "#27ae60",
    "数通": "#e74c3c",
    "传送": "#9b59b6",
    "接入": "#3498db",
    "芯片硬件": "#f39c12",
}

# 业务标签的类目定义 + 代表性关键词（写进提示词，让**模型**按场景判断）。
# 数通/传送/接入 是通信领域的邻接术语，只给名字模型会漂移（「800G 以太网」算哪个？），
# 必须给场景定义。完整关键词表见下方 BUSINESS_TAG_KEYWORDS（用于模型漏答时兜底）。
BUSINESS_TAG_STANDARD = """- 数通：数据中心 / AI 集群场景 —— DPU/SmartNIC、可编程数据平面、
  NVLink/InfiniBand/RoCE、拥塞控制、P4/DPDK、GPU 架构、NoC、Chiplet/先进封装、HBM/CXL
- 传送：光通信与高速传输 —— 硅光/CPO、相干光、SerDes/PAM4、光 DSP、
  FEC、DWDM/OTN、800G/1.6T 以太网、LPO、调制器/探测器
- 接入：接入网与确定性网络 —— PON/OLT/ONU、FTTx、xDSL、DOCSIS、
  TSN/时间敏感网络、Wi-Fi/5G/6G 接入、IEEE 1588/SyncE 时间同步
- 公共：跨领域通用芯片设计技术 —— FPGA/HLS/EDA、RTL/Verilog、
  形式验证、物理设计/布局布线、仿真/原型验证、处理器微架构、存储器与存算一体
- 芯片硬件：半导体制造 / 器件 / 工艺 —— FinFET/GAA/CMOS、光刻/EUV、
  刻蚀/沉积（ALD/CVD/PVD）、CMP、晶圆/衬底、键合/混合键合、玻璃基板/金刚石材料

判断顺序建议：先看**应用场景**（数据中心 / 光传输 / 接入网 / 通用设计 / 制造工艺），
再看关键词。一篇可能命中多个，最多取 3 个最相关的。"""

# 完整关键词表（源自用户原有周报流水线里的 BUSINESS_TAG_KEYWORDS，
# 即其本人给出的分类标准）。
# 用途：**模型没给出有效标签时的兜底** —— 绝不静默退化成「公共」。
BUSINESS_TAG_KEYWORDS: dict[str, list[str]] = {
    "数通": [
        "数据中心", "AI集群", "超大规模", "DPU", "SmartNIC", "智能网卡",
        "可编程数据平面", "网络内计算", "网络处理器", "分组处理器", "线速",
        "NVMe-oF", "RoCEv2", "iWARP", "GPUDirect", "NVLink", "InfiniBand",
        "Slingshot", "SHARP", "集合通信", "NCCL", "拥塞控制", "ECN", "PFC",
        "DCQCN", "负载均衡", "VXLAN", "Geneve", "Segment Routing",
        "P4", "eBPF", "XDP", "DPDK", "SPDK", "VPC", "NFV", "SDN",
        "GPU架构", "片上网络", "NoC", "片上互连", "Chiplet", "先进封装",
        "3D集成", "2.5D集成", "CoWoS", "EMIB", "UCIe", "异构集成",
        "芯片对芯片", "Die-to-Die", "D2D", "HBM", "CXL",
    ],
    "传送": [
        "硅光子学", "硅光", "相干光", "光互连", "共封装光学", "CPO",
        "光子集成电路", "PIC", "光DSP", "SerDes", "PAM4", "高速串行",
        "收发器", "Transceiver", "PHY", "均衡", "CDR", "里德-所罗门码",
        "LDPC", "长距传输", "城域网", "骨干网", "DWDM", "CWDM", "OTN",
        "光交叉连接", "OXC", "ROADM", "Mux/Demux", "TIA", "驱动器",
        "调制器", "探测器", "APD", "PIN", "EDFA", "拉曼放大器", "SOA",
        "色散", "PMD", "CD", "DGD", "OSNR", "Q因子", "误码率", "BER",
        "前向纠错", "FEC", "KP4-FEC", "SC-FEC", "Staircase FEC",
        "OpenZR+", "OSFP", "QSFP-DD", "CFP", "OIF", "IEEE 802.3",
        "800G以太网", "1.6T以太网", "线性驱动可插拔", "LPO",
        "低功耗SerDes", "模拟前端", "AFE", "ADC/DAC", "PLL", "VCO",
    ],
    "接入": [
        "TSN", "时间敏感网络", "确定性网络", "实时以太网",
        "PON", "无源光网络", "OLT", "ONU", "ONT", "ODN", "光分路器",
        "EPON", "GPON", "10G-EPON", "XG-PON", "XGS-PON", "NG-PON2", "WDM-PON",
        "FTTx", "FTTH", "FTTB", "FTTC", "FTTdp",
        "xDSL", "ADSL", "VDSL", "VDSL2", "G.fast", "DSLAM",
        "HFC", "Cable Modem", "DOCSIS", "以太网接入",
        "Wi-Fi", "5G接入", "6G接入", "PPPoE", "L2TP",
        "接入网", "UNI", "SNI", "本地交换局", "业务节点", "CPN",
        "最后一公里", "时间同步", "IEEE 802.1AS", "流量整形",
        "TAS", "时间感知整形器", "CQF", "循环排队转发", "帧抢占",
        "FRER", "URLLC", "TSN交换机", "TSN端点", "门控调度",
        "PTP", "IEEE 1588", "SyncE", "同步以太网",
    ],
    "公共": [
        "FPGA", "可编程", "可重构", "HDL", "Verilog", "VHDL", "RTL",
        "EDA", "电子设计自动化", "HLS", "高层次综合", "逻辑综合",
        "布局", "布线", "时序", "LUT", "BRAM", "DSP", "可配置逻辑",
        "芯片设计", "处理器设计", "CPU设计", "SoC", "RISC-V", "ARM处理器",
        "微架构", "缓存层次结构", "内存子系统",
        "SRAM", "DRAM", "非易失性存储器", "NVM", "存储级内存", "SCM",
        "持久性内存", "PMem", "内存墙",
        "形式验证", "模型检查", "等价性检查", "SAT求解器",
        "物理设计", "布局规划", "功耗分析", "热分析",
        "原型验证", "仿真", "硬件仿真", "FPGA原型验证", "快速原型验证",
        "在线仿真", "ICE", "仿真平台", "硬件在环", "HIL",
        "近似计算", "随机计算", "内存计算", "存内计算", "CIM",
        "自旋电子学", "忆阻器", "相变存储器", "PCM", "铁电存储器", "FeRAM",
        "量子计算硬件", "超导电路", "离子阱", "低温计算", "低温CMOS",
        "AI工作负载", "DSA", "特定域架构", "SIMD", "SIMT", "VLIW",
        "乱序执行", "OoO", "超标量", "分支预测", "虚拟内存", "MMU",
        "中断控制器", "DMA", "AHB", "APB", "AXI", "AMBA",
        "片上调试", "JTAG", "边界扫描", "BIST", "ATPG", "ECO",
        "时序收敛", "信号完整性", "SI", "电源完整性", "PI", "IR压降",
        "电迁移", "EM", "静态时序分析", "STA", "动态仿真",
        "回归测试", "覆盖率", "性能计数器", "Trace",
        "功耗门控", "时钟门控", "电压频率缩放", "内存一致性模型",
        "安全启动", "可信执行环境", "TEE", "PUF", "互连", "DCI",
    ],
    "芯片硬件": [
        "FinFET", "GAA", "MOSFET", "CMOS工艺", "工艺节点", "工艺技术",
        "光刻", "Lithography", "EUV", "DUV", "刻蚀", "沉积",
        "CMP", "化学机械抛光", "离子注入", "外延", "MBE", "分子束外延",
        "ALD", "原子层沉积", "CVD", "化学气相沉积", "PVD", "物理气相沉积",
        "溅射", "电镀", "盲孔工艺", "BEOL", "FEOL", "金属层", "通孔",
        "接触孔", "栅极", "源极", "漏极", "衬底", "晶圆", "Wafer",
        "芯片", "Die", "光掩模", "Photomask", "OPC", "光学邻近校正",
        "SADP", "SAQP", "自对准", "多重图案化", "DSA", "定向自组装",
        "晶体管", "器件", "制程",
        "玻璃基板", "金刚石", "材料", "键合", "混合键合",
    ],
}


# 短 ASCII 缩写必须整词匹配，不能当子串。
# 关键词表是按中文语料写的，`SI`（信号完整性）/`EM`（电迁移）/`PI`（电源完整性）/
# `CD`（色散）在中文标题里不会误伤；但周报要一起跑 arXiv 英文摘要 —— 在那里
# `SI` 会撞上 **silicon**、`EM` 撞上 **system**、`PI` 撞上 **pipeline**，
# 于是「硅光 CPO 模块」被分到「公共」。整词匹配解决它。
# 中文关键词（含 CJK）不走这条路：中文没有词边界，本来就得子串匹配。
_ASCII_ABBREV = re.compile(r"^[A-Za-z0-9]{1,3}$")
_BOUNDARY_CACHE: dict[str, re.Pattern[str]] = {}


def _kw_hit(keyword: str, low_text: str) -> bool:
    kw = keyword.lower()
    if _ASCII_ABBREV.match(keyword):
        pat = _BOUNDARY_CACHE.get(kw)
        if pat is None:
            # (?!…) / (?<!…) 只挡字母：`HBM3`、`800G`、`TSN交换机` 都要能命中，
            # 而 `silicon`、`system`、`pipeline` 不命中。
            pat = re.compile(rf"(?<![a-z]){re.escape(kw)}(?![a-z])")
            _BOUNDARY_CACHE[kw] = pat
        return pat.search(low_text) is not None
    return kw in low_text


def _kw_strong(keyword: str) -> bool:
    """强关键词 = 中文词或长词，单个就足以定类；短 ASCII 缩写需要旁证。"""
    return not _ASCII_ABBREV.match(keyword)


def infer_business_tags(*parts: str, limit: int = 3) -> list[str]:
    """按关键词表推断业务标签（大小写不敏感）。

    只在**模型没给出有效标签**时兜底 —— 绝不静默退化成「公共」。

    类目入选门槛：命中 ≥2 个，或至少命中一个强关键词（中文词 / 长词）。
    一个孤立的短缩写不足以定类 —— 实测 arXiv 语料里 `TAS`（TSN 的 Time-Aware Shaper）
    会撞上论文自带的 **Gen-TAS**，「原子尺度噪声」那篇也因此被错标成「接入」。
    宁可交还给「公共」这个诚实的通用位置，也不要报一个看着很确定的错标。

    命中多个类目时按命中数降序（并列按 BUSINESS_TAGS 声明顺序），取前 ``limit`` 个。
    """
    text = " ".join(str(p or "") for p in parts).lower()
    if not text.strip():
        return []
    hits: list[tuple[int, str]] = []
    for tag, words in BUSINESS_TAG_KEYWORDS.items():
        matched = [w for w in words if _kw_hit(w, text)]
        if len(matched) >= 2 or any(_kw_strong(w) for w in matched):
            hits.append((len(matched), tag))
    hits.sort(key=lambda x: (-x[0], BUSINESS_TAGS.index(x[1])))
    return [t for _, t in hits[:limit]]


# ── 四段可编辑提示词（默认值 = 旧脚本原文）──────────────────────────

DEFAULT_SCORING = (
    """你是芯片与半导体产业技术分析师。请逐篇评估以下文章/论文，重点考察硬件/架构/工艺层面的实质创新，并按五类之一归档领域。

评分维度参考：
- 硬件/架构/工艺层面的实质创新性（这是主要权重）
- 是否有可验证的量化结果或工程实现
- 与半导体/芯片硬件技术的相关性（纯软件、纯市场新闻不相关）
- 业务领域归属（公共 / 数通 / 传送 / 接入 / 芯片硬件），判定标准见下

【业务领域判定标准】
"""
    + BUSINESS_TAG_STANDARD
    + """

入选理由要具体：写明关键数据或创新点，不要写「值得一读」这类空话。"""
)

DEFAULT_DETAIL = """你是芯片与半导体领域技术专家。请深度解读以下文章/论文：

- 关键技术创新：100-150 字，分号分隔多个要点，讲清楚「做了什么、怎么做的、相比已有方案强在哪」
- 数据与实验结果：列出文中关键量化指标，格式「数值: 含义；数值: 含义」；
  没有量化数据时写「文中未给出量化数据，以定性分析为主」，不要编造数字
- 详细摘要：200-300 字，按「背景 → 方法 → 结果 → 意义」组织"""

DEFAULT_INTRO = """你是芯片与半导体产业技术战略专家。基于本期精选文章的标题、领域与创新点，撰写一段 200-300 字的中文「核心洞察与摘要」作为周报开篇。

要求：
- 开头点明本期的时间范围与精选篇数，以及覆盖了哪些技术方向
- 随后用 ① ② ③ ④ ⑤ 分方向列出 3-5 条看点，每条点出代表性成果与关键数据
- 每条看点单独一行，点与点之间不要空行
- 不要使用 Markdown 加粗（**），标题与正文都写纯文本"""

DEFAULT_BRIEF = """请为以下每篇文章/论文写一句 50-100 字的中文摘要，突出方法创新与关键量化数据。

要求：
- 一句话讲清楚做了什么、关键结果是什么
- 有量化数据就写进去；没有就不要编
- 不要写「本文研究了」这类填充语，直接说内容"""

_PROMPT_DEFAULTS: dict[str, str] = {
    "scoring": DEFAULT_SCORING,
    "detail": DEFAULT_DETAIL,
    "intro": DEFAULT_INTRO,
    "brief": DEFAULT_BRIEF,
}

PROMPT_KEYS = tuple(_PROMPT_DEFAULTS)
PROMPT_LABELS = {
    "scoring": "选题打分",
    "detail": "深度解读",
    "intro": "核心洞察",
    "brief": "其他入选摘要",
}

# 代码固定的输出约束：**不可编辑**，永远拼在用户文本之后。
# 与 ai_filter.FIXED_OUTPUT_REQUIREMENTS 同一用意 —— 用户改标准不会把解析改崩。
FIXED_OUTPUT: dict[str, str] = {
    "scoring": """【输出格式（软件固定，不可更改）】
只输出严格 JSON 对象（不要 Markdown 代码块、不要任何多余文字）：
{"items":[{"idx":0,"score":8.5,"semiconductor":true,"title_cn":"中文标题","domain":"五选一领域","business_tags":["公共"],"reason":"30-60字入选理由"}]}
要求：
- idx 必须与输入编号（【第 N 篇】里的 N）一一对应，不能漏项、不能改序；
- 输入可能是多篇：必须**逐篇**输出对应记录，不允许把两篇合并成一条、
  也不允许只答其中一部分；
- score 为 1-10 浮点，可一位小数；
- semiconductor 为 true/false，纯软件/纯市场新闻为 false；
- domain 必须严格取自这五个之一：AI芯片架构与推理优化 / FPGA/可编程计算与架构 / 芯片互联与存储架构 / 处理器安全与可信架构 / 半导体制造与先进封装；
- business_tags 严格取自 ["公共","数通","传送","接入","芯片硬件"] 这五个字面量（不要自造新词），
  按上面「业务领域判定标准」的场景定义判断，1-3 个；判不准就给 1 个最贴近的；
- title_cn：英文标题给准确中文译名，中文标题原样返回。""",
    "detail": """【输出格式（软件固定，不可更改）】
只输出严格 JSON 对象（不要 Markdown 代码块、不要任何多余文字）：
{"key_innovation":"100-150字，分号分隔多个要点","data_results":"数值: 含义；数值: 含义","summary":"200-300字"}""",
    # 核心洞察要的是成稿文字而不是 JSON —— 交给模板前由代码清洗成 HTML。
    # ⚠️ 这段文案**刻意不出现 "JSON" 字样**：DeepSeek 要求 response_format=json_object
    # 时 prompt 必须含 "json"，早先那句「不要 JSON」里的 "JSON" 恰好满足了它的门槛，
    # 于是模型被迫回 {"content": "…"}，整包进了报告（2026-09 事故）。
    # 根因已在调用侧关掉 json_mode，这里再拆掉陷阱，别让后来人重新踩上。
    "intro": """【输出格式（软件固定，不可更改）】
只输出这段中文正文本身 —— 不要任何包装（不要对象、不要字段名）、不要 Markdown、
不要解释性前后缀。第一行就是正文第一句。""",
    "brief": """【输出格式（软件固定，不可更改）】
只输出严格 JSON 对象（不要 Markdown 代码块、不要任何多余文字）：
{"items":[{"idx":0,"brief":"50-100字中文摘要"}]}
要求：idx 必须与输入编号一一对应，不能漏项、不能改序。""",
}


def build_prompt(key: str, text: str | None = None) -> str:
    """完整提示词 = 用户可编辑标准 + 软件固定输出约束。"""
    body = (text if text is not None else _PROMPT_DEFAULTS.get(key, "")).strip()
    if not body:
        body = _PROMPT_DEFAULTS.get(key, "")
    return f"{body}\n\n{FIXED_OUTPUT.get(key, '')}".strip()


def prompt_fingerprint(key: str, text: str | None = None) -> str:
    """提示词指纹：缓存键的前缀。改一个字就换一个键 —— 该阶段缓存自动失效。"""
    return hashlib.sha256(build_prompt(key, text).encode("utf-8")).hexdigest()[:8]


# ── 提示词持久化 ──────────────────────────────────────────────────


def load_prompts(path: str | Path) -> dict[str, str]:
    """读用户自定义提示词；缺失/损坏的键回落到内置默认（绝不抛异常）。"""
    out = dict(_PROMPT_DEFAULTS)
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return out
    if not isinstance(raw, dict):
        return out
    for k in PROMPT_KEYS:
        v = raw.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v
    return out


def save_prompts(path: str | Path, prompts: dict[str, str]) -> bool:
    """原子写；只保留认识的键。"""
    p = Path(path)
    keep = {k: str(prompts.get(k) or "") for k in PROMPT_KEYS}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(keep, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
        return True
    except Exception:  # noqa: BLE001
        return False


def prompts_payload(path: str | Path) -> dict[str, dict[str, str]]:
    """给前端：每段的当前文本 + 内置默认（供「恢复默认」）。"""
    current = load_prompts(path)
    return {
        k: {"text": current.get(k, ""), "default": _PROMPT_DEFAULTS[k], "label": PROMPT_LABELS[k]}
        for k in PROMPT_KEYS
    }


# ── 模型调用（严格 JSON）──────────────────────────────────────────


def _extract_json(text: str) -> Any:
    """从模型回复里抠 JSON：剥代码围栏，容忍前置废话。"""
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.S).strip()
    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        pass
    # 容错：从第一个 { / [ 取到最后一个 } / ]
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        i, j = s.find(open_ch), s.rfind(close_ch)
        if i != -1 and j > i:
            try:
                return json.loads(s[i : j + 1])
            except Exception:  # noqa: BLE001
                continue
    raise ValueError("模型回复里找不到可解析的 JSON")


def llm_json(
    cfg: Any,
    system_prompt: str,
    user_content: str,
    *,
    max_tokens: int = 6000,
    timeout: float = 300,
    retries: int = 1,
    validate: Callable[[Any], str | None] | None = None,
) -> Any:
    """调用模型并解析 JSON；不合格时补一句话重试一次。

    传输层复用 ``ai_filter._call_model``（它自带的 HTTP 重试/限流退避照旧生效），
    这里只负责「拿到的文本 → 结构化数据」这一段。

    ``validate`` 检查解析出来的数据是否**内容**合格（返回 ``None`` 通过，返回字符串
    说明哪里不合格）。这很关键：模型可能返回一个语法完全正确、但漏了必填字段的
    JSON —— 那种情况以前会直接落到关键词兜底，等于放弃了模型对正文的理解。

    重试后仍不合格时**返回那份不合格的数据**而不是抛错，让调用方决定怎么兜底；
    只有从头到尾连 JSON 都没解析出来才抛。
    """
    from mp_harvest.core import ai_filter as ai_mod

    last_err: Exception | None = None
    last_data: Any = None
    for attempt in range(retries + 1):
        prompt = system_prompt
        if attempt:
            why = last_err or "内容不完整"
            prompt = prompt + f"\n\n【重要】上一次回复不合格：{why}。请只输出 JSON 本身，不要任何解释或代码块。"
        text = ai_mod._call_model(cfg, prompt, user_content, max_tokens=max_tokens, timeout=timeout)
        try:
            data = _extract_json(text)
        except Exception as exc:  # noqa: BLE001
            last_err = f"无法解析为 JSON（{exc}）"
            continue
        if validate is not None:
            problem = validate(data)
            if problem:
                # 留着这份「语法对但内容不全」的结果：重试仍不合格时交回调用方，
                # 由它走自己的兜底 —— 总好过把整篇丢掉
                last_data, last_err = data, problem
                continue
        return data
    if last_data is not None:
        return last_data
    raise RuntimeError(f"模型「{getattr(cfg, 'name', '')}」返回的内容无法解析为 JSON：{last_err}")


# 打分阶段的正文窗口。旧脚本 gen_weekly_17.py:183 是 700，照抄了过来，
# 但拿真实语料量过：arXiv 摘要中位数 1474 字、最长 1916，**30% 的领域关键词落在
# 700 字之后**，56 篇里有 10 篇因此判不出领域（全是「截断后判不出 → 全文能判出」，
# 截断只丢信号、从不加噪声）。2000 字覆盖整个摘要长度分布。
# 代价：打分阶段（跑全部候选）的字符量约 2 倍，且有缓存，同一提示词只花一次。
SCORING_TEXT_CHARS = 2000

# 打分阶段每批几篇（1 = 逐篇，等价于 2026-09 改造前的行为）
SCORING_BATCH_DEFAULT = 8
SCORING_BATCH_MAX = 20


def valid_business_tags(rec: dict[str, Any]) -> bool:
    """记录里的 business_tags 是否是至少一个合法标签。"""
    raw = rec.get("business_tags")
    if not isinstance(raw, list):
        return False
    return any(t in BUSINESS_TAGS for t in raw)


# ── 批处理的解析（2026-09 打分提速）──────────────────────────────
#
# 编号一律用**批内下标**（0..n-1），不用全局下标：命中缓存的篇目会被剔除，
# 全局编号在每次重跑里都整体错位，出错时根本没法复现。


def _scoring_max_tokens(n: int) -> int:
    """一批 n 篇的输出预算（**只对 anthropic 生效** —— OpenAI 兼容格式不发 max_tokens）。

    每篇约 130–160 token，给足余量但别无限涨。DeepSeek 的 ``json_object`` 模式
    服务端默认输出上限 4096，这正是批大小默认 8、上限 20 的原因。
    """
    return min(8000, max(1500, 400 * int(n)))


def _extract_items(data: Any) -> list[dict[str, Any]]:
    """模型回复 → 记录数组。``{"items":[…]}`` / 裸数组 / 裸对象三种都认。"""
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return [r for r in data["items"] if isinstance(r, dict)]
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    return [data] if isinstance(data, dict) else []


def _as_idx(v: Any) -> int | None:
    """模型给的编号 → int；认不出返回 None。

    ``bool`` 要**先挡掉** —— 它是 ``int`` 的子类，``True`` 会变成 1，
    于是「写了个 true」会被当成第 1 篇的记录。
    """
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v.strip())
    return None


def _records_by_pos(data: Any, n: int) -> dict[int, dict[str, Any]]:
    """模型回复 → ``{位置: 记录}``。位置 = 发出去时的批内编号（0..n-1）。

    **只认编号，不靠数组顺序** —— 模型打乱顺序、多答、少答都不会错位。
    越界与认不出的编号直接丢，重复编号后者覆盖（与 ``ai_filter`` 的 last-wins 一致）。
    """
    rows = _extract_items(data)
    if n == 1 and len(rows) == 1:
        # 单篇时接受「不带编号」的老写法 —— 与逐篇模式的 _pick_item 严格等价
        return {0: rows[0]}
    recs: dict[int, dict[str, Any]] = {}
    shifted: dict[int, dict[str, Any]] = {}
    for r in rows:
        i = _as_idx(r.get("idx"))
        if i is None:
            continue
        if 0 <= i < n:
            recs[i] = r
        # 同时收一份「左移一位」的候选。注意上界必须含 n：1-based 回复的最后一篇
        # 编号正好是 n，若在这里就按越界丢掉，下面的守卫永远不可能成立
        # （2026-09 实测：守卫连同它要救的情况一起失效）。
        if 1 <= i <= n:
            shifted[i - 1] = r
    # 整批用了 1-based 编号（模型偶尔如此）：**有守卫地**整体左移一位。
    # 守卫要求「0 缺席 且 左移后恰好铺满 0..n-1」——不满足就不动，
    # 绝不做启发式猜测（残缺的 1-based 回复无从判断，宁可按 0-based 处理）。
    if 0 not in recs and set(shifted) == set(range(n)):
        return shifted
    return recs


def _batch_user(batch: list[dict[str, Any]], *, supplement: bool = False) -> str:
    """一批候选 → user message。

    每篇**各自**截断到 ``SCORING_TEXT_CHARS`` —— 对整条 message 截断会让第 2 篇
    之后的正文全丢（有测试钉住）。编号写成 ``【第 N 篇】`` 独立成行，比 JSON
    数组更抗错位、也更省 token；单篇的形状与改造前逐字一致。
    """
    n = len(batch)
    head = (
        f"下面是需要**补充打分**的 {n} 篇（编号从 0 开始）：\n"
        if supplement
        else f"下面共 {n} 篇文章/论文，请逐篇打分。\n"
    )
    head += (
        f"items 里必须给出 idx 0 到 {n - 1} 的**全部**记录："
        "不能合并两篇、不能漏项、不能改序。"
    )
    parts = [head]
    for i, it in enumerate(batch):
        parts.append(
            f"\n\n【第 {i} 篇】\n"
            f"类型:{it['kind']} 来源:{it['source']} 日期:{it['date']}\n"
            f"标题: {it['title']}\n"
            f"正文/摘要: {it['text'][:SCORING_TEXT_CHARS] or '（无正文）'}"
        )
    return "".join(parts)


def _record_of(it: dict[str, Any], rec: dict[str, Any]) -> dict[str, Any]:
    """模型给的单篇记录 → 落库形状（字段整形 + 标签兜底）。

    整批 / 补漏 / 降级逐篇三条路径**共用这一份** —— 各写一遍必然漂移。
    """
    score = _as_float(rec.get("score"), 0.0)
    # 模型给的有效标签优先；一个都没给（漏字段 / 自造词）才降级到关键词匹配 ——
    # 不能静默退化成「公共」，那会把分类错误伪装成正常结果。
    tags = [t for t in (rec.get("business_tags") or []) if t in BUSINESS_TAGS][:3]
    if not tags:
        # 关键词也没命中时，「公共」= 跨领域通用，是诚实的兜底位置
        tags = infer_business_tags(it["title"], it.get("text") or "") or ["公共"]
    domain = str(rec.get("domain") or "")
    return {
        "score": score,
        "semiconductor": _as_bool(rec.get("semiconductor"), True),
        "title_cn": str(rec.get("title_cn") or it["title"]),
        "domain": domain if domain in DOMAINS else DOMAINS[0],
        "business_tags": tags,
        "reason": str(rec.get("reason") or "")[:140],
    }


def _fetch_batch(
    batch: list[dict[str, Any]],
    model: Any,
    system: str,
    *,
    check_cancelled: Callable[[], None] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """一批候选 → ``({文章键: 记录}, [没拿到记录的篇目说明])``。

    三级阶梯（正常情况只走 L1 一次请求）::

      L1 整批一次。批级 validate 只做**结构性**判断 —— 有没有任何一条能对上编号。
         整批不可用 → llm_json 自己带原因重发一次（此刻不存在会被浪费的已付费成果）；
         仍不可用且 n>1 → **降级逐篇**（就是改造前那条路径，输出预算也还给每一篇）。
      L2 漏答 ∪ 标签非法的位置拼成 mini-batch，**重新从 0 编号**再问一次。
      L3 仍不合法 → 标签走关键词兜底；仍漏答 → 记 error、**不写缓存**。

    返回的字典里**只有模型明确给出了记录的篇目** —— 调用方据此写缓存
    （一篇写缓存的充要条件就是这个，否则一次漏答会把文章永久拉黑）。

    **传输失败（``ModelCallError``）不降级、不补漏**：``_call_model`` 内部已经退避
    重试过，再拿 N 倍请求去打同一个坏端点（每次还带 5s/10s 睡眠）会让整期卡死。
    补偿机制是「重跑很便宜」—— 成功的批都已进缓存，重跑只剩失败那几篇。
    """
    n = len(batch)
    if not n:
        return {}, []

    def _title(it: dict[str, Any]) -> str:
        return str(it.get("title") or "")[:40]

    out: dict[str, Any] = {}
    errors: list[str] = []

    def ask(
        items: list[dict[str, Any]],
        *,
        supplement: bool = False,
        check_tags: bool = False,
        retries: int = 1,
    ) -> dict[int, dict[str, Any]]:
        """问一次 → ``{items 内的下标: 记录}``（位置相对 items）。"""
        m = len(items)
        user = _batch_user(items, supplement=supplement)

        def _validate(d: Any) -> str | None:
            recs = _records_by_pos(d, m)
            if not recs:
                return "没有任何一条记录能对上输入的编号"
            if check_tags:
                bad = sorted(i for i, r in recs.items() if not valid_business_tags(r))
                if bad:
                    return f"idx {bad} 的 business_tags 缺失或不在允许的五个取值内"
            return None

        data = llm_json(
            model, system, user,
            max_tokens=_scoring_max_tokens(m), retries=retries, validate=_validate,
        )
        return _records_by_pos(data, m)

    # ── L1：整批一次 ─────────────────────────────────────────────
    if check_cancelled:
        check_cancelled()
    try:
        pos = ask(batch)
    except ModelCallError as exc:
        # 传输层失败：**不降级、不补漏**（拆成 N 个小请求只会把同一个坏端点打得更狠）
        return {}, [f"{_title(it)}：{exc}" for it in batch]
    except Exception:  # noqa: BLE001 —— 解析不出来等：整批不可用
        if n > 1:
            # 降级逐篇。最常见的整批失败成因是**输出被截断**，逐篇恰好把输出预算
            # 还给每一篇；而且这条路径就是改造前那条，行为有既有测试钉着。
            # 逐篇仍失败的**不在这里收尾**，交给下面的 L2/L3 统一处理。
            for it in batch:
                if check_cancelled:
                    check_cancelled()
                try:
                    one = ask([it])
                except Exception:  # noqa: BLE001
                    continue
                if 0 in one:
                    out[it["key"]] = one[0]
    else:
        for i, rec in pos.items():
            out[batch[i]["key"]] = rec

    # ── L2：漏答 ∪ 标签非法的，补一轮 ──────────────────────────────
    #
    # 只补这几篇，**绝不整批重发**：整批重发会让其余已付费的好答案冒被改写/
    # 重排的风险，还白付一遍 token。llm_json 的「带原因重发同一条 user 内容」
    # 对「部分不合格」没有表达力，所以这一轮由我们自己组织（mini-batch 就是
    # 重试集合，重新从 0 编号，它的 validate 语义此时恰好正确）。
    need = [
        it for it in batch if it["key"] not in out or not valid_business_tags(out[it["key"]])
    ]
    if need:
        if check_cancelled:
            check_cancelled()
        try:
            # retries=0：**L2 本身就是那次重试**，再让 llm_json 叠一层内层重试
            # 会变成两轮 —— 实测比改造前的逐篇路径多花一次调用，等于把批处理
            # 省下的钱又还回去一部分。
            pos2 = ask(need, supplement=True, check_tags=True, retries=0)
        except Exception:  # noqa: BLE001 —— 补漏失败就走 L3，不再纠缠
            pos2 = {}
        for i, rec in pos2.items():
            out[need[i]["key"]] = rec

    # ── L3：仍漏答的记一笔（标签不合法不算失败，_record_of 会兜底）──
    for it in batch:
        if it["key"] not in out:
            errors.append(f"{_title(it)}：模型没有给出这一篇的记录")
    return out, errors


def _map_parallel(
    items: list[dict[str, Any]],
    models: list[Any],
    work: Callable[[dict[str, Any], Any], Any],
    *,
    workers: int = 4,
    on_progress: Callable[[int, int], None] | None = None,
    check_cancelled: Callable[[], None] | None = None,
    on_result: Callable[[dict[str, Any], Any], None] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """并发逐篇调用；模型轮询分配（多模型时天然负载均衡）。

    返回 ``({key: 结果}, [错误信息])``。单篇失败**不影响其余篇目** ——
    计进 errors 后继续，调用方按需上报。
    """
    enabled = [m for m in models if getattr(m, "enabled", True)] or list(models)
    if not enabled:
        raise RuntimeError("没有可用的 AI 模型，请先到「AI 模型」页配置并启用")
    results: dict[str, Any] = {}
    errors: list[str] = []
    done = 0
    total = len(items)

    def _guarded(it: dict[str, Any], model: Any) -> Any:
        if check_cancelled:
            check_cancelled()
        return work(it, model)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
        futures = {
            ex.submit(_guarded, it, enabled[i % len(enabled)]): it for i, it in enumerate(items)
        }
        for fut in concurrent.futures.as_completed(futures):
            it = futures[fut]
            try:
                val = fut.result()
                results[it["key"]] = val
                if on_result:
                    on_result(it, val)
            except Exception as exc:  # noqa: BLE001
                # 取消要原样抛出（TaskCancelled），其余按单篇失败处理
                if exc.__class__.__name__ == "TaskCancelled":
                    raise
                errors.append(f"{str(it.get('title') or '')[:40]}：{exc}")
            done += 1
            if on_progress:
                on_progress(done, total)
    return results, errors


# ── 候选收集 ──────────────────────────────────────────────────────


def normalize_wechat(row: dict[str, Any], *, source_name: str = "") -> dict[str, Any] | None:
    """公众号缓存行 → 候选。缺标题的丢掉。"""
    title = str(row.get("title") or "").strip()
    link = str(row.get("link") or "").strip()
    if not title and not link:
        return None
    ts = int(row.get("publish_ts") or 0)
    return {
        # 键必须有兜底：真实缓存行都带 identity，但缺了它又缺 link 时会算出
        # "wechat:" 这种空键，多篇候选会撞成同一条（2026-09 修复）
        "key": f"wechat:{row.get('identity') or link or _title_fingerprint(title)}",
        "kind": "公众号",
        "title": title or "(无标题)",
        "source": source_name or str(row.get("account") or ""),
        "date": _date_of(ts, str(row.get("publish_at") or "")),
        "publish_ts": ts,
        "publish_at": str(row.get("publish_at") or ""),
        "url": link,
        "text": str(row.get("body_text") or row.get("digest") or "").strip(),
        "body_html": str(row.get("body_html") or ""),
        "body_text": str(row.get("body_text") or ""),
    }


def normalize_external(item: dict[str, Any]) -> dict[str, Any] | None:
    """外部来源条目 → 候选。正文优先用本地正文文件，退回中文摘要/英文摘要。"""
    title = str(item.get("title") or "").strip()
    url = str(item.get("url") or "").strip()
    if not title and not url:
        return None
    ts = int(item.get("publish_ts") or 0)
    authors = item.get("authors") or []
    first_author = str(authors[0]) if authors else ""
    body = read_external_body(item)
    return {
        "key": f"ext:{item.get('item_key') or url or _title_fingerprint(title)}",
        "kind": "arXiv" if item.get("arxiv_id") else "外部",
        "arxiv_id": str(item.get("arxiv_id") or ""),
        "title": title or "(无标题)",
        "title_cn": str(item.get("title_cn") or ""),
        "source": ("arXiv · " + first_author) if first_author else str(item.get("source_name") or ""),
        "date": _date_of(ts, str(item.get("dir_date") or "")),
        "publish_ts": ts,
        "publish_at": str(item.get("dir_date") or ""),
        "url": url,
        "text": body,
        "body_html": "",
        "body_text": body,
    }


def _title_fingerprint(title: str) -> str:
    """候选键的兜底：标题摘要（identity/链接都缺时不至于让多篇撞成一条）。"""
    return "t" + hashlib.sha1(str(title or "").encode("utf-8", "ignore")).hexdigest()[:12]


def _date_of(ts: int, fallback: str) -> str:
    if ts:
        try:
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001
            pass
    m = re.match(r"(\d{4}-\d{2}-\d{2})", str(fallback or ""))
    return m.group(1) if m else ""


def collect_candidates(
    *,
    wechat_rows: Iterable[tuple[dict[str, Any], str]] = (),
    external_items: Iterable[dict[str, Any]] = (),
    start_ts: int = 0,
    end_ts: int = 0,
) -> list[dict[str, Any]]:
    """按日期窗口收集候选并去重（同 key 只留一条）。

    ``wechat_rows`` 是 ``(文章行, 公众号名)`` 的序列 —— 由调用方从
    ``server.state`` 取（core 不能 import server）；外部条目同理。
    """
    out: dict[str, dict[str, Any]] = {}
    for row, name in wechat_rows:
        c = normalize_wechat(row, source_name=name)
        if c and _in_window(c["publish_ts"], start_ts, end_ts):
            out.setdefault(c["key"], c)
    for item in external_items:
        c = normalize_external(item)
        if c and _in_window(c["publish_ts"], start_ts, end_ts):
            out.setdefault(c["key"], c)
    return sorted(out.values(), key=lambda c: c["publish_ts"], reverse=True)


def _in_window(ts: int, start_ts: int, end_ts: int) -> bool:
    if start_ts and ts and ts < start_ts:
        return False
    if end_ts and ts and ts > end_ts:
        return False
    return True


# ── 缓存 ──────────────────────────────────────────────────────────


def default_cache_path() -> Path:
    from mp_harvest.infra.platform import paths

    return paths.data_dir() / "weekly" / "cache.json"


class WeeklyCache:
    """打分/解读结果的持久缓存（容错：任何故障都不阻断生成）。

    键 = ``提示词指纹 + ":" + 文章键``，所以改提示词只有对应阶段失效。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data: dict[str, dict[str, Any]] = {"scores": {}, "details": {}, "briefs": {}}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for stage in ("scores", "details", "briefs"):
                    if isinstance(raw.get(stage), dict):
                        self._data[stage] = raw[stage]
        except Exception:  # noqa: BLE001
            pass

    def get(self, stage: str, key: str) -> Any:
        with self._lock:
            return self._data.get(stage, {}).get(key)

    def put(self, stage: str, key: str, value: Any) -> None:
        with self._lock:
            self._data.setdefault(stage, {})[key] = value
            self._flush()

    def prune_stage(self, stage: str, keep_prefix: str) -> int:
        """丢掉某阶段里不属于当前提示词指纹的旧条目。

        用户每改一次提示词就换一个指纹，旧条目再也不会被命中 —— 不清的话
        缓存文件会随改动次数无限膨胀。返回清掉的条数。
        """
        with self._lock:
            rows = self._data.get(stage) or {}
            stale = [k for k in rows if not k.startswith(f"{keep_prefix}:")]
            for k in stale:
                rows.pop(k, None)
            if stale:
                self._flush()
            return len(stale)

    def _flush(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False), encoding="utf-8"
            )
            tmp.replace(self.path)
        except Exception:  # noqa: BLE001
            pass


def cache_key(stage: str, prompt_text: str | None, article_key: str) -> str:
    return f"{prompt_fingerprint(stage, prompt_text)}:{article_key}"


# ── 五个阶段 ──────────────────────────────────────────────────────


def score_candidates(
    items: list[dict[str, Any]],
    models: list[Any],
    *,
    prompts: dict[str, str],
    cache: WeeklyCache,
    workers: int = 4,
    batch_size: int = SCORING_BATCH_DEFAULT,
    on_progress: Callable[[int, int], None] | None = None,
    on_stage: Callable[[str], None] | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """逐**批**打分：评分 / 半导体相关性 / 中文译名 / 领域 / 业务标签 / 入选理由。

    两个旋钮互补：**批大小**省请求数（system prompt 每批只付一次），
    **并发**控限流。线程池大小固定等于 ``workers``，**不随语料规模浮动** ——
    否则某周 200 篇候选会一次打出几十个请求，直接撞上模型的速率限制，
    而 ``_call_model`` 的退避没有抖动，多线程会同步睡、同步醒、再一起撞。

    切块在**缓存过滤之后**：命中缓存的篇目不该白占批位，也不该把一批挤成两批。
    ``on_progress`` 报的是**篇数**（不是批数），前端进度条语义不变。
    """
    system = build_prompt("scoring", prompts.get("scoring"))
    prompt_text = prompts.get("scoring")
    cached: dict[str, Any] = {}
    pending: list[dict[str, Any]] = []
    for it in items:
        hit = cache.get("scores", cache_key("scoring", prompt_text, it["key"]))
        if hit is not None:
            cached[it["key"]] = hit
        else:
            pending.append(it)
    if not pending:
        return cached, []

    enabled = [m for m in models if getattr(m, "enabled", True)] or list(models)
    if not enabled:
        raise RuntimeError("没有可用的 AI 模型，请先到「AI 模型」页配置并启用")

    step = max(1, int(batch_size))
    concurrency = max(1, int(workers))
    chunks = [pending[i : i + step] for i in range(0, len(pending), step)]
    if on_stage:
        on_stage(
            f"打分 {len(pending)} 篇（{len(chunks)} 批 × 并发 {concurrency}"
            f"，每批 {step} 篇）…"
        )

    errors: list[str] = []
    done = 0
    total = len(pending)
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {
            # 一批只发给一个模型；多模型时按**批序号**轮询（负载均衡的粒度从
            # 每 k 篇变成每 k 批，篇数误差 ±(batch_size-1)，可接受）
            ex.submit(
                _fetch_batch, chunk, enabled[b % len(enabled)], system,
                check_cancelled=check_cancelled,
            ): chunk
            for b, chunk in enumerate(chunks)
        }
        for fut in concurrent.futures.as_completed(futures):
            chunk = futures[fut]
            try:
                got, errs = fut.result()
            except Exception as exc:  # noqa: BLE001
                # 取消要原样抛出（TaskCancelled），其余按整批失败处理
                if exc.__class__.__name__ == "TaskCancelled":
                    raise
                got, errs = {}, [f"{str(chunk[0].get('title') or '')[:40]}：{exc}"]
            errors.extend(errs)
            for it in chunk:
                rec = got.get(it["key"])
                if rec is None:
                    continue  # 没拿到记录就**不写缓存** —— 漏答不能把文章永久拉黑
                val = _record_of(it, rec)
                cached[it["key"]] = val
                cache.put("scores", cache_key("scoring", prompt_text, it["key"]), val)
            done += len(chunk)
            if on_progress:
                on_progress(done, total)
            if on_stage:
                on_stage(f"打分 {min(done, total)}/{total} 篇…")
    return cached, errors


def analyze_selected(
    selected: list[dict[str, Any]],
    models: list[Any],
    *,
    prompts: dict[str, str],
    cache: WeeklyCache,
    workers: int = 4,
    on_progress: Callable[[int, int], None] | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Top N 逐篇深度解读：关键技术创新 / 数据与实验结果 / 详细摘要。"""
    system = build_prompt("detail", prompts.get("detail"))
    pending = []
    cached: dict[str, Any] = {}
    for it in selected:
        k = cache_key("detail", prompts.get("detail"), it["key"])
        hit = cache.get("details", k)
        if hit is not None:
            cached[it["key"]] = hit
        else:
            pending.append((it, k))

    def work(it: dict[str, Any], model: Any) -> dict[str, Any]:
        user = (
            f"标题: {it['title']}\n来源: {it['source']}（{it['kind']}）\n\n"
            f"正文/摘要:\n{it['text'][:6000] or '（无正文，请基于标题做谨慎推断并说明依据有限）'}"
        )
        data = llm_json(model, system, user, max_tokens=6000)
        if not isinstance(data, dict):
            raise ValueError("解读结果不是 JSON 对象")
        return {
            "key_innovation": str(data.get("key_innovation") or ""),
            "data_results": str(data.get("data_results") or ""),
            "summary": str(data.get("summary") or ""),
        }

    fresh, errors = _map_parallel(
        [it for it, _ in pending],
        models,
        work,
        workers=workers,
        on_progress=on_progress,
        check_cancelled=check_cancelled,
        on_result=lambda it, val: cache.put(
            "details", cache_key("detail", prompts.get("detail"), it["key"]), val
        ),
    )
    return {**cached, **fresh}, errors


def brief_others(
    others: list[dict[str, Any]],
    models: list[Any],
    *,
    prompts: dict[str, str],
    cache: WeeklyCache,
    workers: int = 4,
    on_progress: Callable[[int, int], None] | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> tuple[dict[str, str], list[str]]:
    """其余入选文章的一句话摘要（50-100 字）。"""
    system = build_prompt("brief", prompts.get("brief"))
    pending = []
    cached: dict[str, str] = {}
    for it in others:
        k = cache_key("brief", prompts.get("brief"), it["key"])
        hit = cache.get("briefs", k)
        if isinstance(hit, str) and hit.strip():
            cached[it["key"]] = hit
        else:
            pending.append((it, k))

    def work(it: dict[str, Any], model: Any) -> str:
        user = f"标题: {it['title']}\n正文/摘要: {it['text'][:600] or '（无正文）'}"
        data = llm_json(model, system, user, max_tokens=2000)
        rec: dict[str, Any] = {}
        if isinstance(data, dict) and isinstance(data.get("items"), list) and data["items"]:
            rec = data["items"][0]
        elif isinstance(data, dict):
            rec = data
        out = str(rec.get("brief") or "").strip()
        if not out:
            raise ValueError("摘要为空")
        return out

    fresh, errors = _map_parallel(
        [it for it, _ in pending],
        models,
        work,
        workers=workers,
        on_progress=on_progress,
        check_cancelled=check_cancelled,
        on_result=lambda it, val: cache.put(
            "briefs", cache_key("brief", prompts.get("brief"), it["key"]), val
        ),
    )
    return {**cached, **fresh}, errors


def generate_intro(
    selected: list[dict[str, Any]],
    scores: dict[str, Any],
    details: dict[str, Any],
    models: list[Any],
    *,
    prompts: dict[str, str],
    from_date: str,
    to_date: str,
    on_progress: Callable[[str], None] | None = None,
) -> str:
    """本期核心洞察（① ② ③ 编号式）。失败时退回规则拼接的兜底文案。"""
    enabled = [m for m in models if getattr(m, "enabled", True)] or list(models)
    lines = []
    for k, it in enumerate(selected, 1):
        s = scores.get(it["key"]) or {}
        d = details.get(it["key"]) or {}
        lines.append(
            f"{k}. [{s.get('domain', '')}] {s.get('title_cn') or it['title']}："
            f"{str(d.get('key_innovation') or '')[:60]}"
        )
    if on_progress:
        on_progress("生成核心洞察…")
    try:
        if not enabled:
            raise RuntimeError("没有可用的 AI 模型")
        from mp_harvest.core import ai_filter as ai_mod

        system = build_prompt("intro", prompts.get("intro"))
        user = (
            f"本期时间范围：{from_date} 至 {to_date}\n"
            f"精选 {len(selected)} 篇，清单如下：\n" + "\n".join(lines)
        )
        # 核心洞察要的是**成稿文字**，不是 JSON —— 走 llm_json 会永远解析失败、
        # 永远落到兜底文案（2026-09 由测试抓出）。这里直接取文本。
        # **必须 json_mode=False**：否则传输层强制 json_object，模型只能回
        # {"content": "…"}，_clean_intro 处理不了就整包进报告（2026-09 事故根因）。
        text = ai_mod._call_model(
            enabled[0], system, user, max_tokens=4000, timeout=300, json_mode=False
        )
        cleaned = _clean_intro(text)
        return cleaned or _fallback_intro(selected, scores, from_date, to_date)
    except Exception:  # noqa: BLE001
        return _fallback_intro(selected, scores, from_date, to_date)


def _fallback_intro(
    selected: list[dict[str, Any]], scores: dict[str, Any], from_date: str, to_date: str
) -> str:
    """模型不可用时的兜底：按领域拼接，至少不是空白。"""
    doms = sorted({(scores.get(it["key"]) or {}).get("domain", "") for it in selected} - {""})
    return (
        f"本期（{from_date} 至 {to_date}）精选 {len(selected)} 篇，"
        f"覆盖{'、'.join(doms)}等方向。"
    )


# 模型把正文包进对象时，这些字段名最可能装的是正文本身
_INTRO_TEXT_FIELDS = ("content", "text", "intro", "summary", "result", "output", "body")


def _unwrap_intro_text(raw: str) -> str:
    """模型把正文包进 JSON 对象时，把正文取出来。

    根因（``json_object`` 强制）已经在调用侧用 ``json_mode=False`` 关掉了，这里是
    **防御层**：别的模型也可能自作主张包一层，而一旦漏进模板就是满屏乱码 ——
    2026-09 实跑那期核心洞察的 ``{"content": "…"}`` 就是这么来的。

    只认「对象里有一个已知正文字段、且值是字符串」这一种形状；认不出**原样返回** ——
    绝不因为正文本身长得像 JSON 就把它吃掉。
    """
    s = raw.strip()
    if not (s.startswith("{") and s.endswith("}")):
        return raw
    try:
        data = json.loads(s)
    except Exception:  # noqa: BLE001
        # 解析不了的「像 JSON 的东西」（常见成因：正文里带裸换行、尾逗号）——
        # 退一步按字段抠。贪婪匹配到最后一个引号，正好覆盖单字段对象；
        # `,?` 容忍模型爱加的尾逗号。抠不出就原样返回。
        m = re.search(
            r'"(?:' + "|".join(_INTRO_TEXT_FIELDS) + r')"\s*:\s*"(.*)"\s*,?\s*\}\s*$',
            s,
            re.S,
        )
        if not m:
            return raw
        # 这条路径没有 JSON 解码，转义过的换行要手动还原（否则分段全部失效）
        return m.group(1).replace("\\n", "\n").replace('\\"', '"')
    if isinstance(data, dict):
        for k in _INTRO_TEXT_FIELDS:
            v = data.get(k)
            if isinstance(v, str) and v.strip():
                return v
    return raw


def _clean_intro(text: Any) -> str:
    """核心洞察清洗：解包、去 Markdown 加粗、折叠多余空行、换行 → `<br>`（模板里 `| safe`）。"""
    s = str(text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", s, flags=re.S).strip()
    s = _unwrap_intro_text(s)
    s = s.replace("**", "").replace("\r\n", "\n")
    s = re.sub(r"\n\s*\n+", "\n", s)
    return html_mod.escape(s).replace("\n", "<br>\n")


def repair_saved_intro(text: Any) -> str:
    """修掉**历史快照**里已经被转义过的引言（2026-09 事故的一次性数据修复）。

    那批快照的形状是「`_clean_intro` 跑在 JSON 包装上」的产物 —— 引号已经变成
    ``&quot;``，`json.loads` 再也解不开。这里先还原实体、再走一遍正常清洗，
    好让**已生成的往期报告重渲染一次就能修好**，不必重新花钱生成。

    认不出来（正常引言）就**原样返回**，不做任何猜测。
    """
    s = str(text or "")
    unescaped = html_mod.unescape(s).strip()
    if _unwrap_intro_text(unescaped) == unescaped:   # 解不出包装 → 不是那批坏数据
        return s
    return _clean_intro(unescaped)


# ── 渲染 ──────────────────────────────────────────────────────────



def _make_undefined(sink: set[str]):
    import jinja2

    class _Undefined(jinja2.Undefined):
        """记录所有「模板引用了但上下文没给」的名字。

        Jinja 默认把未定义变量渲染成空串 —— 用户把 `{{ARTICLE_NO}}` 敲成
        `{{ARTILCE_NO}}` 时页面只是默默少一块，很难发现。这里收集起来在渲染完成后
        作为警告返回，让模板改动有明确的反馈。
        """

        def _note(self) -> str:
            name = str(self._undefined_name or "")
            if name:
                sink.add(name)
            return ""

        def __str__(self) -> str:  # type: ignore[override]
            return self._note()

        def __iter__(self):  # type: ignore[override]
            self._note()
            return iter(())

        def __bool__(self) -> bool:
            self._note()
            return False

        def __len__(self) -> int:  # type: ignore[override]
            self._note()
            return 0

    return _Undefined


def _make_env(template_dir: Path):
    import jinja2

    sink: set[str] = set()
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(template_dir)),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=_make_undefined(sink),
    )
    env.filters["tag_spans"] = _tag_spans
    env.filters["nl2br"] = _nl2br
    return env, sink


def _tag_spans(tags: Any) -> Any:
    """业务标签 → 彩色 span（颜色规范见模块常量；模板不用手写内联样式）。"""
    from markupsafe import Markup

    if isinstance(tags, str):
        tags = [tags]
    parts = []
    for t in tags or []:
        name = str(t)
        color = TAG_COLORS.get(name)
        if not color:
            continue
        parts.append(
            f'<span style="background-color:{color}; color:#ffffff; font-size:11px; '
            f'font-weight:bold; padding:1px 6px; line-height:1.5;">{html_mod.escape(name)}</span>'
        )
    return Markup("&nbsp;".join(parts))


def _nl2br(text: Any) -> Any:
    from markupsafe import Markup

    s = html_mod.escape(str(text or ""))
    return Markup(s.replace("\r\n", "\n").replace("\n", "<br>\n"))


BUILTIN_TEMPLATE_NAME = "weekly.html"


def resolve_template_dir() -> Path:
    """定位内置模板目录（兼容 PyInstaller 各布局）。

    自带一份探测，而不是复用 ``article_reader._resolve_template_dir``：
    那个函数以 ``article.html`` 为哨兵、又是私有函数，周报模板与它只是「恰好
    同目录」—— 一旦哪天挪了位置或改了哨兵，周报会以一句 ImportError 崩掉。
    """
    import sys

    here = Path(__file__).resolve().parent
    roots: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))
    roots.append(here)
    for r in roots:
        for cand in (r / "templates", r / "mp_harvest" / "core" / "templates"):
            if (cand / BUILTIN_TEMPLATE_NAME).is_file():
                return cand
    return here / "templates"


def render_report(
    context: dict[str, Any],
    *,
    template_path: str | Path | None = None,
) -> dict[str, Any]:
    """按模板渲染周报。

    ``template_path`` 为空用内置模板（``core/templates/weekly.html``）；
    否则加载用户任意路径的模板文件。

    返回 ``{ok, html, missing, error}``：``missing`` 是模板引用了但上下文没提供的
    变量名（拼写错误会在这里暴露，而不是静默渲染成空白）。
    """
    path = str(template_path or "").strip()
    if path:
        p = Path(path).expanduser()
        if not p.is_file():
            return {"ok": False, "html": "", "missing": [], "error": f"模板文件不存在：{p}"}
        tdir, tname = p.parent, p.name
    else:
        tdir, tname = resolve_template_dir(), BUILTIN_TEMPLATE_NAME
        if not (Path(tdir) / tname).is_file():
            return {
                "ok": False, "html": "", "missing": [],
                "error": f"内置模板缺失：{Path(tdir) / tname}",
            }

    try:
        env, sink = _make_env(Path(tdir))
        tpl = env.get_template(tname)
        html_out = tpl.render(**context)
    except Exception as exc:  # noqa: BLE001
        err = f"{exc.__class__.__name__}: {exc}"
        lineno = getattr(exc, "lineno", None)
        if lineno:
            err = f"模板第 {lineno} 行：{getattr(exc, 'message', exc)}"
        return {"ok": False, "html": "", "missing": [], "error": err}

    return {"ok": True, "html": html_out, "missing": sorted(sink), "error": ""}


# ── 上下文组装 ────────────────────────────────────────────────────


def build_context(
    *,
    issue_num: int,
    from_date: str,
    to_date: str,
    selected: list[dict[str, Any]],
    others: list[dict[str, Any]],
    scores: dict[str, Any],
    details: dict[str, Any],
    briefs: dict[str, str],
    intro: str,
    total: int,
    wechat_count: int,
    arxiv_count: int,
    archive_rel: dict[str, str] | None = None,
    org: dict[str, str] | None = None,
    title: str = "逻辑芯片行业洞察快报",
) -> dict[str, Any]:
    """组装模板上下文。字段名即模板契约，改动需同步更新内置模板的说明注释。"""
    rel = archive_rel or {}
    org = org or {}

    def _article(it: dict[str, Any], no: int) -> dict[str, Any]:
        s = scores.get(it["key"]) or {}
        d = details.get(it["key"]) or {}
        title_cn = s.get("title_cn") or it.get("title_cn") or it["title"]
        return {
            "no": no,
            "title": it["title"],
            "title_cn": title_cn,
            "title_en": it["title"] if it["title"] != title_cn else "",
            "source": it["source"],
            "kind": it["kind"],
            "date": it["date"],
            "url": it["url"],
            "domain": s.get("domain", ""),
            "business_tags": s.get("business_tags") or [],
            "reason": s.get("reason", ""),
            "key_innovation": d.get("key_innovation", ""),
            "data_results": d.get("data_results", ""),
            "summary": d.get("summary", ""),
            "brief": briefs.get(it["key"], ""),
            "score": s.get("score", 0),
            "local_file": rel.get(it["key"], ""),
        }

    sel = [_article(it, i) for i, it in enumerate(selected, 1)]
    oth = [_article(it, i) for i, it in enumerate(others, len(selected) + 1)]
    return {
        "issue": {
            "num": issue_num,
            "from_date": from_date,
            "to_date": to_date,
            "generated_at": time.strftime("%Y-%m-%d %H:%M"),
            "title": title,
        },
        "stats": {
            "wechat": wechat_count,
            "arxiv": arxiv_count,
            "total": total,
            "selected_count": len(sel),
            "other_count": len(oth),
        },
        "intro": intro,
        "selected": sel,
        "others": oth,
        "tags": dict(TAG_COLORS),
        "org_name": org.get("name", ""),
        "org_email": org.get("email", ""),
        "archive_url": org.get("archive_url", ""),
    }


# ── 小工具 ────────────────────────────────────────────────────────


def _as_float(v: Any, default: float) -> float:
    try:
        return float(v)
    except Exception:  # noqa: BLE001
        return default


def _as_bool(v: Any, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "1", "yes", "y"):
            return True
        if s in ("false", "0", "no", "n", ""):
            return False
    if v is None:
        return default
    try:
        return bool(v)
    except Exception:  # noqa: BLE001
        return default


def suggest_issue_number(out_dir: str | Path, default: int = 1) -> int:
    """扫描输出目录里已有的「第N期_…」子目录，返回最大期号 + 1。"""
    root = Path(str(out_dir)).expanduser()
    best = 0
    try:
        if root.is_dir():
            for p in root.iterdir():
                m = re.match(r"第(\d+)期", p.name)
                if m and p.is_dir():
                    best = max(best, int(m.group(1)))
    except Exception:  # noqa: BLE001
        pass
    return best + 1 if best else default


def issue_dirname(issue_num: int, to_date: str) -> str:
    """归档目录名：``第17期_2026-09-07``。"""
    return f"第{int(issue_num)}期_{to_date}"


# ── 归档 ──────────────────────────────────────────────────────────


def _text_to_html(text: str) -> str:
    """纯文本 → 段落 HTML。

    外部来源的条目只有摘要文本，没有正文片段；直接交给 ``render_article_html``
    会落进它的 ``<pre>`` 兜底分支（长段落不折行、观感差）。按空行切段落更接近
    公众号文章的排版，归档页看起来才一致。
    """
    out = []
    for block in re.split(r"\n\s*\n+", str(text or "").strip()):
        block = block.strip()
        if block:
            out.append(f"<p>{html_mod.escape(block).replace(chr(10), '<br>')}</p>")
    return "".join(out)


def archive_article(
    candidate: dict[str, Any],
    articles_dir: Path,
    *,
    meta: dict[str, Any],
    download_images: bool = False,
) -> Path:
    """把一篇候选的原文写进归档目录，返回文件路径。

    公众号与外部来源通吃：``render_article_html`` 只认
    ``title/link/publish_at/body_html/body_text`` 五个键。
    """
    from mp_harvest.core.article_reader import (
        _article_content_hash,
        safe_export_filename,
        write_article_export,
    )

    title = str(meta.get("title_cn") or candidate["title"])
    link = candidate.get("url") or ""
    arxiv_id = str(candidate.get("arxiv_id") or "")
    if arxiv_id:
        # arXiv 条目按编号命名，与「其他来源」目录里 {arxiv_id}.pdf 的习惯一致
        stem = re.sub(r'[\\/:*?"<>|\s]+', "_", arxiv_id)
        fname = f"{stem}.html"
    else:
        fname = safe_export_filename(
            title,
            ext="html",
            date=candidate.get("date", ""),
            account=candidate.get("source", ""),
            content_hash=_article_content_hash(link=link, body_text=candidate.get("body_text", "")),
        )
    art = {
        "title": title,
        "link": link,
        "publish_at": str(candidate.get("publish_at") or candidate.get("date") or ""),
        "account": candidate.get("source", ""),
        "body_html": str(candidate.get("body_html") or "")
        or _text_to_html(str(candidate.get("body_text") or "")),
        "body_text": str(candidate.get("body_text") or ""),
    }
    return write_article_export(
        articles_dir / fname,
        art,
        account=candidate.get("source", ""),
        download_images=download_images,
        assets_dir=articles_dir.parent / "assets",
        assets_rel="../assets",
    )


def issue_root(out_dir: str | Path, issue_num: int, to_date: str) -> Path:
    return Path(str(out_dir)).expanduser() / issue_dirname(issue_num, to_date)


def archive_articles(
    candidates: list[dict[str, Any]],
    root: Path,
    *,
    titles: dict[str, str] | None = None,
    reasons: dict[str, str] | None = None,
    download_images: bool = False,
    on_progress: Callable[[str], None] | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """把候选原文逐篇写进 ``<root>/articles/``。单篇失败不阻断整期。

    **必须在渲染周报之前调用** —— 周报里要带指向本地全文的链接（``local_file``），
    而文件名要等归档才知道。

    返回 ``{rel_map, index_rows, dir, archived, errors}``：
    - ``rel_map`` 的路径**相对 root**（供周报正文链接）
    - ``index_rows`` 的 ``file`` **相对 articles/**（供目录页链接，它就住在那一层）
    """
    articles_dir = root / "articles"
    titles = titles or {}
    reasons = reasons or {}
    errors: list[str] = []
    rel_map: dict[str, str] = {}
    index_rows: list[dict[str, Any]] = []
    try:
        articles_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        return {"rel_map": {}, "index_rows": [], "dir": articles_dir,
                "archived": 0, "errors": [f"无法创建归档目录：{exc}"]}

    for it in candidates:
        if check_cancelled:
            check_cancelled()
        title_cn = titles.get(it["key"]) or it.get("title_cn") or it["title"]
        meta = {"title_cn": title_cn}
        try:
            path = archive_article(it, articles_dir, meta=meta, download_images=download_images)
            rel_map[it["key"]] = path.relative_to(root).as_posix()
            index_rows.append(
                {
                    "title": title_cn,
                    "publish_at": it.get("publish_at") or it.get("date") or "",
                    "publish_ts": int(it.get("publish_ts") or 0),
                    "account": it.get("source", ""),
                    # 目录页就在 articles/ 里，链接必须相对它自己 ——
                    # 写成相对 root 的 articles/xxx.html 会 404（2026-09 修复）
                    "file": path.relative_to(articles_dir).as_posix(),
                    "link": it.get("url", ""),
                    "keep": True,
                    "reason": reasons.get(it["key"], ""),
                }
            )
            if on_progress:
                on_progress(f"归档 {str(title_cn)[:24]}")
        except Exception as exc:  # noqa: BLE001
            # 无链接 / 正文取不到：记一笔继续，周报正文不依赖这一步
            errors.append(f"{str(it.get('title'))[:40]}：{exc}")

    return {
        "rel_map": rel_map,
        "index_rows": index_rows,
        "dir": articles_dir,
        "archived": len(rel_map),
        "errors": errors,
    }


def write_report_outputs(
    root: Path,
    *,
    report_html: str,
    context: dict[str, Any],
    scores: dict[str, Any],
    details: dict[str, Any],
    briefs: dict[str, str],
    index_rows: list[dict[str, Any]],
    issue_num: int,
    report_title: str = "逻辑芯片行业洞察快报",
) -> dict[str, Any]:
    """写周报正文 + 数据快照 + 归档目录页。"""
    from mp_harvest.core.article_reader import _render_index_page

    errors: list[str] = []
    data_dir = root / "data"
    report_path = root / f"{report_title}_第{int(issue_num)}期.html"
    try:
        root.mkdir(parents=True, exist_ok=True)
        data_dir.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report_html, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "report_path": str(report_path), "errors": [f"周报写入失败：{exc}"]}

    # 目录页：直接调 _render_index_page（不走 _write_index_from_records ——
    # 那条路从 SQLite 取行会把 publish_at 置空，日期列全是空白）
    try:
        (root / "articles" / "index.html").write_text(
            _render_index_page(index_rows, account_name=f"第{int(issue_num)}期 · 文章归档"),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        errors.append(f"归档目录页生成失败：{exc}")

    # 数据快照：改模板后可直接拿 report.json 重渲染，不必重跑 AI
    try:
        (data_dir / "report.json").write_text(
            json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (data_dir / "scores.json").write_text(
            json.dumps({"scores": scores, "details": details, "briefs": briefs},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        errors.append(f"数据快照写入失败：{exc}")

    return {"ok": True, "report_path": str(report_path), "errors": errors}


def load_saved_context(issue_dir: str | Path) -> dict[str, Any] | None:
    """读回某期归档的渲染上下文（供「改模板后重渲染」，不调 AI）。

    顺带修一下 2026-09 那批被 JSON 包装污染过的 ``intro`` —— 让往期报告
    重渲染就能恢复，不必重新生成（见 :func:`repair_saved_intro`）。
    """
    p = Path(str(issue_dir)).expanduser() / "data" / "report.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("intro"), str):
        data["intro"] = repair_saved_intro(data["intro"])
    return data


# ── 整期编排 ──────────────────────────────────────────────────────


def generate_issue(
    *,
    candidates: list[dict[str, Any]],
    models: list[Any],
    prompts: dict[str, str],
    cache: WeeklyCache,
    out_dir: str | Path,
    issue_num: int,
    from_date: str,
    to_date: str,
    selected_count: int = 15,
    org: dict[str, str] | None = None,
    report_title: str = "逻辑芯片行业洞察快报",
    template_path: str | Path | None = None,
    download_images: bool = False,
    workers: int = 4,
    batch_size: int = SCORING_BATCH_DEFAULT,
    on_stage: Callable[[str], None] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """跑完整一期：打分 → 选题 → 归档 → 解读 → 渲染 → 落盘。

    **先归档后渲染**：周报里要带指向本地全文的链接，文件名得先落地才知道。
    """
    stage = on_stage or (lambda _m: None)
    n = max(1, int(selected_count))

    # 阶段文案由 score_candidates 自己报（它知道会切成几批、并发多少）
    scores, score_errors = score_candidates(
        candidates, models, prompts=prompts, cache=cache, workers=workers,
        batch_size=batch_size, on_progress=on_progress, on_stage=stage,
        check_cancelled=check_cancelled,
    )

    valid = [c for c in candidates if (scores.get(c["key"]) or {}).get("semiconductor", True)]
    valid.sort(key=lambda c: (scores.get(c["key"]) or {}).get("score", 0), reverse=True)
    if not valid:
        return {"ok": False, "error": "没有通过相关性筛选的文章（半导体相关的都为空）"}
    selected, others = valid[:n], valid[n:]

    root = issue_root(out_dir, issue_num, to_date)
    stage(f"归档原文 {len(valid)} 篇…")
    art = archive_articles(
        valid, root,
        titles={c["key"]: (scores.get(c["key"]) or {}).get("title_cn") or c["title"] for c in valid},
        reasons={c["key"]: (scores.get(c["key"]) or {}).get("reason", "") for c in valid},
        download_images=download_images,
        on_progress=None, check_cancelled=check_cancelled,
    )

    stage(f"深度解读 Top{len(selected)}…")
    details, detail_errors = analyze_selected(
        selected, models, prompts=prompts, cache=cache, workers=workers,
        on_progress=on_progress, check_cancelled=check_cancelled,
    )
    stage(f"概括其他入选 {len(others)} 篇…")
    briefs, brief_errors = brief_others(
        others, models, prompts=prompts, cache=cache, workers=workers,
        on_progress=on_progress, check_cancelled=check_cancelled,
    )

    stage("生成核心洞察…")
    intro = generate_intro(
        selected, scores, details, models, prompts=prompts,
        from_date=from_date, to_date=to_date, on_progress=None,
    )

    context = build_context(
        issue_num=issue_num, from_date=from_date, to_date=to_date,
        selected=selected, others=others, scores=scores, details=details,
        briefs=briefs, intro=intro, total=len(valid),
        wechat_count=sum(1 for c in valid if c["kind"] == "公众号"),
        arxiv_count=sum(1 for c in valid if c["kind"] == "arXiv"),
        archive_rel=art["rel_map"], org=org, title=report_title,
    )

    stage("渲染周报…")
    rendered = render_report(context, template_path=template_path)
    if not rendered["ok"]:
        return {
            "ok": False,
            "error": rendered["error"],
            "issue_dir": str(root),
            # 数据快照照写：模板坏了但 AI 的钱已经花了，重渲染时不该再花一遍
            "context": context,
        }

    stage("写入归档…")
    out = write_report_outputs(
        root, report_html=rendered["html"], context=context,
        scores=scores, details=details, briefs=briefs,
        index_rows=art["index_rows"], issue_num=issue_num, report_title=report_title,
    )

    errors = [*score_errors, *detail_errors, *brief_errors, *art["errors"], *out["errors"]]
    return {
        "ok": bool(out["ok"]),
        "issue_dir": str(root),
        "report_path": out.get("report_path", ""),
        "articles_dir": str(art["dir"]),
        "selected": len(selected),
        "others": len(others),
        "total": len(valid),
        "dropped": len(candidates) - len(valid),
        "archived": art["archived"],
        "failed": len(errors),
        "errors": errors[:20],
        # 模板引用了但没提供的变量 —— 改模板时最容易犯的错，显式报出来
        "missing_vars": rendered.get("missing", []),
    }


__all__ = [
    "BUSINESS_TAGS",
    "BUSINESS_TAG_KEYWORDS",
    "BUSINESS_TAG_STANDARD",
    "SCORING_BATCH_DEFAULT",
    "SCORING_BATCH_MAX",
    "infer_business_tags",
    "archive_articles",
    "archive_article",
    "generate_issue",
    "issue_root",
    "load_saved_context",
    "write_report_outputs",
    "TAG_COLORS",
    "DOMAINS",
    "PROMPT_KEYS",
    "PROMPT_LABELS",
    "WeeklyCache",
    "analyze_selected",
    "brief_others",
    "build_context",
    "build_prompt",
    "cache_key",
    "collect_candidates",
    "generate_intro",
    "issue_dirname",
    "llm_json",
    "load_prompts",
    "normalize_external",
    "normalize_wechat",
    "prompt_fingerprint",
    "prompts_payload",
    "repair_saved_intro",
    "render_report",
    "resolve_template_dir",
    "save_prompts",
    "score_candidates",
    "suggest_issue_number",
]
