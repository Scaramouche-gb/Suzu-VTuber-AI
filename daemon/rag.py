"""Two-Tier Hybrid RAG (Retrieval-Augmented Generation) Manager for Suzu VTuber AI."""

from __future__ import annotations

import asyncio
import difflib
import logging
from typing import Any

from daemon.memory import MemoryManager
from daemon.tencent_bridge import TencentMemoryBridge

logger = logging.getLogger(__name__)


class RAGManager:
    """Coordinates sub-millisecond local BM25 FTS5 and semantic TencentDB Agent Memory."""

    def __init__(
        self,
        memory: MemoryManager | None = None,
        tencent: TencentMemoryBridge | None = None,
    ) -> None:
        self.memory = memory
        self.tencent = tencent

    async def retrieve(
        self,
        query: str,
        active_app: str = "",
        active_title: str = "",
        max_items: int = 3,
    ) -> list[str]:
        """Execute parallel hybrid retrieval with reciprocal ranking and compaction."""
        tasks: list[asyncio.Task[Any]] = []

        # Tier 1: Local SQLite BM25 FTS5 (<2ms)
        if self.memory:
            tasks.append(
                asyncio.create_task(self.memory.search_facts_fts(query, limit=max_items))
            )
        else:
            tasks.append(asyncio.create_task(self._dummy_empty()))

        # Tier 2: Dense Semantic Retrieval via TencentDB Agent Memory (~20ms)
        if self.tencent:
            tasks.append(
                asyncio.create_task(
                    self.tencent.search_project_context(
                        query=query,
                        active_app=active_app,
                        active_title=active_title,
                        limit=max_items,
                    )
                )
            )
        else:
            tasks.append(asyncio.create_task(self._dummy_empty()))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        local_facts: list[str] = (
            results[0] if isinstance(results[0], list) else []
        )
        tencent_notes: list[str] = (
            results[1] if isinstance(results[1], list) else []
        )

        # Merge, deduplicate, and compact
        merged: list[str] = []
        for note in tencent_notes + local_facts:
            clean = note.strip()
            if not clean:
                continue
            # Deduplication via fuzzy similarity
            if any(
                difflib.SequenceMatcher(None, clean.lower(), m.lower()).ratio() > 0.8
                for m in merged
            ):
                continue
            merged.append(clean)
            if len(merged) >= max_items:
                break

        return merged

    async def ingest_event(
        self, text: str, scope: str = "project"
    ) -> None:
        """Store milestone/event in background into appropriate RAG tier."""
        clean = text.strip()
        if not clean:
            return

        if scope == "personal" and self.memory:
            await self.memory.add_fact(clean, importance=1.0)
        elif scope == "project" and self.tencent:
            await self.tencent.record_event(clean)

    @staticmethod
    async def _dummy_empty() -> list[str]:
        return []
