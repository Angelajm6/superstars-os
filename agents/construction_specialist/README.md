# agents/construction_specialist/ — Construction Specialist Agent (Phase A / v0)

> ## Working invocation in this runtime (read first)
>
> - **Working invocation — manual prompt.** Instruct Claude Code (directly, or via a
>   coordinating Cowork session that hands Claude Code the message) to **read
>   `agents/construction_specialist/`** (persona + scope + corpus) and **act as the
>   Construction Specialist Agent for the duration of that response** — answer with
>   citation discipline and write one PII-safe provenance row. This is the only path
>   verified to work today.
> - **NOT supported in current runtime:** the `/construction-specialist` **slash
>   command** — returns "Unknown command" (verified 2026-05-28).
> - **NOT supported in current runtime:** Claude Code **Task-tool subagent
>   auto-discovery** — `Task(subagent_type="construction-specialist")` returns
>   "Agent type … not found" (verified 2026-05-28).
> - **Future work:** re-evaluate both auto-discovery paths when runtime capabilities
>   change.

Co-located **definition + corpus** for SSC's foundational AI specialist (spec
decision Q4). The directory is the **corpus + persona reference** that the
manual-prompt invocation reads (the `.claude/` discovery files exist but are inert
in this runtime — see the callout above). Contract:
`CONSTRUCTION_SPECIALIST_AGENT_SPEC.md` (v1.4) in the Superstars Dashboard project folder.

## Layout

| Path | What |
|------|------|
| `construction_specialist.md` | Authoritative operating contract (persona, scope, gates, answer shape, provenance step). |
| `provenance.py` | PII-safe provenance writer (importable + CLI). One audit row per substantive interaction. |
| `corpus/` | Version-controlled MVP context pack. |
| `corpus/CORPUS_MANIFEST.md` / `corpus_manifest.json` | What's in vs. deferred; the `CORPUS_VERSION` stamp. |
| `corpus/chapter33/` | Chapter 33 orientation index (C1), from `toolbox_talks_data.py`. |
| `corpus/sika/` | Sika spec index (C2), from the `spec_products` table. |
| `corpus/products/` | Frequent-three product write-ups (validated #198 Batch 2): `concrete_repair.md` (C10), `sealants_joint_design.md` (C13), `brick_masonry_repointing.md` (C12). |
| `corpus/_deferred/` | Clearly-marked placeholders (LL11/77/126, SPRAT/IRATA, deep product write-ups) — a separate later batch. |
| `corpus/build_corpus.py` | Regenerates the corpus indexes + re-stamps `CORPUS_VERSION`. |
| `.claude/commands/construction-specialist.md` | Slash-command body — **inert in current runtime** (`/construction-specialist` → "Unknown command"); kept as a copy-paste source for the manual prompt and for if/when slash commands resolve. |
| `.claude/agents/construction-specialist.md` | Subagent stub — **inert in current runtime** (Task-tool auto-discovery not supported); kept for a pure interactive Claude Code CLI that supports filesystem subagents. |

## Invoke (MVP) — manual prompt

The working invocation is a plain message to Claude Code (no `/command`, no Task
subagent). For example:

```
Read agents/construction_specialist/ and act as the Construction Specialist —
follow its rules (cite sources or flag as general-knowledge-unverified, never
fabricate, route engineering/architect/legal/attestation out, W-#### only), answer
<my question>, then log one provenance row via
agents/construction_specialist/provenance.py --db <isolated.db>.
```

Claude Code then reads this contract + the relevant corpus index, answers
citation-anchored with an adjacency prompt, hard-declines/routes anything out of
scope (EOR/architect/attorney/attestation; procurement = draft+analyze only), and
writes **exactly one** PII-safe row to `construction_agent_provenance`. The body of
`.claude/commands/construction-specialist.md` is a ready-made source for that prompt.

**Why not the slash command or Task sub-agent (#198 finding):** this runtime (the
Claude Agent SDK / FleetView environment) auto-discovers **neither** project
`.claude/commands/*.md` slash commands **nor** project `.claude/agents/*.md` Task
subagents. Verified 2026-05-28: `/construction-specialist` → "Unknown command", and
`Task(subagent_type="construction-specialist")` → "Agent type … not found" (even
cold). Both are interactive-CLI-only features. Until runtime capabilities change,
use the manual prompt above; revisit the auto-discovery shortcuts in v2.

## Audit trail

Every substantive interaction → one row in `construction_agent_provenance`
(migration: `apply_construction_agent_provenance_schema.py`). PII-safe: W-#### only,
no names/PINs/rates. The anti-corruption meta-smoke
(`tests/smoke_no_production_data_corruption.py`) snapshots the table as a backstop;
the surface's own regression net is `tests/smoke_construction_agent.py` (a throwaway
database; it also proves `db_guard` refuses the real `superstars.db`, in the gate).

**Database guard (2026-10-26):** `db_guard.py` decides what the helper and the corpus
builder may open: an explicit `--db` or `SSC_AGENT_DB` only, the file must exist, and
the real `superstars.db` is refused by name or path. The Task subagent has no Bash; it
hands back a PROVENANCE block for the calling session to log.

## Regenerate corpus / re-stamp version

```
python agents/construction_specialist/corpus/build_corpus.py --db <isolated.db>
```
