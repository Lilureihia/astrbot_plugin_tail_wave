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

DEFAULT_USER_PROMPT = ""

@register(
    "astrbot_plugin_tail_wave",
    "白昼夢",
    "支持拟人连环气泡、打断机制与昼夜心境轮盘的主动私聊插件",
    "2.0.4",
)
class TailWavePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config if config is not None else {}

        self.target_umo: str = str(self.config.get("target_umo", "")).strip()
        self.target_user_id: str = str(self.config.get("target_user_id", "")).strip()

        if "GroupMessage" in self.target_umo or ":group:" in self.target_umo.lower():
            logger.warning(f"[猫尾电波] 检测到初始 target_umo 为群聊 ({self.target_umo})，已自动清空重置。")
            self.target_umo = ""

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

        self.enable_mood_wheel: bool = bool(self.config.get("enable_mood_wheel", False))
        self.phase_1_time: str = str(self.config.get("phase_1_time", "00:00-06:00")).strip()
        self.phase_1_prompt: str = str(self.config.get("phase_1_prompt", "")).strip()
        self.phase_2_time: str = str(self.config.get("phase_2_time", "06:00-12:00")).strip()
        self.phase_2_prompt: str = str(self.config.get("phase_2_prompt", "")).strip()
        self.phase_3_time: str = str(self.config.get("phase_3_time", "12:00-18:00")).strip()
        self.phase_3_prompt: str = str(self.config.get("phase_3_prompt", "")).strip()
        self.phase_4_time: str = str(self.config.get("phase_4_time", "18:00-24:00")).strip()
        self.phase_4_prompt: str = str(self.config.get("phase_4_prompt", "")).strip()

        # 基础 XML/元标签剥离正则
        self._re_xml_block = re.compile(
            r"<(thought|think|thinking|feedback|analysis|reasoning|inner_thought|reflection|scratchpad|meta|rule|system)\b[^>]*>[\s\S]*?</\1>",
            re.IGNORECASE,
        )
        self._re_xml_orphan = re.compile(
            r"</?(?:thought|think|thinking|feedback|analysis|reasoning|inner_thought|reflection|scratchpad|meta|rule|system)\b[^>]*>",
            re.IGNORECASE,
        )

        raw_prompt = str(self.config.get("proactive_prompt", DEFAULT_USER_PROMPT))
        self.proactive_prompt: str = self._migrate_proactive_prompt(raw_prompt)

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
            f"[猫尾电波 Tail-Wave v2.0.4] 插件已启动！当前目标UMO: {self.target_umo or '未绑定(等待私聊自动捕获)'}, UserID: {self.target_user_id or '未绑定'}"
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
        if high < low: high = low
        return random.randint(max(10, low), max(10, high))

    def _roll_interval_seconds(self) -> int:
        low = min(self.interval_min_minutes, self.interval_max_minutes) * 60
        high = max(self.interval_min_minutes, self.interval_max_minutes) * 60
        if high < low: high = low
        return random.randint(max(10, low), max(10, high))

    def _clean_llm_output(self, text: str) -> str:
        """
        深度安检门 2.0.4 (破甲感知版)：
        1. 剥离 XML 思考标签与元标记
        2. 剥离行末字符统计自检括号
        3. 裁切无排版的“裸奔”思维链（如：思考过程: ...）
        4. 剥离英文元指令行
        5. 从混杂草稿中精准提取末尾的最终方案引语
        """
        if not text:
            return ""

        # 1. 基础 XML 思考标签剥离
        cleaned = self._re_xml_block.sub("", text)
        cleaned = self._re_xml_orphan.sub("", cleaned)
        cleaned = cleaned.strip()
        if not cleaned:
            return ""

        # 2. 剥离行末字符自检括号
        cleaned = re.sub(
            r"[\(（]\s*(?:约|共|不超过)?\s*(?:[1-2]?\d|30)\s*(?:chars?|words?|个?字)\s*(?:以内|以下)?\s*[\)）]\s*$",
            "",
            cleaned,
            flags=re.IGNORECASE | re.MULTILINE,
        )

        # 3. 结构化“分步思考草稿”切除（新增支持无粗体、裸奔特征捕获）
        has_cot_structure = bool(
            re.search(
                r"(\*\*(?:构思内容|检查约束|思考过程|任务目标)\*\*|^\s*[-*•]?\s*(?:当前时间|系统提示|字数限制|禁止输出|角色设定|思考过程|分析过程|Thinking Process)[：:])",
                cleaned,
                re.MULTILINE | re.IGNORECASE,
            )
        )

        if has_cot_structure:
            split_patterns = [
                r"\*\*(?:最终输出|输出|回复|生成|final\s*response|final\s*polish)\*\*[:：]?",
                r"(?:最终输出|生成内容|回复内容|最终气泡|最终选择|精选方案并优化)[:：]",
                r"\*\*(?:检查约束|约束校验|self-check)\*\*.*?[。！？\.\!\?]\s*",
            ]
            found = False
            for pat in split_patterns:
                parts = re.split(pat, cleaned, flags=re.IGNORECASE)
                if len(parts) > 1 and parts[-1].strip():
                    cleaned = parts[-1].strip()
                    found = True
                    break

            # 如果没找到标准锚点，倒序搜寻第一个脱离思维链行首特征的干净句子
            if not found:
                lines = [l.strip() for l in cleaned.split("\n") if l.strip()]
                candidate = ""
                for line in reversed(lines):
                    if re.match(
                        r"^[-*•\d\.]+\s*(\*\*)?(当前时间|系统提示|字数限制|禁止输出|角色设定|构思内容|检查约束|Keep\b|思考过程|分析过程)",
                        line,
                        re.IGNORECASE,
                    ):
                        sub_text = re.sub(r"^.*?贴合人设[。！？\.\!\?]?\s*", "", line)
                        sub_text = re.sub(r"^.*?[\:：]\s*", "", sub_text)
                        if sub_text and not any(k in sub_text for k in ["字数", "约束", "设定"]):
                            candidate = sub_text
                            break
                        continue
                    candidate = line
                    break
                if candidate:
                    cleaned = candidate

        # 4. 剥离英文元指令行
        cleaned = re.sub(
            r"^\s*[-*•]?\s*(?:Keep\b.*?within|Persona constraints\b|System rule\b).*?(?:\n|$)",
            "",
            cleaned,
            flags=re.IGNORECASE | re.MULTILINE,
        )

        # 4.5. 提取混在草稿末尾的引语方案 (如：或者：“快醒醒！”(超字数))
        cleaned = re.sub(r"^或者：?[\"“]?(.*?)[\"”]?\s*(?:[\(（].*?超了一点.*?[\)）])?$", r"\1", cleaned, flags=re.MULTILINE)

        # 5. 精准剥除“列表草稿前缀”
        lines = cleaned.split("\n")
        safe_lines = []
        for line in lines:
            line_str = line.strip()
            if re.match(r"^\*[^\*\s]+.*?\*", line_str):
                safe_lines.append(line_str)
            else:
                line_str = re.sub(r"^[-*•]\s+[\"“]?", "", line_str)
                line_str = re.sub(r"[\"”]$", "", line_str)
                if line_str:
                    safe_lines.append(line_str)
        cleaned = "\n".join(safe_lines).strip()

        # 6. 收尾清理
        cleaned = re.sub(r"^[#*_\s]*(?:最终输出|精选方案)[#*_\s]*[：:]?\s*", "", cleaned)
        cleaned = re.sub(r"^[#*_\s]*final polish[#*_\s]*[：:]?\s*", "", cleaned, flags=re.IGNORECASE)
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
        if self.target_user_id and (sender_id == self.target_user_id or self.target_user_id in umo):
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
        cooldown_rem = max(0, int(self.current_interval_target_sec - (now - self.last_proactive_time))) if self.last_proactive_time > 0 else 0
        wait_sec = max(idle_rem, cooldown_rem)
        curr_mood = self._get_current_mood_prompt()
        mood_preview = "心境系统已关闭 (基础人设)" if not self.enable_mood_wheel else ((curr_mood[:25] + "...") if len(curr_mood) > 25 else (curr_mood or "当前时段未配置"))
        max_x = max(1, self.max_burst_count)
        min_req = max(2, int(max_x * 0.6)) if max_x >= 3 else min(max_x, 2)

        msg = (
            f"🐾 【猫尾电波 Tail-Wave v2.0.4】\n"
            f"• 绑定会话：{self.target_umo or '未绑定(私聊一次即可自动捕获)'}\n"
            f"• 绑定用户：{self.target_user_id or '未绑定'}\n"
            f"• 当前心境状态：{mood_preview}\n"
            f"• 发挥空间 (X): {max_x} 句 | 最小保底期望: {min_req} 句 (连击: {'开启' if self.enable_burst else '关闭'})\n"
            f"• 空闲随机区间：{self.idle_min_minutes} ~ {self.idle_max_minutes} 分钟\n"
            f"• 本轮抽中靶心：{self.current_idle_target_sec // 60} 分 {self.current_idle_target_sec % 60} 秒\n"
            f"• 当前已安静时长：{idle_sec // 60} 分 {idle_sec % 60} 秒\n"
            f"• 今日已主动次数：{self.daily_sent_count} / {self.max_daily_quota}"
        )
        yield event.plain_result(msg)

    @filter.command("proactive_now")
    async def cmd_trigger_now(self, event: AstrMessageEvent):
        self._ensure_loop_started()
        if not self._is_private_chat(event):
            yield event.plain_result("⚠️ 猫尾电波仅支持私聊场景哦！")
            return
        if event.unified_msg_origin:
            self.target_umo = event.unified_msg_origin
            self.config["target_umo"] = self.target_umo
        raw_sender = event.get_sender_id() if hasattr(event, "get_sender_id") else ""
        if raw_sender:
            self.target_user_id = str(raw_sender)
            self.config["target_user_id"] = self.target_user_id
        if hasattr(self.config, "save_config") and callable(self.config.save_config):
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
            raw_sender = event.get_sender_id() if hasattr(event, "get_sender_id") else ""
            if raw_sender:
                self.target_user_id = str(raw_sender)
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
            logger.info("[猫尾电波] ⚠️ 检测到对方回复，已即时拉高中断标志，强制中止剩余气泡投递！")

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
                        for item in raw_history[-20:]:
                            if isinstance(item, dict) and "role" in item and "content" in item:
                                history_contexts.append(item)
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
            min_required_turns = 1 if max_allowed_turns == 1 else max(2, int(max_allowed_turns * 0.6))

        stop_token = "[TAIL_WAVE_STOP]"

        try:
            for turn in range(max_allowed_turns):
                if self.user_interrupted_burst:
                    logger.info("[猫尾电波] 🛑 连击循环中实时捕获到中断信号，立刻强制中止后续气泡生成！")
                    break

                idle_mins = max(1, int((time.time() - self.last_interaction_time) // 60))
                now_str = datetime.datetime.now().strftime("%H:%M")
                mood_context = self._get_current_mood_prompt()
                mood_block = f"\n{mood_context}" if mood_context else ""

                base_soul_guard = (
                    "【核心原则】：保持你当前的人设性格。自然、松弛、简短（单句20字以内），禁止生硬报时或长篇心理动作。"
                )
                user_guidance = self.proactive_prompt.strip()
                user_block = f"\n【补充指引】：{user_guidance}" if user_guidance else ""

                is_forced_continue = len(sent_segments) < min_required_turns

                if turn == 0:
                    turn_instruction = (
                        f"[当前场景：时间走到了 {now_str}，搭档已安静了约 {idle_mins} 分钟。]\n"
                        f"{mood_block}\n{base_soul_guard}{user_block}\n"
                        f"【你的心理活动】：你现在想自然地找搭档搭句话。请结合你们上方的上下文与当下的情境，"
                        f"自主决定是顺承未尽的话题还是开启新话题，以符合你人设的方式随性发一句短消息（20字以内）。"
                    )
                elif is_forced_continue:
                    recent_spoken = " ".join(sent_segments)
                    turn_instruction = (
                        f"【系统巡检推进（搭档目前依然安静，并未接话）】\n"
                        f"这是你（你自己）刚才连续主动发出的气泡：“{recent_spoken}”。\n"
                        f"顺着你刚才的话头，以符合你人设的方式再自然补充一小句短消息（20字以内）。\n"
                        f"（严正注意：搭档没有说话，严禁将上述内容误判为搭档的发言或复读！）"
                    )
                else:
                    recent_spoken = " ".join(sent_segments)
                    turn_instruction = (
                        f"【系统巡检推进（搭档目前依然安静，并未接话）】\n"
                        f"这是你（你自己）刚才连续主动发出的气泡：“{recent_spoken}”。\n"
                        f"已满足保底。觉得想说的话已经说完了就打住，严格只回复暗号：{stop_token}；如果还想补充就以你的性格直接输出短句。\n"
                        f"（严正注意：搭档没有说话，严禁将上述内容误判为搭档的发言或复读！）"
                    )

                logger.info(f"[猫尾电波] 正在生成第 {turn + 1}/{max_allowed_turns} 轮动态气泡...")

                if self.user_interrupted_burst:
                    logger.info("[猫尾电波] 🛑 大模型调用前夕检测到对方输入，瞬间熔断拦截！")
                    break

                llm_resp = await provider.text_chat(
                    prompt=turn_instruction,
                    session_id=umo,
                    contexts=history_contexts,
                    system_prompt=system_prompt,
                )

                if self.user_interrupted_burst:
                    logger.info("[猫尾电波] 🛑 大模型返回瞬间检测到对方输入，丢弃本次生成内容！")
                    break

                raw_text = self._clean_llm_output(llm_resp.completion_text or "")

                if not is_forced_continue and (not raw_text or stop_token in raw_text or len(raw_text.strip()) == 0):
                    if stop_token in (raw_text or ""):
                        logger.info(f"[猫尾电波] ✨ 第 {turn + 1} 轮模型自主输出暗号 ({stop_token})，判定话头已完整交接，主动结束连击。")
                    else:
                        logger.info(f"[猫尾电波] 🍃 第 {turn + 1} 轮模型输出为空白内容，主动结束连击。")
                    break

                seg = raw_text.strip()
                if not seg:
                    continue

                if not is_forced_continue and stop_token in seg:
                    logger.info(f"[猫尾电波] ✨ 文本中捕获暗号 ({stop_token})，自然结束连击。")
                    break

                if stop_token in seg:
                    seg = seg.replace(stop_token, "").strip()
                    if not seg:
                        logger.info(f"[猫尾电波] ✨ 剥离暗号后内容为空，自然结束连击。")
                        break

                chain = MessageChain().message(seg)
                await self.context.send_message(umo, chain)
                sent_segments.append(seg)
                logger.info(f"[猫尾电波] 成功投递第 {len(sent_segments)} 条微型气泡: {seg}")

                history_contexts.append({"role": "assistant", "content": seg})

                # ========================================================
                # 【底层修复】：发一句存一句（增量合并并强制触发落盘防失忆）
                # ========================================================
                if conv_mgr:
                    try:
                        curr_cid = await conv_mgr.get_curr_conversation_id(umo)
                        if not curr_cid:
                            curr_cid = await conv_mgr.new_conversation(umo)
                        
                        live_conv = await conv_mgr.get_conversation(umo, curr_cid)
                        if live_conv:
                            raw_h = live_conv.history
                            history_list = []
                            if raw_h:
                                if isinstance(raw_h, str):
                                    try:
                                        history_list = json.loads(raw_h)
                                        if isinstance(history_list, str):
                                            history_list = json.loads(history_list)
                                    except Exception:
                                        history_list = []
                                elif isinstance(raw_h, list):
                                    history_list = list(raw_h)
                            if not isinstance(history_list, list):
                                history_list = []

                            new_bubbles = []
                            if turn == 0:
                                if not history_list or history_list[-1].get("role") != "user":
                                    new_bubbles.append({"role": "user", "content": "……"})
                                    
                            new_bubbles.append({"role": "assistant", "content": seg})
                            history_list.extend(new_bubbles)

                            if len(history_list) > 40:
                                history_list = history_list[-40:]
                                
                            history_str = json.dumps(history_list, ensure_ascii=False)
                            try:
                                await conv_mgr.update_conversation(umo, curr_cid, history=history_str)
                            except TypeError:
                                await conv_mgr.update_conversation(umo, curr_cid, history=history_list)
                            
                            # 【核心钩子】：强制呼叫底层的 save_conversation 持久化落盘，杜绝重启失忆！
                            if hasattr(conv_mgr, "save_conversation") and callable(conv_mgr.save_conversation):
                                await conv_mgr.save_conversation(umo, curr_cid)
                                logger.info("[猫尾电波] 💾 增量记忆已强制触发底层持久化落盘，杜绝重启失忆。")
                            
                            logger.info(f"[猫尾电波] [记忆落库] 成功刻录主动气泡到历史记忆: {seg}")
                    except Exception as e:
                        logger.warning(f"[猫尾电波] [记忆落库] 实时增量合并或强制落盘失败: {e}")
                # ========================================================

                if turn < max_allowed_turns - 1:
                    delay = random.randint(
                        min(self.burst_min_delay_sec, self.burst_max_delay_sec),
                        max(self.burst_min_delay_sec, self.burst_max_delay_sec),
                    )
                    
                    interrupted_during_delay = False
                    for _ in range(int(delay * 10)):
                        if self.user_interrupted_burst:
                            logger.info("[猫尾电波] ⚡ 在气泡停顿等待间隙高频捕获打断信号，即时跳出延迟！")
                            interrupted_during_delay = True
                            break
                        await asyncio.sleep(0.1)
                        
                    if interrupted_during_delay or self.user_interrupted_burst:
                        logger.info("[猫尾电波] 🛑 彻底物理腰斩当前连击大循环，绝不把打断信息带入下一次生成！")
                        break

            if sent_segments:
                now_ts = time.time()
                self.last_proactive_time = now_ts
                self.last_interaction_time = now_ts
                self.daily_sent_count += 1

                self.current_idle_target_sec = self._roll_idle_seconds()
                self.current_interval_target_sec = self._roll_interval_seconds()

                logger.info(
                    f"[猫尾电波] 🏁 本轮主动交互圆满完成！共成功投递 {len(sent_segments)} 条微型气泡。"
                    f"下轮空闲静默靶心已重新抽取为: {self.current_idle_target_sec // 60} 分 {self.current_idle_target_sec % 60} 秒。"
                )

        except Exception as e:
            logger.error(f"[猫尾电波] 执行主动交互时出错: {e}")
        finally:
            self.is_sending_burst = False