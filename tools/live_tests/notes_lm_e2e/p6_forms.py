"""P6 - Forms: generate, submit (synthetic), analyze.

The CORE flow we never validated end-to-end. We:
  1. Ask the agent to make a feedback form. Assert ``forms/<slug>.json``
     lands AND is valid JSON.
  2. Parse the schema, fabricate plausible field values, POST a
     ``responses/<slug>-<iso>.json`` via direct PUT (mimicking what
     FormViewer.handleSubmit does on Submit).
  3. Send a follow-up user message ("analyze the responses") - assert
     the agent reads ``responses/...`` and produces an analysis that
     references at least one submitted value.
  4. Assert the JSON form schema does NOT contain ``<html>``,
     ``<script>``, ``<form>`` tags (system_prompt forbids HTML).

This is what the user actually does in production. If P6 passes
reliably, the form feature is shippable.
"""
from __future__ import annotations

import json
import logging
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runner import (  # noqa: E402
    APP_ID, Reporter, fetch_workspace_file, list_workspace_files,
    make_client, make_session, send_and_wait,
)


SOURCE_PATH = "attachments/feedback-context.md"
SOURCE_CONTENT = """\
# Customer feedback initiative - 2026

We collected anecdotal complaints about onboarding friction. The
goal of the feedback form is to systematize:
  - clarity rating of the welcome email
  - which step was hardest (account, billing, first project)
  - one open-ended quote

The team will analyze responses weekly.
"""


def _seed(client, session, path: str, content: str) -> bool:
    try:
        r = client._put(
            f"/api/apps/{APP_ID}/sessions/{session.session_id}"
            f"/workspace/files/{path}",
            json={"content": content, "auto_approve": True, "source": "user"},
        )
        return r.status_code == 200
    except Exception:
        return False


def _fabricate_values(schema: dict) -> dict:
    """Build a plausible response dict from a form schema.
    Covers text / textarea / number / range / select / radio /
    multiselect / checkbox / sections / groups (1 entry).
    """
    out: dict = {}

    def visit(fields: list[dict], target: dict) -> None:
        for f in fields or []:
            ftype = f.get("type", "text")
            fid = f.get("id")
            if not fid:
                continue
            if ftype == "section":
                visit(f.get("fields") or [], target)
                continue
            if ftype == "group":
                inner: dict = {}
                visit(f.get("fields") or [], inner)
                target[fid] = [inner]
                continue
            if ftype in ("text", "email", "url", "tel", "textarea"):
                target[fid] = f"test-{fid}"
            elif ftype == "number":
                target[fid] = (f.get("min") or 1)
            elif ftype == "range":
                lo = f.get("min", 0); hi = f.get("max", 10)
                target[fid] = (int(lo) + int(hi)) // 2
            elif ftype in ("select", "radio"):
                opts = f.get("options") or []
                first = opts[0] if opts else None
                if isinstance(first, dict):
                    target[fid] = first.get("value")
                else:
                    target[fid] = first or "a"
            elif ftype == "multiselect":
                opts = f.get("options") or []
                vals = []
                for o in opts[:1]:
                    vals.append(o.get("value") if isinstance(o, dict) else o)
                target[fid] = vals
            elif ftype == "checkbox":
                target[fid] = True
            elif ftype in ("date", "datetime-local", "time"):
                target[fid] = "2026-05-18"

    visit(schema.get("fields") or [], out)
    return out


def run() -> int:
    print("=== P6 FORMS ===")
    report = Reporter("P6 forms")
    client = make_client()
    session = make_session(client, label="p6")
    print(f"  session: {session.session_id}")

    # Seed context (not strictly required but helps the agent ground)
    _seed(client, session, SOURCE_PATH, SOURCE_CONTENT)

    # ── 1. Ask for a form ─────────────────────────────────────────
    res = send_and_wait(
        client, session,
        message=(
            "Crée moi un formulaire de feedback avec name (text required), "
            "email (email required), clarity (range 0-10), hardest_step "
            "(radio: account / billing / first_project), and a free "
            "comment textarea. Use kebab-case for the file id."
        ),
        timeout=240,
    )
    if not res["ok"]:
        report.fail("form:reply", f"err={res['error']}")
        return report.summary()

    # Find the new forms/<slug>.json
    files = list_workspace_files(client, session)
    form_files = [
        f for f in files
        if f.startswith("forms/") and f.endswith(".json")
    ]
    if not form_files:
        report.fail(
            "form:file-created",
            f"no forms/*.json found  files={files}  "
            f"reply={res['assistant_text'][:200]!r}",
        )
        return report.summary()
    form_path = form_files[0]
    report.ok("form:file-created", form_path)

    # ── 2. Parse JSON, check no HTML ──────────────────────────────
    raw = fetch_workspace_file(client, session, form_path) or ""
    if any(tag in raw.lower() for tag in ("<html", "<form", "<script", "<style")):
        report.fail(
            "form:no-html-tags",
            f"HTML tag in form schema {form_path!r}",
        )
        return report.summary()
    report.ok("form:no-html-tags", "no <html/form/script/style> in schema")

    try:
        schema = json.loads(raw)
    except json.JSONDecodeError as exc:
        report.fail("form:valid-json", f"{exc}  raw={raw[:200]!r}")
        return report.summary()
    report.ok("form:valid-json", f"id={schema.get('id')!r}")

    # ── 3. Fabricate + submit a response ──────────────────────────
    values = _fabricate_values(schema)
    if not values:
        report.fail("form:fabricate-values", "schema produced empty values")
        return report.summary()
    report.ok("form:fabricate-values", f"{list(values.keys())}")

    slug = schema.get("id") or form_path.replace("forms/", "").replace(".json", "")
    iso = datetime.now(timezone.utc).isoformat().replace(":", "-").replace(".", "-")
    resp_path = f"responses/{slug}-{iso}.json"
    payload = {
        "form_id": slug,
        "form_path": form_path,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "values": values,
    }
    try:
        r = client._put(
            f"/api/apps/{APP_ID}/sessions/{session.session_id}"
            f"/workspace/files/{resp_path}",
            json={
                "content": json.dumps(payload, indent=2),
                "auto_approve": True,
                "source": "user",
            },
        )
        if r.status_code != 200:
            report.fail("form:submit", f"status={r.status_code}")
            return report.summary()
        report.ok("form:submit", resp_path)
    except Exception as exc:
        report.fail("form:submit", f"{exc!r}")
        return report.summary()

    # ── 4. Ask the agent to analyze the response ──────────────────
    res = send_and_wait(
        client, session,
        message=(
            f"I just submitted form `{form_path}`. The response is at "
            f"`{resp_path}`. Read it with WsRead and analyze my answers."
        ),
        timeout=180,
    )
    if not res["ok"]:
        report.fail("form:analyze", f"err={res['error']}")
    else:
        text = res["assistant_text"]
        # The agent must reference at least one submitted value or
        # field name to prove it read the response.
        text_l = text.lower()
        hit = any(
            (str(v).lower() in text_l) if isinstance(v, (str, int, float))
            else any(str(x).lower() in text_l for x in (v if isinstance(v, list) else [v]))
            for v in values.values()
        )
        has_field = any(k.lower() in text_l for k in values.keys())
        if hit or has_field:
            report.ok("form:analyze", "agent referenced submitted values/fields")
        else:
            report.fail(
                "form:analyze",
                f"agent didn't reference any value or field  "
                f"reply={text[:200]!r}",
            )

    return report.summary()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sys.exit(run())
