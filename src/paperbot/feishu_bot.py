"""FR-4 飞书推送与交互 + FR-5 指令集。

长连接 + 卡片按钮回调的接入模式参考
https://github.com/Win7win/elena-daily-paper-scout 的 core/feishu_app.py 与
core/card_actions.py；按本 spec 改为代码内直接构造 interactive card JSON
（不走飞书卡片模板平台），并新增会话式指令集与 owner 身份校验。
"""

from __future__ import annotations

import json
import logging
import threading
import time

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    P2ImMessageReceiveV1,
)
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    CallBackToast,
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)
from lark_oapi.ws import Client as WSClient

from . import digest, queue_service as qs
from .cardfmt import (
    HELP_MARKDOWN,
    chunk_text,
    list_markdown,
    paper_card_json,
    simple_card_json,
    stats_markdown,
)
from .config import Settings
from .models import Paper
from .pipeline import DailyReport

logger = logging.getLogger(__name__)

SEND_RETRY_TIMES = 2  # 首次失败后再重试 2 次

# FR-5 指令表（先规范化：去空格、转小写）
COMMAND_MAP = {
    "开始": "next", "下一条": "next", "next": "next", "n": "next",
    "详细": "detail", "detail": "detail", "d": "detail",
    "跳过": "skip", "skip": "skip", "s": "skip",
    "收藏": "star", "star": "star", "fav": "star",
    "列表": "list", "list": "list", "ls": "list",
    "统计": "stats", "stats": "stats",
    "帮助": "help", "help": "help", "?": "help", "？": "help",
}

UNKNOWN_REPLY = "未识别指令，回复「帮助」查看可用指令"
NO_CURRENT_REPLY = "还没有当前论文，先回复「下一条」开始阅读"
QUEUE_EMPTY_REPLY = "🎉 今日队列已清空"


class FeishuConfigError(RuntimeError):
    pass


