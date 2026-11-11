"""The forecast job — discovery, atomic publication, idempotency, pruning. No network.

A fake source on a coarse 0.1° grid keeps renders fast; one end-to-end test runs the
real PIAF client (respx) and decoder on a real fixture. The properties pinned here are
the ones ``specs/forecast-nowcast.md`` §6 asks Phase 2 to test: "not ready" is a retry
and never a failure, a half-rendered run is never advertised, re-running a completed
run fetches nothing — plus the Phase 0 additions (a rejected payload publishes
nothing; the published pointer survives a Redis flush).
"""

from __future__ import annotations

import gzip
import json

import httpx
import numpy as np
import pytest
import respx
from django.test import override_settings

from radar import cache
from radar import storage
from radar.forecast import job
from radar.forecast import piaf
from radar.forecast.base import ForecastUnavailable
from radar.forecast.base import PayloadRejected
from radar.forecast.base import RunNotReady
from radar.forecast.grib_decode import PROJDEF
from radar.forecast.grib_decode import PiafField
from radar.providers.meteofrance_render import Coverage
from radar.providers.meteofrance_render import GridGeo
from radar.tests.test_forecast_grib import DRY_BBOX
from radar.tests.test_forecast_grib import DRY_RUN
from radar.tests.test_forecast_piaf import GRIB
from radar.tests.test_forecast_piaf import URL
from radar.tests.test_forecast_piaf import FakeAuth

RUN = 1794395700  # 2026-11-11T11:15Z
NOW = RUN + 545  # the median moment a run became downloadable (§8.5)
LEADS = (600, 900, 1200)  # FORECAST_HORIZON_SECONDS=600 below

# 0.1° over the France bbox: ~150x100 cells, so a frame renders in milliseconds.
GEO = GridGeo(projdef=PROJDEF, xscale=0.1, yscale=0.1, xsize=158, ysize=104, x_ul=-6.05, y_ul=51.55)


class FakeSource:
    name = "fake"
    run_interval = 300
    publication_delay = 480

    def __init__(self, *, not_ready=(), fail=(), reject=()) -> None:
        self.downloads: list[tuple[int, int]] = []
        self.not_ready, self.fail, self.reject = set(not_ready), set(fail), set(reject)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient()

    async def download(self, run_ts: int, valid_ts: int, *, client) -> bytes:
        self.downloads.append((run_ts, valid_ts))
        if run_ts in self.not_ready:
            raise RunNotReady
        if valid_ts in self.fail:
            raise ForecastUnavailable("boom")
        return b"x" * 1000

    def decode(self, _message: bytes, run_ts: int, valid_ts: int) -> PiafField:
        if valid_ts in self.reject:
            raise PayloadRejected("not ours")
        rate = np.zeros((GEO.ysize, GEO.xsize), dtype=np.float32)
        rate[40:60, 70:90] = 5.0  # a 2° block of 5 mm/h over central France
        return PiafField(run_ts=run_ts, valid_ts=valid_ts, rate=rate, geo=GEO)


class FakeProvider:
    """The two passive seams the job reads from the Météo-France provider."""

    def __init__(self, coverage: Coverage | None) -> None:
        self._coverage = coverage
        self.auth = FakeAuth()

    def latest_coverage(self) -> Coverage | None:
        return self._coverage


def _coverage(*, covered: bool = True, ts: int = NOW - 120) -> Coverage:
    return Coverage(ts=ts, geo=GEO, covered=np.full((GEO.ysize, GEO.xsize), covered))


@pytest.fixture(autouse=True)
def forecast_root(tmp_path):
    with override_settings(FORECAST_ROOT=str(tmp_path), FORECAST_HORIZON_SECONDS=600):
        yield tmp_path


async def _poll(source, provider=None, now=NOW):
    return await job.poll_forecast(provider or FakeProvider(_coverage()), source, now=now)


# -- pure helpers ----------------------------------------------------------------


