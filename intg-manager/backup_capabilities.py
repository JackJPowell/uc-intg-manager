"""Registry-defined eligibility for integration configuration backups."""

from typing import Any

from packaging.version import InvalidVersion, Version


def backup_support_status(
    current_version: str, registry_item: dict[str, Any]
) -> tuple[bool, str]:
    """Return whether a specific installed version can create a backup.

    ``supports_backup`` declares that an integration implements the feature;
    ``backup_min_version`` records the first release where it is available.
    Version parsing failures remain permissive for compatibility with legacy
    integration version strings.
    """
    if not registry_item.get("supports_backup", False):
        return False, "Integration doesn't support backup"

    min_version = registry_item.get("backup_min_version")
    if not min_version:
        return True, ""

    try:
        if Version(current_version) < Version(min_version):
            return (
                False,
                f"Requires version {min_version} or higher (current: {current_version})",
            )
    except (InvalidVersion, TypeError):
        pass

    return True, ""
