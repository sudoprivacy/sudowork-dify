"""POST /sudowork/sso/exchange — SudoWork admin SSO landing.

The browser hits this URL after sudowork-server signs a short-lived JWT and
issues a 302 to it. We verify the JWT, upsert the Account/membership, write
the standard Dify console cookies, then 302 the browser to `next` so the
admin lands inside Dify already authenticated.
"""

from __future__ import annotations

import logging
from urllib.parse import urlencode

from flask import make_response, redirect, request
from flask_restx import Resource
from pydantic import BaseModel
from werkzeug.exceptions import BadRequest

from configs import dify_config
from controllers.common.schema import register_schema_models
from controllers.sudowork import api
from controllers.sudowork.wraps import sudowork_integration_enabled
from libs.token import (
    set_access_token_to_cookie,
    set_csrf_token_to_cookie,
    set_refresh_token_to_cookie,
)
from services.account_service import AccountService
from services.sudowork import sso_service

logger = logging.getLogger(__name__)


sudowork_ns = api.namespace("sudowork", description="SudoWork SSO + management endpoints", path="/")


class SsoExchangePayload(BaseModel):
    token: str
    next: str | None = None


register_schema_models(sudowork_ns, SsoExchangePayload)


def _safe_next(raw: str | None) -> str:
    """Restrict `next` to same-origin paths to prevent open-redirect.

    Dify is the only valid destination here; allowing absolute URLs would
    let a forged JWT bounce the admin to an attacker-controlled site.
    """

    if not raw:
        return "/"
    if not raw.startswith("/") or raw.startswith("//"):
        return "/"
    return raw


def _build_redirect_target(next_path: str) -> str:
    """Resolve `next_path` against the Dify Web origin.

    The browser hits us on the api host:port (e.g. localhost:5001) but the
    routes in `next` (`/app/.../configuration`, `/apps`, ...) belong to the
    Dify Web (Next.js) origin. A plain `redirect("/app/...")` would keep the
    browser on the api host and trigger a Flask 404. Prepend `CONSOLE_WEB_URL`
    so the browser bounces to the web origin instead.

    Cookies just set in this response remain visible: browsers scope cookies
    by hostname (not port) when no explicit Domain attribute is given, so
    `localhost:5001` → `localhost:80` carries them through.
    """

    web_base = (dify_config.CONSOLE_WEB_URL or "").rstrip("/")
    if not web_base:
        # Fall back to a same-origin redirect; admin will land at api 404
        # which is at least obvious, vs silently sending them to localhost
        # in another tenant's deployment.
        return next_path
    return f"{web_base}{next_path}"


@sudowork_ns.route("/sso/exchange")
class SudoworkSsoExchange(Resource):
    @sudowork_integration_enabled
    @sudowork_ns.doc("sudowork_sso_exchange")
    @sudowork_ns.doc(description="Exchange a SudoWork-issued SSO JWT for a Dify console session.")
    def get(self):
        token = (request.args.get("token") or "").strip()
        if not token:
            raise BadRequest("missing token")
        next_url = _safe_next(request.args.get("next"))

        try:
            result = sso_service.exchange(token)
        except sso_service.SudoworkSsoError as exc:
            logger.warning("sudowork_sso_failed: %s", exc)
            qs = urlencode({"error": "sso_failed", "reason": str(exc)})
            return redirect(f"/signin?{qs}")

        token_pair = AccountService.login(account=result.account, ip_address=request.remote_addr)

        response = make_response(redirect(_build_redirect_target(next_url)))
        set_access_token_to_cookie(request, response, token_pair.access_token)
        set_refresh_token_to_cookie(request, response, token_pair.refresh_token)
        set_csrf_token_to_cookie(request, response, token_pair.csrf_token)
        return response
