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
PROACTIVE_RECORD_PREFIX = "（对话框聊天记录：刚才我安静未打字时，你在对话框里主动向我发出了以下"


@register(
    "astrbot_plugin_tail_wave",
    "白昼夢",
    "支持拟人连环气泡、打断机制与昼夜心境轮盘的主动私聊插件",
    "2.1.0",
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

        # 小本本：持久化保存当前会话发过的主动消息（仅在 /reset 或表面历史清空时才跟着清空，绝不在收到普通回复时误删）
        self.plugin_cache = self.config.get("_tail_wave_cache", {})
        if not isinstance(self.plugin_cache, dict):
            self.plugin_cache = {}

        # 状态锁：记录当前会话是否处于“主动模式待收尾（等待搭档第一句回应/打断）”状态
        self.proactive_pending = self.config.get("_tail_wave_pending", {})
        if not isinstance(self.proactive_pending, dict):
            self.proactive_pending = {}

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
            f"[猫尾电波 Tail-Wave v2.1.0] 插件已启动！当前目标UMO: {self.target_umo or '未绑定(等待私聊自动捕获)'}"
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

    def _save_cache(self):
        if hasattr(self.config, "save_config") and callable(self.config.save_config):
            try:
                self.config["_tail_wave_cache"] = self.plugin_cache
                self.config["_tail_wave_pending"] = self.proactive_pending
                self.config.save_config()
            except Exception:
                pass

    def _get_cached_bubble_list(self, umo: str) -> List[str]:
        """获取小本本中记录的主动气泡列表"""
        raw = self.plugin_cache.get(umo, [])
        if isinstance(raw, list):
            return [str(x).strip() for x in raw if str(x).strip()]
        if isinstance(raw, str) and raw.strip():
            return [line.strip() for line in raw.split("\n") if line.strip()]
        return []

    def _set_cached_bubble_list(self, umo: str, bubbles: List[str]):
        """更新小本本中的主动气泡列表"""
        cleaned = [str(x).strip() for x in bubbles if str(x).strip()]
        if cleaned:
            self.plugin_cache[umo] = cleaned[-20:]
        else:
            self.plugin_cache.pop(umo, None)
        self._save_cache()

    def _build_proactive_user_marker(self, index: int = 1) -> str:
        return f"{PROACTIVE_RECORD_PREFIX}第 {index} 条消息）"

    def _is_proactive_marker(self, content: str) -> bool:
        if not content:
            return False
        return content.startswith(PROACTIVE_RECORD_PREFIX) or content.startswith("[系统场景：搭档当前安静未发言")

    def _parse_raw_history(self, raw_h) -> List[dict]:
        """安全解析 AstrBot 的 conv.history，自动修复可能被双重 JSON 编码的历史数据"""
        if not raw_h:
            return []
        data = raw_h
        for _ in range(3):
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except Exception:
                    return []
            else:
                break
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict) and "role" in item and "content" in item]
        return []

    async def _write_history_to_conv(self, conv_mgr, umo: str, curr_cid: str, history_list: List[dict]):
        """安全写入 conv_mgr，自动探测底层需要 list 还是 JSON str，杜绝双重编码导致失明"""
        if len(history_list) > 40:
            history_list = history_list[-40:]

        history_str = json.dumps(history_list, ensure_ascii=False)
        try:
            await conv_mgr.update_conversation(umo, curr_cid, history=history_list)
            check_conv = await conv_mgr.get_conversation(umo, curr_cid)
            if check_conv and isinstance(check_conv.history, list):
                await conv_mgr.update_conversation(umo, curr_cid, history=history_str)
        except TypeError:
            await conv_mgr.update_conversation(umo, curr_cid, history=history_str)
        except Exception:
            try:
                await conv_mgr.update_conversation(umo, curr_cid, history=history_str)
            except Exception as e:
                logger.error(f"[猫尾电波] 写入会话历史异常: {e}")

        if hasattr(conv_mgr, "save_conversation") and callable(conv_mgr.save_conversation):
            try:
                await conv_mgr.save_conversation(umo, curr_cid)
            except Exception:
                pass

    async def _get_surface_history_list(self, umo: str) -> List[dict]:
        conv_mgr = getattr(self.context, "conversation_manager", None)
        if not conv_mgr:
            return []
        try:
            curr_cid = await conv_mgr.get_curr_conversation_id(umo)
            if not curr_cid:
                return []
            conv = await conv_mgr.get_conversation(umo, curr_cid)
            if not conv or not conv.history:
                return []
            return self._parse_raw_history(conv.history)
        except Exception:
            return []

    def _extract_proactive_bubbles_from_history(self, history_list: List[dict]) -> List[str]:
        """从表面上下文历史中提取所有已归档的主动消息气泡"""
        extracted: List[str] = []
        for i in range(len(history_list) - 1):
            curr_item = history_list[i]
            next_item = history_list[i + 1]
            if (
                curr_item.get("role") == "user"
                and self._is_proactive_marker(str(curr_item.get("content", "")))
                and next_item.get("role") == "assistant"
            ):
                text = str(next_item.get("content", "")).strip()
                for line in text.split("\n"):
                    line_s = line.strip()
                    if line_s and line_s not in extracted:
                        extracted.append(line_s)
        return extracted

    async def _append_proactive_bubble_to_surface(self, umo: str, seg: str, turn_index: int = 1):
        """
        将单条主动气泡实时同步至表面历史：
        每条主动气泡以独立的 `user(对话框聊天记录说明) -> assistant(气泡正文)` 结对写入，
        不合并、不使用“[系统场景]”字样，让大模型 100% 识别为对话框内真实发出的聊天消息。
        """
        if not seg:
            return
        conv_mgr = getattr(self.context, "conversation_manager", None)
        if not conv_mgr:
            return
        try:
            curr_cid = await conv_mgr.get_curr_conversation_id(umo)
            if not curr_cid:
                curr_cid = await conv_mgr.new_conversation(umo)
            live_conv = await conv_mgr.get_conversation(umo, curr_cid)
            history_list = self._parse_raw_history(live_conv.history if live_conv else None)

            # 避免重复插入完全相同的末尾气泡
            if (
                len(history_list) >= 2
                and history_list[-1].get("role") == "assistant"
                and str(history_list[-1].get("content", "")).strip() == seg
            ):
                return

            marker = self._build_proactive_user_marker(turn_index)
            history_list.append({"role": "user", "content": marker})
            history_list.append({"role": "assistant", "content": seg})

            await self._write_history_to_conv(conv_mgr, umo, curr_cid, history_list)
            logger.info(
                f"[猫尾电波] 💾 主动气泡已实时同步至表面上下文 (当前表面记录数: {len(history_list)}): {seg}"
            )
        except Exception as e:
            logger.error(f"[猫尾电波] 实时同步主动气泡失败: {e}")

    async def _sync_surface_history(self, umo: str, user_text: str, bot_text: str = ""):
        """在打断互动闭环完成时，确保此前的主动气泡均在表面历史中，再归档用户的回复与猫猫的接话"""
        conv_mgr = getattr(self.context, "conversation_manager", None)
        if not conv_mgr:
            return
        try:
            curr_cid = await conv_mgr.get_curr_conversation_id(umo)
            if not curr_cid:
                curr_cid = await conv_mgr.new_conversation(umo)
            live_conv = await conv_mgr.get_conversation(umo, curr_cid)
            history_list = self._parse_raw_history(live_conv.history if live_conv else None)

            # 双向核验：确保小本本里的主动气泡 100% 存在于表面历史中（哪边少就补齐哪边）
            cached_bubbles = self._get_cached_bubble_list(umo)
            surface_Assistant_texts = [
                str(item.get("content", "")).strip()
                for item in history_list
                if item.get("role") == "assistant"
            ]
            missing_idx = 1
            for b_text in cached_bubbles:
                if not any(b_text in s_text for s_text in surface_Assistant_texts):
                    history_list.append({"role": "user", "content": self._build_proactive_user_marker(missing_idx)})
                    history_list.append({"role": "assistant", "content": b_text})
                missing_idx += 1

            if user_text:
                history_list.append({"role": "user", "content": user_text})
            if bot_text:
                history_list.append({"role": "assistant", "content": bot_text})

            await self._write_history_to_conv(conv_mgr, umo, curr_cid, history_list)
            logger.info(
                f"[猫尾电波] 💾 主动模式打断互动已完整归档至表面上下文 (当前表面记录数: {len(history_list)})！"
            )
        except Exception as e:
            logger.error(f"[猫尾电波] 同步表面历史失败: {e}")

    async def _reconcile_memory_sync(self, umo: str, is_reset_cmd: bool = False):
        """
        双向严格同步小本本 (plugin_cache) 与表面消息 (conv_mgr)：
        1. 若触发了 /reset 或表面消息已被清空，则同生共死，清空小本本与待收尾状态。
        2. 若表面非空：对比小本本与表面历史中的主动消息，哪边少就自动补齐另一边！
        """
        if is_reset_cmd:
            self.plugin_cache.pop(umo, None)
            self.proactive_pending.pop(umo, None)
            self._save_cache()
            logger.info("[猫尾电波] 🔄 检测到会话重置指令，已同步清空私有小本本！")
            return

        history_list = await self._get_surface_history_list(umo)
        if len(history_list) == 0:
            if umo in self.plugin_cache or umo in self.proactive_pending:
                self.plugin_cache.pop(umo, None)
                self.proactive_pending.pop(umo, None)
                self._save_cache()
                logger.info("[猫尾电波] 🔄 检测到表面上下文为空，已同步清空私有小本本（同生共死）！")
            return

        cached_bubbles = self._get_cached_bubble_list(umo)
        surface_proactive_bubbles = self._extract_proactive_bubbles_from_history(history_list)

        # 情况 A：表面历史中有主动气泡，但小本本里少了 -> 补齐到小本本
        merged_bubbles = list(cached_bubbles)
        cache_changed = False
        for sb in surface_proactive_bubbles:
            if sb not in merged_bubbles:
                merged_bubbles.append(sb)
                cache_changed = True
        if cache_changed:
            self._set_cached_bubble_list(umo, merged_bubbles)
            logger.info(f"[猫尾电波] 🔄 双向同步：已从表面上下文同步 {len(merged_bubbles)} 条主动消息至小本本。")

        # 情况 B：小本本中有主动气泡，但表面历史里少了 -> 补齐到表面历史
        surface_assistant_texts = [
            str(item.get("content", "")).strip()
            for item in history_list
            if item.get("role") == "assistant"
        ]
        missing_in_surface = [
            b for b in merged_bubbles if not any(b in s_text for s_text in surface_assistant_texts)
        ]
        if missing_in_surface:
            conv_mgr = getattr(self.context, "conversation_manager", None)
            if conv_mgr:
                curr_cid = await conv_mgr.get_curr_conversation_id(umo)
                if curr_cid:
                    for idx, mb in enumerate(missing_in_surface, start=1):
                        history_list.append({"role": "user", "content": self._build_proactive_user_marker(idx)})
                        history_list.append({"role": "assistant", "content": mb})
                    await self._write_history_to_conv(conv_mgr, umo, curr_cid, history_list)
                    logger.info(
                        f"[猫尾电波] 🔄 双向同步：已将小本本中缺失的 {len(missing_in_surface)} 条主动气泡补齐至表面上下文。"
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
        if high < low:
            high = low
        return random.randint(max(10, low), max(10, high))

    def _roll_interval_seconds(self) -> int:
        low = min(self.interval_min_minutes, self.interval_max_minutes) * 60
        high = max(self.interval_min_minutes, self.interval_max_minutes) * 60
        if high < low:
            high = low
        return random.randint(max(10, low), max(10, high))

    def _clean_llm_output(self, text: str) -> str:
        if not text:
            return ""
        cleaned = self._re_xml_block.sub("", text)
        cleaned = self._re_xml_orphan.sub("", cleaned)
        cleaned = cleaned.strip()
        if not cleaned:
            return ""
        lines = cleaned.split("\n")
        filtered_lines = []
        for line in lines:
            line_str = line.strip()
            if re.search(r"(?:let'?s\s*use|characters?|words?|个?字)", line_str, re.IGNORECASE):
                line_str = re.sub(r"^.*?(?:let'?s\s*use[:：]\s*[`'\"“]?)", "", line_str, flags=re.IGNORECASE)
                line_str = re.sub(
                    r"\s*[`'\"”]?\s*[\(（]?\d*\s*(?:chars?|words?|characters?|个?字)?[\)）]?\s*$",
                    "",
                    line_str,
                    flags=re.IGNORECASE,
                )
                if not line_str.strip() or "let's use" in line_str.lower():
                    continue
            if re.match(r"^(?:Perfect\.?|OK\.?|Pass\.?|->|-&gt;|\d+|\(.*\))+[\s\.\!]*$", line_str, re.IGNORECASE):
                continue
            filtered_lines.append(line_str)
        cleaned = "\n".join(filtered_lines).strip()
        if not cleaned:
            return ""
        cleaned = re.sub(r"^[`'\"“]+|[`'\"”]+$", "", cleaned).strip()
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
        curr_mood = self._get_current_mood_prompt()
        mood_preview = (
            "心境系统已关闭 (基础人设)"
            if not self.enable_mood_wheel
            else ((curr_mood[:25] + "...") if len(curr_mood) > 25 else (curr_mood or "当前时段未配置"))
        )
        max_x = max(1, self.max_burst_count)
        min_req = max(2, int(max_x * 0.6)) if max_x >= 3 else min(max_x, 2)
        cached_list = self._get_cached_bubble_list(self.target_umo) if self.target_umo else []

        msg = (
            f"🐾 【猫尾电波 Tail-Wave v2.1.0】\n"
            f"• 绑定会话：{self.target_umo or '未绑定(私聊一次即可自动捕获)'}\n"
            f"• 绑定用户：{self.target_user_id or '未绑定'}\n"
            f"• 当前心境状态：{mood_preview}\n"
            f"• 发挥空间 (X): {max_x} 句 | 最小保底期望: {min_req} 句 (连击: {'开启' if self.enable_burst else '关闭'})\n"
            f"• 小本本已记主动气泡：{len(cached_list)} 条\n"
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

        umo = event.unified_msg_origin
        if not isinstance(self.plugin_cache, dict):
            self.plugin_cache = {}
        if not isinstance(self.proactive_pending, dict):
            self.proactive_pending = {}

        original_msg = (event.message_str or "").strip()
        if not original_msg or "proactive_status" in original_msg or "proactive_now" in original_msg:
            return

        need_save = False
        if umo and not self.target_umo:
            self.target_umo = umo
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

        # 1. 若正在发送连击气泡中被搭档打断，第一时间拉高熔断信号，阻止后续未发出的气泡
        if self.is_sending_burst:
            self.user_interrupted_burst = True
            logger.info("[猫尾电波] ⚡ 捕获到连击途中的实时打断！已拉高熔断标志，当场进入主动收尾互动！")

        # 2. 检查是否为重置指令或表面清空，执行双向对账同步（哪边少就补哪边；若表面被清空则一起失忆）
        is_reset_cmd = original_msg.startswith("/reset") or original_msg == "reset"
        await self._reconcile_memory_sync(umo, is_reset_cmd=is_reset_cmd)
        if is_reset_cmd or original_msg.startswith("/"):
            self._refresh_active_timer(reason="指令交互")
            return

        # 3. 如果当前处于“主动模式待收尾”状态，针对当前这句打断/回应立即执行 0/1 闭环决策
        is_pending = bool(self.proactive_pending.get(umo, False))
        cached_bubbles = self._get_cached_bubble_list(umo)
        last_proactive_bubble = "\n".join(cached_bubbles[-self.max_burst_count:]) if cached_bubbles else ""

        if is_pending and last_proactive_bubble:
            # 🌟 关键修复：仅将 proactive_pending 置为 False（结束主动模式并切回被动模式），
            # 绝不删除 plugin_cache 小本本！让小本本继续与表面消息保持严格同步，供后续被动检查和下一轮主动发信前读取！
            self.proactive_pending[umo] = False
            self._save_cache()

            logger.info(f"[猫尾电波] 🪄 正在对当句打断/回应“{original_msg}”执行【主动模式 0/1 收尾闭环】！")

            provider = None
            try:
                provider = self.context.get_using_provider(umo)
            except Exception:
                try:
                    provider = self.context.get_using_provider()
                except Exception:
                    pass

            if provider:
                prompt = (
                    f"[情境：你刚才在对话框里主动向搭档发了：“{last_proactive_bubble}”，搭档随即回复：“{original_msg}”。]\n"
                    f"遵循你的既定人设，自主决定如何处理这句回复：\n"
                    f"- 若这是对你刚才主动消息的顺承回应或打断互动，请直接输出一小句简短自然的接话（20字以内），完成本次主动互动收尾；\n"
                    f"- 若话题已自然结束无需再回，仅严格输出暗号：[TAIL_WAVE_STOP]\n"
                    f"- 若搭档说的完全是另一个需要长篇解答的全新独立问题（如要求测试上下文、写代码等），仅严格输出暗号：[TAIL_WAVE_PASS]"
                )

                system_prompt = ""
                try:
                    prov_mgr = getattr(self.context, "provider_manager", None)
                    if prov_mgr and hasattr(prov_mgr, "selected_default_persona"):
                        default_persona = prov_mgr.selected_default_persona
                        if isinstance(default_persona, dict):
                            system_prompt = default_persona.get("prompt", "")
                except Exception:
                    pass

                try:
                    surface_contexts = await self._get_surface_history_list(umo)
                    llm_resp = await provider.text_chat(
                        prompt=prompt,
                        session_id=umo,
                        contexts=surface_contexts[-16:],
                        system_prompt=system_prompt,
                    )
                    raw_reply = self._clean_llm_output(llm_resp.completion_text or "")

                    if "[TAIL_WAVE_PASS]" in raw_reply:
                        logger.info("[猫尾电波] 🔄 判定为全新独立提问，主动模式即时退场，无缝交由被动模式解答！")
                        self._refresh_active_timer(reason="对话互动")
                        return

                    if raw_reply and "[TAIL_WAVE_STOP]" not in raw_reply:
                        lines = [l.strip() for l in raw_reply.split("\n") if l.strip()]
                        short_reply = lines[0] if lines else raw_reply.strip()
                        logger.info(f"[猫尾电波] ⚡ 主动闭环决定接话 (1): {short_reply}")
                        self._refresh_active_timer(reason="对话互动")
                        await self._sync_surface_history(umo, user_text=original_msg, bot_text=short_reply)
                        event.message_str = ""
                        yield event.plain_result(short_reply)
                        if hasattr(event, "stop_event") and callable(event.stop_event):
                            event.stop_event()
                        return
                    else:
                        logger.info("[猫尾电波] ⚡ 主动闭环决定不接话 (0)，同步用户回复后静默结束主动周期。")
                        self._refresh_active_timer(reason="对话互动")
                        await self._sync_surface_history(umo, user_text=original_msg, bot_text="")
                        event.message_str = ""
                        if hasattr(event, "stop_event") and callable(event.stop_event):
                            event.stop_event()
                        return
                except Exception as e:
                    logger.error(f"[猫尾电波] 主动互动闭环生成失败，平滑退回被动模式: {e}")

        self._refresh_active_timer(reason="对话互动")

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

        umo = self.target_umo
        self.is_sending_burst = True
        self.user_interrupted_burst = False

        sent_segments: List[str] = []

        try:
            provider = self.context.get_using_provider(umo)
        except TypeError:
            provider = self.context.get_using_provider()

        if not provider:
            logger.warning("[猫尾电波] 未检测到可用的 LLM Provider，跳过本次主动发送。")
            self.is_sending_burst = False
            return

        if not isinstance(self.plugin_cache, dict):
            self.plugin_cache = {}
        if not isinstance(self.proactive_pending, dict):
            self.proactive_pending = {}

        # 🌟 发主动消息前第 1 道检查：先做双向同步对账（确保表面上下文与小本本 100% 同步）
        await self._reconcile_memory_sync(umo, is_reset_cmd=False)
        was_unanswered = bool(self.proactive_pending.get(umo, False))
        prev_proactive_list = self._get_cached_bubble_list(umo)

        system_prompt = ""
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

        attempts = 0
        max_attempts = max_allowed_turns + 3
        need_delay_before_next = False

        try:
            while len(sent_segments) < max_allowed_turns and attempts < max_attempts:
                if self.user_interrupted_burst:
                    break

                if need_delay_before_next:
                    delay = random.randint(
                        min(self.burst_min_delay_sec, self.burst_max_delay_sec),
                        max(self.burst_min_delay_sec, self.burst_max_delay_sec),
                    )
                    logger.info(f"[猫尾电波] ⏳ 严格停顿倒计时: 等待 {delay} 秒 (真·实时打断监听中)...")
                    for _ in range(int(delay * 10)):
                        if self.user_interrupted_burst:
                            logger.info("[猫尾电波] ⚡ 停顿窗口内捕获搭档打断！立刻熔断，交由主动收尾闭环直接回复当句！")
                            break
                        await asyncio.sleep(0.1)

                    if self.user_interrupted_burst:
                        break
                    need_delay_before_next = False

                attempts += 1
                delivered_so_far = len(sent_segments)
                is_forced_continue = delivered_so_far < min_required_turns

                # 🌟 发主动消息前第 2 道检查：每次生成新气泡前，实时读取最新表面历史 + 校验全部已发主动消息！
                live_surface_history = await self._get_surface_history_list(umo)
                burst_contexts: List[dict] = list(live_surface_history[-16:])

                all_visible_proactive = list(prev_proactive_list)
                for s in sent_segments:
                    if s not in all_visible_proactive:
                        all_visible_proactive.append(s)

                preview_str = " | ".join(all_visible_proactive[-4:]) if all_visible_proactive else "无(当前为全新周期首句)"
                logger.info(
                    f"[猫尾电波] 👁️ 发主动消息前自检 (准备生成第 {delivered_so_far + 1} 句): "
                    f"已见历史主动气泡 {len(prev_proactive_list)} 条 + 本轮已发 {len(sent_segments)} 条 "
                    f"| 表面上下文 {len(burst_contexts)} 条 | 可见主动内容: {preview_str}"
                )

                idle_mins = max(1, int((time.time() - self.last_interaction_time) // 60))
                now_str = datetime.datetime.now().strftime("%H:%M")
                mood_context = self._get_current_mood_prompt()
                mood_block = f"\n{mood_context}" if mood_context else ""

                base_soul_guard = (
                    "保持你当前的人设性格。自然、松弛、简短（单句20字以内），只输出你当下想发给搭档的一小句短消息，不要自问自答。"
                )
                user_guidance = self.proactive_prompt.strip()
                user_block = f"\n补充指引：{user_guidance}" if user_guidance else ""

                if delivered_so_far == 0:
                    if prev_proactive_list:
                        formatted_prev = "\n".join([f"- “{p}”" for p in prev_proactive_list[-6:]])
                        if was_unanswered:
                            action_intent = (
                                f"【你此前在对话框已发出的主动消息】：\n{formatted_prev}\n"
                                f"搭档至今还未回复你上面的主动消息。请确保你已看到上述自己发过的消息，"
                                f"顺着之前的话头或换个自然的角度再随性搭句话（单句20字以内，严禁重复上面已发过的台词或动作）。"
                            )
                        else:
                            action_intent = (
                                f"【你此前在对话框已发过的主动消息】：\n{formatted_prev}\n"
                                f"你们此前已经互动过，现在时间到了 {now_str}，搭档又安静了约 {idle_mins} 分钟。\n"
                                f"请在已知悉上述你自己发过的主动消息与对话框上下文的基础上，主动找搭档搭一句新的短话（单句20字以内，严禁重复之前发过的台词或动作）。"
                            )
                    else:
                        action_intent = (
                            f"当前时间 {now_str}，搭档已安静了约 {idle_mins} 分钟。"
                            f"请结合上下文与当下心境，以符合你人设的方式主动找搭档搭一句短话（单句20字以内）。"
                        )
                    turn_instruction = f"{mood_block}\n{base_soul_guard}{user_block}\n{action_intent}".strip()
                elif is_forced_continue:
                    formatted_curr = "\n".join([f"{idx}. “{s}”" for idx, s in enumerate(sent_segments, start=1)])
                    prev_note = (
                        f"（更早前你还主动发过：{' / '.join(prev_proactive_list[-3:])}）\n"
                        if prev_proactive_list
                        else ""
                    )
                    turn_instruction = (
                        f"{prev_note}【你刚刚在对话框里连续发出的主动消息】：\n{formatted_curr}\n"
                        f"搭档尚未接话，请在看到自己上述主动消息的基础上，顺着最后一句话的语气自然补充下一小句短消息（单句15字以内，严禁重复上面已发过的动作或台词）。"
                    )
                else:
                    formatted_curr = "\n".join([f"{idx}. “{s}”" for idx, s in enumerate(sent_segments, start=1)])
                    turn_instruction = (
                        f"【你刚刚在对话框里连续发出的主动消息】：\n{formatted_curr}\n"
                        f"请在看到自己上述主动消息的基础上判断：如果觉得话已说完无需再补，请仅输出暗号：{stop_token}；"
                        f"如果还想顺势再补最后一小句，请直接输出短句（15字以内，严禁重复上面已发过的动作或台词）。"
                    )

                try:
                    llm_resp = await provider.text_chat(
                        prompt=turn_instruction,
                        session_id=umo,
                        contexts=list(burst_contexts),
                        system_prompt=system_prompt,
                    )
                except Exception as e:
                    logger.warning(
                        f"[猫尾电波] 第 {delivered_so_far + 1} 条气泡生成被拦截或异常 ({e})，不计入投递数，正在重试 ({attempts}/{max_attempts})..."
                    )
                    await asyncio.sleep(1)
                    continue

                if self.user_interrupted_burst:
                    break

                raw_text = self._clean_llm_output(llm_resp.completion_text or "")

                if not raw_text or stop_token in raw_text:
                    if is_forced_continue:
                        cleaned_retry = raw_text.replace(stop_token, "").strip() if raw_text else ""
                        if not cleaned_retry:
                            logger.info(
                                f"[猫尾电波] 🔄 当前已投递 {delivered_so_far}/{min_required_turns} 句（未达最小保底期望），"
                                f"本次空响应被忽略，不计入投递数，自动重试 ({attempts}/{max_attempts})..."
                            )
                            await asyncio.sleep(0.8)
                            continue
                        raw_text = cleaned_retry
                    else:
                        logger.info(f"[猫尾电波] ✨ 已满足最小保底期望 ({delivered_so_far} 句)，模型自主收尾结束连击。")
                        break

                lines = [l.strip() for l in raw_text.split("\n") if l.strip()]
                seg = lines[0] if lines else raw_text.strip()
                if not seg:
                    continue

                chain = MessageChain().message(seg)
                await self.context.send_message(umo, chain)
                sent_segments.append(seg)
                logger.info(f"[猫尾电波] 成功投递第 {len(sent_segments)} 条微型气泡: {seg}")

                # 🌟 投递成功后，当场追加进小本本列表（保留历史主动消息）并实时以独立对话条目写入表面上下文
                updated_cache = list(prev_proactive_list)
                for s in sent_segments:
                    if s not in updated_cache:
                        updated_cache.append(s)
                self._set_cached_bubble_list(umo, updated_cache)
                self.proactive_pending[umo] = True
                self._save_cache()

                total_proactive_idx = len(updated_cache)
                await self._append_proactive_bubble_to_surface(umo, seg, turn_index=total_proactive_idx)
                need_delay_before_next = True

            if sent_segments:
                now_ts = time.time()
                self.last_proactive_time = now_ts
                self.last_interaction_time = now_ts
                self.daily_sent_count += 1
                self.current_idle_target_sec = self._roll_idle_seconds()
                self.current_interval_target_sec = self._roll_interval_seconds()
                logger.info(
                    f"[猫尾电波] 🏁 本轮主动连击结束（实际成功投递 {len(sent_segments)} 句，"
                    f"中途打断: {'是' if self.user_interrupted_burst else '否'}）。"
                )

        except Exception as e:
            logger.error(f"[猫尾电波] 执行主动交互时出错: {e}")
        finally:
            self.is_sending_burst = False
