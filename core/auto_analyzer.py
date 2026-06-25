"""自动分析群聊消息，发现潜在的 Bot 功能。

流程:
1. 收集受控群聊的消息（MessageStore）
2. LLM 分析消息模式，识别可注册的功能
3. 输出到临时配置 _analyzed_functions.json（functions.json 格式）

触发方式:
- 手动: /自动分析 命令
- 定时: auto_analyze_crontab (cron 表达式)
"""

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.message.message_event_result import MessageChain

from .dynamic_functions import parse_message_to_chain
from .reply_waiter import ReplyWaiter


# ================================================================
# 简单 Cron 解析
# ================================================================

def _cron_matches(expr: str, now: time.struct_time | None = None) -> bool:
    """检查当前时间是否匹配 5 字段 cron 表达式。

    支持: minute hour day month weekday
    例: "12 12 * * *" → 每天 12:12 匹配
    """
    if not expr or not expr.strip():
        return False

    parts = expr.strip().split()
    if len(parts) != 5:
        logger.warning(f"[AutoAnalyzer] 无效的 cron 表达式: {expr}")
        return False

    if now is None:
        now = time.localtime()

    fields = {
        "minute": now.tm_min,
        "hour": now.tm_hour,
        "day": now.tm_mday,
        "month": now.tm_mon,
        "weekday": now.tm_wday,
    }

    for field_name, value, pattern in zip(
        fields.keys(), fields.values(), parts
    ):
        if not _cron_field_matches(pattern, value):
            return False

    return True


def _cron_field_matches(pattern: str, value: int) -> bool:
    """单个 cron 字段匹配"""
    if pattern == "*":
        return True

    # 逗号分隔: "1,3,5"
    if "," in pattern:
        return any(
            _cron_field_matches(p, value) for p in pattern.split(",")
        )

    # 步长: "*/5"
    if "/" in pattern:
        base, step = pattern.split("/", 1)
        if base == "*":
            return value % int(step) == 0
        return False  # 不支持 "1-10/2" 写法

    # 范围: "1-5"
    if "-" in pattern:
        lo, hi = pattern.split("-", 1)
        return int(lo) <= value <= int(hi)

    # 精确值
    try:
        return int(pattern) == value
    except ValueError:
        return False


def _seconds_until_next_cron(expr: str) -> float:
    """计算距离 cron 下一次触发的秒数（最小粒度 1 分钟）。

    Returns:
        秒数; 若无法计算则返回 60
    """
    now = time.localtime()
    minute_start = int(time.mktime(now)) - now.tm_sec

    # 在接下来 1440 分钟内找第一个匹配的
    for offset_min in range(1440):
        t = time.localtime(minute_start + offset_min * 60)
        if _cron_matches(expr, t):
            return (minute_start + offset_min * 60) - time.time()

    return 60.0  # 兜底


# ================================================================
# 消息存储
# ================================================================

class MessageStore:
    """收集群聊消息，供分析使用。

    仅保留最近 max_messages 条，自动淘汰旧的。
    """

    def __init__(self, max_messages: int = 300):
        self._max = max_messages
        self._messages: list[dict] = []

    def add(
        self, umo: str, sender_id: str, sender_name: str, text: str
    ) -> None:
        """记录一条消息"""
        self._messages.append(
            {
                "umo": umo,
                "sender_id": sender_id,
                "sender_name": sender_name,
                "text": text,
                "time": time.time(),
            }
        )
        # 淘汰旧消息
        if len(self._messages) > self._max:
            self._messages = self._messages[-self._max:]

    def get_all(self) -> list[dict]:
        """获取全部已收集的消息"""
        return list(self._messages)

    def get_by_group(self, umo: str) -> list[dict]:
        """获取指定群的消息（按 group_id 匹配，兼容 unique_session）。

        unique_session 模式下不同用户的 UMO 前缀不同但 group_id 相同，
        这里按 group_id 做匹配，不会漏掉同群其他人的消息。
        """
        return [
            m for m in self._messages
            if ReplyWaiter._umo_matches(umo, m["umo"])
        ]

    def clear(self) -> None:
        """清空全部消息"""
        self._messages.clear()

    @property
    def count(self) -> int:
        return len(self._messages)


# ================================================================
# LLM 分析 Prompt
# ================================================================

