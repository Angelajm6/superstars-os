# Superstars OS — Flask + vanilla JS; SQLite local, Postgres on Render

Goal: the most intuitive, responsive, clean, modern OS for construction companies. Field-first: phone and tablet before desktop.

## Security (non-negotiable)
- Real PII never enters chat or logs. Print counts/booleans; redact.
- Never open the real `superstars.db`; use the isolated DB.
- Secrets in 1Password only. Pay data only on the company console.
- Bind `127.0.0.1`; never widen `/files`; parameterized SQL; vet every dependency.
- Details: `RULES_ARCHIVE.md` (read before PII, auth, deploy, pay work).

## Workflow
- Plan Mode first; local prototype before cloud wiring.
- One worktree + branch per task from `origin/main`.
- UI: follow `DESIGN_SYSTEM.md`; mobile-first. Prototype first; Angela reviews only that.
- On "accepted": build, verify yourself (gate + phone-width check), PR to `upstream` main, merge when green. No second review.
- No `/compact`. Start: read `HANDOFF_NEXT.md` + `ROADMAP.md`.
- End every reply with one proposed next action.

## Commands
- Run: `.local/dev/run_dev.sh`
- Gate: `python tests/run_gate_ci.py --backend sqlite`

## Conventions
Idempotent migrations; local dates, never `toISOString()`; Worker ID `W-####`; brand SSC, never "AMG".
