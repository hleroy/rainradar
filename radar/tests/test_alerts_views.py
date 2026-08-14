"""Web Push subscribe/unsubscribe endpoints.

Flag-gated, csrf-exempt POST views with hard validation (incl. the hostname-suffix
SSRF allow-list), anchor coarsening, and a new-endpoint cap. Driven through the async
test client; the ``push_subscription`` table is created by migration 0003.
"""

from __future__ import annotations

import json

import pytest
from django.test import override_settings

from radar.models import PushSubscription

pytestmark = [pytest.mark.asyncio, pytest.mark.django_db(transaction=True)]

# Enabling push at request time is enough — the fail-fast VAPID check runs once at
# settings import, not per request, so override_settings never trips it.
ENABLED = {
    "PUSH_ALERTS_ENABLED": True,
    "VAPID_PUBLIC_KEY": "BPUBLICKEY",
    "VAPID_PRIVATE_KEY": "privatekey",
    "VAPID_SUBJECT": "mailto:ops@example.com",
}

_VALID_ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc123"


def _body(  # noqa: PLR0913, PLR0917
    endpoint=_VALID_ENDPOINT,
    lat=45.0,
    lon=3.0,
    locale="en",
    p256dh="k" * 40,
    auth="a" * 20,
):
    return {
        "endpoint": endpoint,
        "keys": {"p256dh": p256dh, "auth": auth},
        "lat": lat,
        "lon": lon,
        "locale": locale,
    }


async def _post(async_client, url, body):
    return await async_client.post(url, data=json.dumps(body), content_type="application/json")


# -- gating -------------------------------------------------------------------


async def test_subscribe_404_when_flag_off(async_client):
    resp = await _post(async_client, "/api/alerts/subscribe", _body())
    assert resp.status_code == 404


async def test_unsubscribe_404_when_flag_off(async_client):
    resp = await _post(async_client, "/api/alerts/unsubscribe", {"endpoint": _VALID_ENDPOINT})
    assert resp.status_code == 404


async def test_subscribe_get_405(async_client):
    with override_settings(**ENABLED):
        resp = await async_client.get("/api/alerts/subscribe")
    assert resp.status_code == 405


# -- happy path + upsert ------------------------------------------------------


async def test_subscribe_creates_row_and_coarsens(async_client):
    with override_settings(**ENABLED):
        resp = await _post(async_client, "/api/alerts/subscribe", _body(lat=45.12345, lon=3.98765))
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    row = await PushSubscription.objects.aget(endpoint=_VALID_ENDPOINT)
    assert row.lat == 45.12  # coarsened to ~1 km (2 decimals)
    assert row.lon == 3.99
    assert row.locale == "en"


async def test_subscribe_upserts_by_endpoint_and_refreshes_seen(async_client):
    with override_settings(**ENABLED):
        await _post(async_client, "/api/alerts/subscribe", _body(lat=45.0))
        first = await PushSubscription.objects.aget(endpoint=_VALID_ENDPOINT)
        seen_before = first.last_seen_at
        await _post(async_client, "/api/alerts/subscribe", _body(lat=46.0))
    assert await PushSubscription.objects.acount() == 1  # not a new row
    row = await PushSubscription.objects.aget(endpoint=_VALID_ENDPOINT)
    assert row.lat == 46.0
    assert row.last_seen_at >= seen_before


async def test_locale_coerced_to_en_for_junk(async_client):
    with override_settings(**ENABLED):
        await _post(async_client, "/api/alerts/subscribe", _body(locale="de"))
    row = await PushSubscription.objects.aget(endpoint=_VALID_ENDPOINT)
    assert row.locale == "en"


async def test_locale_fr_preserved(async_client):
    with override_settings(**ENABLED):
        await _post(async_client, "/api/alerts/subscribe", _body(locale="fr"))
    row = await PushSubscription.objects.aget(endpoint=_VALID_ENDPOINT)
    assert row.locale == "fr"


# -- validation (400s) --------------------------------------------------------


async def test_malformed_json_400(async_client):
    with override_settings(**ENABLED):
        resp = await async_client.post(
            "/api/alerts/subscribe",
            data="{not json",
            content_type="application/json",
        )
    assert resp.status_code == 400


async def test_oversized_body_400(async_client):
    big = _body()
    big["blob"] = "x" * 9000  # > 8 KiB
    with override_settings(**ENABLED):
        resp = await _post(async_client, "/api/alerts/subscribe", big)
    assert resp.status_code == 400


async def test_out_of_bbox_anchor_400(async_client):
    with override_settings(**ENABLED):
        resp = await _post(async_client, "/api/alerts/subscribe", _body(lat=10.0, lon=3.0))
    assert resp.status_code == 400


async def test_missing_keys_400(async_client):
    body = _body()
    body["keys"] = {}
    with override_settings(**ENABLED):
        resp = await _post(async_client, "/api/alerts/subscribe", body)
    assert resp.status_code == 400


