import pytest

from birthday_cog import _birthday_announcement, _normalize_birth_year


@pytest.mark.parametrize("value", [None, "", 0, "0", " 0 ", "unknown"])
def test_missing_legacy_birth_year_has_no_age(value):
    assert _normalize_birth_year(value, 2026) is None


def test_real_birth_year_is_preserved():
    assert _normalize_birth_year("1998", 2026) == 1998


def test_zero_string_year_announcement_does_not_invent_an_age():
    message = _birthday_announcement("<@42>", "0", 2026)
    assert message == "🎂 It's <@42>'s birthday today! Happy Birthday! 🎉"
    assert "2026" not in message


def test_real_year_announcement_includes_correct_age():
    message = _birthday_announcement("<@42>", "1998", 2026)
    assert "turning **28** years old" in message