class FeishuBot:
    def __init__(self, settings: Settings):
        if not (settings.feishu_app_id and settings.feishu_app_secret):
            raise FeishuConfigError("FEISHU_APP_ID / FEISHU_APP_SECRET 未配置")
        if not settings.feishu_owner_open_id:
            raise FeishuConfigError(
                "FEISHU_OWNER_OPEN_ID 未配置（先填任意值启动 serve，给机器人发条消息，从日志拿到 open_id 再填回）"
            )
        self.settings = settings
        self.owner = settings.feishu_owner_open_id
        self.client = (
            lark.Client.builder()
            .app_id(settings.feishu_app_id)
            .app_secret(settings.feishu_app_secret)
            .build()
        )

    # ---------- 发送（失败重试 2 次，记日志） ----------

    def _send(self, msg_type: str, content: dict) -> bool:
        body = (
            CreateMessageRequestBody.builder()
            .receive_id(self.owner)
            .msg_type(msg_type)
            .content(json.dumps(content, ensure_ascii=False))
            .build()
        )
        req = CreateMessageRequest.builder().receive_id_type("open_id").request_body(body).build()
        for attempt in range(SEND_RETRY_TIMES + 1):
            try:
                resp = self.client.im.v1.message.create(req)
                if resp.success():
                    return True
                logger.warning(
                    "飞书发送失败（第 %d 次）：code=%s msg=%s log_id=%s",
                    attempt + 1, resp.code, resp.msg, resp.get_log_id(),
                )
            except Exception as exc:  # noqa: BLE001 — SDK 异常类型不公开
                logger.warning("飞书发送异常（第 %d 次）：%s", attempt + 1, exc)
            if attempt < SEND_RETRY_TIMES:
                time.sleep(1.0)
        logger.error("飞书发送重试 %d 次仍失败（msg_type=%s）", SEND_RETRY_TIMES, msg_type)
        return False

    def send_text(self, text: str) -> bool:
        return self._send("text", {"text": text})

    def send_card(self, card: dict) -> bool:
        return self._send("interactive", card)

    def send_paper_card(self, paper: Paper) -> bool:
        return self.send_card(paper_card_json(paper))

    def send_summary(self, report: DailyReport) -> bool:
        card = simple_card_json(f"📄 arXiv 日报 · {report.date}", report.summary_text, "blue")
        return self.send_card(card)

    def send_markdown(self, title: str, markdown: str, template: str = "blue") -> bool:
        return self.send_card(simple_card_json(title, markdown, template))

    def send_long_markdown(self, title: str, text: str) -> None:
        """深度解读：超 4000 字符按片发送（FR-6 允许分片方案）。"""
        chunks = chunk_text(text)
        for i, chunk in enumerate(chunks):
            suffix = f"（{i + 1}/{len(chunks)}）" if len(chunks) > 1 else ""
            self.send_markdown(f"{title}{suffix}", chunk, "violet")

    # ---------- 指令实现（文字指令与按钮回调共用） ----------

    def _resolve_paper(self, arxiv_id: str | None) -> Paper | None:
        """按钮带 arxiv_id 时作用于该篇，否则作用于 current 指针。"""
        if arxiv_id:
            from sqlalchemy import select

            from .db import SessionLocal

            with SessionLocal() as s:
                paper = s.scalars(select(Paper).where(Paper.arxiv_id == arxiv_id)).first()
                if paper is not None:
                    s.expunge(paper)
                return paper
        return qs.get_current()

    def cmd_next(self) -> None:
        paper = qs.next_paper()
        if paper is None:
            self.send_text(QUEUE_EMPTY_REPLY)
            return
        if not self.send_paper_card(paper):
            qs.requeue(paper.id)  # 发送失败状态不前进
            return
        remaining = qs.pending_count()
        if remaining == 0:
            self.send_text(f"📤 这是最后一篇。{QUEUE_EMPTY_REPLY}")

    def cmd_detail(self, arxiv_id: str | None = None) -> None:
        paper = self._resolve_paper(arxiv_id)
        if paper is None:
            self.send_text(NO_CURRENT_REPLY)
            return
        qs.set_current_by_id(paper.id)
        try:
            result = digest.get_or_create_detail(paper.id)
        except Exception as exc:  # noqa: BLE001 — LLM/PDF 任一环节失败都给用户明确反馈
            logger.exception("深度解读失败 %s", paper.arxiv_id)
            self.send_text(f"⚠️ 深度解读生成失败：{exc}")
            return
        qs.mark_read(paper.id)
        cached = "（缓存）" if result.from_cache else ""
        self.send_long_markdown(f"🔬 深度解读 · {paper.title}{cached}", result.text)
        # 用户要求「看完一篇自动刷下一篇」：新鲜深读（非缓存）完成后自动发下一篇；
        # 缓存重读不前进，避免连点两次「详细」连跳两篇
        if not result.from_cache:
            self.cmd_next()

    def cmd_skip(self, arxiv_id: str | None = None) -> None:
        paper = self._resolve_paper(arxiv_id)
        if paper is None:
            self.send_text(NO_CURRENT_REPLY)
            return
        qs.skip_paper(paper.id)
        self.cmd_next()  # 跳过当前篇，自动发下一篇

    def cmd_star(self, arxiv_id: str | None = None) -> None:
        paper = self._resolve_paper(arxiv_id)
        if paper is None:
            self.send_text(NO_CURRENT_REPLY)
            return
        qs.star_paper(paper.id)
        self.send_text(f"⭐ 已收藏：《{paper.title}》")

    def cmd_list(self) -> None:
        self.send_markdown("📋 今日队列", list_markdown(qs.list_queue()), "turquoise")

    def cmd_stats(self) -> None:
        self.send_markdown("📊 统计", stats_markdown(qs.get_stats()), "wathet")

    def cmd_help(self) -> None:
        self.send_markdown("📚 帮助", HELP_MARKDOWN, "indigo")

    def dispatch_command(self, command: str, arxiv_id: str | None = None) -> None:
        handler = {
            "next": lambda: self.cmd_next(),
            "detail": lambda: self.cmd_detail(arxiv_id),
            "skip": lambda: self.cmd_skip(arxiv_id),
            "star": lambda: self.cmd_star(arxiv_id),
            "list": self.cmd_list,
            "stats": self.cmd_stats,
            "help": self.cmd_help,
        }.get(command)
        if handler is None:
            self.send_text(UNKNOWN_REPLY)
            return
        try:
            handler()
        except Exception:  # noqa: BLE001 — 指令失败不能炸掉长连接
            logger.exception("指令 %s 执行失败", command)
            self.send_text(f"⚠️ 指令「{command}」执行出错，详情见日志")

    def _dispatch_in_thread(self, command: str, arxiv_id: str | None = None) -> None:
        """指令可能触发 LLM 调用（耗时），异步执行避免阻塞长连接事件循环。"""
        threading.Thread(
            target=self.dispatch_command, args=(command, arxiv_id),
            name=f"cmd-{command}", daemon=True,
        ).start()

    # ---------- 事件处理 ----------

    def _is_owner(self, open_id: str | None, source: str) -> bool:
        """owner 身份校验：其他人消息静默忽略（open_id 记日志，便于首次配置）。"""
        if open_id == self.owner:
            return True
        logger.info("忽略非 owner 的%s（open_id=%s）", source, open_id)
        return False

    def on_message(self, data: P2ImMessageReceiveV1) -> None:
        event = data.event
        if event is None or event.message is None:
            return
        sender = event.sender
        open_id = sender.sender_id.open_id if sender and sender.sender_id else None
        if not self._is_owner(open_id, "消息"):
            return
        msg = event.message
        text = ""
        if msg.message_type == "text":
            try:
                text = json.loads(msg.content or "{}").get("text", "")
            except json.JSONDecodeError:
                text = ""
        command = COMMAND_MAP.get(" ".join(text.split()).lower())
        logger.info("收到消息：%r → %s", text, command or "未识别")
        if command is None:
            self.send_text(UNKNOWN_REPLY)
            return
        self._dispatch_in_thread(command)

    def on_card_action(self, data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        """card.action.trigger：按钮回调等价于文字指令（spec 5.4 保留的回调入口）。"""
        event = data.event
        action = event.action if event else None
        operator = event.operator if event else None
        open_id = operator.open_id if operator else None
        value = (action.value or {}) if action else {}

        response = P2CardActionTriggerResponse()
        toast = CallBackToast()
        response.toast = toast

        if not self._is_owner(open_id, "卡片操作"):
            toast.type, toast.content = "warning", "非授权用户"
            return response

        name = str(value.get("action") or "")
        arxiv_id = str(value.get("arxiv_id") or "") or None
        logger.info("卡片按钮：action=%s arxiv_id=%s", name, arxiv_id)

        if name == "detail":
            self._dispatch_in_thread("detail", arxiv_id)
            toast.type, toast.content = "info", "⏳ 正在生成深度解读…"
        elif name == "skip":
            self._dispatch_in_thread("skip", arxiv_id)
            toast.type, toast.content = "info", "⏭️ 已跳过"
        elif name == "star":
            self._dispatch_in_thread("star", arxiv_id)
            toast.type, toast.content = "success", "⭐ 已收藏"
        else:
            logger.warning("未知卡片 action：%r", name)
            toast.type, toast.content = "error", f"未知操作：{name or '空'}"
        return response


def run_forever(settings: Settings, *, before_serve=None) -> None:
    """启动飞书 WebSocket 长连接（阻塞主线程；断线由 SDK 自动重连）。

    before_serve: 阻塞前执行的钩子（启动调度器等）。
    """
    bot = FeishuBot(settings)
    dispatcher = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(bot.on_message)
        .register_p2_card_action_trigger(bot.on_card_action)
        .build()
    )
    if before_serve is not None:
        before_serve(bot)
    ws_client = WSClient(
        settings.feishu_app_id,
        settings.feishu_app_secret,
        event_handler=dispatcher,
        log_level=lark.LogLevel.WARNING,
    )
    logger.info("飞书长连接启动，等待消息…")
    ws_client.start()
