"""Regenerate sql/schema.sql without a configured environment:

    python -m railmon.dump_schema > sql/schema.sql
"""
import sys

from . import create_app
from .cli import render_mysql_schema
from .config import TestConfig

if __name__ == "__main__":
    with create_app(TestConfig).app_context():
        sys.stdout.write(render_mysql_schema())
