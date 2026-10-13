"""Unit tests for streaming TagParser."""

from daemon.tagparser import EmotionTag, GestureTag, LookTag, SilentTag, TagParser


def test_tag_parser_simple() -> None:
    parser = TagParser()
    items = parser.feed("<emo=happy:0.8>Привет!")
    assert len(items) == 2
    assert isinstance(items[0], EmotionTag)
    assert items[0].name == "happy"
    assert items[0].intensity == 0.8
    assert items[1] == "Привет!"


def test_tag_parser_split_across_chunks() -> None:
    parser = TagParser()
    out1 = parser.feed("<em")
    assert out1 == []
    out2 = parser.feed("o=sad> Всё плохо")
    assert len(out2) == 2
    assert isinstance(out2[0], EmotionTag)
    assert out2[0].name == "sad"
    assert out2[0].intensity == 1.0
    assert out2[1] == " Всё плохо"


def test_tag_parser_gesture_and_look() -> None:
    parser = TagParser()
    items = parser.feed("<gst=wave>Опа!<look=screen>Смотри сюда")
    assert len(items) == 4
    assert isinstance(items[0], GestureTag)
    assert items[0].name == "wave"
    assert items[1] == "Опа!"
    assert isinstance(items[2], LookTag)
    assert items[2].target == "screen"
    assert items[3] == "Смотри сюда"


def test_tag_parser_silent() -> None:
    parser = TagParser()
    items = parser.feed("<silent>")
    assert len(items) == 1
    assert isinstance(items[0], SilentTag)


def test_tag_parser_invalid_tags_treated_as_text() -> None:
    parser = TagParser()
    items = parser.feed("<unknown_tag> текст")
    assert items == ["<unknown_tag> текст"]


def test_tag_parser_unclosed_tag_flush() -> None:
    parser = TagParser()
    items = parser.feed("Привет <незакрытый")
    assert items == ["Привет "]
    flushed = parser.flush()
    assert flushed == ["<незакрытый"]


def test_tag_parser_command() -> None:
    parser = TagParser()
    items = parser.feed("<cmd=launch:telegram>Запускаю телегу!")
    assert len(items) == 2
    assert items[0].action == "launch:telegram"
    assert items[1] == "Запускаю телегу!"
