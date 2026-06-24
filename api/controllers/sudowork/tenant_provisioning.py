"""POST /sudowork/system/tenants — provision a Dify tenant for a SudoWork enterprise.

Server-to-server. The caller signs the raw JSON body with HMAC-SHA256 and
puts the hex digest in X-Sudowork-Signature; the wraps module verifies it.
"""

from __future__ import annotations

import logging

from flask_restx import Resource
from pydantic import BaseModel

from controllers.common.schema import register_schema_models
from controllers.sudowork import api
from controllers.sudowork.wraps import sudowork_integration_enabled, sudowork_system_hmac_required
from services.sudowork import tenant_provisioning_service

logger = logging.getLogger(__name__)


sudowork_provisioning_ns = api.namespace(
    "sudowork_provisioning",
    description="Tenant provisioning",
    path="/system",
)


class TenantProvisionRequest(BaseModel):
    enterprise_code: str
    enterprise_name: str | None = None


class TenantProvisionResponse(BaseModel):
    dify_tenant_id: str
    system_account_id: str
    service_api_key: str


register_schema_models(sudowork_provisioning_ns, TenantProvisionRequest, TenantProvisionResponse)


@sudowork_provisioning_ns.route("/tenants")
class SudoworkTenantProvision(Resource):
    @sudowork_integration_enabled
    @sudowork_system_hmac_required
    @sudowork_provisioning_ns.expect(sudowork_provisioning_ns.models[TenantProvisionRequest.__name__])
    @sudowork_provisioning_ns.doc("sudowork_tenant_provision")
    def post(self):
        args = TenantProvisionRequest.model_validate(sudowork_provisioning_ns.payload or {})
        result = tenant_provisioning_service.provision(
            enterprise_code=args.enterprise_code,
            enterprise_name=args.enterprise_name or args.enterprise_code,
        )
        return (
            TenantProvisionResponse(
                dify_tenant_id=result.dify_tenant_id,
                system_account_id=result.system_account_id,
                service_api_key=result.service_api_key,
            ).model_dump(),
            200,
        )
