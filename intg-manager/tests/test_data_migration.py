"""Tests for data_migration – v1.0 → v2.0 manager.json migration."""

import json
import os
import sys


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import data_migration  # noqa: E402


# ---------------------------------------------------------------------------
# migrate()
# ---------------------------------------------------------------------------


def test_migrate_no_file_returns_false(tmp_path, monkeypatch):
    monkeypatch.setattr(
        data_migration, "MANAGER_DATA_FILE", str(tmp_path / "manager.json")
    )
    assert data_migration.migrate() is False


def test_migrate_already_v2_returns_false(tmp_path, monkeypatch):
    mgr = tmp_path / "manager.json"
    mgr.write_text(json.dumps({"version": "2.0", "remotes": {}, "shared": {}}))
    monkeypatch.setattr(data_migration, "MANAGER_DATA_FILE", str(mgr))
    assert data_migration.migrate() is False


def test_migrate_v1_returns_true(tmp_path, monkeypatch):
    mgr = tmp_path / "manager.json"
    mgr.write_text(json.dumps({"settings": {}}))
    monkeypatch.setattr(data_migration, "MANAGER_DATA_FILE", str(mgr))
    assert data_migration.migrate(target_remote_id="r1") is True


def test_migrate_v1_produces_v2_structure(tmp_path, monkeypatch):
    mgr = tmp_path / "manager.json"
    v1 = {
        "settings": {"auto_update": True, "backup_configs": False},
        "integrations": {"uc-driver-hue": {"version": "1.0.0"}},
    }
    mgr.write_text(json.dumps(v1))
    monkeypatch.setattr(data_migration, "MANAGER_DATA_FILE", str(mgr))

    data_migration.migrate(target_remote_id="remote-1")

    result = json.loads(mgr.read_text())
    assert result["version"] == "2.0"
    assert "remote-1" in result["remotes"]
    assert result["remotes"]["remote-1"]["settings"] == v1["settings"]
    assert result["remotes"]["remote-1"]["integrations"] == v1["integrations"]


def test_migrate_v1_notification_settings_to_shared(tmp_path, monkeypatch):
    mgr = tmp_path / "manager.json"
    v1 = {
        "notification_settings": {
            "home_assistant": {
                "enabled": True,
                "url": "",
                "token": "",
                "service": "notify",
            },
            "_last_registry_count": 5,
            "_known_integration_ids": ["a", "b"],
        }
    }
    mgr.write_text(json.dumps(v1))
    monkeypatch.setattr(data_migration, "MANAGER_DATA_FILE", str(mgr))

    data_migration.migrate(target_remote_id="r1")

    result = json.loads(mgr.read_text())
    ns = result["shared"]["notification_settings"]
    assert ns["home_assistant"]["enabled"] is True
    # Registry tracking must be stripped from notification_settings…
    assert "_last_registry_count" not in ns
    assert "_known_integration_ids" not in ns
    # …and moved to shared.registry_tracking
    assert result["shared"]["registry_tracking"]["_last_registry_count"] == 5
    assert result["shared"]["registry_tracking"]["_known_integration_ids"] == ["a", "b"]


def test_migrate_v1_repo_cache_to_shared(tmp_path, monkeypatch):
    mgr = tmp_path / "manager.json"
    v1 = {"repo_cache": {"repos": {"owner/repo": {"stars": 10}}}}
    mgr.write_text(json.dumps(v1))
    monkeypatch.setattr(data_migration, "MANAGER_DATA_FILE", str(mgr))

    data_migration.migrate(target_remote_id="r1")

    result = json.loads(mgr.read_text())
    assert result["shared"]["repo_cache"] == v1["repo_cache"]


def test_migrate_v1_read_message_ids_to_shared(tmp_path, monkeypatch):
    mgr = tmp_path / "manager.json"
    v1 = {"read_message_ids": ["msg-1", "msg-2"]}
    mgr.write_text(json.dumps(v1))
    monkeypatch.setattr(data_migration, "MANAGER_DATA_FILE", str(mgr))

    data_migration.migrate(target_remote_id="r1")

    result = json.loads(mgr.read_text())
    assert result["shared"]["read_message_ids"] == ["msg-1", "msg-2"]