def test_default_leads_are_10_to_70_minutes():
    with override_settings(FORECAST_HORIZON_SECONDS=3600):
        leads = job.lead_seconds()
    assert leads == list(range(600, 4201, 300))
    assert len(leads) == 13


def test_candidates_are_the_two_newest_plausible_runs():
    source = FakeSource()
    assert job.candidate_runs(NOW, source, 0) == [RUN, RUN - 300]
    assert job.candidate_runs(NOW, source, RUN - 300) == [RUN]
    assert job.candidate_runs(NOW, source, RUN) == []
    # Before the publication delay has passed, RUN is not even a candidate.
    assert job.candidate_runs(RUN + 400, source, 0) == [RUN - 300, RUN - 600]


# -- publishing ------------------------------------------------------------------


async def test_publishes_a_complete_run(forecast_root):
    source = FakeSource()
    result = await _poll(source)

    frames = [RUN + lead for lead in LEADS]
    assert result["status"] == job.STATUS_PUBLISHED
    assert result["frames"] == frames
    record = await cache.get_forecast_current()
    assert record["run"] == RUN
    assert record["frames"] == frames
    assert record["coverage_ts"] == NOW - 120
    assert storage.read_forecast_run(RUN) == record
    for valid in frames:
        assert storage.forecast_frame_done(RUN, valid)
    tiles = list((forecast_root / str(RUN)).rglob("*.png"))
    assert tiles
    assert all(t.read_bytes().startswith(b"\x89PNG") for t in tiles)
    # Leads render concurrently: a lost update on the running total would undercount.
    assert result["tiles"] == len(tiles)
    counters = await cache.get_forecast_counters()
    assert counters["runs_published"] == 1
    assert counters["bytes"] == 3000


async def test_rerunning_a_published_run_fetches_nothing():
    source = FakeSource()
    await _poll(source)
    downloads = len(source.downloads)

    result = await _poll(source)
    assert result["status"] == job.STATUS_IDLE
    assert len(source.downloads) == downloads


async def test_not_ready_is_a_retry_not_a_failure():
    source = FakeSource(not_ready={RUN, RUN - 300})
    result = await _poll(source)

    assert result["status"] == job.STATUS_NOT_READY
    assert await cache.get_forecast_current() is None
    # Only the probe (first lead) of each candidate went out.
    assert source.downloads == [(RUN, RUN + 600), (RUN - 300, RUN - 300 + 600)]
    counters = await cache.get_forecast_counters()
    assert counters["not_ready"] == 2
    assert counters["fetch_failed"] == counters["rejected"] == 0


async def test_older_run_is_published_while_the_newest_is_not_out():
    result = await _poll(FakeSource(not_ready={RUN}))
    assert result["status"] == job.STATUS_PUBLISHED
    assert result["run"] == RUN - 300


async def test_a_half_written_run_is_never_advertised():
    source = FakeSource(fail={RUN + 900})
    result = await _poll(source)

    assert result["status"] == job.STATUS_FAILED
    assert await cache.get_forecast_current() is None
    assert storage.read_forecast_run(RUN) is None
    assert storage.forecast_frame_done(RUN, RUN + 600)
    assert not storage.forecast_frame_done(RUN, RUN + 900)
    assert (await cache.get_forecast_counters())["fetch_failed"] == 1


async def test_a_failed_run_is_not_retried_on_the_next_tick():
    await _poll(FakeSource(fail={RUN + 900}))
    nxt = FakeSource(not_ready={RUN - 300})
    await _poll(nxt, now=NOW + 20)
    # The older candidate is still worth a probe; the failed run is left alone.
    assert [run for run, _ in nxt.downloads] == [RUN - 300]


async def test_cooling_down_when_the_failed_run_is_the_only_candidate():
    await _poll(FakeSource(not_ready={RUN}))  # publishes RUN - 300
    await _poll(FakeSource(fail={RUN + 900}))  # RUN fails
    quiet = FakeSource()
    result = await _poll(quiet, now=NOW + 20)
    assert result["status"] == job.STATUS_COOLING_DOWN
    assert quiet.downloads == []


