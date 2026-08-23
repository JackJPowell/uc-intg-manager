"""
Integration Manager Driver.

This is the main entry point for the integration manager. It initializes
the driver, sets up logging, and starts the integration API.

:copyright: (c) 2025.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import asyncio
import logging
import os
import socket

import aiohttp
import ucapi
import device as _device_module
from const import RemoteConfig, WEB_SERVER_PORT, is_external_mode
from data_migration import migrate
from device import IntegrationManagerDevice, _all_remote_configs
from discover import ManagerDiscovery
from log_handler import setup_log_handler
from setup import RemoteSetupFlow
from ucapi_framework import BaseConfigManager, BaseIntegrationDriver, get_config_path
from web_server import WebServer, is_remote_online

_LOG = logging.getLogger("driver")


def _remote_id_from_ws(websocket) -> str | None:
    """Identifier of the configured remote whose IP matches the WebSocket peer."""
    if not websocket or not getattr(websocket, "remote_address", None):
        return None
    host = websocket.remote_address[0]
    for cfg in _all_remote_configs:
        if cfg.address == host:
            return cfg.identifier
    return None


class IntegrationManagerDriver(BaseIntegrationDriver):
    """Dispatch ucapi events to the originating remote (by client IP).

    When the websocket cannot be mapped to a configured remote (e.g.,
    rootless Docker rewrites the source IP to the bridge gateway), run a
    parallel HTTP probe against every configured remote and reconcile
    each device's connect/disconnect state with the probe result. The
    actual sender is then implicit: whichever remote just changed
    reachability is what the event was about.
    """

    async def _probe_and_reconcile_one(self, device_id: str) -> None:
        """Probe a single remote and align its device connect state."""
        ws = _device_module._web_server_instance
        if ws is None or not ws.is_running:
            return
        await ws.run_on_server_loop(ws.check_connectivity(device_id))
        device = self._device_instances.get(device_id)
        if device is None:
            return
        online = is_remote_online(device_id)
        connected = device.is_connected
        if online and not connected:
            _LOG.debug("Reconcile: %s online but not connected - connecting", device_id)
            self._loop.create_task(device.connect())
        elif not online and connected:
            _LOG.debug("Reconcile: %s offline but connected - disconnecting", device_id)
            self._loop.create_task(device.disconnect())

    async def _reconcile_devices_from_probe(self) -> None:
        """Probe every configured remote in parallel and align each device's
        connect state as soon as its own probe completes."""
        ws = _device_module._web_server_instance
        if ws is None or not ws.is_running:
            return
        device_ids = list(self._device_instances.keys())
        if not device_ids:
            return
        results = await asyncio.gather(
            *(self._probe_and_reconcile_one(did) for did in device_ids),
            return_exceptions=True,
        )
        for did, result in zip(device_ids, results):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                _LOG.warning("[%s] Reconcile raised: %r", did, result)

    async def on_r2_connect_cmd(self, websocket=None) -> None:
        """Connect the originating remote, or reconcile by probe if unmappable."""
        # DeviceState is set once and never flipped back: in external mode
        # the host is reachable while the process is alive, and the state
        # broadcasts to every connected remote.
        await self.api.set_device_state(ucapi.DeviceStates.CONNECTED)
        rid = _remote_id_from_ws(websocket)
        device = self._device_instances.get(rid) if rid else None
        if device:
            _LOG.debug("Connect command from %s", rid)
            self._loop.create_task(device.connect())
        else:
            _LOG.debug(
                "Connect command without identifiable source - reconciling by probe"
            )
            self._loop.create_task(self._reconcile_devices_from_probe())
        self._loop.create_task(self._recheck_all_connectivity(delay=3))

    async def on_r2_disconnect_cmd(self, websocket=None) -> None:
        """Disconnect the originating remote, or reconcile by probe if unmappable."""
        rid = _remote_id_from_ws(websocket)
        device = self._device_instances.get(rid) if rid else None
        if device:
            _LOG.debug("Disconnect command from %s", rid)
            self._loop.create_task(device.disconnect())
            return
        _LOG.debug(
            "Disconnect command without identifiable source - reconciling by probe"
        )
        self._loop.create_task(self._reconcile_devices_from_probe())

    async def on_r2_enter_standby(self, websocket=None) -> None:
        """Disconnect the remote that entered standby, or reconcile by probe."""
        rid = _remote_id_from_ws(websocket)
        device = self._device_instances.get(rid) if rid else None
        if device:
            _LOG.debug("Enter standby from %s", rid)
            self._loop.create_task(device.disconnect())
            return
        _LOG.debug(
            "Enter standby without identifiable source - reconciling by probe"
        )
        self._loop.create_task(self._reconcile_devices_from_probe())

    async def on_r2_exit_standby(self, websocket=None) -> None:
        """Reconnect the originating remote, or reconcile by probe if unmappable."""
        rid = _remote_id_from_ws(websocket)
        device = self._device_instances.get(rid) if rid else None
        if device:
            _LOG.debug("Exit standby from %s", rid)
            self._loop.create_task(device.connect())
        else:
            _LOG.debug(
                "Exit standby without identifiable source - reconciling by probe"
            )
            self._loop.create_task(self._reconcile_devices_from_probe())
        self._loop.create_task(self._recheck_all_connectivity(delay=3))

    async def _recheck_all_connectivity(self, delay: float = 3) -> None:
        """Wait briefly for connections to settle, then update all remote online statuses."""
        await asyncio.sleep(delay)
        ws = _device_module._web_server_instance
        if ws and ws.is_running:
            _LOG.debug(
                "Rechecking connectivity for all remotes after connect/exit-standby"
            )
            await ws.run_on_server_loop(ws.check_all_remote_connectivity(force=True))


_HEALTH_OK_BODIES = frozenset({"OK", "UPDATING"})


def _tcp_port_in_use(port: int, timeout: float = 0.5) -> bool:
    """Return True if any process is accepting TCP connections on 127.0.0.1:port.

    Distinct from /health probing: this only answers "is the port bound?",
    so it can detect a wedged-but-still-listening old server that would
    refuse a new bind on the same port.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        return sock.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False
    finally:
        sock.close()


