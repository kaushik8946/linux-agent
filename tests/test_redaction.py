"""Secret redaction, including literal environment values."""

import pytest

from agent.redaction import REDACTED, redact

# -- short plain-word literals: whole words only --------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "g++ (Ubuntu 15.2.0-16ubuntu1) 15.2.0",
        "linux-image-7.0.0-42-generic ubuntu1 build",
        "path /usr/share/ubuntuish/file",
        "Ubuntu 26.04.1 LTS",
    ],
)
def test_short_plain_word_secret_does_not_mangle_other_words(text: str) -> None:
    # "ubuntu1" and "ubuntuish" contain the value but are different words;
    # "Ubuntu" differs in case. None is the secret, so all stay intact.
    assert redact(text, ["ubuntu"]) == text


@pytest.mark.parametrize(
    "text",
    [
        "password ubuntu",
        "ubuntu",
        "value=ubuntu;",
        "(ubuntu)",
        "x 'ubuntu' y",
        "a\nubuntu\n",
    ],
)
def test_short_plain_word_secret_is_still_redacted_as_a_word(text: str) -> None:
    out = redact(text, ["ubuntu"])
    assert "ubuntu" not in out and REDACTED in out


# -- long or mixed literals: replaced anywhere -----------------------------------


def test_long_secret_is_redacted_inside_other_text() -> None:
    secret = "verysecretvalue987"
    assert secret not in redact(f"x{secret}y and {secret}", [secret])


def test_long_plain_word_secret_is_redacted_inside_other_text() -> None:
    secret = "correcthorsebattery"  # letters only, but long
    assert secret not in redact(f"abc{secret}def", [secret])


@pytest.mark.parametrize("secret", ["abc123", "pa$$wd", "hunter2", "s3cr3t!"])
def test_mixed_secret_is_redacted_inside_other_text(secret: str) -> None:
    assert secret not in redact(f"xx{secret}yy", [secret])


def test_very_short_literals_are_ignored() -> None:
    assert redact("a b c abc", ["abc"]) == "a b c abc"


def test_empty_values_are_ignored() -> None:
    assert redact("text", [""]) == "text"


def test_regex_characters_in_secret_are_literal() -> None:
    secret = "a.b*c+d"
    assert redact("a.b*c+d and axbxcd", [secret]) == f"{REDACTED} and axbxcd"


# -- pattern redaction is unchanged ------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "password=hunter2hunter2",
        "API_KEY: abcdefgh12345678",
        "Authorization: Bearer abcdefghijklmnop",
        "https://user:pw12345@host.example/x",
        "sk-abcdefghijklmnopqrstuv",
        "ghp_" + "a" * 36,
    ],
)
def test_pattern_based_secrets_still_redacted(text: str) -> None:
    assert REDACTED in redact(text)