def test_migrate_creates_backup_file(tmp_path, monkeypatch):
    mgr = tmp_path / "manager.json"
    mgr.write_text(json.dumps({"settings": {}}))
    monkeypatch.setattr(data_migration, "MANAGER_DATA_FILE", str(mgr))

    data_migration.migrate(target_remote_id="r1")

    backup = tmp_path / "manager.json.v1.backup"
    assert backup.exists()
    # Backup must contain original v1 content
    assert json.loads(backup.read_text()) == {"settings": {}}


def test_migrate_initialises_ui_preferences(tmp_path, monkeypatch):
    mgr = tmp_path / "manager.json"
    mgr.write_text(json.dumps({"settings": {}}))
    monkeypatch.setattr(data_migration, "MANAGER_DATA_FILE", str(mgr))

    data_migration.migrate(target_remote_id="r1")

    result = json.loads(mgr.read_text())
    prefs = result["shared"]["ui_preferences"]
    assert "sort_by" in prefs
    assert "sort_reverse" in prefs


def test_migrate_initialises_registry_tracking_when_absent(tmp_path, monkeypatch):
    """registry_tracking is initialised with defaults when not in v1 data."""
    mgr = tmp_path / "manager.json"
    mgr.write_text(json.dumps({"settings": {}}))
    monkeypatch.setattr(data_migration, "MANAGER_DATA_FILE", str(mgr))

    data_migration.migrate(target_remote_id="r1")

    result = json.loads(mgr.read_text())
    rt = result["shared"]["registry_tracking"]
    assert rt["_last_registry_count"] == 0
    assert rt["_known_integration_ids"] == []


# ---------------------------------------------------------------------------
# _get_remote_id_from_config()
# ---------------------------------------------------------------------------


def test_get_remote_id_no_config_file(tmp_path, monkeypatch):
    monkeypatch.setattr(
        data_migration, "MANAGER_DATA_FILE", str(tmp_path / "manager.json")
    )
    assert data_migration._get_remote_id_from_config() is None


def test_get_remote_id_reads_first_identifier(tmp_path, monkeypatch):
    mgr = tmp_path / "manager.json"
    config = tmp_path / "config.json"
    config.write_text(json.dumps([{"identifier": "my-remote-abc"}]))
    monkeypatch.setattr(data_migration, "MANAGER_DATA_FILE", str(mgr))

    assert data_migration._get_remote_id_from_config() == "my-remote-abc"


def test_get_remote_id_multiple_remotes_returns_first(tmp_path, monkeypatch):
    mgr = tmp_path / "manager.json"
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps([{"identifier": "first-remote"}, {"identifier": "second-remote"}])
    )
    monkeypatch.setattr(data_migration, "MANAGER_DATA_FILE", str(mgr))

    assert data_migration._get_remote_id_from_config() == "first-remote"


def test_get_remote_id_empty_list(tmp_path, monkeypatch):
    mgr = tmp_path / "manager.json"
    config = tmp_path / "config.json"
    config.write_text(json.dumps([]))
    monkeypatch.setattr(data_migration, "MANAGER_DATA_FILE", str(mgr))

    assert data_migration._get_remote_id_from_config() is None


def test_get_remote_id_missing_identifier_key(tmp_path, monkeypatch):
    mgr = tmp_path / "manager.json"
    config = tmp_path / "config.json"
    config.write_text(json.dumps([{"name": "no-identifier-here"}]))
    monkeypatch.setattr(data_migration, "MANAGER_DATA_FILE", str(mgr))

    assert data_migration._get_remote_id_from_config() is None


def test_get_remote_id_invalid_json(tmp_path, monkeypatch):
    mgr = tmp_path / "manager.json"
    config = tmp_path / "config.json"
    config.write_text("not valid json{{")
    monkeypatch.setattr(data_migration, "MANAGER_DATA_FILE", str(mgr))

    assert data_migration._get_remote_id_from_config() is None
