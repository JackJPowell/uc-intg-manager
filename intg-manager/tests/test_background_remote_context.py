"""Scheduled Remote checks use their own Remote, without a browser session."""

import asyncio
import logging
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import web_server as ws  # noqa: E402


def test_orphan_check_localizes_and_notifies_without_request_context(monkeypatch):
    notified = []

    async def find_orphaned_entities():
        return [
            {
                "activity_id": "watch-tv",
                "activity_name": {"en_GB": "Watch TV", "de_DE": "Fernsehen"},
            }
        ]

    async def notify_orphaned_entities(names, ids):
        notified.append((names, ids))

    remote = SimpleNamespace(
        helpers=SimpleNamespace(find_orphaned_entities=find_orphaned_entities),
        settings=SimpleNamespace(localization=SimpleNamespace(language_code="de_DE")),
    )
    monkeypatch.setitem(ws._remote_clients, "remote-1", remote)
    def no_request_context():
        raise AssertionError("No request context")

    monkeypatch.setattr(ws, "get_active_remote_id", no_request_context)
    def notifications_for(remote_id):
        assert remote_id == "remote-1"
        return SimpleNamespace(notify_orphaned_entities=notify_orphaned_entities)

    monkeypatch.setattr(ws, "get_notification_manager", notifications_for)

    server = object.__new__(ws.WebServer)
    asyncio.run(server.check_orphaned_entities("remote-1"))

    assert notified == [(["Fernsehen"], ["watch-tv"])]


def test_automatic_update_activity_check_localizes_without_request_context(
    monkeypatch, caplog
):
    async def get_charger():
        return {"power_supply": True}

    async def get_activities():
        return [
            {
                "entity_id": "watch-tv",
                "name": {"en_GB": "Watch TV", "de_DE": "Fernsehen"},
                "attributes": {"state": "ON"},
            }
        ]

    remote = SimpleNamespace(
        api=SimpleNamespace(get_charger=get_charger, get_activities=get_activities),
        settings=SimpleNamespace(localization=SimpleNamespace(language_code="de_DE")),
    )
    monkeypatch.setitem(ws._remote_clients, "remote-1", remote)
    def no_request_context():
        raise AssertionError("No request context")

    monkeypatch.setattr(ws, "get_active_remote_id", no_request_context)

    with caplog.at_level(logging.INFO):
        assert asyncio.run(ws._automatic_update_is_safe("remote-1")) is False
    assert "active activities: Fernsehen" in caplog.text
