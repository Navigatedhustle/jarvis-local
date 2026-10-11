import os
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from jarvis.config import Settings
from jarvis.db import Database


@pytest.fixture
def db():
    url = os.environ.get("JARVIS_TEST_DATABASE_URL") or Settings().database_url
    if not url:
        pytest.skip("PostgreSQL integration test requires JARVIS_TEST_DATABASE_URL or runtime.env")
    name = "test_" + uuid4().hex
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
    database = Database(url, options=f"-c search_path={name},public")
    database.migrate()
    try:
        yield database
    finally:
        database.close()
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))
