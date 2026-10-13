"""Unit tests for TTS engine, pronunciation dictionary, and audio cache."""

import time
from pathlib import Path

import pytest

from daemon.bus import EventBus
from daemon.conductor import Conductor
from daemon.config import Config, TTSConfig
from daemon.llm import LLMClient
from daemon.tts import (
    EdgeTTSEngine,
    MockTTSEngine,
    PronunciationDictionary,
    cleanup_audio_cache,
    save_audio_file,
)
from daemon.ws_server import WebSocketServer


def test_pronunciation_dictionary() -> None:
    pronounce = PronunciationDictionary(Path("pronounce.toml"))
    sample = "Я запустила Linux и открыла VS Code, чтобы проверить GPU."
    processed = pronounce.apply(sample)
    assert "линукс" in processed.lower()
    assert "ви эс код" in processed.lower()
    assert "джи пи ю" in processed.lower()


@pytest.mark.asyncio
async def test_mock_tts_synthesize() -> None:
    engine = MockTTSEngine()
    audio = await engine.synthesize("Привет мир!", emotion="happy", intensity=0.9)
    assert isinstance(audio, bytes)
    assert len(audio) > 0


def test_edge_tts_emotion_modulation() -> None:
    cfg = TTSConfig(voice="ru-RU-SvetlanaNeural", rate="+5%", pitch="+2Hz")
    engine = EdgeTTSEngine(cfg)

    happy_rate, happy_pitch = engine._get_emotion_modulation("happy", 1.0)
    assert "+" in happy_rate
    assert "+" in happy_pitch

    sad_rate, sad_pitch = engine._get_emotion_modulation("sad", 1.0)
    assert "-" in sad_rate
    assert "-" in sad_pitch


def test_audio_cache_save_and_cleanup(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("daemon.tts.AUDIO_CACHE_DIR", tmp_path)
    file_path = save_audio_file("test_u1", b"fake-audio-bytes")
    assert file_path.exists()
    assert file_path.read_bytes() == b"fake-audio-bytes"

    # Set file modification time to 2 hours ago
    old_time = time.time() - 7200
    file_path.touch()
    import os
    os.utime(file_path, (old_time, old_time))

    cleaned = cleanup_audio_cache(max_age_s=3600)
    assert cleaned == 1
    assert not file_path.exists()


@pytest.mark.asyncio
async def test_conductor_with_tts_audio_dispatch() -> None:
    cfg = Config()
    bus = EventBus()

    class MockWS(WebSocketServer):
        def __init__(self) -> None:
            super().__init__(cfg, bus)
            self.broadcasted: list[dict] = []

        async def broadcast(self, message: dict) -> None:
            self.broadcasted.append(message)

    ws = MockWS()
    llm = LLMClient(cfg.llm, dry_run=True)
    tts = MockTTSEngine()
    conductor = Conductor(llm=llm, ws_server=ws, bus=bus, tts=tts)

    await conductor.process_user_turn("Привет Сузу")

    say_msgs = [m for m in ws.broadcasted if m.get("type") == "say"]
    assert len(say_msgs) > 0
    for say in say_msgs:
        assert "audio_url" in say
        assert say["audio_url"].startswith("/audio/")


@pytest.mark.asyncio
async def test_rvc_disabled() -> None:
    from daemon.config import RVCConfig
    from daemon.rvc import RVCVoiceConverter

    converter = RVCVoiceConverter(RVCConfig(enabled=False))
    result = await converter.convert(b"test-audio-bytes")
    assert result == b"test-audio-bytes"
    await converter.close()


@pytest.mark.asyncio
async def test_rvc_sidecar_fallback_on_unreachable() -> None:
    from daemon.config import RVCConfig
    from daemon.rvc import RVCVoiceConverter

    converter = RVCVoiceConverter(
        RVCConfig(enabled=True, mode="sidecar", server_url="http://127.0.0.1:59999/infer")
    )
    result = await converter.convert(b"test-audio-bytes")
    # Gracefully falls back to original audio
    assert result == b"test-audio-bytes"
    await converter.close()


def test_build_silero_ssml() -> None:
    from daemon.tts import build_silero_ssml

    ssml_whisper = build_silero_ssml("Хе-хе, я шёпотом скажу.", emotion="whisper")
    assert "<speak>" in ssml_whisper
    assert '<prosody rate="medium" pitch="high">' in ssml_whisper
    assert '<break time="110ms"/>' in ssml_whisper

    ssml_excited = build_silero_ssml("Ого! Вот это да!", emotion="excited")
    assert '<prosody rate="fast" pitch="x-high">' in ssml_excited


def test_apply_emotion_mastering_empty() -> None:
    from daemon.tts import apply_emotion_mastering

    assert apply_emotion_mastering(b"", "whisper") == b""


@pytest.mark.asyncio
async def test_xtts_engine_creation_and_fallback() -> None:
    from daemon.config import XTTSConfig
    from daemon.tts import XTTSEngine, create_tts_engine

    cfg = TTSConfig(
        engine="xtts",
        xtts=XTTSConfig(server_url="http://127.0.0.1:59998/synthesize"),
    )
    engine = create_tts_engine(cfg)
    assert isinstance(engine, XTTSEngine)

    # Calling an unreachable endpoint returns empty bytes gracefully
    res = await engine.synthesize("Привет, это тестовый голос")
    assert res == b""

