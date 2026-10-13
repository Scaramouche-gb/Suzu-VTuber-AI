"""Retrieval-based Voice Conversion (RVC v2) pipeline and sidecar client."""

from __future__ import annotations

import logging

import aiohttp

from daemon.config import RVCConfig

logger = logging.getLogger(__name__)


class RVCVoiceConverter:
    """Voice converter that transforms TTS output into authentic anime character timbre."""

    def __init__(self, cfg: RVCConfig) -> None:
        self.cfg = cfg
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=4.0)
            )
        return self._session

    async def convert(self, audio_bytes: bytes, f0_up_key: int | None = None) -> bytes:
        """Convert input audio bytes (MP3/WAV) using RVC model or HTTP sidecar."""
        if not self.cfg.enabled or not audio_bytes:
            return audio_bytes

        pitch = f0_up_key if f0_up_key is not None else self.cfg.f0_up_key

        if self.cfg.mode == "sidecar":
            return await self._convert_sidecar(audio_bytes, pitch)

        return audio_bytes

    async def _convert_sidecar(self, audio_bytes: bytes, pitch: int) -> bytes:
        """Call local RVC HTTP sidecar (e.g., http://127.0.0.1:5050/infer)."""
        try:
            session = await self._get_session()
            data = aiohttp.FormData()
            data.add_field(
                "audio",
                audio_bytes,
                filename="input.mp3",
                content_type="audio/mpeg",
            )
            data.add_field("f0_up_key", str(pitch))
            if self.cfg.model_path:
                data.add_field("model", self.cfg.model_path)
            if self.cfg.index_path:
                data.add_field("index", self.cfg.index_path)

            async with session.post(self.cfg.server_url, data=data) as resp:
                if resp.status == 200:
                    converted = await resp.read()
                    if converted:
                        logger.debug("[RVC] Converted %d bytes audio via sidecar", len(converted))
                        return converted
                logger.warning(
                    "[RVC] Sidecar returned status %d, falling back to base TTS",
                    resp.status,
                )
        except (TimeoutError, aiohttp.ClientError, ConnectionRefusedError, OSError) as exc:
            logger.debug("[RVC] Sidecar conversion unavailable (%s), using base TTS", exc)

        return audio_bytes

    async def close(self) -> None:
        """Close client session."""
        if self._session and not self._session.closed:
            await self._session.close()
