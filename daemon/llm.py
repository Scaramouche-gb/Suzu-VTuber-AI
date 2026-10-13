"""LiteLLM streaming client with fallback, prompt assembly, and budget tracking."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any

import litellm

litellm.drop_params = True

from daemon.config import LLMConfig

logger = logging.getLogger(__name__)

PERSONA_DIR = Path("persona")


class BudgetTracker:
    """Tracks daily spending on LLM API calls."""

    def __init__(self, max_usd_per_day: float = 3.0) -> None:
        self.max_usd_per_day = max_usd_per_day
        self.current_day = datetime.now().date()
        self.spent_usd = 0.0
        self.budget_exceeded = False

    @property
    def daily_spent(self) -> float:
        return self.spent_usd

    def is_exceeded(self) -> bool:
        return self.budget_exceeded

    def is_budget_exceeded(self) -> bool:
        return self.budget_exceeded

    def add_cost(self, cost: float) -> None:
        today = datetime.now().date()
        if today != self.current_day:
            self.current_day = today
            self.spent_usd = 0.0
            self.budget_exceeded = False

        self.spent_usd += cost
        logger.info(
            "LLM call cost: $%.5f (Daily total: $%.4f / $%.2f)",
            cost,
            self.spent_usd,
            self.max_usd_per_day,
        )

        if self.spent_usd >= self.max_usd_per_day and not self.budget_exceeded:
            self.budget_exceeded = True
            logger.warning(
                "Daily LLM budget of $%.2f exceeded! Restricting background tasks.",
                self.max_usd_per_day,
            )


class LLMClient:
    """Handles LLM stream generation, fallbacks, and prompt construction."""

    def __init__(self, cfg: LLMConfig, dry_run: bool = False) -> None:
        self.cfg = cfg
        self.dry_run = dry_run
        self.budget = BudgetTracker(max_usd_per_day=cfg.max_usd_per_day)
        self.persona_text = self._load_file(PERSONA_DIR / "persona.md")
        self.rules_text = self._load_file(PERSONA_DIR / "rules.md")

    @staticmethod
    def _load_file(path: Path) -> str:
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        return ""

    def build_messages(
        self,
        user_message: str,
        history: list[dict[str, str]] | None = None,
        context_info: str | None = None,
        facts: list[str] | None = None,
        episodes: list[str] | None = None,
        image_base64: str | None = None,
        project_notes: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Assemble system prompt, history, context, and user replica."""
        messages: list[dict[str, Any]] = []

        # System message: persona + rules + facts + episodes + project knowledge
        system_parts = [self.persona_text, self.rules_text]
        if facts:
            facts_block = "\n## ФАКТЫ О ПОЛЬЗОВАТЕЛЕ:\n" + "\n".join(f"- {f}" for f in facts[:20])
            system_parts.append(facts_block)
        if project_notes:
            project_block = "\n## БАЗА ЗНАНИЙ ПРОЕКТОВ И КОДА (TencentDB):\n" + "\n".join(
                f"• {p}" for p in project_notes[:3]
            )
            system_parts.append(project_block)
        if episodes:
            episodes_block = "\n## ЗАМЕТКИ ПРОШЛЫХ РАЗГОВОРОВ:\n" + "\n".join(
                f"- {e}" for e in episodes[:5]
            )
            system_parts.append(episodes_block)

        messages.append({"role": "system", "content": "\n\n".join(p for p in system_parts if p)})

        # History turns
        if history:
            messages.extend(history)

        # Context block
        now_str = datetime.now().strftime("%H:%M, %A")
        context_prefix = f"КОНТЕКСТ: время {now_str}"
        if context_info:
            context_prefix += f"; {context_info}"

        user_content_text = f"{context_prefix}\n----\nРЕПЛИКА: «{user_message}»"

        if image_base64:
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_content_text},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"},
                        },
                    ],
                }
            )
        else:
            messages.append({"role": "user", "content": user_content_text})

        return messages

    async def stream_response(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        timeout_first_token: float = 8.0,
    ) -> AsyncIterator[str]:
        """Stream response tokens with fallback on failure or first-token timeout."""
        if self.dry_run:
            async for token in self._mock_stream():
                yield token
            return

        target_model = model or self.cfg.chat_model

        # Attempt primary model
        try:
            async for chunk in self._attempt_stream(target_model, messages, timeout_first_token):
                yield chunk
            return
        except (
            litellm.exceptions.APIError,
            TimeoutError,
            ConnectionError,
            OSError,
            RuntimeError,
            ValueError,
        ) as exc:
            logger.warning(
                "Primary LLM (%s) failed: %s. Switching to fallback (%s)",
                target_model,
                exc,
                self.cfg.fallback_model,
            )

        # Attempt fallback model
        try:
            async for chunk in self._attempt_stream(
                self.cfg.fallback_model, messages, timeout_first_token=12.0
            ):
                yield chunk
        except (
            litellm.exceptions.APIError,
            TimeoutError,
            ConnectionError,
            OSError,
            RuntimeError,
            ValueError,
        ) as exc:
            logger.error("Fallback LLM (%s) also failed: %s", self.cfg.fallback_model, exc)
            yield "<emo=sad> Ой, у меня что-то с сетью. Не могу сейчас ответить."

    async def _attempt_stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        timeout_first_token: float,
    ) -> AsyncIterator[str]:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": self.cfg.max_tokens,
            "stream": True,
            "num_retries": 0,
        }
        if "claude" not in model.lower() and self.cfg.temperature != 1.0:
            kwargs["temperature"] = self.cfg.temperature

        # Bypass slow multi-second reasoning thinking phases on compatible models for instant TTFT (~1s)
        if any(m in model.lower() for m in ("gpt-5", "gpt-6", "sol", "luna", "terra")):
            kwargs["extra_body"] = {"reasoning_effort": "none"}

        api_base = getattr(self.cfg, "api_base", "") or os.environ.get("OPENAI_API_BASE", "")
        if api_base:
            kwargs["api_base"] = api_base

        api_key = getattr(self.cfg, "api_key", "") or os.environ.get("OPENAI_API_KEY", "")
        if api_key:
            kwargs["api_key"] = api_key

        response = await litellm.acompletion(**kwargs)

        first_token_received = False
        full_completion_content: list[str] = []

        try:
            async for chunk in response:
                if not first_token_received:
                    first_token_received = True

                delta = chunk.choices[0].delta.content or ""
                if delta:
                    full_completion_content.append(delta)
                    yield delta
        except TimeoutError:
            if not first_token_received:
                raise TimeoutError(f"Model {model} timed out waiting for first token") from None
            raise

        # Track usage / cost
        try:
            cost = litellm.completion_cost(completion_response="".join(full_completion_content))
            if cost:
                self.budget.add_cost(cost)
        except (ValueError, TypeError, LookupError) as cost_exc:
            logger.debug("Cost tracking skipped: %s", cost_exc)

    async def _mock_stream(self) -> AsyncIterator[str]:
        """Simulate realistic token streaming for local testing / dry-run."""
        mock_chunks = [
            "<emo=happy:0.8>",
            " Привет!",
            " <gst=wave>",
            " Я здесь,",
            " готова",
            " поболтать.",
            " <look=screen>",
            " Что у нас",
            " новенького?",
        ]
        for token in mock_chunks:
            await asyncio.sleep(0.04)
            yield token
