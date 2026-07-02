import json
import zipfile
from pathlib import Path

from services.sudowork import offline_plugin_package_service as service


def _write_package(path: Path, manifest: str = "") -> None:
    with zipfile.ZipFile(path, "w") as package:
        package.writestr(
            "manifest.yaml",
            manifest
            or """
version: 0.4.2
type: plugin
author: langgenius
name: openai
description:
  en_US: Models provided by OpenAI.
label:
  en_US: OpenAI
icon: icon_s_en.svg
plugins:
  models:
    - provider/openai.yaml
""",
        )
        package.writestr(
            "provider/openai.yaml",
            """
provider: openai
icon_small:
  en_US: icon_s_en.svg
  zh_Hans: icon_s_zh.svg
icon_small_dark:
  en_US: icon_s_dark.svg
""",
        )
        package.writestr("_assets/icon_s_en.svg", "<svg />")
        package.writestr("_assets/icon_s_zh.svg", "<svg zh />")
        package.writestr("_assets/icon_s_dark.svg", "<svg dark />")


def _patch_package_paths(
    monkeypatch,
    tmp_path: Path,
    *,
    lock_sha: str = "local",
    file_sha: str = "local",
    enabled_specs: list[str] | None = None,
) -> str:
    package_dir = tmp_path / "plugin_packages" / "langgenius"
    package_dir.mkdir(parents=True)
    plugin_unique_identifier = f"langgenius/openai:0.4.2@{lock_sha}"
    _write_package(package_dir / f"openai:0.4.2@{file_sha}")
    lockfile = tmp_path / "default-plugins.lock.json"
    lockfile.write_text(
        json.dumps({"resolved": {"langgenius/openai:0.4.2": plugin_unique_identifier}}),
        encoding="utf-8",
    )
    listfile = tmp_path / "default-plugins.json"
    listfile.write_text(
        json.dumps({"plugins": enabled_specs if enabled_specs is not None else ["langgenius/openai:0.4.2"]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(service, "_DEFAULT_PLUGINS_LOCK_PATH", str(lockfile))
    monkeypatch.setattr(service, "_DEFAULT_PLUGINS_LIST_PATH", str(listfile))
    monkeypatch.setattr(service, "_DEFAULT_PLUGINS_PKG_DIR", str(package_dir))
    return plugin_unique_identifier


def test_list_default_model_packages_reads_lockfile_and_package_manifest(monkeypatch, tmp_path) -> None:
    plugin_unique_identifier = _patch_package_paths(monkeypatch, tmp_path)

    packages = service.list_default_model_packages()

    assert len(packages) == 1
    assert packages[0]["plugin_unique_identifier"] == plugin_unique_identifier
    assert packages[0]["plugin_id"] == "langgenius/openai"
    assert packages[0]["manifest"]["label"] == {"en_US": "OpenAI"}


def test_list_default_model_packages_uses_actual_local_package_identifier(monkeypatch, tmp_path) -> None:
    _patch_package_paths(monkeypatch, tmp_path, lock_sha="marketplace", file_sha="local")

    packages = service.list_default_model_packages()

    assert packages[0]["plugin_unique_identifier"] == "langgenius/openai:0.4.2@local"


def test_resolve_default_package_identifier_uses_actual_local_package_identifier(monkeypatch, tmp_path) -> None:
    _patch_package_paths(monkeypatch, tmp_path, lock_sha="marketplace", file_sha="local")

    identifier = service.resolve_default_package_identifier("langgenius/openai:0.4.2@upstream")

    assert identifier == "langgenius/openai:0.4.2@local"


def test_lockfile_entries_not_in_active_plugin_list_are_ignored(monkeypatch, tmp_path) -> None:
    _patch_package_paths(monkeypatch, tmp_path, enabled_specs=[])

    packages = service.list_default_model_packages()
    identifier = service.resolve_default_package_identifier("langgenius/openai:0.4.2@upstream")

    assert packages == []
    assert identifier is None


def test_get_default_package_manifest_returns_marketplace_response_shape(monkeypatch, tmp_path) -> None:
    _patch_package_paths(monkeypatch, tmp_path, lock_sha="marketplace", file_sha="local")

    manifest = service.get_default_package_manifest("langgenius/openai:0.4.2@upstream")

    assert manifest is not None
    assert manifest["name"] == "openai"
    assert manifest["category"] == "model"
    assert manifest["plugins"] == {"models": ["provider/openai.yaml"]}
    assert manifest["created_at"] == "1970-01-01T00:00:00Z"
    assert manifest["meta"] == {}


def test_read_default_package_uses_actual_local_package_identifier(monkeypatch, tmp_path) -> None:
    _patch_package_paths(monkeypatch, tmp_path, lock_sha="marketplace", file_sha="local")

    package = service.read_default_package("langgenius/openai:0.4.2@upstream")

    assert package is not None
    assert package["plugin_unique_identifier"] == "langgenius/openai:0.4.2@local"
    assert package["content"]


def test_extract_default_package_asset_reads_safe_asset(monkeypatch, tmp_path) -> None:
    plugin_unique_identifier = _patch_package_paths(monkeypatch, tmp_path)

    asset = service.extract_default_package_asset(plugin_unique_identifier, "icon_s_en.svg")

    assert asset is not None
    assert asset["content"] == b"<svg />"
    assert asset["mimetype"] == "image/svg+xml"


def test_extract_default_package_asset_rejects_path_traversal(monkeypatch, tmp_path) -> None:
    plugin_unique_identifier = _patch_package_paths(monkeypatch, tmp_path)

    asset = service.extract_default_package_asset(plugin_unique_identifier, "../manifest.yaml")

    assert asset is None


def test_extract_default_model_provider_icon_reads_provider_asset(monkeypatch, tmp_path) -> None:
    _patch_package_paths(monkeypatch, tmp_path)

    asset = service.extract_default_model_provider_icon(
        provider="langgenius/openai/openai",
        icon_type="icon_small",
        lang="zh_Hans",
    )

    assert asset is not None
    assert asset["content"] == b"<svg zh />"
    assert asset["mimetype"] == "image/svg+xml"


def test_extract_default_model_provider_icon_matches_short_provider_name(monkeypatch, tmp_path) -> None:
    _patch_package_paths(monkeypatch, tmp_path)

    asset = service.extract_default_model_provider_icon(
        provider="openai",
        icon_type="icon_small_dark",
        lang="en_US",
    )

    assert asset is not None
    assert asset["content"] == b"<svg dark />"
