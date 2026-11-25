"""Planning off the event loop, and a watchdog for when the loop stalls
anyway (docs/ROUTING.md 1.12).

Her connection, safety, healing, combat and chat all run on one asyncio
event loop. A planner that searches the whole world on that loop holds all
of them: live, ranking the 23 temple priests took about 45 s, the QUIC
connection went idle and the server dropped her, and every reconnect
started the same ranking again until she starved.

So expensive planning runs here, on one worker thread:

- ``submit`` hands a planner to the single worker and returns its
  ``concurrent.futures.Future`` at once. A tick polls ``done()`` and
  carries on with safety and combat until it is.
- The worker never reads live state. ``PlanningClient`` is her client as
  it stood when the plan was asked for: a copy of her game state, with
  attribute writes kept on the copy and ``record_action`` calls kept for
  the loop to replay (``replay_actions``).
- The worker never touches the graph caches the loop uses: on the worker
  a NavigateTask plans on ``WorldGraph.planner_view()``, which shares the
  immutable map data and has caches of its own. One worker means one
  thread ever uses that view.

navigation.BackgroundPlans runs route rankings this way.

``LoopWatchdog`` measures how late the loop wakes up and records each
stall over ``STALL_SECONDS`` (``event-loop-stall``), so any planner still
on the loop shows up on the dashboard.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import dataclasses
import logging
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

from .alerts import LOOP_STALL_ACTION as STALL_ACTION

log = logging.getLogger(__name__)


class _Worker:
    """One daemon thread that runs submitted planners in order.

    A daemon, unlike ThreadPoolExecutor's workers, never holds up the
    process's exit behind a long plan."""

    def __init__(self, name: str):
        self._jobs: queue.SimpleQueue = queue.SimpleQueue()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    @property
    def thread(self) -> threading.Thread:
        return self._thread

    def submit(self, fn: Callable[..., Any], /, *args: Any,
               **kwargs: Any) -> concurrent.futures.Future:
        future: concurrent.futures.Future = concurrent.futures.Future()
        self._jobs.put((future, fn, args, kwargs))
        return future

    def _run(self) -> None:
        while True:
            future, fn, args, kwargs = self._jobs.get()
            if not future.set_running_or_notify_cancel():
                continue
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:  # handed to whoever polls it
                future.set_exception(exc)
            else:
                future.set_result(result)


_worker: _Worker | None = None
_worker_lock = threading.Lock()


def worker() -> _Worker:
    """The planner worker, started on first use."""
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.thread.is_alive():
            # None yet, or this is a forked child, which has no threads.
            _worker = _Worker("sera-planner")
        return _worker


def on_worker() -> bool:
    """Whether this code runs on the planner worker."""
    current = _worker
    return current is not None and threading.current_thread() is current.thread


def submit(fn: Callable[..., Any], /, *args: Any,
           **kwargs: Any) -> concurrent.futures.Future:
    """Run ``fn(*args, **kwargs)`` on the planner worker."""
    return worker().submit(fn, *args, **kwargs)


def _copy_container(value: Any) -> Any:
    if isinstance(value, dict):
        copied = copy.copy(value)  # keeps a defaultdict's factory
        for key, inner in value.items():
            if isinstance(inner, (dict, list, set)):
                copied[key] = copy.copy(inner)
        return copied
    if isinstance(value, (list, set)):
        return copy.copy(value)
    return value


