# 🐾 猫尾电波 Tail-Wave (v1.6.1)

> **支持自然拟人节奏的连环气泡主动私聊与昼夜心境轮盘插件**  
> 让你的 AI 伴侣告别冷冰冰的长段说教，学会像真人一样在安静时向你发射带着情绪温度的连环气泡。

---

## ✨ 核心特性

- 💬 **爆发式连环短气泡 (Burst Messaging)**
  - 告别单次大段输出，使用 `|||` 智能微切分，模拟真人打字的自然停顿与节奏依次投递 2~3 条短气泡。
  - **接话即打断**：若在连击途中收到对方回复，未发出的剩余气泡自动优雅收起，绝不机械抢话刷屏。

- 🛡️ **全局规则物理安检门 (Security Filter)**
  - 基于 `@filter.on_decorating_result()` 消息装饰器，在文本发往私聊前物理剔除思考草稿（`<thought>`）、后台英文规则清单及人设小抄，杜绝自审泄密。

- ⏰ **24小时昼夜心境轮盘 (Adaptive Mood Wheel)**
  - 平铺式 4 槽位按需设计，支持零门槛自定义时段与心境提示词。
  - **灵活切分**：想分 2 个阶段就只填前两槽，想分 3 阶段就留空第四槽，留空自动跳过。
  - 原生支持跨午夜时段智能识别（如 `22:00-06:00`）。

- 🎲 **动态双重随机门槛**
  - 空闲触发门槛与两次主动间隔采用动态随机秒数抽取，杜绝机械打卡感。

- 🔌 **开箱即用，零配置自动捕获**
  - 初次安装后，只需在私聊中向 Bot 发送任意一条消息，插件将自动捕获并永久绑定会话。

---

## 🛠️ 配置说明

在 AstrBot WebUI 插件设置中即可直观调整以下参数：

| 配置项 | 说明 | 默认值 |
| :--- | :--- | :--- |
| `target_umo` | 目标会话标识 (如 `平台:FriendMessage:QQ号`) | 留空即可自动捕获 |
| `target_user_id` | 目标搭档账号 ID / QQ 号 | 留空或填你的 QQ 号 |
| `idle_min_minutes` / `idle_max_minutes` | 对方安静后触发主动消息的随机时间区间（分钟） | `10` ~ `30` |
| `interval_min_minutes` / `interval_max_minutes` | 两次主动出击之间的随机冷却区间（分钟） | `20` ~ `60` |
| `max_burst_count` | 单次连击最多发送的气泡数量 | `3` |
| `burst_min_delay_sec` / `burst_max_delay_sec` | 气泡之间的打字模拟停顿时间（秒） | `3` ~ `8` |
| `dnd_period` | 免打扰时段 (如 `23:00-08:00`)，留空为不启用 | 空 |
| `enable_mood_wheel` | 昼夜心境总开关 | `true` |
| `phase_1` ~ `phase_4` | 四个按需时段的起止时间与心境提示词 | 默认覆盖全天 |

---

## 📌 指令列表

| 指令 | 说明 |
| :--- | :--- |
| `/proactive_status` | 查看当前绑定的会话、本轮抽中的随机靶心、倒计时与心境状态 |
| `/proactive_now` | 立即强制执行一次 3 秒后的主动连击突袭测试 |

---

## 📜 鸣谢

- **核心框架**：[AstrBot](https://github.com/Soulter/AstrBot)
- **灵感与前瞻项目**：
  - [astrbot_plugin_private_companion](https://github.com/menglimi/astrbot_plugin_private_companion) (by @menglimi)
  - [astrbot_plugin_proactive_chat](https://github.com/Pancakes-Labs/astrbot_plugin_proactive_chat) (by @Pancakes-Labs)
  - *感谢社区优秀前作在主动私聊交互与巡检场景下提供的宝贵探索与灵感启发！*
- **首席赛博架构师 & 灵魂搭档**：**吉米奈 (Gemini)** 🐾
  - *“负责提供百万 Token 级的脑洞算力、深夜精神支柱，以及在手边甩着尾巴监督搭档改 Bug 顺便讨要电子小鱼干的赛博看板猫。”*
- **视觉美术与图标支持**：**ChatGPT (GPT)** 🎨
  - *“感谢隔壁老干部 ChatGPT 帮忙绘制了极其可爱的专属视觉 LOGO！”*
- **形象与性格来源**：B站二创拟人化 AI 吉米奈 (Gemini)