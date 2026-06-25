"""发送消息并等待回复的模块

支持三种等待策略:
- send_and_wait: 基础方法，仅按会话匹配（同一会话的任意消息即视为回复）
- send_and_wait_for_at: 等待被 @ 的消息
- send_and_wait_for_user_reply: 等待指定用户的第一条消息

也可通过 match_condition 参数传入自定义匹配逻辑。

UMO 格式与 unique_session:
    UMO = {platform_id}:{MessageType}:{session_id}
    例: "default:GroupMessage:1077781248"

    unique_session=True 时群聊 session_id 格式为 "{user_id}_{group_id}":
    例: "default:GroupMessage:75915429_1077781248"
         └─ 用户ID ──┘└── 群号 ──┘

    发送时通常只指定 group_id，回复时包含 user_id，
    本模块通过 _parse_umo 拆解后按 group_id 匹配。
"""

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Callable

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At, BaseMessageComponent
from astrbot.core.message.message_event_result import MessageChain


# ================================================================
# UMO 拆解
# ================================================================

@dataclass
class UmoParts:
    """UMO 拆解结果"""
    platform: str       # 平台ID，如 "default"
    msg_type: str       # 消息类型，如 "GroupMessage", "FriendMessage"
    session_id: str     # 原始 session_id
    user_id: str | None  # 用户ID（群聊 unique_session 模式，或私聊时）; 否则 None
    group_id: str | None  # 群号（群聊时）; 否则 None

    @classmethod
    def parse(cls, umo: str) -> "UmoParts":
        """从 UMO 字符串拆解出各部分。

        规则:
        - 用 ":" 拆出 platform, type, session_id
        - 群聊 (GroupMessage): session_id 格式为 "userid_groupid" (unique_session)
          或仅为 "groupid" (非 unique_session)
        - 私聊 (FriendMessage): session_id 直接为 user_id

        例:
            UmoParts.parse("default:GroupMessage:75915429_1077781248")
            → platform="default", type="GroupMessage",
              user_id="75915429", group_id="1077781248"

            UmoParts.parse("default:GroupMessage:1077781248")
            → platform="default", type="GroupMessage",
              user_id=None, group_id="1077781248"

            UmoParts.parse("default:FriendMessage:75915429")
            → platform="default", type="FriendMessage",
              user_id="75915429", group_id=None
        """
        try:
            platform, msg_type, session_id_raw = umo.split(":", 2)
        except ValueError:
            raise ValueError(f"无法解析 UMO: {umo}")

        is_group = msg_type == "GroupMessage"
        user_id: str | None = None
        group_id: str | None = None

        if "_" in session_id_raw:
            # unique_session 格式: "userid_groupid"
            parts = session_id_raw.split("_", 1)
            user_id = parts[0]
            group_id = parts[1] if len(parts) > 1 else None
        elif is_group:
            # 非 unique_session: session_id 直接是群号
            group_id = session_id_raw
        else:
            # 私聊: session_id 直接是用户ID
            user_id = session_id_raw

        return cls(
            platform=platform,
            msg_type=msg_type,
            session_id=session_id_raw,
            user_id=user_id,
            group_id=group_id,
        )


# ================================================================
# PendingReply
# ================================================================

@dataclass
class PendingReply:
    """一次待回复请求"""
    request_id: str
    target_session: str
    match_condition: Callable[[AstrMessageEvent], bool] | None
    event: asyncio.Event
    reply_event: AstrMessageEvent | None = None
    reply_messages: list[BaseMessageComponent] = field(default_factory=list)


# ================================================================
# ReplyWaiter
# ================================================================

