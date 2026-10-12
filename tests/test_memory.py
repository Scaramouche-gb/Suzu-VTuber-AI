"""Unit tests for Phase 8 SQLite persistent memory and sanitization."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from daemon.bus import EventBus
from daemon.conductor import Conductor
from daemon.config import LLMConfig
from daemon.llm import LLMClient
from daemon.memory import MemoryManager, sanitize_sensitive_data
from daemon.ws_server import WebSocketServer


def test_sanitize_sensitive_data() -> None:
    # Credit card
    text_card = "Моя карта 4276 1234 5678 9012, запиши"
    assert "[REDACTED_CARD]" in sanitize_sensitive_data(text_card)
    assert "4276" not in sanitize_sensitive_data(text_card)

    # API key
    text_key = "Вот ключ sk-1234567890abcdefghijklmnopqrstuvwxyz для теста"
    assert "[REDACTED_KEY]" in sanitize_sensitive_data(text_key)
    assert "sk-" not in sanitize_sensitive_data(text_key)

    # Password
    text_pwd = "Мой пароль: MySuperSecretPass123!"
    sanitized = sanitize_sensitive_data(text_pwd)
    assert "[REDACTED]" in sanitized
    assert "MySuperSecretPass123!" not in sanitized


@pytest.mark.asyncio
async def test_memory_crud_operations(tmp_path) -> None:
    db_file = tmp_path / "test_memory.db"
    mem = MemoryManager(db_path=str(db_file), context_turns=5, summarize_every=10)

    # 1. Record messages
    msg1_id = await mem.record_message("user", "Привет, я люблю чай эрл грей")
    msg2_id = await mem.record_message("assistant", "Запомнила, эрл грей!")
    assert msg1_id > 0
    assert msg2_id > 0

    recent = await mem.get_recent_messages(limit=2)
    assert len(recent) == 2
    assert recent[0]["role"] == "user"
    assert recent[1]["role"] == "assistant"

    # 2. Facts
    f1_id = await mem.add_fact("Любит чай эрл грей", importance=0.9)
    assert f1_id > 0
    facts = await mem.get_facts(limit=10)
    assert len(facts) == 1
    assert facts[0].text == "Любит чай эрл грей"

    # Delete fact
    deleted = await mem.delete_fact(facts[0].id)
    assert deleted is True
    facts_after = await mem.get_facts(limit=10)
    assert len(facts_after) == 0

    # 3. Episodes
    ep_id = await mem.add_episode(100.0, 200.0, "Обсудили предпочтения в чае.")
    assert ep_id > 0
    episodes = await mem.get_recent_episodes(limit=5)
    assert len(episodes) == 1
    assert episodes[0].summary == "Обсудили предпочтения в чае."

    # 4. Export and Wipe
    export_data = await mem.export_memory()
    assert len(export_data["messages"]) == 2
    assert len(export_data["episodes"]) == 1

    await mem.wipe_memory()
    assert len(await mem.get_recent_messages()) == 0
    assert len(await mem.get_facts()) == 0
    assert len(await mem.get_recent_episodes()) == 0

    await mem.close()


@pytest.mark.asyncio
async def test_conductor_memory_integration(tmp_path) -> None:
    db_file = tmp_path / "test_conductor_mem.db"
    mem = MemoryManager(db_path=str(db_file))
    await mem.add_fact("Любит Arch Linux", importance=1.0)

    bus = EventBus()
    llm = LLMClient(LLMConfig(), dry_run=True)
    ws = MagicMock(spec=WebSocketServer)
    ws.broadcast = AsyncMock()
    ws.set_state = MagicMock()

    conductor = Conductor(
        llm=llm,
        ws_server=ws,
        bus=bus,
        memory=mem,
    )

    await conductor.process_user_turn("Привет Сузу!")

    # Check that messages got stored into memory
    recent_msgs = await mem.get_recent_messages()
    assert len(recent_msgs) >= 2
    assert recent_msgs[0]["role"] == "user"
    assert recent_msgs[1]["role"] == "assistant"

    await conductor.close()
