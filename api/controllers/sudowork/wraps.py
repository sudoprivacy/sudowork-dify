"""Authentication decorators for the SudoWork integration namespace.

Three layers:

- sudowork_integration_enabled — gates the whole namespace. Returns 404 when
  off, matching the convention used by inner_api.

- sudowork_system_token_required — long-lived bearer; carries an explicit
  X-Sudowork-Tenant header. The decorator resolves the tenant via the
  binding service and stashes it on flask.g for handlers.

- sudowork_system_hmac_required — HMAC over the raw request body for the
  tenant provisioning call (one-shot, no tenant context yet).

Both system-auth modes additionally honor SUDOWORK_ALLOWED_IPS if set.
"""

from __future__ import annotations

import hmac
import ipaddress
from collections.abc import Callable
from functools import wraps
from hashlib import sha256
from typing import Any

from flask import abort, g, request

from configs import dify_config


def _ip_allowed() -> bool:
    raw = (dify_config.SUDOWORK_ALLOWED_IPS or "").strip()
    if not raw:
        return True
    try:
        client_ip = ipaddress.ip_address(request.remote_addr or "")
    except ValueError:
        return False
    for chunk in raw.split(","):
        token = chunk.strip()
        if not token:
            continue
        try:
            if "/" in token:
                if client_ip in ipaddress.ip_network(token, strict=False):
                    return True
            else:
                if client_ip == ipaddress.ip_address(token):
                    return True
        except ValueError:
            continue
    return False


def sudowork_integration_enabled[**P, R](view: Callable[P, R]) -> Callable[P, R]:
    @wraps(view)
    def decorated(*args: P.args, **kwargs: P.kwargs) -> R:
        if not dify_config.SUDOWORK_INTEGRATION_ENABLED:
            abort(404)
        return view(*args, **kwargs)

    return decorated


def sudowork_system_token_required[**P, R](view: Callable[P, R]) -> Callable[P, R]:
    """Bearer-token auth for system-to-system management calls.

    On success sets g.sudowork_tenant_code (str) and g.sudowork_actor (str|None).
    Handlers are responsible for resolving the actual Tenant via the bound code.
    """

    @wraps(view)
    def decorated(*args: P.args, **kwargs: P.kwargs) -> R:
        if not dify_config.SUDOWORK_INTEGRATION_ENABLED:
            abort(404)
        if not dify_config.SUDOWORK_SYSTEM_TOKEN:
            abort(503, description="SUDOWORK_SYSTEM_TOKEN not configured")
        if not _ip_allowed():
            abort(403, description="source IP not in SUDOWORK_ALLOWED_IPS")

        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            abort(401)
        token = auth[len("Bearer ") :]
        if not hmac.compare_digest(token, dify_config.SUDOWORK_SYSTEM_TOKEN):
            abort(401)

        tenant_code = request.headers.get("X-Sudowork-Tenant", "").strip()
        if not tenant_code:
            abort(400, description="missing X-Sudowork-Tenant header")

        g.sudowork_tenant_code = tenant_code
        g.sudowork_actor = (request.headers.get("X-Sudowork-Actor") or "").strip() or None
        return view(*args, **kwargs)

    return decorated


def sudowork_system_hmac_required[**P, R](view: Callable[P, R]) -> Callable[P, R]:
    """HMAC-SHA256 over the raw request body for one-shot bootstrap calls.

    The client must send:
      X-Sudowork-Signature: hex(HMAC_SHA256(secret, body))
    SUDOWORK_SYSTEM_SECRET must be configured.
    """

    @wraps(view)
    def decorated(*args: P.args, **kwargs: P.kwargs) -> R:
        if not dify_config.SUDOWORK_INTEGRATION_ENABLED:
            abort(404)
        if not dify_config.SUDOWORK_SYSTEM_SECRET:
            abort(503, description="SUDOWORK_SYSTEM_SECRET not configured")
        if not _ip_allowed():
            abort(403, description="source IP not in SUDOWORK_ALLOWED_IPS")

        signature = request.headers.get("X-Sudowork-Signature", "")
        if not signature:
            abort(401)

        body = request.get_data(cache=True) or b""
        expected = hmac.new(
            dify_config.SUDOWORK_SYSTEM_SECRET.encode("utf-8"),
            body,
            sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            abort(401)

        return view(*args, **kwargs)

    return decorated


def get_sudowork_context() -> dict[str, Any]:
    return {
        "tenant_code": getattr(g, "sudowork_tenant_code", None),
        "actor": getattr(g, "sudowork_actor", None),
    }