async def _web_server_port_reachable(port: int = WEB_SERVER_PORT, timeout: float = 2.0) -> bool:
    """Return True if the local web server responds on /health with an
    expected Integration Manager body ("OK" or "UPDATING").

    Verifies both HTTP 200 and the body so an unrelated process answering
    on the same port can't masquerade as a healthy Integration Manager.
    """
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as session:
            async with session.get(f"http://127.0.0.1:{port}/health") as resp:
                if resp.status != 200:
                    return False
                body = (await resp.text()).strip()
                return body in _HEALTH_OK_BODIES
    except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
        return False


async def _wait_for_web_server_ready(
    ws, max_wait: float = 5.0, poll: float = 0.2
) -> bool:
    """Return True once the given WebServer is running and its port accepts
    connections. Gives up after max_wait wall-clock seconds (accounting for
    time spent inside the HTTP probe itself)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_wait
    while loop.time() < deadline:
        await asyncio.sleep(poll)
        if not ws.is_running:
            return False
        remaining = max(0.1, deadline - loop.time())
        if await _web_server_port_reachable(ws.port, timeout=remaining):
            return True
    return False


async def _web_server_watchdog(interval: float = 30) -> None:
    """Restart the web server if it stops serving and refresh remote status.

    Restart triggers: instance cleared, `is_running` is False, or the
    listening port stops accepting connections. The connectivity probe
    runs every cycle to keep status fresh while device polling is idle.
    """
    # Let the server bind before the first probe.
    await asyncio.sleep(0.2)
    while True:
        try:
            if not _all_remote_configs:
                await asyncio.sleep(interval)
                continue
            ws = _device_module._web_server_instance
            port_alive = (
                await _web_server_port_reachable(ws.port) if ws is not None else False
            )
            if ws is None or not ws.is_running or not port_alive:
                _LOG.warning(
                    "Watchdog: web server unhealthy (instance=%s, running=%s, port_alive=%s) - restarting",
                    "present" if ws else "missing",
                    getattr(ws, "is_running", False),
                    port_alive,
                )
                if ws is not None:
                    try:
                        # stop() joins the Hypercorn thread (up to 5s).
                        await asyncio.to_thread(ws.stop)
                    except Exception as e:
                        _LOG.warning("Watchdog: stop() during cleanup failed: %s", e)
                    # Wait for the OS to release the listening port before
                    # creating a new WebServer; otherwise the new instance
                    # races the kernel TIME_WAIT and may fail to bind. Use a
                    # raw TCP probe rather than /health because a wedged old
                    # server can stop answering /health while still holding
                    # the bind.
                    stopped_port = ws.port
                    port_released = False
                    for _ in range(15):
                        if not await asyncio.to_thread(_tcp_port_in_use, stopped_port):
                            port_released = True
                            break
                        await asyncio.sleep(0.2)
                    if not port_released:
                        # Old listener still bound — abort this cycle.
                        # Creating a new WebServer would clear shared globals
                        # the live server depends on and corrupt its state.
                        _LOG.error(
                            "Watchdog: port %d still bound after stop() - skipping restart this cycle",
                            stopped_port,
                        )
                        await asyncio.sleep(interval)
                        continue
                else:
                    # No instance to stop, but a previous Hypercorn thread
                    # may still hold the port (stop() returned after its 5s
                    # join timeout). Constructing WebServer now would clear
                    # the shared globals the old thread still references.
                    if await asyncio.to_thread(_tcp_port_in_use, WEB_SERVER_PORT):
                        _LOG.error(
                            "Watchdog: port %d still bound while instance is missing - skipping restart this cycle",
                            WEB_SERVER_PORT,
                        )
                        await asyncio.sleep(interval)
                        continue
                # Only publish the new instance after the grace period
                # confirms the background thread is still running and the
                # port is actually accepting connections. Hypercorn may
                # bind-fail (port in use, OSError) after start() has
                # already flipped `_running` True, so an early assignment
                # leaks a doomed instance to other tasks.
                new_ws = WebServer(remote_configs=_all_remote_configs)
                new_ws.start()
                if await _wait_for_web_server_ready(new_ws):
                    _device_module._web_server_instance = new_ws
                    _LOG.info("Watchdog: web server restarted successfully")
                    ws = new_ws
                else:
                    # Tear down the unhealthy instance so the next iteration
                    # starts from a clean slate.
                    _LOG.error(
                        "Watchdog: restart attempt failed (running=%s) - retrying in %ss",
                        new_ws.is_running,
                        interval,
                    )
                    try:
                        await asyncio.to_thread(new_ws.stop)
                    except Exception as e:
                        _LOG.warning(
                            "Watchdog: stop() of failed instance raised: %s", e
                        )
                    _device_module._web_server_instance = None
                    await asyncio.sleep(interval)
                    continue

            try:
                await ws.run_on_server_loop(ws.check_all_remote_connectivity())
            except Exception as e:
                _LOG.debug("Watchdog connectivity probe failed: %s", e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _LOG.error("Watchdog loop error: %s", e, exc_info=True)
        await asyncio.sleep(interval)


async def main():
    """Start the Integration Manager driver."""
    logging.basicConfig(
        format="%(asctime)s.%(msecs)03d %(levelname)s:%(name)s:%(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Set up the ring buffer log handler to capture logs for the web UI
    setup_log_handler()

    # Configure logging level from environment variable
    level = os.getenv("UC_LOG_LEVEL", "DEBUG").upper()
    logging.getLogger("driver").setLevel(level)
    logging.getLogger("device").setLevel(level)
    logging.getLogger("setup").setLevel(level)
    logging.getLogger("web_server").setLevel(level)
    logging.getLogger("unfurled").setLevel(level)
    logging.getLogger("github_api").setLevel(level)
    logging.getLogger("integration_service").setLevel(level)
    logging.getLogger("data_migration").setLevel(level)
    logging.getLogger("backup_service").setLevel(level)

    # Force migration to v2.0 format if needed
    # This ensures all subsequent code can assume v2.0 structure
    migrate()

    # Initialize the integration driver
    # This integration doesn't expose entities - it's purely a web UI
    driver = IntegrationManagerDriver(
        device_class=IntegrationManagerDevice,
        entity_classes=[],  # No entities exposed
        driver_id="intg_manager_driver",
    )

    # Configure the device config manager
    driver.config_manager = BaseConfigManager(
        get_config_path(driver.api.config_dir_path),
        driver.on_device_added,
        driver.on_device_removed,
        config_class=RemoteConfig,
    )

    # Register all configured devices from config file
    await driver.register_all_configured_devices()

    # External mode: web server is independent of remote lifecycle.
    # On-remote installs start it lazily from dock/charge state.
    if is_external_mode():
        if _all_remote_configs:
            existing = _device_module._web_server_instance
            if existing is not None and existing.is_running:
                # A device's _on_docked() (or a prior boot path) already
                # published a running instance. Constructing another
                # WebServer here would clear shared module globals out
                # from under the live server thread.
                _LOG.info(
                    "External mode detected at boot - web server already running, reusing"
                )
            else:
                _LOG.info(
                    "External mode detected at boot - starting web server with %d configured remote(s)",
                    len(_all_remote_configs),
                )
                ws = WebServer(remote_configs=_all_remote_configs)
                ws.start()
                # Verify the background thread bound the port before
                # publishing. If start fails (port in use), leave the
                # global as None and let the watchdog retry.
                if await _wait_for_web_server_ready(ws):
                    _device_module._web_server_instance = ws
                else:
                    _LOG.error(
                        "External mode boot: web server failed to start - watchdog will retry"
                    )
                    try:
                        await asyncio.to_thread(ws.stop)
                    except Exception as e:
                        _LOG.warning(
                            "External mode boot: stop() of failed instance raised: %s",
                            e,
                        )
        else:
            # No remotes yet; watchdog brings the server up after setup.
            _LOG.info(
                "External mode detected at boot - no remotes configured yet",
            )

        asyncio.create_task(_web_server_watchdog(interval=30))

    # Set up the setup handler
    discovery = ManagerDiscovery("_uc-remote._tcp.local.", timeout=3)
    setup_handler = RemoteSetupFlow.create_handler(driver, discovery=discovery)

    # Initialize the API with the driver configuration
    await driver.api.init("driver.json", setup_handler)

    # Keep the driver running
    await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
