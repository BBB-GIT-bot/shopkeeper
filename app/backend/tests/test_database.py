import os
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from sqlalchemy.schema import CreateTable
from sqlalchemy.dialects import mysql

from app import db
from app.schema_v1 import metadata
from app.main import metric_values


@pytest.fixture
def database(tmp_path):
    with patch.dict(os.environ, {'DATABASE_URL': 'sqlite:///' + (tmp_path / 'database.db').as_posix()}):
        db.init_db()
        yield db.get_engine()
        db.dispose_engines()


def test_defaults_and_migrations_repeat_without_overwriting(database):
    with db.transaction() as connection:
        connection.execute('UPDATE queue_configs SET concurrency=? WHERE id=?', (3, 1))
    db.init_db()
    with db.connect() as connection:
        assert connection.execute('SELECT version_num FROM alembic_version').fetchone()[0] == db.HEAD
        assert connection.execute('SELECT concurrency FROM queue_configs').fetchone()[0] == 3
        service = connection.execute('SELECT * FROM service_configs').fetchone()
        assert service['api_key'] == service['api_key_ciphertext'] == ''


def test_transaction_rolls_back_all_changes(database):
    with pytest.raises(RuntimeError):
        with db.transaction() as connection:
            connection.execute('UPDATE queue_configs SET concurrency=? WHERE id=?', (5, 1))
            raise RuntimeError('rollback check')
    with db.connect() as connection:
        assert connection.execute('SELECT concurrency FROM queue_configs').fetchone()[0] == 2


def test_foreign_key_and_duplicate_enforcement(database):
    with pytest.raises(sa.exc.IntegrityError):
        with db.transaction() as connection:
            connection.execute('INSERT INTO skus(product_id,sku_code) VALUES (?,?)', (999999, 'missing'))
    with db.transaction() as connection:
        connection.execute('INSERT INTO shops(shop_code,platform,name,platform_shop_id) VALUES (?,?,?,?)', ('A', 'demo', '教学店铺', 'id'))
    with pytest.raises(sa.exc.IntegrityError) as caught:
        with db.transaction() as connection:
            connection.execute('INSERT INTO shops(shop_code,platform,name,platform_shop_id) VALUES (?,?,?,?)', ('A', 'demo', '第二店铺', 'id'))
    assert db.is_duplicate(caught.value)


def test_json_utc_dates_and_decimal_storage(database):
    with db.transaction() as connection:
        inserted = connection.execute('INSERT INTO products(product_code,name,category,base_price,attributes) VALUES (?,?,?,?,?)', ('P', '商品', '服装', '299.01', db.json_value({'颜色': ['蓝', '灰']})))
        product_id = inserted.lastrowid
        connection.execute('UPDATE service_configs SET updated_at=? WHERE id=?', ('2026-09-28T01:02:03Z', 1))
    with db.connect() as connection:
        product = connection.execute('SELECT * FROM products WHERE id=?', (product_id,)).fetchone()
        assert db.parse_json(product['attributes']) == {'颜色': ['蓝', '灰']}
        assert Decimal(str(product['base_price'])) == Decimal('299.01')
        assert connection.execute('SELECT updated_at FROM service_configs').fetchone()[0] == '2026-09-28T01:02:03Z'


def test_bound_values_cannot_become_sql(database):
    unusual = "name' OR 1=1 -- ?"
    with db.transaction() as connection:
        connection.execute('UPDATE service_configs SET model=? WHERE id=?', (unusual, 1))
    with db.connect() as connection:
        assert connection.execute('SELECT model FROM service_configs WHERE id=?', (1,)).fetchone()[0] == unusual
        assert connection.execute("SELECT '?' AS literal,model FROM service_configs WHERE id=?", (1,)).fetchone()[0] == '?'


def test_connections_return_to_pool(database):
    for _ in range(5):
        with db.connect() as connection:
            connection.execute('SELECT 1').fetchone()
    assert not hasattr(database.pool, 'checkedout') or database.pool.checkedout() == 0


def test_mysql_ddl_compiles_with_contract_types():
    statements = [str(CreateTable(table).compile(dialect=mysql.dialect())) for table in metadata.sorted_tables]
    combined = '\n'.join(statements)
    assert 'DECIMAL(18, 2)' in combined
    assert 'DATETIME(6)' in combined
    assert 'utf8mb4_bin' in combined
    assert 'JSON' in combined
    assert 'AUTOINCREMENT' not in combined


def test_configured_mysql_does_not_fall_back_to_sqlite():
    with patch.dict(os.environ, {'DATABASE_URL': 'mysql+pymysql://test@127.0.0.1:1/unavailable'}):
        assert db.get_engine().dialect.name == 'mysql'
        with pytest.raises(sa.exc.OperationalError):
            with db.connect() as connection:
                connection.execute('SELECT 1')
        assert db.get_engine().dialect.name == 'mysql'
        db.dispose_engines()


@pytest.mark.parametrize('value', ['299.015', 'NaN', 'Infinity', '10000000000000000', 'invalid', True])
def test_money_precision_rejected_before_database_write(database, value):
    with pytest.raises(db.DatabaseValueError):
        with db.transaction() as connection:
            connection.execute('INSERT INTO products(product_code,name,category,base_price) VALUES (?,?,?,?)', ('INVALID', 'invalid', 'demo', value))
    with db.connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM products').fetchone()[0] == 0


def test_money_aggregation_uses_decimal_and_zero_denominators():
    result = metric_values([
        {'impressions': 100, 'clicks': 2, 'orders': 1, 'gmv': '0.10', 'ad_spend': '0.01'},
        {'impressions': 900, 'clicks': 9, 'orders': 1, 'gmv': '0.20', 'ad_spend': '0.02'},
    ])
    assert result['totals']['gmv'] == '0.30'
    assert result['totals']['ad_spend'] == '0.03'
    assert result['metrics']['ctr'] == 0.011
    assert result['metrics']['roi'] == 10
    assert result['metrics']['detail_bounce_rate'] is None
    assert metric_values([])['metrics']['roi'] is None
