"""Stream-oriented sentence splitter with abbreviation protection and tag binding."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from daemon.tagparser import TagItem

# Common Russian and English abbreviations that end with a dot but should NOT terminate a sentence
ABBREVIATIONS = {
    "т.е.", "и.т.д.", "и.т.п.", "т.д.", "т.п.",
    "руб.", "коп.", "г.", "ул.", "д.", "кв.", "стр.",
    "e.g.", "i.e.", "etc.", "vs.", "mr.", "mrs.", "ms.", "dr.", "prof.",
}

# Regex to detect trailing numbers like "3." in "3.14" or list items
DECIMAL_OR_NUM_REGEX = re.compile(r"\b\d+\.$")
PUNCTUATION_SPLIT_REGEX = re.compile(r"([.!?…\n]+)")
CLAUSE_SPLIT_REGEX = re.compile(r"[,;:\—\-]\s+")


@dataclass(slots=True)
class SentenceChunk:
    """A single sentence or clause ready for TTS with its associated tags."""
    text: str
    tags: list[TagItem] = field(default_factory=list)


class SentenceSplitter:
    """Accumulates streaming tokens, splits into natural speech sentences."""

    def __init__(self, min_length: int = 25, max_length: int = 220) -> None:
        self.min_length = min_length
        self.max_length = max_length
        self._text_buffer = ""
        self._pending_tags: list[TagItem] = []
        self._first_sentence_emitted = False

    def add_tag(self, tag: TagItem) -> None:
        """Bind a tag that will be attached to the upcoming sentence."""
        self._pending_tags.append(tag)

    def feed(self, text: str) -> list[SentenceChunk]:
        """Feed text chunk and yield complete sentence chunks."""
        self._text_buffer += text
        return self._extract_sentences(is_flush=False)

    def flush(self) -> list[SentenceChunk]:
        """Flush remaining text at the end of LLM generation."""
        return self._extract_sentences(is_flush=True)

    def _extract_sentences(self, is_flush: bool) -> list[SentenceChunk]:
        chunks: list[SentenceChunk] = []

        while True:
            # Check if we should force-split a very long buffer without punctuation
            if len(self._text_buffer) >= self.max_length:
                split_idx = self._find_clause_break(self._text_buffer[:self.max_length])
                if split_idx != -1:
                    sentence_text = self._text_buffer[:split_idx].strip()
                    self._text_buffer = self._text_buffer[split_idx:].lstrip()
                    if sentence_text:
                        chunks.append(self._create_chunk(sentence_text))
                    continue
                else:
                    # No clause break, split at last space
                    space_idx = self._text_buffer[:self.max_length].rfind(" ")
                    if space_idx > self.min_length:
                        sentence_text = self._text_buffer[:space_idx].strip()
                        self._text_buffer = self._text_buffer[space_idx:].lstrip()
                        if sentence_text:
                            chunks.append(self._create_chunk(sentence_text))
                        continue

            boundary_idx = self._find_sentence_boundary(self._text_buffer)
            if boundary_idx == -1:
                break

            candidate = self._text_buffer[:boundary_idx].strip()
            min_len = 15 if not self._first_sentence_emitted else self.min_length

            # If candidate is too short and not flushing, wait for more text
            if len(candidate) < min_len and not is_flush and len(self._text_buffer) < self.max_length:
                break

            self._text_buffer = self._text_buffer[boundary_idx:].lstrip()
            if candidate:
                chunks.append(self._create_chunk(candidate))

        if is_flush and self._text_buffer.strip():
            chunks.append(self._create_chunk(self._text_buffer.strip()))
            self._text_buffer = ""

        return chunks

    def _find_sentence_boundary(self, text: str) -> int:
        for match in PUNCTUATION_SPLIT_REGEX.finditer(text):
            end_pos = match.end()
            candidate = text[:end_pos].strip()

            # Check if this boundary is inside an abbreviation
            words = candidate.split()
            if words:
                last_word = words[-1].lower()
                if last_word in ABBREVIATIONS:
                    continue
                if DECIMAL_OR_NUM_REGEX.search(last_word) and end_pos < len(text) and text[end_pos].isdigit():
                    continue

            # Valid boundary found if followed by whitespace, newline or end of text
            if end_pos >= len(text) or text[end_pos].isspace():
                return end_pos

        return -1

    @staticmethod
    def _find_clause_break(text: str) -> int:
        last_match = None
        for m in CLAUSE_SPLIT_REGEX.finditer(text):
            last_match = m
        if last_match and last_match.end() >= 20:
            return last_match.end()
        return -1

    def _create_chunk(self, text: str) -> SentenceChunk:
        tags = list(self._pending_tags)
        self._pending_tags = []
        self._first_sentence_emitted = True
        return SentenceChunk(text=text, tags=tags)
