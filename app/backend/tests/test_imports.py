import io
import os
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook
from app import db, imports
from app.main import app

@pytest.fixture
def client(tmp_path):
    with patch.dict(os.environ, {'DATABASE_URL':'sqlite:///'+(tmp_path/'imports.db').as_posix()}):
        with TestClient(app) as client:
            token=client.post('/api/auth/login',json={'username':'operator','password':'operator123'}).json()['access_token']
            client.headers['Authorization']='Bearer '+token
            yield client
        db.dispose_engines()

def preview(client, text, kind='products', mapping=None):
    result=client.post('/api/imports/preview',data={'import_type':kind,'field_mapping':db.json_value(mapping or {})},files={'file':('test.csv',text.encode('utf-8'),'text/csv')})
    assert result.status_code==201,result.text
    return result.json()

def test_partial_success_duplicates_mapping_and_replay(client):
    batch=preview(client,'编码,名称,类别,售价\nA,商品 A,服装,299.01\nB,错误,服装,-2\nA,重复,服装,199\nC,商品 C,服装,100\n',mapping={'编码':'product_code','名称':'name','类别':'category','售价':'base_price'})
    assert batch['summary']['invalid']==1 and batch['summary']['duplicate']==2
    assert client.post(f"/api/imports/{batch['id']}/commit",json={}).status_code==422
    result=client.post(f"/api/imports/{batch['id']}/commit",json={'row_resolutions':[{'row_number':2,'action':'keep'},{'row_number':4,'action':'skip'}]})
    assert result.status_code==200,result.text
    assert result.json()['summary']['created']==2 and result.json()['summary']['failed']==1
    assert client.post(f"/api/imports/{batch['id']}/commit",json={}).status_code==409
    products=client.get('/api/products').json()['items']
    assert len(products)==2 and next(x for x in products if x['product_code']=='A')['base_price']=='299.01'
    batch=preview(client,'product_code,name,category,base_price\nA,更新,服装,299.02\n')
    result=client.post(f"/api/imports/{batch['id']}/commit",json={'row_resolutions':{'2':'update'}})
    assert result.json()['summary']['updated']==1

def test_excel_templates_and_malformed_numbers(client):
    for kind in imports.FIELDS:
        response=client.get(f'/api/imports/templates/{kind}?format=xlsx')
        assert response.status_code==200
        workbook=load_workbook(io.BytesIO(response.content)); assert list(next(workbook.active.values))==imports.FIELDS[kind]
    book=Workbook(); book.active.append(['product_code','name','category','base_price']); book.active.append(['X','Excel商品','服装',299.01]); book.active.append(['Y','错误','服装','NaN']); stream=io.BytesIO(); book.save(stream)
    result=client.post('/api/imports/preview',data={'import_type':'products'},files={'file':('data.xlsx',stream.getvalue())})
    assert result.status_code==201 and result.json()['summary']['invalid']==1
    result=client.post('/api/imports/preview',data={'import_type':'products'},files={'file':('bad.xlsx',b'not a zip')})
    assert result.status_code==422

def test_inventory_row_rollback_keeps_other_rows(client):
    shop=client.post('/api/shops',json={'shop_code':'S','platform':'demo','name':'店铺','platform_shop_id':'S'}).json()
    product=client.post('/api/products',json={'product_code':'P','name':'商品','category':'服装','base_price':'299'}).json()
    sku=client.post(f"/api/products/{product['id']}/skus",json={'sku_code':'K'}).json()
    listing=client.post(f"/api/products/{product['id']}/listings",json={'shop_id':shop['id'],'platform_product_id':'P'}).json()
    mapping=client.post(f"/api/listings/{listing['id']}/sku-mappings",json={'sku_id':sku['id'],'platform_sku_id':'K','sale_price':'299','stock_quantity':8,'warning_threshold':10}).json()
    batch=preview(client,'shop_code,product_code,sku_code,stock_quantity\nS,P,K,12\nS,P,UNKNOWN,5\n','inventory')
    original=imports.write_row
    def fail_after_write(*args):
        original(*args)
        raise ValueError('注入行事务失败')
    with patch.object(imports,'write_row',side_effect=fail_after_write):
        result=client.post(f"/api/imports/{batch['id']}/commit",json={})
    assert result.json()['summary']['failed']==2
    with db.connect() as c:
        assert c.execute('SELECT stock_quantity FROM sku_mappings WHERE id=?',(mapping['id'],)).fetchone()[0]==8
        assert c.execute('SELECT COUNT(*) FROM stock_movements WHERE mapping_id=?',(mapping['id'],)).fetchone()[0]==1
    batch=preview(client,'shop_code,product_code,sku_code,stock_quantity\nS,P,K,12\nS,P,UNKNOWN,5\n','inventory')
    result=client.post(f"/api/imports/{batch['id']}/commit",json={})
    assert result.json()['summary']['updated']==1 and result.json()['summary']['failed']==1
