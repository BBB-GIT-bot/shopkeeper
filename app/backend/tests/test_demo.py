import os
from unittest.mock import patch
from fastapi.testclient import TestClient
from app import db
from app.main import app
from app.scripts.demo import create_demo

def test_full_classroom_scenario(tmp_path):
    with patch.dict(os.environ,{'DATABASE_URL':'sqlite:///'+(tmp_path/'demo.db').as_posix(),'STORAGE_ROOT':str(tmp_path/'files')}):
        with TestClient(app) as client:
            result=create_demo(client)
            assert result['status']=='passed' and result['metrics']['ctr']==.025
            assert result['totals']['gmv']=='18837.00'
            assert result['second_round_id']!=result['first_round_id']
            with db.connect() as c:
                assert c.execute('SELECT COUNT(*) FROM metrics').fetchone()[0]==14
                assert c.execute("SELECT COUNT(*) FROM approvals WHERE decision='rejected'").fetchone()[0]==1
                assert c.execute('SELECT COUNT(*) FROM stock_movements').fetchone()[0]==8
                proposal=c.execute("SELECT * FROM proposals WHERE status='approved'").fetchone()
                snapshot=db.parse_json(proposal['content_snapshot'])
                assert snapshot['diagnosis']['id']==proposal['diagnosis_content_id']
                assert snapshot['creative']['id']==proposal['creative_content_id']
                assert snapshot['product']['base_price']=='299.00'
            with db.transaction() as c:
                c.execute('UPDATE products SET base_price=? WHERE id=?',('399.00',result['product_id']))
            with db.connect() as c:
                persisted=db.parse_json(c.execute('SELECT content_snapshot FROM proposals WHERE id=?',(proposal['id'],)).fetchone()[0])
                assert persisted['product']['base_price']=='299.00'
            assert create_demo(client)['status']=='existing'
            token=client.post('/api/auth/login',json={'username':'operator','password':'operator123'}).json()['access_token']
            headers={'Authorization':'Bearer '+token}
            archived=result['material_ids'][0]
            assert client.post(f'/api/materials/{archived}/archive',headers=headers).status_code==200
            assert archived not in {x['id'] for x in client.get('/api/materials',headers=headers).json()['items']}
            assert client.get(f'/api/materials/{archived}',headers=headers).json()['archived_at']
            assert client.get(f'/api/experiments/{result["experiment_id"]}',headers=headers).status_code==200
        db.dispose_engines()
