"""Microphone input capture, Silero VAD with hysteresis, pre-roll buffer and barge-in."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any

import numpy as np
import sounddevice as sd
import torch

from daemon.bus import EventBus, PlaybackEnded, PlaybackStarted, UserSpeech, UserSpeechStart
from daemon.config import AudioInConfig

logger = logging.getLogger(__name__)

FRAME_SIZE = 512  # 32ms @ 16kHz
SAMPLE_RATE = 16000


class AudioInManager:
    """Captures microphone audio, applies Silero VAD and emits speech events."""

    def __init__(self, cfg: AudioInConfig, bus: EventBus) -> None:
        self.cfg = cfg
        self.bus = bus
        self.mode = "always"  # "always" | "push-to-talk" | "muted"
        self.ptt_active = False

        self._vad_model: Any = None
        self._is_speaking_tts = False
        self._playback_start_ts = 0.0
        self._running = False
        self._stream: sd.InputStream | None = None
        self._queue: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=64)
        self._process_task: asyncio.Task[None] | None = None

        # Pre-roll ring buffer: 300ms = ~10 frames of 512 samples
        self._preroll_frames_count = max(
            1, int((cfg.preroll_ms / 1000.0) * (SAMPLE_RATE / FRAME_SIZE))
        )
        self._preroll_buffer: deque[np.ndarray] = deque(maxlen=self._preroll_frames_count)

        # Bus subscriptions for echo threshold adaptation
        self.bus.subscribe(PlaybackStarted, self._on_playback_started)
        self.bus.subscribe(PlaybackEnded, self._on_playback_ended)

    async def _on_playback_started(self, event: PlaybackStarted) -> None:
        self._is_speaking_tts = True
        self._playback_start_ts = time.time()

    async def _on_playback_ended(self, event: PlaybackEnded) -> None:
        self._preroll_buffer.clear()
        # Absorb speaker room reverberation and audio driver latency
        await asyncio.sleep(0.55)
        self._is_speaking_tts = False

    def load_vad_model(self) -> Any:
        """Lazy load Silero VAD model."""
        if self._vad_model is None:
            logger.info("Loading Silero VAD model...")
            try:
                # Silero VAD via torch hub or silero_vad package
                from silero_vad import load_silero_vad

                self._vad_model = load_silero_vad()
            except ImportError:
                model, _ = torch.hub.load(
                    repo_or_dir="snakers4/silero-vad",
                    model="silero_vad",
                    force_reload=False,
                    trust_repo=True,
                )
                self._vad_model = model
            logger.info("Silero VAD model loaded successfully")
        return self._vad_model

    def set_mode(self, mode: str) -> None:
        """Change listening mode: always | push-to-talk | muted."""
        self.mode = mode
        logger.info("Audio input mode set to '%s'", mode)

    def set_push_to_talk(self, active: bool) -> None:
        self.ptt_active = active

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Start sounddevice audio stream and processing loop."""
        if self._running:
            return

        self._running = True
        self.load_vad_model()

        def audio_callback(
            indata: np.ndarray, frames: int, time_info: Any, status: sd.CallbackFlags
        ) -> None:
            if status:
                logger.warning("sounddevice status: %s", status)
            # Copy float32 audio frame to bounded queue, dropping oldest if queue is congested
            frame = indata[:, 0].copy()

            def _enqueue() -> None:
                if self._queue.full():
                    try:
                        self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                try:
                    self._queue.put_nowait(frame)
                except asyncio.QueueFull:
                    pass

            loop.call_soon_threadsafe(_enqueue)

        try:
            device = self.cfg.device if self.cfg.device else None
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                blocksize=FRAME_SIZE,
                dtype="float32",
                device=device,
                callback=audio_callback,
            )
            self._stream.start()
            logger.info(
                "Audio capture started (device: %s, sample_rate: %d)",
                device or "default",
                SAMPLE_RATE,
            )
            self._process_task = asyncio.create_task(self._vad_loop())
        except (sd.PortAudioError, OSError, RuntimeError) as exc:
            logger.error("Failed to start sounddevice InputStream: %s", exc)
            self._running = False

    async def stop(self) -> None:
        """Stop audio capture."""
        self._running = False
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except (sd.PortAudioError, OSError) as exc:
                logger.debug("Error stopping audio stream: %s", exc)
            self._stream = None

        if self._process_task and not self._process_task.done():
            self._process_task.cancel()
            self._process_task = None
        logger.info("Audio capture stopped")

    async def _vad_loop(self) -> None:
        """VAD processing loop with hysteresis, pre-roll and speech accumulation."""
        is_in_speech = False
        consecutive_speech_frames = 0
        silence_duration_ms = 0.0
        active_speech_frames = 0
        speech_frames: list[np.ndarray] = []

        consecutive_required = 3
        frame_duration_ms = (FRAME_SIZE / SAMPLE_RATE) * 1000.0  # 32 ms

        while self._running:
            try:
                frame = await self._queue.get()
            except asyncio.CancelledError:
                break

            # Handle muted and push-to-talk modes
            if self.mode == "muted":
                self._preroll_buffer.clear()
                is_in_speech = False
                active_speech_frames = 0
                continue

            if self.mode == "push-to-talk" and not self.ptt_active:
                self._preroll_buffer.clear()
                if is_in_speech:
                    # User released PTT, finish phrase
                    await self._finish_phrase(speech_frames, active_speech_frames)
                    speech_frames = []
                    is_in_speech = False
                    active_speech_frames = 0
                continue

            # Calculate RMS energy of current frame to detect voice intensity vs quiet background
            frame_rms = float(np.sqrt(np.mean(frame**2)))

            # Compute threshold and required frames (much stricter during TTS speech for barge-in)
            base_threshold = self.cfg.vad_threshold
            if self._is_speaking_tts:
                threshold = 0.95
                required_frames = 12  # ~384ms of sustained intentional voice
                min_rms = 0.018  # Must be audible spoken voice, not quiet keystrokes/creaks
            else:
                threshold = base_threshold
                required_frames = consecutive_required
                min_rms = 0.0035

            # Evaluate VAD probability
            tensor_frame = torch.from_numpy(frame)
            try:
                prob = self._vad_model(tensor_frame, SAMPLE_RATE).item()
            except (RuntimeError, ValueError):
                prob = 0.0

            # Check initial immunity window during playback start to absorb speaker/DAC transients
            in_playback_grace = self._is_speaking_tts and (
                time.time() - self._playback_start_ts < 0.35
            )
            is_valid_speech_frame = (
                (prob >= threshold) and (frame_rms >= min_rms) and not in_playback_grace
            )

            if not is_in_speech:
                if self._is_speaking_tts:
                    self._preroll_buffer.clear()
                else:
                    self._preroll_buffer.append(frame)

                if is_valid_speech_frame:
                    consecutive_speech_frames += 1
                else:
                    consecutive_speech_frames = 0

                if consecutive_speech_frames >= required_frames:
                    # Voice activity detected!
                    is_in_speech = True
                    consecutive_speech_frames = 0
                    silence_duration_ms = 0.0
                    active_speech_frames = required_frames
                    speech_frames = list(self._preroll_buffer)
                    self._preroll_buffer.clear()

                    logger.debug(
                        "VAD: User speech started (prob=%.2f, rms=%.4f, tts_active=%s)",
                        prob,
                        frame_rms,
                        self._is_speaking_tts,
                    )
                    await self.bus.publish(UserSpeechStart())

            else:
                speech_frames.append(frame)

                if (prob >= threshold) and (frame_rms >= (min_rms * 0.7)):
                    active_speech_frames += 1
                    silence_duration_ms = 0.0
                else:
                    silence_duration_ms += frame_duration_ms

                current_total_duration_ms = len(speech_frames) * frame_duration_ms

                # Check if silence threshold exceeded OR max phrase duration (30s) reached
                if (
                    silence_duration_ms >= self.cfg.end_silence_ms
                    or current_total_duration_ms >= 30000.0
                ):
                    await self._finish_phrase(speech_frames, active_speech_frames)
                    speech_frames = []
                    is_in_speech = False
                    active_speech_frames = 0
                    silence_duration_ms = 0.0

    async def _finish_phrase(self, frames: list[np.ndarray], active_frames: int = 0) -> None:
        if not frames:
            return

        total_audio = np.concatenate(frames)
        duration_s = len(total_audio) / SAMPLE_RATE

        # Reject short clicks (< min_speech_ms)
        min_speech_s = self.cfg.min_speech_ms / 1000.0
        if duration_s < min_speech_s:
            logger.debug("VAD: Rejected brief click (%.2fs < %.2fs)", duration_s, min_speech_s)
            return

        # Reject transient noise spikes (fewer than ~224ms of active speech)
        active_speech_s = (active_frames * FRAME_SIZE) / SAMPLE_RATE
        if active_speech_s < 0.22:
            logger.debug(
                "VAD: Rejected transient noise (active speech %.2fs < 0.22s)", active_speech_s
            )
            return

        # Reject isolated sound impulses with low voice energy ratio
        speech_ratio = active_frames / max(1, len(frames))
        if speech_ratio < 0.18:
            logger.debug(
                "VAD: Rejected low-density noise spike (ratio=%.2f < 0.18)", speech_ratio
            )
            return

        # Reject quiet ambient room noise / breathing (RMS threshold)
        rms = float(np.sqrt(np.mean(total_audio**2)))
        if rms < 0.013:
            logger.debug("VAD: Rejected quiet ambient noise (rms=%.4f < 0.0130)", rms)
            return

        logger.info("VAD: Captured user phrase (duration: %.2fs, rms: %.4f)", duration_s, rms)
        await self.bus.publish(UserSpeech(audio_data=total_audio, duration_s=duration_s))
