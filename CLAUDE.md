# CLAUDE.md

All project rules, architecture, commands and conventions live in one place:

@AGENTS.md

Claude-specific notes:

- Run the `humanizer` skill (if available) over READMEs and docs before
  committing; the voice rules in AGENTS.md apply to all prose.
- Local orchestration artifacts (task briefs, reports, progress ledger) live in
  `.superpowers/sdd/` — gitignored, local machine only. Check
  `.superpowers/sdd/progress.md` before re-doing any task; trust it and
  `git log` over conversation memory.
- The master build plan and hackathon notes live one directory ABOVE this repo
  (`../PLAN.md`, `../notes/`) on the local machine only — never copy or commit
  them into the repo.
- Verified stack notes (pinned versions, current API gotchas):
  `.superpowers/sdd/stack-notes.md` — read before touching LangGraph, the LLM
  client, auth hashing, or the frontend toolchain.
