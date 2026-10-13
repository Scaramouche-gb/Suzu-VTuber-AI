"""Unit tests for streaming TagParser."""

from daemon.tagparser import (
    CommandTag,
    EmotionTag,
    GestureTag,
    LookTag,
    SilentTag,
    TagParser,
    ThoughtTag,
)


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


def test_tag_parser_command_preserves_case() -> None:
    parser = TagParser()
    items = parser.feed("<cmd=type:Привет, Саша!>Готово")
    assert len(items) == 2
    assert items[0].action == "type:Привет, Саша!"
    assert items[1] == "Готово"


def test_tag_parser_unclosed_command_flush() -> None:
    parser = TagParser()
    items = parser.feed("<cmd=type:Тестовая строка")
    assert items == []
    flushed = parser.flush()
    assert len(flushed) == 1
    assert flushed[0].action == "type:Тестовая строка"


def test_tag_parser_thought_tags() -> None:
    parser = TagParser()
    items = parser.feed("<thought>Саша пишет код в Zed</thought><emo=playful:0.8>Опять кодишь?")
    assert len(items) == 3
    assert isinstance(items[0], ThoughtTag)
    assert items[0].content == "Саша пишет код в Zed"
    assert isinstance(items[1], EmotionTag)
    assert items[1].name == "playful"
    assert items[2] == "Опять кодишь?"


def test_tag_parser_thought_inline() -> None:
    parser = TagParser()
    items = parser.feed("<thought:размышляет о музыке><emo=happy:0.7>Трек огонь!")
    assert len(items) == 3
    assert isinstance(items[0], ThoughtTag)
    assert items[0].content == "размышляет о музыке"
    assert isinstance(items[1], EmotionTag)
    assert items[1].name == "happy"
    assert items[2] == "Трек огонь!"


def test_tag_parser_unclosed_thought_flush() -> None:
    parser = TagParser()
    items = parser.feed("<thought>Недописанная мысль без закрывающего тега")
    assert items == []
    flushed = parser.flush()
    assert len(flushed) == 1
    assert isinstance(flushed[0], ThoughtTag)
    assert flushed[0].content == "Недописанная мысль без закрывающего тега"


def test_tag_parser_send_command() -> None:
    parser = TagParser()
    items = parser.feed("<cmd=send:Привет из Telegram! :3>Готово")
    assert len(items) == 2
    assert isinstance(items[0], CommandTag)
    assert items[0].action == "send:Привет из Telegram! :3"
    assert items[1] == "Готово"

