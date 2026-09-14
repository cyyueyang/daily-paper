"""展示层：飞书卡片 JSON 构造（spec 5.4）+ 汇总/列表/统计/帮助文案。

与 feishu_bot 分离：本模块只做格式化，不碰网络，便于离线验证。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Paper
from .queue_service import STATUS_EMOJI, Stats, parse_authors

# 方向 → (emoji, 飞书卡片 header 模板色)。8 个核心方向 + 「其他」兜底（含旧数据）
DIRECTION_META: dict[str, tuple[str, str]] = {
    "LLM架构": ("🏗️", "blue"),
    "LLM预训练": ("🧠", "wathet"),
    "LLM中训练": ("🧬", "turquoise"),
    "LLM Infra": ("🛠️", "indigo"),
    "LLM后训练": ("🎯", "violet"),
    "具身智能": ("🦾", "green"),
    "世界模型": ("🌍", "purple"),
    "Omni": ("🎨", "orange"),
    "其他": ("📄", "grey"),
}
DEFAULT_DIRECTION = "其他"

# 方向别名表（按序匹配，前者优先）：兼容大小写/中英文/空格变体与旧版方向值
_DIRECTION_ALIASES: list[tuple[str, list[str]]] = [
    ("LLM Infra", ["infra", "基础设施"]),
    ("LLM预训练", ["预训练", "pretrain", "pre-train", "pretraining"]),
    ("LLM中训练", ["中训练", "midtrain", "mid-train", "midtraining"]),
    ("LLM后训练", ["后训练", "posttrain", "post-train", "posttraining"]),
    ("LLM架构", ["架构", "llm"]),  # 旧值「LLM」归入架构桶
    ("具身智能", ["具身", "embodied"]),
    ("世界模型", ["世界模型", "worldmodel"]),
    ("Omni", ["omni", "全模态", "多模态", "multimodal"]),
]

_ZH_TITLE_RE = re.compile(r"\*\*(.+?)\*\*")
_DIRECTION_RE = re.compile(r"🏷️\s*方向[：:]\s*([^\n🏷️]+)")


@dataclass
class CardMeta:
    zh_title: str
    direction: str


def parse_direction(raw: str) -> str:
    """把方向文本（LLM 输出/旧数据）归一到 DIRECTION_META 的键。"""
    norm = raw.replace(" ", "").lower()
    for name, aliases in _DIRECTION_ALIASES:
        if any(a.replace(" ", "") in norm for a in aliases):
            return name
    return DEFAULT_DIRECTION


def parse_card_meta(card_text: str, fallback_title: str) -> CardMeta:
    """从 5.3 格式的卡片文本解析中文标题与方向；解析失败走兜底。"""
    zh = _ZH_TITLE_RE.search(card_text or "")
    direction = DEFAULT_DIRECTION
    m = _DIRECTION_RE.search(card_text or "")
    if m:
        direction = parse_direction(m.group(1).strip())
    return CardMeta(zh_title=zh.group(1).strip() if zh else fallback_title, direction=direction)


def _authors_line(paper: Paper) -> str:
    authors = parse_authors(paper)
    shown = "、".join(authors[:3]) if authors else "未知"
    suffix = " et al." if len(authors) > 3 else ""
    return f"作者：{shown}{suffix} ｜ {paper.published} ｜ {paper.primary_category or ''}".rstrip(" ｜")


def paper_card_json(paper: Paper) -> dict:
    """5.4 速读卡片：header 按方向配色，body=卡片正文+作者行，底部 3 按钮 + abs 链接。"""
    meta = parse_card_meta(paper.card_text or "", paper.title)
    direction = paper.direction or meta.direction  # 优先用语义闸门判定的方向
    emoji, template = DIRECTION_META.get(direction, DIRECTION_META[DEFAULT_DIRECTION])
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": f"{emoji} {meta.zh_title}"},
        },
        "elements": [
            {"tag": "markdown", "content": sanitize_math(paper.card_text or paper.title)},
            {"tag": "note", "elements": [{"tag": "plain_text", "content": _authors_line(paper)}]},
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "📖 详细"},
                        "type": "primary",
                        "value": {"action": "detail", "arxiv_id": paper.arxiv_id},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "⏭️ 跳过"},
                        "type": "default",
                        "value": {"action": "skip", "arxiv_id": paper.arxiv_id},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "⭐ 收藏"},
                        "type": "default",
                        "value": {"action": "star", "arxiv_id": paper.arxiv_id},
                    },
                ],
            },
            {
                "tag": "markdown",
                "content": f"[🔗 abs 页面]({paper.abs_url}) ｜ 回复 详细 / 跳过 / 收藏 亦可",
            },
        ],
    }


def simple_card_json(title: str, markdown: str, template: str = "blue") -> dict:
    """汇总/列表/统计/帮助/深度解读共用的通用卡片。"""
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": template, "title": {"tag": "plain_text", "content": title}},
        "elements": [{"tag": "markdown", "content": sanitize_math(markdown)}],
    }


# ---------- LaTeX 公式 → Unicode 纯文本（飞书卡片不渲染 LaTeX） ----------

_MACROS = {
    # 希腊字母
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
    "varepsilon": "ε", "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "ϑ",
    "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ",
    "pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ", "upsilon": "υ", "phi": "φ",
    "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ",
    "Pi": "Π", "Sigma": "Σ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
    # 符号与算子
    "int": "∫", "iint": "∬", "sum": "∑", "prod": "∏", "oint": "∮",
    "in": "∈", "notin": "∉", "subset": "⊂", "subseteq": "⊆", "supset": "⊃",
    "cup": "∪", "cap": "∩", "times": "×", "cdot": "·", "cdots": "⋯", "ldots": "…",
    "pm": "±", "mp": "∓", "leq": "≤", "leqslant": "≤", "geq": "≥", "geqslant": "≥",
    "neq": "≠", "ne": "≠", "approx": "≈", "sim": "∼", "simeq": "≃", "propto": "∝",
    "equiv": "≡", "ll": "≪", "gg": "≫", "infty": "∞", "partial": "∂", "nabla": "∇",
    "forall": "∀", "exists": "∃", "rightarrow": "→", "to": "→", "leftarrow": "←",
    "Rightarrow": "⇒", "Leftarrow": "⇐", "mapsto": "↦", "circ": "∘",
    "oplus": "⊕", "otimes": "⊗", "odot": "⊙", "ell": "ℓ", "prime": "′",
    "angle": "∠", "mid": "|", "vert": "|",
    # 函数名（去反斜杠即可）
    "min": "min", "max": "max", "arg": "arg", "log": "log", "ln": "ln",
    "exp": "exp", "sup": "sup", "inf": "inf", "lim": "lim", "det": "det",
    "dim": "dim", "sin": "sin", "cos": "cos", "tan": "tan",
}

_DOUBLE_STRUCK = {"R": "ℝ", "E": "𝔼", "N": "ℕ", "Z": "ℤ", "P": "ℙ", "Q": "ℚ", "C": "ℂ"}

_SUP = dict(zip(
    "0123456789+-=()niabcdefghijklmoprstuvwxyz",
    "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱᵃᵇᶜᵈᵉᶠᵍʰⁱʲᵏˡᵐᵒᵖʳˢᵗᵘᵛʷˣʸᶻ",
))
_SUP.update({"'": "′", "′": "′", "ℓ": "ˡ"})
# 大写上标（Unicode 只有这些）
_SUP.update(dict(zip("ABDEGHIJKLMNOPRTUVW", "ᴬᴮᴰᴱᴳᴴᴵᴶᴷᴸᴹᴺᴼᴾᴿᵀᵁⱽᵂ")))
_SUB = dict(zip(
    "0123456789+-=()aehijklmnoprstuvx",
    "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ",
))
_SUB.update({"ℓ": "ₗ"})

_MATH_RE = re.compile(
    r"\$\$(.+?)\$\$|\$([^\s$][^$]*?)\$|\\\[(.+?)\\\]|\\\((.+?)\\\)", re.DOTALL
)
_MACRO_RE = re.compile(r"\\([A-Za-z]+)")


def _to_script(content: str, table: dict, marker: str) -> str:
    """上下标转 Unicode；有字符映射不了则整体回退为 ^(…) / _(…) 保持可读。"""
    content = content.strip().replace(" ", "")  # 上下标内部不需要空格
    if content and all(c in table for c in content):
        return "".join(table[c] for c in content)
    return f"{marker}({content})"


def _frac_repl(m: re.Match) -> str:
    num, den = m.group(1).strip(), m.group(2).strip()
    simple = re.compile(r"[\wΑ-Ωα-ω]+", re.UNICODE)
    if simple.fullmatch(num) and simple.fullmatch(den):
        return f"{num}/{den}"
    return f"({num})/({den})"


def _convert_math(s: str) -> str:
    # 1) 尺寸修饰、间距、双竖线
    s = re.sub(r"\\(?:left|right|big|Big|bigg|Bigg)\s*([\[\](){}\|.])", r"\1", s)
    s = re.sub(r"\\[,;:!]", " ", s)
    s = s.replace(r"\|", "‖")
    # 2) 带括号的格式命令先剥壳（必须在宏替换之前，否则被宏吃掉反斜杠）
    s = re.sub(r"\\(?:text|mathrm|mathbf|textbf|mathit|mathsf|operatorname|mathcal)\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"\\mathbb\{([A-Za-z])\}", lambda m: _DOUBLE_STRUCK.get(m.group(1), m.group(1)), s)
    # 3) 已知宏替换；未知宏保留反斜杠（可能是重音命令，留给下一步）
    s = _MACRO_RE.sub(lambda m: _MACROS.get(m.group(1), m.group(0)), s)
    # 4) 重音符号 → 组合字符（支持 \hat{x} 与 \hat x 两种写法）
    for cmd, comb in (("hat", "̂"), ("bar", "̄"), ("tilde", "̃"), ("dot", "̇"), ("ddot", "̈"), ("vec", "⃗")):
        s = re.sub(
            r"\\" + cmd + r"\s*(?:\{([^{}]{1,2})\}|([A-Za-z0-9Α-ω]))",
            lambda m, c=comb: (m.group(1) or m.group(2)) + c,
            s,
        )
    # 5) 上下标（先花括号组，再单字符；单字符不含括号，避免回退形式 _(...) 被二次处理）
    s = re.sub(r"\^\{([^{}]*)\}", lambda m: _to_script(m.group(1), _SUP, "^"), s)
    s = re.sub(r"_\{([^{}]*)\}", lambda m: _to_script(m.group(1), _SUB, "_"), s)
    s = re.sub(r"\^([A-Za-z0-9+\-=ℓ′'])", lambda m: _to_script(m.group(1), _SUP, "^"), s)
    s = re.sub(r"_([A-Za-z0-9+\-=ℓ'])", lambda m: _to_script(m.group(1), _SUB, "_"), s)
    # 6) 分数与根号
    s = re.sub(r"\\[cdt]?frac\{([^{}]*)\}\{([^{}]*)\}", _frac_repl, s)
    s = re.sub(r"\\sqrt\{([^{}]*)\}", r"√(\1)", s)
    # 7) 残留未知宏去反斜杠、花括号剥壳；紧连字符间的连字符→减号
    s = _MACRO_RE.sub(lambda m: m.group(1), s)
    s = re.sub(r"\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"(?<=[\w)\]₀-₉ⱼ])-(?=[\w(\[])", "−", s)
    # 8) 空白收敛
    return re.sub(r"[ \t]+", " ", s).strip()


def sanitize_math(text: str) -> str:
    """把文本中的 LaTeX 数学段（$…$/$$…$$/\\(…\\)/\\[…\\]）转成 Unicode 纯文本。

    飞书卡片不渲染 LaTeX；无数学段时原样返回（幂等，可重复调用）。
    """
    if "$" not in text and "\\(" not in text and "\\[" not in text:
        return text

    def repl(m: re.Match) -> str:
        inner = next(g for g in m.groups() if g is not None)
        converted = _convert_math(inner)
        return f"\n{converted}\n" if m.group(0).startswith(("$$", "\\[")) else converted

    return _MATH_RE.sub(repl, text)


# ---------- 每日汇总（spec 5.6） ----------

def summary_markdown(date_str: str, papers: list[Paper], failed_count: int) -> str:
    if not papers:
        text = "😴 今日 arXiv 无符合关注方向的新论文"
        return text + (f"\n（{failed_count} 篇总结失败）" if failed_count else "")

    dist: dict[str, int] = {}
    for p in papers:
        direction = p.direction or parse_card_meta(p.card_text or "", p.title).direction
        dist[direction] = dist.get(direction, 0) + 1
    order = list(DIRECTION_META.keys())
    short = {
        "LLM架构": "架构", "LLM预训练": "预训练", "LLM中训练": "中训练",
        "LLM后训练": "后训练", "LLM Infra": "Infra", "具身智能": "具身",
    }
    dist_str = " · ".join(f"{short.get(d, d)}×{dist[d]}" for d in order if d in dist)

    lines = [f"今日新增 {len(papers)} 篇（{dist_str}）", ""]
    for p in papers[:3]:
        meta = parse_card_meta(p.card_text or "", p.title)
        lines.append(f"· {meta.zh_title}")
        lines.append(f"  `# {p.matched_keyword}`")
    if len(papers) > 3:
        lines.append(f"· …等共 {len(papers)} 篇")
    if failed_count:
        lines.append(f"\n⚠️ {failed_count} 篇总结失败")
    lines.append("\n回复「开始」逐篇速读 ｜ 回复「列表」看全部")
    return "\n".join(lines)


# ---------- 列表 / 统计 / 帮助 ----------

def list_markdown(papers: list[Paper]) -> str:
    if not papers:
        return "📭 队列空空如也，等明日日报吧"
    lines = []
    for i, p in enumerate(papers, 1):
        emoji = "⭐" if p.starred else STATUS_EMOJI.get(p.status, "❓")
        meta = parse_card_meta(p.card_text or "", p.title)
        lines.append(f"{i}. {emoji} {meta.zh_title}")
        lines.append(f"   `{p.arxiv_id}` ｜ {p.published} ｜ 关键词：{p.matched_keyword}")
    lines.append("\n⏳pending ｜ 📖在读 ｜ ✅read ｜ ⏭skipped ｜ ⭐starred")
    return "\n".join(lines)


def stats_markdown(stats: Stats) -> str:
    return (
        f"📊 累计统计\n"
        f"· 总抓取：{stats.total_fetched} 篇（命中关键词 {stats.total_hit} 篇）\n"
        f"· 待读：{stats.pending} 篇 ｜ 已读：{stats.read} 篇 ｜ 收藏：{stats.starred} 篇\n"
        f"· DeepSeek 累计消耗：{stats.total_tokens} tokens"
    )


HELP_MARKDOWN = """📚 可用指令

