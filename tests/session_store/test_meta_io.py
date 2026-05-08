"""MetaIO: atomic write, JSONL tail recovery."""
from __future__ import annotations

import json
from pathlib import Path

from digitorn.core.runtime.session_store.meta_io import MetaIO


def test_write_then_read_roundtrip(tmp_root: Path):
    sd = tmp_root / "s1"
    MetaIO.write(sd, {"a": 1, "b": "x"})
    assert MetaIO.read(sd) == {"a": 1, "b": "x"}


def test_read_missing_returns_none(tmp_root: Path):
    assert MetaIO.read(tmp_root / "doesnotexist") is None


def test_read_corrupt_returns_none(tmp_root: Path):
    sd = tmp_root / "s1"
    sd.mkdir()
    (sd / "meta.json").write_text("not json {{{", encoding="utf-8")
    assert MetaIO.read(sd) is None


def test_update_merges(tmp_root: Path):
    sd = tmp_root / "s1"
    MetaIO.write(sd, {"a": 1})
    MetaIO.update(sd, b=2)
    assert MetaIO.read(sd) == {"a": 1, "b": 2}
    MetaIO.update(sd, a=99)
    assert MetaIO.read(sd) == {"a": 99, "b": 2}


def test_atomic_write_no_partial_on_overwrite(tmp_root: Path):
    sd = tmp_root / "s1"
    MetaIO.write(sd, {"version": 1, "huge": "x" * 100_000})
    MetaIO.write(sd, {"version": 2})
    meta = MetaIO.read(sd)
    assert meta == {"version": 2}


def test_jsonl_tail_finds_last_seq(tmp_root: Path):
    sd = tmp_root / "s1"
    sd.mkdir()
    (sd / "events.jsonl").write_text(
        '\n'.join(
            json.dumps({"seq": i, "type": "x"}) for i in range(1, 51)
        ),
        encoding="utf-8",
    )
    assert MetaIO.last_seq_from_jsonl_tail(sd) == 50


def test_jsonl_tail_missing_returns_zero(tmp_root: Path):
    sd = tmp_root / "doesnotexist"
    assert MetaIO.last_seq_from_jsonl_tail(sd) == 0


def test_jsonl_tail_skips_blanks(tmp_root: Path):
    sd = tmp_root / "s1"
    sd.mkdir()
    (sd / "events.jsonl").write_text(
        json.dumps({"seq": 7, "type": "x"}) + "\n\n\n",
        encoding="utf-8",
    )
    assert MetaIO.last_seq_from_jsonl_tail(sd) == 7


def test_jsonl_tail_corrupt_returns_zero(tmp_root: Path):
    sd = tmp_root / "s1"
    sd.mkdir()
    (sd / "events.jsonl").write_text("garbage\n", encoding="utf-8")
    assert MetaIO.last_seq_from_jsonl_tail(sd) == 0
