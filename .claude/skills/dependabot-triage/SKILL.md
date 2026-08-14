---
name: dependabot-triage
description: Triage open Dependabot PRs on this repo — research each dependency bump against its upstream changelog, judge the real impact on this codebase, label every PR SAFE TO MERGE / REVIEW NEEDED / DO NOT MERGE with a reasoning comment, and squash-merge the safe ones. Use when asked to review, triage, or clear the Dependabot queue, and as the weekly scheduled routine.
---

# Dependabot triage

Review every open Dependabot PR on `hleroy/rainradar`, classify it, and merge what
is genuinely safe. This runs unattended on a weekly schedule, so **the burden of
proof is on merging** — when the evidence is thin, escalate rather than merge.

## Tooling — check for `gh` first

**The scheduled cloud sandbox has no `gh` CLI.** Run `which gh` before relying on it.
Both paths below are supported; the verdicts and the hard rules are identical either
way, only the transport differs.

| Operation | With `gh` (local) | Without `gh` (cloud routine) |
|---|---|---|
| List open PRs | `gh pr list --author 'app/dependabot' --state open --json number,title,url` | `mcp__github__list_pull_requests` (`state: "open"`, then filter to `app/dependabot` yourself) |
| PR facts | `gh pr view <n> --json …` | `mcp__github__pull_request_read` `method: "get"` |
| Check runs | (included in `statusCheckRollup`) | `mcp__github__pull_request_read` `method: "get_check_runs"` |
| Changed files | `gh pr diff <n> --name-only` | `mcp__github__pull_request_read` `method: "get_files"` |
| Label | `gh api -X POST repos/hleroy/rainradar/issues/<n>/labels -f 'labels[]=<verdict>'` | `mcp__github__issue_write` `method: "update"` |
| Comment | `gh pr comment <n> --body "…"` | `mcp__github__add_issue_comment` |
| Merge | `gh pr merge <n> --squash` | `mcp__github__merge_pull_request` (`merge_method: "squash"`) |

Four traps worth knowing before you hit them:

- **`mcp__github__issue_write` replaces the whole label set**, it does not add to it.
  Read the PR's current labels first and pass them back together with the verdict
  label, or you will silently strip `dependencies` / `python:uv`.
- **`get_files` on a lockfile-heavy PR overflows the token limit** and gets spilled to
  a file. Don't try to read it back whole — `grep -oE '"filename":"[^"]+"'` over the
  saved path is enough, since only the file list matters here.
- **Don't use `gh pr edit --add-label` locally.** The pinned `gh` 2.45 fails on it with
  a Projects-classic GraphQL deprecation error. The `gh api` form in the table works.
- **A `403 Resource not accessible by integration` means the GitHub App installation
  does not cover this repo** — not that the rubric forbids the write. Reads keep
  working, which makes it look like a permissions subtlety rather than a scope
  mistake. Report it plainly and leave the verdict unapplied; never work around it.

## Scope

Only PRs authored by `app/dependabot`. Never touch a human-authored PR: do not
label it, comment on it, or merge it.

If there are none, send the push notification saying so and stop.

## Hard rules

These override every verdict below.

- **Never merge into anything but `main` via squash.** Squash is the only merge method.
- **Never push commits to a Dependabot branch.** If a PR needs a code change to be
  correct, that is REVIEW NEEDED — describe the change, do not make it.
- **Never modify `dependabot.yml`, workflows, or project files** during triage.
- **A PR whose checks are not all green is never SAFE TO MERGE**, regardless of how
  benign the bump looks. Pending/queued counts as not green — leave it unlabeled and
  report it as "still running" rather than guessing.
- **Never re-label or re-merge a PR that already carries one of the three labels**,
  unless new commits landed after the label was applied. Re-running the routine must
  be idempotent.
- **The dockerized suite cannot run in a cloud session** (no Docker). GitHub CI is
  the gate — read its result, do not attempt `just pytest`.

## Step 1 — Gather the facts

For each PR, read its facts, its check runs and its changed files (see the tooling
table for the call that fits your environment).

Record: every constituent dependency with its **exact old → new version**, the files
touched, mergeability (`MERGEABLE` + `CLEAN`), and the conclusion of each check
(`ci / tests`, `conventional-title`, `GitGuardian Security Checks`).

A grouped PR (e.g. "Bump the python group with 8 updates") must be decomposed — the
PR body lists each `Updates X from A to B`. Every constituent is researched
individually. Docker and github-actions PRs get the same treatment via the diff.

## Step 2 — Research each bump (mandatory, no shortcuts)

For **every** dependency, across the **exact version range** old → new:

a. Identify the package and the exact range. Handle both Python
   (`pyproject.toml` / `uv.lock`) and JS (`package.json` / lockfile) if present.
b. Fetch the upstream changelog or release notes **covering that entire range** —
   GitHub Releases, `CHANGELOG.md`, PyPI/npm. Search the web when it is not in the
   repo. A range spanning several releases means reading all of them, not just the
   newest.
c. Extract breaking changes, removed/renamed/deprecated APIs, changed defaults, and
   behavioral changes across that range.
d. Determine whether any of those changes touch **what this project actually uses**:
   grep the codebase for the affected imports, function calls, settings, template
   tags, or config keys. **A breaking change the project never exercises is not
   relevant — say so explicitly**, naming what you grepped for and that it was absent.

