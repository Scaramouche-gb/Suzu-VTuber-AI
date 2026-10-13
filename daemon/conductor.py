"""Conductor state machine coordinating LLM stream, tags, sentences, initiative and renderer."""

from __future__ import annotations

import asyncio
import base64
import datetime
import logging
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
from daemon.initiative import InitiativeDecision, InitiativeManager
from daemon.llm import LLMClient
from daemon.memory import MemoryManager
from daemon.rag import RAGManager
from daemon.sentence import SentenceChunk, SentenceSplitter
from daemon.tagparser import EmotionTag, GestureTag, LookTag, SilentTag, TagParser
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

        self.state = "idle"
        self.history: list[dict[str, str]] = []
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
        if self.memory:
            await self.memory.close()
        if self.tencent:
            await self.tencent.close()

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

        proactive_user_prompt = (
            f"КОНТЕКСТ: время {now_str}, активное приложение/окно: {active_app}, "
            f"состояние экрана: {screen_desc}, молчим: {silence_min} мин, "
            f"твоё настроение: {self.initiative.mood.value}.\n"
            f"Последние твои реплики: [{recent_inits}] (НЕ ПОВТОРЯЙСЯ).\n"
            f"Тема: {seed}.\n"
            f"ЗАДАЧА: Сделай живой, забавный, язвительный или заботливый комментарий (1-2 коротких предложения вслух) "
            f"о том, что Макс сейчас делает за компом (Zed, код, терминал, браузер, ночь), либо подколи или спроси что-то интересное. "
            f"Обязательно ставь теги эмоций и жестов в начале (например, <emo=playful:0.9><gst=nod>). Если момент совсем неподходящий, выведи <silent>."
        )

        self._current_gen_task = asyncio.create_task(
            self._generate_turn(
                user_text=proactive_user_prompt,
                context_info=None,
                image_base64=None,
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

        first_token_received = False
        first_audio_dispatched = False
        t_first_token = 0.0
        t_first_audio = 0.0

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
                            await self._dispatch_sentence_chunk(chunk)
                            if not first_audio_dispatched:
                                first_audio_dispatched = True
                                t_first_audio = time.time()
                    else:
                        await self._handle_tag(item, sentence_splitter)

            # Flush remaining buffers
            remaining_tags = tag_parser.flush()
            for r_tag in remaining_tags:
                if isinstance(r_tag, str):
                    full_reply_text.append(r_tag)
                    ready_chunks = sentence_splitter.feed(r_tag)
                    for chunk in ready_chunks:
                        await self._dispatch_sentence_chunk(chunk)

            final_chunks = sentence_splitter.flush()
            for chunk in final_chunks:
                await self._dispatch_sentence_chunk(chunk)

            reply_str = "".join(full_reply_text).strip()
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

    async def _dispatch_sentence_chunk(self, chunk: SentenceChunk) -> None:
        self._utterance_counter += 1
        utt_id = f"u{self._utterance_counter}"

        primary_emo = "neutral"
        intensity = 1.0
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
                await self._desktop_audio_queue.put((utt_id, audio_path))

    async def _desktop_audio_worker(self) -> None:
        """Sequential playback worker ensuring sentences play one by one without overlap."""
        while True:
            try:
                utt_id, audio_path = await self._desktop_audio_queue.get()
            except asyncio.CancelledError:
                break

            try:
                if audio_path.exists():
                    self.set_state("speaking")
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
