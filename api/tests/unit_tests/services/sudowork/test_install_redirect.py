from types import SimpleNamespace
from typing import Any

from services.sudowork import install_redirect


class _Result:
    def fetchone(self) -> None:
        return None


class _Connection:
    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def execute(self, _statement: Any, _params: dict[str, str]) -> _Result:
        return _Result()


class _Engine:
    def connect(self) -> _Connection:
        return _Connection()


def test_resolve_to_local_identifier_falls_back_to_default_package_lock(monkeypatch) -> None:
    monkeypatch.setattr(install_redirect, "_get_plugin_db_engine", lambda: _Engine())
    monkeypatch.setattr(
        install_redirect,
        "resolve_default_package_identifier",
        lambda requested_uid: "langgenius/openai:0.4.2@local",
    )

    local_uid = install_redirect._resolve_to_local_identifier("langgenius/openai:0.4.2@marketplace")

    assert local_uid == "langgenius/openai:0.4.2@local"


def test_ensure_local_identifier_uploaded_reads_default_package_when_daemon_misses(monkeypatch) -> None:
    class _Installer:
        def fetch_plugin_manifest(self, _tenant_id: str, _local_uid: str) -> None:
            raise RuntimeError("plugin not found")

        def upload_pkg(self, tenant_id: str, content: bytes, verify_signature: bool = False):
            assert tenant_id == "tenant-1"
            assert content == b"pkg-bytes"
            assert verify_signature is False
            return SimpleNamespace(unique_identifier="langgenius/openai:0.4.2@uploaded", verification=None)

    class _PluginService:
        @staticmethod
        def _check_plugin_installation_scope(_verification) -> None:
            return None

    monkeypatch.setattr(
        install_redirect,
        "read_default_package",
        lambda requested_uid: {
            "plugin_unique_identifier": requested_uid,
            "content": b"pkg-bytes",
        },
    )
    monkeypatch.setattr(
        "services.feature_service.FeatureService.get_system_features",
        lambda: SimpleNamespace(
            plugin_installation_permission=SimpleNamespace(restrict_to_marketplace_only=False),
        ),
    )
    monkeypatch.setattr("core.plugin.impl.plugin.PluginInstaller", lambda: _Installer())
    monkeypatch.setattr("core.plugin.plugin_service.PluginService", _PluginService)

    identifier = install_redirect._ensure_local_identifier_uploaded("tenant-1", "langgenius/openai:0.4.2@local")

    assert identifier == "langgenius/openai:0.4.2@uploaded"