If the changelog for a range cannot be found at all, that is REVIEW NEEDED. Never
substitute an assumption ("patch bumps are usually fine") for step (b).

## Step 3 — Weigh it against this repo's invariants

`CLAUDE.md` lists non-negotiables. Flag a bump that plausibly touches any of them,
even when CI is green — the suite does not cover everything:

- **Python 3.14 / PEP 758.** `requires-python = "==3.14.*"`. Anything that would
  move the interpreter, or a tool that cannot parse 3.14 syntax, is DO NOT MERGE.
- **Ruff.** Pinned in `pyproject.toml` *and* as the `ruff-pre-commit` rev in
  `.pre-commit-config.yaml`; Dependabot only updates the first. **Any ruff PR is
  always REVIEW NEEDED**, with the comment naming the exact `rev:` line to sync.
- **`pywebpush`** — the only module allowed to import it is `radar/alerts/webpush.py`
  (sync → `to_thread` + semaphore + timeout). Check for API/signature changes.
- **Django** — check release notes for changes to async views, ASGI, cache, or the
  migration machinery. Migrations must stay backward-compatible (expand/contract).
- **`numpy`** — the Météo-France render path averages reflectivity in **linear Z**;
  watch for dtype/casting/`nan` semantics changes in the render or smoothing code.
- **`redis` / `hiredis` / `channels`** — the async client is rebuilt on event-loop
  change; watch for connection-pool or decoding changes.
- **Base images (docker)** — a Postgres or Nginx major is DO NOT MERGE (prod data /
  the `location = /` + terminal 404 routing rules). Python base image must stay 3.14.
- **github-actions** — a major bump of an action can silently change defaults; verify
  against `tests.yml` / `deploy.yml` usage.

### Ask what CI can actually see

Green checks are evidence only about code the suite genuinely exercises. Before
leaning on `ci / tests` to clear a dependency, check whether the tests **mock that
dependency out** — grep for `monkeypatch`, `respx`, or a stubbed module against the
package's import site. If they do, the green check says nothing about the bump, and
the burden falls entirely on step 2's research.

`pywebpush` is the standing example: `radar/tests/test_alerts_evaluator.py`
monkeypatches `radar.alerts.webpush.send` wholesale, so **no** pywebpush change can
ever fail CI here. External HTTP is mocked with `respx` throughout for the same
reason. Treat a dependency in that position as needing stronger changelog evidence
than one the suite really runs.

## Step 4 — Classify

**SAFE TO MERGE** — all of:
- every check green, `MERGEABLE` and `CLEAN`;
- every constituent is a **patch or minor** bump (never a major);
- step 2 completed for each, with the changelog actually read;
- no breaking/deprecated/default change that this codebase exercises;
- touches none of the invariants above;
- not a ruff PR.

**REVIEW NEEDED** — anything unresolved rather than known-bad: a major bump, a ruff
bump, an unreachable or ambiguous changelog, a behavioral change whose impact you
cannot rule out, a failing-but-plausibly-flaky check, or an invariant that needs a
human eye.

**DO NOT MERGE** — known-bad: a breaking change this project demonstrably uses, an
interpreter/base-image violation, a security regression, a check failing for a real
reason, or a conflicted branch.

## Step 5 — Mark

Every triaged PR gets exactly one label — `safe-to-merge`, `review-needed`, or
`do-not-merge` — plus one comment. Remove any of the other two if present.

Apply both with the calls from the tooling table. When labelling via
`mcp__github__issue_write`, remember it **replaces** the label set: pass the PR's
existing labels back alongside the verdict label, minus any of the other two verdicts.

Pass the comment body inline (a `gh` heredoc, or the MCP tool's `body` argument). Do
not write it to a file first — this routine runs without file-write tools by design,
so that it cannot modify the working tree.

The comment must carry the reasoning, not just the verdict:

```markdown
## <VERDICT>

**Checks:** ci / tests ✅ · conventional-title ✅ · GitGuardian ✅ · MERGEABLE/CLEAN

| Package | Old → New | Type | Finding |
|---|---|---|---|
| django | 6.0.8 → 6.1 | minor | <what the release notes say, and whether this repo uses it> |

**Impact on this codebase:** <what was grepped for, what was found or absent>
**Invariants:** <which were considered, and why they are or are not affected>
**Verdict rationale:** <one short paragraph>

<sub>Automated weekly Dependabot triage.</sub>
```

Use the `md-table` skill for the table. State findings honestly — if a changelog was
thin, say so rather than implying research that did not happen.

## Step 6 — Merge the safe ones

Only for PRs labeled `safe-to-merge` in this run, squash-merging via the call from the
tooling table.

The PR title is the squashed subject on `main`, so **verify it is a valid
Conventional Commit** first (Dependabot produces `build(deps): …` or `chore(deps): …`,
both allowed types). If the title is not conventional, downgrade to REVIEW NEEDED
instead of merging — do not rename a Dependabot PR.

Merge one at a time and re-check the next PR's mergeability afterwards: an earlier
merge can put a later PR behind `main` and make it dirty. A PR that goes stale
mid-run is REVIEW NEEDED, not a failure.

## Step 7 — Report

Send a push notification summarizing the run:

> Dependabot triage: N merged, N need review, N blocked (M PRs total)

Then print a short table of every PR with its verdict and a one-line reason. If the
run merged nothing and there was nothing to merge, say that plainly.
