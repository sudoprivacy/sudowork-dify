"""POST /sudowork/system/apps — create a Dify App on behalf of a tenant.

System-token bearer auth + X-Sudowork-Tenant header. The handler resolves
the tenant + actor account from headers, then delegates to AppService so
the App ends up looking identical to one created via the console UI.

Why not just hand sudowork-server a console session? Console JWTs are
short-lived and per-user; sudowork-server is a back-office process and
needs a stable credential it can hold long-term.
"""

from __future__ import annotations

import logging
from typing import Any

from flask import g
from flask_restx import Resource
from pydantic import BaseModel
from sqlalchemy import select
from werkzeug.exceptions import BadRequest, NotFound

from controllers.common.schema import register_schema_models
from controllers.sudowork import api
from controllers.sudowork.wraps import sudowork_integration_enabled, sudowork_system_token_required
from extensions.ext_database import db
from models.account import Account, Tenant, TenantAccountJoin, TenantStatus
from models.model import ApiToken, ApiTokenType, App, AppMode
from services.app_service import AppService, CreateAppParams

logger = logging.getLogger(__name__)


sudowork_system_apps_ns = api.namespace(
    "sudowork_system_apps",
    description="System-token App CRUD on behalf of a SudoWork tenant",
    path="/system",
)


class CreateAppPayload(BaseModel):
    name: str
    description: str | None = None
    mode: str = "agent-chat"
    icon: str | None = None
    icon_type: str | None = None
    icon_background: str | None = None


class CreateAppResponse(BaseModel):
    app_id: str
    mode: str
    name: str
    # Per-app service API key minted alongside the App so sudowork-server's
    # runtime layer can call /v1/chat-messages and /v1/workflows/run with the
    # correct scope. NOT a long-lived secret to leak; it lives in the
    # sudowork-server SQLite next to the binding.
    app_api_key: str


class AppSummary(BaseModel):
    id: str
    name: str
    mode: str
    description: str | None = None


class ListAppsResponse(BaseModel):
    apps: list[AppSummary]


register_schema_models(sudowork_system_apps_ns, CreateAppPayload, CreateAppResponse, AppSummary, ListAppsResponse)


def _resolve_tenant() -> Tenant:
    tenant_id = getattr(g, "sudowork_tenant_code", None)
    if not tenant_id:
        raise BadRequest("missing X-Sudowork-Tenant")
    tenant = db.session.scalar(select(Tenant).where(Tenant.id == tenant_id))
    if tenant is None or tenant.status != TenantStatus.NORMAL:
        raise NotFound(f"tenant not active: {tenant_id}")
    return tenant


def _resolve_actor(tenant: Tenant) -> Account:
    """Find the Account who is the conceptual creator of this call.

    Preference order:
    1. X-Sudowork-Actor (must be a member of the tenant) — used when the
       request originated from a logged-in admin.
    2. The tenant's OWNER (the system account provisioned at bootstrap).
    """

    actor_id = getattr(g, "sudowork_actor", None)
    if actor_id:
        account = db.session.scalar(
            select(Account)
            .join(TenantAccountJoin, TenantAccountJoin.account_id == Account.id)
            .where(Account.id == actor_id, TenantAccountJoin.tenant_id == tenant.id)
        )
        if account is not None:
            account.current_tenant = tenant
            return account
        logger.warning("sudowork_actor_not_member: actor=%s tenant=%s", actor_id, tenant.id)

    owner = db.session.scalar(
        select(Account)
        .join(TenantAccountJoin, TenantAccountJoin.account_id == Account.id)
        .where(TenantAccountJoin.tenant_id == tenant.id, TenantAccountJoin.role == "owner")
        .limit(1)
    )
    if owner is None:
        raise NotFound("tenant has no owner account")
    owner.current_tenant = tenant
    return owner


@sudowork_system_apps_ns.route("/apps")
class SudoworkSystemApps(Resource):
    @sudowork_integration_enabled
    @sudowork_system_token_required
    @sudowork_system_apps_ns.expect(sudowork_system_apps_ns.models[CreateAppPayload.__name__])
    def post(self):
        args = CreateAppPayload.model_validate(sudowork_system_apps_ns.payload or {})
        tenant = _resolve_tenant()
        actor = _resolve_actor(tenant)

        params = CreateAppParams(
            name=args.name,
            description=args.description,
            mode=args.mode,  # type: ignore[arg-type]
            icon=args.icon,
            icon_type=args.icon_type,
            icon_background=args.icon_background,
        )
        app = AppService().create_app(tenant_id=tenant.id, params=params, account=actor)

        # Mint a per-app API key. This is the same operation the console UI
        # performs when a user clicks "Create API key" on the app's API
        # access page; the token type must be 'app' (not the 'dataset' token
        # provisioned at tenant bootstrap) for /v1/chat-messages and
        # /v1/workflows/run to accept it.
        app_token = ApiToken.generate_api_key("app-", 24)
        api_token = ApiToken()
        api_token.token = app_token
        api_token.app_id = app.id
        api_token.tenant_id = tenant.id
        api_token.type = ApiTokenType.APP
        db.session.add(api_token)
        db.session.commit()

        return (
            CreateAppResponse(
                app_id=app.id,
                mode=app.mode,
                name=app.name,
                app_api_key=app_token,
            ).model_dump(),
            200,
        )

    @sudowork_integration_enabled
    @sudowork_system_token_required
    def get(self):
        tenant = _resolve_tenant()
        rows = db.session.scalars(
            select(App).where(App.tenant_id == tenant.id).order_by(App.created_at.desc()).limit(500)
        ).all()
        return (
            ListAppsResponse(
                apps=[
                    AppSummary(
                        id=row.id,
                        name=row.name,
                        mode=AppMode(row.mode).value if row.mode else "",
                        description=row.description,
                    )
                    for row in rows
                ]
            ).model_dump(),
            200,
        )


@sudowork_system_apps_ns.route("/apps/<string:app_id>")
class SudoworkSystemApp(Resource):
    @sudowork_integration_enabled
    @sudowork_system_token_required
    def delete(self, app_id: str):
        tenant = _resolve_tenant()
        app = db.session.scalar(select(App).where(App.id == app_id, App.tenant_id == tenant.id))
        if app is None:
            raise NotFound("app not found in tenant")
        db.session.delete(app)
        db.session.commit()
        return {"deleted": True, "app_id": app_id}, 200


__all__: list[Any] = []
