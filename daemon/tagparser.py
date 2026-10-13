"""Streaming Tag Parser for Suzu VTuber AI."""

from __future__ import annotations

import re
from dataclasses import dataclass

EMOTION_REGEX = re.compile(r"^emo=([a-zA-Z0-9_-]+)(?::([0-9.]+))?$")
GESTURE_REGEX = re.compile(r"^gst=([a-zA-Z0-9_-]+)$")
LOOK_REGEX = re.compile(r"^look=([a-zA-Z0-9_-]+)$")
CMD_REGEX = re.compile(r"^cmd=([a-zA-Z0-9_:-]+)$")


@dataclass(slots=True)
class EmotionTag:
    name: str
    intensity: float = 1.0


@dataclass(slots=True)
class GestureTag:
    name: str


@dataclass(slots=True)
class LookTag:
    target: str


@dataclass(slots=True)
class CommandTag:
    action: str


@dataclass(slots=True)
class SilentTag:
    pass


TagItem = EmotionTag | GestureTag | LookTag | SilentTag | CommandTag


@dataclass(slots=True)
class ParsedToken:
    text: str = ""
    tags: list[TagItem] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.tags is None:
            self.tags = []


class TagParser:
    """Stateful streaming tag parser with cross-chunk boundary support."""

    def __init__(self, max_tag_length: int = 40) -> None:
        self.max_tag_length = max_tag_length
        self._in_tag = False
        self._tag_buffer: list[str] = []

    def feed(self, chunk: str) -> list[str | TagItem]:
        """Process incoming chunk and return speech text segments and parsed tags."""
        output: list[str | TagItem] = []
        speech_buf: list[str] = []

        for ch in chunk:
            if not self._in_tag:
                if ch == "<":
                    self._in_tag = True
                    self._tag_buffer = []
                else:
                    speech_buf.append(ch)
            else:
                if ch == ">":
                    tag_str = "".join(self._tag_buffer).strip()
                    parsed_tag = self._parse_tag_string(tag_str)
                    if parsed_tag:
                        if speech_buf:
                            output.append("".join(speech_buf))
                            speech_buf = []
                        output.append(parsed_tag)
                    else:
                        # Invalid tag, treat as plain text: "<" + buffer + ">"
                        speech_buf.append("<")
                        speech_buf.extend(self._tag_buffer)
                        speech_buf.append(">")
                    self._in_tag = False
                    self._tag_buffer = []
                elif ch == "<":
                    # Nested '<', flush previous as plain text and restart
                    speech_buf.append("<")
                    speech_buf.extend(self._tag_buffer)
                    self._tag_buffer = []
                else:
                    self._tag_buffer.append(ch)
                    if len(self._tag_buffer) > self.max_tag_length:
                        # Exceeded max length, flush as plain text
                        speech_buf.append("<")
                        speech_buf.extend(self._tag_buffer)
                        self._in_tag = False
                        self._tag_buffer = []

        if speech_buf:
            output.append("".join(speech_buf))

        return output

    def flush(self) -> list[str | TagItem]:
        """Flush remaining buffered text at end of stream."""
        output: list[str | TagItem] = []
        if self._in_tag:
            output.append("<" + "".join(self._tag_buffer))
            self._in_tag = False
            self._tag_buffer = []
        return output

    @staticmethod
    def _parse_tag_string(s: str) -> TagItem | None:
        if s == "silent":
            return SilentTag()

        emo_match = EMOTION_REGEX.match(s)
        if emo_match:
            name = emo_match.group(1).lower()
            intensity_str = emo_match.group(2)
            intensity = float(intensity_str) if intensity_str else 1.0
            return EmotionTag(name=name, intensity=min(1.0, max(0.0, intensity)))

        gst_match = GESTURE_REGEX.match(s)
        if gst_match:
            return GestureTag(name=gst_match.group(1).lower())

        look_match = LOOK_REGEX.match(s)
        if look_match:
            return LookTag(target=look_match.group(1).lower())

        cmd_match = CMD_REGEX.match(s)
        if cmd_match:
            return CommandTag(action=cmd_match.group(1).lower())

        return None
