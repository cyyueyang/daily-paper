"""FR-2 / FR-6 DeepSeek 调用（openai SDK 指向 DeepSeek，OpenAI 兼容协议）。

- 速读卡片：deepseek-chat，max_tokens=600，prompt 模板见 spec 5.3
- 深度解读：deepseek-chat，max_tokens=2000，prompt 模板见 spec 5.5
- 失败重试：指数退避 2s/4s/8s，最多 3 次；由调用方决定失败后的状态标记
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx
from openai import OpenAI, OpenAIError

from .config import get_settings

logger = logging.getLogger(__name__)

CARD_MAX_TOKENS = 600
DETAIL_MAX_TOKENS = 2000
RETRY_DELAYS_S = (2.0, 4.0, 8.0)  # 指数退避，最多 3 次
CALL_INTERVAL_S = 0.5  # 多篇串行生成时的间隔，避免触发限流

# spec 5.3 速读卡片 Prompt 模板（不得改动措辞意图）
CARD_PROMPT = """你是一位资深 AI 研究员，擅长用中文一句话讲清论文核心。请基于以下论文信息输出速读卡片，严格遵守输出格式，不要输出任何额外内容。

标题：{title}
摘要：{abstract}

输出格式（Markdown）：
**{{中文标题（自行翻译，忠实原意）}}**
{{英文原标题}}

🏷️ 方向：{{从 LLM / 具身智能 / 世界模型 / RL / Omni / Infra / 其他 中选一个}}
💡 一句话核心：{{≤50字，讲清这篇论文做了什么、解决了什么}}
🔧 方法要点：{{≤60字}}
✨ 亮点：{{≤40字，相比已有工作的差异}}
📖 值不值得深读：{{值得 / 扫一眼即可 / 可跳过}}——{{≤25字理由}}"""

# spec 5.5 深度解读 Prompt 模板（不得改动措辞意图）
DETAIL_PROMPT = """你是一位资深 AI 研究员。请基于以下论文{source_kind}，用中文输出深度解读，面向有 ML 基础的读者，严格遵守格式。

标题：{title}
{content}

输出格式（Markdown）：
## 研究问题
{{2-3 句：这篇论文要解决什么问题，为什么重要}}
## 核心方法
{{分点，3-5 条：方法的关键设计，必要时写出关键公式/模块名}}
## 实验与结果
{{2-4 句：在什么 benchmark 上、对比哪些 baseline、提升多少；数字必须来自原文，不得编造}}
## 局限与开放问题
{{1-3 条}}
## 与相关工作的关系
{{1-2 句：属于哪条技术路线，和哪些知名工作同谱系}}
## 一句话总结
{{≤40字}}"""


@dataclass
class LLMResult:
    text: str
    tokens: int


class LLMError(RuntimeError):
    """重试耗尽后抛出。"""


_client: OpenAI | None = None


def get_client() -> OpenAI:
    global _client
    if _client is None:
        settings = get_settings()
        if not settings.deepseek_api_key:
            raise LLMError("DEEPSEEK_API_KEY 未配置")
        http_client = (
            httpx.Client(proxy=settings.http_proxy, timeout=60.0)
            if settings.http_proxy
            else httpx.Client(timeout=60.0)
        )
        _client = OpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            http_client=http_client,
            timeout=60.0,
            max_retries=0,  # 重试统一由 _chat 的指数退避负责，避免双层重试叠加
        )
    return _client


def _chat(prompt: str, max_tokens: int) -> LLMResult:
    settings = get_settings()
    client = get_client()
    last_exc: Exception | None = None
    total_attempts = 1 + len(RETRY_DELAYS_S)  # 首次 + 重试 3 次（退避 2s/4s/8s）
    for attempt in range(1, total_attempts + 1):
        try:
            resp = client.chat.completions.create(
                model=settings.deepseek_model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                stream=False,
            )
            text = (resp.choices[0].message.content or "").strip()
            tokens = resp.usage.total_tokens if resp.usage else 0
            return LLMResult(text=text, tokens=tokens)
        except (OpenAIError, httpx.HTTPError) as exc:
            last_exc = exc
            logger.warning("DeepSeek 调用失败（第 %d/%d 次）：%s", attempt, total_attempts, exc)
            if attempt <= len(RETRY_DELAYS_S):
                time.sleep(RETRY_DELAYS_S[attempt - 1])
    raise LLMError(f"DeepSeek 重试 {len(RETRY_DELAYS_S)} 次仍失败：{last_exc}")


def generate_card(title: str, abstract: str) -> LLMResult:
    return _chat(CARD_PROMPT.format(title=title, abstract=abstract), CARD_MAX_TOKENS)


def generate_detail(title: str, content: str, *, is_fulltext: bool) -> LLMResult:
    prompt = DETAIL_PROMPT.format(
        source_kind="全文" if is_fulltext else "摘要",
        title=title,
        content=content,
    )
    return _chat(prompt, DETAIL_MAX_TOKENS)
