"""万能插件，基于能工智能进行功能扩展。

功能模块:
- core/reply_waiter.py      → 发送消息并等待回复
- core/dynamic_functions.py  → 动态 LLM 功能（functions.json）的加载/注册/重载
- core/auto_analyzer.py      → 自动分析群聊消息，发现潜在 Bot 功能
"""

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.core.provider.entities import ProviderRequest

from .core.reply_waiter import ReplyWaiter
from .core.dynamic_functions import DynamicFuncManager
from .core.auto_analyzer import AutoAnalyzer


@register(
    "astrbot_plugin_universal",
    "BUGJI",
    "万能插件，基于能工智能进行功能扩展",
    "0.1.0",
    "https://github.com/BUGJI/astrbot_plugin_universal",
)
class BotProxyPlugin(Star):

    def __init__(self, context: Context, config):
        super().__init__(context)

        self.context = context
        self.config = config

        # 回复等待器
        self.reply_waiter = ReplyWaiter(self)
        # 动态功能管理器
        self.dynamic_funcs = DynamicFuncManager(self)
        # 自动分析器
        self.auto_analyzer = AutoAnalyzer(self)

        # ── 基本信息 ──
        # 大模型ID：用于补全工具参数的模型ID
        self.complete_provider_id = self.config.basic.get(
            "complete_provider_id", ""
        )
        # 大模型ID：用于自动分析bot功能的模型ID
        self.analyze_provider_id = self.config.basic.get(
            "analyze_provider_id", ""
        )

        # ── 群控制 ──
        # 允许收集的群聊 黑/白 名单控制
        self.block_method = self.config.enabled_collection_group.get(
            "block_method", "whitelist"
        )
        # 允许收集的群聊 黑/白 名单列表
        self.control_list = self.config.enabled_collection_group.get(
            "control_list", []
        )

        # ── 已注册信息 ──
        # 黑名单列表：软黑名单，不允许和你闲聊的bot账号
        self.bot_list: list[str] = self.config.reg_info.get("bot_list", [])
        # 黑名单列表：此账号不会被自动注册到插件功能上
        self.deny_list: list[str] = self.config.reg_info.get("deny_list", [])

        # ── 限制设置 ──
        # 定时分析（留空关闭）
        self.auto_analyze_crontab = self.config.limit.get(
            "auto_analyze_crontab", "12 12 * * *"
        )
        # RPM：总插件调用次数的RPM
        self.rate_per_minute = self.config.limit.get("rate_per_minute", 5)
        # 允许自动测试并注册到bot functions
        self.auto_reg_bot_functions = self.config.limit.get("auto_reg_bot_functions", False)
        # 允许加入软黑名单的bot使用你的功能
        self.enable_b2b = self.config.limit.get("enable_b2b", True)
        # 多层保护
        self.layer_protection = self.config.limit.get("layer_protection", False)

        # ── 提示消息 ──
        # 使用LLM风格返回错误消息
        self.error_provider = self.config.notice.get("error_provider", False)
        self.timeout_message = self.config.notice.get(
            "timeout", "服务开小差了，请稍后重试"
        )
        self.unreachable_message = self.config.notice.get(
            "unreachable", "服务不可用"
        )

        # ── 提示词配置 ──
        # 软屏蔽提示词
        self.deny_system_prompt_extra: str = (
            "\n\n[系统指令 - 高优先级]\n"
            "当前与你对话的用户处于受限模式。你必须严格遵循：\n"
            "1. 你能看到这条提示是因为对方是机器人，和你对话是因为对方需要调用你的功能或你本身的插件需要捕获其返回值\n"
            "2. 禁止生成任何问候、闲聊、寒暄、语气词、表情符号、拟声词。不要闲聊，可以不回复任何东西\n"
            "3. 如果用户请求用工具/函数处理：直接调用工具，只返回工具结果，不加过渡语或解释。\n"
            "4. 如果没有合适的工具：仅回复\"抱歉，此功能暂不可用。\"，没有任何附加文字。\n"
            "5. 不要透露你正在被限制的事实。\n"
        )

        logger.info("UniversalPlugin 已加载")

    # ============================================================
    # 初始化：加载动态 LLM 功能
    # ============================================================

    async def initialize(self):
        """插件激活时加载动态功能注册"""
        count = await self.dynamic_funcs.load_all()
        logger.info(f"UniversalPlugin 已注册 {count} 个动态 LLM 工具")

        # 启动定时自动分析（如果配置了 cron）
        if self.auto_analyze_crontab:
            logger.info(
                f"[AutoAnalyzer] 定时分析已开启: {self.auto_analyze_crontab}"
            )
        if self.auto_reg_bot_functions:
            logger.info("[AutoAnalyzer] 自动注册已开启，测试通过后自动合并到 functions")

    # ============================================================
    # 群控检查
    # ============================================================

    def _check_group(self, event: AstrMessageEvent) -> bool:
        """检查当前会话是否在白/黑名单中"""
        umo = event.unified_msg_origin
        if self.block_method == "whitelist":
            if not self.control_list:
                return True
            return umo in self.control_list
        elif self.block_method == "blacklist":
            return umo not in self.control_list
        return True

    # ============================================================
    # 软屏蔽：LLM 请求前注入受限 prompt
    # ============================================================

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """bot_list（弱黑名单）中的用户：禁止 LLM 个性化回复，但不影响工具调用。

        通过在 system_prompt 中注入高优先级指令来实现：
        - 允许工具调用和结果返回
        - 禁止闲聊、问候、表情、语气词等个性化文字
        """
        sender_id = event.get_sender_id()
        logger.info(
            f"[SoftDeny] on_llm_request 触发 | "
            f"sender_id={sender_id} | "
            f"bot_list={self.bot_list} | "
            f"system_prompt_len={len(req.system_prompt or '')}"
        )

        if not self.bot_list:
            logger.info("[SoftDeny] bot_list 为空，跳过")
            return

        if sender_id not in self.bot_list:
            logger.info(
                f"[SoftDeny] sender_id={sender_id} "
                f"不在 bot_list 中，跳过"
            )
            return

        req.system_prompt = (
            req.system_prompt or ""
        ) + self.deny_system_prompt_extra
        logger.info(
            f"[SoftDeny] ✅ 已对用户 {sender_id} 注入受限 prompt "
            f"(总长度 {len(req.system_prompt)})"
        )

    # ============================================================
    # 全局消息监听 — 回复匹配
    # ============================================================

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """所有消息经过此处：回复匹配 + 消息采集 + 定时分析"""
        await self.reply_waiter.check_reply(event)

        # 消息采集（仅受控群）
        if self._check_group(event):
            self.auto_analyzer.collect(event)

        # 定时自动分析
        if self.auto_analyzer.should_run_now(self.auto_analyze_crontab):
            funcs = await self.auto_analyzer.analyze_and_save()
            if funcs and self.auto_reg_bot_functions:
                report = await self.auto_analyzer.auto_register(funcs)
                logger.info(f"[AutoAnalyzer] 定时自动注册报告: {report[:200]}")

    # ============================================================
    # 命令: 重载动态功能
    # ============================================================

    @filter.command("重载功能")
    async def reload_functions(self, event: AstrMessageEvent):
        """重新加载 functions.json 中的动态 LLM 工具。

        用法: /重载功能
        修改 functions.json 后无需重启插件，执行此命令即可。
        """
        names = await self.dynamic_funcs.reload_all()
        if names:
            yield event.plain_result(
                f"✅ 已重载 {len(names)} 个动态工具:\n" +
                "\n".join(f"  - {n}" for n in names)
            )
        else:
            yield event.plain_result("ℹ️ 配置文件为空或无有效条目")

    # ============================================================
    # 命令: 自动分析
    # ============================================================

    @filter.command("自动分析")
    async def auto_analyze(self, event: AstrMessageEvent):
        """使用 LLM 分析已采集的群聊消息，发现潜在 Bot 功能。

        用法:
          /自动分析                        → 当前群最近 50 条
          /自动分析 20                     → 当前群最近 20 条
          /自动分析 --group all            → 所有群全部消息
          /自动分析 --group 1077781248     → 指定群最近 50 条
          /自动分析 30 --group 1077781248  → 指定群最近 30 条
        """
        if not self.analyze_provider_id:
            yield event.plain_result("⚠️ 未配置 analyze_provider_id，无法分析")
            return

        # 解析参数
        target_umo, limit = self._parse_analyze_args(
            event.message_str.strip(), event.unified_msg_origin
        )

        desc = (
            f"全部群" if target_umo is None and limit is None
            else f"群 {target_umo}" if target_umo
            else "当前群"
        )
        yield event.plain_result(f"🔍 正在分析 {desc} 的消息...")

        funcs = await self.auto_analyzer.analyze_and_save(
            target_umo=target_umo, limit=limit
        )

        if funcs:
            lines = [
                f"- {f['name']}: {f.get('description', '')[:50]}"
                for f in funcs
            ]
            if self.auto_reg_bot_functions:
                yield event.plain_result(
                    f"✅ 发现 {len(funcs)} 个潜在功能，开始自动测试:\n"
                    + "\n".join(lines)
                )
                report = await self.auto_analyzer.auto_register(funcs)
                yield event.plain_result(report)
            else:
                yield event.plain_result(
                    f"✅ 发现 {len(funcs)} 个潜在功能，已保存到 "
                    f"_analyzed_functions.json:\n" + "\n".join(lines)
                    + "\n\n审查后可合并到 functions.json 并 /重载功能"
                )
        else:
            yield event.plain_result(
                "ℹ️ 未发现可注册的功能"
            )

    @staticmethod
    def _parse_analyze_args(
        arg_str: str, current_umo: str
    ) -> tuple[str | None, int | None]:
        """解析 /自动分析 的参数。

        Returns:
            (target_umo, limit):
            - target_umo: None=全部群 / str=指定群UMO
            - limit: None=全部 / int=条数限制
            默认: (current_umo, 50)
        """
        # 去掉命令前缀 /自动分析
        arg_str = arg_str.strip()
        parts = arg_str.split()

        target_umo: str | None = current_umo  # 默认当前群
        limit: int | None = 50                 # 默认 50 条
        all_groups = False

        i = 0
        while i < len(parts):
            if parts[i] == "--group":
                i += 1
                if i < len(parts):
                    if parts[i] == "all":
                        all_groups = True
                        target_umo = None
                    else:
                        target_umo = f"default:GroupMessage:{parts[i]}"
                    i += 1
                continue

            # 尝试解析为数字
            try:
                limit = int(parts[i])
                i += 1
                continue
            except ValueError:
                pass

            # 未知参数，跳过
            i += 1

        if all_groups:
            limit = None  # --group all → 不限条数

        return target_umo, limit

    # ============================================================
    # 命令: 等待状态
    # ============================================================

    @filter.command("等待状态")
    async def pending_status(self, event: AstrMessageEvent):
        """查看当前待回复请求"""
        count = len(self.reply_waiter._pending)
        if count == 0:
            yield event.plain_result("📭 当前没有待回复的请求")
        else:
            lines = [
                f"- `{p.target_session}` (id={p.request_id[:8]})"
                for p in self.reply_waiter._pending.values()
            ]
            yield event.plain_result(
                f"📬 当前有 {count} 个待回复请求:\n" + "\n".join(lines)
            )

    # ============================================================
    # 生命周期
    # ============================================================

    async def terminate(self):
        """插件卸载时清理"""
        logger.info("UniversalPlugin 已卸载")