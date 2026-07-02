"""Expose locally available model provider packages as marketplace-compatible data.

Sudowork airgapped deployments seed model-provider ``.difypkg`` files into
plugin_daemon and upload their declarations during tenant provisioning. The
normal Dify Studio provider list still searches marketplace.dify.ai from the
browser, so an offline admin PC can see an empty install-provider grid even
though the packages are already available locally.

This service prefers plugin_daemon's declaration table, but customer upgrade
paths may have the package lockfile and package bytes mounted while
``plugin_declarations`` is empty. In that case we parse the local package
manifests directly and return the same small marketplace shape the Studio
model-provider UI needs.
"""

from __future__ import annotations

import json
import logging
from typing import Any, NotRequired, TypedDict

from sqlalchemy import text

from core.plugin.plugin_service import PluginService
from services.sudowork.install_redirect import _get_plugin_db_engine
from services.sudowork.offline_plugin_package_service import (
    get_local_package_icon_url,
    is_enabled_default_plugin,
    list_default_model_packages,
)

logger = logging.getLogger(__name__)

_DEFAULT_MODEL_COLLECTION_ID = "__model-settings-pinned-models"


LocalMarketplacePlugin = TypedDict(
    "LocalMarketplacePlugin",
    {
        "type": str,
        "org": str,
        "author": str | None,
        "name": str,
        "plugin_id": str,
        "version": str,
        "latest_version": str,
        "latest_package_identifier": str,
        "icon": str,
        "icon_dark": NotRequired[str],
        "verified": bool,
        "label": dict[str, str],
        "brief": dict[str, str],
        "description": dict[str, str],
        "introduction": str,
        "repository": str,
        "category": str,
        "install_count": int,
        "endpoint": dict[str, list[Any]],
        "tags": list[dict[str, str]],
        "badges": list[str] | None,
        "verification": dict[str, str],
        "from": str,
    },
)


class LocalMarketplaceResult(TypedDict):
    plugins: list[LocalMarketplacePlugin]
    has_local_source: bool


