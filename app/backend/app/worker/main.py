"""Single standalone dispatcher with terminable execution subprocesses."""
import multiprocessing as mp
import signal
import time
from ..db import connect, init_db
from ..tasks import claim, complete, heartbeat, produce, reap_expired

class Worker:
    def __init__(self):
        self.context = mp.get_context('spawn')
        self.jobs = {}
        self.stopped = False

    def tick(self):
        reap_expired()
        for key, (job, process, last_heartbeat) in list(self.jobs.items()):
            if not process.is_alive():
                process.join()
                complete(job, process.exitcode == 0, f'演示执行失败，退出码 {process.exitcode}')
                del self.jobs[key]
            elif time.monotonic() - last_heartbeat >= min(1, job['config_snapshot']['heartbeat_interval_seconds']):
                if not heartbeat(job, process.pid):
                    process.terminate()
                    process.join(3)
                    del self.jobs[key]
                else:
                    self.jobs[key] = (job, process, time.monotonic())
        while True:
            job = claim()
            if not job:
                break
            process = self.context.Process(target=produce, args=(job,))
            try:
                process.start()
                heartbeat(job, process.pid)
                self.jobs[job['execution_id']] = (job, process, time.monotonic())
            except Exception:
                complete(job, False, '无法启动素材制作子进程')

    def close(self):
        for job, process, _ in self.jobs.values():
            if process.is_alive():
                process.terminate()
                process.join(3)
            # Remains running until its persisted deadline; restart reclaims it.
        self.jobs.clear()

def main():
    init_db()
    worker = Worker()
    def stop(*_):
        worker.stopped = True
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    print('shopkeeper independent worker ready', flush=True)
    try:
        while not worker.stopped:
            worker.tick()
            with connect() as c:
                interval = c.execute('SELECT dispatch_interval_seconds FROM queue_configs WHERE id=1').fetchone()[0]
            time.sleep(interval)
    finally:
        worker.close()

if __name__ == '__main__':
    main()
