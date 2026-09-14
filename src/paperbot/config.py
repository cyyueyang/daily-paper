"""配置加载：.env + pydantic-settings（密钥不落代码）。

所有配置项见 .env.example；类属性默认值即 spec 5.2 节的默认配置。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# FR-1 核心命中词（命中 title 或 abstract 即入选，大小写不敏感）
# 方向：LLM / 具身智能 / 世界模型 / RL / Omni（全模态）/ Infra
DEFAULT_KEYWORDS: list[str] = [
    # LLM
    "large language model",
    "LLM",
    "reasoning",
    "agent",
    "in-context learning",
    "RLHF",
    "MCTS",
    # 具身智能
    "embodied",
    "robot learning",
    "VLA",
    "vision-language-action",
    "diffusion policy",
    # 世界模型
    "world model",
    # RL
    "reinforcement learning",
    # Omni（全模态）
    "omni-modal",
    "omnimodal",
    "any-to-any",
    "multimodal",
    "multi-modal",
    "VLM",
    "vision-language model",
    # Infra
    "KV cache",
    "speculative decoding",
    "inference engine",
    "model serving",
    "distributed training",
    "training framework",
    "quantization",
    "mixture of experts",
    "MoE",
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # DeepSeek
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"

    # 飞书自建应用
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_owner_open_id: str = ""  # 只响应此人消息；也是日报推送目标

    # 抓取配置
    arxiv_categories: str = "cs.CL,cs.LG,cs.AI,cs.RO"
    arxiv_fetch_days: int = 1  # 首次运行（库为空）代码内置回退为 3 天
    arxiv_max_age_days: int = 365  # 只收最近 1 年内论文（新鲜度兜底，正常窗口远小于此）
    daily_push_time: str = "08:00"  # 本地时间 HH:MM
    extra_keywords: str = ""  # 逗号分隔；非空时整体覆盖 DEFAULT_KEYWORDS（支持增删）

    # 可选代理（arXiv 偶发 429 时使用，参考 elena 仓库 README 的已知坑）
    http_proxy: str | None = None

    @field_validator("http_proxy", mode="before")
    @classmethod
    def _empty_proxy_to_none(cls, v):
        # .env 里写 HTTP_PROXY=（空值）时视为未配置，避免 httpx 报 Unknown scheme
        return v or None

    # 运行期路径（相对 CWD）
    data_dir: Path = Path("data")
    log_dir: Path = Path("logs")
    db_path: Path = Path("data/papers.db")

    @property
    def categories(self) -> list[str]:
        return [c.strip() for c in self.arxiv_categories.split(",") if c.strip()]

    @property
    def keywords(self) -> list[str]:
        # SPEC-GAP: spec 同时出现「可增删」「EXTRA_KEYWORDS 覆盖默认值」两种措辞，
        # 取「非空即整体替换默认词表」语义，增删都能表达。
        if self.extra_keywords.strip():
            return [k.strip() for k in self.extra_keywords.split(",") if k.strip()]
        return list(DEFAULT_KEYWORDS)

    @property
    def push_hour_minute(self) -> tuple[int, int]:
        hh, mm = self.daily_push_time.split(":", 1)
        return int(hh), int(mm)

    @property
    def pdf_dir(self) -> Path:
        return self.data_dir / "pdfs"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.pdf_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
