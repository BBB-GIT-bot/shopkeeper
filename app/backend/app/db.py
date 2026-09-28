from __future__ import annotations

import json
import re
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy.pool import NullPool, StaticPool

from .config import ROOT, database_url
from .schema import metadata

import os
DB_PATH = Path(os.getenv("SHOPKEEPER_DB", ROOT / "shopkeeper.db"))
_engines = {}
HEAD = "0003_proposal_snapshot"


def configured_url():
    return database_url(DB_PATH)


def get_engine():
    url = configured_url()
    if url not in _engines:
        if url.drivername == "sqlite" and url.database != ":memory:":
            Path(url.database).parent.mkdir(parents=True, exist_ok=True)
        options = {"pool_pre_ping": True, "hide_parameters": True}
        if url.drivername == "sqlite":
            options.update(connect_args={"check_same_thread": False, "timeout": 30}, poolclass=StaticPool if url.database == ':memory:' else NullPool)
        else:
            options.update(connect_args={"init_command": "SET time_zone='+00:00'", "connect_timeout": 5})
        engine = sa.create_engine(url, **options)
        if url.drivername == "sqlite":
            @sa.event.listens_for(engine, "connect")
            def foreign_keys(connection, record):
                connection.execute("PRAGMA foreign_keys=ON")
        _engines[url] = engine
    return _engines[url]


def dispose_engines():
    for engine in _engines.values():
        engine.dispose()
    _engines.clear()


class Record(dict):
    """Bridge old keyed/positional row access while routes migrate to Core."""
    def __getitem__(self, key):
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


def normalized_record(value):
    if value is None:
        return None
    item = Record(value._mapping)
    for key, val in item.items():
        if isinstance(val, datetime):
            item[key] = val.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
        elif isinstance(val, date):
            item[key] = val.isoformat()
        elif key.endswith("_at") and isinstance(val, str) and val:
            try:
                item[key] = datetime.fromisoformat(val.replace("Z", "+00:00")).replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
            except ValueError:
                pass
    return item


class Result:
    def __init__(self, result):
        self.result = result
        self.lastrowid = result.lastrowid
        self.rowcount = result.rowcount

    def fetchone(self):
        return normalized_record(self.result.fetchone())

    def fetchall(self):
        return [normalized_record(item) for item in self.result.fetchall()]

    def __iter__(self):
        return (normalized_record(item) for item in self.result)


class DatabaseValueError(ValueError):
    def __init__(self, field, reason):
        self.field = field
        self.reason = reason
        super().__init__(reason)


def money_value(value, field):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or abs(amount) >= Decimal('10000000000000000') or amount != amount.quantize(Decimal('0.01')):
            raise InvalidOperation
        return amount
    except (InvalidOperation, ValueError, TypeError):
        raise DatabaseValueError(field, '金额须为最多两位小数、范围内的有限十进制数') from None


def bound_type(column, value):
    if value is None:
        return value
    if isinstance(column.type, sa.JSON):
        return parse_json(value) if isinstance(value, str) else value
    if isinstance(column.type, sa.DateTime) and isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None) if value.endswith("Z") or "+" in value else datetime.fromisoformat(value)
    if isinstance(column.type, sa.Date) and isinstance(value, str):
        return date.fromisoformat(value)
    if isinstance(column.type, sa.Numeric):
        return money_value(value, column.name)
    return value


class Connection:
    def __init__(self, connection):
        self.raw = connection

    def execute(self, statement, values=()):
        if not isinstance(statement, str):
            return Result(self.raw.execute(statement, values or {}))
        # Only placeholders outside SQL literals become named bound parameters.
        parameters = {}
        tokens = re.compile(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|\?")
        def bind(match):
            if match.group() != "?":
                return match.group()
            index = len(parameters)
            parameters[f"p{index}"] = values[index]
            return f":p{index}"
        sql = tokens.sub(bind, statement)
        if len(parameters) != len(values):
            raise ValueError("SQL parameter count mismatch")
        types = {}
        inserted = re.match(r"INSERT INTO (\w+)\s*\(([^)]+)\)\s*VALUES\s*\(([^)]+)\)", sql, re.I)
        updated = re.match(r"UPDATE (\w+) SET (.+?) WHERE ", sql, re.I)
        if inserted:
            table = metadata.tables[inserted[1]]
            columns = [column.strip() for column in inserted[2].split(",")]
            for name, token in zip(columns, inserted[3].split(",")):
                key = token.strip().removeprefix(":")
                if key in parameters:
                    types[key] = table.c[name]
        elif updated:
            table = metadata.tables[updated[1]]
            for name, key in re.findall(r"(\w+)\s*=\s*:(p\d+)", updated[2]):
                types[key] = table.c[name]
        query = sa.text(sql)
        for key, column in types.items():
            parameters[key] = bound_type(column, parameters[key])
            query = query.bindparams(sa.bindparam(key, type_=column.type))
        return Result(self.raw.execute(query, parameters))

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()


@contextmanager
def connect():
    with get_engine().connect() as connection:
        yield Connection(connection)


@contextmanager
def transaction():
    with get_engine().begin() as connection:
        yield Connection(connection)


def row(value):
    return dict(value) if value is not None else None


def json_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=lambda val: str(val) if isinstance(val, Decimal) else val.isoformat())


def parse_json(value: Any, fallback=None):
    if value is None or value == "":
        return fallback
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def migration_config():
    config = Config(str(ROOT / "alembic.ini"))
    return config


def insert_if_absent(connection, table_name, values):
    table = metadata.tables[table_name]
    raw = connection.raw if isinstance(connection, Connection) else connection
    if raw.dialect.name == 'mysql':
        from sqlalchemy.dialects.mysql import insert
        statement = insert(table).values(**values)
        statement = statement.on_duplicate_key_update(id=table.c.id)
    else:
        from sqlalchemy.dialects.sqlite import insert
        statement = insert(table).values(**values).on_conflict_do_nothing()
    raw.execute(statement)


def is_duplicate(exc):
    if not isinstance(exc, sa.exc.IntegrityError):
        return False
    original = exc.orig
    return getattr(original, 'sqlite_errorname', '') in {'SQLITE_CONSTRAINT_UNIQUE', 'SQLITE_CONSTRAINT_PRIMARYKEY'} or 'UNIQUE constraint failed' in str(original) or (original.args and original.args[0] == 1062)


def upgrade_database():
    with get_engine().begin() as connection:
        config = migration_config()
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


def init_db():
    engine = get_engine()
    if engine.dialect.name == "sqlite":
        # Existing classroom SQLite files keep their values and legacy columns.
        # New files get the typed schema. SQLite remains a test/compatibility mode.
        inspector = sa.inspect(engine)
        if inspector.has_table("service_configs") and not inspector.has_table("alembic_version"):
            from . import legacy_sqlite
            legacy_sqlite.DB_PATH = Path(engine.url.database)
            legacy_sqlite.init_db()
        upgrade_database()
    else:
        with engine.connect() as connection:
            revision = MigrationContext.configure(connection).get_current_revision()
        if revision != HEAD:
            raise RuntimeError("MySQL schema is not at head; run python -m alembic upgrade head before startup")
    with engine.begin() as connection:
        for name in ("service_configs", "queue_configs"):
            table = metadata.tables[name]
            insert_if_absent(connection, name, {'id': 1})
