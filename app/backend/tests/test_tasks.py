import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from app import db, tasks
from app.main import app, generated_creative
from app.worker.main import Worker

@pytest.fixture
def queue(tmp_path):
    with patch.dict(os.environ, {'DATABASE_URL':'sqlite:///'+(tmp_path/'queue.db').as_posix(), 'STORAGE_ROOT':str(tmp_path/'files')}):
        with TestClient(app) as client:
            with db.transaction() as c:
                c.execute("INSERT INTO products(product_code,name,category,base_price) VALUES (?,?,?,?)",('Q','Queue','服装','299'))
                c.execute('INSERT INTO rounds(product_id,round_no) VALUES (?,?)',(1,1))
                creative=generated_creative(); creative['selected_image_ids']=['image-1']; creative['selected_video_ids']=['video-1']
                c.execute('INSERT INTO contents(product_id,round_id,kind,version,status,content) VALUES (?,?,?,?,?,?)',(1,1,'creative',1,'confirmed',db.json_value(creative)))
                c.execute('UPDATE queue_configs SET execution_timeout_seconds=?,heartbeat_interval_seconds=?,max_auto_retries=? WHERE id=?',(3,1,2,1))
            token=client.post('/api/auth/login',json={'username':'operator','password':'operator123'}).json()['access_token']
            yield client, {'Authorization':'Bearer '+token}, tmp_path
        db.dispose_engines()

def submit(queue, scenario='success', kind='image'):
    client, headers, _=queue
    response=client.post('/api/tasks',headers=headers,json={'creative_content_id':1,'selection_id':'image-1' if kind=='image' else 'video-1','task_type':kind,'demo_scenario':scenario})
    assert response.status_code==202, response.text
    assert response.json()['status']=='pending' and response.json()['attempts']==[]
    return response.json()['id']

def test_quota_cancel_identity_duplicate_and_snapshot(queue):
    first=submit(queue); second=submit(queue); submit(queue)
    a=tasks.claim(); b=tasks.claim()
    assert {a['id'],b['id']}=={first,second} and tasks.claim() is None
    with db.transaction() as c: c.execute('UPDATE queue_configs SET execution_timeout_seconds=? WHERE id=?',(90,1))
    assert a['config_snapshot']['execution_timeout_seconds']==3
    client, headers, _=queue
    cancelled=client.post(f'/api/tasks/{first}/cancel',headers=headers,json={})
    assert cancelled.status_code==200 and cancelled.json()['cancelled_at']
    Path(a['file_path']).parent.mkdir(parents=True); Path(a['file_path']).write_bytes(b'late')
    assert not tasks.complete(a,True)
    assert tasks.claim() is not None
    Path(b['file_path']).write_bytes(b'valid')
    assert tasks.complete(b,True)
    assert not tasks.complete(b,True)
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM materials').fetchone()[0]==1
    assert client.get(f'/api/tasks/{first}',headers=headers).json()['status']=='cancelled'

def test_retry_budget_recovery_and_manual_retry(queue):
    task_id=submit(queue,'always_fail')
    for number in range(1,4):
        job=tasks.claim(); assert job['attempts']==number
        tasks.complete(job,False,'教学失败')
    client, headers, _=queue
    task=client.get(f'/api/tasks/{task_id}',headers=headers).json()
    assert task['status']=='failed' and task['retry_available'] and len(task['attempts'])==3
    retry=client.post(f'/api/tasks/{task_id}/retry',headers=headers)
    assert retry.status_code==202 and retry.json()['original_task_id']==task_id
    job=tasks.claim()
    with db.transaction() as c:
        c.execute('UPDATE tasks SET deadline_at=? WHERE id=?',('2020-01-01T00:00:00Z',job['id']))
    tasks.reap_expired()
    assert not tasks.complete(job,True)
    recovery=tasks.claim(); assert recovery['attempts']==2
    with db.connect() as c: assert c.execute('SELECT status FROM task_attempts WHERE id=?',(job['current_attempt_id'],)).fetchone()[0]=='timeout'

def test_independent_subprocess_image_video_and_fail_once(queue):
    ids=[submit(queue,'fail_once'),submit(queue,kind='video')]
    worker=Worker()
    try:
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            worker.tick()
            with db.connect() as c: statuses=[c.execute('SELECT status FROM tasks WHERE id=?',(i,)).fetchone()[0] for i in ids]
            if statuses==['succeeded','succeeded']: break
            time.sleep(.15)
        assert statuses==['succeeded','succeeded']
        with db.connect() as c:
            material=c.execute("SELECT * FROM materials WHERE material_type='video'").fetchone()
            image=c.execute("SELECT * FROM materials WHERE material_type='image'").fetchone()
            assert c.execute('SELECT attempts FROM tasks WHERE id=?',(ids[0],)).fetchone()[0]==2
        from PIL import Image
        with Image.open(image['file_path']) as file: assert file.size==(720,960)
        import imageio_ffmpeg
        completed=subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(),'-v','error','-i',material['file_path'],'-f','null','-'],capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
        assert completed.returncode==0, completed.stderr
        client,headers,_=queue
        assert client.get(material['file_url'],headers=headers).status_code==200
    finally: worker.close()

def test_standalone_dispatcher_resumes_persisted_queue(queue):
    task_id=submit(queue)
    process=subprocess.Popen([sys.executable,'-m','app.worker.main'],cwd=db.ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        client,headers,_=queue
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            assert process.poll() is None
            task=client.get(f'/api/tasks/{task_id}',headers=headers).json()
            if task['terminal']:break
            time.sleep(.15)
        assert task['status']=='succeeded'
        assert len(task['attempts'])==1 and task['attempts'][0]['process_id']!=os.getpid()
    finally:
        process.terminate();process.communicate(timeout=8)
