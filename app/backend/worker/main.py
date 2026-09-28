"""独立 worker 入口。任务执行逻辑由 API 事务触发的演示执行器承载，生产部署时可替换为进程队列调度。"""
from app.worker.main import main

if __name__ == '__main__':
    main()
