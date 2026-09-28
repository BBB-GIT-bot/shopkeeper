import os
from fastapi.testclient import TestClient
import pytest
from unittest.mock import patch
from app import db
from app.main import app


client = TestClient(app)


@pytest.fixture(scope='module', autouse=True)
def isolated_database(tmp_path_factory):
    global client
    path = tmp_path_factory.mktemp('catalog-api') / 'test.db'
    with patch.dict(os.environ, {'DATABASE_URL': 'sqlite:///' + path.as_posix()}):
        with TestClient(app) as scoped_client:
            client = scoped_client
            yield
        db.dispose_engines()


def token(username="operator", password=None):
    response = client.post("/api/auth/login", json={"username": username, "password": password or ("operator123" if username == "operator" else "manager123")})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def headers(value):
    return {"Authorization": f"Bearer {value}"}


def test_p1_health_login_and_roles():
    assert client.get("/api/health").json()["status"] == "ok"
    operator = token()
    manager = token("manager")
    assert client.get("/api/auth/me", headers=headers(operator)).json()["role"] == "operator"
    assert client.get("/api/settings/queue", headers=headers(operator)).status_code == 403
    assert client.get("/api/settings/queue", headers=headers(manager)).status_code == 200


def test_p2_catalog_inventory_and_competitor():
    auth = headers(token())
    shop_a = client.post("/api/shops", headers=auth, json={"shop_code": "SHOP-A", "platform": "demo", "name": "演示店 A", "platform_shop_id": "A"}).json()
    shop_b = client.post("/api/shops", headers=auth, json={"shop_code": "SHOP-B", "platform": "demo", "name": "演示店 B", "platform_shop_id": "B"}).json()
    product = client.post("/api/products", headers=auth, json={"product_code": "SUN-001", "name": "夏季凉感防晒衣", "category": "服装", "base_price": 299}).json()
    skus = []
    for code, attrs in [("BLUE-S", {"color": "蓝", "size": "S"}), ("BLUE-M", {"color": "蓝", "size": "M"}), ("GRAY-S", {"color": "灰", "size": "S"}), ("GRAY-M", {"color": "灰", "size": "M"})]:
        skus.append(client.post(f"/api/products/{product['id']}/skus", headers=auth, json={"sku_code": code, "attributes": attrs}).json())
    listing = client.post(f"/api/products/{product['id']}/listings", headers=auth, json={"shop_id": shop_a["id"], "platform_product_id": "A-SUN-001"}).json()
    mapping = client.post(f"/api/listings/{listing['id']}/sku-mappings", headers=auth, json={"sku_id": skus[0]["id"], "platform_sku_id": "A-BLUE-S", "sale_price": 299, "stock_quantity": 8, "warning_threshold": 10}).json()
    adjustment = client.post(f"/api/sku-mappings/{mapping['id']}/stock-adjustments", headers=auth, json={"stock_quantity": 6, "reason": "课堂补录"})
    assert adjustment.status_code == 200 and adjustment.json()["stock_movement"]["delta"] == -2
    competitor = client.post(f"/api/products/{product['id']}/competitors", headers=auth, json={"competitor_code": "COMP-1", "name": "竞品防晒衣", "price": 269, "selling_points": ["透气"], "source_url": "https://example.invalid/demo"}).json()
    assert client.patch(f"/api/competitors/{competitor['id']}", headers=auth, json={"verification_status": "verified"}).status_code == 200
    round_item = client.post(f"/api/products/{product['id']}/rounds", headers=auth).json()
    assert round_item["round_no"] == 1
    assert shop_b["shop_code"] == "SHOP-B"


def test_p3_content_prerequisite_and_versions():
    auth = headers(token())
    product = client.get("/api/products", headers=auth).json()["items"][0]
    rounds = client.get(f"/api/products/{product['id']}/rounds", headers=auth).json()["items"]
    assert rounds
    diagnosis = client.post(f"/api/rounds/{rounds[0]['id']}/diagnoses/generate", headers=auth, json={'demo_mode': True})
    assert diagnosis.status_code == 201, diagnosis.text
    diagnosis_id = diagnosis.json()["id"]
    assert client.post(f"/api/rounds/{rounds[0]['id']}/creatives/generate", headers=auth, json={"diagnosis_content_id": diagnosis_id}).status_code == 409
    assert client.post(f"/api/contents/{diagnosis_id}/confirm", headers=auth, json={"expected_version": 1}).status_code == 200
    creative = client.post(f"/api/rounds/{rounds[0]['id']}/creatives/generate", headers=auth, json={"diagnosis_content_id": diagnosis_id, 'demo_mode': True})
    assert creative.status_code == 201

