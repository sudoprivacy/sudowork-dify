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
