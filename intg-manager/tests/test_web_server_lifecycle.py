"""HTTP sessions must not survive the web server's owning event loop."""

import asyncio
import os
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from unfurled import Remote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import web_server as ws  # noqa: E402


@pytest.fixture
def server(monkeypatch):
    # Avoid persistent configuration and network I/O; use real unfurled/aiohttp
    # resources, with Hypercorn's serving coroutine supplied by each test.
    instance = ws.WebServer.__new__(ws.WebServer)
    instance._host = "127.0.0.1"
    instance._port = 0
    instance._running = False
    instance._server_thread = None
    remote = Remote("http://127.0.0.1/api/", api_key="test")
    instance._owned_remote_clients = [remote]
    monkeypatch.setattr(ws, "_remote_clients", {"test": remote})
    monkeypatch.setattr(ws, "cleanup_stale_archives", lambda: None)
    yield instance, remote
    # Also release leaked sessions when testing the pre-fix implementation.
    asyncio.run(remote.close())


@pytest.mark.parametrize("startup_error", [False, True])
def test_server_exit_closes_sessions_before_loop_and_allows_restart(
    server, monkeypatch, startup_error
):
    instance, remote = server
    sessions = []

    async def serve(*_args, **_kwargs):
        await remote.api.__aenter__()
        session = remote.api._session
        sessions.append(session)
        assert session._loop is asyncio.get_running_loop()
        if startup_error:
            raise OSError("Simulated bind failure after lifespan startup")

    monkeypatch.setattr("hypercorn.asyncio.serve", serve)

    for _ in range(2):
        instance._running = True
        instance._run_server()
        assert sessions[-1].closed
        assert instance._loop.is_closed()
        assert instance.is_running is False

    assert sessions[0] is not sessions[1]


def test_stop_closes_sessions_after_inflight_requests_finish(server, monkeypatch):
    instance, remote = server
    started = threading.Event()
    sessions = []

    async def serve(_app, _config, *, shutdown_trigger):
        await remote.api.__aenter__()
        sessions.append(remote.api._session)
        started.set()
        await shutdown_trigger()
        # Hypercorn drains accepted requests before returning. A request can
        # still acquire an HTTP session after the shutdown signal is received.
        await remote.api.__aenter__()
        sessions.append(remote.api._session)

    monkeypatch.setattr("hypercorn.asyncio.serve", serve)
    instance.start()
    try:
        assert started.wait(timeout=5)
    finally:
        instance.stop()

    assert len(sessions) == 2
    assert all(session.closed for session in sessions)
    assert instance._loop.is_closed()


def test_exit_cancels_background_work_before_closing_sessions(server, monkeypatch):
    instance, remote = server
    sessions = []
    cancelled = []

    async def background_work():
        try:
            await asyncio.Future()
        finally:
            # Cancellation handlers may still use the client, so they must run
            # before the final close and before the owning loop exits.
            await remote.api.__aenter__()
            sessions.append(remote.api._session)
            cancelled.append(True)

    async def serve(*_args, **_kwargs):
        asyncio.create_task(background_work())
        await asyncio.sleep(0)

    monkeypatch.setattr("hypercorn.asyncio.serve", serve)
    instance._running = True
    instance._run_server()

    assert cancelled == [True]
    assert sessions[0].closed
    assert not asyncio.all_tasks(instance._loop)


def test_exit_only_closes_clients_owned_by_that_server(server, monkeypatch):
    instance, remote = server
    replacement = SimpleNamespace(close=AsyncMock())
    sessions = []

    async def serve(*_args, **_kwargs):
        await remote.api.__aenter__()
        sessions.append(remote.api._session)
        # A slow shutdown can outlast stop()'s join timeout, allowing a new
        # server to publish clients before the previous server exits.
        monkeypatch.setattr(ws, "_remote_clients", {"replacement": replacement})

    monkeypatch.setattr("hypercorn.asyncio.serve", serve)
    instance._running = True
    instance._run_server()

    assert sessions[0].closed
    replacement.close.assert_not_awaited()

