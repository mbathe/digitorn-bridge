"""Test live daemon — actions, vitesse I/O, et appel LLM DeepSeek.

Usage:
    DEEPSEEK_API_KEY=sk-xxx python -m pytest tests/test_daemon_live.py -v -s

Ce fichier est temporaire — supprimer après validation.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from digitorn.core.config import Settings, override_settings
from digitorn.core.server import create_app

# ---------------------------------------------------------------------------
# Fixture: daemon HTTP client (in-process, no network)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client():
    settings = Settings()
    settings.database.url = "sqlite+aiosqlite://"
    settings.modules.load_all = True
    settings.server.auth_enabled = False
    settings.server.rate_limit_rpm = 100_000  # disable rate limiting in tests
    settings.server.kv_backend = tempfile.mkdtemp()  # fresh rate limiter per test
    override_settings(settings)
    asgi_app = create_app(settings=settings)
    inner_app = asgi_app.other_asgi_app

    async with LifespanManager(inner_app) as _:
        transport = ASGITransport(app=inner_app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


async def execute(client: AsyncClient, module: str, action: str, params: dict) -> dict:
    r = await client.post(f"/api/modules/{module}/execute", json={
        "action": action,
        "params": params,
    })
    assert r.status_code == 200, f"HTTP {r.status_code}: {r.text}"
    return r.json()


# ===========================================================================
# 1. Test: modules chargés correctement dans le daemon
# ===========================================================================


class TestModulesLoaded:
    @pytest.mark.asyncio
    async def test_list_modules(self, client: AsyncClient):
        r = await client.get("/api/modules")
        assert r.status_code == 200
        data = r.json()
        ids = [m["module_id"] for m in data["modules"]]
        print(f"\n  Modules chargés: {ids}")
        assert "hello" in ids
        assert "filesystem" in ids

    @pytest.mark.asyncio
    async def test_hello_module_detail(self, client: AsyncClient):
        r = await client.get("/api/modules/hello")
        assert r.status_code == 200
        data = r.json()
        actions = [a["name"] for a in data["actions"]]
        print(f"\n  Hello actions: {actions}")
        assert "say_hello" in actions
        assert "greet_many" in actions
        assert "status" in actions

    @pytest.mark.asyncio
    async def test_filesystem_module_detail(self, client: AsyncClient):
        r = await client.get("/api/modules/filesystem")
        assert r.status_code == 200
        data = r.json()
        actions = [a["name"] for a in data["actions"]]
        print(f"\n  Filesystem actions: {actions}")
        expected = ["read", "write", "edit", "insert", "ls", "grep", "find",
                    "mv", "cp", "rm", "mkdir", "file_stat"]
        for a in expected:
            assert a in actions, f"Action manquante: {a}"


# ===========================================================================
# 2. Test: exécution d'actions via le daemon (hello + filesystem)
# ===========================================================================


class TestActionExecution:
    @pytest.mark.asyncio
    async def test_hello_say_hello(self, client: AsyncClient):
        body = await execute(client, "hello", "say_hello", {"name": "Daemon"})
        assert body["success"] is True
        assert body["data"] == "Hello, Daemon!"
        print(f"\n  say_hello => {body['data']}")

    @pytest.mark.asyncio
    async def test_hello_greet_many(self, client: AsyncClient):
        body = await execute(client, "hello", "greet_many", {"names": ["A", "B"]})
        assert body["success"] is True
        assert len(body["data"]) == 2
        print(f"\n  greet_many => {body['data']}")

    @pytest.mark.asyncio
    async def test_hello_status(self, client: AsyncClient):
        body = await execute(client, "hello", "status", {})
        assert body["success"] is True
        assert body["data"]["module_id"] == "hello"
        print(f"\n  status => {body['data']}")

    @pytest.mark.asyncio
    async def test_filesystem_write_read(self, client: AsyncClient):
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            path = f.name

        try:
            # write
            body = await execute(client, "filesystem", "write", {
                "path": path,
                "content": "line1\nline2\nline3\n",
            })
            assert body["success"] is True
            print(f"\n  write => {body['data']}")

            # read
            body = await execute(client, "filesystem", "read", {"path": path})
            assert body["success"] is True
            assert body["data"]["total_lines"] == 3
            print(f"  read => {body['data']['total_lines']} lines")
        finally:
            Path(path).unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_filesystem_edit(self, client: AsyncClient):
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w") as f:
            f.write("def foo():\n    return 42\n")
            path = f.name

        try:
            # Must read before edit (security: read-before-edit check)
            await execute(client, "filesystem", "read", {"path": path})

            body = await execute(client, "filesystem", "edit", {
                "path": path,
                "old_string": "return 42",
                "new_string": "return 99",
            })
            assert body["success"] is True
            assert body["data"]["replacements"] == 1
            print(f"\n  edit => {body['data']}")

            # verify
            content = Path(path).read_text()
            assert "return 99" in content
        finally:
            Path(path).unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_filesystem_insert(self, client: AsyncClient):
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w") as f:
            f.write("line1\nline2\nline3\n")
            path = f.name

        try:
            body = await execute(client, "filesystem", "insert", {
                "path": path,
                "line": 2,
                "content": "INSERTED\n",
            })
            assert body["success"] is True
            assert body["data"]["lines_inserted"] == 1
            print(f"\n  insert => {body['data']}")

            content = Path(path).read_text()
            assert content.splitlines()[1] == "INSERTED"
        finally:
            Path(path).unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_filesystem_ls_mkdir_rm(self, client: AsyncClient):
        with tempfile.TemporaryDirectory() as tmpdir:
            # mkdir
            subdir = str(Path(tmpdir) / "sub" / "deep")
            body = await execute(client, "filesystem", "mkdir", {"path": subdir})
            assert body["success"] is True

            # ls
            body = await execute(client, "filesystem", "ls", {"path": tmpdir, "recursive": True})
            assert body["success"] is True
            assert body["data"]["count"] >= 1
            print(f"\n  ls => {body['data']['count']} entries")

            # rm
            body = await execute(client, "filesystem", "rm", {
                "path": subdir, "recursive": True,
            })
            assert body["success"] is True

    @pytest.mark.asyncio
    async def test_validation_error_clean(self, client: AsyncClient):
        """Mauvais paramètres => erreur propre, pas de crash."""
        body = await execute(client, "filesystem", "read", {"path": 12345})
        assert body["success"] is False
        assert "path" in body["error"].lower()
        print(f"\n  validation error => {body['error'][:80]}")


# ===========================================================================
# 3. Benchmark: vitesse I/O via le daemon
# ===========================================================================


class TestIOBenchmark:
    @pytest.mark.asyncio
    async def test_write_read_speed(self, client: AsyncClient):
        """Benchmark write + read pour différentes tailles."""
        sizes = [
            ("1 KB", 1024),
            ("10 KB", 10 * 1024),
            ("100 KB", 100 * 1024),
            ("500 KB", 500 * 1024),
            ("1 MB", 1024 * 1024),
        ]

        print("\n  ┌─────────┬────────────┬────────────┬─────────────┐")
        print("  │  Size   │  Write ms  │  Read ms   │  Edit ms    │")
        print("  ├─────────┼────────────┼────────────┼─────────────┤")

        for label, size in sizes:
            content = "x" * size + "\n"
            with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
                path = f.name

            try:
                # write
                t0 = time.perf_counter()
                body = await execute(client, "filesystem", "write", {
                    "path": path, "content": content,
                })
                write_ms = (time.perf_counter() - t0) * 1000
                assert body["success"]

                # read
                t0 = time.perf_counter()
                body = await execute(client, "filesystem", "read", {"path": path})
                read_ms = (time.perf_counter() - t0) * 1000
                assert body["success"]

                # edit (replace first 10 chars)
                t0 = time.perf_counter()
                body = await execute(client, "filesystem", "edit", {
                    "path": path,
                    "old_string": content[:10],
                    "new_string": "Y" * 10,
                })
                edit_ms = (time.perf_counter() - t0) * 1000
                assert body["success"]

                print(f"  │ {label:>7} │ {write_ms:>8.1f} ms │ {read_ms:>8.1f} ms │ {edit_ms:>9.1f} ms │")
            finally:
                Path(path).unlink(missing_ok=True)

        print("  └─────────┴────────────┴────────────┴─────────────┘")

    @pytest.mark.asyncio
    async def test_concurrent_actions(self, client: AsyncClient):
        """10 actions en parallèle via le daemon."""
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w") as f:
            f.write("Hello World\n" * 100)
            path = f.name

        try:
            t0 = time.perf_counter()
            tasks = [
                execute(client, "filesystem", "read", {"path": path})
                for _ in range(10)
            ]
            results = await asyncio.gather(*tasks)
            elapsed_ms = (time.perf_counter() - t0) * 1000

            assert all(r["success"] for r in results)
            print(f"\n  10 reads concurrents: {elapsed_ms:.1f} ms total")
        finally:
            Path(path).unlink(missing_ok=True)


# ===========================================================================
# 4. Test LLM DeepSeek: l'agent découvre et appelle les actions
# ===========================================================================


DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")


def _build_tools_from_manifest(actions: list[dict]) -> list[dict]:
    """Construit les tools OpenAI-style depuis le manifest du daemon."""
    tools = []
    for a in actions:
        # Récupérer le schema depuis l'action spec
        tool = {
            "type": "function",
            "function": {
                "name": a["name"],
                "description": a.get("description", ""),
                "parameters": a.get("input_schema", {"type": "object", "properties": {}}),
            },
        }
        tools.append(tool)
    return tools


@pytest.mark.skipif(not DEEPSEEK_KEY, reason="DEEPSEEK_API_KEY not set")
class TestDeepSeekAgent:
    """Test qu'un LLM DeepSeek peut découvrir et appeler les actions."""

    @pytest.mark.asyncio
    async def test_agent_discovers_and_calls_hello(self, client: AsyncClient):
        """Le LLM doit appeler say_hello avec le bon paramètre."""
        from openai import OpenAI

        # 1. Récupérer le manifest du module hello depuis le daemon
        r = await client.get("/api/modules/hello")
        manifest = r.json()

        # Construire les tools
        tools = _build_tools_from_manifest(manifest["actions"])
        print(f"\n  Tools exposés au LLM: {[t['function']['name'] for t in tools]}")

        # 2. Appeler DeepSeek avec les tools
        llm = OpenAI(
            api_key=DEEPSEEK_KEY,
            base_url="https://api.deepseek.com",
        )

        messages = [
            {"role": "system", "content": (
                "Tu es un agent qui utilise les outils disponibles. "
                "Appelle l'outil approprié pour répondre."
            )},
            {"role": "user", "content": "Dis bonjour à Paul"},
        ]

        t0 = time.perf_counter()
        response = llm.chat.completions.create(
            model="deepseek-chat",
            messages=messages,
            tools=tools,
            tool_choice="auto",
        )
        llm_ms = (time.perf_counter() - t0) * 1000

        # 3. Vérifier que le LLM a choisi le bon tool
        msg = response.choices[0].message
        assert msg.tool_calls, "Le LLM n'a pas appelé de tool!"

        call = msg.tool_calls[0]
        print(f"  LLM a choisi: {call.function.name}({call.function.arguments})")
        print(f"  Temps LLM: {llm_ms:.0f} ms")

        assert call.function.name == "say_hello"
        args = json.loads(call.function.arguments)
        # Le LLM doit avoir fourni un name (grâce au schema exposé)
        assert "name" in args, "Le LLM n'a pas fourni le paramètre 'name'!"
        print(f"  Param name reçu: {args['name']}")

        # 4. Exécuter l'action dans le daemon
        body = await execute(client, "hello", call.function.name, args)
        assert body["success"] is True
        print(f"  Résultat daemon: {body['data']}")

    @pytest.mark.asyncio
    async def test_agent_discovers_and_calls_filesystem(self, client: AsyncClient):
        """Le LLM doit appeler write puis read avec les bons paramètres."""
        from openai import OpenAI

        # 1. Récupérer le manifest filesystem
        r = await client.get("/api/modules/filesystem")
        manifest = r.json()
        tools = _build_tools_from_manifest(manifest["actions"])
        print(f"\n  {len(tools)} tools filesystem exposés au LLM")

        # 2. Demander au LLM d'écrire un fichier
        llm = OpenAI(
            api_key=DEEPSEEK_KEY,
            base_url="https://api.deepseek.com",
        )

        tmp_path = str(Path(tempfile.gettempdir()) / "deepseek_test_file.txt")

        messages = [
            {"role": "system", "content": (
                "Tu es un agent qui utilise les outils disponibles pour manipuler des fichiers. "
                "Appelle l'outil approprié."
            )},
            {"role": "user", "content": (
                f"Écris le texte 'Hello from DeepSeek!' dans le fichier {tmp_path}"
            )},
        ]

        t0 = time.perf_counter()
        response = llm.chat.completions.create(
            model="deepseek-chat",
            messages=messages,
            tools=tools,
            tool_choice="auto",
        )
        llm_ms = (time.perf_counter() - t0) * 1000

        msg = response.choices[0].message
        assert msg.tool_calls, "Le LLM n'a pas appelé de tool!"

        call = msg.tool_calls[0]
        args = json.loads(call.function.arguments)
        print(f"  LLM write: {call.function.name}({json.dumps(args, ensure_ascii=False)[:100]})")
        print(f"  Temps LLM: {llm_ms:.0f} ms")

        assert call.function.name == "write"

        # 3. Exécuter dans le daemon
        try:
            body = await execute(client, "filesystem", call.function.name, args)
            assert body["success"] is True
            print(f"  Daemon write: {body['data']}")

            # 4. Vérifier le contenu
            body = await execute(client, "filesystem", "read", {"path": tmp_path})
            assert body["success"] is True
            assert "Hello from DeepSeek!" in body["data"]["content"]
            print(f"  Daemon read: fichier OK, {body['data']['total_lines']} lignes")
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_agent_handles_bad_params(self, client: AsyncClient):
        """Le LLM reçoit l'erreur propre et corrige."""
        from openai import OpenAI

        llm = OpenAI(
            api_key=DEEPSEEK_KEY,
            base_url="https://api.deepseek.com",
        )

        # Simuler un appel avec mauvais params (edit sans old_string)
        body = await execute(client, "filesystem", "edit", {
            "path": "/tmp/nonexistent.txt",
            "new_string": "test",
        })
        assert body["success"] is False
        error_msg = body["error"]
        print(f"\n  Erreur renvoyée: {error_msg[:120]}")

        # Donner l'erreur au LLM et voir s'il comprend
        tools = [{
            "type": "function",
            "function": {
                "name": "edit",
                "description": "Replace text in a file (old_string -> new_string).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path"},
                        "old_string": {"type": "string", "minLength": 1, "description": "Text to find"},
                        "new_string": {"type": "string", "description": "Replacement text"},
                    },
                    "required": ["path", "old_string", "new_string"],
                },
            },
        }]

        messages = [
            {"role": "system", "content": "Tu es un agent. Corrige l'appel après l'erreur."},
            {"role": "user", "content": "Remplace 'foo' par 'bar' dans /tmp/test.txt"},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "edit", "arguments": json.dumps({
                    "path": "/tmp/test.txt", "new_string": "bar",
                })},
            }]},
            {"role": "tool", "tool_call_id": "call_1", "content": error_msg},
            {"role": "user", "content": "Corrige ton appel."},
        ]

        response = llm.chat.completions.create(
            model="deepseek-chat",
            messages=messages,
            tools=tools,
            tool_choice="auto",
        )

        msg = response.choices[0].message
        if msg.tool_calls:
            call = msg.tool_calls[0]
            args = json.loads(call.function.arguments)
            print(f"  LLM corrigé: {call.function.name}({json.dumps(args, ensure_ascii=False)[:100]})")
            assert "old_string" in args, "Le LLM n'a pas ajouté old_string!"
            print("  Le LLM a compris l'erreur et corrigé son appel.")
        else:
            print(f"  LLM réponse texte: {msg.content[:120]}")

    @pytest.mark.asyncio
    async def test_agent_reads_then_fixes_bug(self, client: AsyncClient):
        """Scénario réel: le LLM lit un fichier bugué, comprend le contenu, corrige."""
        from openai import OpenAI

        # 1. Créer un fichier Python avec un bug réel
        buggy_code = '''\
"""Module de calcul de prix avec réductions."""


def calculate_discount(price: float, discount_percent: float) -> float:
    """Applique une réduction au prix.

    Args:
        price: Prix de base en euros.
        discount_percent: Pourcentage de réduction (0-100).

    Returns:
        Prix après réduction.
    """
    if discount_percent < 0 or discount_percent > 100:
        raise ValueError(f"Réduction invalide: {discount_percent}%")
    return price * discount_percent / 100  # BUG: devrait être price * (1 - discount_percent/100)


def calculate_total(items: list[dict]) -> float:
    """Calcule le total d'un panier.

    Args:
        items: Liste de dicts avec 'price', 'quantity', et optionnel 'discount'.

    Returns:
        Total en euros.
    """
    total = 0.0
    for item in items:
        price = item["price"]
        qty = item["quantity"]
        discount = item.get("discount", 0)
        subtotal = calculate_discount(price * qty, discount)
        total += subtotal
    return round(total, 2)


def format_receipt(items: list[dict]) -> str:
    """Génère un ticket de caisse."""
    lines = ["=== TICKET DE CAISSE ==="]
    for item in items:
        price = item["price"]
        qty = item["quantity"]
        discount = item.get("discount", 0)
        subtotal = calculate_discount(price * qty, discount)
        lines.append(f"  {item['name']:.<30} {subtotal:>8.2f} EUR")
    lines.append(f"  {'TOTAL':.<30} {calculate_total(items):>8.2f} EUR")
    lines.append("========================")
    return "\\n".join(lines)
'''

        with tempfile.NamedTemporaryFile(
            suffix=".py", delete=False, mode="w", encoding="utf-8",
        ) as f:
            f.write(buggy_code)
            path = f.name

        try:
            # 2. Récupérer les tools filesystem depuis le daemon
            r = await client.get("/api/modules/filesystem")
            manifest = r.json()
            tools = _build_tools_from_manifest(manifest["actions"])

            llm = OpenAI(
                api_key=DEEPSEEK_KEY,
                base_url="https://api.deepseek.com",
            )

            messages = [
                {"role": "system", "content": (
                    "Tu es un agent de développement. Tu as accès à des outils filesystem. "
                    "Pour corriger du code, tu dois d'abord LIRE le fichier avec l'outil 'read', "
                    "analyser le contenu, puis utiliser 'edit' pour corriger. "
                    "Utilise old_string/new_string pour faire des modifications chirurgicales."
                )},
                {"role": "user", "content": (
                    f"Le fichier {path} contient un bug dans calculate_discount: "
                    f"quand on applique 20% de réduction sur 100€, ça donne 20€ au lieu de 80€. "
                    f"Lis le fichier et corrige le bug."
                )},
            ]

            # --- Tour 1: le LLM doit appeler read ---
            print("\n  --- Tour 1: lecture du fichier ---")
            t0 = time.perf_counter()
            response = llm.chat.completions.create(
                model="deepseek-chat",
                messages=messages,
                tools=tools,
                tool_choice="auto",
            )
            t1 = time.perf_counter()

            msg = response.choices[0].message
            assert msg.tool_calls, "Tour 1: le LLM n'a pas appelé de tool!"
            call = msg.tool_calls[0]
            args = json.loads(call.function.arguments)
            print(f"  LLM appelle: {call.function.name}({json.dumps(args, ensure_ascii=False)[:80]})")
            print(f"  Temps LLM: {(t1-t0)*1000:.0f} ms")

            # Exécuter dans le daemon
            body = await execute(client, "filesystem", call.function.name, args)
            assert body["success"] is True

            # Afficher ce que le LLM voit
            if call.function.name == "read":
                content = body["data"]["content"]
                print(f"  Le LLM voit {body['data']['total_lines']} lignes avec numéros")
                # Montrer les premières lignes
                for line in content.split("\n")[:5]:
                    print(f"    {line}")
                print("    ...")

            # Ajouter la réponse au contexte
            messages.append(msg.model_dump())
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps(body["data"], ensure_ascii=False),
            })

            # --- Tour 2: le LLM doit appeler edit avec la correction ---
            print("\n  --- Tour 2: correction du bug ---")
            t0 = time.perf_counter()
            response = llm.chat.completions.create(
                model="deepseek-chat",
                messages=messages,
                tools=tools,
                tool_choice="auto",
            )
            t2 = time.perf_counter()

            msg = response.choices[0].message
            assert msg.tool_calls, "Tour 2: le LLM n'a pas appelé de tool!"
            call = msg.tool_calls[0]
            args = json.loads(call.function.arguments)
            print(f"  LLM appelle: {call.function.name}({json.dumps(args, ensure_ascii=False)[:120]})")
            print(f"  Temps LLM: {(t2-t0)*1000:.0f} ms")

            assert call.function.name == "edit", f"Attendu 'edit', reçu '{call.function.name}'"
            assert "old_string" in args, "Le LLM n'a pas fourni old_string!"
            assert "new_string" in args, "Le LLM n'a pas fourni new_string!"

            # Exécuter l'edit dans le daemon
            body = await execute(client, "filesystem", "edit", args)
            assert body["success"] is True, f"Edit a échoué: {body.get('error')}"
            print(f"  Edit réussi: {body['data']['replacements']} remplacement(s)")
            print(f"  Preview:\n{body['data']['preview']}")

            # --- Vérification: le bug est-il corrigé? ---
            print("\n  --- Vérification ---")
            fixed_code = Path(path).read_text()

            # Le fix doit contenir la formule correcte
            assert "price * discount_percent / 100" not in fixed_code or \
                   "1 - discount_percent" in fixed_code or \
                   "(100 - discount_percent)" in fixed_code, \
                "Le bug n'a pas été corrigé!"

            # Test fonctionnel: exécuter le code corrigé
            ns: dict[str, Any] = {}
            exec(compile(fixed_code, path, "exec"), ns)
            result = ns["calculate_discount"](100.0, 20.0)
            print(f"  calculate_discount(100, 20) = {result}")
            assert result == 80.0, f"Attendu 80.0, obtenu {result}"
            print("  BUG CORRIGÉ avec succès!")

            total_ms = (t1 - t0 + t2 - t0) * 1000
            print(f"\n  Temps total (2 tours LLM): {total_ms:.0f} ms")

        finally:
            Path(path).unlink(missing_ok=True)