async def test_a_retry_fetches_only_the_missing_frames():
    await _poll(FakeSource(fail={RUN + 900}))
    retry = FakeSource()
    result = await _poll(retry, now=NOW + 60)  # past the cooldown

    assert result["status"] == job.STATUS_PUBLISHED
    assert retry.downloads == [(RUN, RUN + 900)]


async def test_a_rejected_payload_publishes_nothing():
    result = await _poll(FakeSource(reject={RUN + 600}))
    assert result["status"] == job.STATUS_FAILED
    assert await cache.get_forecast_current() is None
    assert (await cache.get_forecast_counters())["rejected"] == 1


@pytest.mark.parametrize("coverage", [None, _coverage(ts=NOW - 3601)])
async def test_no_recent_coverage_means_no_forecast(coverage):
    source = FakeSource()
    result = await _poll(source, FakeProvider(coverage))
    assert result["status"] == job.STATUS_NO_COVERAGE
    assert source.downloads == []


async def test_a_fully_masked_frame_is_still_a_frame(forecast_root):
    """No coverage anywhere ⇒ no tiles, but the run is complete and published."""
    result = await _poll(FakeSource(), FakeProvider(_coverage(covered=False)))
    assert result["status"] == job.STATUS_PUBLISHED
    assert not list((forecast_root / str(RUN)).rglob("*.png"))
    done = forecast_root / str(RUN) / str(RUN + 600) / storage.FORECAST_FRAME_DONE
    assert done.read_text() == "0"


# -- pruning and recovery --------------------------------------------------------


@override_settings(FORECAST_RETAIN_RUNS=2)
async def test_prune_keeps_the_newest_complete_runs(forecast_root):
    abandoned = forecast_root / str(RUN - 900) / str(RUN - 300)  # an old partial run
    abandoned.mkdir(parents=True)
    for step in range(3):
        result = await _poll(FakeSource(), now=NOW + 300 * step)
        assert result["status"] == job.STATUS_PUBLISHED
    arriving = forecast_root / str(RUN + 1200)  # a partial run newer than published
    arriving.mkdir()
    await _poll(FakeSource(), now=NOW + 900)

    # The two newest complete runs, plus the partial one still arriving.
    assert storage.forecast_runs() == [RUN + 600, RUN + 900, RUN + 1200]
    assert arriving.exists()
    assert not abandoned.exists()


class FailsLastLead(FakeSource):
    """Every run gets part-way: its first leads are written, then the last one fails."""

    async def download(self, run_ts: int, valid_ts: int, *, client) -> bytes:
        if valid_ts - run_ts == LEADS[-1]:
            raise ForecastUnavailable("boom")
        return await super().download(run_ts, valid_ts, client=client)


async def test_abandoned_partial_runs_are_pruned_while_nothing_publishes(forecast_root):
    """Pruning must not wait for a publish, or failing runs pile up on the shared volume."""
    for step in range(6):
        result = await _poll(FailsLastLead(), now=NOW + 300 * step)
        assert result["status"] == job.STATUS_FAILED

    assert await cache.get_forecast_current() is None  # nothing was ever published
    # Only the two runs still in the candidate window survive; the four before are gone.
    assert storage.forecast_runs() == [RUN + 1200, RUN + 1500]


async def test_a_crashing_tick_still_prunes(forecast_root):
    abandoned = forecast_root / str(RUN - 900) / str(RUN - 300)
    abandoned.mkdir(parents=True)

    # A crash outside the per-run handler (here: reading the coverage) escapes the tick.
    class CrashingProvider(FakeProvider):
        def latest_coverage(self):
            raise RuntimeError("unexpected")

    with pytest.raises(RuntimeError):
        await _poll(FakeSource(), CrashingProvider(None))
    assert not abandoned.parent.exists()


