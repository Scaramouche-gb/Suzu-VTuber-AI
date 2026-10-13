"""Unit tests for TelegramBridge, MediaGenerator and Conductor Telegram integration."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from daemon.bus import EventBus
from daemon.conductor import Conductor
from daemon.config import TelegramConfig
from daemon.media_generator import MediaGenerator
from daemon.tagparser import CommandTag
from daemon.telegram_bridge import TelegramBridge


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def tg_config() -> TelegramConfig:
    return TelegramConfig(
        enabled=True,
        mode="both",
        bot_token="123456:TEST_TOKEN",
        default_chat_id=987654321,
        api_id=12345,
        api_hash="testhash",
        session_name="test_session",
        auto_reply=True,
    )


def test_media_generator_voice_ffmpeg_command() -> None:
    """Verify MediaGenerator invokes ffmpeg with Telegram voip Opus specifications."""
    with patch("subprocess.run") as mock_run:
        mock_proc = MagicMock()
        mock_run.return_value = mock_proc

        out_path = Path("/tmp/test_voice.ogg")
        dummy_audio = b"\x00\x01" * 100

        with patch.object(Path, "stat") as mock_stat:
            mock_stat.return_value.st_size = 200
            result = MediaGenerator.encode_voice_note(dummy_audio, output_path=out_path)

        assert result == out_path
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "ffmpeg" in cmd
        assert "libopus" in cmd
        assert "48000" in cmd
        assert "voip" in cmd


def test_media_generator_round_ffmpeg_command() -> None:
    """Verify MediaGenerator invokes ffmpeg with 1:1 Baseline H.264 specifications."""
    with patch("subprocess.run") as mock_run:
        mock_proc = MagicMock()
        mock_run.return_value = mock_proc

        out_path = Path("/tmp/test_round.mp4")
        dummy_video = b"\x1aE\xdf\xa3" * 50
        dummy_audio = b"\xff\xfb\x90" * 50

        with patch.object(Path, "stat") as mock_stat:
            mock_stat.return_value.st_size = 500
            result = MediaGenerator.encode_round_video(
                dummy_video, audio_bytes=dummy_audio, output_path=out_path, dimension=384
            )

        assert result == out_path
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "ffmpeg" in cmd
        assert "libx264" in cmd
        assert "baseline" in cmd
        assert "crop=384:384" in "".join(cmd)
        assert "+faststart" in cmd


@pytest.mark.asyncio
async def test_telegram_bridge_resolve_target_default(bus: EventBus, tg_config: TelegramConfig) -> None:
    """Verify default chat_id is used when no specific active chat is found."""
    bridge = TelegramBridge(tg_config, bus)
    target = await bridge.resolve_target()
    assert target == 987654321


@pytest.mark.asyncio
async def test_telegram_bridge_resolve_target_vision(bus: EventBus, tg_config: TelegramConfig) -> None:
    """Verify dialog name matching against vision active window title."""
    mock_vision = MagicMock()
    mock_vision.active_app = "org.telegram.desktop"
    mock_vision.active_title = "Alex — Telegram"

    bridge = TelegramBridge(tg_config, bus, vision=mock_vision)
    bridge._telethon_client = AsyncMock()
    bridge._telethon_client.is_user_authorized.return_value = True

    mock_dialog = MagicMock()
    mock_dialog.name = "Alex"
    mock_dialog.entity = 11223344
    bridge._telethon_client.get_dialogs.return_value = [mock_dialog]

    target = await bridge.resolve_target()
    assert target == 11223344


@pytest.mark.asyncio
async def test_telegram_bot_send_message(bus: EventBus, tg_config: TelegramConfig) -> None:
    """Verify Bot API sends message with correct JSON payload."""
    bridge = TelegramBridge(tg_config, bus)
    mock_session = MagicMock()
    mock_resp = AsyncMock()
    mock_resp.json = AsyncMock(return_value={"ok": True, "result": {"message_id": 1}})
    mock_session.post.return_value.__aenter__.return_value = mock_resp
    bridge._http_session = mock_session

    success = await bridge.send_text("Привет!", target=987654321)
    assert success is True
    mock_session.post.assert_called_once()
    url = mock_session.post.call_args[0][0]
    assert "123456:TEST_TOKEN/sendMessage" in url
    payload = mock_session.post.call_args[1]["json"]
    assert payload["chat_id"] == 987654321
    assert payload["text"] == "Привет!"


@pytest.mark.asyncio
async def test_conductor_telegram_command_tags(bus: EventBus, tg_config: TelegramConfig) -> None:
    """Verify Conductor triggers voice, round, and text handlers on <cmd=tg_*> tags."""
    mock_llm = MagicMock()
    mock_ws = MagicMock()
    mock_ws.broadcast = AsyncMock()
    mock_tts = MagicMock()
    mock_tts.synthesize = AsyncMock(return_value=b"fake_mp3_data")

    bridge = TelegramBridge(tg_config, bus)
    bridge.send_text = AsyncMock(return_value=True)
    bridge.send_voice = AsyncMock(return_value=True)
    bridge.send_round_video = AsyncMock(return_value=True)

    conductor = Conductor(
        llm=mock_llm,
        ws_server=mock_ws,
        bus=bus,
        tts=mock_tts,
        telegram=bridge,
    )

    # Test tg_reply
    await conductor._handle_tag(CommandTag(action="tg_reply:Привет из авто-ответа"))
    await asyncio.sleep(0.02)
    bridge.send_text.assert_called_with("Привет из авто-ответа")

    # Test tg_voice
    await conductor._handle_tag(CommandTag(action="tg_voice:Привет голосом"))
    await asyncio.sleep(0.02)
    bridge.send_voice.assert_called()

    await conductor.close()


@pytest.mark.asyncio
async def test_conductor_direct_tg_speech_intents(bus: EventBus, tg_config: TelegramConfig) -> None:
    """Verify spoken intents like 'запиши голосовое в телеграм' trigger Telegram tasks."""
    mock_llm = MagicMock()
    mock_llm.budget.is_exceeded.return_value = False
    mock_llm.generate_stream = AsyncMock(return_value=iter([]))
    mock_ws = MagicMock()
    mock_ws.broadcast = AsyncMock()
    mock_ws.request_round_recording = AsyncMock(return_value=b"fake_webm")
    mock_tts = MagicMock()
    mock_tts.synthesize = AsyncMock(return_value=b"fake_audio")

    bridge = TelegramBridge(tg_config, bus)
    bridge.send_voice = AsyncMock(return_value=True)
    bridge.send_round_video = AsyncMock(return_value=True)

    conductor = Conductor(
        llm=mock_llm,
        ws_server=mock_ws,
        bus=bus,
        tts=mock_tts,
        telegram=bridge,
    )

    # Voice intent
    await conductor.process_user_turn("Сузу, отправь голосовое в телеграм: я уже в пути")
    await asyncio.sleep(0.05)
    bridge.send_voice.assert_called()

    # Round video intent
    await conductor.process_user_turn("Сузу, запиши кружок в телеграм: всем привет")
    await asyncio.sleep(0.05)
    bridge.send_round_video.assert_called()

    await conductor.close()
