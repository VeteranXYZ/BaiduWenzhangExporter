"""可停止的有限并发任务；同一时刻只调度 worker 数量的工作。"""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from threading import Event


class TaskStopped(RuntimeError):
    """主动停止，不计为抓取失败；已落盘的内容可继续使用。"""


class TaskControl:
    def __init__(self):
        self._stop = Event()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def stop(self) -> None:
        self._stop.set()

    def check(self) -> None:
        if self.stopped:
            raise TaskStopped("任务已停止，重新运行即可继续")

    def wait(self, seconds: float) -> None:
        if self._stop.wait(max(0, seconds)):
            self.check()


def bounded_results(items, worker, *, workers: int, control: TaskControl):
    """返回 (条目, Future)。停止后不补充任务，正在执行的原子保存可以完成。"""
    iterator = iter(items)
    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    pending = {}

    def fill():
        while len(pending) < max(1, workers) and not control.stopped:
            try:
                item = next(iterator)
            except StopIteration:
                break
            pending[pool.submit(worker, item)] = item

    try:
        fill()
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                yield pending.pop(future), future
            fill()
    except BaseException:
        control.stop()
        raise
    finally:
        for future in pending:
            future.cancel()
        pool.shutdown(wait=True, cancel_futures=True)
