"""GET /sudowork/system/datasets — list datasets in a tenant.

System-token bearer auth + X-Sudowork-Tenant. Used by the SudoWork admin
UI to populate the "attach knowledge base" dropdown when configuring an
enterprise assistant.

Read-only. Dataset creation/edit happens in Dify Studio (admin SSO'd in).
"""

from __future__ import annotations

import logging

from flask import g
from flask_restx import Resource
from pydantic import BaseModel
from sqlalchemy import select
from werkzeug.exceptions import BadRequest, NotFound

from controllers.common.schema import register_schema_models
from controllers.sudowork import api
from controllers.sudowork.wraps import sudowork_integration_enabled, sudowork_system_token_required
from extensions.ext_database import db
from models.account import Tenant, TenantStatus
from models.dataset import Dataset

logger = logging.getLogger(__name__)


sudowork_system_datasets_ns = api.namespace(
    "sudowork_system_datasets",
    description="System-token dataset list on behalf of a SudoWork tenant",
    path="/system",
)


class DatasetSummary(BaseModel):
    id: str
    name: str
    description: str | None = None
    permission: str | None = None


class ListDatasetsResponse(BaseModel):
    datasets: list[DatasetSummary]


register_schema_models(sudowork_system_datasets_ns, DatasetSummary, ListDatasetsResponse)


def _resolve_tenant() -> Tenant:
    tenant_id = getattr(g, "sudowork_tenant_code", None)
    if not tenant_id:
        raise BadRequest("missing X-Sudowork-Tenant")
    tenant = db.session.scalar(select(Tenant).where(Tenant.id == tenant_id))
    if tenant is None or tenant.status != TenantStatus.NORMAL:
        raise NotFound(f"tenant not active: {tenant_id}")
    return tenant


@sudowork_system_datasets_ns.route("/datasets")
class SudoworkSystemDatasets(Resource):
    @sudowork_integration_enabled
    @sudowork_system_token_required
    def get(self):
        tenant = _resolve_tenant()
        rows = db.session.scalars(
            select(Dataset)
            .where(Dataset.tenant_id == tenant.id)
            .order_by(Dataset.created_at.desc())
            .limit(500)
        ).all()
        return (
            ListDatasetsResponse(
                datasets=[
                    DatasetSummary(
                        id=row.id,
                        name=row.name,
                        description=row.description,
                        permission=row.permission,
                    )
                    for row in rows
                ]
            ).model_dump(),
            200,
        )
