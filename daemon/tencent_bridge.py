"""Bridge between Suzu AI-VTuber and TencentDB Agent Memory."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx
from tencentdb_agent_memory import AsyncMemoryClient
from tencentdb_agent_memory.errors import TDAMError

logger = logging.getLogger(__name__)

DEFAULT_ADMIN_KEY_FILE = (
    Path.home() / ".local/share/tencentdb-agent-memory/deploy/global-images/.admin-key"
)
DEFAULT_ENDPOINT = os.getenv("TENCENT_MEMORY_ENDPOINT", "http://127.0.0.1:8420")
DEFAULT_SERVICE_ID = os.getenv("TENCENT_MEMORY_SERVICE_ID", "default")
NETWORK_ERRORS = (httpx.HTTPError, TDAMError, RuntimeError, ValueError, KeyError, OSError)


def get_tencent_api_key() -> str:
    env_key = os.getenv("TENCENT_MEMORY_API_KEY")
    if env_key:
        return env_key
    if DEFAULT_ADMIN_KEY_FILE.exists():
        try:
            return DEFAULT_ADMIN_KEY_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return "default-key"


class TencentMemoryBridge:
    """Provides semantic retrieval of user project knowledge and event recording."""

    def __init__(
        self, endpoint: str = DEFAULT_ENDPOINT, service_id: str = DEFAULT_SERVICE_ID
    ) -> None:
        self.endpoint = endpoint
        self.service_id = service_id
        self._client: AsyncMemoryClient | None = None
        self._available = False

    async def initialize(self) -> bool:
        """Initialize connection to TencentDB Agent Memory."""
        try:
            api_key = get_tencent_api_key()
            self._client = AsyncMemoryClient(
                endpoint=self.endpoint,
                api_key=api_key,
                service_id=self.service_id,
            )
            self._available = True
            logger.info("[TencentDB] Successfully connected to Agent Memory at %s", self.endpoint)
            return True
        except NETWORK_ERRORS as exc:
            logger.warning("[TencentDB] Could not connect to Agent Memory: %s", exc)
            self._available = False
            return False

    async def search_project_context(
        self, query: str, active_app: str = "", active_title: str = "", limit: int = 3
    ) -> list[str]:
        """Retrieve relevant project architecture notes from TencentDB Memory."""
        if not self._client or not self._available:
            return []

        search_query = query.strip()
        # If query is short or general, derive topic from active window
        if len(search_query) < 4:
            combined = f"{active_app} {active_title}".strip()
            if "nachzehrer" in combined.lower():
                search_query = "Nachzehrer"
            elif "shadowgram" in combined.lower():
                search_query = "ShadowGram"
            elif "quickshell" in combined.lower() or "driftwm" in combined.lower():
                search_query = "quickshell driftwm Launchpad"
            elif "suzu" in combined.lower() or "vtuber" in combined.lower():
                search_query = "Suzu-VTuber-AI"
            else:
                search_query = combined or "architecture"

        try:
            res = await self._client.search_conversation(query=search_query, limit=limit)
            messages = res.get("messages", [])
            results: list[str] = []
            for msg in messages:
                score = msg.get("score", 0.0)
                content = msg.get("content", "").strip()
                if (
                    score >= 0.65
                    and content
                    and content != "Acknowledged and stored in memory."
                ):
                    results.append(content)
            return results
        except NETWORK_ERRORS as exc:
            logger.debug("[TencentDB] Search failed: %s", exc)
            return []

    async def record_event(
        self, event_text: str, session_id: str = "suzu-vtuber"
    ) -> bool:
        """Store a high-level project/conversation milestone into TencentDB Agent Memory."""
        if not self._client or not self._available:
            return False

        try:
            await self._client.add_conversation(
                session_id=session_id,
                messages=[
                    {"role": "user", "content": event_text.strip()},
                    {
                        "role": "assistant",
                        "content": "Acknowledged and stored in memory.",
                    },
                ],
            )
            logger.info("[TencentDB] Ingested event: '%s'", event_text[:80])
            return True
        except NETWORK_ERRORS as exc:
            logger.warning("[TencentDB] Failed to record event: %s", exc)
            return False

    async def close(self) -> None:
        if self._client:
            try:
                await self._client.close()
            except NETWORK_ERRORS as exc:
                logger.debug("[TencentDB] Error closing client: %s", exc)
            self._client = None