async def test_an_unexpected_error_is_a_failed_run_cooled_down_and_counted(caplog):
    """A full disk or a bug fails the run like a rejection: no retry every tick."""

    class DiskFull(FakeSource):
        async def download(self, run_ts: int, valid_ts: int, *, client) -> bytes:
            self.downloads.append((run_ts, valid_ts))
            raise OSError(28, "No space left on device")

    result = await _poll(DiskFull())
    assert result["status"] == job.STATUS_FAILED
    assert result["run"] == RUN
    assert (await cache.get_forecast_counters())["crashed"] == 1
    assert await cache.get_forecast_current() is None
    failed = [r for r in caplog.records if getattr(r, "event", None) == "forecast_run_failed"]
    assert [r.reason for r in failed] == ["crashed"]
    assert failed[0].exc_info is not None  # the traceback is kept

    # The next tick leaves the crashed run alone and only probes the older candidate.
    nxt = DiskFull()
    await _poll(nxt, now=NOW + 20)
    assert [run for run, _ in nxt.downloads] == [RUN - 300]


async def test_prune_never_removes_the_published_run(forecast_root):
    await _poll(FakeSource())
    # Far later, with no newer run ever completed: the published one is all there is.
    result = await _poll(FakeSource(not_ready={RUN + 3000, RUN + 3300}), now=NOW + 3300)
    assert result["status"] == job.STATUS_NOT_READY
    assert storage.forecast_runs() == [RUN]


async def test_pointer_is_rebuilt_from_disk_after_a_redis_flush():
    await _poll(FakeSource())
    await cache.get_client().delete(cache.FORECAST_CURRENT_KEY)

    assert await job.published_run() == RUN
    assert (await cache.get_forecast_current())["run"] == RUN


@pytest.mark.parametrize("raw", ['{"frames": []}', '{"run": "x"}', "[1, 2]", '"run"', "{bad"])
async def test_a_mangled_pointer_is_rebuilt_from_disk_not_a_crash_loop(raw):
    """A pointer the job cannot read must not wedge it until the next Redis flush."""
    await _poll(FakeSource())
    await cache.get_client().set(cache.FORECAST_CURRENT_KEY, raw)

    assert await job.published_run() == RUN
    assert (await cache.get_forecast_current())["run"] == RUN
    result = await _poll(FakeSource(), now=NOW + 300)  # and the job carries on
    assert result["status"] == job.STATUS_PUBLISHED


def test_a_run_record_must_name_its_own_run(forecast_root):
    run_dir = forecast_root / str(RUN)
    run_dir.mkdir()
    for record in ({"run": RUN + 300}, {"frames": []}, [RUN]):
        (run_dir / storage.FORECAST_RUN_RECORD).write_text(json.dumps(record))
        assert storage.read_forecast_run(RUN) is None, record
    (run_dir / storage.FORECAST_RUN_RECORD).write_text(json.dumps({"run": RUN}))
    assert storage.read_forecast_run(RUN) == {"run": RUN}


# -- one coverage mask per run ------------------------------------------------------


def _tile_count(run: int, valid: int) -> int:
    return int(
        (storage.forecast_run_dir(run) / str(valid) / storage.FORECAST_FRAME_DONE).read_text()
    )


async def test_a_run_finished_over_several_ticks_keeps_its_first_coverage():
    """Every frame of a run, and its record, use the mask its first frame was drawn with."""
    covered = FakeProvider(_coverage(covered=True, ts=NOW - 120))
    await _poll(FakeSource(fail={RUN + 900}), covered)  # part-way: RUN+600 is on disk
    assert _tile_count(RUN, RUN + 600) > 0

    # The coverage has moved on since — to one that would blank every frame.
    blind = FakeProvider(_coverage(covered=False, ts=NOW + 30))
    result = await _poll(FakeSource(), blind, now=NOW + 61)  # past the cooldown

    assert result["status"] == job.STATUS_PUBLISHED
    assert all(_tile_count(RUN, valid) > 0 for valid in result["frames"])
    assert (await cache.get_forecast_current())["coverage_ts"] == NOW - 120
    assert RUN not in job._run_coverage  # released once published


