from __future__ import annotations

from flask import Blueprint, jsonify, redirect, render_template, request, url_for

import db as storage
from auth import login_user, logout_user, verify_password


def create_auth_blueprint(*, app_name: str, db_conn, serialize_user, json_error):
    bp = Blueprint("auth_routes", __name__)

    @bp.get("/login", endpoint="login")
    def login():
        from flask import g

        if getattr(g, "current_user", None):
            return redirect(url_for("calendar_page"))
        return render_template("login.html", app_name=app_name, error=None)

    @bp.post("/login", endpoint="login_post")
    def login_post():
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        if not email or not password:
            return render_template("login.html", app_name=app_name, error="Email and password are required."), 400
        with db_conn() as conn:
            user = storage.get_user_by_email(conn, email)
        if not user or not verify_password(user["password_hash"], password):
            return render_template("login.html", app_name=app_name, error="Invalid email or password."), 401
        login_user(int(user["id"]))
        target = request.args.get("next") or url_for("calendar_page")
        return redirect(target)

    @bp.post("/logout", endpoint="logout_post")
    def logout_post():
        logout_user()
        return redirect(url_for("login"))

    @bp.post("/api/login", endpoint="api_login")
    def api_login():
        data = request.get_json(silent=True) or {}
        email = str(data.get("email") or "").strip().lower()
        password = str(data.get("password") or "")
        if not email or not password:
            return json_error("email and password are required.", 400, "invalid_credentials")
        with db_conn() as conn:
            user = storage.get_user_by_email(conn, email)
        if not user or not verify_password(user["password_hash"], password):
            return json_error("Invalid email or password.", 401, "invalid_credentials")
        login_user(int(user["id"]))
        return jsonify({"ok": True, "user": serialize_user(user)})

    @bp.post("/api/logout", endpoint="api_logout")
    def api_logout():
        logout_user()
        return jsonify({"ok": True})

    return bp
