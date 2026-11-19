"""Shared authentication for the optional HTTP dashboards.

There are exactly two accepted credentials:

* an admin Telegram id (``ADMINS``), passed as ``?admin_id=`` or ``X-Admin-Id``;
* the operator's ``ADMIN_DASHBOARD_TOKEN``, passed as ``X-Admin-Token`` or
  ``?token=`` and compared in constant time.

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

    admins = {str(admin) for admin in getattr(info, "ADMINS", []) or []}
    supplied_id = str(request.query.get("admin_id") or request.headers.get("X-Admin-Id") or "")
    if supplied_id and supplied_id in admins:
        return True

    configured = str(getattr(info, "ADMIN_DASHBOARD_TOKEN", "") or "")
    supplied_token = str(
        request.headers.get("X-Admin-Token") or request.query.get("token") or ""
    )
    if configured and supplied_token:
        return hmac.compare_digest(supplied_token, configured)
    return False