def _coerce_i18n(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        return {str(key): str(text) for key, text in value.items() if text is not None}
    if isinstance(value, str) and value:
        return {"en_US": value}
    return {}


def _org_from_plugin_id(plugin_id: str) -> str:
    return plugin_id.split("/", 1)[0] if "/" in plugin_id else ""


def _normalize_tag(tag: Any) -> dict[str, str] | None:
    if isinstance(tag, dict):
        name = tag.get("name")
    else:
        name = tag
    if not name:
        return None
    return {"name": str(name)}


def _declaration_to_marketplace_plugin(
    *,
    tenant_id: str,
    plugin_unique_identifier: str,
    plugin_id: str,
    declaration: dict[str, Any],
) -> LocalMarketplacePlugin | None:
    if declaration.get("category") != "model" and not declaration.get("model"):
        return None

    version = str(declaration.get("version") or plugin_unique_identifier.split(":", 1)[-1].split("@", 1)[0])
    name = str(declaration.get("name") or plugin_id.rsplit("/", 1)[-1])
    tags = [_tag for tag in declaration.get("tags") or [] if (_tag := _normalize_tag(tag))]
    icon = str(declaration.get("icon") or "")
    icon_dark = str(declaration.get("icon_dark") or "")
    plugin: LocalMarketplacePlugin = {
        "type": "plugin",
        "org": _org_from_plugin_id(plugin_id),
        "author": declaration.get("author"),
        "name": name,
        "plugin_id": plugin_id,
        "version": version,
        "latest_version": version,
        "latest_package_identifier": plugin_unique_identifier,
        "icon": PluginService.get_plugin_icon_url(tenant_id, icon) if icon else "",
        "verified": bool(declaration.get("verified", False)),
        "label": _coerce_i18n(declaration.get("label")),
        "brief": _coerce_i18n(declaration.get("description")),
        "description": _coerce_i18n(declaration.get("description")),
        "introduction": "",
        "repository": str(declaration.get("repo") or ""),
        "category": "model",
        "install_count": 0,
        "endpoint": {"settings": []},
        "tags": tags,
        "badges": None,
        "verification": {"authorized_category": "community"},
        "from": "marketplace",
    }
    if icon_dark:
        plugin["icon_dark"] = PluginService.get_plugin_icon_url(tenant_id, icon_dark)
    return plugin


def _manifest_to_marketplace_plugin(
    *,
    plugin_unique_identifier: str,
    plugin_id: str,
    manifest: dict[str, Any],
) -> LocalMarketplacePlugin | None:
    version = str(manifest.get("version") or plugin_unique_identifier.split(":", 1)[-1].split("@", 1)[0])
    name = str(manifest.get("name") or plugin_id.rsplit("/", 1)[-1])
    icon = str(manifest.get("icon") or "")
    icon_dark = str(manifest.get("icon_dark") or "")
    tags = [_tag for tag in manifest.get("tags") or [] if (_tag := _normalize_tag(tag))]
    plugin: LocalMarketplacePlugin = {
        "type": "plugin",
        "org": _org_from_plugin_id(plugin_id),
        "author": manifest.get("author"),
        "name": name,
        "plugin_id": plugin_id,
        "version": version,
        "latest_version": version,
        "latest_package_identifier": plugin_unique_identifier,
        "icon": get_local_package_icon_url(plugin_unique_identifier, icon) if icon else "",
        "verified": bool(manifest.get("verified", False)),
        "label": _coerce_i18n(manifest.get("label")),
        "brief": _coerce_i18n(manifest.get("description")),
        "description": _coerce_i18n(manifest.get("description")),
        "introduction": "",
        "repository": str(manifest.get("repo") or ""),
        "category": "model",
        "install_count": 0,
        "endpoint": {"settings": []},
        "tags": tags,
        "badges": None,
        "verification": {"authorized_category": "community"},
        "from": "marketplace",
    }
    if icon_dark:
        plugin["icon_dark"] = get_local_package_icon_url(plugin_unique_identifier, icon_dark)
    return plugin


def _matches_query(plugin: LocalMarketplacePlugin, query: str) -> bool:
    query = query.strip().lower()
    if not query:
        return True

    haystack = [
        plugin["plugin_id"],
        plugin["name"],
        plugin["org"],
        *(plugin["label"].values()),
        *(plugin["description"].values()),
    ]
    return any(query in value.lower() for value in haystack if value)


def _sort_plugins(plugins: list[LocalMarketplacePlugin]) -> list[LocalMarketplacePlugin]:
    plugins.sort(key=lambda plugin: plugin["label"].get("en_US") or plugin["name"])
    return plugins


class OfflineMarketplaceService:
    @staticmethod
    def list_model_plugins_result(
        tenant_id: str,
        *,
        query: str = "",
        exclude: list[str] | None = None,
    ) -> LocalMarketplaceResult:
        """Return local model plugins and whether an offline source exists."""

        excluded_plugin_ids = set(exclude or [])
        plugins: list[LocalMarketplacePlugin] = []
        try:
            engine = _get_plugin_db_engine()
            with engine.connect() as conn:
                rows = conn.execute(
                    text(
                        "SELECT plugin_unique_identifier, plugin_id, declaration "
                        "FROM plugin_declarations ORDER BY created_at DESC"
                    )
                ).mappings()

                plugins: list[LocalMarketplacePlugin] = []
                for row in rows:
                    plugin_unique_identifier = row["plugin_unique_identifier"]
                    plugin_id = row["plugin_id"]
                    declaration_raw = row["declaration"]
                    if not plugin_unique_identifier or not plugin_id or not declaration_raw:
                        continue
                    if not is_enabled_default_plugin(str(plugin_unique_identifier)):
                        continue
                    if plugin_id in excluded_plugin_ids:
                        continue

                    try:
                        declaration = json.loads(declaration_raw)
                    except json.JSONDecodeError:
                        logger.warning("sudowork_offline_marketplace_bad_declaration plugin_id=%s", plugin_id)
                        continue

                    plugin = _declaration_to_marketplace_plugin(
                        tenant_id=tenant_id,
                        plugin_unique_identifier=str(plugin_unique_identifier),
                        plugin_id=str(plugin_id),
                        declaration=declaration,
                    )
                    if plugin and _matches_query(plugin, query):
                        plugins.append(plugin)
        except Exception:
            logger.exception("sudowork_offline_marketplace_list_failed")
            plugins = []

        if plugins:
            return {"plugins": _sort_plugins(plugins), "has_local_source": True}

        default_packages = list_default_model_packages()
        package_plugins: list[LocalMarketplacePlugin] = []
        for package in default_packages:
            plugin_id = package["plugin_id"]
            if plugin_id in excluded_plugin_ids:
                continue

            plugin = _manifest_to_marketplace_plugin(
                plugin_unique_identifier=package["plugin_unique_identifier"],
                plugin_id=plugin_id,
                manifest=package["manifest"],
            )
            if plugin and _matches_query(plugin, query):
                package_plugins.append(plugin)

        if package_plugins:
            return {"plugins": _sort_plugins(package_plugins), "has_local_source": True}

        return {"plugins": [], "has_local_source": bool(default_packages)}

    @staticmethod
    def list_model_plugins(
        tenant_id: str,
        *,
        query: str = "",
        exclude: list[str] | None = None,
    ) -> list[LocalMarketplacePlugin]:
        result = OfflineMarketplaceService.list_model_plugins_result(tenant_id, query=query, exclude=exclude)
        return result["plugins"]

    @staticmethod
    def list_model_collection_plugins_result(
        tenant_id: str,
        collection_id: str,
        *,
        query: str = "",
        exclude: list[str] | None = None,
    ) -> LocalMarketplaceResult:
        if collection_id != _DEFAULT_MODEL_COLLECTION_ID:
            return {"plugins": [], "has_local_source": False}
        return OfflineMarketplaceService.list_model_plugins_result(tenant_id, query=query, exclude=exclude)

    @staticmethod
    def has_local_model_source() -> bool:
        return bool(list_default_model_packages())

    @staticmethod
    def list_model_collection_plugins(
        tenant_id: str,
        collection_id: str,
        *,
        query: str = "",
        exclude: list[str] | None = None,
    ) -> list[LocalMarketplacePlugin]:
        result = OfflineMarketplaceService.list_model_collection_plugins_result(
            tenant_id,
            collection_id,
            query=query,
            exclude=exclude,
        )
        return result["plugins"]