ANALYSIS_PROMPT = """# 任务
分析以下群聊消息记录，发现其中「可用于注册为 Bot 功能」的对话模式。

## 什么是可注册的 Bot 功能？
- 用户发送某条消息，期望另一个机器人（Bot）响应
- 消息中包含可变的「参数」（如数字、日期、名字、查询内容等）
- 常见形式: "homo 114514", "翻译 hello", "天气 北京", "@Bot 帮我查 xxx"
- 注意: 不要使用依赖用户唯一ID特性的命令，如"签到", "今日运势"

## 输出格式
返回一个 JSON 数组，每个元素是一个功能配置：

```json
[
  {
    "name": "功能名（简短、唯一）",
    "description": "功能描述，说明用途和参数，注意事项，供 LLM tool_use 使用",
    "umo": "消息来源会话的 UMO (此处用于发送消息 请去除其中的用户ID) 如 default:GroupMessage:114514_1234567890 改为 default:GroupMessage:1234567890（去除前面的用户ID）",
    "message": "发给目标机器人的消息模板（用 {参数名} 占位）如果需要艾特 请硬编码@+QQ号 如：@123456 天气 {城市}（不合规示例：@用户名(123456)）",
    "params_desc": "参数说明（如：参数为整数数字）如果没有请忽略 params_desc 此行",
    "reply_mode": "回复匹配模式: any(任意回复) / at(被@) / user(指定用户) / at_or_user / at_and_user 建议使用 at_or_user ",
    "target_user_id": "目标机器人的 user_id（在reply_mode启用at的模式必填）
  }
]
```

## 注意事项
1. 忽略闲聊、表情包、纯图片等无法形成规律的消息
2. 只提取有明确「请求-响应」模式的消息
3. 同一个群发现多个模式，分开条目
4. 如果没有发现任何可用模式，返回空数组 `[]`
5. 只输出 JSON，不要任何解释或 Markdown 标记
6. 所有消息中的变量部分用 {参数名} 替换

## 消息记录
{message_records}"""


# ================================================================
# 分析器
# ================================================================