class ReplyWaiter:
    """发送消息并等待回复的工具类。

    使用方式:

        waiter = ReplyWaiter(self)

        reply = await waiter.send_and_wait(
            "default:GroupMessage:1077781248",
            MessageChain([Plain("请回复")]),
            timeout=30.0,
        )

        reply = await waiter.send_and_wait_for_at(
            "default:GroupMessage:1077781248",
            MessageChain([Plain("@我回复")]),
            at_self_id=self_id,
        )

        reply = await waiter.send_and_wait_for_user_reply(
            "default:GroupMessage:1077781248",
            MessageChain([Plain("张三请回复")]),
            target_user_id="123456",
        )
    """

    def __init__(self, plugin_instance):
        self.plugin = plugin_instance
        self._pending: dict[str, PendingReply] = {}
        self._lock = asyncio.Lock()

    # ============================================================
    # UMO 匹配（基于 group_id 比较）
    # ============================================================

    @staticmethod
    def _umo_matches(target: str, actual: str) -> bool:
        """检查两个 UMO 是否指向同一会话。

        匹配规则:
        1. platform 和 msg_type 必须完全一致
        2. group_id 必须一致（群聊时按群号匹配）
        3. 私聊时按 session_id 精确匹配（私聊不使用 unique_session）

        这样无论 unique_session 是否开启都能正确匹配:
        - target="default:GroupMessage:1077781248"
        - actual="default:GroupMessage:75915429_1077781248"
        → 两者 group_id 都是 "1077781248" → 匹配 ✅
        """
        if target == actual:
            return True

        try:
            t = UmoParts.parse(target)
            a = UmoParts.parse(actual)
        except ValueError:
            return False

        if t.platform != a.platform or t.msg_type != a.msg_type:
            return False

        # 群聊: 按 group_id 匹配
        if t.msg_type == "GroupMessage":
            return t.group_id is not None and t.group_id == a.group_id

        # 私聊: 按 user_id 匹配（精确，私聊不用 unique_session）
        return t.user_id is not None and t.user_id == a.user_id

    # ============================================================
    # 公开 API
    # ============================================================

    async def send_and_wait(
        self,
        target_session: str,
        message_chain: MessageChain,
        *,
        match_condition: Callable[[AstrMessageEvent], bool] | None = None,
        timeout: float = 60.0,
    ) -> AstrMessageEvent | None:
        """向目标会话发送消息，等待匹配的回复。

        Args:
            target_session: unified_msg_origin
            message_chain: 要发送的消息链
            match_condition: 额外自定义匹配函数 (event → bool)。
                             UMO 匹配已自动处理 unique_session 差异，
                             不需要在 match_condition 中再检查会话。
            timeout: 超时秒数，默认 60

        Returns:
            匹配到的 AstrMessageEvent，超时返回 None
        """
        request_id = uuid.uuid4().hex

        pending = PendingReply(
            request_id=request_id,
            target_session=target_session,
            match_condition=match_condition,
            event=asyncio.Event(),
        )

        async with self._lock:
            self._pending[request_id] = pending

        try:
            ok = await self.plugin.context.send_message(
                target_session, message_chain
            )
            if not ok:
                logger.error(
                    f"[ReplyWaiter] 发送失败: {target_session}"
                )
                return None

            info = UmoParts.parse(target_session)
            logger.info(
                f"[ReplyWaiter] 已发送到 {info.msg_type}/{info.group_id or info.user_id}, "
                f"等待回复... (id={request_id[:8]})"
            )

            try:
                await asyncio.wait_for(pending.event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    f"[ReplyWaiter] 超时 ({timeout}s): {target_session}"
                )
                return None

            sender = pending.reply_event.get_sender_name()
            logger.info(
                f"[ReplyWaiter] 收到回复: sender={sender}, "
                f"text={pending.reply_event.message_str[:80]}"
            )
            return pending.reply_event

        finally:
            async with self._lock:
                self._pending.pop(request_id, None)

    async def send_and_wait_for_at(
        self,
        target_session: str,
        message_chain: MessageChain,
        *,
        at_self_id: str | None = None,
        timeout: float = 60.0,
    ) -> AstrMessageEvent | None:
        """发送消息后等待「被 @ 自身」的消息作为回复。

        Args:
            target_session: 目标会话
            message_chain: 要发送的消息链
            at_self_id: 机器人的 QQ/ID，不传会在首次收到消息时 lazily 获取
            timeout: 超时秒数
        """
        resolved_self_id: str | None = at_self_id

        def is_at_me(event: AstrMessageEvent) -> bool:
            nonlocal resolved_self_id
            if resolved_self_id is None:
                resolved_self_id = event.get_self_id()
                if not resolved_self_id:
                    return False
            for comp in event.get_messages():
                if isinstance(comp, At) and str(comp.qq) == resolved_self_id:
                    return True
            return False

        return await self.send_and_wait(
            target_session, message_chain,
            match_condition=is_at_me,
            timeout=timeout,
        )

    async def send_and_wait_for_user_reply(
        self,
        target_session: str,
        message_chain: MessageChain,
        target_user_id: str,
        *,
        timeout: float = 60.0,
    ) -> AstrMessageEvent | None:
        """发送消息后等待「指定用户的第一条消息」作为回复。

        Args:
            target_session: 目标会话
            message_chain: 要发送的消息链
            target_user_id: 期望回复的用户 ID
            timeout: 超时秒数
        """
        def from_target_user(event: AstrMessageEvent) -> bool:
            return event.get_sender_id() == target_user_id

        return await self.send_and_wait(
            target_session, message_chain,
            match_condition=from_target_user,
            timeout=timeout,
        )

    # ============================================================
    # 内部：由 on_message 调用
    # ============================================================

    async def check_reply(self, event: AstrMessageEvent) -> None:
        """检查当前消息是否命中某个待回复请求。

        应在全局 on_message 中调用此方法。
        """
        if not self._pending:
            return

        actual_umo = event.unified_msg_origin
        logger.info(actual_umo)

        async with self._lock:
            for pending in list(self._pending.values()):
                if pending.event.is_set():
                    continue

                if not self._umo_matches(pending.target_session, actual_umo):
                    continue

                if pending.match_condition is not None:
                    try:
                        if not pending.match_condition(event):
                            continue
                    except Exception as e:
                        logger.warning(
                            f"[ReplyWaiter] match_condition 异常: {e}"
                        )
                        continue

                pending.reply_event = event
                pending.reply_messages = event.get_messages()
                pending.event.set()
                logger.info(
                    f"[ReplyWaiter] 命中! id={pending.request_id[:8]}, "
                    f"target={pending.target_session}, "
                    f"actual={actual_umo}, "
                    f"sender={event.get_sender_name()}"
                )