"""Claude updates a client's record from an email, as one card the person approves (2026-11-18).

Accepted prototype: https://claude.ai/artifact/JWDdszQjNdNqPyPcnKN3st. Claude proposes a flat list of changes
(`update_client_record`): a contact's name / email / phone / title / roles, a new contact, or the client's own
phone / email / website / address / billing email. This module turns that into the card's rows, each showing what is
on file NOW next to the new value, and applies only the rows the person kept.

Rules (Angela, 2026-11-18):
  * it only adds or changes: never deletes a contact, never empties a field, never removes a role
  * never payment terms, deposit, retainage or the primary contact (not in the field list at all)
  * a row whose value is already on file is dropped; a card with no rows left is "already up to date"
  * a "new" contact whose email or name is already on this client becomes a change to that contact
  * Undo puts the old values back; a contact the card added is removed again (made inactive when something uses it)
"""
from __future__ import annotations

import re
from typing import Optional

CLIENT_FIELDS = {"phone": "Main phone", "email": "Main email", "website": "Website", "address_line": "Address",
                 "city": "City", "state": "State", "zip": "ZIP", "billing_email": "Billing email"}
CONTACT_FIELDS = {"full_name": "Name", "email": "Email", "phone": "Phone", "title": "Title", "roles": "Roles"}
FIELDS = sorted(set(CLIENT_FIELDS) | set(CONTACT_FIELDS))
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _rows(conn, sql, params=()) -> list:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _norm(field: str, v) -> str:
    v = re.sub(r"\s+", " ", str(v or "")).strip()
    return v.lower() if field in ("email", "billing_email") else v


def _same(field: str, a, b) -> bool:
    if field == "phone":
        da, db = re.sub(r"\D", "", str(a or "")), re.sub(r"\D", "", str(b or ""))
        return bool(da) and da[-10:] == db[-10:]
    if field in ("website",):
        strip = lambda s: re.sub(r"^https?://(www\.)?", "", _norm(field, s).lower()).rstrip("/")  # noqa: E731
        return strip(a) == strip(b)
    return _norm(field, a).lower() == _norm(field, b).lower()


def _role_keys(v) -> list:
    """'Billing / AP, signs' or ['billing'] -> ['billing', 'signs'] (labels or keys; unknown dropped)."""
    import crm
    by_label = {lab.lower(): k for k, lab in crm.CONTACT_ROLES.items()}
    items = v if isinstance(v, list) else re.split(r"[,;]", str(v or ""))
    out = []
    for x in items:
        x = str(x).strip().lower()
        k = x if x in crm.CONTACT_ROLES else by_label.get(x)
        if k and k not in out:
            out.append(k)
    return out


def _role_labels(keys) -> str:
    import crm
    return ", ".join(crm.CONTACT_ROLES.get(k, k) for k in keys)


def _valid(field: str, v: str) -> None:
    if field in ("email", "billing_email") and not _EMAIL_RE.match(v):
        raise ValueError(f"'{v}' doesn't look like an email address")