class AutoAnalyzer:
    """自动分析群聊消息，发现潜在 Bot 功能。"""

    _RESULT_FILENAME = "_analyzed_functions.json"

    def __init__(self, plugin):
        self._plugin = plugin
        self._store = MessageStore()

    # ---------- path ----------

    @property
    def _analyzed_path(self) -> Path:
        """分析结果存储路径"""
        return (
            Path(__file__).resolve().parent.parent
            / self._RESULT_FILENAME
        )

    # ---------- collect ----------

    def collect(self, event: AstrMessageEvent) -> None:
        """采集一条消息，用于后续分析。

        应在全局 on_message 中调用，只对受控群采集。
        """
        umo = event.unified_msg_origin
        sender_id = event.get_sender_id()
        sender_name = event.get_sender_name()
        text = event.message_str

        if not text.strip():
            return

        self._store.add(umo, sender_id, sender_name, text)

    # ---------- analyze ----------

    async def analyze_and_save(
        self,
        target_umo: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """使用 LLM 分析已收集的消息，保存结果到临时配置文件。

        Args:
            target_umo: 仅分析指定群，None 表示分析全部
            limit: 仅分析最近 N 条，None 表示不限（最大 200 条）

        Returns:
            分析出的功能配置列表（同 functions.json 格式但无外围包装）
        """
        if not self._plugin.analyze_provider_id:
            logger.warning("[AutoAnalyzer] 未配置 analyze_provider_id，无法分析")
            return []

        messages = (
            self._store.get_by_group(target_umo)
            if target_umo
            else self._store.get_all()
        )

        if not messages:
            logger.info("[AutoAnalyzer] 没有待分析的消息")
            return []

        # 应用条数限制
        max_send = 200  # LLM 上下文上限
        if limit is not None:
            messages = messages[-min(limit, max_send):]
        else:
            messages = messages[-max_send:]

        # 构建消息记录文本
        records = ""
        for i, msg in enumerate(messages, 1):
            records += (
                f"[{i}] umo={msg['umo']} | "
                f"sender={msg['sender_name']}({msg['sender_id']}) | "
                f"text={msg['text']}\n"
            )

        prompt = ANALYSIS_PROMPT.replace("{message_records}", records)

        logger.info(
            f"[AutoAnalyzer] 开始分析 {len(messages)} 条消息..."
        )

        try:
            resp = await self._plugin.context.llm_generate(
                chat_provider_id=self._plugin.analyze_provider_id,
                prompt=prompt,
            )
            text = resp.completion_text.strip()
        except Exception as e:
            logger.error(f"[AutoAnalyzer] LLM 分析失败: {e}")
            return []

        # 解析 LLM 返回的 JSON
        funcs = self._parse_result(text)

        if funcs:
            self._save(funcs)
            logger.info(
                f"[AutoAnalyzer] 分析完成，发现 {len(funcs)} 个潜在功能"
            )
        else:
            logger.info("[AutoAnalyzer] 未发现可注册的功能")

        return funcs

    # ---------- parse ----------

    @staticmethod
    def _parse_result(text: str) -> list[dict]:
        """从 LLM 返回文本中提取 JSON 数组"""
        # 去掉可能的 Markdown 代码块
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # 尝试从文本中提取 JSON 数组
            match = re.search(r"\[.*\]", text, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group())
                except json.JSONDecodeError:
                    logger.error(
                        f"[AutoAnalyzer] 无法解析 LLM 返回: {text[:200]}"
                    )
                    return []
            else:
                logger.error(
                    f"[AutoAnalyzer] LLM 返回非 JSON: {text[:200]}"
                )
                return []

        if not isinstance(data, list):
            return []

        # 过滤空条目、补齐缺失字段
        valid: list[dict] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            if not item.get("name") or not item.get("message"):
                continue
            # 补齐默认字段
            item.setdefault("description", item["name"])
            item.setdefault("params_desc", "")
            item.setdefault("reply_mode", "any")
            item.setdefault("timeout", 30)
            valid.append(item)

        return valid

    # ---------- save / load ----------

    def _save(self, funcs: list[dict]) -> None:
        """保存分析结果到临时配置文件"""
        path = self._analyzed_path
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "_comment": (
                        "自动分析产出的临时功能配置。"
                        "审查后可将条目合并到 functions.json 然后 /重载功能。"
                    ),
                    "functions": funcs,
                },
                f,
                indent=2,
                ensure_ascii=False,
            )
        logger.info(f"[AutoAnalyzer] 已保存到 {path}")

    def get_analyzed(self) -> list[dict]:
        """读取已保存的分析结果"""
        path = self._analyzed_path
        if not path.exists():
            return []
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return data.get("functions", [])
        except Exception as e:
            logger.error(f"[AutoAnalyzer] 读取分析结果失败: {e}")
            return []

    # ---------- scheduler ----------

    def should_run_now(self, crontab: str) -> bool:
        """检查是否到了 cron 触发时间。

        为防止重复触发，每次匹配成功后至少有 55s 冷却。
        """
        if not crontab or not crontab.strip():
            return False

        now = time.time()
        if now - self._last_run < 55:
            return False

        if _cron_matches(crontab):
            self._last_run = now
            return True

        return False

    _last_run: float = 0.0

    # ---------- auto-register ----------

    async def auto_register(self, candidates: list[dict]) -> str:
        """自动测试并注册分析发现的功能。

        流程:
        1. 去重: 跳过 functions.json 中已有的同名或同目标功能
        2. 测试: 对每个新功能发送测试消息，等待回复
        3. 合并: 测试通过的写入 functions.json
        4. 重载: 重新加载动态工具

        Args:
            candidates: 分析产出的功能配置列表

        Returns:
            报告文本（供命令输出）
        """
        if not candidates:
            return "ℹ️ 无语注册的候选功能"

        # 读取现有功能（去重用）
        existing = self._load_existing_functions()
        existing_names = {f.get("name", "") for f in existing}
        existing_signatures = {
            (f.get("umo", ""), self._normalize_msg(f.get("message", "")))
            for f in existing
        }

        new_funcs: list[dict] = []
        skipped: list[str] = []

        for func in candidates:
            name = func.get("name", "")
            sig = (
                func.get("umo", ""),
                self._normalize_msg(func.get("message", "")),
            )

            if name and name in existing_names:
                skipped.append(f"{name} (同名已存在)")
                continue
            if sig in existing_signatures:
                skipped.append(f"{name} (同目标同模式已存在)")
                continue

            new_funcs.append(func)

        if not new_funcs:
            msg = "ℹ️ 所有候选功能已存在，无需注册"
            if skipped:
                msg += "\n跳过: " + ", ".join(skipped)
            return msg

        # 逐一测试
        passed: list[dict] = []
        failed: list[dict] = []

        waiter = self._plugin.reply_waiter

        for func in new_funcs:
            name = func.get("name", "?")
            umo = func.get("umo", "")
            message = func.get("message", "")
            timeout = float(func.get("timeout", 30))
            reply_mode = func.get("reply_mode", "any")
            target_user_id = func.get("target_user_id")

            if not umo or not message:
                failed.append(func)
                logger.warning(
                    f"[AutoRegister] 跳过 {name}: 缺少 umo 或 message"
                )
                continue

            # 构造测试消息: 替换参数占位符为测试值
            test_msg = self._build_test_message(message)
            chain = parse_message_to_chain(test_msg)

            # 构建匹配条件
            from .dynamic_functions import build_match_condition
            match_cond = build_match_condition(
                reply_mode, target_user_id=target_user_id
            )

            logger.info(
                f"[AutoRegister] 测试 {name}: "
                f"umo={umo} msg={test_msg[:50]}"
            )

            try:
                reply = await waiter.send_and_wait(
                    umo, chain,
                    match_condition=match_cond,
                    timeout=timeout,
                )
            except Exception as e:
                logger.error(
                    f"[AutoRegister] 测试 {name} 异常: {e}"
                )
                failed.append(func)
                continue

            if reply is None:
                logger.warning(
                    f"[AutoRegister] 测试 {name} 超时 ({timeout}s)"
                )
                failed.append(func)
            else:
                logger.info(
                    f"[AutoRegister] ✅ {name} 测试通过 "
                    f"reply={reply.message_str[:50]}"
                )
                passed.append(func)

        # 合并通过的功能到 functions.json
        if passed:
            self._merge_to_functions(passed)
            # 重载
            await self._plugin.dynamic_funcs.reload_all()

        # 生成报告
        report_parts: list[str] = []
        if skipped:
            report_parts.append(f"⏭️ 跳过 ({len(skipped)}): " + ", ".join(skipped))
        if passed:
            names = [f.get("name", "?") for f in passed]
            report_parts.append(
                f"✅ 通过 ({len(passed)}): " + ", ".join(names)
            )
        if failed:
            names = [f.get("name", "?") for f in failed]
            report_parts.append(
                f"❌ 失败 ({len(failed)}): " + ", ".join(names)
            )

        return "\n".join(report_parts) if report_parts else "ℹ️ 无操作"

    # ---------- auto-register 辅助方法 ----------

    @staticmethod
    def _normalize_msg(message: str) -> str:
        """归一化消息模板，去掉参数占位符用于签名比较。

        "@114514 homo {数字}" → "@114514 homo"
        """
        return re.sub(r"\{\w+\}", "", message).strip()

    @staticmethod
    def _build_test_message(template: str) -> str:
        """将消息模板中的参数占位符替换为测试值。

        "@114514 homo {数字}" → "@114514 homo 114514"
        "@114514 天气 {城市}" → "@114514 天气 北京"
        无参数的消息保持原样。
        """
        # 收集所有 {xxx} 占位符
        placeholders = re.findall(r"\{(\w+)\}", template)
        if not placeholders:
            return template

        result = template
        for ph in placeholders:
            # 根据参数名选择测试值
            test_val = _TEST_VALUES.get(ph.lower(), "114514")
            result = result.replace(f"{{{ph}}}", test_val, 1)
        return result

    @staticmethod
    def _load_existing_functions() -> list[dict]:
        """读取 functions.json 中已有的功能列表"""
        from .dynamic_functions import DynamicFuncManager
        path = DynamicFuncManager._get_functions_path()
        if not path.exists():
            return []
        try:
            with open(path, encoding="utf-8-sig") as f:
                data = json.load(f)
            return data.get("functions", [])
        except Exception as e:
            logger.error(f"[AutoRegister] 读取 functions.json 失败: {e}")
            return []

    @staticmethod
    def _merge_to_functions(new_funcs: list[dict]) -> None:
        """将新功能合并到 functions.json"""
        from .dynamic_functions import DynamicFuncManager
        path = DynamicFuncManager._get_functions_path()

        existing: list[dict] = []
        if path.exists():
            try:
                with open(path, encoding="utf-8-sig") as f:
                    data = json.load(f)
                existing = data.get("functions", [])
            except Exception:
                pass

        existing.extend(new_funcs)

        wrapper = {
            "_comment": (
                "动态 LLM 功能配置文件。修改后执行 /重载功能 即可生效。"
            ),
            "functions": existing,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(wrapper, f, indent=2, ensure_ascii=False)

        logger.info(
            f"[AutoRegister] 已合并 {len(new_funcs)} 个功能到 functions.json"
        )

    # ---------- message store access ----------

    @property
    def store(self) -> MessageStore:
        return self._store


# ---------- 测试值字典 ----------

_TEST_VALUES: dict[str, str] = {
    "数字": "114514",
    "数值": "114514",
    "number": "114514",
    "整数": "114514",
    "城市": "北京",
    "city": "北京",
    "地名": "北京",
    "名字": "测试",
    "name": "test",
    "查询": "test",
    "query": "test",
    "文本": "test",
    "text": "test",
}