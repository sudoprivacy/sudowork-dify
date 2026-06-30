"""Redirect `install_from_marketplace_pkg` to `install_from_local_pkg`
when the requested plugin is already declared locally.

Dify's "Install from Marketplace" button on the Studio "Model Providers"
page calls /workspaces/current/plugin/install/marketplace, which fans
out to PluginService.install_from_marketplace_pkg → plugin_daemon
downloads the .difypkg from marketplace.dify.ai on demand.

That's wrong for a Sudowork deployment:
  - The customer host has no PyPI / marketplace.dify.ai egress.
  - We've already pre-seeded every .difypkg + every transitive wheel
    via seed-plugins.sh, and tenant_provisioning_service has called
    upload_pkg to register declarations on the daemon side.
  - So any subsequent install should source from the local cache, not
    fetch fresh — fast, offline, no network bill.

We patch the service method (not the controller) so the redirect
covers every caller: console UI, internal scripts, API consumers.

Identifier translation: seed-plugins.sh re-zips each .difypkg after
rewriting its uv.lock URLs, which changes the sha256 portion of the
unique_identifier. plugin_declarations therefore stores identifiers
like `langgenius/openai:0.4.2@<NEW_sha>`, while Studio UI pulls
plugin metadata from marketplace.dify.ai and sends us
`langgenius/openai:0.4.2@<ORIGINAL_sha>`. We resolve the mismatch by
stripping `@<sha>` and looking up the matching local row.

plugin_declarations lives in the `dify_plugin` Postgres database
(plugin_daemon's schema), not the main `dify` DB that
extensions.ext_database connects to. We maintain a separate
read-only engine for the lookup, lazily constructed on first use.

Idempotent — re-importing this module won't double-wrap.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence

from services.sudowork.offline_plugin_package_service import resolve_default_package_identifier

logger = logging.getLogger(__name__)

_plugin_db_engine = None


def _get_plugin_db_engine():
    """Lazy-build a read-only SQLAlchemy engine for the plugin_daemon DB."""
    global _plugin_db_engine
    if _plugin_db_engine is not None:
        return _plugin_db_engine

    from sqlalchemy import create_engine

    host = os.environ.get("DB_HOST", "db_postgres")
    port = os.environ.get("DB_PORT", "5432")
    user = os.environ.get("DB_USERNAME", "postgres")
    password = os.environ.get("DB_PASSWORD", "")
    database = os.environ.get("DB_PLUGIN_DATABASE", "dify_plugin")
    url = f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{database}"
    _plugin_db_engine = create_engine(url, pool_size=1, max_overflow=0, pool_pre_ping=True)
    return _plugin_db_engine


def _resolve_to_local_identifier(requested_uid: str) -> str | None:
    """Map an upstream marketplace identifier (`<org>/<id>:<ver>@<sha_upstream>`)
    to whatever sha we have locally for the same `<org>/<id>:<ver>`. Returns
    the local uid, or None if no matching declaration exists.
    """
    from sqlalchemy import text

    prefix = requested_uid.split("@", 1)[0]  # e.g. "langgenius/openai:0.4.2"

    try:
        engine = _get_plugin_db_engine()
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT plugin_unique_identifier FROM plugin_declarations "
                    "WHERE plugin_unique_identifier LIKE :p "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {"p": f"{prefix}@%"},
            ).fetchone()
        if row:
            return row[0]
        return resolve_default_package_identifier(requested_uid)
    except Exception:
        logger.exception("sudowork_resolve_local_identifier_failed prefix=%s", prefix)
        return resolve_default_package_identifier(requested_uid)


def _silence_category_list_404() -> None:
    """Wrap PluginInstaller.list_plugins_by_category so a 404 from
    plugin_daemon returns an empty list instead of bubbling up as an
    error toast.

    Background: upstream Dify api (1.14.x) calls
    `GET /plugin/<tenant>/management/<category>/list` for tool, model,
    agent_strategy, datasource, trigger. But the bundled plugin_daemon
    (langgenius/dify-plugin-daemon:0.6.1-local) doesn't implement any
    of those category routes — they all return 404 "route not found".
    The Studio polls these endpoints periodically (sidebar tool icon,
    etc.), so admins see a steady drip of 404 toasts that look
    catastrophic but are completely benign.
    """
    from core.plugin.entities.plugin_daemon import PluginListWithoutTotalResponse
    from core.plugin.impl import plugin as plugin_impl

    cls = plugin_impl.PluginInstaller
    if getattr(cls.list_plugins_by_category, "_sudowork_wrapped", False):
        return

    original = cls.list_plugins_by_category

    def list_plugins_by_category_safe(self, tenant_id, category, page, page_size):
        try:
            return original(self, tenant_id, category, page, page_size)
        except Exception as e:
            msg = str(e).lower()
            if "404" in msg or "not found" in msg or "route not found" in msg:
                logger.debug(
                    "sudowork_category_list_empty tenant=%s category=%s (daemon 404, returning empty)",
                    tenant_id,
                    getattr(category, "value", category),
                )
                return PluginListWithoutTotalResponse(list=[], has_more=False)
            raise

    list_plugins_by_category_safe._sudowork_wrapped = True  # type: ignore[attr-defined]
    cls.list_plugins_by_category = list_plugins_by_category_safe  # type: ignore[assignment]

    logger.info(
        "sudowork: PluginInstaller.list_plugins_by_category patched to swallow daemon 404s"
    )


def apply_marketplace_to_local_redirect() -> None:
    from core.plugin.plugin_service import PluginService

    if getattr(PluginService.install_from_marketplace_pkg, "_sudowork_wrapped", False):
        return

    original = PluginService.install_from_marketplace_pkg

    @staticmethod
    def install_from_marketplace_pkg_redirected(
        tenant_id: str,
        plugin_unique_identifiers: Sequence[str],
    ):
        resolved: list[str] = []
        all_local = True
        for uid in plugin_unique_identifiers:
            local_uid = _resolve_to_local_identifier(uid)
            if local_uid is None:
                all_local = False
                break
            resolved.append(local_uid)

        if all_local:
            logger.info(
                "sudowork_install_short_circuit tenant=%s requested=%s resolved=%s reason=declared_locally",
                tenant_id,
                list(plugin_unique_identifiers),
                resolved,
            )
            return PluginService.install_from_local_pkg(tenant_id, resolved)

        logger.info(
            "sudowork_install_fallback_to_marketplace tenant=%s identifiers=%s reason=not_in_local_cache",
            tenant_id,
            list(plugin_unique_identifiers),
        )
        return original(tenant_id, plugin_unique_identifiers)

    install_from_marketplace_pkg_redirected._sudowork_wrapped = True  # type: ignore[attr-defined]
    PluginService.install_from_marketplace_pkg = install_from_marketplace_pkg_redirected  # type: ignore[method-assign]

    logger.info("sudowork: PluginService.install_from_marketplace_pkg patched to prefer local")

    # Also silence the upstream Dify polling 404s — same package since
    # both are "plumb over upstream daemon quirks for airgapped deploys".
    _silence_category_list_404()
