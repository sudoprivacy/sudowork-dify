"""PATCH /sudowork/system/tenant — keep Dify tenant.name in sync with the
SudoWork enterprise.name shown in admin UI.

System-token bearer auth + X-Sudowork-Tenant header (same pattern as
system_apps / system_datasets). The tenant id comes from the header so a
caller can't rename someone else's tenant by passing the wrong id in the
body.

Why this exists: when sudowork-server provisions a Dify tenant, we seed
its name from `enterprises.name`. Later if an admin renames the enterprise
in sudowork-server, the Dify workspace title would silently drift. This
endpoint lets sudowork-server keep them in step.
"""

from __future__ import annotations

import logging
from typing import Any

from flask_restx import Resource
from pydantic import BaseModel
from werkzeug.exceptions import BadRequest

from controllers.common.schema import register_schema_models
from controllers.sudowork import api
from controllers.sudowork.wraps import sudowork_integration_enabled, sudowork_system_token_required
from controllers.sudowork.system_apps import _resolve_tenant
from extensions.ext_database import db

logger = logging.getLogger(__name__)


sudowork_system_tenant_ns = api.namespace(
    "sudowork_system_tenant",
    description="System-token tenant metadata sync",
    path="/system",
)


class UpdateTenantPayload(BaseModel):
    name: str | None = None


class UpdateTenantResponse(BaseModel):
    id: str
    name: str


register_schema_models(sudowork_system_tenant_ns, UpdateTenantPayload, UpdateTenantResponse)


@sudowork_system_tenant_ns.route("/tenant")
class SudoworkSystemTenant(Resource):
    @sudowork_integration_enabled
    @sudowork_system_token_required
    @sudowork_system_tenant_ns.expect(sudowork_system_tenant_ns.models[UpdateTenantPayload.__name__])
    def patch(self):
        args = UpdateTenantPayload.model_validate(sudowork_system_tenant_ns.payload or {})
        tenant = _resolve_tenant()

        new_name = (args.name or "").strip()
        if not new_name:
            raise BadRequest("name must be a non-empty string")

        if tenant.name != new_name:
            tenant.name = new_name
            db.session.commit()
            logger.info("sudowork_tenant_renamed: tenant=%s new_name=%s", tenant.id, new_name)

        return (
            UpdateTenantResponse(id=tenant.id, name=tenant.name).model_dump(),
            200,
        )


__all__: list[Any] = []
