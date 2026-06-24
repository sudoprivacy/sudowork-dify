"""Services backing the SudoWork integration namespace.

Kept in their own package so upstream Dify rebases never touch us:
- sso_service: verify the short-lived SSO JWT, upsert Account + tenant join.
- tenant_provisioning_service: bootstrap Tenant + system Account + Service API key.
- tenant_lookup_service: code <-> Tenant id mapping used by management endpoints.
"""
