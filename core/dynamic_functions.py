"""动态 LLM 功能配置、加载、注册与重载。

从 functions.json 读取功能定义，注册为 LLM FunctionTool，
支持固定消息和 LLM 动态补全两种模式。

UMO 格式参考 ReplyWaiter 文档。
"""

import json
import re
from pathlib import Path
from typing import Callable

import astrbot.api.message_components as Comp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.message.message_event_result import MessageChain


# ================================================================
# 动态功能配置
# ================================================================

class DynamicFuncConfig:
    """一条动态 LLM 功能的配置"""

    __slots__ = (
        "name", "description", "umo", "message", "params_desc",
        "reply_mode", "at_self_id", "target_user_id", "timeout",
    )

    def __init__(self, raw: dict):
        self.name: str = raw["name"]
        self.description: str = raw.get("description", self.name)
        self.umo: str = raw["umo"]
        self.message: str = raw["message"]
        self.params_desc: str = raw.get("params_desc", "")
        self.reply_mode: str = raw.get("reply_mode", "any")
        self.at_self_id: str | None = raw.get("at_self_id")
        self.target_user_id: str | None = raw.get("target_user_id")
        self.timeout: float = float(raw.get("timeout", 30))


# ================================================================
# 消息解析
# ================================================================

def parse_message_to_chain(message_text: str) -> MessageChain:
    """解析消息文本为 MessageChain，支持 @QQ号 语法。

    "@114514 天气" → [At(qq="114514"), Plain(" 天气")]
    """
    components: list[Comp.BaseMessageComponent] = []
    pattern = re.compile(r"@(\d+)")
    last_end = 0
    for match in pattern.finditer(message_text):
        if match.start() > last_end:
            components.append(
                Comp.Plain(message_text[last_end:match.start()])
            )
        components.append(Comp.At(qq=match.group(1)))
        last_end = match.end()
    if last_end < len(message_text):
        components.append(Comp.Plain(message_text[last_end:]))
    if not components:
        components.append(Comp.Plain(message_text))
    return MessageChain(components)


# ================================================================
# 匹配条件构建
# ================================================================

def build_match_condition(
    reply_mode: str,
    at_self_id: str | None = None,
    target_user_id: str | None = None,
) -> Callable[[AstrMessageEvent], bool] | None:
    """根据 reply_mode 构建自定义匹配条件。

    所有模式已自动包含 UMO 会话匹配（由 ReplyWaiter._umo_matches 处理），
    这里只做额外的 at / user_id 检查。

    Args:
        reply_mode: "any" | "at" | "user" | "at_or_user" | "at_and_user"
        at_self_id: 机器人自身 ID（reply_mode=at/* 时使用，可 lazy 解析）
        target_user_id: 目标用户 ID（reply_mode=user/* 时使用）

    Returns:
        匹配函数，None 表示不做额外检查（reply_mode="any"）
    """
    if reply_mode == "any":
        return None

    if reply_mode == "at":
        resolved_sid: str | None = at_self_id

        def check(event: AstrMessageEvent) -> bool:
            nonlocal resolved_sid
            if resolved_sid is None:
                resolved_sid = event.get_self_id()
            for comp in event.get_messages():
                if isinstance(comp, Comp.At) and str(comp.qq) == resolved_sid:
                    return True
            return False

        return check

    if reply_mode == "user" and target_user_id:

        def check(event: AstrMessageEvent) -> bool:
            return event.get_sender_id() == target_user_id

        return check

    if reply_mode == "at_or_user":
        resolved_sid2: str | None = at_self_id

        def check(event: AstrMessageEvent) -> bool:
            nonlocal resolved_sid2
            if resolved_sid2 is None:
                resolved_sid2 = event.get_self_id()
            if target_user_id and event.get_sender_id() == target_user_id:
                return True
            for comp in event.get_messages():
                if isinstance(comp, Comp.At) and str(comp.qq) == resolved_sid2:
                    return True
            return False

        return check

    if reply_mode == "at_and_user":
        resolved_sid3: str | None = at_self_id

        def check(event: AstrMessageEvent) -> bool:
            nonlocal resolved_sid3
            if resolved_sid3 is None:
                resolved_sid3 = event.get_self_id()
            if target_user_id and event.get_sender_id() != target_user_id:
                return False
            for comp in event.get_messages():
                if isinstance(comp, Comp.At) and str(comp.qq) == resolved_sid3:
                    return True
            return False

        return check

    logger.warning(
        f"[DynamicFunc] 未知 reply_mode={reply_mode}, 回退为 any"
    )
    return None


