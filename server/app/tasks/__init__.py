"""worker 周期任务（docs/02 §2.8、docs/01 §5）。

每个文件暴露一个可独立调用的入口函数（返回 ``int`` 或 ``dict`` 计数），由 ``app.worker`` / ``app.monitor_worker`` 主循环按周期
提交到进程内线程池；函数内部获取自己的单例锁 ``lock:*`` 并在 ``finally`` 释放，网络 I/O 与长事务只在入口函数内发生。
业务规则、状态机与事务写在 ``services/*``，任务文件只做领取 / 调度 / 分派。

``app.worker``：``run_ai_tasks`` / ``poll_media_tasks``（第 4 步）/ ``transfer_media``（第 4 步）/ ``reconcile_usage`` /
``sync_models`` / ``health_probe`` / ``recover_stale_tasks`` / ``cleanup_media``（第 4 步）。
"""