async def test_oversized_key_400(async_client):
    with override_settings(**ENABLED):
        resp = await _post(async_client, "/api/alerts/subscribe", _body(p256dh="k" * 300))
    assert resp.status_code == 400


# -- SSRF: hostname-suffix allow-list -----------------------------------------

_REJECT = [
    "https://push.apple.com.evil.com/x",  # suffix as a left-label of a bad host
    "https://evil.com/fcm.googleapis.com",  # suffix only in the path
    "https://evil.com/?u=push.services.mozilla.com",  # suffix only in the query
    "http://fcm.googleapis.com/x",  # not https
    "https://notfcm.googleapis.com/x",  # not on a label boundary
    "https://web.push.apple.com@evil.com/x",  # userinfo trick → host is evil.com
]
_ACCEPT = [
    "https://web.push.apple.com/x",
    "https://fcm.googleapis.com/fcm/send/z",
    "https://updates.push.services.mozilla.com/wpush/v2/abc",
]


@pytest.mark.parametrize("endpoint", _REJECT)
async def test_endpoint_ssrf_rejected(async_client, endpoint):
    with override_settings(**ENABLED):
        resp = await _post(async_client, "/api/alerts/subscribe", _body(endpoint=endpoint))
    assert resp.status_code == 400, f"should reject {endpoint}"


@pytest.mark.parametrize("endpoint", _ACCEPT)
async def test_endpoint_allowed_accepted(async_client, endpoint):
    with override_settings(**ENABLED):
        resp = await _post(async_client, "/api/alerts/subscribe", _body(endpoint=endpoint))
    assert resp.status_code == 200, f"should accept {endpoint}"


# -- capacity cap -------------------------------------------------------------


async def test_new_endpoint_capped_but_existing_reupserts(async_client):
    with override_settings(**ENABLED, PUSH_MAX_SUBSCRIPTIONS=1):
        r1 = await _post(async_client, "/api/alerts/subscribe", _body(endpoint=_ACCEPT[1]))
        assert r1.status_code == 200
        # A different (new) endpoint is refused once we're at capacity…
        r2 = await _post(async_client, "/api/alerts/subscribe", _body(endpoint=_ACCEPT[2]))
        assert r2.status_code == 429
        # …but the existing endpoint may always re-upsert.
        existing = _body(endpoint=_ACCEPT[1], lat=44.0)
        r3 = await _post(async_client, "/api/alerts/subscribe", existing)
        assert r3.status_code == 200
    assert await PushSubscription.objects.acount() == 1


# -- unsubscribe --------------------------------------------------------------


async def test_unsubscribe_deletes_and_is_idempotent(async_client):
    with override_settings(**ENABLED):
        await _post(async_client, "/api/alerts/subscribe", _body())
        assert await PushSubscription.objects.acount() == 1
        r1 = await _post(async_client, "/api/alerts/unsubscribe", {"endpoint": _VALID_ENDPOINT})
        assert r1.status_code == 200
        assert await PushSubscription.objects.acount() == 0
        # Deleting an already-absent endpoint still succeeds (idempotent).
        r2 = await _post(async_client, "/api/alerts/unsubscribe", {"endpoint": _VALID_ENDPOINT})
        assert r2.status_code == 200


async def test_unsubscribe_missing_endpoint_400(async_client):
    with override_settings(**ENABLED):
        resp = await _post(async_client, "/api/alerts/unsubscribe", {})
    assert resp.status_code == 400


# -- advert -------------------------------------------------------------------


async def test_frames_advert_push_enabled(async_client):
    from datetime import UTC  # noqa: PLC0415
    from datetime import datetime  # noqa: PLC0415

    from radar.models import RadarFrame  # noqa: PLC0415

    now = int(datetime.now(tz=UTC).timestamp())
    await RadarFrame.objects.acreate(
        timestamp=now - 300,
        provider="rainviewer",
        tile_count=62,
        status="ok",
        missing=[],
    )
    with override_settings(**ENABLED):
        resp = await async_client.get("/api/radar/frames")
    push = resp.json()["lightning"]["push"]
    assert push["enabled"] is True
    assert push["vapid_public_key"] == "BPUBLICKEY"


async def test_frames_advert_push_disabled_by_default(async_client):
    from datetime import UTC  # noqa: PLC0415
    from datetime import datetime  # noqa: PLC0415

    from radar.models import RadarFrame  # noqa: PLC0415

    now = int(datetime.now(tz=UTC).timestamp())
    await RadarFrame.objects.acreate(
        timestamp=now - 300,
        provider="rainviewer",
        tile_count=62,
        status="ok",
        missing=[],
    )
    resp = await async_client.get("/api/radar/frames")
    assert resp.json()["lightning"]["push"]["enabled"] is False
