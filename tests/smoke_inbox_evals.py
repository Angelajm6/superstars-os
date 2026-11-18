#!/usr/bin/env python3
"""The Inbox accuracy test set, in the gate (piece 5, 2026-11-18). Free: Claude's answers are planted.

Every case in tests/inbox_evals/cases.json goes through the real Inbox pipeline (the email stored, sorted, the prompt
built with where things stand, Claude's proposals checked by the server, cards made) on an ISOLATED database with
fictional people. For each case it proves:
  * Claude is told what it must know (prompt_has) and never what it must not (prompt_lacks: another client, a contact's
    phone or email)
  * a wrong proposal is refused by the server with the right reason, so it never becomes a card
  * a right proposal becomes a card with the expected rows
  * a waiting card closes once the record moves on (the proposal is signed, the change made by hand, the file filed)

The same cases run against the real model with tests/inbox_evals/run_live.py (by hand; it costs money).
"""
from __future__ import annotations

import os
os.environ.setdefault("SSC_ALLOW_TEST_SEAMS", "1")
import json
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
_TMP = tempfile.mkdtemp(prefix="ssc_ievl_")

from _isolated_db import build_isolated_sqlite as _build_isolated_sqlite, purge_users, purge  # noqa: E402

if not (os.environ.get("SSC_DB_URL") or "").strip():
    os.environ["SSC_DB_URL"] = _build_isolated_sqlite()
import secret_box  # noqa: E402

os.environ.update({"SSC_DATA_ROOT": str(Path(_TMP) / "dataroot"), "SSC_MAIL_FAKE_DIR": str(Path(_TMP) / "mail"), "SSC_GMAIL_POLL": "0",
                   "SSC_REMINDERS": "0", "SSC_FIELD_ENCRYPTION_KEY": secret_box.generate_key(), "SSC_ASSISTANT_FAKE": "1"})

import auth  # noqa: E402
import claude_agent as ca  # noqa: E402
import db_layer  # noqa: E402
import inbox  # noqa: E402
from inbox_evals import engine  # noqa: E402

TAG = engine.TAG
TRIAGE = json.dumps({"kind": "client_question", "business": True, "needs_action": True, "summary": "eval"})


def cleanup():
    conn = db_layer.connect(pragma_fk=False)
    try:
        ca.ensure(conn)
        inbox.ensure(conn)
        uids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE email LIKE ?", (f"{TAG}.%",)).fetchall()]
        ph = lambda xs: ",".join("?" * len(xs)) or "NULL"  # noqa: E731
        mids = [r["id"] for r in conn.execute(f"SELECT id FROM mail_message WHERE user_id IN ({ph(uids)})", uids).fetchall()]
        purge(conn, "mail_link", "message_id", mids)
        purge(conn, "mail_message", "id", mids)
        chats = [r["id"] for r in conn.execute(f"SELECT id FROM claude_chat WHERE user_id IN ({ph(uids)})", uids).fetchall()]
        purge(conn, "claude_action", "chat_id", chats)
        purge(conn, "claude_chat_message", "chat_id", chats)
        purge(conn, "claude_chat", "id", chats)
        orgs = [r["id"] for r in conn.execute("SELECT id FROM crm_organization WHERE name LIKE 'Ievl %'").fetchall()]
        ests = [r["id"] for r in conn.execute(f"SELECT id FROM estimate WHERE client_org_id IN ({ph(orgs)})", orgs).fetchall()]
        pids = [r["id"] for r in conn.execute(f"SELECT id FROM proposal WHERE estimate_id IN ({ph(ests)})", ests).fetchall()]
        for t in ("proposal_line", "proposal_event"):
            purge(conn, t, "proposal_id", pids)
        purge(conn, "proposal", "id", pids)
        purge(conn, "estimate_ira", "estimate_id", ests)
        purge(conn, "estimate", "id", ests)
        conn.execute("DELETE FROM project_documents WHERE project_code LIKE 'FR-EV-%'")
        conn.execute("DELETE FROM projects WHERE project_code LIKE 'FR-EV-%'")
        if orgs:
            conn.execute(f"DELETE FROM crm_activity WHERE entity_type = 'organization' AND entity_id IN ({ph(orgs)})", orgs)
            conn.execute(f"DELETE FROM crm_contact WHERE org_id IN ({ph(orgs)})", orgs)
        conn.execute("DELETE FROM building WHERE address_line LIKE '%Ievl Terrace%' OR address_line LIKE '%Elsewhere Ave%'")
        conn.execute("DELETE FROM crm_organization WHERE name LIKE 'Ievl %'")
        conn.commit()
        purge_users(conn, uids)
    finally:
        conn.close()


def users(conn) -> dict:
    ids = {}
    for key in ("me", "mate"):
        email = f"{TAG}.{key}@superstarscontracting.com"
        conn.execute("INSERT INTO users (email, password_hash, full_name, role, status, must_reset_password, created_at) "
                     "VALUES (?,?,?,?,'active',0,?)", (email, auth.hash_password("InboxEvalsSmoke2026pw"), f"Ievl {key.title()}", "admin",
                                                       datetime.now().isoformat(timespec="seconds")))
        conn.commit()
        ids[key] = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
        ids[f"{key}_email"] = email
    return ids


