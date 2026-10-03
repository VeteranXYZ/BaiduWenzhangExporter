from contextlib import closing
from threading import Event, Thread

import httpx
import pytest

from baidu_archive.auth import Credentials
from baidu_archive.client import ArchiveClient, AuthExpired, RateLimiter
from baidu_archive.config import Config
from baidu_archive.locking import ArchiveBusy, ArchiveLock
from baidu_archive.tasks import TaskControl, TaskStopped, bounded_results


def test_stopping_prevents_pending_items_from_starting():
    control = TaskControl()
    started = []

    def work(number):
        started.append(number)
        return number

    with closing(bounded_results(range(500), work, workers=1, control=control)) as results:
        for _, future in results:
            assert future.result() == 0
            control.stop()
    assert started == [0]


def test_closing_results_stops_queue_and_preserves_completed_work():
    control = TaskControl()
    finished = []

    def work(number):
        finished.append(number)
        return number

    with closing(bounded_results(range(500), work, workers=1, control=control)) as results:
        _, future = next(results)
        assert future.result() == 0
    assert control.stopped and finished == [0]


def test_stop_interrupts_long_rate_limit_wait():
    control = TaskControl()
    limiter = RateLimiter(60, 0, control)
    limiter.wait()
    entered = Event()
    stopped = Event()

    def wait_again():
        entered.set()
        with pytest.raises(TaskStopped):
            limiter.wait()
        stopped.set()

    thread = Thread(target=wait_again)
    thread.start()
    assert entered.wait(1)
    control.stop()
    thread.join(1)
    assert not thread.is_alive() and stopped.is_set()


def test_auth_abort_stops_subsequent_requests():
    calls = []
    client = ArchiveClient(Config(delay=0, jitter=0, auth_failure_threshold=1), Credentials())
    client._client.close()

    def handler(request):
        calls.append(request)
        return httpx.Response(403)

    client._client = httpx.Client(transport=httpx.MockTransport(handler),
                                 event_hooks={"request": [client._scope_credentials]})
    with client:
        with pytest.raises(AuthExpired):
            client.get("https://wenzhang.baidu.com/fav/list")
        with pytest.raises(TaskStopped):
            client.get("https://wenzhang.baidu.com/fav/list")
    assert len(calls) == 1


def test_user_stop_during_backoff_prevents_retry():
    control = TaskControl()
    retried = []
    client = ArchiveClient(Config(delay=0, jitter=0, backoff_base=60), Credentials(),
                           control=control, logger=lambda *_: control.stop())
    client._client.close()

    def handler(request):
        retried.append(request)
        return httpx.Response(503)

    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    with client:
        with pytest.raises(TaskStopped):
            client.get("https://wenzhang.baidu.com/fav/list")
    assert len(retried) == 1


def test_archive_lock_excludes_second_writer_and_releases(tmp_path):
    with ArchiveLock(tmp_path):
        with pytest.raises(ArchiveBusy):
            with ArchiveLock(tmp_path):
                pass
    with ArchiveLock(tmp_path):
        pass


def test_archive_lock_independent_roots(tmp_path):
    with ArchiveLock(tmp_path / "one"), ArchiveLock(tmp_path / "two"):
        pass


def test_cli_busy_does_not_start_work(tmp_path, monkeypatch):
    from baidu_archive import cli
    config = Config(root=tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda **kwargs: config)
    with ArchiveLock(tmp_path):
        assert cli.main(["parse"]) == 6
    assert not config.db_path.exists()


def test_run_records_interruption_without_sensitive_exception_text(tmp_path):
    from baidu_archive.state import ArchiveState
    with ArchiveState(tmp_path / "state.db") as state:
        with pytest.raises(TaskStopped):
            with state.tracked_run("fetch"):
                raise TaskStopped("PRIVATE_VALUE")
        run = state.recent_runs(1)[0]
        assert run["finished_at"]
        assert run["stats"] == {"outcome": "stopped"}
        with state.tracked_run("assets") as stats:
            stats.update(downloaded=1, failed=1)
        assert state.recent_runs(1)[0]["stats"]["outcome"] == "partial"
