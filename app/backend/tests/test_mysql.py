"""Only runs against a newly created, explicitly named MySQL test database."""
import os
import subprocess
import sys
from decimal import Decimal
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url

from app import db
from app.main import app

TEST_URL = os.getenv('SHOPKEEPER_MYSQL_TEST_URL')
pytestmark = pytest.mark.skipif(not TEST_URL, reason='No isolated MySQL test URL supplied')


@pytest.fixture(scope='module')
def client():
    url = make_url(TEST_URL)
    if url.drivername != 'mysql+pymysql' or not url.database.startswith('shopkeeper_test_'):
        pytest.fail('Integration tests require a dedicated shopkeeper_test_* database')
    with patch.dict(os.environ, {'DATABASE_URL': TEST_URL, 'CONFIG_ENCRYPTION_KEY': Fernet.generate_key().decode()}):
        if sa.inspect(db.get_engine()).get_table_names():
            pytest.fail('MySQL integration validation requires a fresh empty test database')
        with pytest.raises(RuntimeError, match='schema is not at head'):
            db.init_db()
        assert sa.inspect(db.get_engine()).get_table_names() == []
        hidden = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        for module, args in (('alembic', ['upgrade', 'head']), ('app.scripts.seed', [])):
            completed = subprocess.run([sys.executable, '-m', module, *args], cwd=db.ROOT, capture_output=True, text=True, creationflags=hidden)
            assert completed.returncode == 0, completed.stderr
        with TestClient(app) as value:
            yield value
        db.dispose_engines()


def auth(client, username='operator'):
    result = client.post('/api/auth/login', json={'username': username, 'password': username + '123'})
    assert result.status_code == 200, result.text
    return {'Authorization': 'Bearer ' + result.json()['access_token']}


def test_migration_and_native_types(client):
    assert db.get_engine().dialect.name == 'mysql'
    inspector = sa.inspect(db.get_engine())
    columns = {col['name']: col['type'] for col in inspector.get_columns('products')}
    assert columns['base_price'].precision == 18 and columns['base_price'].scale == 2
    assert isinstance(columns['attributes'], sa.JSON)
    assert inspector.get_foreign_keys('sku_mappings')
    assert inspector.get_unique_constraints('metrics')
    with db.connect() as connection:
        assert connection.execute('SELECT version_num FROM alembic_version').fetchone()[0] == db.HEAD


def test_queue_save_restart_preserves_values_and_roles(client):
    manager = auth(client, 'manager')
    assert client.patch('/api/settings/queue', headers=manager, json={'max_auto_retries': 0}).status_code == 200
    with TestClient(app) as restarted:
        assert restarted.get('/api/settings/queue', headers=auth(restarted, 'manager')).json()['max_auto_retries'] == 0
    assert client.get('/api/settings/services', headers=auth(client)).status_code == 403
    services = client.get('/api/settings/services', headers=manager).json()
    assert services['api_key_configured'] is False
    assert 'api_key' not in services and 'api_key_ciphertext' not in services


