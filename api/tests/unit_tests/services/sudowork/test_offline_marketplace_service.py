import json
from collections.abc import Iterable
from typing import Any

from services.sudowork import offline_marketplace_service as service


class _Rows:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> Iterable[dict[str, Any]]:
        return iter(self._rows)


class _Connection:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def execute(self, _statement: Any) -> _Rows:
        return _Rows(self._rows)


class _Engine:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def connect(self) -> _Connection:
        return _Connection(self._rows)


def _declaration(
    *,
    name: str = "openai",
    label: str = "OpenAI",
    description: str = "Models provided by OpenAI.",
    category: str = "model",
) -> dict[str, Any]:
    return {
        "version": "0.4.2",
        "author": "langgenius",
        "name": name,
        "label": {"en_US": label},
        "description": {"en_US": description},
        "icon": f"{name}.svg",
        "icon_dark": f"{name}-dark.svg",
        "category": category,
        "model": {"provider": name} if category == "model" else None,
        "tags": ["llm"],
        "verified": True,
    }


def _row(plugin_id: str, unique_identifier: str, declaration: dict[str, Any]) -> dict[str, Any]:
    return {
        "plugin_id": plugin_id,
        "plugin_unique_identifier": unique_identifier,
        "declaration": json.dumps(declaration),
    }


def _patch_engine(monkeypatch, rows: list[dict[str, Any]]) -> None:
    def get_icon_url(tenant_id: str, filename: str) -> str:
        return (
            "/console/api/workspaces/current/plugin/icon"
            f"?tenant_id={tenant_id}&filename={filename}"
        )

    monkeypatch.setattr(service, "_get_plugin_db_engine", lambda: _Engine(rows))
    monkeypatch.setattr(
        service.PluginService,
        "get_plugin_icon_url",
        get_icon_url,
    )


def test_list_model_plugins_returns_marketplace_compatible_local_declarations(monkeypatch) -> None:
    _patch_engine(
        monkeypatch,
        [
            _row(
                "langgenius/openai",
                "langgenius/openai:0.4.2@local",
                _declaration(),
            )
        ],
    )

    plugins = service.OfflineMarketplaceService.list_model_plugins("tenant-1")

    assert len(plugins) == 1
    plugin = plugins[0]
    assert plugin["type"] == "plugin"
    assert plugin["plugin_id"] == "langgenius/openai"
    assert plugin["latest_package_identifier"] == "langgenius/openai:0.4.2@local"
    assert plugin["category"] == "model"
    assert plugin["from"] == "marketplace"
    assert plugin["label"] == {"en_US": "OpenAI"}
    assert plugin["brief"] == {"en_US": "Models provided by OpenAI."}
    assert plugin["tags"] == [{"name": "llm"}]
    assert plugin["icon"].endswith("tenant_id=tenant-1&filename=openai.svg")
    assert plugin["icon_dark"].endswith("tenant_id=tenant-1&filename=openai-dark.svg")


def test_list_model_plugins_filters_by_query_and_exclude(monkeypatch) -> None:
    _patch_engine(
        monkeypatch,
        [
            _row("langgenius/openai", "langgenius/openai:0.4.2@local", _declaration()),
            _row(
                "langgenius/anthropic",
                "langgenius/anthropic:0.2.0@local",
                _declaration(name="anthropic", label="Anthropic", description="Claude models."),
            ),
            _row(
                "langgenius/webscraper",
                "langgenius/webscraper:0.1.0@local",
                _declaration(name="webscraper", label="Web Scraper", category="tool"),
            ),
        ],
    )

    plugins = service.OfflineMarketplaceService.list_model_plugins(
        "tenant-1",
        query="claude",
        exclude=["langgenius/openai"],
    )

    assert [plugin["plugin_id"] for plugin in plugins] == ["langgenius/anthropic"]


def test_list_model_plugins_ignores_declarations_removed_from_default_list(monkeypatch) -> None:
    _patch_engine(
        monkeypatch,
        [
            _row(
                "langgenius/sagemaker",
                "langgenius/sagemaker:0.0.17@local",
                _declaration(name="sagemaker", label="Amazon SageMaker"),
            ),
            _row("langgenius/openai", "langgenius/openai:0.4.2@local", _declaration()),
        ],
    )
    monkeypatch.setattr(
        service,
        "is_enabled_default_plugin",
        lambda plugin_unique_identifier: "sagemaker" not in plugin_unique_identifier,
    )

    plugins = service.OfflineMarketplaceService.list_model_plugins("tenant-1")

    assert [plugin["plugin_id"] for plugin in plugins] == ["langgenius/openai"]


def test_list_model_collection_plugins_only_returns_pinned_model_collection(monkeypatch) -> None:
    _patch_engine(
        monkeypatch,
        [_row("langgenius/openai", "langgenius/openai:0.4.2@local", _declaration())],
    )

    assert service.OfflineMarketplaceService.list_model_collection_plugins("tenant-1", "other") == []
    plugins = service.OfflineMarketplaceService.list_model_collection_plugins(
        "tenant-1",
        "__model-settings-pinned-models",
    )

    assert [plugin["plugin_id"] for plugin in plugins] == ["langgenius/openai"]


def test_list_model_plugins_falls_back_to_default_package_manifests(monkeypatch) -> None:
    _patch_engine(monkeypatch, [])
    monkeypatch.setattr(
        service,
        "list_default_model_packages",
        lambda: [
            {
                "plugin_id": "langgenius/openai",
                "plugin_unique_identifier": "langgenius/openai:0.4.2@local",
                "package_path": "/tmp/openai.difypkg",
                "manifest": _declaration(),
            }
        ],
    )
    monkeypatch.setattr(
        service,
        "get_local_package_icon_url",
        lambda plugin_unique_identifier, filename: (
            "/console/api/workspaces/current/plugin/marketplace/local-model-provider-icon"
            f"?plugin_unique_identifier={plugin_unique_identifier}&filename={filename}"
        ),
    )

    result = service.OfflineMarketplaceService.list_model_plugins_result("tenant-1")

    assert result["has_local_source"] is True
    assert [plugin["plugin_id"] for plugin in result["plugins"]] == ["langgenius/openai"]
    assert result["plugins"][0]["icon"].endswith("filename=openai.svg")


def test_list_model_plugins_marks_local_source_when_package_query_has_no_match(monkeypatch) -> None:
    _patch_engine(monkeypatch, [])
    monkeypatch.setattr(
        service,
        "list_default_model_packages",
        lambda: [
            {
                "plugin_id": "langgenius/openai",
                "plugin_unique_identifier": "langgenius/openai:0.4.2@local",
                "package_path": "/tmp/openai.difypkg",
                "manifest": _declaration(),
            }
        ],
    )

    result = service.OfflineMarketplaceService.list_model_plugins_result("tenant-1", query="not-found")

    assert result == {"plugins": [], "has_local_source": True}
