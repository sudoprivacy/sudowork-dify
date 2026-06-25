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

Idempotent — re-importing this module won't double-wrap.
"""

from __future__ import annotations

import logging
from typing import Sequence

logger = logging.getLogger(__name__)


def apply_marketplace_to_local_redirect() -> None:
    from core.plugin.impl.plugin import PluginInstaller
    from core.plugin.plugin_service import PluginService

    if getattr(PluginService.install_from_marketplace_pkg, "_sudowork_wrapped", False):
        return

    original = PluginService.install_from_marketplace_pkg

    @staticmethod
    def install_from_marketplace_pkg_redirected(
        tenant_id: str,
        plugin_unique_identifiers: Sequence[str],
    ):
        installer = PluginInstaller()
        all_local = True
        for uid in plugin_unique_identifiers:
            try:
                installer.decode_plugin_from_identifier(tenant_id, uid)
            except Exception:
                all_local = False
                break

        if all_local:
            logger.info(
                "sudowork_install_short_circuit tenant=%s identifiers=%s reason=already_declared",
                tenant_id,
                list(plugin_unique_identifiers),
            )
            return PluginService.install_from_local_pkg(tenant_id, plugin_unique_identifiers)

        logger.info(
            "sudowork_install_fallback_to_marketplace tenant=%s identifiers=%s reason=not_in_local_cache",
            tenant_id,
            list(plugin_unique_identifiers),
        )
        return original(tenant_id, plugin_unique_identifiers)

    install_from_marketplace_pkg_redirected._sudowork_wrapped = True  # type: ignore[attr-defined]
    PluginService.install_from_marketplace_pkg = install_from_marketplace_pkg_redirected  # type: ignore[method-assign]

    logger.info("sudowork: PluginService.install_from_marketplace_pkg patched to prefer local")