· **开始 / 下一条 / next / n** — 发送下一篇速读卡片
· **详细 / detail / d** — 当前论文的深度解读（PDF 全文），读完自动刷下一篇
· **跳过 / skip / s** — 跳过当前篇，自动发下一篇
· **收藏 / star / fav** — 收藏当前篇
· **列表 / list / ls** — 今日队列概览
· **统计 / stats** — 累计统计与 token 用量
· **帮助 / help / ?** — 本说明

卡片上的按钮（详细 / 跳过 / 收藏）与文字指令等价。
深读或跳过后会自动发下一篇，一直刷到 🎉 队列清空。"""


# ---------- 长文本分片（FR-6：飞书单条消息约 4000 字符限制） ----------

FEISHU_TEXT_LIMIT = 3500


def chunk_text(text: str, limit: int = FEISHU_TEXT_LIMIT) -> list[str]:
    """按换行边界分片，每片 <= limit 字符。"""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.splitlines(keepends=True):
        if current_len + len(line) > limit and current:
            chunks.append("".join(current))
            current, current_len = [], 0
        # 单行超限则硬切
        while len(line) > limit:
            if current:
                chunks.append("".join(current))
                current, current_len = [], 0
            chunks.append(line[:limit])
            line = line[limit:]
        current.append(line)
        current_len += len(line)
    if current:
        chunks.append("".join(current))
    return chunks
