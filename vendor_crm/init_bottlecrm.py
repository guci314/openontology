"""Initialize a REAL BottleCRM SQLite database from the downloaded django-crm source.

We import BottleCRM's actual Django apps (accounts / contacts / opportunity /
cases) and let Django's migration engine create its real tables. No hand-written
CREATE TABLE: the schema comes from BottleCRM's own migrations.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "bottlecrm_src"
DB = HERE / "bottlecrm.sqlite3"

sys.path.insert(0, str(SRC))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "bottlecrm_settings")

import django  # noqa: E402
from django.conf import settings  # noqa: E402

if not settings.configured:
    django.setup()

from django.core.management import call_command  # noqa: E402

if __name__ == "__main__":
    if DB.exists():
        DB.unlink()
    call_command("migrate", verbosity=0, interactive=False)
    print("bottlecrm.sqlite3 created")
