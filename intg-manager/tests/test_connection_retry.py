"""Connection tests must recover when Wi-Fi is unavailable immediately after wake."""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, Mock, call

import pytest
from ucapi_framework import DeviceEvents

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import device as device_module  # noqa: E402
from const import RemoteConfig  # noqa: E402
from device import IntegrationManagerDevice  # noqa: E402
from unfurled.helpers.exceptions import UnfurledError  # noqa: E402


@pytest.fixture
def make_device(monkeypatch):
    monkeypatch.setenv("UC_CONFIG_HOME", "/config")
    monkeypatch.setattr(device_module, "register_remote_config", Mock())
    monkeypatch.setattr(
        IntegrationManagerDevice, "_ensure_remote_in_manager_json", Mock()
    )
    monkeypatch.setattr(device_module, "set_remote_online", Mock())

    def create():
        client = Mock(get_version=AsyncMock(), close=AsyncMock())
        monkeypatch.setattr(device_module, "CoreAPI", Mock(return_value=client))
        instance = IntegrationManagerDevice(
            RemoteConfig("remote-1", "Remote 3", "192.168.178.34"),
            asyncio.get_running_loop(),
        )
        instance._on_docked = AsyncMock()
        instance.poll_device = AsyncMock()
        return instance

    return create


@pytest.mark.parametrize(
    "failure",
    [UnfurledError("Wi-Fi unavailable"), asyncio.TimeoutError(), OSError("No route")],
)
def test_failed_tests_retry_until_recovery_and_start_polling(
    make_device, monkeypatch, failure
):
    sleep = asyncio.sleep
    delays = []

    async def retry_delay(delay):
        delays.append(delay)
        await sleep(0)

    monkeypatch.setattr(device_module.asyncio, "sleep", retry_delay)

    async def run():
        instance = make_device()
        connected = Mock()
        instance.events.on(DeviceEvents.CONNECTED, connected)
        # Start connected, then reproduce the failed connection test after wake.
        instance._client.get_version.return_value = {"os": "2.9.3"}
        try:
            assert await instance.connect() is True
            await instance.disconnect()
            connected.reset_mock()
            instance._client.get_version.side_effect = [failure] * 7 + [{}]

            assert await asyncio.wait_for(instance.connect(), timeout=1) is True
            await sleep(0)

            assert delays == [2, 4, 8, 16, 30, 30, 30]
            assert instance._client.get_version.await_count == 9
            assert instance._connected is True
            assert instance.is_connected is True
            instance.poll_device.assert_awaited()
            assert instance._on_docked.await_count == 2
            connected.assert_called_once_with("remote-1")
            online_updates = device_module.set_remote_online.call_args_list
            assert online_updates[-8:] == [call("remote-1", False)] * 7 + [
                call("remote-1", True)
            ]
        finally:
            await instance.disconnect()

    asyncio.run(run())


@pytest.mark.parametrize("in_flight", [False, True])
def test_disconnect_cancels_shared_connection_and_next_wake_can_recover(
    make_device, monkeypatch, in_flight
):
    sleep = asyncio.sleep

    async def run():
        instance = make_device()
        waiting = asyncio.Event()
        cancelled = asyncio.Event()

        async def wait_for_disconnect(*_args):
            waiting.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        if in_flight:
            instance._client.get_version.side_effect = wait_for_disconnect
        else:
            instance._client.get_version.side_effect = UnfurledError("Offline")
            monkeypatch.setattr(device_module.asyncio, "sleep", wait_for_disconnect)

        first = asyncio.create_task(instance.connect())
        second = None
        try:
            await asyncio.wait_for(waiting.wait(), timeout=1)
            second = asyncio.create_task(instance.connect())
            await sleep(0)
            instance._client.get_version.assert_awaited_once()
            instance._on_docked.assert_not_awaited()

            await instance.disconnect()
            results = await asyncio.gather(first, second, return_exceptions=True)
            assert all(isinstance(result, asyncio.CancelledError) for result in results)
            assert cancelled.is_set()
            assert instance._connected is False
            assert instance.is_connected is False
            instance._client.close.assert_awaited_once()
            await sleep(0)
            instance._client.get_version.assert_awaited_once()

            # A subsequent wake gets a fresh task and starts with the short delay.
            delays = []

            async def retry_delay(delay):
                delays.append(delay)
                await sleep(0)

            monkeypatch.setattr(device_module.asyncio, "sleep", retry_delay)
            instance._client.get_version.side_effect = [UnfurledError("Offline"), {}]
            assert await asyncio.wait_for(instance.connect(), timeout=1) is True
            assert delays == [2]
            instance._on_docked.assert_awaited_once()
        finally:
            await instance.disconnect()
            await asyncio.gather(
                first, *([second] if second is not None else []), return_exceptions=True
            )

    asyncio.run(run())


def test_repeated_connect_events_share_successful_connection_and_polling(make_device):
    async def run():
        instance = make_device()
        instance._client.get_version.return_value = {}
        connected = Mock()
        instance.events.on(DeviceEvents.CONNECTED, connected)
        try:
            assert await asyncio.gather(instance.connect(), instance.connect()) == [
                True,
                True,
            ]
            poll_task = instance._poll_task
            assert await instance.connect() is True
            assert instance._poll_task is poll_task
            instance._client.get_version.assert_awaited_once()
            instance._on_docked.assert_awaited_once()
            connected.assert_called_once_with("remote-1")
        finally:
            await instance.disconnect()

    asyncio.run(run())
