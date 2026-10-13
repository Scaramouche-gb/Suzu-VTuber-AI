"""Unit tests for Two-Tier Hybrid RAG Manager and SQLite FTS5."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from daemon.memory import MemoryManager
from daemon.rag import RAGManager
from daemon.tencent_bridge import TencentMemoryBridge


@pytest.mark.asyncio
async def test_sqlite_fts5_indexing_and_search(tmp_path) -> None:
    db_file = tmp_path / "test_rag_fts.db"
    mem = MemoryManager(db_path=str(db_file))

    # Ingest facts
    await mem.add_fact("Любит писать код на Rust и Python", importance=0.9)
    await mem.add_fact("Любит пить чай эрл грей по утрам", importance=0.8)
    await mem.add_fact("Разрабатывает Suzu-VTuber-AI на driftwm и quickshell", importance=1.0)

    # Search with FTS5 BM25
    rust_results = await mem.search_facts_fts("Rust")
    assert len(rust_results) == 1
    assert "Rust" in rust_results[0]

    tea_results = await mem.search_facts_fts("чай")
    assert len(tea_results) == 1
    assert "эрл грей" in tea_results[0]

    drift_results = await mem.search_facts_fts("driftwm")
    assert len(drift_results) == 1
    assert "quickshell" in drift_results[0]

    # Non-existent search query
    empty_results = await mem.search_facts_fts("несуществующее_слово_xyz")
    assert len(empty_results) == 0

    await mem.close()


@pytest.mark.asyncio
async def test_rag_manager_tier1_tier2_hybrid() -> None:
    mock_memory = MagicMock(spec=MemoryManager)
    mock_memory.search_facts_fts = AsyncMock(
        return_value=["Любит пить чай эрл грей", "Любит Python"]
    )

    mock_tencent = MagicMock(spec=TencentMemoryBridge)
    mock_tencent.search_project_context = AsyncMock(
        return_value=[
            "Архитектура Suzu-VTuber-AI использует driftwm и aiohttp",
            "Любит пить чай эрл грей",  # duplicate from Tier 1
        ]
    )

    rag = RAGManager(memory=mock_memory, tencent=mock_tencent)
    results = await rag.retrieve("чай driftwm", max_items=3)

    assert len(results) <= 3
    # Check deduplication
    assert len([r for r in results if "эрл грей" in r]) == 1
    # Check that project notes were retrieved
    assert any("driftwm" in r for r in results)
    assert any("Python" in r for r in results)


@pytest.mark.asyncio
async def test_rag_manager_ingest_event() -> None:
    mock_memory = MagicMock(spec=MemoryManager)
    mock_memory.add_fact = AsyncMock()

    mock_tencent = MagicMock(spec=TencentMemoryBridge)
    mock_tencent.record_event = AsyncMock()

    rag = RAGManager(memory=mock_memory, tencent=mock_tencent)

    # Personal event -> Memory
    await rag.ingest_event("Пользователь ушел спать в 05:00", scope="personal")
    mock_memory.add_fact.assert_awaited_once_with("Пользователь ушел спать в 05:00", importance=1.0)
    mock_tencent.record_event.assert_not_called()

    # Project milestone -> TencentDB
    await rag.ingest_event("Смержен PR #42 в репозиторий", scope="project")
    mock_tencent.record_event.assert_awaited_once_with("Смержен PR #42 в репозиторий")

    # Empty text -> No-op
    await rag.ingest_event("   ", scope="personal")
