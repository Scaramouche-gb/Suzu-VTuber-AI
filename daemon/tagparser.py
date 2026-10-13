"""Streaming Tag Parser for Suzu VTuber AI."""

from __future__ import annotations

import re
from dataclasses import dataclass

EMOTION_REGEX = re.compile(r"^emo=([a-zA-Z0-9_-]+)(?::([0-9.]+))?$")
GESTURE_REGEX = re.compile(r"^gst=([a-zA-Z0-9_-]+)$")
LOOK_REGEX = re.compile(r"^look=([a-zA-Z0-9_-]+)$")
CMD_REGEX = re.compile(r"^cmd=(.+)$")


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


@dataclass(slots=True)
class ThoughtTag:
    content: str


TagItem = EmotionTag | GestureTag | LookTag | SilentTag | CommandTag | ThoughtTag


@dataclass(slots=True)
class ParsedToken:
    text: str = ""
    tags: list[TagItem] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.tags is None:
            self.tags = []


class TagParser:
    """Stateful streaming tag parser with cross-chunk boundary support and thought isolation."""

    def __init__(self, max_tag_length: int = 500) -> None:
        self.max_tag_length = max_tag_length
        self._in_tag = False
        self._tag_buffer: list[str] = []
        self._in_thought = False
        self._thought_buffer: list[str] = []

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
                    if self._in_thought:
                        self._thought_buffer.append(ch)
                    else:
                        speech_buf.append(ch)
            else:
                if ch == ">":
                    tag_str = "".join(self._tag_buffer).strip()
                    tag_lower = tag_str.lower()

                    if tag_lower == "thought":
                        if speech_buf:
                            output.append("".join(speech_buf))
                            speech_buf = []
                        self._in_thought = True
                        self._thought_buffer = []
                    elif tag_lower in ("/thought", "thought/"):
                        thought_content = "".join(self._thought_buffer).strip()
                        self._in_thought = False
                        self._thought_buffer = []
                        if thought_content:
                            output.append(ThoughtTag(content=thought_content))
                    else:
                        parsed_tag = self._parse_tag_string(tag_str)
                        if parsed_tag:
                            if speech_buf:
                                output.append("".join(speech_buf))
                                speech_buf = []
                            output.append(parsed_tag)
                        else:
                            # Invalid tag, treat as plain text
                            if self._in_thought:
                                self._thought_buffer.append("<")
                                self._thought_buffer.extend(self._tag_buffer)
                                self._thought_buffer.append(">")
                            else:
                                speech_buf.append("<")
                                speech_buf.extend(self._tag_buffer)
                                speech_buf.append(">")
                    self._in_tag = False
                    self._tag_buffer = []
                elif ch == "<":
                    # Nested '<', flush previous
                    if self._in_thought:
                        self._thought_buffer.append("<")
                        self._thought_buffer.extend(self._tag_buffer)
                    else:
                        speech_buf.append("<")
                        speech_buf.extend(self._tag_buffer)
                    self._tag_buffer = []
                else:
                    self._tag_buffer.append(ch)
                    if len(self._tag_buffer) > self.max_tag_length:
                        # Exceeded max length, flush
                        if self._in_thought:
                            self._thought_buffer.append("<")
                            self._thought_buffer.extend(self._tag_buffer)
                        else:
                            speech_buf.append("<")
                            speech_buf.extend(self._tag_buffer)
                        self._in_tag = False
                        self._tag_buffer = []

        if speech_buf and not self._in_thought:
            output.append("".join(speech_buf))

        return output

    def flush(self) -> list[str | TagItem]:
        """Flush remaining buffered text at end of stream."""
        output: list[str | TagItem] = []
        if self._in_thought and self._thought_buffer:
            thought_text = "".join(self._thought_buffer).strip()
            self._in_thought = False
            self._thought_buffer = []
            if thought_text:
                output.append(ThoughtTag(content=thought_text))

        if self._in_tag:
            tag_str = "".join(self._tag_buffer).strip()
            tag_lower = tag_str.lower()
            if tag_lower in ("/thought", "thought/"):
                if self._thought_buffer:
                    output.append(ThoughtTag(content="".join(self._thought_buffer).strip()))
                    self._thought_buffer = []
            else:
                parsed_tag = self._parse_tag_string(tag_str)
                if parsed_tag:
                    output.append(parsed_tag)
                else:
                    output.append("<" + "".join(self._tag_buffer))
            self._in_tag = False
            self._tag_buffer = []
        return output

    @staticmethod
    def _parse_tag_string(s: str) -> TagItem | None:
        if s == "silent":
            return SilentTag()

        s_lower = s.lower()
        if s_lower.startswith(("thought=", "thought:")):
            val = s.split("=", 1)[1] if "=" in s else s.split(":", 1)[1]
            return ThoughtTag(content=val.strip())

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
            raw_action = cmd_match.group(1).strip()
            if ":" in raw_action:
                prefix, val = raw_action.split(":", 1)
                prefix_clean = prefix.strip().lower()
                # Preserve exact case for user text and shell/code commands
                if prefix_clean in ("type", "term", "code"):
                    return CommandTag(action=f"{prefix_clean}:{val.strip()}")
                return CommandTag(action=f"{prefix_clean}:{val.strip().lower()}")
            return CommandTag(action=raw_action.lower())

        return None
