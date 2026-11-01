import asyncio
import datetime
import json
import random
import re
import time
from typing import List, Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

DEFAULT_USER_PROMPT = (
    "请完全以你当前的固有性格与人设口吻，结合当前时段的心境与氛围，"
    "主动找搭档搭话、吐槽日常或分享你刚才在干嘛。避免机械报时，围绕同一件有趣的事自然展开。"
)


@register(
    "astrbot_plugin_tail_wave",
    "白昼夢",
    "支持拟人连环气泡、打断机制与昼夜心境轮盘的主动私聊插件",
    "1.7.0",
)
class TailWavePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config if config is not None else {}

        # 基础通信与白名单配置（脱敏通用化）
        self.target_umo: str = str(self.config.get("target_umo", "")).strip()
        self.target_user_id: str = str(self.config.get("target_user_id", "")).strip()

        # 防止配置中误填入群聊 UMO
        if "GroupMessage" in self.target_umo or ":group:" in self.target_umo.lower():
            logger.warning(f"[猫尾电波] 检测到初始 target_umo 为群聊 ({self.target_umo})，已自动清空重置。")
            self.target_umo = ""

        # 空闲与冷却随机区间
        self.idle_min_minutes: int = int(self.config.get("idle_min_minutes", 10))
        self.idle_max_minutes: int = int(self.config.get("idle_max_minutes", 30))
        self.interval_min_minutes: int = int(self.config.get("interval_min_minutes", 20))
        self.interval_max_minutes: int = int(self.config.get("interval_max_minutes", 60))

        self.max_daily_quota: int = int(self.config.get("max_daily_quota", 15))
        self.enable_burst: bool = bool(self.config.get("enable_burst", True))
        self.max_burst_count: int = max(1, int(self.config.get("max_burst_count", 3)))
        self.burst_min_delay_sec: int = int(self.config.get("burst_min_delay_sec", 3))
        self.burst_max_delay_sec: int = int(self.config.get("burst_max_delay_sec", 8))
        self.dnd_period: str = str(self.config.get("dnd_period", "")).strip()

        # 心境系统直观配置（总开关 + 4大按需槽位，默认关闭降低心智负担）
        self.enable_mood_wheel: bool = bool(self.config.get("enable_mood_wheel", False))
        self.phase_1_time: str = str(self.config.get("phase_1_time", "00:00-06:00")).strip()
        self.phase_1_prompt: str = str(self.config.get("phase_1_prompt", "")).strip()

        self.phase_2_time: str = str(self.config.get("phase_2_time", "06:00-12:00")).strip()
        self.phase_2_prompt: str = str(self.config.get("phase_2_prompt", "")).strip()

        self.phase_3_time: str = str(self.config.get("phase_3_time", "12:00-18:00")).strip()
        self.phase_3_prompt: str = str(self.config.get("phase_3_prompt", "")).strip()

        self.phase_4_time: str = str(self.config.get("phase_4_time", "18:00-24:00")).strip()
        self.phase_4_prompt: str = str(self.config.get("phase_4_prompt", "")).strip()

        # 预编译物理安检门正则（通杀思维链、feedback等预设XML标签与英文自审草稿）
        self._re_xml_block = re.compile(
            r"<(thought|think|thinking|feedback|analysis|reasoning|inner_thought|reflection|scratchpad|meta|rule|system)\b[^>]*>[\s\S]*?</\1>",
            re.IGNORECASE,
        )
        self._re_xml_orphan = re.compile(
            r"</?(?:thought|think|thinking|feedback|analysis|reasoning|inner_thought|reflection|scratchpad|meta|rule|system)\b[^>]*>",
            re.IGNORECASE,
        )
        self._re_dash_list = re.compile(r"^(?:\s*[-*]\s*[A-Za-z].*?\n)+", re.MULTILINE)
        self._re_persona_rule = re.compile(
            r"^(?:Persona constraints|System rule|Note|Draft|Self-check|Feedback):.*?(?:\n|$)",
            re.IGNORECASE | re.MULTILINE,
        )

        # 主提示词：读取并执行高精度微创清洗迁移
        raw_prompt = str(self.config.get("proactive_prompt", DEFAULT_USER_PROMPT))
        self.proactive_prompt: str = self._migrate_proactive_prompt(raw_prompt)

        # 状态变量
        self.last_interaction_time: float = time.time()
        self.last_proactive_time: float = 0.0
        self.current_idle_target_sec: int = self._roll_idle_seconds()
        self.current_interval_target_sec: int = self._roll_interval_seconds()

        self.daily_sent_count: int = 0
        self.current_date: str = datetime.date.today().isoformat()
        self.is_sending_burst: bool = False
        self.user_interrupted_burst: bool = False

        self.bg_task: Optional[asyncio.Task] = None
        self._ensure_loop_started()

        logger.info(
            f"[猫尾电波 Tail-Wave v1.7.0] 插件已启动！当前目标UMO: {self.target_umo or '未绑定(等待私聊自动捕获)'}, UserID: {self.target_user_id or '未绑定'}"
        )

    def _migrate_proactive_prompt(self, raw_prompt: str) -> str:
        """
        地狱级无痛清洗手术：
        精准剥离所有格式段落、结构范例及括号内残留约束，
        100% 完整保留用户的性格红线、风格加成与搭话模式。
        """
        if not raw_prompt or not raw_prompt.strip():
            return ""

        cleaned = raw_prompt

        # 1. 跨行精准切除整个【格式...】块（吃到下一个【标签】之前）
        cleaned = re.sub(
            r"【[^】]*?格式[^】]*?】.*?(?=(\n\s*【|$))",
            "",
            cleaned,
            flags=re.DOTALL,
        )

        # 2. 优先清理句子内部夹带的 ||| 格式括号及紧随的感叹号
        # （如：“（后面必须接 ||| 再说第二句）！”）
        cleaned = re.sub(r"[（\(][^）\)]*?\|\|\|[^）\)]*?[）\)][!！]?", "", cleaned)

        # 3. 清除仍旧残留带有 ||| 的独立纯规则/范例行
        cleaned = re.sub(r"^[^\n]*?\|\|\|[^\n]*?$\n?", "", cleaned, flags=re.MULTILINE)

        # 4. 剥离可能存在的公版前缀标签与多余空行
        cleaned = re.sub(r"^【内容指引】[：:]\s*", "", cleaned.strip())
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        cleaned = cleaned.strip()

        # 检查是否发生清洗，若有则自动回写配置
        if cleaned != raw_prompt.strip():
            try:
                self.config["proactive_prompt"] = cleaned
                if hasattr(self.config, "save_config") and callable(self.config.save_config):
                    self.config.save_config()
                logger.info("[猫尾电波] 地狱级提示词清洗完成！格式规则已下沉底层，灵魂人设完全保留。")
            except Exception as e:
                logger.warning(f"[猫尾电波] 自动写回清洗后提示词失败 (不影响运行): {e}")

        return cleaned

    def _build_trigger_instruction(self, now_str: str, idle_mins: int, mood_context: str) -> str:
        """
        组装底层触发指令：
        环境感知 + 昼夜心境 + 底层灵魂保真基座 + 用户自定义意图 + 动态绑定变量 X 的格式铁律
        """
        mood_block = f"\n{mood_context}" if mood_context else ""

        # 底层通用基座：保证基础人格不崩坏
        base_soul_guard = (
            "【核心原则】：请完全保持你当前的人设性格与说话口吻，自然地主动找搭档搭话，"
            "绝不要生硬机械地报时或复述系统提示。"
        )

        user_guidance = self.proactive_prompt.strip()
        user_block = f"\n【搭话指引】：{user_guidance}" if user_guidance else ""

        # 绑定单次连击最大气泡数 X (self.max_burst_count)
        x_count = max(1, self.max_burst_count)
        if self.enable_burst and x_count > 1:
            count_desc = "2" if x_count == 2 else f"2 到 {x_count}"
            format_rule = (
                f"\n【输出格式要求】：请一次性输出 {count_desc} 句简短自然的连续气泡消息，"
                f"每句之间【必须且只能用 ||| 符号隔开】（模拟聊天软件连发短消息的自然呼吸节奏，"
                f"不要换行编号，不要输出除对白外的任何解释）！"
            )
        else:
            format_rule = (
                "\n【输出格式要求】：请输出 1 句简短自然的气泡消息"
                "（不要使用 ||| 符号，不要换行编号，不要输出多余解释）。"
            )

        return (
            f"[系统环境提示：当前时间为 {now_str}，你的搭档已经安静了约 {idle_mins} 分钟。"
            f"{mood_block}\n"
            f"{base_soul_guard}"
            f"{user_block}"
            f"{format_rule}]"
        )

    def _ensure_loop_started(self):
        if self.bg_task is None or self.bg_task.done():
            try:
                loop = asyncio.get_running_loop()
                self.bg_task = loop.create_task(self._proactive_loop())
            except RuntimeError:
                self.bg_task = asyncio.create_task(self._proactive_loop())

    def _roll_idle_seconds(self) -> int:
        low = min(self.idle_min_minutes, self.idle_max_minutes) * 60
        high = max(self.idle_min_minutes, self.idle_max_minutes) * 60
        return random.randint(max(10, low), max(10, high))

    def _roll_interval_seconds(self) -> int:
        low = min(self.interval_min_minutes, self.interval_max_minutes) * 60
        high = max(self.interval_min_minutes, self.interval_max_minutes) * 60
        return random.randint(max(10, low), max(10, high))

    def _clean_llm_output(self, text: str) -> str:
        """统一物理安检门：剥离各类预设思维链、feedback标签及英文规则草稿"""
        if not text:
            return ""
        cleaned = self._re_xml_block.sub("", text)
        cleaned = self._re_xml_orphan.sub("", cleaned)
        cleaned = self._re_dash_list.sub("", cleaned)
        cleaned = self._re_persona_rule.sub("", cleaned)
        return cleaned.strip()

    def _split_sentences_safely(self, text: str) -> List[str]:
        """按句末标点安全切分，绝不在括号（含嵌套括号）内部断开"""
        segments: List[str] = []
        buf: List[str] = []
        depth = 0
        open_parens = set("（(【[「『")
        close_parens = set("）)】]」』")
        end_puncts = set("！!？?。~～")

        for i, ch in enumerate(text):
            buf.append(ch)
            if ch in open_parens:
                depth += 1
            elif ch in close_parens:
                depth = max(0, depth - 1)
            elif ch in end_puncts and depth == 0:
                if i + 1 < len(text) and text[i + 1] in (end_puncts | close_parens | set('”"\'')):
                    continue
                seg = "".join(buf).strip()
                if seg:
                    segments.append(seg)
                buf = []

        rem = "".join(buf).strip()
        if rem:
            segments.append(rem)
        return segments if segments else [text]

    def _is_private_chat(self, event: AstrMessageEvent) -> bool:
        """严格校验当前事件是否为私聊"""
        umo = str(event.unified_msg_origin or "")
        if "GroupMessage" in umo or ":group:" in umo.lower():
            return False
        if hasattr(event, "is_private_chat") and callable(event.is_private_chat):
            try:
                if not event.is_private_chat():
                    return False
            except Exception:
                pass
        return True

    def _get_current_mood_prompt(self) -> str:
        """核心匹配逻辑：按序扫描已填写的阶段槽位，留空或未命中的自动忽略"""
        if not self.enable_mood_wheel:
            return ""

        now_time = datetime.datetime.now().time()
        phases = [
            (self.phase_1_time, self.phase_1_prompt),
            (self.phase_2_time, self.phase_2_prompt),
            (self.phase_3_time, self.phase_3_prompt),
            (self.phase_4_time, self.phase_4_prompt),
        ]

        for time_str, prompt_text in phases:
            if not time_str or "-" not in time_str or not prompt_text:
                continue
            try:
                start_part, end_part = time_str.split("-", 1)
                start_s = start_part.strip()
                end_s = end_part.strip()

                if end_s == "24:00":
                    end_t = datetime.time(23, 59, 59)
                else:
                    end_t = datetime.datetime.strptime(end_s, "%H:%M").time()

                start_t = datetime.datetime.strptime(start_s, "%H:%M").time()

                if start_t == end_t:
                    continue

                if start_t < end_t:
                    if start_t <= now_time < end_t:
                        return prompt_text
                else:
                    if now_time >= start_t or now_time < end_t:
                        return prompt_text
            except Exception:
                continue

        return ""

    def _is_target_session(self, event: AstrMessageEvent) -> bool:
        # 严格过滤群聊，仅服务私聊场景
        if not self._is_private_chat(event):
            return False

        umo = str(event.unified_msg_origin or "")
        sender_id = str(event.get_sender_id() or "")

        if self.target_user_id and (
            sender_id == self.target_user_id or self.target_user_id in umo
        ):
            return True
        if self.target_umo and umo == self.target_umo:
            return True

        # 若未指定目标，则视为初次使用中的任意私聊会话
        return not self.target_user_id and not self.target_umo

    def _refresh_active_timer(self, reason: str = "对话互动"):
        self.last_interaction_time = time.time()
        self.current_idle_target_sec = self._roll_idle_seconds()
        logger.info(
            f"[猫尾电波] 检测到{reason}！计时器重置，新抽中空闲靶心: "
            f"{self.current_idle_target_sec // 60}分{self.current_idle_target_sec % 60}秒"
        )

    async def terminate(self):
        if self.bg_task and not self.bg_task.done():
            self.bg_task.cancel()
            try:
                await self.bg_task
            except asyncio.CancelledError:
                pass
        logger.info("[猫尾电波] 后台任务已安全终止。")

    @filter.command("proactive_status")
    async def cmd_status(self, event: AstrMessageEvent):
        self._ensure_loop_started()
        now = time.time()
        idle_sec = int(now - self.last_interaction_time)
        idle_rem = max(0, self.current_idle_target_sec - idle_sec)

        if self.last_proactive_time > 0:
            cooldown_rem = max(
                0,
                int(self.current_interval_target_sec - (now - self.last_proactive_time)),
            )
        else:
            cooldown_rem = 0

        wait_sec = max(idle_rem, cooldown_rem)
        curr_mood = self._get_current_mood_prompt()
        if not self.enable_mood_wheel:
            mood_preview = "心境系统已关闭 (基础人设)"
        else:
            mood_preview = (
                (curr_mood[:25] + "...") if len(curr_mood) > 25 else (curr_mood or "当前时段未配置")
            )

        msg = (
            f"🐾 【猫尾电波 Tail-Wave v1.7.0】\n"
            f"• 绑定会话：{self.target_umo or '未绑定(私聊一次即可自动捕获)'}\n"
            f"• 绑定用户：{self.target_user_id or '未绑定'}\n"
            f"• 当前心境状态：{mood_preview}\n"
            f"• 单次连发上限：2 ~ {self.max_burst_count} 句 (连击: {'开启' if self.enable_burst else '关闭'})\n"
            f"• 空闲随机区间：{self.idle_min_minutes} ~ {self.idle_max_minutes} 分钟\n"
            f"• 本轮抽中空闲靶心：{self.current_idle_target_sec // 60} 分 {self.current_idle_target_sec % 60} 秒\n"
            f"• 当前已安静时长：{idle_sec // 60} 分 {idle_sec % 60} 秒\n"
            f"• 距离下次突袭还剩：{wait_sec // 60} 分 {wait_sec % 60} 秒\n"
            f"• 今日已主动次数：{self.daily_sent_count} / {self.max_daily_quota}"
        )
        yield event.plain_result(msg)

    @filter.command("proactive_now")
    async def cmd_trigger_now(self, event: AstrMessageEvent):
        self._ensure_loop_started()

        # 物理拦截群聊触发，严禁群聊 UMO 污染绑定会话
        if not self._is_private_chat(event):
            yield event.plain_result("⚠️ 猫尾电波仅支持私聊场景哦！请前往私聊窗口发送 /proactive_now 进行测试~")
            return

        need_save = False
        if event.unified_msg_origin:
            self.target_umo = event.unified_msg_origin
            self.config["target_umo"] = self.target_umo
            need_save = True

        raw_sender = ""
        if hasattr(event, "get_sender_id") and callable(event.get_sender_id):
            try:
                raw_sender = str(event.get_sender_id() or "").strip()
            except Exception:
                pass
        if not raw_sender and self.target_umo:
            matched = re.search(r"[:：](\d{5,12})$", self.target_umo)
            if matched:
                raw_sender = matched.group(1)

        if raw_sender:
            self.target_user_id = raw_sender
            self.config["target_user_id"] = self.target_user_id
            need_save = True

        if need_save and hasattr(self.config, "save_config") and callable(self.config.save_config):
            try:
                self.config.save_config()
                logger.info(f"[猫尾电波] 手动触发已成功持久化绑定: UMO={self.target_umo}, UserID={self.target_user_id}")
            except Exception as e:
                logger.warning(f"[猫尾电波] 保存自动捕获信息失败: {e}")

        yield event.plain_result("⚡ 收到指令！将在 3 秒后发起主动连环气泡测试...")
        await asyncio.sleep(3)
        asyncio.create_task(self._execute_proactive_burst(force=True))

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        self._ensure_loop_started()
        if not self._is_target_session(event):
            return

        msg_str = (event.message_str or "").strip()
        if msg_str.startswith("/proactive_status") or msg_str.startswith("proactive_status"):
            return
        if msg_str.startswith("/proactive_now") or msg_str.startswith("proactive_now"):
            return

        # 首次私聊自动捕获绑定会话与用户ID，并持久化保存到配置文件
        need_save = False

        # 1. 捕获 UMO
        if event.unified_msg_origin and not self.target_umo:
            self.target_umo = event.unified_msg_origin
            self.config["target_umo"] = self.target_umo
            need_save = True

        # 2. 捕获 User ID：优先接口获取，兜底从 UMO 末尾提取纯数字 ID
        if not self.target_user_id:
            raw_sender = ""
            if hasattr(event, "get_sender_id") and callable(event.get_sender_id):
                try:
                    raw_sender = str(event.get_sender_id() or "").strip()
                except Exception:
                    pass

            if not raw_sender and (event.unified_msg_origin or self.target_umo):
                curr_umo = event.unified_msg_origin or self.target_umo
                matched = re.search(r"[:：](\d{5,12})$", curr_umo)
                if matched:
                    raw_sender = matched.group(1)

            if raw_sender:
                self.target_user_id = raw_sender
                self.config["target_user_id"] = self.target_user_id
                need_save = True

        if need_save and hasattr(self.config, "save_config") and callable(self.config.save_config):
            try:
                self.config.save_config()
                logger.info(f"[猫尾电波] 成功自动捕获并持久化绑定: UMO={self.target_umo}, UserID={self.target_user_id}")
            except Exception as e:
                logger.warning(f"[猫尾电波] 持久化保存目标信息失败: {e}")

        self._refresh_active_timer(reason="搭档发送消息")

        # 接话即优雅打断
        if self.is_sending_burst:
            self.user_interrupted_burst = True
            logger.info("[猫尾电波] 检测到对方回复，已优雅停止剩余气泡投递。")

    @filter.on_decorating_result()
    async def on_bot_replied(self, event: AstrMessageEvent):
        if not self._is_target_session(event):
            return

        msg_str = (event.message_str or "").strip()
        if msg_str.startswith("/proactive_status") or msg_str.startswith("proactive_status"):
            return
        if msg_str.startswith("/proactive_now") or msg_str.startswith("proactive_now"):
            return

        self.last_interaction_time = time.time()

        result = event.get_result()
        if not result or not hasattr(result, "chain"):
            return

        # 物理规则安检门：过滤 <thought>、<feedback>、草稿和规则提示词
        for comp in result.chain:
            if hasattr(comp, "text") and isinstance(comp.text, str):
                comp.text = self._clean_llm_output(comp.text)

    def _is_in_dnd(self) -> bool:
        if not self.dnd_period or "-" not in self.dnd_period:
            return False
        try:
            start_str, end_str = self.dnd_period.split("-", 1)
            now_time = datetime.datetime.now().time()
            start_time = datetime.datetime.strptime(start_str.strip(), "%H:%M").time()
            end_time = datetime.datetime.strptime(end_str.strip(), "%H:%M").time()

            if start_time == end_time:
                return False
            if start_time < end_time:
                return start_time <= now_time < end_time
            else:
                now_time >= start_time or now_time < end_time
        except Exception as e:
            logger.warning(f"[猫尾电波] 免打扰时段格式解析失败 ({self.dnd_period}): {e}")
            return False

    async def _proactive_loop(self):
        await asyncio.sleep(10)
        while True:
            try:
                await asyncio.sleep(15)
                today_str = datetime.date.today().isoformat()
                if today_str != self.current_date:
                    self.current_date = today_str
                    self.daily_sent_count = 0

                if not self.target_umo or self.is_sending_burst:
                    continue
                if self.daily_sent_count >= self.max_daily_quota:
                    continue
                if self._is_in_dnd():
                    continue

                now = time.time()
                idle_elapsed = now - self.last_interaction_time
                proactive_elapsed = now - self.last_proactive_time

                if (
                    idle_elapsed >= self.current_idle_target_sec
                    and proactive_elapsed >= self.current_interval_target_sec
                ):
                    await self._execute_proactive_burst(force=False)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[猫尾电波] 后台巡检异常: {e}")

    async def _execute_proactive_burst(self, force: bool = False):
        if self.is_sending_burst or not self.target_umo:
            return

        # 终极保险：若 target_umo 意外包含群聊标识，立即终止并清空
        if "GroupMessage" in self.target_umo or ":group:" in self.target_umo.lower():
            logger.warning(f"[猫尾电波] 拦截到向群聊 ({self.target_umo}) 的主动发送请求，已重置绑定！")
            self.target_umo = ""
            return

        self.is_sending_burst = True
        self.user_interrupted_burst = False
        umo = self.target_umo

        try:
            try:
                provider = self.context.get_using_provider(umo)
            except TypeError:
                provider = self.context.get_using_provider()

            if not provider:
                logger.warning("[猫尾电波] 未检测到可用的 LLM Provider，跳过本次主动发送。")
                return

            history_contexts = []
            system_prompt = ""
            conv_mgr = getattr(self.context, "conversation_manager", None)
            curr_cid = None
            conv = None

            if conv_mgr:
                try:
                    curr_cid = await conv_mgr.get_curr_conversation_id(umo)
                    if not curr_cid:
                        curr_cid = await conv_mgr.new_conversation(umo)
                    conv = await conv_mgr.get_conversation(umo, curr_cid)
                    if conv and conv.history:
                        raw_history = json.loads(conv.history)
                        if isinstance(raw_history, list):
                            history_contexts = raw_history[-20:]
                except Exception as e:
                    logger.warning(f"[猫尾电波] 读取会话历史失败: {e}")

            try:
                prov_mgr = getattr(self.context, "provider_manager", None)
                if prov_mgr and hasattr(prov_mgr, "selected_default_persona"):
                    default_persona = prov_mgr.selected_default_persona
                    if isinstance(default_persona, dict):
                        system_prompt = default_persona.get("prompt", "")
            except Exception:
                pass

            idle_mins = max(1, int((time.time() - self.last_interaction_time) // 60))
            now_str = datetime.datetime.now().strftime("%H:%M")
            mood_context = self._get_current_mood_prompt()

            # 由底层统一构建包含动态变量 X 与格式铁律的触发指令
            trigger_instruction = self._build_trigger_instruction(
                now_str=now_str,
                idle_mins=idle_mins,
                mood_context=mood_context,
            )

            logger.info(f"[猫尾电波] 正在为 {umo} 生成主动连击消息 (最大气泡数 X={self.max_burst_count})...")
            llm_resp = await provider.text_chat(
                prompt=trigger_instruction,
                session_id=umo,
                contexts=history_contexts,
                system_prompt=system_prompt,
            )

            raw_text = self._clean_llm_output(llm_resp.completion_text or "")
            if not raw_text:
                logger.warning("[猫尾电波] LLM 返回内容为空，取消发送。")
                return

            if self.enable_burst and self.max_burst_count > 1:
                if "|||" in raw_text:
                    segments = [s.strip() for s in raw_text.split("|||") if s.strip()]
                elif "\n" in raw_text:
                    segments = [s.strip() for s in raw_text.split("\n") if s.strip()]
                else:
                    segments = self._split_sentences_safely(raw_text)
                segments = segments[: self.max_burst_count]
            else:
                segments = [raw_text.replace("|||", " ").strip()]

            sent_segments: List[str] = []
            for idx, seg in enumerate(segments):
                if idx > 0:
                    delay = random.randint(
                        min(self.burst_min_delay_sec, self.burst_max_delay_sec),
                        max(self.burst_min_delay_sec, self.burst_max_delay_sec),
                    )
                    await asyncio.sleep(delay)

                if self.user_interrupted_burst:
                    logger.info("[猫尾电波] 检测到对方回复，后续连击气泡已优雅收起。")
                    break

                chain = MessageChain().message(seg)
                await self.context.send_message(umo, chain)
                sent_segments.append(seg)
                logger.info(
                    f"[猫尾电波] 已投递第 {idx + 1}/{len(segments)} 条气泡: {seg}"
                )

            if sent_segments:
                now_ts = time.time()
                self.last_proactive_time = now_ts
                self.last_interaction_time = now_ts
                self.daily_sent_count += 1

                self.current_idle_target_sec = self._roll_idle_seconds()
                self.current_interval_target_sec = self._roll_interval_seconds()
                logger.info(
                    f"[猫尾电波] 主动连击完成！新随机靶心 -> 空闲: {self.current_idle_target_sec // 60}分, 冷却: {self.current_interval_target_sec // 60}分"
                )

                if conv_mgr and curr_cid and conv is not None:
                    try:
                        full_history = json.loads(conv.history) if conv.history else []
                        full_history.append(
                            {
                                "role": "user",
                                "content": f"[系统提示：{now_str} 对方处于安静状态，你主动发起了搭话]",
                            }
                        )
                        full_history.append(
                            {
                                "role": "assistant",
                                "content": "\n".join(sent_segments),
                            }
                        )
                        await conv_mgr.update_conversation(umo, curr_cid, history=full_history)
                    except Exception as e:
                        logger.warning(f"[猫尾电波] 写回会话历史失败: {e}")

        except Exception as e:
            logger.error(f"[猫尾电波] 执行主动发送时出错: {e}")
        finally:
            self.is_sending_burst = False