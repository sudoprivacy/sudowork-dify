"""SudoWork integration blueprint.

All endpoints live under /sudowork:

  POST /sudowork/sso/exchange      — admin SSO landing, signs in browser
  POST /sudowork/system/tenants    — provision tenant + system account + API key
  PATCH /sudowork/system/tenant    — rename the resolved tenant (keeps Dify
                                     workspace name in sync with sudowork
                                     enterprise.name)
  GET  /sudowork/system/apps       — proxy: list apps in a tenant
  POST /sudowork/system/apps       — proxy: create app in a tenant
  DELETE /sudowork/system/apps/<id> — proxy: delete app
  GET  /sudowork/system/datasets   — proxy: list datasets in a tenant

The whole namespace is gated by SUDOWORK_INTEGRATION_ENABLED. See
api/services/sudowork/wraps logic for per-endpoint authentication.
"""

from flask import Blueprint

from libs.external_api import ExternalApi

bp = Blueprint("sudowork", __name__, url_prefix="/sudowork")

api = ExternalApi(
    bp,
    version="1.0",
    title="SudoWork Integration API",
    description="Internal endpoints called by sudowork-server.",
)

from . import sso_exchange as _sso_exchange  # noqa: E402,F401
from . import system_apps as _system_apps  # noqa: E402,F401
from . import system_datasets as _system_datasets  # noqa: E402,F401
from . import system_tenant as _system_tenant  # noqa: E402,F401
from . import tenant_provisioning as _tenant_provisioning  # noqa: E402,F401

__all__ = ["api", "bp"]