def test_catalog_unique_inventory_and_atomic_rollback(client):
    operator = auth(client)
    shop_payload = {'shop_code': 'MYSQL-DEMO', 'platform': 'demo', 'name': '独立数据库教学店铺', 'platform_shop_id': 'demo'}
    shop = client.post('/api/shops', headers=operator, json=shop_payload).json()
    assert client.post('/api/shops', headers=operator, json=shop_payload).status_code == 409
    product = client.post('/api/products', headers=operator, json={'product_code': 'MYSQL-SUN', 'name': '防晒衣', 'category': '服装', 'base_price': '299.01', 'attributes': {'颜色': '蓝'}}).json()
    assert product['base_price'] == '299.01'
    invalid = client.post('/api/products', headers=operator, json={'product_code': 'INVALID-MONEY', 'name': '错误金额', 'category': '服装', 'base_price': '299.015'})
    assert invalid.status_code == 422
    sku = client.post(f"/api/products/{product['id']}/skus", headers=operator, json={'sku_code': 'BLUE-S', 'attributes': {'size': 'S'}}).json()
    listing = client.post(f"/api/products/{product['id']}/listings", headers=operator, json={'shop_id': shop['id'], 'platform_product_id': 'SUN'}).json()
    mapping = client.post(f"/api/listings/{listing['id']}/sku-mappings", headers=operator, json={'sku_id': sku['id'], 'platform_sku_id': 'SUN-BLUE-S', 'sale_price': '299.01', 'stock_quantity': 8, 'warning_threshold': 10}).json()
    changed = client.post(f"/api/sku-mappings/{mapping['id']}/stock-adjustments", headers=operator, json={'stock_quantity': 6, 'reason': '数据库验证'})
    assert changed.status_code == 200, changed.text
    assert changed.json()['stock_movement']['delta'] == -2
    with db.connect() as connection:
        assert connection.execute('SELECT base_price FROM products WHERE id=?', (product['id'],)).fetchone()[0] == Decimal('299.01')
        assert db.parse_json(connection.execute('SELECT attributes FROM products WHERE id=?', (product['id'],)).fetchone()[0]) == {'颜色': '蓝'}
        movements = connection.execute('SELECT COUNT(*) FROM stock_movements WHERE mapping_id=?', (mapping['id'],)).fetchone()[0]
    with pytest.raises(RuntimeError):
        with db.transaction() as connection:
            connection.execute('UPDATE sku_mappings SET stock_quantity=? WHERE id=?', (1, mapping['id']))
            connection.execute('INSERT INTO stock_movements(mapping_id,before_quantity,after_quantity,delta,reason,source) VALUES (?,?,?,?,?,?)', (mapping['id'], 6, 1, -5, 'rollback', 'manual'))
            raise RuntimeError('force rollback')
    with db.connect() as connection:
        assert connection.execute('SELECT stock_quantity FROM sku_mappings WHERE id=?', (mapping['id'],)).fetchone()[0] == 6
        assert connection.execute('SELECT COUNT(*) FROM stock_movements WHERE mapping_id=?', (mapping['id'],)).fetchone()[0] == movements


def test_foreign_keys_and_utc_persist_on_mysql(client):
    with pytest.raises(sa.exc.IntegrityError):
        with db.transaction() as connection:
            connection.execute('INSERT INTO skus(product_id,sku_code) VALUES (?,?)', (9999999, 'wrong-parent'))
    with db.transaction() as connection:
        connection.execute('UPDATE service_configs SET updated_at=? WHERE id=?', ('2026-09-28T01:02:03Z', 1))
    with db.connect() as connection:
        assert connection.execute('SELECT updated_at FROM service_configs').fetchone()[0] == '2026-09-28T01:02:03Z'


def test_demo_content_versions_keep_json_and_prerequisites(client):
    operator = auth(client)
    with db.connect() as connection:
        product_id = connection.execute("SELECT id FROM products WHERE product_code='MYSQL-SUN'").fetchone()[0]
    competitor = client.post(f'/api/products/{product_id}/competitors', headers=operator, json={'competitor_code': 'MYSQL-COMP', 'name': '教学竞品', 'price': '269.00'}).json()
    client.patch(f"/api/competitors/{competitor['id']}", headers=operator, json={'verification_status': 'verified'})
    round_item = client.post(f'/api/products/{product_id}/rounds', headers=operator).json()
    diagnosis = client.post(f"/api/rounds/{round_item['id']}/diagnoses/generate", headers=operator, json={'demo_mode': True})
    assert diagnosis.status_code == 201, diagnosis.text
    content = diagnosis.json()
    assert content['content']['generation_mode'] == 'demo'
    creative_path = f"/api/rounds/{round_item['id']}/creatives/generate"
    assert client.post(creative_path, headers=operator, json={'diagnosis_content_id': content['id']}).status_code == 409
    assert client.post(f"/api/contents/{content['id']}/confirm", headers=operator, json={'expected_version': 1}).status_code == 200
    creative = client.post(creative_path, headers=operator, json={'diagnosis_content_id': content['id'], 'demo_mode': True})
    assert creative.status_code == 201, creative.text
    assert len(creative.json()['content']['image_directions']) >= 3

def test_full_classroom_mysql(client):
    from app.scripts.demo import create_demo
    result=create_demo(client)
    assert result['status']=='passed'
    assert result['metrics']['ctr']==.025 and result['metrics']['detail_bounce_rate']==.76