def plan(conn, inp: dict) -> dict:
    """{org, groups: [{title, meta, is_new, rows: [{key, field, label, old, new, why, roles?}]}]} against the record NOW.
    Raises LookupError / ValueError for an impossible card (no such client, a contact of another client)."""
    import crm
    org = crm.get_org(conn, int(inp.get("client_org_id") or 0))
    if not org:
        raise LookupError("no client with that id; use find to look it up")
    contacts = {c["id"]: c for c in crm.list_contacts(conn, org_id=org["id"])}
    by_email = {_norm("email", c.get("email")): cid for cid, c in contacts.items() if c.get("email")}
    by_name = {_norm("full_name", c.get("full_name")).lower(): cid for cid, c in contacts.items() if c.get("full_name")}

    # who each change is for: 'org', an existing contact id, or a new person (by name)
    items, new_people = [], {}
    for ch in inp.get("changes") or []:
        if not isinstance(ch, dict):
            continue
        field = str(ch.get("field") or "").strip()
        val = ch.get("value")
        why = str(ch.get("why") or "").strip()[:160]
        if ch.get("contact_id"):
            cid = int(ch["contact_id"])
            if cid not in contacts:
                raise ValueError(f"contact {cid} is not on {org['name']}")
            items.append((("c", cid), field, val, why))
        elif str(ch.get("new_contact") or "").strip():
            name = _norm("full_name", ch["new_contact"])
            new_people.setdefault(name.lower(), {"name": name, "rows": []})["rows"].append((field, val, why))
        else:
            items.append((("org", org["id"]), field, val, why))
    # a "new" person already on file (same email, else same name) is that contact
    names = []   # the people really new, in order: n0, n1, ...
    for low, p in new_people.items():
        email = next((_norm("email", v) for f, v, _ in p["rows"] if f == "email"), "")
        cid = by_email.get(email) if email else None
        cid = cid or by_name.get(low)
        if cid:
            items += [(("c", cid), f, v, w) for f, v, w in p["rows"] if f != "full_name"]
        else:
            i = len(names)
            names.append(p["name"])
            items.append((("n", i), "full_name", p["name"], ""))
            items += [(("n", i), f, v, w) for f, v, w in p["rows"] if f != "full_name"]

    groups, order = {}, []
    for (kind, ref), field, val, why in items:
        allowed = CLIENT_FIELDS if kind == "org" else CONTACT_FIELDS
        if field not in allowed:
            raise ValueError(f"'{field}' can't be changed here; " + ("a client's " if kind == "org" else "a contact's ")
                             + "fields are: " + ", ".join(allowed))
        gk = f"{kind}{ref}" if kind != "org" else "org"
        if gk not in groups:
            if kind == "org":
                g = {"title": org["name"], "meta": "client", "is_new": False}
            elif kind == "c":
                c = contacts[ref]
                g = {"title": c.get("full_name") or "Contact", "meta": "contact", "is_new": False}
            else:
                g = {"title": names[ref], "meta": "new contact", "is_new": True}
            g.update(kind=kind, ref=ref, rows=[])
            groups[gk] = g
            order.append(gk)
        g = groups[gk]
        cur = org if kind == "org" else contacts.get(ref, {}) if kind == "c" else {}
        old = cur.get(field)
        key = f"{gk}.{field}"
        if any(r["key"] == key for r in g["rows"]):
            continue
        if field == "roles":
            have = _role_keys(old)
            want = [k for k in _role_keys(val) if k not in have]
            if not want:
                continue
            g["rows"].append({"key": key, "field": field, "label": CONTACT_FIELDS[field], "old": _role_labels(have),
                              "new": ",".join(have + want), "shown": _role_labels(have + want), "why": why, "roles": True})
            continue
        new = _norm(field, val)
        if not new or (old and _same(field, old, new)):
            continue
        _valid(field, new)
        label = (CLIENT_FIELDS if kind == "org" else CONTACT_FIELDS)[field]
        g["rows"].append({"key": key, "field": field, "label": label, "old": old or "", "new": new, "why": why})
    out = [groups[k] for k in order if groups[k]["rows"] and not (groups[k]["is_new"] and len(groups[k]["rows"]) == 1)]
    out.sort(key=lambda g: {"c": 0, "n": 1, "org": 2}[g["kind"]])   # people first, the client last (the prototype)
    return {"org": org, "groups": out}


def count(p: dict) -> int:
    return sum(len(g["rows"]) for g in p["groups"])


def check(ctx, inp) -> None:
    p = plan(ctx.conn, inp)
    if not count(p):
        raise ValueError(f"{p['org']['name']}'s record already has all of this; nothing to propose")


def moot(conn, inp) -> Optional[str]:
    try:
        p = plan(conn, inp)
    except LookupError:
        return "That client was deleted"
    return None if count(p) else f"{p['org']['name']}'s record already matches this email"


def card(ctx, inp) -> dict:
    """The drawer draws `groups` (old → new rows with a tick box); `fields` lists the same keys so Approve accepts
    the person's edits (claude_agent._decide only takes keys a card shows)."""
    try:
        p = plan(ctx.conn, inp)
    except (LookupError, ValueError):
        return {"title": "Update client record", "fields": []}
    import crm
    groups = [{"title": g["title"], "meta": g["meta"], "is_new": g["is_new"],
               "rows": [{k: r.get(k) for k in ("key", "label", "old", "new", "why", "roles")} for r in g["rows"]]}
              for g in p["groups"]]
    return {"title": "Update client record", "client": p["org"]["name"], "groups": groups,
            "role_options": [{"value": k, "label": lab} for k, lab in crm.CONTACT_ROLES.items()],
            "fields": [{"key": r["key"], "label": r["label"], "type": "change", "value": r["new"]}
                       for g in p["groups"] for r in g["rows"]]}


def _kept(inp: dict, r: dict) -> Optional[str]:
    """The value the person approved for one row: their edit, nothing when they unticked it, else Claude's."""
    v = inp.get(r["key"], r["new"]) if r["key"] in inp else r["new"]
    v = _norm(r["field"], v) if v is not None else None
    return v or None


