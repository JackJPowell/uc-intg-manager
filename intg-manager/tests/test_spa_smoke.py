"""Dependency-free smoke checks for the React SPA and JSON API boundary."""

import ast
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
    assert '"backup": can_mutate and installed and integration.backup_available' in serializer


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
    assert "await ws.run_on_server_loop(ws.check_all_remote_connectivity())" in driver


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
    assert "uv run --group dev pytest intg-manager/tests -q" in workflow
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
    assert re.compile(pattern).match("[2026-07-27T21:27:49Z DEBUG service: ready")


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
        source.index('def _normalize_integration_log_payload') : source.index(
            '@app.route("/api/v1/integration-logs/export")'
        )
    ]
    assert 'payload.splitlines()' in log_routes
    assert "as_text=True" in log_routes


def test_manager_logs_are_part_of_the_mobile_navigation_list():
    shell = (ROOT / "ui" / "src" / "components" / "AppShell.tsx").read_text(
        encoding="utf-8"
    )
    navigation = shell[shell.index("const navigation") : shell.index("export function")]
    assert "['/logs', 'Manager logs', BookOpen]" in navigation


def test_installed_integrations_include_a_disconnected_attention_summary():
    collection = (ROOT / "ui" / "src" / "components" / "IntegrationCollection.tsx").read_text(
        encoding="utf-8"
    )
    assert "const attentionCount" in collection
    assert "Needs attention" in collection
    assert "setFilter('disconnected')" in collection
    assert "integration-overview-note" not in collection


def test_backups_pass_the_unfurled_coreapi_not_the_remote_wrapper():
    source = SERVER.read_text(encoding="utf-8")
    assert "backup_all_integrations(\n            client.api" in source
    assert "backup_integration(\n            client.api," in source
    assert "backup_integration(\n                    client.api," in source
