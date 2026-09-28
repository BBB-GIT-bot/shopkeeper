from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.getenv("SHOPKEEPER_DB", ROOT / "shopkeeper.db"))


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    connection = connect()
    try:
        connection.execute("BEGIN")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def row(value: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(value) if value else None


def json_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def parse_json(value: str | None, fallback: Any = None) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def init_db() -> None:
    schema = """
    CREATE TABLE IF NOT EXISTS users (
      id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
      password_hash TEXT NOT NULL, role TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS service_configs (
      id INTEGER PRIMARY KEY CHECK(id=1), base_url TEXT DEFAULT '', model TEXT DEFAULT '',
      api_key TEXT DEFAULT '', api_key_ciphertext TEXT DEFAULT '', temperature REAL DEFAULT 0.2, request_timeout_seconds INTEGER DEFAULT 60,
      multimodal_mode TEXT DEFAULT 'simulation', updated_by INTEGER, updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS queue_configs (
      id INTEGER PRIMARY KEY CHECK(id=1), concurrency INTEGER DEFAULT 2, execution_timeout_seconds INTEGER DEFAULT 60,
      max_auto_retries INTEGER DEFAULT 2, poll_interval_seconds INTEGER DEFAULT 2,
      heartbeat_seconds INTEGER DEFAULT 10, heartbeat_interval_seconds INTEGER DEFAULT 10,
      dispatch_interval_seconds INTEGER DEFAULT 1, updated_by INTEGER, updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS shops (
      id INTEGER PRIMARY KEY AUTOINCREMENT, shop_code TEXT UNIQUE NOT NULL, platform TEXT NOT NULL,
      name TEXT NOT NULL, platform_shop_id TEXT NOT NULL, authorization_status TEXT DEFAULT 'not_configured'
    );
    CREATE TABLE IF NOT EXISTS products (
      id INTEGER PRIMARY KEY AUTOINCREMENT, product_code TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
      category TEXT NOT NULL, base_price REAL NOT NULL, attributes TEXT DEFAULT '{}', main_sku_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS skus (
      id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL REFERENCES products(id),
      sku_code TEXT NOT NULL, attributes TEXT DEFAULT '{}', UNIQUE(product_id, sku_code)
    );
    CREATE TABLE IF NOT EXISTS listings (
      id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL REFERENCES products(id),
      shop_id INTEGER NOT NULL REFERENCES shops(id), platform_product_id TEXT NOT NULL, on_sale INTEGER DEFAULT 1,
      UNIQUE(product_id, shop_id)
    );
    CREATE TABLE IF NOT EXISTS sku_mappings (
      id INTEGER PRIMARY KEY AUTOINCREMENT, listing_id INTEGER NOT NULL REFERENCES listings(id),
      sku_id INTEGER NOT NULL REFERENCES skus(id), platform_sku_id TEXT NOT NULL, sale_price REAL NOT NULL,
      stock_quantity INTEGER NOT NULL, warning_threshold INTEGER NOT NULL DEFAULT 0,
      UNIQUE(listing_id, sku_id)
    );
    CREATE TABLE IF NOT EXISTS stock_movements (
      id INTEGER PRIMARY KEY AUTOINCREMENT, mapping_id INTEGER NOT NULL REFERENCES sku_mappings(id),
      before_quantity INTEGER NOT NULL, after_quantity INTEGER NOT NULL, delta INTEGER NOT NULL,
      reason TEXT NOT NULL, source TEXT NOT NULL, actor_id INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS competitors (
      id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL REFERENCES products(id),
      competitor_code TEXT NOT NULL, name TEXT NOT NULL, price REAL NOT NULL, platform TEXT DEFAULT '',
      attributes TEXT DEFAULT '{}', selling_points TEXT DEFAULT '[]', source_url TEXT DEFAULT '', source_text TEXT DEFAULT '',
      verification_status TEXT DEFAULT 'unverified', UNIQUE(product_id, competitor_code)
    );
    CREATE TABLE IF NOT EXISTS rounds (
      id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL REFERENCES products(id), round_no INTEGER NOT NULL,
      phase TEXT DEFAULT 'setup', source_report_id INTEGER, selected_actions TEXT DEFAULT '[]',
      UNIQUE(product_id, round_no)
    );
    CREATE TABLE IF NOT EXISTS contents (
      id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL REFERENCES products(id), round_id INTEGER NOT NULL REFERENCES rounds(id),
      kind TEXT NOT NULL, version INTEGER NOT NULL, status TEXT DEFAULT 'draft', content TEXT NOT NULL,
      edited_content TEXT, source_content_id INTEGER, confirmed_by INTEGER, confirmed_at TEXT, archived_at TEXT,
      UNIQUE(round_id, kind, version)
    );
    CREATE TABLE IF NOT EXISTS tasks (
      id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL, round_id INTEGER NOT NULL,
      creative_content_id INTEGER NOT NULL, selection_id TEXT NOT NULL, task_type TEXT NOT NULL,
      status TEXT DEFAULT 'pending', scenario TEXT DEFAULT 'success', attempts INTEGER DEFAULT 0,
      max_auto_retries INTEGER DEFAULT 2, poll_interval_seconds INTEGER DEFAULT 2,
      original_task_id INTEGER, material_id INTEGER, cancel_reason TEXT, cancelled_at TEXT,
      queued_at TEXT DEFAULT CURRENT_TIMESTAMP, started_at TEXT, heartbeat_at TEXT, deadline_at TEXT,
      finished_at TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS task_attempts (
      id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL REFERENCES tasks(id),
      attempt_no INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'running', scenario TEXT NOT NULL,
      queued_at TEXT DEFAULT CURRENT_TIMESTAMP, started_at TEXT, heartbeat_at TEXT, finished_at TEXT,
      deadline_at TEXT, error_reason TEXT DEFAULT '', process_id INTEGER,
      UNIQUE(task_id, attempt_no)
    );
    CREATE TABLE IF NOT EXISTS materials (
      id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL, product_id INTEGER NOT NULL, round_id INTEGER NOT NULL,
      material_type TEXT NOT NULL, selection_id TEXT NOT NULL, file_url TEXT NOT NULL, is_demo INTEGER DEFAULT 1,
      review_status TEXT DEFAULT 'pending', tags TEXT DEFAULT '[]', archived_at TEXT, reviewed_by INTEGER, reviewed_at TEXT
    );
    CREATE TABLE IF NOT EXISTS proposals (
      id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL, round_id INTEGER NOT NULL, shop_id INTEGER NOT NULL,
      proposal_key TEXT NOT NULL, version INTEGER NOT NULL, status TEXT DEFAULT 'draft', strategy TEXT NOT NULL,
      groups_data TEXT NOT NULL, targets TEXT NOT NULL, material_ids TEXT NOT NULL, date_from TEXT NOT NULL, date_to TEXT NOT NULL,
      content_confirmed_by INTEGER, content_confirmed_at TEXT, archived_at TEXT,
      UNIQUE(proposal_key, version)
    );
    CREATE TABLE IF NOT EXISTS approvals (
      id INTEGER PRIMARY KEY AUTOINCREMENT, proposal_id INTEGER NOT NULL, decision TEXT NOT NULL,
      comment TEXT DEFAULT '', actor_id INTEGER NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS experiments (
      id INTEGER PRIMARY KEY AUTOINCREMENT, proposal_id INTEGER UNIQUE NOT NULL, shop_id INTEGER NOT NULL,
      product_id INTEGER NOT NULL, date_from TEXT NOT NULL, date_to TEXT NOT NULL, targets TEXT NOT NULL,
      execution_note TEXT DEFAULT '', status TEXT DEFAULT 'approved'
    );
    CREATE TABLE IF NOT EXISTS metrics (
      id INTEGER PRIMARY KEY AUTOINCREMENT, experiment_id INTEGER NOT NULL, date TEXT NOT NULL, group_code TEXT NOT NULL,
      impressions INTEGER NOT NULL, clicks INTEGER NOT NULL, orders INTEGER NOT NULL, gmv REAL NOT NULL, ad_spend REAL NOT NULL,
      detail_visits INTEGER, detail_bounces INTEGER,
      UNIQUE(experiment_id, date, group_code)
    );
    CREATE TABLE IF NOT EXISTS reviews (
      id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL, round_id INTEGER NOT NULL, experiment_id INTEGER NOT NULL,
      version INTEGER NOT NULL, status TEXT DEFAULT 'draft', sections TEXT NOT NULL, actions TEXT NOT NULL,
      snapshot TEXT NOT NULL, confirmed_at TEXT, archived_at TEXT, UNIQUE(round_id, version)
    );
    CREATE TABLE IF NOT EXISTS imports (
      id INTEGER PRIMARY KEY AUTOINCREMENT, import_type TEXT NOT NULL, filename TEXT NOT NULL,
      status TEXT DEFAULT 'previewed', rows_data TEXT NOT NULL, summary TEXT NOT NULL,
      field_mapping TEXT DEFAULT '{}', sheet_name TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """
    with connect() as connection:
        connection.executescript(schema)
        connection.execute("INSERT OR IGNORE INTO service_configs(id) VALUES (1)")
        connection.execute("INSERT OR IGNORE INTO queue_configs(id) VALUES (1)")
        for table, column, definition in (
            ("service_configs", "api_key_ciphertext", "TEXT DEFAULT ''"),
            ("queue_configs", "dispatch_interval_seconds", "INTEGER DEFAULT 1"),
            ("queue_configs", "heartbeat_interval_seconds", "INTEGER DEFAULT 10"),
        ):
            columns = {item["name"] for item in connection.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                if column == "heartbeat_interval_seconds":
                    connection.execute("UPDATE queue_configs SET heartbeat_interval_seconds=heartbeat_seconds")
        # Keep existing demo databases usable after schema additions.
        for statement in (
            "ALTER TABLE tasks ADD COLUMN cancelled_at TEXT",
            "ALTER TABLE tasks ADD COLUMN queued_at TEXT",
            "ALTER TABLE tasks ADD COLUMN started_at TEXT",
            "ALTER TABLE tasks ADD COLUMN heartbeat_at TEXT",
            "ALTER TABLE tasks ADD COLUMN deadline_at TEXT",
            "ALTER TABLE tasks ADD COLUMN finished_at TEXT",
            "ALTER TABLE imports ADD COLUMN field_mapping TEXT DEFAULT '{}'",
            "ALTER TABLE imports ADD COLUMN sheet_name TEXT",
        ):
            try:
                connection.execute(statement)
            except sqlite3.OperationalError:
                pass
