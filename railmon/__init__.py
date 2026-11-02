"""Indian Railway Intelligence & Monitoring System (RailMon)."""
from __future__ import annotations

import logging

from flask import Flask

from .config import Config

__version__ = "1.0.0"


def create_app(config_object: type[Config] | None = None) -> Flask:
    app = Flask(__name__, template_folder="web/templates", static_folder="web/static")
    app.config.from_object(config_object or Config)

    if not app.config.get("SECRET_KEY"):
        raise RuntimeError("SECRET_KEY must be set (see .env.example)")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from .extensions import cache, db

    db.init_app(app)
    cache.init_app(app)

    from .security import crypto

    crypto.init_app(app)

    import importlib

    for module in app.config.get("ADAPTER_MODULES", []):
        importlib.import_module(module)  # registers third-party adapters

    from .web import register_web

    register_web(app)

    from .cli import register_cli

    register_cli(app)
    return app
