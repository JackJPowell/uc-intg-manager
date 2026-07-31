"""Dependency-free smoke checks for the React SPA and JSON API boundary."""

import ast
import json
import os
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "intg-manager" / "web_server.py"
SPA_ENTRY = ROOT / "intg-manager" / "static" / "app" / "index.html"


def test_root_redirect_and_manager_fallback_are_declared():
    source = SERVER.read_text(encoding="utf-8")
    assert '@app.route("/")' in source
    assert 'return redirect("/manager", 302)' in source
    assert '@app.route("/manager/<path:client_path>")' in source
    assert 'return await send_file(entrypoint, mimetype="text/html")' in source


def test_built_spa_has_a_react_mount_point():
    assert SPA_ENTRY.exists(), "Build the UI with `npm --prefix ui run build` first"
    entry = SPA_ENTRY.read_text(encoding="utf-8")
    assert 'id="root"' in entry
    assert "/static/app/assets/" in entry


def test_v1_routes_are_json_only_and_template_free():
    source = SERVER.read_text(encoding="utf-8")
    assert '@app.route("/api/v1/status")' in source
    assert 'return jsonify({"data": {"online": False, "docked": None}})' in source
    assert "render_template" not in source
    assert "TEMPLATE_DIR" not in source
    assert "htmx" not in source.lower()


def test_legacy_ui_and_json_aliases_are_removed():
    source = SERVER.read_text(encoding="utf-8")
    for route in (
        '"/integrations"',
        '"/available"',
        '"/settings"',
        '"/notifications"',
        '"/diagnostics"',
        '"/api/active-remote"',
        '"/api/backups/create"',
        '"/api/backup/',
        '"/api/notifications/',
    ):
        assert route not in source
    assert '@app.route("/api/v1/registry")' in source


def test_backup_capability_is_version_aware_without_legacy_restore_flow():
    source = SERVER.read_text(encoding="utf-8")
    card = (ROOT / "ui" / "src" / "components" / "IntegrationCard.tsx").read_text(
        encoding="utf-8"
    )
    backup_capabilities = (ROOT / "intg-manager" / "backup_capabilities.py").read_text(
        encoding="utf-8"
    )
    assert "def backup_support_status" in backup_capabilities
    assert "backup_available" in source
    assert "supports_automated_backup_restore" not in source
    assert "automatedBackupRestore" not in card
    assert "Backups supported" in card
    assert "const canCreateBackup" in card
    assert "item.connectionState !== 'not_configured'" in card
    serializer = source[
        source.index("def _integration_api_model") : source.index(
            "async def _get_latest_release_for_update"
        )
    ]
    assert 'installed = integration.state.upper() != "NOT_CONFIGURED"' in serializer
    assert (
        '"backup": can_mutate and installed and integration.backup_available'
        in serializer
    )


def test_operation_and_remote_lifecycle_are_scoped_to_the_active_remote():
    source = SERVER.read_text(encoding="utf-8")
    assert "_operation_states: dict[str, _OperationState]" in source
    assert "_operation_lock_for(remote_id)" in source
    assert "_replace_remotes_on_server_loop" in source
    assert "client.close()" in source


def test_automatic_updates_are_scheduled_and_do_not_require_backup_support():
    source = SERVER.read_text(encoding="utf-8")
    assert "await _run_automatic_updates(remote_id)" in source
    assert "Processing %d automatic integration update(s) sequentially" in source
    assert "can_auto_update" not in source
    auto_update_runner = source[
        source.index("async def _run_automatic_updates") : source.index(
            "async def _get_installed_integrations"
        )
    ]
    assert "integration.supports_backup" not in auto_update_runner


def test_automatic_updates_require_charging_and_no_running_activities():
    source = SERVER.read_text(encoding="utf-8")
    client_source = (ROOT / "intg-manager" / "sync_api.py").read_text(encoding="utf-8")
    assert "from unfurled import Remote" in source
    assert "class RemoteClient" not in client_source
    assert "SyncRemoteClient" not in client_source
    safety_check = source[
        source.index("async def _automatic_update_is_safe") : source.index(
            "async def _run_automatic_updates"
        )
    ]
    assert "await client.api.get_charger()" in safety_check
    assert "await client.api.get_activities()" in safety_check
    assert 'attributes.get("state", "")).upper() == "ON"' in safety_check


def test_polling_operations_are_marshalled_to_the_web_server_loop():
    server = SERVER.read_text(encoding="utf-8")
    device = (ROOT / "intg-manager" / "device.py").read_text(encoding="utf-8")
    driver = (ROOT / "intg-manager" / "driver.py").read_text(encoding="utf-8")
    assert "async def run_on_server_loop" in server
    assert "asyncio.run_coroutine_threadsafe(operation, loop)" in server
    assert "await web_server.run_on_server_loop(" in device
    assert (
        "await ws.run_on_server_loop(ws.check_all_remote_connectivity(force=True))"
        in driver
    )


