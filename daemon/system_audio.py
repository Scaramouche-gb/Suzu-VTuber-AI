"""Desktop system audio capture via PipeWire pw-record, Silero VAD, and echo suppression."""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from collections import deque
from typing import Any

import numpy as np
import torch

from daemon.bus import EventBus, PlaybackEnded, PlaybackStarted, SystemSpeech
from daemon.config import SystemAudioConfig

logger = logging.getLogger(__name__)

FRAME_SIZE = 512  # 32ms @ 16kHz
SAMPLE_RATE = 16000
FRAME_BYTES = FRAME_SIZE * 2  # s16le = 2 bytes per sample (1024 bytes)


class SystemAudioInManager:
    """Captures desktop/system output audio (Discord, Telegram, media) and emits SystemSpeech."""

    def __init__(self, cfg: SystemAudioConfig, bus: EventBus) -> None:
        self.cfg = cfg
        self.bus = bus

        self._vad_model: Any = None
        self._is_speaking_tts = False
        self._playback_end_ts = 0.0
        self._running = False
        self._proc: asyncio.subprocess.Process | None = None
        self._worker_task: asyncio.Task[None] | None = None

        # Pre-roll ring buffer (350ms = ~11 frames)
        self._preroll_frames_count = max(
            1, int((cfg.preroll_ms / 1000.0) * (SAMPLE_RATE / FRAME_SIZE))
        )
        self._preroll_buffer: deque[np.ndarray] = deque(maxlen=self._preroll_frames_count)

        # Echo rejection via bus events: ignore audio while Suzu speaks
        self.bus.subscribe(PlaybackStarted, self._on_playback_started)
        self.bus.subscribe(PlaybackEnded, self._on_playback_ended)

    async def _on_playback_started(self, event: PlaybackStarted) -> None:
        self._is_speaking_tts = True
        self._preroll_buffer.clear()

    async def _on_playback_ended(self, event: PlaybackEnded) -> None:
        self._playback_end_ts = time.time()
        self._preroll_buffer.clear()
        # Absorb speaker room reverberation and audio driver buffer drain
        await asyncio.sleep(0.45)
        self._is_speaking_tts = False

    def load_vad_model(self) -> Any:
        """Lazy load Silero VAD model."""
        if self._vad_model is None:
            logger.info("[SystemAudio] Loading Silero VAD model...")
            try:
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
            logger.info("[SystemAudio] Silero VAD model ready")
        return self._vad_model

    @staticmethod
    async def get_default_sink_target() -> str:
        """Query pactl for the active default sink name."""
        if shutil.which("pactl"):
            try:
                proc = await asyncio.create_subprocess_exec(
                    "pactl",
                    "get-default-sink",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=1.0)
                sink_name = stdout.decode().strip()
                if sink_name:
                    return sink_name
            except (OSError, TimeoutError):
                pass
        return "alsa_output.pci-0000_05_00.6.analog-stereo"

    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Start pw-record subprocess and VAD processing loop."""
        if self._running or not self.cfg.enabled:
            return

        if not shutil.which("pw-record"):
            logger.warning("[SystemAudio] pw-record not found, system audio capture disabled")
            return

        self._running = True
        self.load_vad_model()
        self._worker_task = asyncio.create_task(self._capture_loop())
        logger.info("[SystemAudio] Capture worker task spawned")

    async def stop(self) -> None:
        """Stop system audio capture."""
        self._running = False
        if self._proc:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=1.0)
            except (OSError, TimeoutError, ProcessLookupError):
                pass
            self._proc = None

        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            self._worker_task = None
        logger.info("[SystemAudio] Capture stopped")

    async def _capture_loop(self) -> None:
        """Run pw-record and stream chunks through Silero VAD with echo suppression."""
        target_sink = self.cfg.device or await self.get_default_sink_target()
        cmd = [
            "pw-record",
            "--target",
            target_sink,
            "--format",
            "s16",
            "--rate",
            str(SAMPLE_RATE),
            "--channels",
            "1",
            "-",
        ]

        logger.info("[SystemAudio] Starting pw-record on target '%s'", target_sink)

        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except (OSError, RuntimeError) as exc:
            logger.error("[SystemAudio] Failed to launch pw-record: %s", exc)
            self._running = False
            return

        stdout = self._proc.stdout
        if not stdout:
            logger.error("[SystemAudio] No stdout pipe for pw-record")
            self._running = False
            return

        is_in_speech = False
        consecutive_speech_frames = 0
        silence_duration_ms = 0.0
        active_speech_frames = 0
        speech_frames: list[np.ndarray] = []

        consecutive_required = 3
        frame_duration_ms = (FRAME_SIZE / SAMPLE_RATE) * 1000.0  # 32 ms

        while self._running:
            try:
                raw_chunk = await stdout.readexactly(FRAME_BYTES)
            except (asyncio.IncompleteReadError, asyncio.CancelledError):
                break
            except (OSError, RuntimeError) as exc:
                logger.debug("[SystemAudio] Read error: %s", exc)
                break

            # 1. Echo suppression: if Suzu is currently speaking in mpv, ignore everything
            now = time.time()
            if self._is_speaking_tts or (now - self._playback_end_ts < 0.45):
                self._preroll_buffer.clear()
                is_in_speech = False
                speech_frames.clear()
                consecutive_speech_frames = 0
                active_speech_frames = 0
                continue

            # Convert s16le raw bytes to float32 normalized in [-1.0, 1.0]
            int_arr = np.frombuffer(raw_chunk, dtype=np.int16)
            frame = int_arr.astype(np.float32) / 32768.0

            frame_rms = float(np.sqrt(np.mean(frame**2)))
            min_rms = 0.005  # Audible system voice threshold

            tensor_frame = torch.from_numpy(frame)
            try:
                prob = self._vad_model(tensor_frame, SAMPLE_RATE).item()
            except (RuntimeError, ValueError):
                prob = 0.0

            is_valid_speech = (prob >= self.cfg.vad_threshold) and (frame_rms >= min_rms)

            if not is_in_speech:
                self._preroll_buffer.append(frame)
                if is_valid_speech:
                    consecutive_speech_frames += 1
                else:
                    consecutive_speech_frames = 0

                if consecutive_speech_frames >= consecutive_required:
                    is_in_speech = True
                    consecutive_speech_frames = 0
                    silence_duration_ms = 0.0
                    active_speech_frames = consecutive_required
                    speech_frames = list(self._preroll_buffer)
                    self._preroll_buffer.clear()
                    logger.debug(
                        "[SystemAudio] VAD speech started in system audio (prob=%.2f, rms=%.4f)",
                        prob,
                        frame_rms,
                    )
            else:
                speech_frames.append(frame)
                if is_valid_speech:
                    active_speech_frames += 1
                    silence_duration_ms = 0.0
                else:
                    silence_duration_ms += frame_duration_ms

                current_dur_ms = len(speech_frames) * frame_duration_ms
                if (
                    silence_duration_ms >= self.cfg.end_silence_ms
                    or current_dur_ms >= 25000.0  # max 25s phrase
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

        min_speech_s = self.cfg.min_speech_ms / 1000.0
        if duration_s < min_speech_s:
            logger.debug("[SystemAudio] Phrase too short (%.2fs < %.2fs)", duration_s, min_speech_s)
            return

        active_speech_s = (active_frames * FRAME_SIZE) / SAMPLE_RATE
        if active_speech_s < 0.25:
            logger.debug("[SystemAudio] Low active speech density (%.2fs)", active_speech_s)
            return

        rms = float(np.sqrt(np.mean(total_audio**2)))
        if rms < 0.006:
            logger.debug("[SystemAudio] Low RMS energy (%.4f)", rms)
            return

        logger.info(
            "[SystemAudio] Captured system voice utterance (duration: %.2fs, rms: %.4f)",
            duration_s,
            rms,
        )
        await self.bus.publish(SystemSpeech(audio_data=total_audio, duration_s=duration_s))