# ================================================================
# 动态功能管理器
# ================================================================

class DynamicFuncManager:
    """管理动态 LLM 功能的加载、注册、重载。

    使用方式::

        mgr = DynamicFuncManager(plugin_instance)
        await mgr.load_all()

        # 修改 functions.json 后
        names = await mgr.reload_all()

    plugin_instance 需要提供:
    - .context         → 工具注册/注销、发送消息、LLM 补全
    - .reply_waiter    → 发送消息并等待回复
    - .complete_provider_id  → LLM 补全用的 provider ID
    """

    def __init__(self, plugin):
        self._plugin = plugin
        self._funcs: list[DynamicFuncConfig] = []

    # ---------- properties ----------

    @property
    def funcs(self) -> list[DynamicFuncConfig]:
        """已加载的功能配置列表"""
        return self._funcs

    @property
    def count(self) -> int:
        """已加载的功能数量"""
        return len(self._funcs)

    # ---------- path ----------

    @staticmethod
    def _get_functions_path() -> Path:
        """functions.json 路径（相对于插件根目录）"""
        return Path(__file__).resolve().parent.parent / "functions.json"

    # ---------- load ----------

    async def load_all(self) -> int:
        """从 functions.json 读取配置并注册为 LLM 工具。

        Returns:
            成功注册的工具数量
        """
        path = self._get_functions_path()
        if not path.exists():
            logger.info(f"[DynamicFunc] 配置文件不存在: {path}")
            return 0

        try:
            with open(path, encoding="utf-8-sig") as f:
                data = json.load(f)
        except Exception as e:
            logger.error(f"[DynamicFunc] 读取配置文件失败: {e}")
            return 0

        funcs_raw = data.get("functions", [])
        if not funcs_raw:
            logger.info("[DynamicFunc] 配置中无功能定义")
            return 0

        self._funcs = []
        tools: list[FunctionTool] = []

        for raw in funcs_raw:
            try:
                cfg = DynamicFuncConfig(raw)
            except KeyError as e:
                logger.warning(f"[DynamicFunc] 跳过缺少字段的条目: {e}")
                continue

            self._funcs.append(cfg)
            handler = self._make_handler(cfg)

            tool = FunctionTool(
                name=cfg.name,
                description=cfg.description,
                parameters={"type": "object", "properties": {}},
                handler=handler,
                handler_module_path=(
                    "plugins.astrbot_plugin_universal.core.dynamic_functions"
                ),
            )
            tools.append(tool)

            complete_mode = (
                "LLM补全" if self._plugin.complete_provider_id else "固定消息"
            )
            logger.info(
                f"[DynamicFunc] 注册工具: {cfg.name} → "
                f"{cfg.umo} | msg={cfg.message[:30]} | "
                f"mode={cfg.reply_mode} | {complete_mode}"
            )

        if tools:
            self._plugin.context.add_llm_tools(*tools)

        return len(tools)

    # ---------- reload ----------

    async def reload_all(self) -> list[str]:
        """卸载旧工具并重新加载。

        Returns:
            新加载的工具名列表（空列表表示无有效配置）
        """
        for old in self._funcs:
            try:
                self._plugin.context.unregister_llm_tool(old.name)
            except Exception:
                pass
        self._funcs.clear()

        await self.load_all()

        return [f.name for f in self._funcs]

    # ---------- handler factory ----------

    def _make_handler(self, cfg: DynamicFuncConfig):
        """根据配置生成 LLM 工具 handler（闭包）。

        LLM 调用此工具时的执行流程:
        1. 若配置了 complete_provider_id: LLM 根据用户问题动态补全消息
        2. 否则: 直接使用 message 原文（固定消息）
        3. 解析 @QQ号 语法 → MessageChain
        4. 发送到目标会话，等待匹配的回复
        5. 返回回复文本（或超时提示）
        """
        match_cond = build_match_condition(
            cfg.reply_mode, cfg.at_self_id, cfg.target_user_id
        )
        umo = cfg.umo
        timeout = cfg.timeout
        waiter = self._plugin.reply_waiter
        complete_provider_id = self._plugin.complete_provider_id
        context = self._plugin.context

        # 防护相关
        bot_list = self._plugin.bot_list
        layer_protection = self._plugin.layer_protection
        enable_b2b = self._plugin.enable_b2b
        unreachable_message = self._plugin.unreachable_message

        async def handler(first_arg, **kwargs) -> str:
            # 兼容 v4.26.0+ (ContextWrapper) 和旧版 (AstrMessageEvent)
            if isinstance(first_arg, AstrMessageEvent):
                event = first_arg
            else:
                # ContextWrapper: context.context.event → AstrMessageEvent
                event = first_arg.context.event

            if not event:
                return "[错误] 无法获取消息上下文"

            # ── 防护检查 ──
            sender_id = event.get_sender_id()
            if bot_list and sender_id in bot_list:
                if layer_protection:
                    logger.info(
                        f"[Guard:{cfg.name}] 层级保护阻断 "
                        f"sender={sender_id}"
                    )
                    return (
                        "[阻断] 检测到机器人串联调用，已阻止上游使用"
                    )
                if not enable_b2b:
                    logger.info(
                        f"[Guard:{cfg.name}] B2B 已禁用，拒绝 "
                        f"sender={sender_id}"
                    )
                    return unreachable_message

            # 步骤1: 确定消息文本
            if complete_provider_id:
                user_question = event.message_str
                desc_line = (
                    f"参数说明：{cfg.params_desc}" if cfg.params_desc else ""
                )
                prompt = (
                    f"# 任务\n"
                    f"用户提出了一个问题，你需要将其转换为一条发给目标群的指令消息。\n\n"
                    f"# 用户原始问题\n"
                    f"{user_question}\n\n"
                    f"# 消息模板（请在此基础上补全具体内容）\n"
                    f"{cfg.message}\n\n"
                    f"{desc_line}\n"
                    f"# 要求\n"
                    f"直接输出补全后的消息文本，不要任何解释、引号或前缀。"
                )
                try:
                    resp = await context.llm_generate(
                        chat_provider_id=complete_provider_id,
                        prompt=prompt,
                    )
                    message_text = resp.completion_text.strip()
                    logger.info(
                        f"[DynamicFunc:{cfg.name}] LLM补全: {message_text[:80]}"
                    )
                except Exception as e:
                    logger.error(f"[DynamicFunc:{cfg.name}] LLM补全失败: {e}")
                    message_text = cfg.message
            else:
                message_text = cfg.message

            # 步骤2: 解析 @QQ号 → MessageChain
            chain = parse_message_to_chain(message_text)

            logger.info(
                f"[DynamicFunc:{cfg.name}] 发送 → {umo} | {message_text[:50]}"
            )

            # 步骤3+4: 发送并等待回复
            reply = await waiter.send_and_wait(
                umo, chain,
                match_condition=match_cond,
                timeout=timeout,
            )

            # 步骤5: 返回结果
            if reply is None:
                return f"[超时] 在 {timeout}s 内未收到回复"
            return reply.message_str

        handler.__name__ = f"dynamic_{cfg.name}"
        handler.__module__ = (
            "plugins.astrbot_plugin_universal.core.dynamic_functions"
        )
        return handler