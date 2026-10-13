"""Unit tests for SystemAudioInManager and Conductor system speech handling."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from daemon.bus import EventBus, PlaybackEnded, PlaybackStarted, SystemSpeech
from daemon.conductor import Conductor
from daemon.config import Config, SystemAudioConfig
from daemon.llm import LLMClient
from daemon.system_audio import SystemAudioInManager


@pytest.mark.asyncio
async def test_system_audio_manager_echo_suppression() -> None:
    bus = EventBus()
    cfg = SystemAudioConfig(enabled=True)
    manager = SystemAudioInManager(cfg, bus)

    # Initial state
    assert manager._is_speaking_tts is False

    # When avatar playback starts
    await bus.publish(PlaybackStarted())
    assert manager._is_speaking_tts is True

    # When avatar playback ends
    await bus.publish(PlaybackEnded())
    await asyncio.sleep(0.5)
    assert manager._is_speaking_tts is False


@pytest.mark.asyncio
async def test_conductor_handle_system_speech() -> None:
    bus = EventBus()
    cfg = Config()
    llm = LLMClient(cfg.llm, dry_run=True)
    ws_mock = AsyncMock()
    stt_mock = MagicMock()
    stt_mock.transcribe.return_value = "Саня, погнали играть в доту!"

    conductor = Conductor(
        llm=llm,
        ws_server=ws_mock,
        bus=bus,
        stt=stt_mock,
    )

    speech_event = SystemSpeech(
        audio_data=np.zeros(16000, dtype=np.float32),
        duration_s=1.0,
    )

    await conductor.handle_system_speech(speech_event)
    await asyncio.sleep(0.1)

    # Conductor should have invoked transcription or generated turn
    stt_mock.transcribe.assert_called_once()
    await conductor.close()
