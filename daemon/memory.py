"""Persistent SQLite memory management for Suzu VTuber AI."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

# Regex patterns for sensitive data redaction
RE_CREDIT_CARD = re.compile(r"\b(?:\d[ -]*?){13,16}\b")
RE_API_KEY = re.compile(r"\b(?:sk-[a-zA-Z0-9_\-]{20,}|ghp_[a-zA-Z0-9]{20,}|Bearer\s+[a-zA-Z0-9_\-\.]{20,})\b")
RE_PASSWORD = re.compile(r"(?i)\b(?:password|passwd|пароль)\s*[:=]\s*(\S+)")


def sanitize_sensitive_data(text: str) -> str:
    """Filter out sensitive information (credit card numbers, API keys, passwords)."""
    if not text:
        return ""
    text = RE_CREDIT_CARD.sub("[REDACTED_CARD]", text)
    text = RE_API_KEY.sub("[REDACTED_KEY]", text)
    text = RE_PASSWORD.sub(r"пароль: [REDACTED]", text)
    return text


@dataclass(slots=True)
class FactRecord:
    id: int
    ts: float
    text: str
    importance: float
    last_used: float


@dataclass(slots=True)
class EpisodeRecord:
    id: int
    ts_from: float
    ts_to: float
    summary: str


class MemoryManager:
    """Manages conversational memory, fact extraction, and episodes using SQLite."""

    def __init__(
        self,
        db_path: str = "~/.local/share/vtuber/memory.db",
        context_turns: int = 12,
        summarize_every: int = 20,
    ) -> None:
        self.db_path = Path(db_path).expanduser()
        self.context_turns = context_turns
        self.summarize_every = summarize_every
        self._unsummarized_turns = 0
        self._conn: aiosqlite.Connection | None = None

    async def _get_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = await aiosqlite.connect(str(self.db_path))
            await self._init_schema(self._conn)
        return self._conn

    async def _init_schema(self, conn: aiosqlite.Connection) -> None:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                role TEXT NOT NULL,
                text TEXT NOT NULL,
                kind TEXT NOT NULL
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                text TEXT NOT NULL UNIQUE,
                importance REAL NOT NULL DEFAULT 1.0,
                last_used REAL NOT NULL
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_from REAL NOT NULL,
                ts_to REAL NOT NULL,
                summary TEXT NOT NULL
            );
        """)
        await conn.commit()

    async def close(self) -> None:
        """Close SQLite database connection."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def record_message(self, role: str, text: str, kind: str = "chat") -> int:
        """Store a sanitized message record into the messages table."""
        clean_text = sanitize_sensitive_data(text.strip())
        if not clean_text:
            return -1

        conn = await self._get_conn()
        now = time.time()
        cursor = await conn.execute(
            "INSERT INTO messages (ts, role, text, kind) VALUES (?, ?, ?, ?)",
            (now, role, clean_text, kind),
        )
        await conn.commit()
        msg_id = cursor.lastrowid or 0
        self._unsummarized_turns += 1
        return msg_id

    async def get_recent_messages(self, limit: int | None = None) -> list[dict[str, str]]:
        """Retrieve recent dialogue messages formatted for LLM conversation history."""
        num_turns = limit or self.context_turns
        conn = await self._get_conn()
        async with conn.execute(
            "SELECT role, text FROM messages ORDER BY id DESC LIMIT ?",
            (num_turns,),
        ) as cursor:
            rows = await cursor.fetchall()

        # Reverse to chronological order
        result: list[dict[str, str]] = []
        for role, text in reversed(rows):
            result.append({"role": role, "content": text})
        return result

    async def add_fact(self, text: str, importance: float = 1.0) -> int:
        """Add or update a user fact."""
        clean_text = sanitize_sensitive_data(text.strip())
        if not clean_text:
            return -1

        conn = await self._get_conn()
        now = time.time()
        cursor = await conn.execute(
            """
            INSERT INTO facts (ts, text, importance, last_used)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(text) DO UPDATE SET
                importance = excluded.importance,
                last_used = excluded.last_used
            """,
            (now, clean_text, importance, now),
        )
        await conn.commit()
        return cursor.lastrowid or 0

    async def get_facts(self, limit: int = 20) -> list[FactRecord]:
        """Fetch top facts sorted by importance and recency."""
        conn = await self._get_conn()
        async with conn.execute(
            "SELECT id, ts, text, importance, last_used FROM facts ORDER BY importance DESC, last_used DESC LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()

        return [
            FactRecord(
                id=r[0],
                ts=r[1],
                text=r[2],
                importance=r[3],
                last_used=r[4],
            )
            for r in rows
        ]

    async def delete_fact(self, fact_id: int) -> bool:
        """Delete fact by ID."""
        conn = await self._get_conn()
        cursor = await conn.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
        await conn.commit()
        return (cursor.rowcount or 0) > 0

    async def list_facts(self) -> list[dict[str, Any]]:
        """List all facts as dictionaries for CLI / UI."""
        facts = await self.get_facts(limit=100)
        return [
            {
                "id": f.id,
                "text": f.text,
                "importance": f.importance,
                "last_used": f.last_used,
            }
            for f in facts
        ]

    async def add_episode(self, ts_from: float, ts_to: float, summary: str) -> int:
        """Save an episodic summary of a conversation interval."""
        conn = await self._get_conn()
        cursor = await conn.execute(
            "INSERT INTO episodes (ts_from, ts_to, summary) VALUES (?, ?, ?)",
            (ts_from, ts_to, summary.strip()),
        )
        await conn.commit()
        return cursor.lastrowid or 0

    async def get_recent_episodes(self, limit: int = 5) -> list[EpisodeRecord]:
        """Fetch recent episodic summaries."""
        conn = await self._get_conn()
        async with conn.execute(
            "SELECT id, ts_from, ts_to, summary FROM episodes ORDER BY id DESC LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()

        return [
            EpisodeRecord(
                id=r[0],
                ts_from=r[1],
                ts_to=r[2],
                summary=r[3],
            )
            for r in reversed(rows)
        ]

    async def wipe_memory(self) -> None:
        """Erase all messages, facts, and episodes."""
        conn = await self._get_conn()
        await conn.execute("DELETE FROM messages;")
        await conn.execute("DELETE FROM facts;")
        await conn.execute("DELETE FROM episodes;")
        await conn.commit()
        self._unsummarized_turns = 0
        logger.info("[Memory] Persistent database wiped")

    async def export_memory(self) -> dict[str, Any]:
        """Export full memory dump as JSON-serializable dictionary."""
        conn = await self._get_conn()
        async with conn.execute("SELECT id, ts, role, text, kind FROM messages") as cur:
            messages = [
                {"id": r[0], "ts": r[1], "role": r[2], "text": r[3], "kind": r[4]}
                for r in await cur.fetchall()
            ]

        async with conn.execute("SELECT id, ts, text, importance, last_used FROM facts") as cur:
            facts = [
                {"id": r[0], "ts": r[1], "text": r[2], "importance": r[3], "last_used": r[4]}
                for r in await cur.fetchall()
            ]

        async with conn.execute("SELECT id, ts_from, ts_to, summary FROM episodes") as cur:
            episodes = [
                {"id": r[0], "ts_from": r[1], "ts_to": r[2], "summary": r[3]}
                for r in await cur.fetchall()
            ]

        return {
            "messages": messages,
            "facts": facts,
            "episodes": episodes,
        }

    async def maybe_summarize(self, llm_client: Any) -> None:
        """Trigger fact and episodic extraction when turn threshold is exceeded."""
        if self._unsummarized_turns < self.summarize_every:
            return

        conn = await self._get_conn()
        async with conn.execute(
            "SELECT id, ts, role, text FROM messages ORDER BY id DESC LIMIT ?",
            (self.summarize_every,),
        ) as cur:
            recent_rows = await cur.fetchall()

        if not recent_rows:
            return

        recent_rows.reverse()
        ts_from = recent_rows[0][1]
        ts_to = recent_rows[-1][1]

        conversation_text = "\n".join(f"{r[2]}: {r[3]}" for r in recent_rows)
        existing_facts = await self.get_facts(limit=20)
        existing_facts_str = "; ".join(f.text for f in existing_facts) or "нет"

        prompt = (
            "Проанализируй диалог и выдели новые факты о пользователе, а также составь краткую сводку.\n"
            f"Текущие факты: [{existing_facts_str}]\n\n"
            f"Диалог:\n{conversation_text}\n\n"
            "Ответь ТОЛЬКО в формате JSON:\n"
            "{\n"
            '  "summary": "краткая сводка диалога (2-3 предложения)",\n'
            '  "facts": ["факт 1", "факт 2"]\n'
            "}"
        )

        try:
            full_reply: list[str] = []
            messages = [{"role": "user", "content": prompt}]
            async for token in llm_client.stream_response(messages, model=llm_client.cfg.fast_model):
                full_reply.append(token)

            raw_json = "".join(full_reply).strip()
            # Clean possible markdown fence
            if raw_json.startswith("```"):
                raw_json = re.sub(r"^```(?:json)?\n?", "", raw_json)
                raw_json = re.sub(r"\n?```$", "", raw_json)

            data = json.loads(raw_json)
            summary = data.get("summary", "")
            if summary:
                await self.add_episode(ts_from, ts_to, summary)
                logger.info("[Memory] Added episode summary: %s", summary)

            new_facts = data.get("facts", [])
            for fact_text in new_facts:
                if isinstance(fact_text, str) and fact_text.strip():
                    await self.add_fact(fact_text.strip())
                    logger.info("[Memory] Extracted new user fact: %s", fact_text.strip())

            self._unsummarized_turns = 0

        except (RuntimeError, ValueError, OSError, TimeoutError, json.JSONDecodeError, KeyError) as exc:
            logger.warning("[Memory] Summarization extraction failed: %s", exc)
