"""Tests for what actually starts work.

The webhook used to set a bool that the loop read once per iteration - after the
full tick interval had already elapsed - so a delivery bought back exactly zero
latency and the system was a poller wearing a webhook's clothes. These pin the
two properties that make it a trigger: a delivery wakes the loop early, and a
delivery that lands mid-tick is not swallowed.
"""

import asyncio

import pytest

from cognition.core import config, reconciler
from cognition.verification import scanner
from cognition.web import web

import main


# Long enough that any test finishing inside it proves the loop woke early
# rather than slept it out, short enough to bound a hung test.
SLOW_TICK = 30
WAKE_TIMEOUT = 2


def _run(scenario):
    """Run an async scenario without taking a pytest-asyncio dependency."""
    return asyncio.run(scenario())


async def _stop(task):
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


class TestWebhookSignal:
    """POST /webhook -> the reconciler loop."""

    def test_webhook_sets_the_signal(self):
        async def scenario():
            web.webhook_signal().clear()
            await web.webhook(None)
            assert web.webhook_signal().is_set()

        _run(scenario)

    def test_delivery_wakes_the_loop_before_the_interval(self, monkeypatch):
        """The whole point: a delivery must not wait out TICK_SECONDS.

        The assertion is the timeout. With a 30s tick interval, a second tick
        arriving within 2s can only have come from the webhook.
        """

        async def scenario():
            ticked = asyncio.Event()

            async def fake_tick():
                ticked.set()

            monkeypatch.setattr(reconciler, "tick", fake_tick)
            monkeypatch.setattr(config, "TICK_SECONDS", SLOW_TICK)
            web.webhook_signal().clear()

            task = asyncio.create_task(main.reconciler_task())
            try:
                await asyncio.wait_for(ticked.wait(), timeout=WAKE_TIMEOUT)
                ticked.clear()

                await web.webhook(None)
                await asyncio.wait_for(ticked.wait(), timeout=WAKE_TIMEOUT)
            finally:
                await _stop(task)

        _run(scenario)

    def test_delivery_during_a_tick_is_not_swallowed(self, monkeypatch):
        """The race the clear-before-tick ordering exists to close.

        A delivery that lands while a tick is already running describes work that
        tick may have read too early to see. Clearing the signal after the tick
        would drop it and stall that work for a full interval.
        """

        async def scenario():
            ticks = 0
            second_tick = asyncio.Event()

            async def fake_tick():
                nonlocal ticks
                ticks += 1
                if ticks == 1:
                    # Arrives mid-tick, before the loop reaches its wait.
                    await web.webhook(None)
                else:
                    second_tick.set()

            monkeypatch.setattr(reconciler, "tick", fake_tick)
            monkeypatch.setattr(config, "TICK_SECONDS", SLOW_TICK)
            web.webhook_signal().clear()

            task = asyncio.create_task(main.reconciler_task())
            try:
                await asyncio.wait_for(second_tick.wait(), timeout=WAKE_TIMEOUT)
            finally:
                await _stop(task)

        _run(scenario)

    def test_loop_survives_a_failing_tick(self, monkeypatch):
        """A tick that raises must not kill the trigger with it."""

        async def scenario():
            ticks = 0
            failed = asyncio.Event()
            recovered = asyncio.Event()

            async def fake_tick():
                nonlocal ticks
                ticks += 1
                if ticks == 1:
                    failed.set()
                    raise RuntimeError("GitHub is down")
                recovered.set()

            monkeypatch.setattr(reconciler, "tick", fake_tick)
            monkeypatch.setattr(config, "TICK_SECONDS", SLOW_TICK)
            web.webhook_signal().clear()

            task = asyncio.create_task(main.reconciler_task())
            try:
                # Deliver only once the first tick has been entered. Delivering
                # before the loop starts would be wiped by its clear-before-tick,
                # which is correct behaviour and not what this test is about.
                await asyncio.wait_for(failed.wait(), timeout=WAKE_TIMEOUT)
                await web.webhook(None)
                await asyncio.wait_for(recovered.wait(), timeout=WAKE_TIMEOUT)
            finally:
                await _stop(task)

        _run(scenario)


class TestScheduledScan:
    """The trigger that puts findings into the ledger in the first place."""

    def test_run_scan_records_liveness(self, monkeypatch):
        monkeypatch.setattr(scanner, "_last_scan_time", None)
        monkeypatch.setattr(scanner, "_last_scan_error", "stale failure")
        monkeypatch.setattr(scanner, "scan", lambda path: ["a", "b", "c"])
        monkeypatch.setattr(scanner, "sync_findings", lambda findings: None)

        assert scanner.run_scan() == 3
        assert scanner.get_last_scan_time() is not None
        assert scanner.get_last_scan_findings() == 3
        # A success has to clear the previous failure, or the status page keeps
        # showing a red banner for a scan that has since recovered.
        assert scanner.get_last_scan_error() is None

    def test_failed_scan_is_recorded_and_raised(self, monkeypatch):
        monkeypatch.setattr(scanner, "_last_scan_error", None)

        def boom(path):
            raise OSError("bandit not found")

        monkeypatch.setattr(scanner, "scan", boom)

        with pytest.raises(OSError):
            scanner.run_scan()

        assert "bandit not found" in scanner.get_last_scan_error()

    def test_scheduled_scan_can_be_disabled(self, monkeypatch):
        """SCAN_INTERVAL_MINUTES=0 leaves the manual trigger and returns."""

        async def scenario():
            monkeypatch.setattr(config, "SCAN_INTERVAL_MINUTES", 0)

            def fail(*args, **kwargs):
                raise AssertionError("disabled schedule must not scan")

            monkeypatch.setattr(scanner, "run_scan", fail)

            await asyncio.wait_for(main.scanner_task(), timeout=WAKE_TIMEOUT)

        _run(scenario)

    def test_scheduled_scan_runs_at_startup(self, monkeypatch):
        """It scans on boot rather than waiting out the first interval."""

        async def scenario():
            scanned = asyncio.Event()

            def fake_run_scan():
                scanned.set()
                return 7

            monkeypatch.setattr(config, "SCAN_INTERVAL_MINUTES", SLOW_TICK)
            monkeypatch.setattr(scanner, "run_scan", fake_run_scan)

            task = asyncio.create_task(main.scanner_task())
            try:
                await asyncio.wait_for(scanned.wait(), timeout=WAKE_TIMEOUT)
            finally:
                await _stop(task)

        _run(scenario)

    def test_scan_failure_does_not_kill_the_schedule(self, monkeypatch):
        """A stalled trigger is not a stopped system; the loop keeps going."""

        async def scenario():
            calls = 0
            second_call = asyncio.Event()

            def fake_run_scan():
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise OSError("target repo unreadable")
                second_call.set()
                return 0

            # Interval in minutes; a fractional value keeps the retry quick.
            monkeypatch.setattr(config, "SCAN_INTERVAL_MINUTES", 0.005)
            monkeypatch.setattr(scanner, "run_scan", fake_run_scan)

            task = asyncio.create_task(main.scanner_task())
            try:
                await asyncio.wait_for(second_call.wait(), timeout=WAKE_TIMEOUT)
            finally:
                await _stop(task)

        _run(scenario)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
