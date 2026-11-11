# Kick-off prompt — 1-hour forecast, Phase 0

Paste everything below the line into a fresh Claude Code session started at the repo
root, on the branch that carries `specs/forecast-nowcast.md`.

Before starting: on <https://portail-api.meteofrance.fr>, subscribe the existing
application to the **PrevisionImmediatePrecipitations** API ("Souscrire à l'API
gratuitement"). The spike cannot run without it.

---

I want to add a 1-hour rain forecast to Rain Radar. The research and the plan are already
written in `specs/forecast-nowcast.md`; read it in full first, then `CLAUDE.md` and
§4.1, §5, §6.2, §12 and §13.1 of `specs/rain-lightning-radar-design.md`. The plan takes
the forecast from Météo-France's PIAF product rather than computing it ourselves, and it
is deliberately explicit about which facts are still unverified.

Your job in this session is **Phase 0 only: a spike that replaces those unknowns with
measurements.** Do not write production code, add dependencies to `pyproject.toml`, or
touch `radar/` or `frontend/`. The design in §4 of the plan is a proposal — I would
rather you tell me where real data contradicts it than build on it.

**What to find out** (this is §2.4 of the plan — answer every item):

1. Does our existing OAuth2 application token work against
   `https://api.meteofrance.fr/pro/piaf/1.0`? `radar/providers/meteofrance_auth.py` shows
   how we obtain it today. If you get 401/403 on `GetCapabilities`, stop and tell me — I
   have probably not finished the subscription on the portal.
2. What does WCS `GetCapabilities` return: the exact coverage ID format for PIAF
   (parameter name, run timestamp, accumulation suffix), how many runs are listed, and
   how large the document is.
3. What does `DescribeCoverage` say about the grid: extent, dimensions, row order, and
   the list of lead times.
4. Download one lead time, subset to `RADAR_BBOX`, as GRIB (`application/wmo-grib`) and
   as GeoTIFF (`image/tiff`). For each: size, packing or compression, units (mm per 5 min
   or mm/h?), and — importantly — how "no radar coverage" is encoded versus "no rain".
   Our renderer treats those differently (`rate_field` in
   `radar/providers/meteofrance_render.py`).
5. Publication latency: poll `GetCapabilities` for about an hour and record, for each new
   run, how long after its reference time it appeared, and how often a `GetCoverage` for
   it returned 403 (which this API uses for "not yet available", not for auth).
6. Does the forecast line up with our observation? Compare the first lead time of a run
   against the matching `LAME_D_EAU` frame (same valid time, once it exists): position,
   intensity, coverage mask. A quick side-by-side image is enough.

**Then recommend a decode approach**, on the evidence: GeoTIFF through the Pillow we
already ship, a small hand-written GRIB2 decoder in the style of
`radar/providers/bufr_decode.py`, or `eccodes`. This repo hand-decoded BUFR specifically
to avoid a native dependency and to validate the message template strictly, so a new
dependency needs a real argument — including whether it ships a cp314 manylinux wheel.

**How to work**

- Put the throwaway scripts and downloaded samples outside the tracked tree (or in a
  gitignored scratch directory). Run them inside the django container
  (`docker compose -f docker-compose.local.yml run --rm django …`) so they use Python
  3.14 and the real settings; the credential is `METEOFRANCE_APPLICATION_ID` from the
  local env file.
- Never print or log that credential or any access token, including in scripts, command
  output you paste back to me, or files you save.
- Stay well under the 50 requests/min quota; the latency poll needs about one request a
  minute.
- This is production-adjacent only in that it uses the real credential. Nothing you do
  here should start the archiver or write under `TILE_ROOT`.

**What I want back**

- A "Phase 0 findings" section appended to `specs/forecast-nowcast.md`: one entry per
  question above with the measured answer, and the tables in §2.3 corrected where the
  guide's AROME examples turned out not to match PIAF.
- Two or three small sample files suitable as test fixtures for Phase 1 (cropped to
  `RADAR_BBOX`, ideally one with rain and one dry), with a note on where they came from.
  Tell me their sizes before committing them.
- Your decode recommendation, and a go / no-go on PIAF versus the `pysteps` fallback
  described in §1 of the plan.
- A list of anything in §3–§6 of the plan that the findings invalidate.

Commit the document changes on a `docs/…` branch off the current one with a Conventional
Commit message, and **stop there** — don't open a PR and don't start Phase 1. I'll review
the findings before we go further. The product decisions in §5 are already made; §5.1
is still a proposal.
