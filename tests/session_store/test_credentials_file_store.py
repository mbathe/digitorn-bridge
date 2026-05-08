"""FileCredentialStore: encrypted per-user credential persistence.

Uses a stub cipher (XOR-based, just to roundtrip without depending
on the real master-key infra). The real ``Cipher`` is exercised
in ``tests/credentials/`` via the legacy DB store -- here we only
care about the file-store behaviour above the cipher layer.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from digitorn.core.credentials.file_store import (
    CredentialSummary, FileCredentialStore,
)


class _StubCipher:
    """Minimal cipher stub for tests. NOT cryptographically secure --
    only roundtrips so the file-store contract can be tested without
    bringing up the real master-key provider."""

    async def encrypt(self, fields: dict[str, Any]) -> tuple[bytes, bytes]:
        return json.dumps(fields).encode("utf-8"), b"\x00" * 12

    async def decrypt(self, payload: bytes, nonce: bytes) -> dict[str, Any]:
        return json.loads(payload.decode("utf-8"))


@pytest.fixture
def stub_cipher():
    return _StubCipher()


@pytest.mark.asyncio
async def test_put_then_get_roundtrip(tmp_root: Path, stub_cipher):
    store = FileCredentialStore(root=tmp_root, cipher=stub_cipher)
    summary = await store.put_credential(
        user_id="u1", provider="github_copilot",
        label="Production", fields={"api_key": "sk-123"},
    )
    assert summary.user_id == "u1"
    assert summary.provider == "github_copilot"
    assert summary.label == "Production"
    assert summary.scope == "per_user"
    fields = await store.get_credential(
        user_id="u1", credential_id=summary.credential_id,
    )
    assert fields == {"api_key": "sk-123"}


@pytest.mark.asyncio
async def test_get_unknown_returns_none(tmp_root: Path, stub_cipher):
    store = FileCredentialStore(root=tmp_root, cipher=stub_cipher)
    assert await store.get_credential(
        user_id="u1", credential_id="nope",
    ) is None
    assert await store.get_summary(
        user_id="u1", credential_id="nope",
    ) is None


@pytest.mark.asyncio
async def test_list_per_user_only(tmp_root: Path, stub_cipher):
    store = FileCredentialStore(root=tmp_root, cipher=stub_cipher)
    await store.put_credential(
        user_id="alice", provider="openai",
        label="A", fields={"k": "v"},
    )
    await store.put_credential(
        user_id="alice", provider="anthropic",
        label="A2", fields={"k": "v"},
    )
    await store.put_credential(
        user_id="bob", provider="openai",
        label="B", fields={"k": "v"},
    )
    alice_creds = await store.list_user_credentials(user_id="alice")
    assert len(alice_creds) == 2
    assert {c.label for c in alice_creds} == {"A", "A2"}
    bob_creds = await store.list_user_credentials(user_id="bob")
    assert len(bob_creds) == 1
    assert bob_creds[0].label == "B"


@pytest.mark.asyncio
async def test_list_unknown_user_empty(tmp_root: Path, stub_cipher):
    store = FileCredentialStore(root=tmp_root, cipher=stub_cipher)
    assert await store.list_user_credentials(user_id="ghost") == []


@pytest.mark.asyncio
async def test_delete_credential(tmp_root: Path, stub_cipher):
    store = FileCredentialStore(root=tmp_root, cipher=stub_cipher)
    summary = await store.put_credential(
        user_id="u", provider="p", label="L", fields={"k": "v"},
    )
    ok = await store.delete_credential(
        user_id="u", credential_id=summary.credential_id,
    )
    assert ok is True
    assert await store.get_credential(
        user_id="u", credential_id=summary.credential_id,
    ) is None


@pytest.mark.asyncio
async def test_delete_unknown_returns_false(tmp_root: Path, stub_cipher):
    store = FileCredentialStore(root=tmp_root, cipher=stub_cipher)
    assert await store.delete_credential(
        user_id="u", credential_id="nope",
    ) is False


@pytest.mark.asyncio
async def test_update_label(tmp_root: Path, stub_cipher):
    store = FileCredentialStore(root=tmp_root, cipher=stub_cipher)
    summary = await store.put_credential(
        user_id="u", provider="p", label="old", fields={"k": "v"},
    )
    import asyncio
    await asyncio.sleep(0.01)
    ok = await store.update_label(
        user_id="u", credential_id=summary.credential_id,
        new_label="new",
    )
    assert ok is True
    s = await store.get_summary(
        user_id="u", credential_id=summary.credential_id,
    )
    assert s.label == "new"
    assert s.updated_at >= summary.updated_at


@pytest.mark.asyncio
async def test_update_label_unknown_returns_false(tmp_root: Path, stub_cipher):
    store = FileCredentialStore(root=tmp_root, cipher=stub_cipher)
    ok = await store.update_label(
        user_id="u", credential_id="nope", new_label="x",
    )
    assert ok is False


@pytest.mark.asyncio
async def test_overwrite_with_explicit_id(tmp_root: Path, stub_cipher):
    store = FileCredentialStore(root=tmp_root, cipher=stub_cipher)
    s1 = await store.put_credential(
        user_id="u", provider="p", label="L",
        fields={"v": 1}, credential_id="fixed",
    )
    s2 = await store.put_credential(
        user_id="u", provider="p", label="L",
        fields={"v": 2}, credential_id="fixed",
    )
    assert s1.credential_id == s2.credential_id == "fixed"
    fields = await store.get_credential(user_id="u", credential_id="fixed")
    assert fields == {"v": 2}


@pytest.mark.asyncio
async def test_invalid_user_id_rejected(tmp_root: Path, stub_cipher):
    store = FileCredentialStore(root=tmp_root, cipher=stub_cipher)
    with pytest.raises(ValueError, match="invalid user_id"):
        await store.put_credential(
            user_id="../etc", provider="p", label="L", fields={},
        )
    with pytest.raises(ValueError, match="invalid user_id"):
        await store.put_credential(
            user_id="u/sub", provider="p", label="L", fields={},
        )
    with pytest.raises(ValueError, match="invalid user_id"):
        await store.put_credential(
            user_id="", provider="p", label="L", fields={},
        )


@pytest.mark.asyncio
async def test_invalid_credential_id_rejected(tmp_root: Path, stub_cipher):
    store = FileCredentialStore(root=tmp_root, cipher=stub_cipher)
    with pytest.raises(ValueError, match="invalid credential_id"):
        await store.put_credential(
            user_id="u", provider="p", label="L", fields={},
            credential_id="../escape",
        )


@pytest.mark.asyncio
async def test_corrupt_envelope_returns_none(tmp_root: Path, stub_cipher):
    store = FileCredentialStore(root=tmp_root, cipher=stub_cipher)
    user_dir = tmp_root / "u"
    user_dir.mkdir(parents=True)
    (user_dir / "broken.json").write_text("{not json", encoding="utf-8")
    assert await store.get_credential(
        user_id="u", credential_id="broken",
    ) is None
    assert await store.list_user_credentials(user_id="u") == []


@pytest.mark.asyncio
async def test_atomic_write_no_partial_files(tmp_root: Path, stub_cipher):
    """After write, the user dir should contain ONLY the final file,
    no stale tmp files. Tests the os.replace happy path."""
    store = FileCredentialStore(root=tmp_root, cipher=stub_cipher)
    summary = await store.put_credential(
        user_id="u", provider="p", label="L", fields={"k": "v"},
    )
    user_dir = tmp_root / "u"
    files = sorted(p.name for p in user_dir.iterdir())
    assert files == [f"{summary.credential_id}.json"]


@pytest.mark.asyncio
async def test_summary_to_dict(tmp_root: Path, stub_cipher):
    store = FileCredentialStore(root=tmp_root, cipher=stub_cipher)
    summary = await store.put_credential(
        user_id="u", provider="github_copilot",
        label="P", fields={"k": "v"},
    )
    d = summary.to_dict()
    assert d["user_id"] == "u"
    assert d["provider"] == "github_copilot"
    assert d["label"] == "P"
    assert "credential_id" in d
    assert "created_at" in d
