"""展示层：飞书卡片 JSON 构造（spec 5.4）+ 汇总/列表/统计/帮助文案。

与 feishu_bot 分离：本模块只做格式化，不碰网络，便于离线验证。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Paper
from .queue_service import STATUS_EMOJI, Stats, parse_authors

# 方向 → (emoji, 飞书卡片 header 模板色)（spec 5.4：LLM=blue，具身=green，世界模型=purple，RL=orange，其他=grey）
DIRECTION_META: dict[str, tuple[str, str]] = {
    "LLM": ("🤖", "blue"),
    "具身智能": ("🦾", "green"),
    "世界模型": ("🌍", "purple"),
    "RL": ("🎮", "orange"),
    "其他": ("📄", "grey"),
}
DEFAULT_DIRECTION = "其他"

_ZH_TITLE_RE = re.compile(r"\*\*(.+?)\*\*")
_DIRECTION_RE = re.compile(r"🏷️\s*方向[：:]\s*([^\s🏷️]+)")


@dataclass
class CardMeta:
    zh_title: str
    direction: str


def parse_card_meta(card_text: str, fallback_title: str) -> CardMeta:
    """从 5.3 格式的卡片文本解析中文标题与方向；解析失败走兜底。"""
    zh = _ZH_TITLE_RE.search(card_text or "")
    direction = DEFAULT_DIRECTION
    m = _DIRECTION_RE.search(card_text or "")
    if m:
        raw = m.group(1)
        # 容忍模型输出「具身」「世界模型 」等变体
        for name in DIRECTION_META:
            if name in raw or raw in name:
                direction = name
                break
        else:
            if "具身" in raw:
                direction = "具身智能"
            elif "世界" in raw:
                direction = "世界模型"
    return CardMeta(zh_title=zh.group(1).strip() if zh else fallback_title, direction=direction)


def _authors_line(paper: Paper) -> str:
    authors = parse_authors(paper)
    shown = "、".join(authors[:3]) if authors else "未知"
    suffix = " et al." if len(authors) > 3 else ""
    return f"作者：{shown}{suffix} ｜ {paper.published} ｜ {paper.primary_category or ''}".rstrip(" ｜")


def paper_card_json(paper: Paper) -> dict:
    """5.4 速读卡片：header 按方向配色，body=卡片正文+作者行，底部 3 按钮 + abs 链接。"""
    meta = parse_card_meta(paper.card_text or "", paper.title)
    emoji, template = DIRECTION_META[meta.direction]
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": f"{emoji} {meta.zh_title}"},
        },
        "elements": [
            {"tag": "markdown", "content": paper.card_text or paper.title},
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
        "elements": [{"tag": "markdown", "content": markdown}],
    }


# ---------- 每日汇总（spec 5.6） ----------

def summary_markdown(date_str: str, papers: list[Paper], failed_count: int) -> str:
    if not papers:
        text = "😴 今日 arXiv 无符合关注方向的新论文"
        return text + (f"\n（{failed_count} 篇总结失败）" if failed_count else "")

    dist: dict[str, int] = {}
    for p in papers:
        direction = parse_card_meta(p.card_text or "", p.title).direction
        dist[direction] = dist.get(direction, 0) + 1
    order = ["LLM", "具身智能", "世界模型", "RL", "其他"]
    short = {"具身智能": "具身"}
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
