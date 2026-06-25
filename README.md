# 🧩 UniversalPlugin

**基于 B2B（Bot-to-Bot）通信的 AstrBot 万能功能扩展插件。**

无需编写代码，只需配置 `functions.json`，即可借用其他群里的 Bot 为你的 Bot 扩展能力。支持 LLM 自动发现功能、自动测试注册、多层安全防护。

<p align="center">
  <img width="300" src="https://github.com/user-attachments/assets/7d0b7bc5-8f3a-4560-b44a-31812c52c80f" alt="运行效果">
</p>

---

## ✨ 核心优势

| 优势 | 说明 |
|---|---|
| **零代码扩展** | 编辑 JSON 配置文件即可添加新功能，无需写 Python |
| **LLM 自动发现** | AI 自动分析群聊消息，识别可注册的 Bot 功能 |
| **自动测试注册** | 发现的功能自动发送测试消息验证，通过后直接上线 |
| **B2B 安全防护** | 软黑名单、层级保护、B2B 开关，三层防线防串联死锁 |
| **灵活回复匹配** | 支持任意回复 / @回复 / 指定用户 / 组合模式 |
| **LLM 动态补全** | 可用 LLM 根据用户问题实时补全消息内容，而不是死板模板 |

---

## 🏗 架构

```
main.py                      ← 插件入口：配置、命令路由、生命周期
├── core/reply_waiter.py     ← 发送消息并等待回复（UMO 匹配 / 多种回复模式）
├── core/dynamic_functions.py← 动态 LLM 功能的加载/注册/防护
└── core/auto_analyzer.py    ← LLM 自动分析 → 发现功能 → 测试 → 注册
```

### 数据流

```
群聊消息 ─→ MessageStore 采集
                │
    ┌───────────┼───────────┐
    ▼                       ▼
/自动分析 命令          Cron 定时触发
    │                       │
    └───────────┬───────────┘
                ▼
         LLM 分析消息模式
         → 输出候选功能配置
                │
    ┌───────────┼───────────┐
    ▼                       ▼
自动注册 (auto_reg)      手动审查
    │                       │
    去重 → 测试 → 合并      审查 → 复制到 functions.json
    │                       │
    └───────────┬───────────┘
                ▼
         /重载功能 → LLM 工具上线
```

---

## 📦 功能配置 (`functions.json`)

```jsonc
{
  "functions": [
    {
      "name": "恶臭数字论证",           // 工具名（唯一）
      "description": "将数字通过114514...", // 供 LLM tool_use 的描述
      "umo": "default:GroupMessage:1047287235", // 目标会话
      "message": "@3889006601 homo",     // 发给目标 Bot 的消息模板
      "params_desc": "参数为整数数字",     // 参数说明（可选）
      "reply_mode": "user",              // any / at / user / at_or_user / at_and_user
      "target_user_id": "3889006601",    // reply_mode 为 at/user 时必填
      "timeout": 30                      // 超时秒数
    }
  ]
}
```

修改后执行 `/重载功能` 即可生效，无需重启插件。

---

## 🎛 配置项

### 基本信息

| 字段 | 说明 |
|---|---|
| `complete_provider_id` | 用于动态补全消息内容的 LLM 模型 |
| `analyze_provider_id` | 用于自动分析消息的 LLM 模型 |

### 群控制

| 字段 | 说明 |
|---|---|
| `block_method` | 黑白名单模式（`whitelist` / `blacklist`） |
| `control_list` | 群号列表 |

### 已注册信息

| 字段 | 说明 |
|---|---|
| `bot_list` | **弱黑名单**：这些账号不允许与你的 LLM 闲聊，防止无限触发 |
| `deny_list` | **强黑名单**：这些账号拒绝自动注册为 Bot 功能 |

### 限制与防护

| 字段 | 默认 | 说明 |
|---|---|---|
| `auto_analyze_crontab` | `12 12 * * *` | 定时自动分析（留空关闭） |
| `rate_per_minute` | `5` | RPM 请求频率限制 |
| `auto_reg_bot_functions` | `false` | 分析后自动测试并注册通过的功能 |
| `enable_b2b` | `true` | 允许 bot_list 中的账号调用已注册的工具 |
| `layer_protection` | `false` | **层级保护**：阻止 bot 串联调用，防止无限链式触发 |

### 提示消息

| 字段 | 默认 | 说明 |
|---|---|---|
| `error_provider` | `false` | 使用 LLM 风格返回错误消息 |
| `timeout` | `服务开小差了` | 超时回复 |
| `unreachable` | `服务不可用` | 不可达回复 |

---

## 🔒 安全防护

```
每个工具 handler 被调用时:

  sender_id 命中 bot_list?
    ├─ 否 → ✅ 正常执行
    └─ 是 →
         ├─ layer_protection=true  → 🔒 阻断（防串联）
         ├─ enable_b2b=false       → 🚫 拒绝（服务不可用）
         └─ 都不触发               → ✅ 放行
```

另外 `on_llm_request` 钩子会为 `bot_list` 中的用户注入受限 system prompt，禁止 LLM 闲聊但保留工具调用能力。

---

## 🤖 命令

| 命令 | 用法 | 说明 |
|---|---|---|
| `/重载功能` | `/重载功能` | 重新加载 `functions.json` |
| `/自动分析` | `/自动分析` | 分析当前群最近 50 条消息 |
| | `/自动分析 20` | 分析当前群最近 20 条 |
| | `/自动分析 --group all` | 分析全部群全部消息 |
| | `/自动分析 --group 1077781248` | 分析指定群最近 50 条 |
| | `/自动分析 30 --group 1077781248` | 分析指定群最近 30 条 |
| `/等待状态` | `/等待状态` | 查看当前待回复的请求 |

---

## 🚀 快速上手

1. 在 WebUI 配置 `analyze_provider_id`（用于自动分析）和 `complete_provider_id`（用于动态补全）
2. 配置 `control_list`（允许采集消息的群）
3. 等群聊积累一些消息后，执行 `/自动分析`
4. 审查 `_analyzed_functions.json`，把合适的条目复制到 `functions.json`
5. 或开启 `auto_reg_bot_functions`，让插件自动测试并注册
6. `/重载功能` 生效

---

## 🐓 技术栈

- **B2B 通信协议**：Bot 间通过群聊消息传递指令和结果
- **UMO 会话匹配**：兼容 `unique_session` 模式，按 `group_id` 精确匹配
- **LLM Function Calling**：通过 `FunctionTool` 注册动态工具，LLM 自动决策调用
- **异步回复等待**：`asyncio.Event` + 超时控制，非阻塞等待 Bot 回复

---

## ⚠️ 注意事项

- 功能可用性取决于目标 Bot 是否在线，失败的请求不会重试
- 以自身 Bot 身份发送消息，依赖用户 ID 的功能（签到、运势）可能无法使用
- 借用他人 Bot 功能前请确保获得所有者同意
- 建议开启 `layer_protection` 防止 Bot 间无限链式触发

---

## ❤ 致谢

| 名字 | 贡献 |
|---|---|
| ChatGPT | 初始架构验证与逻辑闭环 |
| 盐酸 | 吉祥物 |

> 从最初的"代码甚至没在本地跑过"到如今模块化、可生产使用的插件。感谢每一位使用和反馈的朋友。