def snapshot_state(state: Any) -> Any:
    """A copy of her game state that the loop's packet handlers can no
    longer change: every container field copied (one level deep inside a
    dict), the map state likewise, and each item with its own ``extra``.
    Immutable values are shared."""
    if not dataclasses.is_dataclass(state):
        return copy.deepcopy(state)
    copied = copy.copy(state)
    for field in dataclasses.fields(state):
        value = getattr(state, field.name)
        if field.name == "items" and isinstance(value, dict):
            value = {tag: dataclasses.replace(item, extra=dict(item.extra))
                     if dataclasses.is_dataclass(item) and
                     isinstance(getattr(item, "extra", None), dict)
                     else copy.copy(item)
                     for tag, item in value.items()}
        elif dataclasses.is_dataclass(value) and not isinstance(value, type):
            inner = copy.copy(value)
            for inner_field in dataclasses.fields(value):
                setattr(inner, inner_field.name,
                        _copy_container(getattr(value, inner_field.name)))
            value = inner
        else:
            value = _copy_container(value)
        setattr(copied, field.name, value)
    return copied


class PlanningClient:
    """Her client as it stood when a plan was asked for, for a planner
    thread (module doc).

    ``state`` is a snapshot (``snapshot_state``), and so are her decision
    and action histories. Any other attribute is read from her client; one the planner sets stays on this copy (the
    planners' own caches, such as the learned route model's). Recorded
    actions are kept in ``actions`` for ``replay_actions`` on the loop.
    """

    # Her client's histories the planners read (deaths, recent decisions):
    # copied too, as the loop appends to them.
    HISTORIES = ("decision_history", "action_history")

    def __init__(self, client: Any):
        object.__setattr__(self, "_source", client)
        object.__setattr__(self, "state", snapshot_state(client.state))
        object.__setattr__(self, "actions", [])
        for name in self.HISTORIES:
            history = getattr(client, name, None)
            if isinstance(history, list):
                object.__setattr__(self, name, list(history))

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_source"), name)

    def record_action(self, action: str, detail: str = "", **_: Any) -> None:
        self.actions.append((action, detail))

    def replay_actions(self, client: Any) -> None:
        """Record the planner's actions on her real client (on the loop)."""
        recorder = getattr(client, "record_action", None)
        actions, self.actions[:] = list(self.actions), []
        if recorder is None:
            return
        for action, detail in actions:
            recorder(action, detail)


class LoopWatchdog:
    """Measure how late the event loop wakes up (module doc).

    Every ``interval`` seconds it sleeps and measures the overshoot. Above
    ``threshold`` it logs a warning and records ``event-loop-stall`` with
    the lag and what was running (``describe``), which the dashboard turns
    into an alert (alerts.loop_stall). The worst lag seen is kept in
    ``worst`` and the last in ``last``.
    """

    INTERVAL_SECONDS = 0.5
    STALL_SECONDS = 1.0
    # A ledger save never holds the loop it is reporting on for long.
    SAVE_TIMEOUT = 0.2

    def __init__(self, client: Any = None, *,
                 describe: Callable[[], str] | None = None,
                 interval: float = INTERVAL_SECONDS,
                 threshold: float = STALL_SECONDS,
                 clock: Callable[[], float] = time.monotonic):
        self.client = client
        self.describe = describe
        self.interval = interval
        self.threshold = threshold
        self.clock = clock
        self.stalls = 0
        self.last = 0.0
        self.worst = 0.0

    def observe(self, lag: float) -> bool:
        """Account one measured lag; True when it was a stall."""
        self.last = lag
        self.worst = max(self.worst, lag)
        if lag <= self.threshold:
            return False
        self.stalls += 1
        running = ""
        if self.describe is not None:
            try:
                running = self.describe()
            except Exception:  # a report never ends the watchdog
                running = ""
        detail = f"{lag:.1f}s late" + (f" in {running}" if running else "")
        log.warning("event loop stalled: %s", detail)
        recorder = getattr(self.client, "record_action", None)
        if recorder is not None:
            try:
                recorder(STALL_ACTION, detail, save_timeout=self.SAVE_TIMEOUT)
            except TypeError:
                recorder(STALL_ACTION, detail)
            except Exception:
                log.exception("event loop stall not recorded")
        return True

    async def run(self) -> None:
        while True:
            started = self.clock()
            await asyncio.sleep(self.interval)
            self.observe(max(0.0, self.clock() - started - self.interval))