def execute(ctx, inp) -> dict:
    import crm
    p = plan(ctx.conn, inp)
    org = p["org"]
    before = {"org": {}, "contacts": {}, "created": []}
    lines, done = [], 0
    for g in p["groups"]:
        vals = {}
        for r in g["rows"]:
            v = _kept(inp, r)
            if v is None:
                continue
            if r.get("roles"):
                old = _role_keys(r["old"].split(", ") if r["old"] else [])
                v = ",".join(old + [k for k in _role_keys(v.split(",")) if k not in old])   # adds only
                if v == ",".join(old):
                    continue
            else:
                _valid(r["field"], v)
            vals[r["field"]] = v
        if not vals:
            continue
        if g["kind"] == "org":
            cur = crm.get_org(ctx.conn, org["id"])
            before["org"].update({k: cur.get(k) for k in vals})
            crm.update_org(ctx.conn, org["id"], actor_user_id=ctx.uid, **vals)
        elif g["kind"] == "c":
            cur = crm.get_contact(ctx.conn, g["ref"])
            before["contacts"][str(g["ref"])] = {k: cur.get(k) for k in vals}
            crm.update_contact(ctx.conn, g["ref"], **vals)
        else:
            name = vals.pop("full_name", None)
            if not name:
                continue
            roles = vals.pop("roles", None)
            cid = crm.create_contact(ctx.conn, org_id=org["id"], full_name=name, email=vals.get("email"),
                                     phone=vals.get("phone"), title=vals.get("title"), roles=roles.split(",") if roles else None,
                                     created_by=ctx.uid)
            before["created"].append(cid)
            vals["full_name"] = name
            if roles:
                vals["roles"] = roles
        n = len(vals)
        done += n
        shown = [("added" if g["kind"] == "n" else CONTACT_FIELDS.get(k) if g["kind"] != "org" else CLIENT_FIELDS[k]).lower()
                 for k in (["full_name"] if g["kind"] == "n" else vals)]
        lines.append(f"{g['title']}: " + (", ".join(dict.fromkeys(shown))))
    if not done:
        raise ValueError("every change was unticked; nothing to save (Decline instead)")
    who = ""
    if inp.get("mail_id"):
        try:
            import inbox
            from email.utils import parseaddr
            m = inbox.message(ctx.conn, int(inp["mail_id"]), ctx.uid)
            if m:
                who = parseaddr(m["from"])[0] or parseaddr(m["from"])[1]
                inbox.link_record(ctx.conn, m["id"], "client", org["id"], ctx.uid)
        except Exception:
            ctx.conn.rollback()
    crm.add_activity(ctx.conn, entity_type="organization", entity_id=org["id"], activity_type="system", author_user_id=ctx.uid,
                     summary=(f"Updated from {who}'s email: " if who else "Updated from an email: ") + "; ".join(lines))
    return {"id": org["id"], "summary": f"{org['name']} updated · {done} change{'s' if done != 1 else ''}",
            "lines": lines, "link": f"/company#clients/{org['id']}", "undo": True, "before": before}


def undo(ctx, action, result) -> None:
    import crm
    b = result.get("before") or {}
    org_id = int(result["id"])
    if b.get("org"):
        crm.update_org(ctx.conn, org_id, actor_user_id=ctx.uid, **{k: v for k, v in b["org"].items()})
    for cid, old in (b.get("contacts") or {}).items():
        fields = dict(old)
        if "full_name" in fields and not fields["full_name"]:
            fields.pop("full_name")
        if "roles" in fields:
            fields["roles"] = (fields["roles"] or "").split(",") if fields["roles"] else None
            if not fields["roles"]:
                ctx.conn.execute("UPDATE crm_contact SET roles = NULL WHERE id = ?", (int(cid),))
                fields.pop("roles")
        crm.update_contact(ctx.conn, int(cid), **fields)
    for cid in b.get("created") or []:
        used = any(_rows(ctx.conn, f"SELECT 1 FROM {t} WHERE contact_id = ? LIMIT 1", (cid,)) for t in ("estimate", "proposal"))
        ctx.conn.execute("UPDATE mail_message SET contact_id = NULL WHERE contact_id = ?", (cid,))
        if used:
            crm.update_contact(ctx.conn, cid, status="inactive")
        else:
            ctx.conn.execute("DELETE FROM crm_contact WHERE id = ? AND org_id = ?", (cid, org_id))
    ctx.conn.commit()
    crm.add_activity(ctx.conn, entity_type="organization", entity_id=org_id, activity_type="system", author_user_id=ctx.uid,
                     summary="Undid the update from an email: " + "; ".join(result.get("lines") or []))


def today_text(conn, inp) -> str:
    """The dashboard's Needs you card: what will change, one line per row."""
    try:
        p = plan(conn, inp)
    except (LookupError, ValueError):
        return ""
    out = []
    for g in p["groups"]:
        for r in g["rows"]:
            new = r.get("shown") or r["new"]
            out.append(f"{g['title']} · {r['label']}: " + (f"{r['old']} → {new}" if r["old"] else new))
    return "\n".join(out)

