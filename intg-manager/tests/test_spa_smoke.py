"""Dependency-free smoke checks for the React SPA and JSON API boundary."""

import os
from pathlib import Path


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
