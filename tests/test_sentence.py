"""Unit tests for SentenceSplitter."""

from daemon.sentence import SentenceSplitter
from daemon.tagparser import EmotionTag, GestureTag


def test_sentence_splitter_basic() -> None:
    splitter = SentenceSplitter(min_length=20)
    # Feed without ending punctuation for second clause to test buffering
    chunks = splitter.feed("Привет, хозяин! Как твои дела")
    assert len(chunks) == 1
    assert chunks[0].text == "Привет, хозяин!"

    remaining = splitter.flush()
    assert len(remaining) == 1
    assert remaining[0].text == "Как твои дела"


def test_sentence_splitter_abbreviation_protection() -> None:
    splitter = SentenceSplitter(min_length=15)
    # т.е. and 3.14 should not split within the sentence
    text = "Константа pi равна 3.14, т.е. значение не целое. И это факт!"
    chunks = splitter.feed(text) + splitter.flush()
    assert len(chunks) == 2
    assert chunks[0].text == "Константа pi равна 3.14, т.е. значение не целое."
    assert chunks[1].text == "И это факт!"


def test_sentence_splitter_with_tags() -> None:
    splitter = SentenceSplitter(min_length=15)
    splitter.add_tag(EmotionTag(name="happy", intensity=0.9))
    splitter.add_tag(GestureTag(name="wave"))

    chunks = splitter.feed("Привет, я рада тебя видеть! ")
    assert len(chunks) == 1
    assert chunks[0].text == "Привет, я рада тебя видеть!"
    assert len(chunks[0].tags) == 2
    assert isinstance(chunks[0].tags[0], EmotionTag)
    assert chunks[0].tags[0].name == "happy"
    assert isinstance(chunks[0].tags[1], GestureTag)
    assert chunks[0].tags[1].name == "wave"


def test_sentence_splitter_long_clause_split() -> None:
    splitter = SentenceSplitter(min_length=20, max_length=100)
    long_clause = (
        "Это очень длинное предложение без точки в середине, "
        "но мы обязательно должны разделить его аккуратно по запятой, "
        "иначе TTS не успеет начать говорить вовремя."
    )
    chunks = splitter.feed(long_clause) + splitter.flush()
    assert len(chunks) >= 2
    assert all(len(c.text) <= 120 for c in chunks)
