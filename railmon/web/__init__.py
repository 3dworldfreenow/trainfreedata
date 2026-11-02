"""Web layer: dashboard pages (Jinja + Bootstrap) and the internal REST API."""
from __future__ import annotations

from ..timeutil import age_minutes, to_ist


def register_web(app) -> None:
    from . import auth
    from .api import api_bp
    from .views import views_bp

    auth.init_app(app)
    app.register_blueprint(auth.auth_bp)
    app.register_blueprint(views_bp)
    app.register_blueprint(api_bp, url_prefix="/api/v1")

    from flask import jsonify, render_template, request
    from werkzeug.exceptions import HTTPException

    @app.errorhandler(HTTPException)
    def http_error(exc):
        if request.path.startswith("/api/"):
            return jsonify({"status": "error", "error": exc.description}), exc.code
        return render_template("pages/error.html", code=exc.code, message=exc.description), exc.code

    @app.errorhandler(Exception)
    def unhandled(exc):
        from ..extensions import db
        from ..syslog import log_event

        db.session.rollback()
        log_event("ERROR", "web", "unhandled exception", path=request.path, error=f"{type(exc).__name__}: {exc}")
        if app.config.get("TESTING"):
            raise exc
        if request.path.startswith("/api/"):
            return jsonify({"status": "error", "error": "internal error"}), 500
        return render_template("pages/error.html", code=500, message="Internal error - details are in the system log."), 500

    @app.template_filter("ist")
    def ist_filter(dt, fmt="%d %b %Y %H:%M"):
        local = to_ist(dt)
        return local.strftime(fmt) + " IST" if local else "—"

    @app.template_filter("age")
    def age_filter(dt):
        m = age_minutes(dt)
        if m is None:
            return "never"
        if m < 1:
            return "just now"
        if m < 60:
            return f"{m} min ago"
        if m < 1440:
            return f"{m // 60} h {m % 60} min ago"
        return f"{m // 1440} d ago"

    @app.template_filter("hhmm")
    def hhmm(t):
        return t[:5] if t and t != "None" else "—"

    @app.template_filter("days")
    def days_filter(mask):
        if not mask or len(mask) != 7:
            return "Unknown"
        names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        if mask == "1111111":
            return "Daily"
        return " ".join(n for n, f in zip(names, mask) if f == "1")

    @app.context_processor
    def inject_globals():
        return {"stale_after": app.config["STALE_AFTER_MINUTES"], "age_minutes": age_minutes}
