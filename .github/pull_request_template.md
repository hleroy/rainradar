<!-- Keep it short: this description becomes the commit message on main, so it
     fits on one screen (about 150 words).
     The PR title must be a Conventional Commit (`type: summary`); CI checks it.
     Describe the result, not the investigation, and do not tour the diff file by file.
     Delete a section that does not apply, and delete these comments. -->

## What and why

<!-- Two or three sentences: what changes for the user, and the problem it solves.
     Link issues with "Closes #123". Structural change? Update
     specs/rain-lightning-radar-design.md in the same PR. -->

## How I verified it

<!-- One line per check, results only. CI is the gate (dockerized pytest with
     coverage >= 85% on radar/, ruff check, ruff format); docker is unavailable in
     a Claude Code web session, so say whether the suite ran locally or only in CI.
     For a frontend change, say which views you opened and what you checked.
     Say what was NOT checked. Screenshots for a visual change. -->

## Risk

<!-- Only what applies, five bullets at most:
     - touches a failure domain boundary (radar, lightning, alerts, forecast, Météo-France)?
     - new upstream call, or anything the browser reaches other than our own paths?
     - migration: backward-compatible (expand/contract)? tile layout or Redis keys changed?
     - shell asset changed: bumped both STATIC_SHELL and CACHE_VERSION in sw.js?
     - new setting, secret or feature flag; known limits and review findings left unfixed -->
