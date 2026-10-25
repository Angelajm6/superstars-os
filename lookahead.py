"""Two-Week Look-Ahead (#255) — auto-draft from the Drop Plan + the editable
activity-row schedule.

The look-ahead is NOT blocked on a master schedule: it AUTO-DRAFTS planned
activity bars from the Drop Plan (each active/upcoming drop's next stages,
projected across the next 10 working days using current stage + start + the
stage-template target durations) into source='auto' rows. The super then
adjusts by dragging — which flips a row to source='manual' (locked). A
Refresh re-projects ONLY the untouched auto rows; manual + custom rows survive.

LOCAL dates only (CLAUDE.md dates rule): planned_start / planned_finish are
'YYYY-MM-DD' computed with local date arithmetic, never UTC.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Optional


# ---------- working-day helpers (Mon-Fri) ----------

def working_days_window(start: date, n_days: int = 10) -> list[date]:
    """The first n_days working days (Mon-Fri) at or after `start`."""
    out: list[date] = []
    cur = start
    while cur.weekday() >= 5:
        cur += timedelta(days=1)
    while len(out) < n_days:
        if cur.weekday() < 5:
            out.append(cur)
        cur += timedelta(days=1)
    return out


def _next_working_day(d: date) -> date:
    cur = d
    while cur.weekday() >= 5:
        cur += timedelta(days=1)
    return cur


def _add_working_days(d: date, n: int) -> date:
    """The date n working days after `d` (n>=0; counts d's own day as day 0
    once d has been advanced to a working day)."""
    cur = _next_working_day(d)
    added = 0
    while added < n:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            added += 1
    return cur


def _iso(d: date) -> str:
    return d.isoformat()


def _drop_label(drop_id: str, elevation: Optional[str]) -> str:
    """'FR-BX-001-DP3' + 'North' -> 'DP-3 · North'."""
    tag = drop_id
    if "-DP" in drop_id:
        tag = "DP-" + drop_id.rsplit("-DP", 1)[-1]
    return f"{tag} · {elevation}" if elevation else tag


# ---------- AUTO-DRAFT ----------

def draft_lookahead(conn: sqlite3.Connection, project_code: str, window_start_iso: str,
                    until_iso: Optional[str] = None) -> int:
    """(Re)project source='auto' rows from the Drop Plan for the 10-working-day
    window — or, with `until_iso`, from the window start through that day (so a
    window viewed weeks ahead is still projected from THIS week's progress).
    Manual + custom rows are NEVER touched. Runs in the caller's transaction (no
    commit). Returns the number of auto rows drafted."""
    conn.row_factory = sqlite3.Row
    window = working_days_window(date.fromisoformat(window_start_iso), 10)
    if until_iso:
        until = date.fromisoformat(until_iso)
        while window[-1] < until:
            window += working_days_window(window[-1] + timedelta(days=1), 1)
    win_start, win_end = window[0], window[-1]
    win_start_iso, win_end_iso = _iso(win_start), _iso(win_end)

    # A re-draft RECONCILES the auto rows: a stage still projected keeps its row (and id,
    # so an open edit never points at a vanished row), moved to its new dates; rows no
    # longer projected go. A stage someone edited (source='manual') is never re-projected:
    # it holds its place and the drop's later stages follow its finish.
    existing = {}
    for r in conn.execute(
        "SELECT id, drop_id, name, activity_type, source_step, planned_start, planned_finish, sort_order "
        "FROM lookahead_activity WHERE project_code=? AND source='auto' ORDER BY id", (project_code,)
    ).fetchall():
        existing.setdefault(_draft_key(r["drop_id"], r["activity_type"], r["source_step"], r["name"], r["planned_start"]),
                            []).append(dict(r))
    manual = {}
    for r in conn.execute(
        "SELECT drop_id, source_step, activity_type, name, planned_start, planned_finish FROM lookahead_activity "
        "WHERE project_code=? AND source='manual'", (project_code,)
    ).fetchall():
        manual[_draft_key(r["drop_id"], r["activity_type"], r["source_step"], r["name"], r["planned_start"])] = dict(r)
    kept = set()

    # Project stage-template steps (name + target duration + signoff gate).
    trow = conn.execute("SELECT template_id FROM stage_templates WHERE project_code=? LIMIT 1",
                        (project_code,)).fetchone()
    steps: dict[int, dict] = {}
    if trow:
        for s in conn.execute(
            "SELECT step_no, name, default_working_days, is_signoff_gate "
            "FROM stage_template_steps WHERE template_id=? ORDER BY step_no",
            (trow["template_id"],)
        ).fetchall():
            steps[s["step_no"]] = dict(s)
    step_nos = sorted(steps.keys())

    # Focus the look-ahead on CURRENT + IMMINENT work: every active drop, plus
    # the next 2 not-started by sequence ("starts soon"). Far-future drops would
    # flood the window with overlapping projections.
    drops_all = conn.execute(
        "SELECT drop_id, elevation, sequence_no, lifecycle FROM drops "
        "WHERE project_code=? AND lifecycle IN ('not_started','scaffold_active','awaiting_paint') "
        "ORDER BY sequence_no", (project_code,)
    ).fetchall()
    active = [d for d in drops_all if d["lifecycle"] in ("scaffold_active", "awaiting_paint")]
    soon = [d for d in drops_all if d["lifecycle"] == "not_started"][:2]
    drops = active + soon

    sort = 0
    drafted = 0

    def _ins(drop_id, name, atype, ps, pf, source_step):
        nonlocal sort, drafted
        key = _draft_key(drop_id, atype, source_step, name, ps)
        if key in manual:
            return
        mine = existing.get(key) or []
        if mine:
            row = mine.pop(0)
            kept.add(row["id"])
            if (row["planned_start"], row["planned_finish"], row["sort_order"]) != (ps, pf, sort):
                conn.execute("UPDATE lookahead_activity SET planned_start=?, planned_finish=?, sort_order=?, "
                             "updated_at=CURRENT_TIMESTAMP WHERE id=?", (ps, pf, sort, row["id"]))
        else:
            conn.execute(
                "INSERT INTO lookahead_activity "
                "(project_code, drop_id, name, activity_type, planned_start, planned_finish, "
                " source, source_step, sort_order) VALUES (?,?,?,?,?,?, 'auto', ?, ?)",
                (project_code, drop_id, name, atype, ps, pf, source_step, sort))
        sort += 1
        drafted += 1

    for d in drops:
        drop_id = d["drop_id"]
        st = {r["step_no"]: dict(r) for r in conn.execute(
            "SELECT step_no, status, started_on FROM drop_stage_status WHERE drop_id=?",
            (drop_id,)).fetchall()}
        # current step = first not complete / not n_a
        cursor = None
        for sn in step_nos:
            if st.get(sn, {}).get("status") in ("complete", "n_a"):
                continue
            cursor = sn
            break
        if cursor is None:
            continue
        # projection start: an in_progress current step uses its started_on
        # (clamped to the window start); otherwise the window start.
        cur_st = st.get(cursor, {})
        if cur_st.get("status") == "in_progress" and cur_st.get("started_on"):
            try:
                proj = max(date.fromisoformat(cur_st["started_on"]), win_start)
            except ValueError:
                proj = win_start
        else:
            proj = win_start
        proj = _next_working_day(proj)

        for sn in step_nos:
            if sn < cursor:
                continue
            if st.get(sn, {}).get("status") in ("complete", "n_a"):
                continue
            tmpl = steps[sn]
            dur = max(1, int(round(tmpl.get("default_working_days") or 1)))
            pstart = proj
            if pstart > win_end:
                break  # remaining stages fall beyond the window
            held = manual.get(_draft_key(drop_id, "stage", sn, tmpl["name"], None))
            if held:   # edited by hand: keep it where it was put, follow on from its finish
                try:
                    proj = _add_working_days(date.fromisoformat(held["planned_finish"]), 1)
                except ValueError:
                    proj = _add_working_days(pstart, dur)
                continue
            pfinish = _add_working_days(pstart, dur - 1)
            _ins(drop_id, tmpl["name"], "stage", _iso(pstart), _iso(pfinish), sn)
            # an inspection milestone on the signoff gate's finish day
            if tmpl.get("is_signoff_gate") and pfinish <= win_end:
                _ins(drop_id, f"{tmpl['name']} — inspection", "inspection",
                     _iso(pfinish), _iso(pfinish), sn)
            proj = _add_working_days(pfinish, 1)

    # Real deliveries + inspections in the window -> milestones in the General
    # (no-drop) group. These tables are project-level (no drop link).
    for r in conn.execute(
        "SELECT date, material, supplier FROM deliveries "
        "WHERE project_code=? AND date BETWEEN ? AND ? ORDER BY date",
        (project_code, win_start_iso, win_end_iso)
    ).fetchall():
        name = (r["material"] or r["supplier"] or "Delivery")
        _ins(None, f"{name} delivery", "delivery", r["date"], r["date"], None)
    for r in conn.execute(
        "SELECT date, type, agency FROM inspections "
        "WHERE project_code=? AND date BETWEEN ? AND ? ORDER BY date",
        (project_code, win_start_iso, win_end_iso)
    ).fetchall():
        name = (r["type"] or r["agency"] or "Inspection")
        _ins(None, f"{name} inspection", "inspection", r["date"], r["date"], None)

    stale = [row["id"] for rows in existing.values() for row in rows if row["id"] not in kept]
    for i in range(0, len(stale), 200):
        part = stale[i:i + 200]
        conn.execute(f"DELETE FROM lookahead_activity WHERE id IN ({','.join('?' * len(part))})", part)
    return drafted


def _draft_key(drop_id, atype, source_step, name, planned_start):
    """What makes two drafted rows "the same activity": a drop's stage (or its sign-off
    inspection) by step; a job-wide delivery / inspection by name and day."""
    if source_step is not None:
        return (drop_id, atype, int(source_step))
    return (drop_id, atype, None, name, planned_start)


# ---------- WINDOW LOAD (grouped, with grid positions for the UI) ----------

def _constraints_by_loc(conn: sqlite3.Connection, project_code: str) -> dict[str, int]:
    """Open/overdue schedule-impact RFI counts keyed by location_id (drop)."""
    today_iso = date.today().isoformat()
    out: dict[str, int] = {}
    for r in conn.execute(
        "SELECT location_id, status, date_response_received "
        "FROM rfi_log WHERE project_code=? AND schedule_impact_flag=1",
        (project_code,)
    ).fetchall():
        status = (r["status"] or "").strip()
        if status in ("Closed", "Void") or r["date_response_received"]:
            continue
        loc = r["location_id"]
        if not loc:
            continue
        out[loc] = out.get(loc, 0) + 1
    return out


def load_window(conn: sqlite3.Connection, project_code: str, window_start_iso: str) -> dict:
    """The grouped look-ahead for the window: day columns, KPI hero, drop groups
    (each with its activity rows + computed grid positions), and the General
    (no-drop) group. Grid positions are 1-based CSS grid-column indices over the
    10 day columns; a bar is start/(end+1), a milestone is the single day."""
    conn.row_factory = sqlite3.Row
    window = working_days_window(date.fromisoformat(window_start_iso), 10)
    day_index = {_iso(d): i + 1 for i, d in enumerate(window)}  # 1..10
    win_start, win_end = window[0], window[-1]
    today_iso = date.today().isoformat()

    days = [{
        "iso": _iso(d), "dow": d.strftime("%a").upper(), "md": d.strftime("%m-%d"),
        "is_today": _iso(d) == today_iso,
    } for d in window]

    def _grid(ps_iso: str, pf_iso: str):
        """Map planned start/finish to (col_start, col_end_exclusive), clipped to
        the 10-day window. Returns None if entirely outside the window."""
        try:
            ps = date.fromisoformat(ps_iso); pf = date.fromisoformat(pf_iso)
        except (TypeError, ValueError):
            return None
        if pf < win_start or ps > win_end:
            return None
        cs = day_index.get(_iso(max(ps, win_start)))
        if cs is None:
            # start lands on a weekend inside the window — snap forward to the next column
            cur = max(ps, win_start)
            while _iso(cur) not in day_index and cur <= win_end:
                cur += timedelta(days=1)
            cs = day_index.get(_iso(cur), 1)
        ce = day_index.get(_iso(min(pf, win_end)))
        if ce is None:
            cur = min(pf, win_end)
            while _iso(cur) not in day_index and cur >= win_start:
                cur -= timedelta(days=1)
            ce = day_index.get(_iso(cur), cs)
        return [cs, max(cs, ce) + 1]

    rows = conn.execute(
        "SELECT id, drop_id, name, activity_type, planned_start, planned_finish, crew, "
        "       source, source_step, notes, sort_order "
        "FROM lookahead_activity WHERE project_code=? ORDER BY sort_order, id",
        (project_code,)
    ).fetchall()

    by_drop: dict[str, list[dict]] = {}
    general: list[dict] = []
    n_deliveries = 0
    for r in rows:
        a = dict(r)
        a["grid"] = _grid(a["planned_start"], a["planned_finish"])
        if a["activity_type"] == "delivery":
            n_deliveries += 1
        if a["drop_id"]:
            by_drop.setdefault(a["drop_id"], []).append(a)
        else:
            general.append(a)

    cons = _constraints_by_loc(conn, project_code)

    drops = conn.execute(
        "SELECT drop_id, elevation, sequence_no, lifecycle FROM drops "
        "WHERE project_code=? ORDER BY (lifecycle='scaffold_active') DESC, sequence_no",
        (project_code,)
    ).fetchall()

    groups = []
    n_behind = 0
    for d in drops:
        acts = by_drop.get(d["drop_id"], [])
        if not acts:
            continue  # only show drops that have look-ahead activity in the window
        # progress: stages complete / total template steps
        total = conn.execute(
            "SELECT COUNT(*) FROM drop_stage_status WHERE drop_id=?", (d["drop_id"],)
        ).fetchone()[0]
        cur_idx = conn.execute(
            "SELECT COALESCE(MIN(step_no), 0) FROM drop_stage_status "
            "WHERE drop_id=? AND status NOT IN ('complete','n_a')", (d["drop_id"],)
        ).fetchone()[0]
        # behind = a stage that should have finished by today is not complete
        behind_days = 0
        bd = conn.execute(
            "SELECT ds.started_on, st.default_working_days FROM drop_stage_status ds "
            "JOIN stage_template_steps st ON st.step_no=ds.step_no "
            "JOIN stage_templates t ON t.template_id=st.template_id AND t.project_code=? "
            "WHERE ds.drop_id=? AND ds.status='in_progress' AND ds.started_on IS NOT NULL "
            "ORDER BY ds.step_no LIMIT 1", (project_code, d["drop_id"])
        ).fetchone()
        if bd and bd["started_on"]:
            try:
                exp_finish = _add_working_days(date.fromisoformat(bd["started_on"]),
                                               max(1, int(round(bd["default_working_days"] or 1))) - 1)
                if exp_finish.isoformat() < today_iso:
                    behind_days = sum(1 for dd in
                                      (exp_finish + timedelta(days=k) for k in range(1, 60))
                                      if dd.weekday() < 5 and dd.isoformat() <= today_iso)
            except ValueError:
                pass
        if d["lifecycle"] == "not_started":
            status = "soon"
        elif 0 < behind_days <= 10:
            status = "behind"; n_behind += 1
        else:
            # >10 working days "behind" means the stage status is stale (started
            # weeks ago, never advanced), not actionable catch-up — show active.
            status = "active"; behind_days = 0
        groups.append({
            "drop_id": d["drop_id"],
            "label": _drop_label(d["drop_id"], d["elevation"]),
            "stage_index": cur_idx, "stage_total": total,
            "status": status, "behind_days": behind_days,
            "constraint_count": cons.get(d["drop_id"], 0),
            "activities": acts,
        })

    n_activities = sum(len(g["activities"]) for g in groups) + len(general)
    return {
        "project_code": project_code,
        "window_start": _iso(win_start), "window_end": _iso(win_end),
        "days": days,
        "kpis": {
            "active_drops": len(groups), "activities": n_activities,
            "deliveries": n_deliveries, "behind": n_behind,
        },
        "groups": groups,
        "general": [dict(g, grid=g["grid"]) for g in general],
    }