# ===========================================================================
# 5. Test LLM DeepSeek + Index: context-aware editing
# ===========================================================================


@pytest.mark.skipif(not DEEPSEEK_KEY, reason="DEEPSEEK_API_KEY not set")
class TestDeepSeekWithIndex:
    """Test que le LLM utilise index.context pour comprendre le code avant d'éditer."""

    @pytest.mark.asyncio
    async def test_index_context_then_edit(self, client: AsyncClient):
        """Scénario complet:
        1. Créer un mini-projet Python avec plusieurs fichiers liés
        2. Register source + scan via index
        3. Le LLM utilise index.query/context pour comprendre le code
        4. Le LLM utilise filesystem.edit pour corriger un bug
        5. Vérifier que le code corrigé fonctionne
        """
        from openai import OpenAI

        with tempfile.TemporaryDirectory() as tmpdir:
            # ── 1. Créer un mini-projet avec des dépendances ──
            (Path(tmpdir) / "models.py").write_text('''\
"""Data models for the shop."""


class Product:
    """A product in the catalog."""

    def __init__(self, name: str, price: float, category: str = "general"):
        self.name = name
        self.price = price
        self.category = category

    def __repr__(self) -> str:
        return f"Product({self.name!r}, {self.price})"


class CartItem:
    """An item in the shopping cart."""

    def __init__(self, product: Product, quantity: int):
        self.product = product
        self.quantity = quantity

    @property
    def subtotal(self) -> float:
        return self.product.price * self.quantity
''')

            (Path(tmpdir) / "pricing.py").write_text('''\
"""Pricing engine — applies discounts and taxes."""

from models import Product, CartItem


def apply_discount(price: float, discount_pct: float) -> float:
    """Apply a percentage discount.

    Args:
        price: Original price in EUR.
        discount_pct: Discount percentage (0-100).

    Returns:
        Discounted price.
    """
    if discount_pct < 0 or discount_pct > 100:
        raise ValueError(f"Invalid discount: {discount_pct}")
    return price * discount_pct / 100  # BUG: should be price * (1 - discount_pct / 100)


def calculate_tax(price: float, tax_rate: float = 0.20) -> float:
    """Add tax to a price."""
    return price * (1 + tax_rate)


def checkout(items: list[CartItem], discount_pct: float = 0) -> dict:
    """Process checkout for a list of cart items.

    Returns dict with subtotal, discount, tax, and total.
    """
    subtotal = sum(item.subtotal for item in items)
    after_discount = apply_discount(subtotal, discount_pct)
    total = calculate_tax(after_discount)
    return {
        "subtotal": round(subtotal, 2),
        "after_discount": round(after_discount, 2),
        "total": round(total, 2),
        "items_count": len(items),
    }
''')

            (Path(tmpdir) / "test_pricing.py").write_text('''\
"""Tests for the pricing engine."""

from models import Product, CartItem
from pricing import apply_discount, checkout


def test_apply_discount():
    """100 EUR with 20% discount should be 80 EUR."""
    result = apply_discount(100.0, 20.0)
    assert result == 80.0, f"Expected 80.0, got {result}"


def test_checkout():
    """Full checkout with discount."""
    items = [
        CartItem(Product("Widget", 50.0), 2),
        CartItem(Product("Gadget", 30.0), 1),
    ]
    result = checkout(items, discount_pct=10)
    assert result["subtotal"] == 130.0
    assert result["after_discount"] == 117.0  # 130 * 0.9
''')

            # ── 2. Register source + scan via daemon API ──
            print("\n  ── Étape 1: Indexation du projet ──")
            body = await execute(client, "index", "register_source", {
                "source_id": "test-shop",
                "module_id": "filesystem",
                "root": tmpdir,
                "scan_pattern": "**/*.py",
            })
            assert body["success"] is True
            print(f"  Source registered: {body['data']['source_id']}")

            body = await execute(client, "index", "scan", {
                "source_id": "test-shop",
            })
            assert body["success"] is True
            scan_data = body["data"]
            print(f"  Scan: {scan_data['files_scanned']} files, {scan_data['total_entries']} entries")

            # Verify index has the right entries
            body = await execute(client, "index", "query", {
                "q": "apply_discount",
                "kind": "function",
            })
            assert body["success"] is True
            assert body["data"]["count"] >= 1
            print(f"  Query 'apply_discount': {body['data']['count']} résultats")

            # ── 3. Build tools for LLM (index + filesystem) ──
            r_index = await client.get("/api/modules/index")
            r_fs = await client.get("/api/modules/filesystem")

            # Only give the LLM the tools it needs
            index_actions = r_index.json()["actions"]
            fs_actions = r_fs.json()["actions"]

            # Filter to relevant actions
            index_tool_names = {"query", "context", "relations"}
            fs_tool_names = {"read", "edit"}

            tools = []
            for a in index_actions:
                if a["name"] in index_tool_names:
                    tools.append({
                        "type": "function",
                        "function": {
                            "name": f"index_{a['name']}",
                            "description": f"[Index module] {a.get('description', '')}",
                            "parameters": a.get("input_schema", {"type": "object", "properties": {}}),
                        },
                    })
            for a in fs_actions:
                if a["name"] in fs_tool_names:
                    tools.append({
                        "type": "function",
                        "function": {
                            "name": f"fs_{a['name']}",
                            "description": f"[Filesystem module] {a.get('description', '')}",
                            "parameters": a.get("input_schema", {"type": "object", "properties": {}}),
                        },
                    })

            print(f"  Tools pour le LLM: {[t['function']['name'] for t in tools]}")

            # ── 4. Multi-turn conversation with LLM ──
            llm = OpenAI(
                api_key=DEEPSEEK_KEY,
                base_url="https://api.deepseek.com",
            )

            messages = [
                {"role": "system", "content": (
                    "You are a coding agent. You have tools for searching an index and editing files.\n"
                    "STRICT WORKFLOW — complete in exactly 3 steps:\n"
                    "  Step 1: Call index_context to understand the target function\n"
                    "  Step 2: Call fs_read to see the exact file content\n"
                    "  Step 3: Call fs_edit to fix the bug (old_string → new_string)\n"
                    "Do NOT make more than 3 tool calls. Act decisively."
                )},
                {"role": "user", "content": (
                    f"The file {tmpdir}/pricing.py has a bug in apply_discount(). "
                    f"The formula `return price * discount_pct / 100` is wrong — "
                    f"it should be `return price * (1 - discount_pct / 100)`. "
                    f"Use index_context to check callers, then fs_read to see the file, then fs_edit to fix it."
                )},
            ]

            all_tool_calls = []
            total_llm_time = 0.0

            for turn in range(8):  # Max 8 turns
                print(f"\n  ── Tour {turn + 1} ──")
                t0 = time.perf_counter()
                response = llm.chat.completions.create(
                    model="deepseek-chat",
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                )
                turn_ms = (time.perf_counter() - t0) * 1000
                total_llm_time += turn_ms

                msg = response.choices[0].message

                if not msg.tool_calls:
                    # LLM is done
                    print(f"  LLM (texte): {(msg.content or '')[:150]}")
                    print(f"  Temps: {turn_ms:.0f} ms")
                    break

                # Process all tool calls
                messages.append(msg.model_dump())

                for call in msg.tool_calls:
                    fn_name = call.function.name
                    args = json.loads(call.function.arguments)
                    all_tool_calls.append(fn_name)
                    print(f"  LLM appelle: {fn_name}({json.dumps(args, ensure_ascii=False)[:100]})")

                    # Route to the right module
                    if fn_name.startswith("index_"):
                        action = fn_name[6:]  # strip "index_"
                        body = await execute(client, "index", action, args)
                    elif fn_name.startswith("fs_"):
                        action = fn_name[3:]  # strip "fs_"
                        body = await execute(client, "filesystem", action, args)
                    else:
                        body = {"success": False, "error": f"Unknown tool: {fn_name}"}

                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(body.get("data", body), ensure_ascii=False)[:4000],
                    })

                    if body.get("success"):
                        print(f"    → OK")
                    else:
                        print(f"    → ERREUR: {body.get('error', '')[:80]}")

                print(f"  Temps tour: {turn_ms:.0f} ms")

            # ── 5. Verify the fix ──
            print(f"\n  ── Vérification finale ──")
            print(f"  Tool calls: {all_tool_calls}")
            print(f"  Temps LLM total: {total_llm_time:.0f} ms")

            # Check that index tools were used
            index_calls = [c for c in all_tool_calls if c.startswith("index_")]
            fs_edit_calls = [c for c in all_tool_calls if c == "fs_edit"]
            print(f"  Index calls: {len(index_calls)}, fs_edit calls: {len(fs_edit_calls)}")
            assert len(index_calls) >= 1, "Le LLM n'a pas utilisé l'index!"
            assert len(fs_edit_calls) >= 1, "Le LLM n'a pas fait d'edit!"

            # Verify the code is actually fixed
            fixed_code = (Path(tmpdir) / "pricing.py").read_text()
            print(f"\n  Code corrigé (apply_discount):")
            for line in fixed_code.split("\n"):
                if "return" in line and "discount" in line:
                    print(f"    {line.strip()}")

            # Functional test
            import importlib.util
            import sys

            # Add tmpdir to path for imports
            sys.path.insert(0, tmpdir)
            try:
                # Force reimport
                for mod_name in ["models", "pricing"]:
                    sys.modules.pop(mod_name, None)

                spec = importlib.util.spec_from_file_location(
                    "pricing_fixed", Path(tmpdir) / "pricing.py",
                )
                mod = importlib.util.module_from_spec(spec)

                # Need models available
                models_spec = importlib.util.spec_from_file_location(
                    "models", Path(tmpdir) / "models.py",
                )
                models_mod = importlib.util.module_from_spec(models_spec)
                sys.modules["models"] = models_mod
                models_spec.loader.exec_module(models_mod)

                spec.loader.exec_module(mod)

                result = mod.apply_discount(100.0, 20.0)
                print(f"\n  apply_discount(100, 20) = {result}")
                assert result == 80.0, f"BUG PAS CORRIGÉ! Attendu 80.0, obtenu {result}"
                print("  BUG CORRIGÉ avec succès!")

                # Test checkout too
                items = [
                    models_mod.CartItem(models_mod.Product("Widget", 50.0), 2),
                    models_mod.CartItem(models_mod.Product("Gadget", 30.0), 1),
                ]
                checkout_result = mod.checkout(items, discount_pct=10)
                print(f"  checkout(130 EUR, -10%) = {checkout_result}")
                assert checkout_result["after_discount"] == 117.0, \
                    f"Checkout broken! after_discount={checkout_result['after_discount']}"
                print("  CHECKOUT OK!")

            finally:
                sys.path.remove(tmpdir)
                for mod_name in ["models", "pricing", "pricing_fixed"]:
                    sys.modules.pop(mod_name, None)
