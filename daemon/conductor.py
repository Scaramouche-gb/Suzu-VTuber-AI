"""Conductor state machine coordinating LLM stream, tags, sentences and renderer."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from daemon.bus import (
    EventBus,
    Interrupt,
    PlaybackEnded,
    PlaybackStarted,
    UserCommand,
    UserSpeech,
    UserSpeechStart,
)
from daemon.llm import LLMClient
from daemon.sentence import SentenceChunk, SentenceSplitter
from daemon.tagparser import EmotionTag, GestureTag, LookTag, SilentTag, TagParser
from daemon.tts import BaseTTSEngine, save_audio_file
from daemon.ws_server import WebSocketServer

logger = logging.getLogger(__name__)


class Conductor:
    """Orchestrates dialogue turns, LLM generation, tag parsing, and avatar state."""

    def __init__(
        self,
        llm: LLMClient,
        ws_server: WebSocketServer,
        bus: EventBus,
        tts: BaseTTSEngine | None = None,
        stt: Any | None = None,
    ) -> None:
        self.llm = llm
        self.ws = ws_server
        self.bus = bus
        self.tts = tts
        self.stt = stt

        self.state = "idle"
        self.history: list[dict[str, str]] = []
        self._current_gen_task: asyncio.Task[None] | None = None
        self._utterance_counter = 0

        # Subscribe to bus events
        self.bus.subscribe(Interrupt, self.handle_interrupt)
        self.bus.subscribe(PlaybackStarted, self.handle_playback_started)
        self.bus.subscribe(PlaybackEnded, self.handle_playback_ended)
        self.bus.subscribe(UserCommand, self.handle_user_command)
        self.bus.subscribe(UserSpeechStart, self.handle_user_speech_start)
        self.bus.subscribe(UserSpeech, self.handle_user_speech)

    def set_state(self, new_state: str) -> None:
        """Update and broadcast state."""
        if self.state != new_state:
            self.state = new_state
            self.ws.set_state(new_state)
            logger.info("[State] %s", new_state)

    async def handle_interrupt(self, event: Interrupt) -> None:
        """Cancel ongoing generation and speech on user barge-in."""
        logger.info("Interrupt triggered: %s", event.reason)
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
        if event.command == "click":
            # Micro-reaction to click
            region = event.args.get("region", "body")
            if region == "head":
                await self.ws.broadcast({"type": "emotion", "name": "surprised", "intensity": 0.8, "hold_ms": 1500})
                await self.ws.broadcast({"type": "gesture", "name": "nod"})
            else:
                await self.ws.broadcast({"type": "gesture", "name": "shrug"})

    async def handle_user_speech_start(self, event: UserSpeechStart) -> None:
        """User started speaking; immediately interrupt active speech and transition to listening."""
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

        # If already generating or thinking, cancel stale task and merge user text
        if self._current_gen_task and not self._current_gen_task.done():
            logger.info("Canceling ongoing turn to process new user utterance")
            self._current_gen_task.cancel()

        self._current_gen_task = asyncio.create_task(
            self._generate_turn(user_text, context_info, image_base64, vad_stt_ms, start_ts)
        )
        await self._current_gen_task

    async def _generate_turn(
        self,
        user_text: str,
        context_info: str | None,
        image_base64: str | None,
        vad_stt_ms: float = 0.0,
        start_ts: float | None = None,
    ) -> None:
        self.set_state("thinking")
        t_turn_start = start_ts or time.time()

        messages = self.llm.build_messages(
            user_message=user_text,
            history=self.history[-12:],
            context_info=context_info,
            image_base64=image_base64,
        )

        tag_parser = TagParser()
        sentence_splitter = SentenceSplitter()
        full_reply_text: list[str] = []

        first_token_received = False
        first_audio_dispatched = False
        t_first_token = 0.0
        t_first_audio = 0.0

        try:
            async for token in self.llm.stream_response(messages):
                if not first_token_received:
                    first_token_received = True
                    t_first_token = time.time()

                # 1. Parse tags from token
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

            # Record in conversation history
            reply_str = "".join(full_reply_text).strip()
            if reply_str:
                self.history.append({"role": "user", "content": user_text})
                self.history.append({"role": "assistant", "content": reply_str})
                if self.stt:
                    self.stt.set_last_avatar_phrase(reply_str)

                if first_audio_dispatched:
                    llm_token_ms = (t_first_token - t_turn_start) * 1000.0 if t_first_token else 0.0
                    tts_audio_ms = (t_first_audio - t_first_token) * 1000.0 if (t_first_audio and t_first_token) else 0.0
                    total_ms = (t_first_audio - t_turn_start) * 1000.0
                    logger.info(
                        "[Latency Budget] VAD+STT: %.0fms | LLM 1st Token: %.0fms | TTS 1st Audio: %.0fms | Total: %.0fms",
                        vad_stt_ms,
                        llm_token_ms,
                        tts_audio_ms,
                        total_ms,
                    )

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
            await self.ws.broadcast({
                "type": "emotion",
                "name": tag.name,
                "intensity": tag.intensity,
            })
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

        # Extract primary emotion and gesture from chunk tags if present
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
            except (OSError, RuntimeError, ConnectionError, TimeoutError) as exc:
                logger.warning("TTS synthesis failed for utterance %s: %s", utt_id, exc)

        # Notify renderer with say command and optional audio_url
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
