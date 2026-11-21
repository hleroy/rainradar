"""DB connection hygiene for the long-lived archiver process.

Django discards dead connections only at request start/end
(``close_old_connections`` on the request signals). The archiver never serves a
request: polls, the strike writer, the push evaluator and the janitor all share one
connection for the life of the process. So once Postgres terminates it — a server
restart, the deploy swap — every later ORM call fails with "the connection is
closed" until the container restarts. Calling :func:`discard_unusable_connections`
on an error path closes such a connection, and the next query reconnects.

It is deliberately **not** ``close_old_connections``: with ``CONN_MAX_AGE = 0`` that
closes even a healthy connection, and this runs only after a failure, so a healthy
connection must survive it.
"""

from __future__ import annotations

import contextlib
import logging

from asgiref.sync import sync_to_async
from django.db import connections

from radar.logging_json import emit

logger = logging.getLogger("radar.db")


def _discard_unusable_sync() -> list[str]:
    discarded = []
    for conn in connections.all(initialized_only=True):
        if conn.connection is not None and not conn.is_usable():
            with contextlib.suppress(Exception):
                conn.close()
            discarded.append(conn.alias)
    return discarded


async def discard_unusable_connections() -> None:
    """Close any DB connection the server has dropped, so the next query reconnects.

    Safe to call from any ``except`` block: it never raises. Runs in the ORM's own
    thread (``thread_sensitive``), because Django connections are thread-local.
    """
    try:
        discarded = await sync_to_async(_discard_unusable_sync)()
    except Exception:  # must never escape the error handler that called it
        logger.exception("checking DB connections failed")
        return
    for alias in discarded:
        emit(logger, logging.WARNING, "db_connection_discarded", service="db", alias=alias)
