"""Initiative and liveliness manager for Suzu VTuber AI."""

from __future__ import annotations

import datetime
import logging
import math
import random
import time
from collections import deque
from dataclasses import dataclass
from enum import StrEnum

from daemon.config import InitiativeConfig

logger = logging.getLogger(__name__)


class Mood(StrEnum):
    PLAYFUL = "playful"
    CURIOUS = "curious"
    SLEEPY = "sleepy"
    SASSY = "sassy"
    CARING = "caring"


MOOD_FACTORS: dict[Mood, float] = {
    Mood.PLAYFUL: 0.8,
    Mood.CURIOUS: 0.9,
    Mood.CARING: 1.0,
    Mood.SASSY: 1.1,
    Mood.SLEEPY: 2.2,
}

INITIATIVE_SEEDS: list[str] = [
    "tease_screen",
    "critique_code_or_bugs",
    "tech_banter_or_linux",
    "geek_curiosity",
    "joke_or_banter",
    "roast_active_window",
    "cyber_lifestyle_joke",
    "recall_project_milestone",
]


@dataclass(slots=True)
class InitiativeDecision:
    """Represents the outcome of an initiative evaluation tick."""

    should_speak: bool
    reason: str
    seed: str = ""
    idle_action: str = ""


class InitiativeManager:
    """Manages proactive conversation triggers, Poisson hazard rate, and liveliness."""

    def __init__(self, config: InitiativeConfig | None = None) -> None:
        self.config = config or InitiativeConfig()
        self.mood: Mood = Mood.PLAYFUL
        self.energy: float = 1.0  # 0.0 .. 1.0
        self.ignored_count: int = 0
        self.dnd: bool = False
        self.muted: bool = False

        now = time.time()
        self.last_user_speech_ts: float = now
        self.last_avatar_speech_ts: float = now
        self.last_initiative_speech_ts: float = 0.0

        self.last_app: str = ""
        self.last_app_comment_ts: dict[str, float] = {}
        self.recent_initiatives: list[str] = []
        self.recent_seeds: deque[str] = deque(maxlen=4)

        self.last_mood_switch_ts: float = now
        self.mood_switch_interval_s: float = 1800.0  # 30 minutes

    def on_user_speech_start(self) -> None:
        """User started speaking; resets ignored count and refreshes silence timer."""
        self.ignored_count = 0
        self.last_user_speech_ts = time.time()
        logger.debug("[Initiative] User spoke, ignored count reset to 0")

    def on_avatar_spoken(self, is_proactive: bool = False, text: str = "") -> None:
        """Avatar spoke a phrase."""
        now = time.time()
        self.last_avatar_speech_ts = now
        if is_proactive:
            self.last_initiative_speech_ts = now
            self.ignored_count += 1
            self.energy = max(0.0, self.energy - 0.15)
            if text:
                self.recent_initiatives.append(text)
                if len(self.recent_initiatives) > 5:
                    self.recent_initiatives.pop(0)
            logger.info(
                "[Initiative] Avatar proactive phrase completed. Ignored count: %d, Energy: %.2f",
                self.ignored_count,
                self.energy,
            )
        else:
            self.ignored_count = 0
            self.energy = max(0.0, self.energy - 0.05)

    def set_mood(self, mood: Mood | str) -> None:
        """Explicitly switch avatar mood."""
        self.mood = Mood(mood)
        self.last_mood_switch_ts = time.time()
        logger.info("[Initiative] Mood manually set to %s", self.mood.value)

    def set_dnd(self, enabled: bool) -> None:
        """Toggle Do Not Disturb mode."""
        self.dnd = enabled
        logger.info("[Initiative] DND set to %s", enabled)

    def set_muted(self, enabled: bool) -> None:
        """Toggle microphone/speech mute mode."""
        self.muted = enabled
        logger.info("[Initiative] Muted set to %s", enabled)

    def is_in_quiet_hours(self, now_dt: datetime.datetime | None = None) -> bool:
        """Check whether current time falls within configured quiet hours."""
        if not self.config.quiet_hours:
            return False

        current = now_dt or datetime.datetime.now()
        current_time = current.time()

        for interval in self.config.quiet_hours:
            parts = interval.split("-")
            if len(parts) != 2:
                continue
            try:
                start_h, start_m = map(int, parts[0].strip().split(":"))
                end_h, end_m = map(int, parts[1].strip().split(":"))
                t_start = datetime.time(start_h, start_m)
                t_end = datetime.time(end_h, end_m)

                if t_start <= t_end:
                    if t_start <= current_time <= t_end:
                        return True
                else:
                    # Spans across midnight, e.g. 23:00 - 08:00
                    if current_time >= t_start or current_time <= t_end:
                        return True
            except ValueError:
                continue

        return False

    def calculate_hazard_rate(self, dt: float) -> tuple[float, float]:
        """Calculate Poisson hazard rate probability p_tick for step dt."""
        backoff_mult = self.config.backoff_factor**self.ignored_count
        max_mult = self.config.max_backoff_s / max(1.0, self.config.mean_interval_s)
        backoff = min(backoff_mult, max_mult)

        mood_factor = MOOD_FACTORS.get(self.mood, 1.0)
        energy_factor = max(0.2, self.energy)

        current_mean_interval = (
            self.config.mean_interval_s * backoff * mood_factor
        ) / energy_factor
        lam = 1.0 / max(1.0, current_mean_interval)
        p_tick = 1.0 - math.exp(-lam * dt)

        return p_tick, current_mean_interval

    def _maybe_rotate_mood(self) -> None:
        """Periodically rotate mood in idle intervals."""
        now = time.time()
        if now - self.last_mood_switch_ts >= self.mood_switch_interval_s:
            choices = list(Mood)
            self.mood = random.choice(choices)
            self.last_mood_switch_ts = now
            logger.info("[Initiative] Mood rotated to %s", self.mood.value)

    def evaluate_tick(
        self,
        dt: float,
        is_user_speaking: bool,
        is_avatar_speaking: bool,
        is_llm_busy: bool,
        budget_exceeded: bool,
        active_app: str = "",
        screen_context: str = "",
    ) -> InitiativeDecision:
        """Evaluate if avatar should proactively speak or execute idle action."""
        now = time.time()

        # Recharge energy slowly in silence
        self.energy = min(1.0, self.energy + 0.05 * (dt / 60.0))
        self._maybe_rotate_mood()

        # Hard prohibitions
        if not self.config.enabled:
            return InitiativeDecision(False, "initiative_disabled")

        if self.dnd:
            return InitiativeDecision(False, "dnd_enabled")

        if self.muted:
            return InitiativeDecision(False, "muted")

        if is_user_speaking:
            return InitiativeDecision(False, "user_is_speaking")

        if is_avatar_speaking:
            return InitiativeDecision(False, "avatar_is_speaking")

        if is_llm_busy:
            return InitiativeDecision(False, "llm_is_busy")

        if budget_exceeded:
            return InitiativeDecision(False, "budget_exceeded")

        if self.is_in_quiet_hours():
            return InitiativeDecision(False, "quiet_hours")

        since_speech = now - self.last_avatar_speech_ts
        if since_speech < self.config.min_gap_s:
            return InitiativeDecision(
                False,
                f"min_gap_not_reached ({since_speech:.1f}s < {self.config.min_gap_s}s)",
            )

        # Trigger on interesting window switch
        if active_app and active_app != self.last_app:
            self.last_app = active_app
            self.ignored_count = 0
            last_comment = self.last_app_comment_ts.get(active_app, 0.0)
            if (now - last_comment) > 240.0:  # 240s cooldown per app
                self.last_app_comment_ts[active_app] = now
                seed = "tease_screen"
                reason = f"app_switch_trigger ({active_app})"
                logger.info("[Initiative] Triggered proactive turn: %s", reason)
                return InitiativeDecision(True, reason, seed=seed)

        # Poisson hazard rate decision
        p_tick, current_interval = self.calculate_hazard_rate(dt)
        roll = random.random()

        if roll < p_tick:
            available_seeds = (
                [s for s in INITIATIVE_SEEDS if s not in self.recent_seeds] or INITIATIVE_SEEDS
            )
            seed = random.choice(available_seeds)
            self.recent_seeds.append(seed)
            reason = (
                f"hazard_prob_hit (p={p_tick:.4f}, mean={current_interval:.1f}s, roll={roll:.4f})"
            )
            logger.info("[Initiative] Triggered proactive turn: %s, seed=%s", reason, seed)
            return InitiativeDecision(True, reason, seed=seed)

        # Silent liveliness reaction check (cheap, no LLM)
        idle_action = ""
        if random.random() < 0.15:  # ~15% chance during tick
            idle_action = random.choice(["stretch", "look_screen", "sigh"])

        reason = f"hazard_miss (p={p_tick:.4f}, mean={current_interval:.1f}s, roll={roll:.4f})"
        return InitiativeDecision(False, reason, idle_action=idle_action)
