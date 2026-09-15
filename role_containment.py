"""#305 — containment gates for the roles that had none.

`client` (client_portal.py) and `architect` (elevation.py) each have a before_request
gate that decides which ROUTES exist for them at all. `estimator` and `vendor` did
not: an estimator could reach every endpoint that lacks a per-route decorator, and
`vendor` (still in the role catalog) had no policy anywhere. Hiding nav is not
access control — this is the server-side rule.

  estimator -> the estimating workspace + auth/static/shell chrome, nothing else
  vendor    -> nothing but auth/static until a vendor surface exists (and the role
               is no longer assignable from the admin UI — see auth_admin)
"""
from __future__ import annotations

import logging

from flask import jsonify, redirect, request

from auth import current_user

_COMMON_PREFIXES = ("/api/auth/", "/files/static/", "/auth/google/", "/p/", "/api/p/")   # #307 — proposal links
_COMMON_EXACT = {"/login", "/set-password", "/forgot-password", "/reset-password",
                 "/api/health", "/api/today"}

ESTIMATOR_PREFIXES = _COMMON_PREFIXES + ("/estimating", "/api/estimating/", "/settings/interface", "/api/ui/")
ESTIMATOR_EXACT = _COMMON_EXACT | {"/estimating"}
ESTIMATOR_HOME = "/estimating"


def _allowed(path: str, prefixes, exact) -> bool:
    return path in exact or any(path.startswith(p) for p in prefixes)


def _containment_gate():
    user = current_user()
    if not user:
        return None
    role = user.get("role")
    path = request.path
    if role == "estimator":
        if _allowed(path, ESTIMATOR_PREFIXES, ESTIMATOR_EXACT):
            return None
        logging.info(f"role_containment: estimator blocked path={path} uid={user.get('id')}")
        if path.startswith("/api/"):
            return jsonify({"error": "forbidden"}), 403
        return redirect(ESTIMATOR_HOME)
    if role == "vendor":
        if _allowed(path, _COMMON_PREFIXES, _COMMON_EXACT):
            return None
        logging.info(f"role_containment: vendor blocked path={path} uid={user.get('id')}")
        if path.startswith("/api/"):
            return jsonify({"error": "forbidden"}), 403
        return ("<!doctype html><meta charset=\"utf-8\"><title>No access</title>"
                "<p style=\"font-family:sans-serif;padding:40px\">This account has no dashboard "
                "access yet. Contact the office.</p>"), 403
    return None


def register(app) -> None:
    """MUST follow auth.apply_auth_gate (reads current_user())."""
    app.before_request(_containment_gate)
