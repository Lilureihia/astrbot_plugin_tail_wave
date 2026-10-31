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


@register(
    "astrbot_plugin_tail_wave",
    "白昼夢",
    "支持拟人连环气泡、打断机制与昼夜心境轮盘的主动私聊插件",
    "1.6.0",
)
class TailWavePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}

        # 基础通信与白名单配置（脱敏通用化）
        self.target_umo: str = str(self.config.get("target_umo", "")).strip()
        self.target_user_id: str = str(self.config.get("target_user_id", "")).strip()

        # 空闲与冷却随机区间
        self.idle_min_minutes: int = int(self.config.get("idle_min_minutes", 10))
        self.idle_max_minutes: int = int(self.config.get("idle_max_minutes", 30))
        self.interval_min_minutes: int = int(self.config.get("interval_min_minutes", 20))
        self.interval_max_minutes: int = int(self.config.get("interval_max_minutes", 60))

        self.max_daily_quota: int = int(self.config.get("max_daily_quota", 15))
        self.enable_burst: bool = bool(self.config.get("enable_burst", True))
        self.max_burst_count: int = int(self.config.get("max_burst_count", 3))
        self.burst_min_delay_sec: int = int(self.config.get("burst_min_delay_sec", 3))
        self.burst_max_delay_sec: int = int(self.config.get("burst_max_delay_sec", 8))
        self.dnd_period: str = str(self.config.get("dnd_period", "")).strip()

        # 心境系统直观配置（总开关 + 4大按需槽位）
        self.enable_mood_wheel: bool = bool(self.config.get("enable_mood_wheel", True))
        self.phase_1_time: str = str(self.config.get("phase_1_time", "00:00-06:00")).strip()
        self.phase_1_prompt: str = str(self.config.get("phase_1_prompt", "")).strip()

        self.phase_2_time: str = str(self.config.get("phase_2_time", "06:00-12:00")).strip()
        self.phase_2_prompt: str = str(self.config.get("phase_2_prompt", "")).strip()

        self.phase_3_time: str = str(self.config.get("phase_3_time", "12:00-18:00")).strip()
        self.phase_3_prompt: str = str(self.config.get("phase_3_prompt", "")).strip()

        self.phase_4_time: str = str(self.config.get("phase_4_time", "18:00-24:00")).strip()
        self.phase_4_prompt: str = str(self.config.get("phase_4_prompt", "")).strip()

        # 主提示词
        self.proactive_prompt: str = str(
            self.config.get(
                "proactive_prompt",
                "现在你的搭档已经有一会儿没理你了。请以你当前的人设口吻，主动找搭档搭话或吐槽。"
                "请一次性输出 2 到 3 句简短自然的连续气泡消息，每句之间必须用 ||| 符号分隔。",
            )
        )

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
            f"[猫尾电波 Tail-Wave v1.6.0] 插件已启动！当前目标UMO: {self.target_umo or '未绑定(等待私聊自动捕获)'}"
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
        umo = str(event.unified_msg_origin or "")
        sender_id = str(event.get_sender_id() or "")

        # 过滤群聊，仅服务私聊场景
        if "GroupMessage" in umo:
            return False

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
            f"🐾 【猫尾电波 Tail-Wave v1.6.0】\n"
            f"• 绑定会话：{self.target_umo or '未绑定(私聊一次即可自动捕获)'}\n"
            f"• 当前心境状态：{mood_preview}\n"
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
        if event.unified_msg_origin:
            self.target_umo = event.unified_msg_origin
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

        # 首次私聊自动捕获绑定会话
        if event.unified_msg_origin and not self.target_umo:
            self.target_umo = event.unified_msg_origin
            logger.info(f"[猫尾电波] 成功自动捕获目标私聊会话: {self.target_umo}")

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

        # 物理规则安检门：过滤草稿和规则提示词
        pattern_dash_list = re.compile(r"^(?:\s*-\s*[A-Za-z].*?\n)+", re.MULTILINE)
        pattern_persona = re.compile(
            r"^(?:Persona constraints|System rule|Note):.*?\n",
            re.IGNORECASE | re.MULTILINE,
        )
        pattern_thought = re.compile(r"<thought>.*?</thought>", re.DOTALL)

        for comp in result.chain:
            if hasattr(comp, "text") and isinstance(comp.text, str):
                cleaned = comp.text
                cleaned = pattern_thought.sub("", cleaned)
                cleaned = pattern_dash_list.sub("", cleaned)
                cleaned = pattern_persona.sub("", cleaned)
                comp.text = cleaned.strip()

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
            mood_block = f"\n{mood_context}" if mood_context else ""

            trigger_instruction = (
                f"[系统环境提示：当前时间为 {now_str}，搭档已经空闲了约 {idle_mins} 分钟。"
                f"{mood_block}\n"
                f"{self.proactive_prompt}]"
            )

            logger.info(f"[猫尾电波] 正在为 {umo} 生成主动连击消息...")
            llm_resp = await provider.text_chat(
                prompt=trigger_instruction,
                session_id=umo,
                contexts=history_contexts,
                system_prompt=system_prompt,
            )

            raw_text = (llm_resp.completion_text or "").strip()
            if not raw_text:
                logger.warning("[猫尾电波] LLM 返回内容为空，取消发送。")
                return

            pattern_thought = re.compile(r"<thought>.*?</thought>", re.DOTALL)
            pattern_dash = re.compile(r"^(?:\s*-\s*[A-Za-z].*?\n)+", re.MULTILINE)
            raw_text = pattern_thought.sub("", raw_text)
            raw_text = pattern_dash.sub("", raw_text).strip()

            if self.enable_burst:
                if "|||" in raw_text:
                    segments = [s.strip() for s in raw_text.split("|||") if s.strip()]
                elif "\n" in raw_text:
                    segments = [s.strip() for s in raw_text.split("\n") if s.strip()]
                else:
                    split_parts = re.split(r"(?<=[！!？?。~])\s*", raw_text)
                    segments = [p.strip() for p in split_parts if p.strip()]
                    if not segments:
                        segments = [raw_text]
                segments = segments[: max(1, self.max_burst_count)]
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

                if self.user_interrupted_burst and not force:
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