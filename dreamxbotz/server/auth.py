"""Shared authentication for the optional HTTP dashboards.

The credential is the operator's ``ADMIN_DASHBOARD_TOKEN``, passed as
``X-Admin-Token`` (preferred) or ``?token=`` and compared in constant time.

A Telegram user id is PUBLIC information (any forwarded message or
@userinfobot reveals it), so it is NOT a credential. The legacy
``?admin_id=`` / ``X-Admin-Id`` path is only honoured when the operator
explicitly opts in with ``ADMIN_DASHBOARD_ALLOW_ID=True`` — it exists for
backwards compatibility and must not be relied on for access control.

``ADMIN_DASHBOARD_PUBLIC=True`` exists only for the offline preview harness
(``tools/preview_section.py``); it is never implied by a Host header or by an
empty admin list.
"""
from __future__ import annotations

import hmac
from aiohttp import web


def is_admin_request(request: web.Request) -> bool:
    try:
        import info
    except Exception:
        return False

    if getattr(info, "ADMIN_DASHBOARD_PUBLIC", False):
        return True

    configured = str(getattr(info, "ADMIN_DASHBOARD_TOKEN", "") or "")
    supplied_token = str(
        request.headers.get("X-Admin-Token") or request.query.get("token") or ""
    )
    if configured and supplied_token:
        return hmac.compare_digest(supplied_token, configured)

    # Legacy, insecure convenience — explicit opt-in only.
    if getattr(info, "ADMIN_DASHBOARD_ALLOW_ID", False):
        admins = {str(admin) for admin in getattr(info, "ADMINS", []) or []}
        supplied_id = str(request.query.get("admin_id") or request.headers.get("X-Admin-Id") or "")
        if supplied_id and supplied_id in admins:
            return True
    return False
