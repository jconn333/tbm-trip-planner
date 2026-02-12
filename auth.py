from __future__ import annotations

from functools import wraps
import secrets

from flask import g, jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash


def hash_password(password: str) -> str:
    return generate_password_hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    return check_password_hash(password_hash, password)


def is_api_request() -> bool:
    return request.path.startswith("/api/")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not getattr(g, "current_user", None):
            if is_api_request():
                return jsonify({"error": "Authentication required.", "code": "auth_required"}), 401
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = getattr(g, "current_user", None)
        if not user:
            if is_api_request():
                return jsonify({"error": "Authentication required.", "code": "auth_required"}), 401
            return redirect(url_for("login", next=request.path))
        if user["role"] != "admin":
            if is_api_request():
                return jsonify({"error": "Admin privileges required.", "code": "forbidden"}), 403
            return redirect(url_for("calendar_page"))
        return view(*args, **kwargs)

    return wrapped


def login_user(user_id: int) -> None:
    session["user_id"] = int(user_id)
    if not session.get("csrf_token"):
        session["csrf_token"] = secrets.token_urlsafe(32)


def logout_user() -> None:
    session.pop("user_id", None)
    session.pop("csrf_token", None)
