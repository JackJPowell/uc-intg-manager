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


def test_integration_lifecycle_uses_coreapi_operations():
    """Installation and configuration routes must not rely on legacy HTTP helpers."""
    source = SERVER.read_text(encoding="utf-8")
    lifecycle = source[source.index("async def update_integration_inplace") : source.index("async def check_versions")]
    for operation in (
        "post_integration_install",
        "delete_integration",
        "delete_driver",
    ):
        assert operation in lifecycle


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
    assert "intg-manager/static/img/intg-manager.png artifacts/" in workflow
    assert "intg-${INTG_NAME}/templates:templates" not in workflow


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
