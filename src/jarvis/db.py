from contextlib import contextmanager
from importlib.resources import files

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool


class Database:
    def __init__(self, url: str, **kwargs):
        if not url:
            raise RuntimeError("Database is not configured")
        self.pool = ConnectionPool(
            url, min_size=1, max_size=8, open=True, kwargs={"row_factory": dict_row, **kwargs}
        )

    def migrate(self):
        with self.pool.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(71008100)")
            conn.execute(files("jarvis").joinpath("schema.sql").read_text())
            conn.execute(files("jarvis").joinpath("consolidation.sql").read_text())
            conn.execute(files("jarvis").joinpath("graph.sql").read_text())
            conn.execute(files("jarvis").joinpath("memory_views.sql").read_text())

    @contextmanager
    def transaction(self):
        with self.pool.connection() as conn:
            with conn.transaction():
                yield conn

    def query(self, sql, params=()):
        with self.pool.connection() as conn:
            return conn.execute(sql, params).fetchall()

    def one(self, sql, params=()):
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql, params=()):
        with self.pool.connection() as conn:
            return conn.execute(sql, params).rowcount

    def audit(self, action, entity_id=None, details=None, actor="jarvis", source="internal", conn=None):
        sql = "INSERT INTO audit(actor,source,action,entity_id,details) VALUES (%s,%s,%s,%s,%s)"
        params = (
            actor,
            source,
            action,
            str(entity_id) if entity_id is not None else None,
            Jsonb(details or {}),
        )
        if conn:
            conn.execute(sql, params)
        else:
            self.execute(sql, params)

    def get_state(self, key, default=None):
        row = self.one("SELECT value FROM state WHERE key=%s", (key,))
        return row["value"] if row else default

    def set_state(self, key, value, conn=None):
        sql = """INSERT INTO state(key,value) VALUES (%s,%s)
                 ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=now()"""
        if conn:
            conn.execute(sql, (key, Jsonb(value)))
        else:
            self.execute(sql, (key, Jsonb(value)))

    def close(self):
        self.pool.close()