async def test_frames_left_by_a_restart_are_redone_with_one_coverage():
    """After a restart the pin is gone and the frames' mask unknown: start the run over."""
    await _poll(FakeSource(fail={RUN + 900}))
    job._run_coverage.clear()  # the archiver restarted
    job._failed_until.clear()

    source = FakeSource()
    result = await _poll(source, FakeProvider(_coverage(ts=NOW + 30)), now=NOW + 61)

    assert result["status"] == job.STATUS_PUBLISHED
    assert (RUN, RUN + 600) in source.downloads  # re-fetched, not reused
    assert (await cache.get_forecast_current())["coverage_ts"] == NOW + 30


async def test_a_run_not_out_yet_follows_the_newest_coverage():
    """No frame on disk yet ⇒ nothing to stay consistent with: use the newest mask."""
    await _poll(FakeSource(not_ready={RUN, RUN - 300}), FakeProvider(_coverage(ts=NOW - 120)))
    result = await _poll(FakeSource(), FakeProvider(_coverage(ts=NOW + 10)), now=NOW + 20)
    assert result["status"] == job.STATUS_PUBLISHED
    assert (await cache.get_forecast_current())["coverage_ts"] == NOW + 10


async def test_no_run_on_disk_means_nothing_published():
    assert await job.published_run() == 0


def test_storage_ignores_foreign_dirs_and_bad_records(forecast_root):
    (forecast_root / "tmp").mkdir()
    (forecast_root / str(RUN)).mkdir()
    (forecast_root / str(RUN) / storage.FORECAST_RUN_RECORD).write_text("{not json")
    assert storage.forecast_runs() == [RUN]
    assert storage.read_forecast_run(RUN) is None


# -- end to end on a real PIAF response --------------------------------------------


@override_settings(FORECAST_FIRST_LEAD_SECONDS=300, FORECAST_HORIZON_SECONDS=0)
@respx.mock
async def test_end_to_end_with_the_real_client_and_decoder(forecast_root):
    """PIAF client → strict decode → mask → render → disk → pointer, on real bytes."""
    grib = gzip.decompress(DRY_BBOX.read_bytes())
    route = respx.get(URL).mock(return_value=httpx.Response(200, content=grib, headers=GRIB))
    provider = FakeProvider(_coverage(ts=DRY_RUN + 60))

    result = await job.poll_forecast(provider, piaf.PiafSource(provider.auth), now=DRY_RUN + 545)

    assert result["status"] == job.STATUS_PUBLISHED
    assert result["frames"] == [DRY_RUN + 300]
    assert route.call_count == 1
    assert list((forecast_root / str(DRY_RUN)).rglob("*.png"))
    record = json.loads((forecast_root / str(DRY_RUN) / "run.json").read_text())
    assert record["source"] == "piaf"


def test_forecast_requires_meteofrance():
    from django.core.exceptions import ImproperlyConfigured  # noqa: PLC0415

    from config.settings.base import require_meteofrance_credentials  # noqa: PLC0415

    with pytest.raises(ImproperlyConfigured, match="FORECAST_ENABLED requires"):
        require_meteofrance_credentials(enabled=False, application_id="", forecast=True)
    require_meteofrance_credentials(enabled=True, application_id="x", forecast=True)


async def test_cooldown_only_skips_the_failed_run():
    """A failed older run must not block a newer one that has just come out."""
    await _poll(FakeSource(not_ready={RUN}, fail={RUN - 300 + 900}))
    newer = FakeSource()
    result = await _poll(newer, now=NOW + 20)
    assert result["status"] == job.STATUS_PUBLISHED
    assert result["run"] == RUN
