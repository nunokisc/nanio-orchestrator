"""The shared nginx write path: write → nginx -t → (reload) → rollback on failure."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanio_orchestrator.nginx.apply import apply_config_file, apply_config_files, nginx_lock

OK = MagicMock(ok=True, output="syntax ok")
FAIL = MagicMock(ok=False, output="syntax error")


def _nginx(test=OK, reload=OK):
    test_mock = AsyncMock(return_value=test)
    reload_mock = AsyncMock(return_value=reload)
    return (
        patch("nanio_orchestrator.nginx.executor.test_config", test_mock),
        patch("nanio_orchestrator.nginx.executor.reload_nginx", reload_mock),
        test_mock,
        reload_mock,
    )


class TestApplyConfig:
    async def test_candidate_is_in_place_when_nginx_tests_it(self, tmp_path):
        """nginx only includes *.conf — the new content must be at the final path during nginx -t."""
        conf = tmp_path / "a.conf"
        conf.write_text("old")
        seen = {}

        async def fake_test():
            seen["content"] = conf.read_text()
            seen["leftovers"] = sorted(p.name for p in tmp_path.iterdir())
            return OK

        with patch("nanio_orchestrator.nginx.executor.test_config", fake_test):
            result = await apply_config_file(str(conf), "new")

        assert result.ok and result.stage == "test"
        assert seen == {"content": "new", "leftovers": ["a.conf"]}
        assert conf.read_text() == "new"

    async def test_failed_test_restores_previous_content(self, tmp_path):
        conf = tmp_path / "a.conf"
        conf.write_text("old")
        p_test, p_reload, _, reload_mock = _nginx(test=FAIL)
        with p_test, p_reload:
            result = await apply_config_file(str(conf), "broken", reload=True)

        assert not result.ok and result.stage == "test" and result.output == "syntax error"
        assert conf.read_text() == "old"
        reload_mock.assert_not_called()

    async def test_failed_test_removes_file_that_did_not_exist(self, tmp_path):
        conf = tmp_path / "new.conf"
        p_test, p_reload, _, _ = _nginx(test=FAIL)
        with p_test, p_reload:
            result = await apply_config_file(str(conf), "broken")

        assert not result.ok
        assert list(tmp_path.iterdir()) == []

    async def test_failed_reload_restores_previous_content(self, tmp_path):
        conf = tmp_path / "a.conf"
        conf.write_text("old")
        p_test, p_reload, _, _ = _nginx(reload=MagicMock(ok=False, output="nginx is not running"))
        with p_test, p_reload:
            result = await apply_config_file(str(conf), "new", reload=True)

        assert not result.ok and result.stage == "reload"
        assert conf.read_text() == "old"

    async def test_no_reload_unless_requested(self, tmp_path):
        p_test, p_reload, test_mock, reload_mock = _nginx()
        with p_test, p_reload:
            await apply_config_file(str(tmp_path / "a.conf"), "new")
        test_mock.assert_awaited_once()
        reload_mock.assert_not_called()

    async def test_multi_file_failure_restores_every_file(self, tmp_path):
        a, b, c = tmp_path / "a.conf", tmp_path / "b.conf", tmp_path / "c.conf"
        a.write_text("old-a")
        b.write_text("old-b")
        p_test, p_reload, _, _ = _nginx(test=FAIL)
        with p_test, p_reload:
            result = await apply_config_files([(str(a), "new-a"), (str(b), "new-b"), (str(c), "new-c")])

        assert not result.ok
        assert a.read_text() == "old-a" and b.read_text() == "old-b"
        assert not c.exists()

    async def test_cancellation_restores_previous_content(self, tmp_path):
        """A migration cancelled mid-apply must not leave an unvalidated file behind."""
        conf = tmp_path / "a.conf"
        conf.write_text("old")
        started = asyncio.Event()

        async def hanging_test():
            started.set()
            await asyncio.sleep(60)

        with patch("nanio_orchestrator.nginx.executor.test_config", hanging_test):
            task = asyncio.create_task(apply_config_file(str(conf), "new"))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert conf.read_text() == "old"

    async def test_applies_are_serialized(self, tmp_path):
        """A second writer must not be validated against the first writer's in-flight file."""
        a, b = tmp_path / "a.conf", tmp_path / "b.conf"
        in_test = 0
        overlap = False

        async def slow_test():
            nonlocal in_test, overlap
            in_test += 1
            overlap = overlap or in_test > 1
            await asyncio.sleep(0.01)
            in_test -= 1
            return OK

        with patch("nanio_orchestrator.nginx.executor.test_config", slow_test):
            await asyncio.gather(apply_config_file(str(a), "a"), apply_config_file(str(b), "b"))

        assert not overlap
        async with nginx_lock():  # released afterwards
            pass
