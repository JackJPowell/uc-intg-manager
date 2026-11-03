"""Automatic updates recheck Remote usage after preparing an archive."""

import asyncio
import logging
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import web_server as ws  # noqa: E402


@pytest.mark.parametrize("bundled", [False, True], ids=["regular", "bundle"])
@pytest.mark.parametrize("after_download", ["idle", "undocked", "activity", "unreachable"])
def test_automatic_updates_recheck_before_install(
    monkeypatch, caplog, bundled, after_download
):
    state = {"prepared": False}
    archives = []
    installs = []
    refreshes = []

    async def get_charger():
        if state["prepared"] and after_download == "unreachable":
            raise ws.UnfurledError("Remote is unreachable")
        return {"power_supply": not (state["prepared"] and after_download == "undocked")}

    async def get_activities():
        if state["prepared"] and after_download == "activity":
            return [{"entity_id": "watch-tv", "attributes": {"state": "ON"}}]
        return []

    client = SimpleNamespace(
        api=SimpleNamespace(get_charger=get_charger, get_activities=get_activities),
        settings=SimpleNamespace(localization=SimpleNamespace(language_code="en_US")),
    )

    class Archive:
        size = 123
        filename = "integration.tar.gz"

        def __init__(self):
            self.closed = False

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.closed = True

        async def install(self, api, *, update=False):
            assert api is client.api and update
            installs.append(self)

    async def prepare(*args, **kwargs):
        # Preparation represents a cloud build/download while the user can
        # start using the Remote. The initial safety checks have already run.
        state["prepared"] = True
        archive = Archive()
        archives.append(archive)
        return ({"updates": 1}, archive) if bundled else archive

    integrations = [
        ws.IntegrationInfo(
            instance_id=f"{driver_id}.main",
            driver_id=driver_id,
            name=driver_id,
            version="1.0.0",
            home_page=f"https://github.com/example/{driver_id}",
            custom=True,
            update_available=True,
            can_update=True,
            bundle_driver={"driver_id": driver_id} if bundled else None,
        )
        for driver_id in ("first", "second")
    ]

    async def installed(remote_id):
        return integrations

    async def refresh(remote_id):
        refreshes.append(remote_id)

    monkeypatch.setattr(ws, "_remote_clients", {"test-remote": client})
    monkeypatch.setattr(ws, "_operation_states", {})
    monkeypatch.setattr(ws, "_operation_locks", {})
    monkeypatch.setattr(ws, "_get_installed_integrations", installed)
    monkeypatch.setattr(ws, "_refresh_version_cache", refresh)
    monkeypatch.setattr(ws, "load_registry", lambda: [])
    monkeypatch.setattr(ws, "is_remote_online", lambda _rid: True)
    monkeypatch.setattr(ws, "_inplace_upgrade_available", lambda _rid: True)
    monkeypatch.setattr(
        ws.Settings, "load", lambda **kwargs: SimpleNamespace(auto_update=True)
    )
    monkeypatch.setattr(ws, "_github_client", SimpleNamespace(download_release_asset=prepare))
    monkeypatch.setattr(ws._bundle_client, "build_update", prepare)

    with caplog.at_level(logging.INFO):
        asyncio.run(ws._run_automatic_updates("test-remote"))

    expected = 2 if after_download == "idle" else 0
    assert len(installs) == expected
    assert len(archives) == (2 if after_download == "idle" else 1)
    assert all(archive.closed for archive in archives)
    assert caplog.text.count("Automatically updated") == expected
    assert not ws._operation_state_for("test-remote").in_progress
    assert refreshes == ["test-remote"]
