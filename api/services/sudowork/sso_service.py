"""SSO exchange: verify HMAC JWT from sudowork-server, upsert Account+join."""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from typing import Any

import jwt
from sqlalchemy import select, update

from configs import dify_config
from extensions.ext_database import db
from extensions.ext_redis import redis_client
from libs.helper import generate_string
from models.account import (
    Account,
    AccountStatus,
    Tenant,
    TenantAccountJoin,
    TenantAccountRole,
    TenantStatus,
)

logger = logging.getLogger(__name__)


class SudoworkSsoError(Exception):
    """Raised when the SSO JWT is malformed, replayed, or references missing state."""


@dataclass(frozen=True)
class SudoworkSsoResult:
    account: Account
    tenant: Tenant


_JTI_NAMESPACE = "sudowork:sso:jti:"


def _validate_role(role: str) -> TenantAccountRole:
    """Map the SudoWork-supplied role string onto a Dify role.

    We intentionally never grant OWNER from SSO — the owner seat belongs to the
    system-provisioned account and must not be claimable by a remote user.
    """

    normalized = (role or "").strip().lower()
    if normalized == "owner":
        normalized = "admin"
    if normalized == "":
        normalized = (dify_config.SUDOWORK_DEFAULT_ROLE or "admin").lower()
    try:
        candidate = TenantAccountRole(normalized)
    except ValueError as exc:
        raise SudoworkSsoError(f"unsupported role: {role}") from exc
    if candidate == TenantAccountRole.OWNER:
        candidate = TenantAccountRole.ADMIN
    return candidate


def _synthetic_email(sub: str) -> str:
    domain = dify_config.SUDOWORK_ACCOUNT_EMAIL_DOMAIN or "local.sudowork"
    return f"sudowork-{sub}@{domain}"


def _claim_jti(jti: str) -> None:
    """Reserve a JTI; raise if it was already used in the replay window."""

    key = _JTI_NAMESPACE + jti
    # NX -> only set if absent; if present, this is a replay.
    ok = redis_client.set(key, "1", nx=True, ex=dify_config.SUDOWORK_SSO_JTI_TTL_SECONDS)
    if not ok:
        raise SudoworkSsoError("jti already used (replay protection)")


def _decode(token: str) -> dict[str, Any]:
    secret = dify_config.SUDOWORK_SSO_SECRET
    if not secret:
        raise SudoworkSsoError("SUDOWORK_SSO_SECRET not configured")
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            options={"require": ["exp", "iat", "jti", "sub", "dify_tenant_id"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise SudoworkSsoError("token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise SudoworkSsoError(f"invalid token: {exc}") from exc
    return payload


def _load_tenant(tenant_id: str) -> Tenant:
    tenant = db.session.scalar(select(Tenant).where(Tenant.id == tenant_id))
    if tenant is None:
        raise SudoworkSsoError(f"tenant not found: {tenant_id}")
    if tenant.status != TenantStatus.NORMAL:
        raise SudoworkSsoError(f"tenant not active: {tenant_id}")
    return tenant


def _upsert_account(*, sub: str, email: str, name: str) -> Account:
    """Find or create the Account for this SudoWork user.

    We key on email — sudowork-server is expected to send a stable address per
    user (synthetic if no real email exists), so two SSO landings map to the
    same Dify account.
    """

    account = db.session.scalar(select(Account).where(Account.email == email))
    if account is not None:
        # SSO-provisioned accounts may still be PENDING from an earlier run.
        if account.status == AccountStatus.PENDING:
            account.status = AccountStatus.ACTIVE
        return account

    account = Account(
        name=name or sub,
        email=email,
        interface_language="en-US",
        interface_theme="light",
        timezone="UTC",
    )
    account.status = AccountStatus.ACTIVE
    db.session.add(account)
    db.session.flush()
    return account


def _ensure_membership(*, tenant: Tenant, account: Account, role: TenantAccountRole) -> None:
    """Upsert (tenant_id, account_id) membership and mark it as the *only*
    current workspace for this Account.

    2026-06-23 fix: previously this function set the target join's
    `current=True` but left other joins of the same account untouched, which
    allowed multiple `current=True` rows to coexist. Dify's UI then resolved
    the workspace name by picking the first such row, so an admin who SSO'd
    into different enterprises kept seeing the workspace from their first
    SSO. We now mirror what Dify's own ``TenantService.switch_tenant`` does:
    flip every *other* join to ``current=False`` first, then set the target to
    ``current=True``.
    """

    join = db.session.scalar(
        select(TenantAccountJoin).where(
            TenantAccountJoin.tenant_id == tenant.id,
            TenantAccountJoin.account_id == account.id,
        )
    )
    if join is None:
        join = TenantAccountJoin(
            tenant_id=tenant.id,
            account_id=account.id,
            role=role,
            current=False,
        )
        db.session.add(join)
        db.session.flush()
    elif join.role != role:
        # Re-bind role to whatever sudowork-server claims today; SudoWork is
        # the source of truth for membership.
        join.role = role

    # Clear `current` on every other join for this account, then set it on
    # this one. Order matters: do the bulk update first so we don't fight our
    # own write.
    db.session.execute(
        update(TenantAccountJoin)
        .where(
            TenantAccountJoin.account_id == account.id,
            TenantAccountJoin.tenant_id != tenant.id,
        )
        .values(current=False)
    )
    join.current = True


def exchange(token: str) -> SudoworkSsoResult:
    """Validate the SSO token and produce (Account, Tenant) for cookie issuance.

    The caller — controllers/sudowork/sso_exchange.py — uses the returned pair
    to call AccountService.login() and write the standard Dify console cookies,
    so downstream handlers see a fully-formed session.
    """

    payload = _decode(token)
    _claim_jti(str(payload["jti"]))

    sub = str(payload["sub"])
    tenant_id = str(payload["dify_tenant_id"])
    role_str = str(payload.get("role") or "admin")
    name = str(payload.get("name") or sub)
    email = str(payload.get("email") or "").strip() or _synthetic_email(sub)

    role = _validate_role(role_str)
    tenant = _load_tenant(tenant_id)
    account = _upsert_account(sub=sub, email=email, name=name)
    _ensure_membership(tenant=tenant, account=account, role=role)

    # Make this tenant the "current" workspace for the account so AccountService.login
    # places us into the right place.
    db.session.flush()
    account.current_tenant = tenant

    db.session.commit()
    return SudoworkSsoResult(account=account, tenant=tenant)


def stamp_audit(*, sub: str, tenant_id: str, jti: str) -> None:
    """Lightweight audit hook; promoted to a dedicated table later."""

    logger.info(
        "sudowork_sso_exchange",
        extra={
            "sudowork_sub": sub,
            "dify_tenant_id": tenant_id,
            "jti": jti,
        },
    )


# Re-export utilities used by tests / debug:
__all__ = ["SudoworkSsoError", "SudoworkSsoResult", "exchange", "stamp_audit"]


# silence unused-imports flagged by static checkers
_ = (json, secrets, generate_string)
