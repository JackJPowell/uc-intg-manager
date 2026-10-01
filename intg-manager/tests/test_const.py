"""Tests for const.py – Settings, UIPreferences, and RemoteConfig."""

import json
import os
import sys
from dataclasses import fields


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import const  # noqa: E402
from const import RemoteConfig, Settings, UIPreferences, is_external_mode  # noqa: E402


# ---------------------------------------------------------------------------
# Settings defaults
# ---------------------------------------------------------------------------


def test_settings_default_version():
    assert Settings().settings_version == 2


def test_settings_default_booleans():
    s = Settings()
    assert s.shutdown_on_battery is False
    assert s.auto_update is False
    assert s.backup_configs is False
    assert s.show_beta_releases is False


def test_settings_default_backup_time():
    assert Settings().backup_time == "02:00"


def test_settings_to_dict_contains_all_fields():
    s = Settings()
    d = s.to_dict()
    field_names = {f.name for f in fields(Settings)}
    assert set(d.keys()) == field_names


def test_settings_to_dict_values():
    s = Settings()
    s.auto_update = True
    d = s.to_dict()
    assert d["auto_update"] is True
    assert d["settings_version"] == 2


def test_settings_migration_removes_obsolete_entity_registration_preference(
    tmp_path, monkeypatch
):
    manager_data = tmp_path / "manager.json"
    manager_data.write_text(
        json.dumps(
            {
                "version": "2.0",
                "remotes": {
                    "remote-1": {
                        "settings": {
                            "settings_version": 1,
                            "auto_register_entities": False,
                        }
                    }
                },
                "shared": {},
            }
        )
    )
    monkeypatch.setattr(const, "MANAGER_DATA_FILE", str(manager_data))

    settings = Settings.load(remote_id="remote-1")

    assert settings.settings_version == 2
    saved = json.loads(manager_data.read_text())
    assert "auto_register_entities" not in saved["remotes"]["remote-1"]["settings"]


def test_settings_custom_values():
    s = Settings(
        auto_update=True,
        backup_configs=True,
        backup_time="03:30",
        show_beta_releases=True,
    )
    assert s.auto_update is True
    assert s.backup_time == "03:30"
    assert s.show_beta_releases is True


# ---------------------------------------------------------------------------
# UIPreferences defaults
# ---------------------------------------------------------------------------


def test_ui_preferences_default_sort_by():
    assert UIPreferences().sort_by == "stars"


def test_ui_preferences_default_sort_reverse():
    assert UIPreferences().sort_reverse is False


def test_ui_preferences_to_dict_contains_all_fields():
    prefs = UIPreferences()
    d = prefs.to_dict()
    field_names = {f.name for f in fields(UIPreferences)}
    assert set(d.keys()) == field_names


def test_ui_preferences_custom_values():
    prefs = UIPreferences(sort_by="name", sort_reverse=True)
    assert prefs.sort_by == "name"
    assert prefs.sort_reverse is True
    assert prefs.to_dict()["sort_by"] == "name"


# ---------------------------------------------------------------------------
# RemoteConfig
# ---------------------------------------------------------------------------


def test_remote_config_required_fields():
    rc = RemoteConfig(identifier="abc", name="My Remote", address="192.168.1.100")
    assert rc.identifier == "abc"
    assert rc.name == "My Remote"
    assert rc.address == "192.168.1.100"


def test_remote_config_optional_fields_default_empty():
    rc = RemoteConfig(identifier="x", name="X", address="1.2.3.4")
    assert rc.pin == ""
    assert rc.api_key == ""


def test_remote_config_repr_masks_pin():
    rc = RemoteConfig(identifier="abc", name="Remote", address="1.2.3.4", pin="9876")
    assert "9876" not in repr(rc)
    assert "****" in repr(rc)


def test_remote_config_repr_masks_api_key():
    rc = RemoteConfig(
        identifier="abc", name="Remote", address="1.2.3.4", api_key="supersecret"
    )
    assert "supersecret" not in repr(rc)
    assert "****" in repr(rc)


def test_remote_config_repr_exposes_non_sensitive_fields():
    rc = RemoteConfig(identifier="my-id", name="Living Room", address="10.0.0.5")
    r = repr(rc)
    assert "my-id" in r
    assert "Living Room" in r
    assert "10.0.0.5" in r


# ---------------------------------------------------------------------------
# is_external_mode
# ---------------------------------------------------------------------------


def _clear_env(monkeypatch):
    """Strip env vars that influence is_external_mode()."""
    monkeypatch.delenv("UC_INTG_MANAGER_EXTERNAL", raising=False)
    monkeypatch.delenv("UC_CONFIG_HOME", raising=False)


def test_is_external_mode_override_truthy_values(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("UC_CONFIG_HOME", "/opt/uc-remote/data")  # would otherwise be False
    for v in ("1", "true", "TRUE", "True", "yes", "Yes", "on", "  on  "):
        monkeypatch.setenv("UC_INTG_MANAGER_EXTERNAL", v)
        assert is_external_mode() is True, f"expected True for {v!r}"


def test_is_external_mode_override_falsy_values(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("UC_CONFIG_HOME", "/config")  # would otherwise be True
    for v in ("0", "false", "FALSE", "no", "off", "anything-else", ""):
        monkeypatch.setenv("UC_INTG_MANAGER_EXTERNAL", v)
        assert is_external_mode() is False, f"expected False for {v!r}"


def test_is_external_mode_unset_config_home_is_external(monkeypatch):
    _clear_env(monkeypatch)
    assert is_external_mode() is True


def test_is_external_mode_config_home_starts_with_config_is_external(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("UC_CONFIG_HOME", "/config")
    assert is_external_mode() is True
    monkeypatch.setenv("UC_CONFIG_HOME", "/config/sub/dir")
    assert is_external_mode() is True


def test_is_external_mode_other_config_home_is_on_remote(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("UC_CONFIG_HOME", "/opt/uc-remote/data")
    assert is_external_mode() is False
    monkeypatch.setenv("UC_CONFIG_HOME", "/var/lib/intg")
    assert is_external_mode() is False


def test_is_external_mode_override_wins_over_config_home(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("UC_CONFIG_HOME", "/config")
    monkeypatch.setenv("UC_INTG_MANAGER_EXTERNAL", "0")
    assert is_external_mode() is False

    monkeypatch.setenv("UC_CONFIG_HOME", "/opt/uc-remote/data")
    monkeypatch.setenv("UC_INTG_MANAGER_EXTERNAL", "1")
    assert is_external_mode() is True
