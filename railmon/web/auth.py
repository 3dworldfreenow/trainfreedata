"""Session authentication, roles and CSRF protection."""
from __future__ import annotations

import hmac
import secrets
from functools import wraps

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from ..extensions import cache, db
from ..models import User
from ..syslog import log_event
from ..timeutil import utcnow

auth_bp = Blueprint("auth", __name__)


def _internal_token_ok() -> bool:
    expected = current_app.config.get("INTERNAL_API_TOKEN") or ""
    supplied = request.headers.get("X-Internal-Token", "")
    return bool(expected) and hmac.compare_digest(expected, supplied)


def csrf_token() -> str:
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


def init_app(app) -> None:
    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.before_request
    def load_user():
        g.user = None
        g.internal = False
        if request.path.startswith("/api/") and _internal_token_ok():
            # Internal service call: act as the first active admin.
            g.internal = True
            g.user = db.session.query(User).filter_by(role="admin", is_active=True).order_by(User.id).first()
            return
        uid = session.get("uid")
        if uid:
            user = db.session.get(User, uid)
            if user and user.is_active:
                g.user = user
            else:
                session.clear()

    @app.before_request
    def csrf_protect():
        if request.method in ("GET", "HEAD", "OPTIONS") or g.get("internal"):
            return
        token = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
        if not session.get("_csrf") or not hmac.compare_digest(session["_csrf"], token):
            abort(400, description="CSRF token missing or invalid")

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        return resp


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            if request.path.startswith("/api/"):
                abort(401)
            return redirect(url_for("auth.login", next=request.full_path))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            if request.path.startswith("/api/"):
                abort(401)
            return redirect(url_for("auth.login", next=request.full_path))
        if g.user.role != "admin":
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def public_page(view):
    """Public railway pages; optionally require login via LOGIN_REQUIRED_FOR_PUBLIC_PAGES."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_app.config.get("LOGIN_REQUIRED_FOR_PUBLIC_PAGES") and g.user is None:
            if request.path.startswith("/api/"):
                abort(401)
            return redirect(url_for("auth.login", next=request.full_path))
        return view(*args, **kwargs)

    return wrapped


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()
        if not cache.allow(f"login:{ip}", 10, 600):
            flash("Too many login attempts. Try again in a few minutes.", "danger")
            return render_template("pages/login.html"), 429
        user = db.session.query(User).filter_by(username=request.form.get("username", "")).first()
        if user and user.is_active and check_password_hash(user.password_hash, request.form.get("password", "")):
            session.clear()
            session["uid"] = user.id
            session.permanent = True
            user.last_login_at = utcnow()
            db.session.commit()
            log_event("INFO", "auth", "login", user=user.username)
            nxt = request.args.get("next") or ""
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("views.home"))
        log_event("WARNING", "auth", "failed login", username=request.form.get("username", "")[:64], ip=ip)
        flash("Invalid username or password", "danger")
    return render_template("pages/login.html")


@auth_bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("views.home"))
