"""Resolve configuration relative to the backend, shared by API and worker."""
import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.engine import URL, make_url

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env', override=False)


def database_url(sqlite_path: Path) -> URL:
    configured = os.getenv('DATABASE_URL')
    if not configured:
        return URL.create('sqlite', database=str((ROOT / sqlite_path).resolve()))
    value = make_url(configured)
    if value.drivername not in {'sqlite', 'mysql+pymysql'}:
        raise ValueError('DATABASE_URL 仅支持 mysql+pymysql 或测试用 sqlite')
    if value.drivername == 'sqlite' and value.database != ':memory:':
        value = value.set(database=str((ROOT / (value.database or 'shopkeeper.db')).resolve()))
    return value


def storage_root() -> Path:
    return (ROOT / os.getenv('STORAGE_ROOT', 'storage')).resolve()
