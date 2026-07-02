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
_DEFAULT_PLUGINS_LIST_PATH = os.environ.get(
    "SUDOWORK_DEFAULT_PLUGINS_LIST",
    "/app/api/sudowork_patches/default-plugins.json",
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


class LocalPackageBytes(TypedDict):
    plugin_unique_identifier: str
    content: bytes


class LocalPackageManifest(TypedDict):
    version: str
    author: str | None
    name: str
    description: dict[str, Any]
    icon: str
    icon_dark: str | None
    label: dict[str, Any]
    category: str
    created_at: str
    resource: dict[str, Any]
    plugins: dict[str, Any]
    tags: list[str]
    repo: str | None
    verified: bool
    tool: dict[str, Any] | None
    model: dict[str, Any] | None
    endpoint: dict[str, Any] | None
    agent_strategy: dict[str, Any] | None
    datasource: dict[str, Any] | None
    trigger: dict[str, Any] | None
    meta: dict[str, Any]


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

    enabled_specs = _load_enabled_plugin_specs()
    if enabled_specs is None:
        return [str(identifier) for identifier in resolved.values() if identifier]

    return [
        str(identifier)
        for spec, identifier in resolved.items()
        if spec in enabled_specs and identifier
    ]


def _load_enabled_plugin_specs() -> set[str] | None:
    """Return the active plugin specs from ``default-plugins.json`` when present.

    The generated lockfile can lag behind the curated list after an operator
    removes a plugin. Filtering lockfile rows by the active list prevents stale
    packages, such as sdist-only providers, from showing up in airgapped
    deployments.
    """
    if not os.path.isfile(_DEFAULT_PLUGINS_LIST_PATH):
        return None

    try:
        with open(_DEFAULT_PLUGINS_LIST_PATH, encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:
        logger.exception("sudowork_offline_package_list_unreadable path=%s", _DEFAULT_PLUGINS_LIST_PATH)
        return None

    plugins = doc.get("plugins")
    if not isinstance(plugins, list):
        return None
    return {str(spec) for spec in plugins if spec}


def is_enabled_default_plugin(plugin_unique_identifier: str) -> bool:
    enabled_specs = _load_enabled_plugin_specs()
    if enabled_specs is None:
        return True
    return _identifier_prefix(plugin_unique_identifier) in enabled_specs


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


def get_default_package_manifest(requested_uid: str) -> LocalPackageManifest | None:
    """Return the marketplace manifest shape from a pre-seeded local package.

    ``/plugin/marketplace/pkg`` can be called before plugin_daemon has a
    declaration row for the requested tenant. In airgapped installs, falling
    back to marketplace download would hang, so the local package manifest is
    used as read-only metadata when the package cache contains the plugin.
    """
    resolved_uid = resolve_default_package_identifier(requested_uid)
    if not resolved_uid:
        return None

    package_path = _resolve_package_path(resolved_uid)
    if not package_path:
        return None

    manifest = _read_manifest(package_path)
    if not manifest or not _is_model_package_manifest(manifest):
        return None

    return {
        "version": str(manifest.get("version", "")),
        "author": manifest.get("author"),
        "name": str(manifest.get("name", "")),
        "description": manifest.get("description") or {},
        "icon": str(manifest.get("icon", "")),
        "icon_dark": manifest.get("icon_dark"),
        "label": manifest.get("label") or {},
        "category": "model",
        "created_at": str(manifest.get("created_at") or "1970-01-01T00:00:00Z"),
        "resource": manifest.get("resource") or {},
        "plugins": manifest.get("plugins") or {},
        "tags": manifest.get("tags") or [],
        "repo": manifest.get("repo"),
        "verified": bool(manifest.get("verified", False)),
        "tool": manifest.get("tool"),
        "model": manifest.get("model"),
        "endpoint": manifest.get("endpoint"),
        "agent_strategy": manifest.get("agent_strategy"),
        "datasource": manifest.get("datasource"),
        "trigger": manifest.get("trigger"),
        "meta": manifest.get("meta") or {},
    }


def read_default_package(requested_uid: str) -> LocalPackageBytes | None:
    resolved_uid = resolve_default_package_identifier(requested_uid)
    if not resolved_uid:
        return None

    package_path = _resolve_package_path(resolved_uid)
    if not package_path:
        return None

    try:
        return {
            "plugin_unique_identifier": resolved_uid,
            "content": Path(package_path).read_bytes(),
        }
    except Exception:
        logger.warning("sudowork_offline_package_unreadable uid=%s path=%s", requested_uid, package_path, exc_info=True)
        return None


def get_local_package_icon_url(plugin_unique_identifier: str, filename: str) -> str:
    query = urlencode({"plugin_unique_identifier": plugin_unique_identifier, "filename": filename})
    return f"/console/api/workspaces/current/plugin/marketplace/local-model-provider-icon?{query}"


def _safe_package_path(filename: str) -> str | None:
    normalized = filename.removeprefix("./").lstrip("/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        return None
    return normalized


def _safe_asset_candidates(filename: str) -> list[str]:
    normalized = _safe_package_path(filename)
    if normalized is None:
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


def _model_provider_paths(manifest: dict[str, Any]) -> list[str]:
    plugins = manifest.get("plugins")
    if not isinstance(plugins, dict):
        return []

    models = plugins.get("models")
    if isinstance(models, str):
        return [models]
    if not isinstance(models, list):
        return []
    return [model for model in models if isinstance(model, str)]


def _provider_matches_request(requested_provider: str, plugin_id: str, provider_name: str) -> bool:
    requested = requested_provider.strip("/")
    if not requested or not provider_name:
        return False

    if requested in {provider_name, plugin_id, f"{plugin_id}/{provider_name}"}:
        return True

    parts = requested.split("/")
    if len(parts) == 1:
        plugin_name = plugin_id.split("/", 1)[1] if "/" in plugin_id else plugin_id
        return requested in {plugin_name, provider_name}
    if len(parts) == 2:
        return requested == plugin_id
    if len(parts) == 3:
        organization, plugin_name, requested_provider_name = parts
        return plugin_id == f"{organization}/{plugin_name}" and provider_name == requested_provider_name
    return False


def _localized_icon_filename(icon: Any, lang: str) -> str | None:
    if isinstance(icon, str):
        return icon
    if not isinstance(icon, dict):
        return None

    normalized_lang = lang.lower().replace("-", "_")
    preferred_keys = ("zh_Hans", "zh_hans") if normalized_lang == "zh_hans" else ("en_US", "en_us")
    for key in preferred_keys:
        value = icon.get(key)
        if isinstance(value, str) and value:
            return value

    for value in icon.values():
        if isinstance(value, str) and value:
            return value
    return None


def extract_default_model_provider_icon(provider: str, icon_type: str, lang: str) -> LocalPackageAsset | None:
    """Read a model provider icon from the pre-seeded local plugin packages.

    Installed model providers normally serve icons through plugin_daemon's
    asset cache. In airgapped upgrades the declaration can exist while that
    asset cache is missing, so the package zip remains the only reliable local
    source for ``provider/*.yaml`` metadata and ``_assets`` icon bytes.
    """
    icon_key = icon_type.lower()
    for package in list_default_model_packages():
        provider_paths = _model_provider_paths(package["manifest"])
        if not provider_paths:
            continue

        try:
            with zipfile.ZipFile(package["package_path"]) as package_zip:
                names = set(package_zip.namelist())
                for provider_path in provider_paths:
                    safe_provider_path = _safe_package_path(provider_path)
                    if safe_provider_path is None or safe_provider_path not in names:
                        continue

                    raw_provider = package_zip.read(safe_provider_path)
                    provider_doc = yaml.safe_load(raw_provider.decode("utf-8"))
                    if not isinstance(provider_doc, dict):
                        continue

                    provider_name = str(provider_doc.get("provider") or "")
                    if not _provider_matches_request(provider, package["plugin_id"], provider_name):
                        continue

                    icon_filename = _localized_icon_filename(provider_doc.get(icon_key), lang)
                    if not icon_filename:
                        return None
                    return extract_default_package_asset(package["plugin_unique_identifier"], icon_filename)
        except Exception:
            logger.warning(
                "sudowork_offline_model_provider_icon_unreadable provider=%s package=%s",
                provider,
                package["package_path"],
                exc_info=True,
            )
    return None
