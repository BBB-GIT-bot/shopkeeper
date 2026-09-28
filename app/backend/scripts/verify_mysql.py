"""Create an isolated local MySQL process, validate it, then stop that process.

Uses the installed MySQL binary, a fresh workspace data directory and an unused
localhost port. Never connects to or alters the existing Windows MySQL service.
No source directories, test data or database history are deleted.
"""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import uuid

import pymysql

ROOT = Path(__file__).resolve().parents[1]


def main():
    binary = shutil.which('mysqld')
    if not binary:
        raise RuntimeError('mysqld not found; supply an installed MySQL 8.0 binary in PATH')
    directory = ROOT / 'storage' / ('mysql-validation-' + uuid.uuid4().hex[:12])
    directory.mkdir(parents=True)
    data = directory / 'data'
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    log = directory / 'server.log'
    with log.open('w', encoding='utf-8') as output:
        subprocess.run([binary, '--no-defaults', '--initialize-insecure', '--basedir=' + str(Path(binary).parent.parent), '--datadir=' + str(data)], stdout=output, stderr=subprocess.STDOUT, check=True, timeout=60, creationflags=flags)
    with socket.socket() as available:
        available.bind(('127.0.0.1', 0))
        port = available.getsockname()[1]
    with log.open('a', encoding='utf-8') as output:
        server = subprocess.Popen([binary, '--no-defaults', '--basedir=' + str(Path(binary).parent.parent), '--datadir=' + str(data), '--bind-address=127.0.0.1', '--port=' + str(port), '--mysqlx=OFF', '--console'], stdout=output, stderr=subprocess.STDOUT, creationflags=flags)
    result = {'status': 'failed', 'scope': 'isolated localhost MySQL; synthetic data only', 'port': port}
    try:
        connection = None
        for _ in range(60):
            if server.poll() is not None:
                raise RuntimeError('MySQL process exited; inspect server.log')
            try:
                connection = pymysql.connect(host='127.0.0.1', port=port, user='root', connect_timeout=1)
                break
            except pymysql.err.OperationalError:
                time.sleep(0.5)
        if connection is None:
            raise RuntimeError('MySQL did not become ready within 30 seconds')
        name = 'shopkeeper_test_' + uuid.uuid4().hex[:12]
        with connection, connection.cursor() as cursor:
            cursor.execute('SELECT VERSION()')
            result['mysql_version'] = cursor.fetchone()[0]
            cursor.execute('CREATE DATABASE ' + name + ' CHARACTER SET utf8mb4 COLLATE utf8mb4_bin')
        environment = os.environ.copy()
        environment['PYTHONIOENCODING'] = 'utf-8'
        environment['SHOPKEEPER_MYSQL_TEST_URL'] = f'mysql+pymysql://root@127.0.0.1:{port}/{name}?charset=utf8mb4'
        print('Isolated MySQL ready; running migration and API tests', flush=True)
        with (directory / 'tests.log').open('w', encoding='utf-8') as output:
            completed = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', '--tb=short', 'tests/test_mysql.py'], cwd=ROOT, env=environment, stdout=output, stderr=subprocess.STDOUT, timeout=90, creationflags=flags)
        result['test_exit_code'] = completed.returncode
        result['database'] = name
        result['status'] = 'passed' if completed.returncode == 0 else 'failed'
        print((directory / 'tests.log').read_text(encoding='utf-8', errors='replace')[-5000:], flush=True)
    finally:
        try:
            with pymysql.connect(host='127.0.0.1', port=port, user='root', connect_timeout=1) as connection:
                connection.query('SHUTDOWN')
        except pymysql.err.OperationalError:
            pass
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.terminate()
            server.wait(timeout=5)
        result['test_process_stopped'] = server.poll() is not None
        (directory / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print('Verification evidence: ' + str(directory / 'result.json'), flush=True)
    return result.get('test_exit_code', 1)


if __name__ == '__main__':
    raise SystemExit(main())
