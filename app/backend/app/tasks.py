"""Persistent queue control; execution runs only in the independent worker."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import os
import time
import uuid
from pathlib import Path

from .db import Connection, connect, get_engine, json_value, parse_json
from .config import storage_root

def now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

def expired(value):
    return bool(value and datetime.fromisoformat(value.replace('Z', '+00:00')) <= datetime.now(timezone.utc))

@contextmanager
def queue_transaction():
    with get_engine().connect() as raw:
        if raw.dialect.name == 'sqlite':
            raw.exec_driver_sql('BEGIN IMMEDIATE')
        else:
            raw.begin()
            raw.exec_driver_sql('SELECT id FROM queue_configs WHERE id=1 FOR UPDATE')
        try:
            yield Connection(raw)
            raw.commit()
        except BaseException:
            raw.rollback()
            raise

def snapshot(queue):
    return {key: queue[key] for key in ('concurrency', 'execution_timeout_seconds', 'max_auto_retries', 'poll_interval_seconds', 'heartbeat_interval_seconds', 'dispatch_interval_seconds')}

def public_task(item, connection):
    result = dict(item)
    result['attempt_count'] = result['attempts']
    result['attempts'] = [dict(x) for x in connection.execute('SELECT * FROM task_attempts WHERE task_id=? ORDER BY attempt_no', (item['id'],))]
    for attempt in result['attempts']:
        attempt.pop('execution_id', None)
    result['terminal'] = result['status'] in {'succeeded', 'cancelled', 'failed', 'timeout'}
    result['retry_available'] = result['status'] in {'failed', 'timeout'}
    result['config_snapshot'] = parse_json(result.get('config_snapshot'), {})
    queue = connection.execute('SELECT poll_interval_seconds FROM queue_configs WHERE id=1').fetchone()
    result['poll_interval_seconds'] = queue[0]
    result['execution_deadline'] = result['deadline_at']
    result['is_demo'] = True
    return result

def enqueue(connection, creative, selection, task_type, scenario='success', original=None):
    queue = connection.execute('SELECT * FROM queue_configs WHERE id=1').fetchone()
    return connection.execute('INSERT INTO tasks(product_id,round_id,creative_content_id,selection_id,task_type,scenario,max_auto_retries,poll_interval_seconds,queued_at,config_snapshot,original_task_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
        (creative['product_id'], creative['round_id'], creative['id'], selection, task_type, scenario, queue['max_auto_retries'], queue['poll_interval_seconds'], now(), json_value(snapshot(queue)), original)).lastrowid

def claim():
    with queue_transaction() as c:
        queue = c.execute('SELECT * FROM queue_configs WHERE id=1').fetchone()
        count = c.execute("SELECT COUNT(*) FROM tasks WHERE status='running'").fetchone()[0]
        if count >= queue['concurrency']:
            return None
        item = c.execute("SELECT * FROM tasks WHERE status='pending' ORDER BY queued_at,id LIMIT 1").fetchone()
        if not item:
            return None
        config = parse_json(item['config_snapshot'], None) or snapshot(queue)
        attempt_no = item['attempts'] + 1
        started = now()
        deadline = (datetime.now(timezone.utc) + timedelta(seconds=config['execution_timeout_seconds'])).isoformat()
        identity = uuid.uuid4().hex
        attempt = c.execute('INSERT INTO task_attempts(task_id,attempt_no,status,scenario,queued_at,started_at,heartbeat_at,deadline_at,execution_id) VALUES (?,?,?,?,?,?,?,?,?)',
            (item['id'], attempt_no, 'running', item['scenario'], item['queued_at'], started, started, deadline, identity)).lastrowid
        c.execute("UPDATE tasks SET status='running',attempts=?,current_attempt_id=?,started_at=?,heartbeat_at=?,deadline_at=?,finished_at=NULL,error_reason=NULL,updated_at=? WHERE id=?", (attempt_no, attempt, started, started, deadline, started, item['id']))
        result = dict(item)
        result.update(current_attempt_id=attempt, execution_id=identity, attempts=attempt_no, deadline_at=deadline, config_snapshot=config)
        extension = 'png' if item['task_type'] == 'image' else 'mp4'
        result['file_path'] = str(storage_root() / 'materials' / f"task-{item['id']}-attempt-{attempt}-{identity}.{extension}")
        return result

def finish_failure(c, task, status, reason):
    ended = now()
    c.execute('UPDATE task_attempts SET status=?,finished_at=?,error_reason=? WHERE id=?', (status, ended, reason, task['current_attempt_id']))
    retry = task['attempts'] <= task['max_auto_retries']
    c.execute('UPDATE tasks SET status=?,error_reason=?,finished_at=?,queued_at=?,updated_at=? WHERE id=?', ('pending' if retry else status, reason, None if retry else ended, ended, ended, task['id']))

def reap_expired():
    with queue_transaction() as c:
        for task in c.execute("SELECT * FROM tasks WHERE status='running'").fetchall():
            if expired(task['deadline_at']):
                finish_failure(c, task, 'timeout', '执行超过配置期限，已回收名额')

def heartbeat(job, process_id):
    with queue_transaction() as c:
        task = c.execute('SELECT * FROM tasks WHERE id=?', (job['id'],)).fetchone()
        if not task or task['status'] != 'running' or task['current_attempt_id'] != job['current_attempt_id'] or expired(task['deadline_at']):
            return False
        moment = now()
        c.execute('UPDATE tasks SET heartbeat_at=? WHERE id=?', (moment, task['id']))
        c.execute('UPDATE task_attempts SET heartbeat_at=?,process_id=? WHERE id=? AND execution_id=?', (moment, process_id, job['current_attempt_id'], job['execution_id']))
        return True

def complete(job, succeeded, reason='演示执行失败'):
    with queue_transaction() as c:
        task = c.execute('SELECT * FROM tasks WHERE id=?', (job['id'],)).fetchone()
        attempt = c.execute('SELECT * FROM task_attempts WHERE id=?', (job['current_attempt_id'],)).fetchone()
        if not task or not attempt or task['status'] != 'running' or task['current_attempt_id'] != job['current_attempt_id'] or attempt['execution_id'] != job['execution_id']:
            return False
        if expired(task['deadline_at']):
            finish_failure(c, task, 'timeout', '执行结果晚于期限，未入素材库')
            return False
        if not succeeded or not Path(job['file_path']).is_file():
            finish_failure(c, task, 'failed', reason)
            return False
        material_id = c.execute('INSERT INTO materials(task_id,product_id,round_id,material_type,selection_id,file_url,attempt_id,creative_content_id,file_path,review_history) VALUES (?,?,?,?,?,?,?,?,?,?)',
            (task['id'], task['product_id'], task['round_id'], task['task_type'], task['selection_id'], f"/api/materials/file/{task['id']}", job['current_attempt_id'], task['creative_content_id'], job['file_path'], '[]')).lastrowid
        version=c.execute('SELECT COUNT(*) FROM materials WHERE product_id=? AND round_id=? AND material_type=? AND selection_id=?',(task['product_id'],task['round_id'],task['task_type'],task['selection_id'])).fetchone()[0]
        c.execute('UPDATE materials SET version=? WHERE id=?',(version,material_id))
        ended = now()
        c.execute("UPDATE tasks SET status='succeeded',material_id=?,finished_at=?,updated_at=? WHERE id=?", (material_id, ended, ended, task['id']))
        c.execute("UPDATE task_attempts SET status='succeeded',finished_at=? WHERE id=?", (ended, job['current_attempt_id']))
        return True

def produce(job):
    """A terminable subprocess. Source is explicitly a classroom demonstration."""
    scenario = job['scenario']
    if scenario in {'timeout', 'late_result'}:
        time.sleep(job['config_snapshot']['execution_timeout_seconds'] + 5)
    else:
        time.sleep(0.5)
    if scenario == 'always_fail' or (scenario == 'fail_once' and job['attempts'] == 1):
        raise RuntimeError('教学模式触发素材制作失败')
    path = Path(job['file_path'])
    path.parent.mkdir(parents=True, exist_ok=True)
    from PIL import Image, ImageDraw
    cover = Image.new('RGB', (720, 960), '#eaf3ee')
    draw = ImageDraw.Draw(cover)
    draw.rounded_rectangle((130, 230, 590, 730), 40, fill='#729e9b')
    draw.text((70, 70), f'DEMO MATERIAL / TASK {job["id"]}', fill='#163c3b', font_size=30)
    draw.text((70, 800), 'SUN PROTECTION / TEACHING ONLY', fill='#163c3b', font_size=26)
    if job['task_type'] == 'image':
        cover.save(path)
    else:
        import imageio_ffmpeg
        import subprocess
        command = [imageio_ffmpeg.get_ffmpeg_exe(), '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=0x729e9b:s=720x960:r=24:d=3', '-vf', "drawtext=text='DEMO MATERIAL - TEACHING ONLY':fontcolor=white:fontsize=28:x=(w-text_w)/2:y=(h-text_h)/2", '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(path)]
        subprocess.run(command, check=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