def test_integration_lifecycle_uses_coreapi_operations():
    """Installation and configuration routes must not rely on legacy HTTP helpers."""
    source = SERVER.read_text(encoding="utf-8")
    lifecycle = source[
        source.index("async def update_integration_inplace") : source.index(
            "async def check_versions"
        )
    ]
    for operation in (
        "post_integration_install",
        "delete_integration",
        "delete_driver",
    ):
        assert operation in lifecycle


def test_integration_service_uses_current_unfurled_collection_method():
    source = (ROOT / "intg-manager" / "integration_service.py").read_text(
        encoding="utf-8"
    )
    assert "get_integration_instances" not in source
    assert "await self._remote.get_integrations()" in source


def test_bootstrapper_and_legacy_reinstall_paths_are_removed():
    source = SERVER.read_text(encoding="utf-8")
    assert "async def _perform_update_integration" not in source
    assert '"/api/driver/<driver_id>/update"' not in source
    assert '"/api/v1/self-update/restore"' not in source
    assert '"/api/dev/test-bootstrapper-setup"' not in source
    assert not (ROOT / "intg-bootstrapper").exists()
    assert not (ROOT / "dev" / "build-install-bootstrapper.sh").exists()


def test_release_workflow_builds_only_the_manager_and_packages_its_icon():
    workflow = (ROOT / ".github" / "workflows" / "build.yml").read_text(
        encoding="utf-8"
    )
    assert "intg-bootstrapper" not in workflow
    assert "intg-manager/static/img/intg-manager.png" in workflow
    assert "artifacts/intg-manager.png" in workflow
    assert "intg-${INTG_NAME}/templates:templates" not in workflow
    assert "\n  docker:\n" in workflow
    assert "npm --prefix ui run build" in workflow
    assert "uv run --frozen --group dev pytest intg-manager/tests -q" in workflow
    assert "uv export --frozen --no-dev" in workflow
    assert "-resize 90x90" in workflow


def test_diagnostics_uses_unfurled_helpers():
    source = SERVER.read_text(encoding="utf-8")
    assert "from unfurled import Remote" in source
    assert "helpers.find_orphaned_entities()" in source
    assert "helpers.find_unused_activity_entities()" in source
    assert "helpers.find_orphaned_ir_codesets()" in source
    assert "diagnostics_service" not in source


def test_remote_log_regex_compiles():
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    assignment = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "_REMOTE_LOG_LINE"
            for target in node.targets
        )
    )
    pattern = ast.literal_eval(assignment.value.args[0])
    compiled = re.compile(pattern)
    assert compiled.match("[2026-07-27T21:27:49Z DEBUG service: ready")
    journal = compiled.match(
        "2026-07-30 12:27:18.259341 +00:00 core ERROR [driver] unavailable"
    )
    assert journal and journal.group("timestamp") == "2026-07-30 12:27:18.259341 +00:00"
    assert journal.group("source") == "core"
    assert journal.group("level") == "ERROR"


def test_integration_log_normalization_supports_remote_compact_keys():
    source = SERVER.read_text(encoding="utf-8")
    normalizer = source[
        source.index("def _normalize_integration_log_entry") : source.index(
            '@app.route("/api/v1/integration-logs/services")'
        )
    ]
    assert 'normalized.get("ts")' in normalizer
    assert 'normalized.get("m")' in normalizer
    assert 'normalized.get("prio")' in normalizer


def test_integration_logs_accept_remote_plain_text_responses():
    source = SERVER.read_text(encoding="utf-8")
    log_routes = source[
        source.index("def _normalize_integration_log_payload") : source.index(
            '@app.route("/api/v1/integration-logs/export")'
        )
    ]
    assert "payload.splitlines()" in log_routes
    assert "as_text=True" in log_routes


def test_backup_import_applies_settings_without_restarting_the_manager():
    source = SERVER.read_text(encoding="utf-8")
    device = (ROOT / "intg-manager" / "device.py").read_text(encoding="utf-8")
    settings = (ROOT / "ui" / "src" / "components" / "SettingsPage.tsx").read_text(
        encoding="utf-8"
    )
    assert '"restartRequired": False' in source
    assert '"settingsRestored": settings_restored' in source
    assert "Settings.load(self.identifier).shutdown_on_battery" in device
    assert "self._settings" not in device
    assert "Backup imported." in settings


def test_settings_save_has_stable_feedback():
    settings = (ROOT / "ui" / "src" / "components" / "SettingsPage.tsx").read_text(
        encoding="utf-8"
    )
    assert "settings-save-button" in settings
    assert "Settings saved" in settings


def test_integration_mark_border_tracks_its_state_on_hover():
    styles = (ROOT / "ui" / "src" / "styles.css").read_text(encoding="utf-8")
    assert ".integration-mark.success{--mark-border:" in styles
    assert ".integration-mark.warning{--mark-border:" in styles
    assert ".integration-mark.danger{--mark-border:" in styles
    assert "var(--mark-inset),0 7px 16px" in styles


def test_inplace_update_clears_stale_release_data_before_the_spa_refetches():
    server = SERVER.read_text(encoding="utf-8")
    collection = (
        ROOT / "ui" / "src" / "components" / "IntegrationCollection.tsx"
    ).read_text(encoding="utf-8")
    assert "_get_version_cache(remote_id).pop(integration.driver_id, None)" in server
    assert "queryClient.refetchQueries({ queryKey: [mode, 'integrations'], type: 'active' })" in collection


