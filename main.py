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

# 表面提示词默认留空，真正发力点在模型自身的人格配置与下方的底层算法兜底中
DEFAULT_USER_PROMPT = ""


@register(
    "astrbot_plugin_tail_wave",
    "白昼夢",
    "支持拟人连环气泡、打断机制与昼夜心境轮盘的主动私聊插件",
    "2.0.0",
)
class TailWavePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config if config is not None else {}

        # 基础通信与白名单配置（完全脱敏通用化）
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
            f"[猫尾电波 Tail-Wave v2.0.0] 插件已启动！当前目标UMO: {self.target_umo or '未绑定(等待私聊自动捕获)'}, UserID: {self.target_user_id or '未绑定'}"
        )

    def _migrate_proactive_prompt(self, raw_prompt: str) -> str:
        if not raw_prompt or not raw_prompt.strip():
            return ""

        cleaned = raw_prompt
        cleaned = re.sub(r"【[^】]*?格式[^】]*?】.*?(?=(\n\s*【|$))", "", cleaned, flags=re.DOTALL)
        cleaned = re.sub(r"^【内容指引】[：:]\s*", "", cleaned.strip())
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        cleaned = cleaned.strip()

        if cleaned != raw_prompt.strip():
            try:
                self.config["proactive_prompt"] = cleaned
                if hasattr(self.config, "save_config") and callable(self.config.save_config):
                    self.config.save_config()
            except Exception:
                pass

        return cleaned

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
        """
        终极无误伤智能安检门：
        精准过滤思维链大纲，100% 保护正常对话，杜绝误杀！
        """
        if not text:
            return ""
        
        cleaned = self._re_xml_block.sub("", text)
        cleaned = self._re_xml_orphan.sub("", cleaned)
        cleaned = self._re_dash_list.sub("", cleaned)
        cleaned = self._re_persona_rule.sub("", cleaned)

        cleaned = cleaned.strip()
        if not cleaned:
            return ""

        lower_text = cleaned.lower()
        has_thinking_keyword = any(kw in lower_text for kw in ["思考过程", "reasoning process", "inner thought", "分析任务目标", "构思内容"])
        has_structure_outline = bool(re.search(r"(\n\s*1\.\s*\*\*|\n\s*-\s*\*\*[分析构思任务]*|^\s*1\.\s+)", cleaned))

        if has_thinking_keyword and has_structure_outline:
            split_keywords = [
                "最终输出生成", "最终输出：", "最终输出", 
                "输出：", "输出:", "回复：", "回复:", 
                "生成内容：", "生成：", "**最终输出**"
            ]
            
            found_split = False
            for kw in split_keywords:
                if kw in cleaned:
                    parts = cleaned.split(kw)
                    if len(parts) > 1 and len(parts[-1].strip()) > 0:
                        cleaned = parts[-1].strip()
                        found_split = True
                        break
            
            if not found_split:
                lines = cleaned.split("\n")
                valid_lines = []
                is_thinking_zone = True
                
                for line in lines:
                    line_str = line.strip()
                    if is_thinking_zone:
                        if re.match(r"^(\d+\.|\-|\*)\s*(\*\*)?(分析|构思|字数|约束|角色|任务|目标)", line_str):
                            continue
                        elif any(kw in line_str.lower() for kw in ["思考过程", "reasoning process"]):
                            continue
                        else:
                            is_thinking_zone = False
                            if line_str:
                                valid_lines.append(line_str)
                    else:
                        if line_str:
                            valid_lines.append(line_str)
                
                if valid_lines:
                    cleaned = "\n".join(valid_lines).strip()

        cleaned = re.sub(r"^[#*_\s]*最终输出[#*_\s]*[：:]?\s*", "", cleaned)
        return cleaned.strip()

    def _is_private_chat(self, event: AstrMessageEvent) -> bool:
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
        mood_preview = "心境系统已关闭 (基础人设)" if not self.enable_mood_wheel else ((curr_mood[:25] + "...") if len(curr_mood) > 25 else (curr_mood or "当前时段未配置"))

        max_x = max(1, self.max_burst_count)
        min_req = max(2, int(max_x * 0.6)) if max_x >= 3 else min(max_x, 2)

        target_min = self.current_idle_target_sec // 60
        target_sec = self.current_idle_target_sec % 60
        wait_min = wait_sec // 60
        wait_s = wait_sec % 60

        msg = (
            f"🐾 【猫尾电波 Tail-Wave v2.0.0】\n"
            f"• 绑定会话：{self.target_umo or '未绑定(私聊一次即可自动捕获)'}\n"
            f"• 绑定用户：{self.target_user_id or '未绑定'}\n"
            f"• 当前心境状态：{mood_preview}\n"
            f"• 发挥空间 (X): {max_x} 句 | 最小保底期望: {min_req} 句 (连击: {'开启' if self.enable_burst else '关闭'})\n"
            f"• 空闲随机区间：{self.idle_min_minutes} ~ {self.idle_max_minutes} 分钟\n"
            f"• 本轮抽中靶心：{target_min} 分 {target_sec} 秒\n"
            f"• 当前已安静时长：{idle_sec // 60} 分 {idle_sec % 60} 秒 (预计还需等待 {wait_min} 分 {wait_s} 秒)\n"
            f"• 今日已主动次数：{self.daily_sent_count} / {self.max_daily_quota}"
        )
        yield event.plain_result(msg)

    @filter.command("proactive_now")
    async def cmd_trigger_now(self, event: AstrMessageEvent):
        self._ensure_loop_started()
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
            except Exception:
                pass

        yield event.plain_result("⚡ 收到指令！将在 3 秒后发起自然连击互动测试...")
        await asyncio.sleep(3)
        asyncio.create_task(self._execute_proactive_burst(force=True))

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        self._ensure_loop_started()
        if not self._is_target_session(event):
            return

        msg_str = (event.message_str or "").strip()
        if "proactive_status" in msg_str or "proactive_now" in msg_str:
            return

        need_save = False
        if event.unified_msg_origin and not self.target_umo:
            self.target_umo = event.unified_msg_origin
            self.config["target_umo"] = self.target_umo
            need_save = True

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
            except Exception:
                pass

        self._refresh_active_timer(reason="对话互动")
        if self.is_sending_burst:
            self.user_interrupted_burst = True
            logger.info("[猫尾电波] 检测到对方回复，已优雅停止剩余气泡投递。")

    @filter.on_decorating_result()
    async def on_bot_replied(self, event: AstrMessageEvent):
        if not self._is_target_session(event):
            return
        msg_str = (event.message_str or "").strip()
        if "proactive_status" in msg_str or "proactive_now" in msg_str:
            return
        self.last_interaction_time = time.time()
        result = event.get_result()
        if not result or not hasattr(result, "chain"):
            return
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
                return now_time >= start_time or now_time < end_time
        except Exception:
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

        if "GroupMessage" in self.target_umo or ":group:" in self.target_umo.lower():
            logger.warning(f"[猫尾电波] 拦截到向群聊 ({self.target_umo}) 的主动发送请求，已重置绑定！")
            self.target_umo = ""
            return

        self.is_sending_burst = True
        self.user_interrupted_burst = False
        umo = self.target_umo

        sent_segments: List[str] = []

        try:
            provider = self.context.get_using_provider(umo)
        except TypeError:
            provider = self.context.get_using_provider()

        if not provider:
            logger.warning("[猫尾电波] 未检测到可用的 LLM Provider，跳过本次主动发送。")
            self.is_sending_burst = False
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

        if not self.enable_burst:
            max_allowed_turns = 1
            min_required_turns = 1
        else:
            max_allowed_turns = max(1, self.max_burst_count)
            if max_allowed_turns == 1:
                min_required_turns = 1
            else:
                min_required_turns = max(2, int(max_allowed_turns * 0.6))
                min_required_turns = min(min_required_turns, max_allowed_turns)

        stop_token = "[TAIL_WAVE_STOP]"

        try:
            for turn in range(max_allowed_turns):
                if self.user_interrupted_burst:
                    logger.info("[猫尾电波] 检测到对方回复，后续动态气泡已优雅取消。")
                    break

                idle_mins = max(1, int((time.time() - self.last_interaction_time) // 60))
                now_str = datetime.datetime.now().strftime("%H:%M")
                mood_context = self._get_current_mood_prompt()
                mood_block = f"\n{mood_context}" if mood_context else ""

                base_soul_guard = (
                    "【核心原则】：请完全保持你当前的人设性格与说话口吻，自然地主动搭话，"
                    "绝不要生硬机械地报时或复述系统提示。"
                )
                user_guidance = self.proactive_prompt.strip()
                user_block = f"\n【补充指引】：{user_guidance}" if user_guidance else ""

                is_forced_continue = len(sent_segments) < min_required_turns

                if idle_mins < 30:
                    vibe_balance_hint = (
                        "【对话语境提示】：你们不久前刚聊过。你可以顺着刚才的语境或话题自然地延伸、轻声搭话，"
                        "然后再聊点你自己的小日常，语气要松弛自然。"
                    )
                elif idle_mins < 180:
                    vibe_balance_hint = (
                        "【对话语境提示】：你们已经安静了一会儿。你可以凭直觉自由发挥："
                        "既可以顺着上一句的余温接话，也可以毫无违和感地直接分享一个你刚才想到的新鲜事或小吐槽。"
                    )
                else:
                    vibe_balance_hint = (
                        "【对话语境提示】：你们已经很久没聊天了。旧话题已淡去，请随性地分享一个你刚才在干嘛、或者突发奇想的日常碎碎念，"
                        "展现属于你的独立小情绪，切忌死板生硬地套用旧话题。"
                    )

                if turn == 0:
                    turn_instruction = (
                        f"[系统环境提示：当前时间为 {now_str}，你的搭档已经安静了约 {idle_mins} 分钟。"
                        f"{mood_block}\n"
                        f"{base_soul_guard}"
                        f"{user_block}\n"
                        f"{vibe_balance_hint}\n"
                        f"【微型气泡开场】：请输出第 1 条极其短小的单句开场白（字数 20 字以内，绝对不要把多句话揉成一大坨，不要换行编号）。]"
                    )
                elif is_forced_continue:
                    recent_spoken = " ".join(sent_segments)
                    turn_instruction = (
                        f"[系统环境提示：当前时间为 {now_str}。已发送 {len(sent_segments)}/{max_allowed_turns} 句（连击保底要求至少 {min_required_turns} 句）。]\n"
                        f"[连续短打中]：你刚才已经连续发了：“{recent_spoken}”。\n"
                        f"**注意：当前连击尚未达标，请不要收手！**\n"
                        f"请顺着刚才的话头，再输出一句极其简短的**自然补充或顺势闲聊**（字数 20 字以内，绝对禁止输出停止暗号）。"
                    )
                else:
                    recent_spoken = " ".join(sent_segments)
                    turn_instruction = (
                        f"[系统环境提示：当前时间为 {now_str}。已发送 {len(sent_segments)}/{max_allowed_turns} 句。]\n"
                        f"[随性发挥中]：你刚才已经连续发了：“{recent_spoken}”。\n"
                        f"现在既然已经满足了连击保底，请凭直觉判断：\n"
                        f"•如果你觉得还想再补一句短消息（且未超过上限 {max_allowed_turns}），请直接输出新的一句话；\n"
                        f"•如果你觉得【已经说够了、见好就收】，请【严格只输出专属控制暗号：{stop_token}】（绝不允许输出其他任何多余解释）。"
                    )

                logger.info(f"[猫尾电波] 正在让模型进行第 {turn + 1}/{max_allowed_turns} 轮动态气泡生成 (安静时长: {idle_mins}分钟, 保底需求: {min_required_turns})...")
                
                llm_resp = await provider.text_chat(
                    prompt=turn_instruction,
                    session_id=umo,
                    contexts=history_contexts,
                    system_prompt=system_prompt,
                )

                raw_text = self._clean_llm_output(llm_resp.completion_text or "")
                
                if not is_forced_continue and (not raw_text or stop_token in raw_text or len(raw_text.strip()) == 0):
                    logger.info(f"[猫尾电波] 模型自主选择收手（触发专属暗号 {stop_token}），完美结束本轮连击。")
                    break

                seg = raw_text.strip()
                if not seg:
                    continue

                if not is_forced_continue and stop_token in seg:
                    logger.info(f"[猫尾电波] 捕捉到模型试图输出暗号 {stop_token}，已成功在内存中拦截并静默收手！")
                    break

                if stop_token in seg:
                    seg = seg.replace(stop_token, "").strip()
                    if not seg:
                        logger.info(f"[猫尾电波] 保底轮次中仅输出暗号 {stop_token}，已安全静默收手。")
                        break

                chain = MessageChain().message(seg)
                await self.context.send_message(umo, chain)
                sent_segments.append(seg)
                logger.info(f"[猫尾电波] 模型自主投递第 {len(sent_segments)} 条微型气泡: {seg}")

                history_contexts.append({"role": "assistant", "content": seg})

                if turn < max_allowed_turns - 1:
                    delay = random.randint(
                        min(self.burst_min_delay_sec, self.burst_max_delay_sec),
                        max(self.burst_min_delay_sec, self.burst_max_delay_sec),
                    )
                    for _ in range(delay * 2):
                        if self.user_interrupted_burst:
                            break
                        await asyncio.sleep(0.5)

            if sent_segments:
                now_ts = time.time()
                self.last_proactive_time = now_ts
                self.last_interaction_time = now_ts
                self.daily_sent_count += 1

                self.current_idle_target_sec = self._roll_idle_seconds()
                self.current_interval_target_sec = self._roll_interval_seconds()
                logger.info(
                    f"[猫尾电波] 动态气泡连击完成！本次共发送 {len(sent_segments)} 句（上限 X={max_allowed_turns}，保底期望={min_required_turns}）。"
                )

                if conv_mgr and curr_cid and conv is not None:
                    try:
                        full_history = json.loads(conv.history) if conv.history else []
                        full_history.append(
                            {
                                "role": "user",
                                "content": f"[系统提示：对方处于安静时间差 {idle_mins} 分钟，你自主发起了动态搭话]",
                            }
                        )
                        full_history.append(
                            {
                                "role": "assistant",
                                "content": "\n".join(sent_segments),
                            }
                        )
                        history_str = json.dumps(full_history, ensure_ascii=False)
                        try:
                            await conv_mgr.update_conversation(umo, curr_cid, history=history_str)
                        except TypeError:
                            await conv_mgr.update_conversation(umo, curr_cid, history=full_history)
                    except Exception as e:
                        logger.warning(f"[猫尾电波] 写回会话历史失败: {e}")

        except Exception as e:
            logger.error(f"[猫尾电波] 执行主动交互时出错: {e}")
        finally:
            self.is_sending_burst = False