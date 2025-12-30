import asyncio
import time
import uuid
import random
from typing import Dict, Any, List

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger


@register(
    "astrbot_plugin_universal",
    "BUGJI",
    "万能插件，基于能工智能进行功能扩展",
    "0.1.0",
    "https://github.com/BUGJI/astrbot_plugin_universal"
)
class BotProxyPlugin(Star):

    def __init__(self, context: Context, config):
        super().__init__(context)
        self.context = context
        self.config = config

        # ===== message 配置 =====
        message_cfg = self.config.message
        self.timeout_message = message_cfg.get("timeout_message", "请求超时")
        self.unreachable_message = message_cfg.get("unreachable_message", "不可达")

        # ===== rate limit =====
        self.rate_per_minute = int(self.config.get("rate_per_minute", 5))
        self.request_timestamps: List[float] = []

        # ===== action 解析 =====
        self.actions = self._parse_actions(
            self.config.get("known_bots_action", [])
        )

        # 正在等待的请求
        self.pending_requests: Dict[str, Dict[str, Any]] = {}

        logger.info(f"BotProxyPlugin 加载完成，共 {len(self.actions)} 个 action")

    # =========================
    # 配置解析
    # =========================
    def _parse_actions(self, lines: List[str]) -> List[Dict[str, Any]]:
        actions = []

        for line in lines:
            try:
                botQQ, groups, command, mode, desc = line.split(";", 4)
                action = {
                    "id": str(uuid.uuid4()),
                    "bot_id": int(botQQ),
                    "groups": [int(g) for g in groups.split(",") if g],
                    "command": command,
                    "return_mode": mode.strip(),
                    "desc": desc.strip(),
                    "timeout": int(self.config.get("timeout", 30))
                }
                actions.append(action)
            except Exception as e:
                logger.error(f"解析 action 失败: {line} -> {e}")

        return actions

    # =========================
    # 限速
    # =========================
    def _rate_limited(self) -> bool:
        now = time.time()
        self.request_timestamps = [
            t for t in self.request_timestamps if now - t < 60
        ]
        if len(self.request_timestamps) >= self.rate_per_minute:
            return True
        self.request_timestamps.append(now)
        return False

    # =========================
    # Tool：对外能力
    # =========================
    @filter.llm_tool(name="use_bot_action")
    async def use_bot_action(
        self,
        event: AstrMessageEvent,
        action_desc: str
    ) -> MessageEventResult:
        '''调用其它 Bot 的能力。

        Args:
            action_desc(string): 功能描述
        '''

        if self._rate_limited():
            yield event.plain_result("🚦 请求过于频繁")
            return

        action = next(
            (a for a in self.actions if action_desc in a["desc"]),
            None
        )

        if not action:
            yield event.plain_result("❌ 未找到匹配的功能")
            return

        if not action["groups"]:
            yield event.plain_result(self.unreachable_message)
            return

        target_group = random.choice(action["groups"])
        request_id = str(uuid.uuid4())

        self.pending_requests[request_id] = {
            "bot_id": action["bot_id"],
            "group_id": target_group,
            "source_group": event.get_group_id(),
            "return_mode": action["return_mode"],
            "expire_at": time.time() + action["timeout"],
        }

        logger.info(
            f"请求 {request_id} -> 群 {target_group} | {action['command']}"
        )

        await self.context.send_group_message(
            target_group,
            action["command"]
        )

        asyncio.create_task(
            self._wait_timeout(request_id)
        )

    # =========================
    # 超时处理
    # =========================
    async def _wait_timeout(self, request_id: str):
        info = self.pending_requests.get(request_id)
        if not info:
            return

        await asyncio.sleep(
            max(0, info["expire_at"] - time.time())
        )

        if request_id in self.pending_requests:
            self.pending_requests.pop(request_id, None)
            await self.context.send_group_message(
                info["source_group"],
                self.timeout_message
            )

    # =========================
    # 消息监听（核心）
    # =========================
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent):
        sender_id = event.get_sender_id()
        group_id = event.get_group_id()
        msg = event.message_str or ""

        for req_id, info in list(self.pending_requests.items()):
            if sender_id != info["bot_id"]:
                continue
            if group_id != info["group_id"]:
                continue
            if time.time() > info["expire_at"]:
                continue

            # 返回方式判断
            if info["return_mode"] == "@":
                if not event.is_at_me():
                    continue

            logger.info(f"请求 {req_id} 命中结果")

            self.pending_requests.pop(req_id, None)

            await self.context.send_group_message(
                info["source_group"],
                f"🤖 结果：\n{msg}"
            )

            event.stop_event()
            return

    async def terminate(self):
        self.pending_requests.clear()
        logger.info("BotProxyPlugin 已卸载")