def test_prerelease_images_do_not_move_the_latest_tag():
    workflow = (ROOT / ".github" / "workflows" / "build.yml").read_text(
        encoding="utf-8"
    )
    assert 'if [[ "$VERSION" == *-* ]]; then' in workflow
    assert "prerelease: ${{ needs.build.outputs.prerelease }}" in workflow
    assert "type=raw,value=latest,enable=${{ needs.build.outputs.prerelease != 'true' }}" in workflow


def test_self_update_keeps_the_stable_driver_identity_and_reconnects_the_spa():
    driver = json.loads((ROOT / "driver.json").read_text(encoding="utf-8"))
    driver_source = (ROOT / "intg-manager" / "driver.py").read_text(encoding="utf-8")
    hook = (ROOT / "git-hooks" / "pre-push").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "build.yml").read_text(
        encoding="utf-8"
    )
    server = SERVER.read_text(encoding="utf-8")
    api = (ROOT / "ui" / "src" / "lib" / "api.ts").read_text(encoding="utf-8")
    collection = (
        ROOT / "ui" / "src" / "components" / "IntegrationCollection.tsx"
    ).read_text(encoding="utf-8")

    assert driver["driver_id"] == "intg_manager_driver"
    assert f'driver_id="{driver["driver_id"]}"' in driver_source
    assert '"_dev"' in hook
    assert '"_dev"' in workflow
    assert '"selfUpdate": management == "self_managed"' in server
    assert "'/self-update/inplace'" in api
    assert "api.managerHealth() === 'OK'" in collection
    assert "manager-update-overlay" in collection


def test_manager_logs_are_part_of_the_mobile_navigation_list():
    shell = (ROOT / "ui" / "src" / "components" / "AppShell.tsx").read_text(
        encoding="utf-8"
    )
    navigation = shell[shell.index("const navigation") : shell.index("export function")]
    assert "['/logs', 'Manager logs', BookOpen]" in navigation


def test_installed_integrations_include_a_disconnected_attention_summary():
    collection = (
        ROOT / "ui" / "src" / "components" / "IntegrationCollection.tsx"
    ).read_text(encoding="utf-8")
    assert "const attentionCount" in collection
    assert "Needs attention" in collection
    assert "setFilter('disconnected')" in collection
    assert "integration-overview-note" not in collection


def test_firmware_updates_use_unfurled_and_expose_progress_to_the_spa():
    server = SERVER.read_text(encoding="utf-8")
    diagnostics = (
        ROOT / "ui" / "src" / "components" / "DiagnosticsPage.tsx"
    ).read_text(encoding="utf-8")
    api = (ROOT / "ui" / "src" / "lib" / "api.ts").read_text(encoding="utf-8")
    assert '"/api/v1/diagnostics/system-update/install"' in server
    assert '"/api/v1/diagnostics/system-update/status"' in server
    assert "await client.system.update_firmware()" in server
    assert "await _start_firmware_update_websocket(remote_id, client)" in server
    assert "await _stop_firmware_update_websocket(remote_id, client)" in server
    assert "firmwareUpdateStatus" in api
    assert "installFirmware" in api
    assert "Update firmware" in diagnostics
    assert "firmware-progress" in diagnostics
    assert "enabled: firmwareUpdateActive" in diagnostics
    assert "refetchInterval: firmwareUpdateActive ? 2_000 : false" in diagnostics
    assert "total_steps" in server
    assert "current_step" in server
    assert '"currentStepPercent"' in server
    assert "firmwareStepLabel" in diagnostics
    assert "if e.status_code == 404:" in server
    assert '"state": "DONE"' in server
    assert "firmwareRecheckActive" in diagnostics
    assert "firmware-recheck" in diagnostics


def test_remote_heartbeats_are_bounded_concurrent_and_back_off_offline_remotes():
    server = SERVER.read_text(encoding="utf-8")
    driver = (ROOT / "intg-manager" / "driver.py").read_text(encoding="utf-8")
    startup = server[
        server.index("async def _startup_fetch_localization") : server.index(
            "@app.before_request"
        )
    ]
    replacement = server[
        server.index("async def _replace_remotes_on_server_loop") : server.index(
            "async def _close_remote_clients_on_server_loop"
        )
    ]

    assert "_CONNECTIVITY_TIMEOUT = aiohttp.ClientTimeout(total=2, connect=1)" in server
    assert '"GET", "pub/version", timeout=_CONNECTIVITY_TIMEOUT' in server
    assert "next_probe_at" in server
    assert "asyncio.gather(" in server
    assert "connect_websocket" not in startup
    assert "connect_websocket" not in replacement
    assert "check_all_remote_connectivity(force=True)" in driver


def test_backups_pass_the_unfurled_coreapi_not_the_remote_wrapper():
    source = SERVER.read_text(encoding="utf-8")
    assert "backup_all_integrations(\n            client.api" in source
    assert "backup_integration(\n            client.api," in source
    assert "backup_integration(\n                    client.api," in source
