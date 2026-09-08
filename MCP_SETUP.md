# MCP servers for this repo (Claude Code)

`.mcp.json` at the repo root declares the MCP servers Claude Code should
connect to when a session opens in this folder. It contains **no secrets** —
keys are read from environment variables (`${VAR}` expansion), per the
CLAUDE.md secrets rule (names in the repo, values only in 1Password / your
shell).

| Server | What it gives Claude | Needs |
|---|---|---|
| `render` | Render's official MCP: list services, read deploy status + logs, read/set env vars, query Render Postgres (preprod DB) | `RENDER_API_KEY` — Render dashboard → Account Settings → API Keys |
| `github` | GitHub's official MCP: PRs, issues, checks, code search on `Angelajm6/superstars-os` and the upstream repo | `GITHUB_PAT` — fine-grained PAT with repo + pull-request scopes (or `gh auth token`) |
| `context7` | Up-to-date library docs (Flask, psycopg, SendGrid, QuickBooks/Intuit SDK, Fieldwire API) pulled into context on demand | nothing |

## One-time setup

Add the two keys to your shell profile so every Claude Code session (terminal
or desktop app) inherits them, then restart the app:

```bash
echo 'export RENDER_API_KEY="rnd_..."' >> ~/.zshrc
echo 'export GITHUB_PAT="github_pat_..."' >> ~/.zshrc
```

`GITHUB_PAT` can be the token the `gh` CLI already holds:
`export GITHUB_PAT="$(gh auth token)"`.

On the next session start Claude Code asks once whether to trust the
project's `.mcp.json`; approve it. `/mcp` shows connection status.

## Scope discipline

* Point the Render key at the workspace that holds the **preprod** service
  first. Production stays read-only until the C-suite sign-off flow is in
  place.
* Never paste key values into chat, commits, or `.mcp.json`.