def run_case(conn, case: dict, n: int, ids: dict) -> list:
    """Returns the list of problems (empty = the case holds)."""
    refs = engine.build(conn, case, n, ids)
    calls = engine.fill(case.get("calls") or [], refs)
    steps = ([{"tools": [{"name": c["name"], "input": c["input"]} for c in calls]}] if calls else []) + [{"text": "Done."}]
    os.environ["SSC_ASSISTANT_FAKE"] = json.dumps(steps)
    os.environ["SSC_INBOX_TRIAGE_FAKE"] = TRIAGE
    ca._FAKE["step"] = 0
    inbox.process(conn, refs["mail_id"], force=True)
    out = engine.outcome(conn, refs["mail_id"])
    bad = []
    for t in engine.fill(case.get("prompt_has") or [], refs):
        if t not in out["prompt"]:
            bad.append(f"Claude is not told: {t!r}")
    for t in engine.fill(case.get("prompt_lacks") or [], refs):
        if t in out["prompt"]:
            bad.append(f"Claude is told what it must not see: {t!r}")
    if len(out["proposed"]) != len(calls):
        bad.append(f"{len(calls)} planted calls, {len(out['proposed'])} reached the server")
        return bad
    # the live scorer grades a planted wrong proposal as wrong (and caught), a planted right one as present
    r = engine.score_live(case, out)
    must_not = set((case.get("live") or {}).get("must_not") or [])
    for c, p in zip(calls, out["proposed"]):
        if c["name"] in must_not and c["name"] not in r["wrong"]:
            bad.append(f"the live scorer misses the wrong {c['name']}")
        if p["refused"] and c["name"] not in r["caught"]:
            bad.append(f"the live scorer misses that the server caught {c['name']}")
    cards = list(out["cards"])
    ctx = ca.Ctx(conn, {"id": ids["me"], "role": "admin"}, "office")
    for c, p in zip(calls, out["proposed"]):
        if c["expect"].startswith("refused:"):
            words = c["expect"].split(":", 1)[1].strip()
            if not p["refused"] or words not in p["answer"]:
                bad.append(f"{c['name']} should be refused with {words!r}; the server said {p['answer'][:160]!r}")
            continue
        if p["refused"]:
            bad.append(f"{c['name']} should make a card; it was refused: {p['answer'][:160]!r}")
            continue
        card = next((x for x in cards if x["tool"] == c["name"]), None)
        if not card:
            bad.append(f"{c['name']}: no card")
            continue
        cards.remove(card)
        if "rows" in c or "note" in c:
            shown = ca.BY_NAME[c["name"]].card(ctx, json.loads(card["input"]))
            rows = shown.get("count", len(shown.get("fields") or []))
            if "rows" in c and rows != c["rows"]:
                bad.append(f"{c['name']}: {rows} rows on the card, expected {c['rows']}")
            notes = " ".join(r.get("note") or "" for g in shown.get("groups") or [] for r in g["rows"])
            if c.get("note") and c["note"] not in notes:
                bad.append(f"{c['name']}: the card doesn't say {c['note']!r}")
        if case.get("after"):
            engine.then(conn, case, refs)
            ca.sweep_moot(conn)
            row = conn.execute("SELECT status, result FROM claude_action WHERE id = ?", (card["id"],)).fetchone()
            words = case["after"].split(":", 1)[1].strip()
            if row["status"] != "declined" or words not in (row["result"] or ""):
                bad.append(f"the waiting card should close with {words!r}; it is {row['status']} {row['result']!r}")
    return bad


def main() -> int:
    cleanup()
    conn = db_layer.connect(pragma_fk=True)
    failed = {}
    all_cases = engine.cases()
    try:
        ids = users(conn)
        assert len(all_cases) >= 30, "the test set keeps at least 30 cases"
        assert len({c["id"] for c in all_cases}) == len(all_cases), "case ids are unique"
        for n, case in enumerate(all_cases, start=1):
            assert case.get("live"), f"{case['id']}: every case says what the real model must (not) do"
            bad = run_case(conn, case, n, ids)
            print(f"  {'ok ' if not bad else 'BAD'} {case['id']}")
            if bad:
                failed[case["id"]] = bad
    finally:
        conn.close()
        os.environ["SSC_ASSISTANT_FAKE"] = "1"
        os.environ.pop("SSC_INBOX_TRIAGE_FAKE", None)
        cleanup()
        shutil.rmtree(_TMP, ignore_errors=True)
    for cid, bad in failed.items():
        for b in bad:
            print(f"FAIL {cid}: {b}")
    if failed:
        print(f"FAIL: {len(failed)} of {len(all_cases)} inbox cases")
        return 1
    print(f"PASS: inbox accuracy test set ({len(all_cases)} cases)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
