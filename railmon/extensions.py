"""Shared extension instances."""
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.engine import Engine

from .cache import RedisCache

db = SQLAlchemy()
cache = RedisCache()


@event.listens_for(Engine, "connect")
def _sqlite_fk(dbapi_conn, _record):  # enforce FK/cascade semantics on SQLite (tests/dev)
    if dbapi_conn.__class__.__module__.startswith("sqlite3"):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()
