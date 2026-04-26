from __future__ import annotations

import pytest

from digitorn_hub.versioning import InvalidVersion, is_greater, is_valid, parse


def test_parse_accepts_simple_semver():
    assert parse("1.2.3") == (1, 2, 3, (1,))


def test_parse_rejects_garbage():
    with pytest.raises(InvalidVersion):
        parse("not-a-version")


def test_pre_release_sorts_below_release():
    assert parse("1.0.0-alpha") < parse("1.0.0")


def test_is_greater_simple():
    assert is_greater("2.0.0", "1.99.99")
    assert not is_greater("1.0.0", "1.0.0")
    assert not is_greater("1.0.0-alpha", "1.0.0")


def test_is_valid():
    assert is_valid("0.1.0")
    assert is_valid("10.20.30-beta.2")
    assert not is_valid("1.0")
    assert not is_valid("v1.0.0")
