"""Conductor state machine coordinating LLM stream, tags, sentences, initiative and renderer."""

from __future__ import annotations

import asyncio
import base64
import datetime
import logging
import re
import time
from pathlib import Path
from typing import Any

import aiohttp

from daemon.bus import (
    EventBus,
    Interrupt,
    PlaybackEnded,
    PlaybackStarted,
    ScreenChange,
    UserCommand,
    UserSpeech,
    UserSpeechStart,
)
from daemon.coder import AntigravityBridge, CodingTask
from daemon.initiative import InitiativeDecision, InitiativeManager
from daemon.launcher import AppLauncher
from daemon.llm import LLMClient
from daemon.memory import MemoryManager
from daemon.rag import RAGManager
from daemon.sentence import SentenceChunk, SentenceSplitter
from daemon.tagparser import CommandTag, EmotionTag, GestureTag, LookTag, SilentTag, TagParser
from daemon.tencent_bridge import TencentMemoryBridge
from daemon.tts import BaseTTSEngine, get_audio_path, save_audio_file
from daemon.ws_server import WebSocketServer

logger = logging.getLogger(__name__)


class Conductor:
    """Orchestrates dialogue turns, LLM generation, tag parsing, proactive initiative, and avatar state."""

    def __init__(
        self,
        llm: LLMClient,
        ws_server: WebSocketServer,
        bus: EventBus,
        tts: BaseTTSEngine | None = None,
        stt: Any | None = None,
        vision: Any | None = None,
        initiative: InitiativeManager | None = None,
        memory: MemoryManager | None = None,
        rag: RAGManager | None = None,
        coder: AntigravityBridge | None = None,
    ) -> None:
        self.llm = llm
        self.ws = ws_server
        self.bus = bus
        self.tts = tts
        self.stt = stt
        self.vision = vision
        self.initiative = initiative or InitiativeManager()
        self.memory = memory
        self.tencent = TencentMemoryBridge()
        asyncio.create_task(self.tencent.initialize())
        self.rag = rag or RAGManager(memory=self.memory, tencent=self.tencent)
        self.coder = coder or AntigravityBridge()
        self._pending_coder_notice: str | None = None

        self.state = "idle"
        self.history: list[dict[str, str]] = []
        if self.memory:
            asyncio.create_task(self._restore_history_from_memory())
        self._current_gen_task: asyncio.Task[None] | None = None
        self._initiative_task: asyncio.Task[None] | None = None
        self._utterance_counter = 0

        # Desktop audio sequential playback queue
        self._desktop_audio_queue: asyncio.Queue[tuple[str, Path]] = asyncio.Queue()
        self._current_mpv_proc: asyncio.subprocess.Process | None = None
        self._desktop_audio_task: asyncio.Task[None] = asyncio.create_task(
            self._desktop_audio_worker()
        )

        # Subscribe to bus events
        self.bus.subscribe(Interrupt, self.handle_interrupt)
        self.bus.subscribe(PlaybackStarted, self.handle_playback_started)
        self.bus.subscribe(PlaybackEnded, self.handle_playback_ended)
        self.bus.subscribe(UserCommand, self.handle_user_command)
        self.bus.subscribe(UserSpeechStart, self.handle_user_speech_start)
        self.bus.subscribe(UserSpeech, self.handle_user_speech)
        self.bus.subscribe(ScreenChange, self.handle_screen_change)

        # Start initiative evaluation loop
        self._initiative_task = asyncio.create_task(self._initiative_loop())

    async def close(self) -> None:
        """Clean up background tasks and persistent memory."""
        self._interrupt_desktop_audio()
        if self._desktop_audio_task and not self._desktop_audio_task.done():
            self._desktop_audio_task.cancel()
        if self._initiative_task and not self._initiative_task.done():
            self._initiative_task.cancel()
        if self._current_gen_task and not self._current_gen_task.done():
            self._current_gen_task.cancel()
        if self.coder and self.coder.is_busy():
            await self.coder.cancel_current_task()
        if self.memory:
            await self.memory.close()
        if self.tencent:
            await self.tencent.close()

    async def _restore_history_from_memory(self) -> None:
        """Restore previous dialogue turns from persistent SQLite database."""
        if not self.memory:
            return
        try:
            recent = await self.memory.get_recent_messages(limit=16)
            if recent:
                self.history = recent
                logger.info(
                    "[Memory] Restored %d dialogue turns from persistent memory",
                    len(recent),
                )
        except (OSError, RuntimeError, ValueError) as exc:
            logger.warning("[Memory] Failed to restore history: %s", exc)

    async def _on_coder_task_completed(self, task: CodingTask) -> None:
        """Callback invoked when background Antigravity task finishes."""
        status_text = (
            "успешно выполнил задачу" if task.status == "completed" else "завершил с ошибкой"
        )
        summary = task.summary or task.error or "без подробностей"
        notice = (
            f"coder_done: Агент Антигравити {status_text} «{task.prompt}»! "
            f"Итог: {summary[:300]}. Скажи пользователю живым голосом, что задача сделана и вкратце что там получилось."
        )
        logger.info("[Conductor] Antigravity task %s (%s): %s", task.id, task.status, summary)
        self._pending_coder_notice = notice
        if self.state == "idle" and (not self._current_gen_task or self._current_gen_task.done()):
            self._pending_coder_notice = None
            await self.process_proactive_turn(seed=notice)

    def set_state(self, new_state: str) -> None:
        """Update and broadcast state."""
        if self.state != new_state:
            self.state = new_state
            self.ws.set_state(new_state)
            logger.info("[State] %s", new_state)

    async def handle_interrupt(self, event: Interrupt) -> None:
        """Cancel ongoing generation and speech on user barge-in."""
        logger.info("Interrupt triggered: %s", event.reason)
        self._interrupt_desktop_audio()
        if self._current_gen_task and not self._current_gen_task.done():
            self._current_gen_task.cancel()
            self._current_gen_task = None

        await self.ws.broadcast({"type": "stop"})
        self.set_state("idle")

    def _is_stop_command(self, text: str) -> bool:
        """Check if user utterance is an urgent stop / cancel command."""
        clean = text.strip().lower()
        clean = re.sub(r"^(?:сузу|сузука|слушай|эй)\s*[,!\.-]?\s*", "", clean).strip()
        pattern = (
            r"^(?:быстро\s+)?(?:стоп|стопни|стой|хватит|остановись|останови|прекрати|тормози|заткнись|отмена|отбой)"
            r"(?:\s+(?:машина|всё|проект|код|кодить|антигравити|подожди|что\s+(?:ты\s+)?делаешь))?[!.]*$"
        )
        return bool(re.match(pattern, clean, re.IGNORECASE))

    async def _execute_fast_stop(self) -> str:
        """Immediately stop any active Antigravity coding task, speech playback, and generation."""
        logger.info("[Conductor] Fast emergency STOP executed")
        coder_was_busy = self.coder.is_busy()
        if coder_was_busy:
            _ok, msg = await self.coder.cancel_current_task()
            logger.info("[Conductor] Cancelled Antigravity coding task on STOP: %s", msg)

        # Immediately kill any playing speech in mpv and clear queued audio
        self._interrupt_desktop_audio()

        # Cancel active LLM generation
        if self._current_gen_task and not self._current_gen_task.done():
            self._current_gen_task.cancel()
            self._current_gen_task = None

        # Reset renderer avatar animation/speech
        await self.ws.broadcast({"type": "stop"})
        await self.ws.broadcast({"type": "emotion", "name": "surprised", "intensity": 0.8})
        await self.ws.broadcast({"type": "gesture", "name": "surprise"})
        self.set_state("idle")

        if coder_was_busy:
            reply = "Всё, стопнула задачу в проекте и замерла!"
        else:
            reply = "Стопнула, молчу!"

        # Synthesize and speak snappy reply directly (zero LLM wait)
        chunk = SentenceChunk(text=reply)
        await self._dispatch_sentence_chunk(chunk, default_emo="surprised", default_intensity=0.8)

        # Update history and memory
        self.history.append({"role": "user", "content": "Стоп"})
        self.history.append({"role": "assistant", "content": reply})
        if self.memory:
            await self.memory.record_message(role="user", text="Стоп", kind="user")
            await self.memory.record_message(role="assistant", text=reply, kind="chat")

        return reply

    async def handle_playback_started(self, event: PlaybackStarted) -> None:
        self.set_state("speaking")

    async def handle_playback_ended(self, event: PlaybackEnded) -> None:
        if self.state == "speaking":
            self.set_state("idle")

    async def handle_user_command(self, event: UserCommand) -> None:
        """Handle control commands from vtuberctl or UI."""
        cmd = event.command
        args = event.args

        if cmd == "click":
            region = args.get("region", "body")
            if region == "head":
                await self.ws.broadcast(
                    {"type": "emotion", "name": "surprised", "intensity": 0.8, "hold_ms": 1500}
                )
                await self.ws.broadcast({"type": "gesture", "name": "nod"})
            else:
                await self.ws.broadcast({"type": "gesture", "name": "shrug"})

        elif cmd == "mood":
            mood_val = args.get("mood", "playful")
            self.initiative.set_mood(mood_val)

        elif cmd == "dnd":
            enabled = bool(args.get("enabled", True))
            self.initiative.set_dnd(enabled)

        elif cmd == "mute":
            enabled = bool(args.get("enabled", True))
            self.initiative.set_muted(enabled)

        elif cmd == "blind":
            enabled = bool(args.get("enabled", True))
            if self.vision:
                self.vision.set_blind(enabled)

        elif cmd == "say":
            text = args.get("text", "")
            if text:
                chunk = SentenceChunk(text=text)
                await self._dispatch_sentence_chunk(chunk)

        elif cmd == "ask":
            text = args.get("text", "")
            if text:
                asyncio.create_task(self.bus.publish(UserSpeech(audio_data=b"", text=text)))

        elif cmd == "look":
            target = args.get("target", "cursor")
            await self.ws.broadcast({"type": "look", "target": target})

        elif cmd == "memory":
            action = args.get("action", "list")
            if self.memory:
                if action == "wipe":
                    await self.memory.wipe_memory()
                elif action == "forget":
                    fact_id = int(args.get("id", 0))
                    await self.memory.delete_fact(fact_id)

        elif cmd == "stop":
            await self._execute_fast_stop()

    async def handle_user_speech_start(self, event: UserSpeechStart) -> None:
        """User started speaking; immediately interrupt active speech and transition to listening."""
        self.initiative.on_user_speech_start()
        self._interrupt_desktop_audio()
        if self.state in ("speaking", "thinking"):
            await self.bus.publish(Interrupt(reason="user_speech_start"))
        self.set_state("listening")

    async def handle_user_speech(self, event: UserSpeech) -> None:
        """Transcribe speech audio or process text, then trigger user turn."""
        t_speech_end = time.time()
        text = event.text
        if not text and event.audio_data is not None and self.stt:
            try:
                text = await asyncio.to_thread(self.stt.transcribe, event.audio_data)
            except (RuntimeError, ValueError, OSError) as exc:
                logger.warning("STT transcription failed: %s", exc)
                text = ""

        t_stt_done = time.time()
        vad_stt_ms = (t_stt_done - t_speech_end) * 1000.0

        clean_text = text.strip()
        if clean_text:
            await self.process_user_turn(clean_text, vad_stt_ms=vad_stt_ms, t_start=t_speech_end)
        else:
            if self.state == "listening":
                self.set_state("idle")

    async def handle_screen_change(self, event: ScreenChange) -> None:
        """Handle noticeable screen content changes."""
        logger.debug(
            "Conductor received ScreenChange: %s (%s)", event.active_app, event.active_title
        )
        now = time.time()
        # Proactively react to screen changes when idle and user has been quiet for a moment
        if (
            self.state == "idle"
            and (self._current_gen_task is None or self._current_gen_task.done())
            and not self.initiative.dnd
            and not self.initiative.muted
            and not self.initiative.is_in_quiet_hours()
            and (now - self.initiative.last_user_speech_ts >= 20.0)
            and (now - self.initiative.last_avatar_speech_ts >= 45.0)
        ):
            logger.info("[Initiative] Triggering proactive screen reaction for %s", event.active_app)
            await self.process_proactive_turn(seed=f"screen_change:{event.active_app}")

    async def process_user_turn(
        self,
        user_text: str,
        context_info: str | None = None,
        image_base64: str | None = None,
        vad_stt_ms: float = 0.0,
        t_start: float | None = None,
    ) -> None:
        """Handle a complete user message turn with cancellation of stale turns."""
        start_ts = t_start or time.time()

        # Fast Emergency Stop path: immediately abort any coding task, speech, or generation
        if self._is_stop_command(user_text):
            logger.info("[Conductor] Fast emergency STOP triggered by: '%s'", user_text)
            await self._execute_fast_stop()
            return

        # On-the-fly user name correction detection (strict affirmative pattern, ignore questions)
        if self.memory and not re.search(
            r"\b(?:как|помнишь|знаешь|угадай|кто)\b", user_text, re.IGNORECASE
        ):
            name_match = re.search(
                r"(?:(?:меня зовут|мо[её] имя)\s+([А-ЯЁA-Z][а-яёa-z]+)|я\s+(?:не\s+([А-ЯЁA-Z][а-яёa-z]+)[,\s]+а\s+([А-ЯЁA-Z][а-яёa-z]+)|([А-ЯЁA-Z][а-яёa-z]+)[,\s]+а\s+не\s+([А-ЯЁA-Z][а-яёa-z]+)))",
                user_text,
            )
            if name_match:
                g1 = name_match.group(1)
                g2, g3 = name_match.group(2), name_match.group(3)
                g4, g5 = name_match.group(4), name_match.group(5)
                detected_name = g1 or g3 or g4
                wrong_name = g2 or g5
                if detected_name and len(detected_name) >= 2:
                    asyncio.create_task(
                        self.memory.update_user_name_facts(detected_name, wrong_name)
                    )

        # Direct desktop app launch intent recognition
        launch_match = re.search(
            r"\b(?:запусти|открой|стартани)\s+([a-zA-Zа-яёА-ЯЁ0-9_-]+)\b",
            user_text,
            re.IGNORECASE,
        )
        if launch_match:
            cand = launch_match.group(1).lower()
            if AppLauncher.resolve_app_command(cand):
                logger.info("[Conductor] Detected direct launch intent for '%s'", cand)
                asyncio.create_task(AppLauncher.launch(cand))

        # Close app intent recognition
        close_match = re.search(
            r"\b(?:закрой|прибей|выключи|кильни)\s+([a-zA-Zа-яёА-ЯЁ0-9_-]+)\b",
            user_text,
            re.IGNORECASE,
        )
        if close_match:
            cand = close_match.group(1).lower()
            logger.info("[Conductor] Detected close intent for '%s'", cand)
            asyncio.create_task(AppLauncher.close(cand))

        # Media playback intent recognition
        if re.search(r"\b(?:пауза|плей|музык|трек|стоп трек|след трек|следующий трек)\b", user_text, re.IGNORECASE):
            low = user_text.lower()
            if "след" in low:
                asyncio.create_task(AppLauncher.media("next"))
            elif "пред" in low:
                asyncio.create_task(AppLauncher.media("previous"))
            else:
                asyncio.create_task(AppLauncher.media("play-pause"))

        # Volume control intent recognition
        if re.search(r"\b(?:погромче|сделай громче|прибавь звук)\b", user_text, re.IGNORECASE):
            asyncio.create_task(AppLauncher.volume("up"))
        elif re.search(r"\b(?:потише|сделай тише|убавь звук)\b", user_text, re.IGNORECASE):
            asyncio.create_task(AppLauncher.volume("down"))

        # Antigravity coding agent intent recognition
        code_start_match = re.search(
            r"\b(?:попроси|пусть|дай задачу|напиши код|запусти код|поставь задачу)\s+(?:в\s+)?антигравити\s*(?:написать|сделать|выполнить)?\s*[:,-]?\s*(.+)",
            user_text,
            re.IGNORECASE,
        )
        if not code_start_match:
            code_start_match = re.search(
                r"\bантигравити\b[,\s]+(?:сделай|напиши|исправь|проверь|выполни|запусти)\s+(.+)",
                user_text,
                re.IGNORECASE,
            )

        coder_ctx = ""
        if code_start_match:
            task_prompt = code_start_match.group(1).strip()
            _ok, msg = await self.coder.start_task(
                prompt=task_prompt,
                on_complete=self._on_coder_task_completed,
            )
            coder_ctx = f"Статус запуска задачи в Антигравити: {msg}. Обязательно подтверди пользователю голосом, что ты запустила кодинг в Антигравити!"

        elif re.search(
            r"\b(?:как там|статус|что делает|как поживает|что с задачей)\s+антигравити\b",
            user_text,
            re.IGNORECASE,
        ) or re.search(r"\bантигравити\s+(?:статус|как там)\b", user_text, re.IGNORECASE):
            st = self.coder.get_status()
            if st["active"]:
                coder_ctx = (
                    f"Статус задачи Антигравити: выполняется уже {st['elapsed_seconds']} сек. "
                    f"Текущая задача: «{st['prompt']}». Последний вывод: {st['latest_snippet']}"
                )
            else:
                coder_ctx = (
                    f"Статус Антигравити: {st.get('message', 'свободен')}. "
                    f"Последняя задача: {st.get('prompt', 'нет')}, результат: {st.get('summary', 'нет')}"
                )

        elif re.search(
            r"\b(?:стоп|отмени|останови|выключи)\s+(?:задачу\s+)?антигравити\b",
            user_text,
            re.IGNORECASE,
        ):
            _ok, msg = await self.coder.cancel_current_task()
            coder_ctx = f"Результат отмены Антигравити: {msg}"

        # Direct terminal command typing intent recognition
        term_match = re.search(
            r"\b(?:напиши|введи|выполни|запусти)\s+(?:в\s+)?(?:терминал[еа]|консол[ие]|команд[уе])\s*[:,-]?\s*(.+)",
            user_text,
            re.IGNORECASE,
        )
        if term_match:
            cmd_to_run = term_match.group(1).strip()
            logger.info("[Conductor] Typing command into active terminal: %s", cmd_to_run)
            asyncio.create_task(AppLauncher.type_text(cmd_to_run, press_enter=True))

        # Direct text typing into active window / chat intent recognition
        type_match = re.search(
            r"\b(?:напечатай|напиши|введи|отправь)\s+(?:в\s+)?(?:чат[еа]?|окн[ео]|браузер[еа]?|сообщени[ея])\s*[:,-]?\s*(.+)",
            user_text,
            re.IGNORECASE,
        )
        if type_match:
            msg_to_type = type_match.group(1).strip()
            press_ent = "отправь" in user_text.lower()
            logger.info("[Conductor] Typing text into active chat/window: %s", msg_to_type)
            asyncio.create_task(AppLauncher.type_text(msg_to_type, press_enter=press_ent))

        # Enter key intent recognition
        if re.search(r"\b(?:нажми\s+(?:энтер|enter|ввод)|отправь\s+текст)\b", user_text, re.IGNORECASE):
            asyncio.create_task(AppLauncher.press_key("Return"))

        if coder_ctx:
            context_info = f"{context_info}; {coder_ctx}" if context_info else coder_ctx

        # Context block from vision
        if context_info is None and self.vision:
            context_info = f"активное окно: {self.vision.active_app} ({self.vision.active_title}); {self.vision.screen_context}"

        # Vision Layer 2: Attach screen frame if user asks about screen
        if image_base64 is None and self.vision and not self.vision.blind_mode:
            keywords = (
                "экран",
                "смотри",
                "видишь",
                "что тут",
                "что здесь",
                "ошибка",
                "взгляни",
                "глянь",
            )
            if any(kw in user_text.lower() for kw in keywords):
                latest_frame = self.vision.get_latest_frame()
                if latest_frame:
                    image_base64 = base64.b64encode(latest_frame).decode("ascii")
                    logger.info("Attached live screen frame to user turn query")

        # If already generating or thinking, cancel stale task
        if self._current_gen_task and not self._current_gen_task.done():
            logger.info("Canceling ongoing turn to process new user utterance")
            self._current_gen_task.cancel()

        self._current_gen_task = asyncio.create_task(
            self._generate_turn(
                user_text=user_text,
                context_info=context_info,
                image_base64=image_base64,
                vad_stt_ms=vad_stt_ms,
                start_ts=start_ts,
                is_proactive=False,
            )
        )
        await self._current_gen_task

    async def process_proactive_turn(self, seed: str) -> None:
        """Trigger a proactive turn generated by the fast LLM model."""
        if self.state != "idle" or (self._current_gen_task and not self._current_gen_task.done()):
            logger.debug("[Initiative] Skipped proactive turn: conductor busy (%s)", self.state)
            return

        now_str = datetime.datetime.now().strftime("%H:%M")
        active_app = self.vision.active_app if self.vision else "неизвестно"
        screen_desc = self.vision.screen_context if self.vision else "экран спокоен"
        silence_min = max(0, int((time.time() - self.initiative.last_user_speech_ts) / 60))
        recent_inits = "; ".join(self.initiative.recent_initiatives[-3:]) or "нет"

        # Vision: Attach actual live screen capture for multimodal observation
        image_base64: str | None = None
        if self.vision and not self.vision.blind_mode:
            latest_frame = self.vision.get_latest_frame()
            if latest_frame:
                image_base64 = base64.b64encode(latest_frame).decode("ascii")
                logger.info("[Initiative] Attached live screen frame to proactive observation")

        is_coding = any(
            ide in active_app.lower()
            for ide in ("zed", "code", "cursor", "kitty", "terminal", "foot", "alacritty", "neovim", "nvim")
        )

        if seed.startswith("coder_done:"):
            proactive_user_prompt = (
                f"СОБЫТИЕ: {seed}\n"
                f"КОНТЕКСТ: время {now_str}, активное приложение: {active_app}.\n\n"
                f"ЗАДАЧА:\n"
                f"1. Ты поручала автономному кодинг-агенту Антигравити задачу по коду, и он только что закончил работу!\n"
                f"2. Сообщи пользователю бодро и живо (СТРОГО 1 короткое предложение вслух), что Антигравити справился (или выдал ошибку), "
                f"и в паре слов суть итога.\n"
                f"3. Используй тег эмоции <emo=happy:0.8> или <emo=surprised:0.8> и жест <gst=nod> или <gst=wave>."
            )
        elif is_coding:
            proactive_user_prompt = (
                f"ТЫ СМОТРИШЬ НА ЭКРАН ПОЛЬЗОВАТЕЛЯ, ОН СЕЙЧАС КОДИТ (скриншот прикреплен).\n"
                f"КОНТЕКСТ: время {now_str}, редактор/терминал: {active_app} ({screen_desc}), тишина: {silence_min} мин, "
                f"твоё настроение: {self.initiative.mood.value}.\n"
                f"Твои прошлые недавние реплики: [{recent_inits}] (КАТЕГОРИЧЕСКИ НЕ ПОВТОРЯЙ ИХ И ЭТИ ТЕМЫ).\n\n"
                f"ПРАВИЛА ПАРНОГО ПРОГРАММИРОВАНИЯ:\n"
                f"1. Если на экране всё спокойно, пользователь пишет код или нет явного бага — выведи ТОЛЬКО <silent>.\n"
                f"2. Если заметила реальный упавший тест (FAILED), ошибку линтера или смешной момент в коде — "
                f"выдай СТРОГО ОДНО КОРОТКОЕ ПРЕДЛОЖЕНИЕ (до 10-12 слов): подколи, заметь баг или подбодри.\n"
                f"3. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО говорить банальности ('ты в Zed', 'вижу код', 'ты кодишь'). "
                f"Пользователь сам знает, что он делает! Реагируй только на саму суть.\n"
                f"4. СТРОГИЙ ЗАПРЕТ: НЕ говорить про сон, отдых и режим. НЕ называть по имени.\n"
                f"5. Обязательно ставь теги эмоций и жестов в начале (например, <emo=curious:0.8><look=screen>). Если говорить нечего — ТОЛЬКО <silent>."
            )
        else:
            proactive_user_prompt = (
                f"ТЫ СМОТРИШЬ НА ЭКРАН ПОЛЬЗОВАТЕЛЯ (скриншот прикреплен).\n"
                f"КОНТЕКСТ: время {now_str}, активное приложение: {active_app} ({screen_desc}), тишина: {silence_min} мин, "
                f"твоё настроение: {self.initiative.mood.value}.\n"
                f"Твои прошлые недавние реплики: [{recent_inits}] (КАТЕГОРИЧЕСКИ НЕ ПОВТОРЯЙ ИХ И ЭТИ ТЕМЫ).\n\n"
                f"ПРАВИЛА ЖИВОЙ РЕАКЦИИ:\n"
                f"1. Если на экране ничего нового, статичная страница или нет реального повода сказать — выведи ТОЛЬКО <silent>.\n"
                f"2. Если есть реальный повод пошутить или заметить свежую деталь — выдай СТРОГО ОДНО КОРОТКОЕ ПРЕДЛОЖЕНИЕ (до 10-12 слов).\n"
                f"3. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО пересказывать экран ('ты в браузере', 'вижу документ'). Пользователь сам видит свой экран!\n"
                f"4. СТРОГИЙ ЗАПРЕТ: НЕ говорить про сон, усталость и режим. НЕ называть по имени. Никакого мата без реального шока.\n"
                f"5. Обязательно ставь теги эмоций и жестов в начале (например, <emo=curious:0.8><look=screen>). Если всё статично — ТОЛЬКО <silent>."
            )

        self._current_gen_task = asyncio.create_task(
            self._generate_turn(
                user_text=proactive_user_prompt,
                context_info=None,
                image_base64=image_base64,
                vad_stt_ms=0.0,
                start_ts=time.time(),
                is_proactive=True,
            )
        )
        await self._current_gen_task

    async def _generate_turn(
        self,
        user_text: str,
        context_info: str | None,
        image_base64: str | None,
        vad_stt_ms: float = 0.0,
        start_ts: float | None = None,
        is_proactive: bool = False,
    ) -> None:
        self.set_state("thinking")
        t_turn_start = start_ts or time.time()

        facts_list = None
        episodes_list = None
        if self.memory:
            fact_records = await self.memory.get_facts(limit=20)
            facts_list = [f.text for f in fact_records]
            episode_records = await self.memory.get_recent_episodes(limit=5)
            episodes_list = [e.summary for e in episode_records]

        # Two-Tier Hybrid RAG retrieval (Tier 1 SQLite FTS5 + Tier 2 TencentDB Agent Memory)
        project_notes = None
        if self.rag:
            active_app = self.vision.active_app if self.vision else ""
            active_title = self.vision.active_title if self.vision else ""
            project_notes = await self.rag.retrieve(
                query=user_text,
                active_app=active_app,
                active_title=active_title,
                max_items=3,
            )

        if is_proactive:
            # Proactive turns have access to memory facts and recent context!
            messages = self.llm.build_messages(
                user_message=user_text,
                history=self.history[-6:],
                context_info=context_info,
                facts=facts_list,
                episodes=episodes_list,
                image_base64=image_base64,
                project_notes=project_notes,
            )
            target_model = self.llm.cfg.fast_model
        else:
            messages = self.llm.build_messages(
                user_message=user_text,
                history=self.history[-12:],
                context_info=context_info,
                facts=facts_list,
                episodes=episodes_list,
                image_base64=image_base64,
                project_notes=project_notes,
            )
            target_model = self.llm.cfg.chat_model

        tag_parser = TagParser()
        sentence_splitter = SentenceSplitter()
        full_reply_text: list[str] = []
        dispatched_texts: list[str] = []

        first_token_received = False
        first_audio_dispatched = False
        t_first_token = 0.0
        t_first_audio = 0.0

        active_turn_emo = "neutral"
        active_turn_intensity = 1.0

        max_sentences = 1 if is_proactive else 2
        if not is_proactive and any(w in user_text.lower() for w in ("подробно", "расскажи", "объясни", "почему")):
            max_sentences = 4
        dispatched_sentences_count = 0

        try:
            async for token in self.llm.stream_response(messages, model=target_model):
                if not first_token_received:
                    first_token_received = True
                    t_first_token = time.time()

                parsed_items = tag_parser.feed(token)
                for item in parsed_items:
                    if isinstance(item, str):
                        full_reply_text.append(item)
                        ready_chunks = sentence_splitter.feed(item)
                        for chunk in ready_chunks:
                            if dispatched_sentences_count < max_sentences:
                                dispatched_sentences_count += 1
                                dispatched_texts.append(chunk.text)
                                await self._dispatch_sentence_chunk(
                                    chunk,
                                    default_emo=active_turn_emo,
                                    default_intensity=active_turn_intensity,
                                    )
                                if not first_audio_dispatched:
                                    first_audio_dispatched = True
                                    t_first_audio = time.time()
                    else:
                        if isinstance(item, EmotionTag):
                            active_turn_emo = item.name
                            active_turn_intensity = item.intensity
                        await self._handle_tag(item, sentence_splitter)

                if dispatched_sentences_count >= max_sentences:
                    break

            # Flush remaining buffers if under limit
            if dispatched_sentences_count < max_sentences:
                remaining_tags = tag_parser.flush()
                for r_tag in remaining_tags:
                    if isinstance(r_tag, str):
                        full_reply_text.append(r_tag)
                        ready_chunks = sentence_splitter.feed(r_tag)
                        for chunk in ready_chunks:
                            if dispatched_sentences_count < max_sentences:
                                dispatched_sentences_count += 1
                                dispatched_texts.append(chunk.text)
                                await self._dispatch_sentence_chunk(
                                    chunk,
                                    default_emo=active_turn_emo,
                                    default_intensity=active_turn_intensity,
                                )

                final_chunks = sentence_splitter.flush()
                for chunk in final_chunks:
                    if dispatched_sentences_count < max_sentences:
                        dispatched_sentences_count += 1
                        dispatched_texts.append(chunk.text)
                        await self._dispatch_sentence_chunk(
                            chunk,
                            default_emo=active_turn_emo,
                            default_intensity=active_turn_intensity,
                        )

            reply_str = " ".join(dispatched_texts).strip()
            if reply_str:
                if is_proactive:
                    self.history.append({"role": "assistant", "content": reply_str})
                    self.initiative.on_avatar_spoken(is_proactive=True, text=reply_str)
                    if self.memory:
                        await self.memory.record_message(
                            role="assistant", text=reply_str, kind="proactive"
                        )
                else:
                    self.history.append({"role": "user", "content": user_text})
                    self.history.append({"role": "assistant", "content": reply_str})
                    self.initiative.on_avatar_spoken(is_proactive=False, text=reply_str)
                    if self.memory:
                        await self.memory.record_message(role="user", text=user_text, kind="user")
                        await self.memory.record_message(
                            role="assistant", text=reply_str, kind="chat"
                        )
                        asyncio.create_task(self.memory.maybe_summarize(self.llm))

                if self.stt:
                    self.stt.set_last_avatar_phrase(reply_str)

                if first_audio_dispatched:
                    llm_token_ms = (t_first_token - t_turn_start) * 1000.0 if t_first_token else 0.0
                    tts_audio_ms = (
                        (t_first_audio - t_first_token) * 1000.0
                        if (t_first_audio and t_first_token)
                        else 0.0
                    )
                    total_ms = (t_first_audio - t_turn_start) * 1000.0
                    logger.info(
                        "[Latency Budget] VAD+STT: %.0fms | LLM 1st Token: %.0fms | TTS 1st Audio: %.0fms | Total: %.0fms",
                        vad_stt_ms,
                        llm_token_ms,
                        tts_audio_ms,
                        total_ms,
                    )
            else:
                if is_proactive:
                    logger.info("[Initiative] Avatar chose to remain silent (<silent>)")

        except asyncio.CancelledError:
            logger.info("Turn generation was cancelled (barge-in)")
            raise
        except Exception:
            logger.exception("Error during turn generation")
        finally:
            if self.state == "thinking":
                self.set_state("idle")

    async def _handle_tag(self, tag: Any, splitter: SentenceSplitter) -> None:
        if isinstance(tag, SilentTag):
            logger.info("[Tag] Avatar chose to remain silent (<silent>)")
            return

        if isinstance(tag, EmotionTag):
            logger.debug("[Tag] Emotion: %s (%s)", tag.name, tag.intensity)
            await self.ws.broadcast(
                {
                    "type": "emotion",
                    "name": tag.name,
                    "intensity": tag.intensity,
                }
            )
            splitter.add_tag(tag)

        elif isinstance(tag, GestureTag):
            logger.debug("[Tag] Gesture: %s", tag.name)
            await self.ws.broadcast({"type": "gesture", "name": tag.name})
            splitter.add_tag(tag)

        elif isinstance(tag, LookTag):
            logger.debug("[Tag] Look: %s", tag.target)
            await self.ws.broadcast({"type": "look", "target": tag.target})
            splitter.add_tag(tag)

        elif isinstance(tag, CommandTag):
            logger.info("[Tag] Command requested: %s", tag.action)
            if tag.action.startswith("launch:"):
                target = tag.action.split("launch:", 1)[1]
                asyncio.create_task(AppLauncher.launch(target))
            elif tag.action.startswith("close:"):
                target = tag.action.split("close:", 1)[1]
                asyncio.create_task(AppLauncher.close(target))
            elif tag.action.startswith("media:"):
                act = tag.action.split("media:", 1)[1]
                asyncio.create_task(AppLauncher.media(act))
            elif tag.action.startswith("volume:"):
                direction = tag.action.split("volume:", 1)[1]
                asyncio.create_task(AppLauncher.volume(direction))
            elif tag.action.startswith("code:"):
                prompt = tag.action.split("code:", 1)[1]
                asyncio.create_task(
                    self.coder.start_task(prompt, on_complete=self._on_coder_task_completed)
                )
            elif tag.action == "code_cancel":
                asyncio.create_task(self.coder.cancel_current_task())
            elif tag.action.startswith("type:"):
                text_to_type = tag.action.split("type:", 1)[1]
                asyncio.create_task(AppLauncher.type_text(text_to_type, press_enter=False))
            elif tag.action.startswith("term:"):
                cmd_to_type = tag.action.split("term:", 1)[1]
                asyncio.create_task(AppLauncher.type_text(cmd_to_type, press_enter=True))
            elif tag.action.startswith("key:"):
                key_to_press = tag.action.split("key:", 1)[1]
                asyncio.create_task(AppLauncher.press_key(key_to_press))

    async def _dispatch_sentence_chunk(
        self,
        chunk: SentenceChunk,
        default_emo: str = "neutral",
        default_intensity: float = 1.0,
    ) -> None:
        self._utterance_counter += 1
        utt_id = f"u{self._utterance_counter}"

        primary_emo = default_emo
        intensity = default_intensity
        primary_gst = None

        for t in chunk.tags:
            if isinstance(t, EmotionTag):
                primary_emo = t.name
                intensity = t.intensity
            elif isinstance(t, GestureTag):
                primary_gst = t.name

        logger.info("[Say %s] %s (emo=%s, gst=%s)", utt_id, chunk.text, primary_emo, primary_gst)

        audio_url = None
        if self.tts:
            try:
                audio_bytes = await self.tts.synthesize(
                    chunk.text,
                    emotion=primary_emo,
                    intensity=intensity,
                )
                if audio_bytes:
                    save_audio_file(utt_id, audio_bytes)
                    audio_url = f"/audio/{utt_id}.mp3"
            except (
                aiohttp.ClientError,
                OSError,
                RuntimeError,
                ConnectionError,
                TimeoutError,
            ) as exc:
                logger.warning("TTS synthesis failed for utterance %s: %s", utt_id, exc)

        say_payload: dict[str, Any] = {
            "type": "say",
            "id": utt_id,
            "text": chunk.text,
            "emotion": primary_emo,
            "intensity": intensity,
            "gesture": primary_gst,
        }
        if audio_url:
            say_payload["audio_url"] = audio_url

        await self.ws.broadcast(say_payload)

        if self.stt and chunk.text:
            self.stt.set_last_avatar_phrase(chunk.text)

        # Direct desktop audio playback via PipeWire (guarantees voice output regardless of browser state)
        if audio_url:
            audio_path = get_audio_path(utt_id)
            if audio_path.exists():
                await self._desktop_audio_queue.put(
                    (utt_id, audio_path, primary_emo, intensity, primary_gst)
                )

    async def _desktop_audio_worker(self) -> None:
        """Sequential playback worker ensuring sentences play one by one without overlap."""
        while True:
            try:
                utt_id, audio_path, chunk_emo, chunk_intensity, chunk_gst = (
                    await self._desktop_audio_queue.get()
                )
            except asyncio.CancelledError:
                break

            try:
                if audio_path.exists():
                    self.set_state("speaking")
                    # Dynamically broadcast this specific sentence's emotion and gesture
                    await self.ws.broadcast(
                        {"type": "emotion", "name": chunk_emo, "intensity": chunk_intensity}
                    )
                    if chunk_gst:
                        await self.ws.broadcast({"type": "gesture", "name": chunk_gst})

                    await self.bus.publish(PlaybackStarted(utterance_id=utt_id))
                    proc = await asyncio.create_subprocess_exec(
                        "mpv",
                        "--no-video",
                        "--really-quiet",
                        str(audio_path),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    self._current_mpv_proc = proc
                    await proc.wait()
                    self._current_mpv_proc = None
                    await self.bus.publish(PlaybackEnded(utterance_id=utt_id))
            except asyncio.CancelledError:
                self._desktop_audio_queue.task_done()
                break
            except (OSError, RuntimeError) as exc:
                logger.debug("Desktop audio playback error: %s", exc)
            finally:
                self._desktop_audio_queue.task_done()
                if self._desktop_audio_queue.empty() and self.state == "speaking":
                    self.set_state("idle")
                    # Automatically reset to neutral expression when speech ends
                    await self.ws.broadcast(
                        {"type": "emotion", "name": "neutral", "intensity": 1.0}
                    )

    def _interrupt_desktop_audio(self) -> None:
        """Immediately stop currently playing audio and discard queued sentences."""
        while not self._desktop_audio_queue.empty():
            try:
                self._desktop_audio_queue.get_nowait()
                self._desktop_audio_queue.task_done()
            except (asyncio.QueueEmpty, ValueError):
                break
        if self._current_mpv_proc and self._current_mpv_proc.returncode is None:
            try:
                self._current_mpv_proc.terminate()
            except OSError:
                pass
            self._current_mpv_proc = None
        if self.state == "speaking":
            self.set_state("idle")
            asyncio.create_task(
                self.ws.broadcast({"type": "emotion", "name": "neutral", "intensity": 1.0})
            )

    async def _initiative_loop(self) -> None:
        """Periodic background ticker evaluating initiative and idle liveliness."""
        while True:
            try:
                await asyncio.sleep(5.0)
                await self._tick_initiative(dt=5.0)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in initiative background ticker")

    async def _tick_initiative(self, dt: float) -> None:
        """Single tick step for initiative evaluation."""
        budget_exceeded = self.llm.budget.is_exceeded()
        is_user_speaking = self.state == "listening"
        is_avatar_speaking = self.state == "speaking"
        is_llm_busy = self.state == "thinking" or (
            self._current_gen_task is not None and not self._current_gen_task.done()
        )

        active_app = self.vision.active_app if self.vision else ""
        screen_context = self.vision.screen_context if self.vision else ""

        if (
            self._pending_coder_notice
            and not is_user_speaking
            and not is_avatar_speaking
            and not is_llm_busy
        ):
            notice = self._pending_coder_notice
            self._pending_coder_notice = None
            await self.process_proactive_turn(seed=notice)
            return

        decision: InitiativeDecision = self.initiative.evaluate_tick(
            dt=dt,
            is_user_speaking=is_user_speaking,
            is_avatar_speaking=is_avatar_speaking,
            is_llm_busy=is_llm_busy,
            budget_exceeded=budget_exceeded,
            active_app=active_app,
            screen_context=screen_context,
        )

        logger.debug(
            "[Initiative Tick] Speak: %s, Reason: %s", decision.should_speak, decision.reason
        )

        if decision.should_speak:
            await self.process_proactive_turn(seed=decision.seed)
        elif decision.idle_action and self.state == "idle":
            if decision.idle_action == "look_screen":
                await self.ws.broadcast({"type": "look", "target": "screen"})
                await asyncio.sleep(2.0)
                await self.ws.broadcast({"type": "look", "target": "cursor"})
            elif decision.idle_action == "stretch":
                await self.ws.broadcast({"type": "gesture", "name": "wave"})
            elif decision.idle_action == "sigh":
                await self.ws.broadcast({"type": "emotion", "name": "relaxed", "intensity": 0.6})
