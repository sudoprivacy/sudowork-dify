"""Read Sudowork's pre-seeded ``.difypkg`` packages without plugin_daemon state.

Airgapped deployments mount the package cache and lockfile into the API
container. Newer tenants should upload those packages to plugin_daemon so
``plugin_declarations`` is populated, but customer upgrades may have package
files on disk while the daemon declaration table is empty. This module treats
the lockfile plus package bytes as the source of truth for read-only metadata
and for mapping marketplace identifiers back to local package identifiers.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, NotRequired, TypedDict
from urllib.parse import urlencode

import yaml

logger = logging.getLogger(__name__)

_DEFAULT_PLUGINS_LOCK_PATH = os.environ.get(
    "SUDOWORK_DEFAULT_PLUGINS_LOCK",
    "/app/api/sudowork_patches/default-plugins.lock.json",
)
_DEFAULT_PLUGINS_PKG_DIR = os.environ.get(
    "SUDOWORK_DEFAULT_PLUGINS_PKG_DIR",
    "/app/api/sudowork_patches/plugin_packages/langgenius",
)


class LocalPluginPackage(TypedDict):
    plugin_unique_identifier: str
    plugin_id: str
    package_path: str
    manifest: dict[str, Any]


class LocalPackageAsset(TypedDict):
    content: bytes
    mimetype: str


class _ResolvedPackage(TypedDict):
    plugin_unique_identifier: str
    package_path: str
    manifest: NotRequired[dict[str, Any]]


def _identifier_prefix(plugin_unique_identifier: str) -> str:
    return plugin_unique_identifier.split("@", 1)[0]


def _plugin_id_from_identifier(plugin_unique_identifier: str) -> str:
    return _identifier_prefix(plugin_unique_identifier).split(":", 1)[0]


def _package_filename(plugin_unique_identifier: str) -> str | None:
    if "/" not in plugin_unique_identifier:
        return None
    return plugin_unique_identifier.split("/", 1)[1]


def _load_lock_identifiers() -> list[str]:
    if not os.path.isfile(_DEFAULT_PLUGINS_LOCK_PATH):
        return []

    try:
        with open(_DEFAULT_PLUGINS_LOCK_PATH, encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:
        logger.exception("sudowork_offline_package_lock_unreadable path=%s", _DEFAULT_PLUGINS_LOCK_PATH)
        return []

    resolved = doc.get("resolved")
    if not isinstance(resolved, dict):
        return []
    return [str(identifier) for identifier in resolved.values() if identifier]


def _resolve_package_path(plugin_unique_identifier: str) -> str | None:
    filename = _package_filename(plugin_unique_identifier)
    if not filename:
        return None

    exact_path = Path(_DEFAULT_PLUGINS_PKG_DIR) / filename
    if exact_path.is_file():
        return str(exact_path)

    prefix = filename.split("@", 1)[0]
    candidates = sorted(Path(_DEFAULT_PLUGINS_PKG_DIR).glob(f"{prefix}@*"))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _identifier_from_package_path(plugin_unique_identifier: str, package_path: str) -> str:
    org = plugin_unique_identifier.split("/", 1)[0]
    return f"{org}/{Path(package_path).name}"


def _read_manifest(package_path: str) -> dict[str, Any] | None:
    try:
        with zipfile.ZipFile(package_path) as package:
            raw = package.read("manifest.yaml")
    except Exception:
        logger.warning("sudowork_offline_package_manifest_unreadable path=%s", package_path, exc_info=True)
        return None

    try:
        manifest = yaml.safe_load(raw.decode("utf-8"))
    except Exception:
        logger.warning("sudowork_offline_package_manifest_invalid path=%s", package_path, exc_info=True)
        return None

    return manifest if isinstance(manifest, dict) else None


def _is_model_package_manifest(manifest: dict[str, Any]) -> bool:
    plugins = manifest.get("plugins")
    return isinstance(plugins, dict) and bool(plugins.get("models"))


def _resolve_default_package(plugin_unique_identifier: str) -> _ResolvedPackage | None:
    package_path = _resolve_package_path(plugin_unique_identifier)
    if package_path:
        return {
            "plugin_unique_identifier": _identifier_from_package_path(plugin_unique_identifier, package_path),
            "package_path": package_path,
        }
    return None


def list_default_model_packages() -> list[LocalPluginPackage]:
    packages: list[LocalPluginPackage] = []
    for plugin_unique_identifier in _load_lock_identifiers():
        resolved = _resolve_default_package(plugin_unique_identifier)
        if not resolved:
            continue

        manifest = _read_manifest(resolved["package_path"])
        if not manifest or not _is_model_package_manifest(manifest):
            continue

        packages.append(
            {
                "plugin_unique_identifier": resolved["plugin_unique_identifier"],
                "plugin_id": _plugin_id_from_identifier(resolved["plugin_unique_identifier"]),
                "package_path": resolved["package_path"],
                "manifest": manifest,
            }
        )
    return packages


def resolve_default_package_identifier(requested_uid: str) -> str | None:
    requested_prefix = _identifier_prefix(requested_uid)
    for local_uid in _load_lock_identifiers():
        if _identifier_prefix(local_uid) != requested_prefix:
            continue

        resolved = _resolve_default_package(local_uid)
        if resolved:
            return resolved["plugin_unique_identifier"]
    return None


def get_local_package_icon_url(plugin_unique_identifier: str, filename: str) -> str:
    query = urlencode({"plugin_unique_identifier": plugin_unique_identifier, "filename": filename})
    return f"/console/api/workspaces/current/plugin/marketplace/local-model-provider-icon?{query}"


def _safe_asset_candidates(filename: str) -> list[str]:
    normalized = filename.removeprefix("./").lstrip("/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        return []

    candidates = [normalized]
    if not normalized.startswith("_assets/"):
        candidates.append(f"_assets/{normalized}")
    return candidates


def extract_default_package_asset(plugin_unique_identifier: str, filename: str) -> LocalPackageAsset | None:
    resolved_uid = resolve_default_package_identifier(plugin_unique_identifier)
    if not resolved_uid:
        return None

    package_path = _resolve_package_path(resolved_uid)
    if not package_path:
        return None

    candidates = _safe_asset_candidates(filename)
    if not candidates:
        return None

    try:
        with zipfile.ZipFile(package_path) as package:
            names = set(package.namelist())
            for candidate in candidates:
                if candidate in names:
                    mimetype, _ = mimetypes.guess_type(candidate)
                    return {
                        "content": package.read(candidate),
                        "mimetype": mimetype or "application/octet-stream",
                    }
    except Exception:
        logger.warning(
            "sudowork_offline_package_asset_unreadable uid=%s filename=%s",
            plugin_unique_identifier,
            filename,
            exc_info=True,
        )
    return None
