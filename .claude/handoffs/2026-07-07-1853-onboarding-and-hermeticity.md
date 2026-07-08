# Handoff — 2026-07-07 — Track root instructions, harden test hermeticity, seed handoffs

## TL;DR
Root `AGENTS.md`/`CLAUDE.md`/`CHEATSHEET.md`/`dev.sh` were untracked — a fresh clone or
worktree got no project instructions at all, the same class of bug fixed for
`.claude/gate.sh`/`.claude/evals.sh` in the two commits just before this one. Also picked
up an uncommitted `tests/conftest.py` hermeticity fix (pins `NETWORK` and
`UI_SETTLE_VIA_X402` so a dev's local `.env` can't leak mainnet/live-settlement config into
the offline suite), and this handoff file is itself the first entry in `.claude/handoffs/`
so the SessionStart banner has something to brief future sessions with.

## State
- DONE and verified: `AGENTS.md`, `CLAUDE.md`, `CHEATSHEET.md`, `dev.sh` now tracked —
  content reviewed, no secrets, matches what CLAUDE.md context already surfaces.
- DONE and verified: `tests/conftest.py` env-pin fix — reviewed the diff, matches the
  hermeticity pattern already established in the rest of that file (neutralize `.env`
  so tests can't inherit mainnet/live settings).
- DONE: `.claude/handoffs/` created with this file as the seed entry.
- Correction to prior framing: jim **does** have a GitHub remote
  (`github.com/geogoesbeepboop/jim-agent`, `gh` authenticated) — the earlier claim of "no
  remote" was stale. PRs and the merge-commit history (#6, #8, #9) already depend on it.
- Not run this session: full `uv run pytest` / `.claude/gate.sh` — the gate digest banner
  already shows jim green as of this same day (2247s pass), and these changes are
  docs/tracked-files + a test-env pin, not runtime logic, so no new gate run was forced.
  Worth a `.claude/gate.sh` run on the PR before merge if any doubt remains.

## Key decisions & context
- Opened a feature branch (`claude/onboarding-and-hermeticity-fixups`) off `main` rather
  than pushing straight to `main`, even though `main` already had 2 unpushed local commits
  (the gate/evals tracking ones) — this repo's history is exclusively PR-merged (see `#6`,
  `#8`, `#9` merge commits), so a direct push to `main` would be off-pattern. Those 2 prior
  commits ride along in this branch/PR since they were already sitting on `main` locally.
- Left `.claude/.DS_Store` and `.claude/settings.local.json` untracked deliberately —
  machine-local, same call already made in the `c0e69e6` commit message.

## Next steps
1. Push `claude/onboarding-and-hermeticity-fixups` and open a PR against `main` — this is
   the immediate next action for this session.
2. Once merged, confirm the next fresh clone/worktree actually gets `AGENTS.md` context
   (the whole point of this fix) — e.g. spin up a worktree and check `CLAUDE.md`/`AGENTS.md`
   are present without a manual copy.
3. Longer term: consider whether `.claude/handoffs/*.md` should get a lightweight naming
   or pruning convention (this repo will accumulate them like `jim-agent`'s sibling repos
   already do) — not urgent, just noted so it isn't reinvented ad hoc later.

## Watch out for
- `tests/conftest.py`'s hermeticity list is easy to silently bit-rot if a new env-sensitive
  feature lands without a corresponding pin here — check this file when adding anything
  that reads `NETWORK`, `UI_SETTLE_VIA_X402`, or similar toggles.
- The gate digest banner reads `~/dev/docs/gate-digests/*.md` (outside this repo) — that's
  a machine-local convenience, not something this repo's CI produces, so don't expect it to
  exist on a different machine or in CI.

## Pointers
- [AGENTS.md](../../AGENTS.md), [CLAUDE.md](../../CLAUDE.md), [CHEATSHEET.md](../../CHEATSHEET.md), [dev.sh](../../dev.sh)
- [tests/conftest.py](../../tests/conftest.py)
- [.claude/gate.sh](../gate.sh), [.claude/evals.sh](../evals.sh) — the sibling fix this one follows
