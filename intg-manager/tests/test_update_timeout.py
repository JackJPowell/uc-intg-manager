"""The complete update must finish before its operation lock can expire."""

import asyncio
import logging
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import web_server as ws  # noqa: E402


@pytest.fixture
def updates(monkeypatch):
    state = SimpleNamespace(
        bundled=True, stall=None, pause=None, phases=[], cancelled=[],
        archives=[], installs=[], refreshes=[],
    )

    async def checkpoint(phase):
        state.phases.append(phase)
        if state.pause is not None:
            await state.pause(phase)
        if state.stall == phase:
            assert ws._operation_state_for("remote").in_progress
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                state.cancelled.append(phase)
                raise

    class Archive:
        size = 1
        filename = "release.tar.gz"

        def __init__(self):
            self.closed = False

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.closed = True

        async def install(self, api, *, update=False):
            await checkpoint("install")
            state.installs.append(self)

    async def download(*args, **kwargs):
        await checkpoint("download")
        archive = Archive()
        state.archives.append(archive)
        return archive

    async def build_update(driver):
        await checkpoint("build")
        return {"updates": 1}, await download()

    def integration(driver_id):
        return ws.IntegrationInfo(
            instance_id=f"{driver_id}.main", driver_id=driver_id,
            name=driver_id, version="1.0.0", custom=True,
            home_page=f"https://github.com/example/{driver_id}",
            update_available=True, can_update=True,
            bundle_driver={"driver_id": driver_id} if state.bundled else None,
        )

    async def installed(remote_id):
        await checkpoint("lookup")
        return [integration("first"), integration("second")]

    async def refresh(remote_id):
        state.refreshes.append(remote_id)

    async def safe(remote_id):
        return True

    client = SimpleNamespace(api=SimpleNamespace())
    monkeypatch.setattr(ws, "_remote_clients", {"remote": client})
    monkeypatch.setattr(ws, "_operation_states", {})
    monkeypatch.setattr(ws, "_operation_locks", {})
    monkeypatch.setattr(ws, "_UPDATE_OPERATION_TIMEOUT", 0.01)
    monkeypatch.setattr(ws, "_get_active_remote_client", lambda: client)
    monkeypatch.setattr(ws, "get_active_remote_id", lambda: "remote")
    monkeypatch.setattr(ws, "_get_installed_integrations", installed)
    monkeypatch.setattr(ws, "_refresh_version_cache", refresh)
    monkeypatch.setattr(ws, "_automatic_update_is_safe", safe)
    monkeypatch.setattr(ws, "_inplace_upgrade_available", lambda _rid: True)
    monkeypatch.setattr(ws, "is_remote_online", lambda _rid: True)
    monkeypatch.setattr(ws, "load_registry", lambda: [])
    monkeypatch.setattr(ws.Settings, "load", lambda **kwargs: SimpleNamespace(auto_update=True))
    monkeypatch.setattr(ws, "_github_client", SimpleNamespace(download_release_asset=download))
    monkeypatch.setattr(ws._bundle_client, "build_update", build_update)
    return state


@pytest.mark.parametrize("automatic", [False, True], ids=["manual", "automatic"])
@pytest.mark.parametrize("bundled,phase", [
    (False, "lookup"), (False, "download"), (False, "install"),
    (True, "lookup"), (True, "build"), (True, "download"), (True, "install"),
])
def test_update_timeout_cancels_work_and_releases_lock(updates, caplog, automatic, bundled, phase):
    updates.bundled = bundled
    updates.stall = phase

    async def run():
        if automatic:
            await ws._run_automatic_updates("remote")
        else:
            async with ws.app.test_request_context("/", method="POST"):
                response, status = await ws.update_integration_inplace("first")
                assert status == 504
                assert (await response.get_json())["error"]["code"] == "operation_timeout"

        # The timed-out operation no longer blocks the next mutation.
        async with ws.app.test_request_context("/"):
            assert await ws._try_acquire_operation_lock("next update", "remote") is None
            await ws._release_operation_lock("remote", "next update")

    with caplog.at_level(logging.INFO):
        asyncio.run(run())

    assert updates.cancelled == [phase]
    assert updates.installs == []
    assert all(archive.closed for archive in updates.archives)
    assert len(updates.archives) == (1 if phase == "install" else 0)
    assert not ws._operation_state_for("remote").in_progress
    assert "operation limit" in caplog.text
    assert "Automatically updated" not in caplog.text


@pytest.mark.parametrize("bundled", [False, True], ids=["regular", "bundle"])
def test_automatic_batch_shares_one_timeout(updates, monkeypatch, bundled):
    updates.bundled = bundled
    monkeypatch.setattr(ws, "_UPDATE_OPERATION_TIMEOUT", 0.1)

    async def pause(phase):
        if phase == "install":
            await asyncio.sleep(0.04)
        elif phase == "download" and updates.installs:
            # Individually both updates would fit within 0.1 seconds, but
            # their combined preparation and installation do not.
            await asyncio.sleep(0.08)

    updates.pause = pause
    asyncio.run(ws._run_automatic_updates("remote"))

    assert len(updates.installs) == 1
    assert updates.phases.count("download") == 2
    assert all(archive.closed for archive in updates.archives)
    assert not ws._operation_state_for("remote").in_progress
    assert updates.refreshes == ["remote"]
