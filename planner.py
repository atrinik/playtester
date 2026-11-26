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
  carries on with safety and combat until it is. Survival plans run
  first (``PRIORITY_FOOD``, then ``PRIORITY_HEAL``); ``unstick`` replaces
  a worker stuck in one job.
- The worker never reads live state. ``PlanningClient`` is her client as
  it stood when the plan was asked for: a copy of her game state, with
  attribute writes kept on the copy and ``record_action`` calls kept for
  the loop to replay (``replay_actions``).
- The worker never touches the graph caches the loop uses: on the worker
  a NavigateTask plans on ``WorldGraph.planner_view()``, which shares the
  immutable map data and has caches of its own. Each worker generation
  has its own view, so one thread ever uses a view.

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
import inspect
import itertools
import logging
import queue
import signal
import threading
import time
from collections.abc import Callable
from typing import Any

from .alerts import LOOP_STALL_ACTION as STALL_ACTION

log = logging.getLogger(__name__)


# Queue priorities: survival rankings first (food, then heals), then the rest.
PRIORITY_FOOD = 0
PRIORITY_HEAL = 1
PRIORITY_OTHER = 2

# Jobs for whichever worker is current, in priority, then submission, order.
_jobs: queue.PriorityQueue = queue.PriorityQueue()
_order = itertools.count()
# The futures of the jobs submitted and not yet finished, queued or
# running (busy): added on submit, removed when a job ends or is skipped as
# cancelled, or when its worker is abandoned (unstick).
_outstanding: set[concurrent.futures.Future] = set()
_outstanding_lock = threading.Lock()


def _settle(future: concurrent.futures.Future) -> None:
    with _outstanding_lock:
        _outstanding.discard(future)


# Signals the process receives from outside: a worker thread never takes
# one. A process-directed signal goes to any thread that does not block it,
# so with a worker alive, a SIGTERM the main thread holds blocked (the
# feedback filer's cleanup, pthread_sigmask) was taken by the worker
# instead and handled early. Faults raised by the thread itself stay
# unblocked.
_FAULTS = {"SIGSEGV", "SIGBUS", "SIGFPE", "SIGILL", "SIGABRT", "SIGTRAP", "SIGSYS"}


def _block_signals() -> None:
    """Block every signal but the faults on the calling (worker) thread:
    they are the main thread's to take."""
    if not hasattr(signal, "pthread_sigmask"):
        return
    signal.pthread_sigmask(signal.SIG_BLOCK, {
        number for number in signal.valid_signals()
        if getattr(number, "name", "") not in _FAULTS})


class _Worker:
    """One daemon thread that runs submitted planners, best priority first.

    A daemon, unlike ThreadPoolExecutor's workers, never holds up the
    process's exit behind a long plan. A worker that is replaced
    (``replace``) finishes, or never finishes, the job it holds and then
    ends; its ``generation`` keeps its graph view apart from its
    successor's (WorldGraph.planner_view)."""

    def __init__(self, generation: int):
        self.generation = generation
        # The job it runs, and since when (time.monotonic()).
        self.future: concurrent.futures.Future | None = None
        self.started_at = 0.0
        self._thread = threading.Thread(
            target=self._run, name=f"sera-planner-{generation}", daemon=True)

    def start(self) -> "_Worker":
        """Start its thread, once it is the current worker."""
        self._thread.start()
        return self

    @property
    def thread(self) -> threading.Thread:
        return self._thread

    def submit(self, fn: Callable[..., Any], /, *args: Any,
               priority: int = PRIORITY_OTHER,
               **kwargs: Any) -> concurrent.futures.Future:
        future: concurrent.futures.Future = concurrent.futures.Future()
        with _outstanding_lock:
            _outstanding.add(future)
        _jobs.put((priority, next(_order), (future, fn, args, kwargs)))
        return future

    def _run(self) -> None:
        _block_signals()
        _local.generation = self.generation
        while _worker is self:
            item = _jobs.get()
            if _worker is not self:
                # Replaced while it waited: the job is its successor's.
                _jobs.put(item)
                return
            future, fn, args, kwargs = item[2]
            if not future.set_running_or_notify_cancel():
                _settle(future)
                continue
            self.future, self.started_at = future, time.monotonic()
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:  # handed to whoever polls it
                future.set_exception(exc)
            else:
                future.set_result(result)
            finally:
                self.future = None
                _settle(future)


_worker: _Worker | None = None
_generation = 0
# Each worker thread's generation (worker_generation).
_local = threading.local()
_worker_lock = threading.Lock()


def worker() -> _Worker:
    """The planner worker, started on first use."""
    global _worker, _generation
    with _worker_lock:
        if _worker is None or not _worker.thread.is_alive():
            # None yet, or this is a forked child, which has no threads.
            _generation += 1
            _worker = _Worker(_generation)
            _worker.start()
        return _worker


def unstick(limit: float) -> str:
    """Abandon the worker when its job has run longer than ``limit``
    seconds, and start a fresh one, which plans on graph views of its own;
    a note of what was abandoned, or "". The abandoned daemon thread may
    finish its job or hang: either way it then ends, and its result is
    never read (its plan's owner gives it up, planner-timeout). Jobs still
    queued go to the fresh worker."""
    global _worker, _generation
    with _worker_lock:
        current = _worker
        if (current is None or current.future is None or
                time.monotonic() - current.started_at <= limit):
            return ""
        _generation += 1
        _worker = _Worker(_generation)
        _worker.start()
        # Its job's result is never read: it no longer makes the planner
        # busy.
        if current.future is not None:
            _settle(current.future)
    note = (f"planner worker {current.generation} ran one job for more than "
            f"{limit:.0f} s; worker {_generation} started")
    log.warning("%s", note)
    return note


def generation() -> int:
    """The current worker's generation (its graph views' key)."""
    return worker().generation


def worker_generation() -> int | None:
    """The generation of the worker running this code, or None off any
    worker (on the loop)."""
    return getattr(_local, "generation", None)


def on_worker() -> bool:
    """Whether this code runs on a planner worker."""
    return worker_generation() is not None


def busy() -> bool:
    """Whether a job is running or queued that a plan submitted now would
    wait for: a dropped (cancelled) job still queued does not count. A job
    counts from its submission until it ends, with no gap while the
    worker takes it from the queue."""
    with _outstanding_lock:
        return any(not future.cancelled() for future in _outstanding)


def submit(fn: Callable[..., Any], /, *args: Any,
           priority: int = PRIORITY_OTHER,
           **kwargs: Any) -> concurrent.futures.Future:
    """Run ``fn(*args, **kwargs)`` on the planner worker."""
    return worker().submit(fn, *args, priority=priority, **kwargs)


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
        # Set when its plan is dropped: the plan's probes stop early.
        object.__setattr__(self, "cancelled", threading.Event())
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
    the lag and what runs as the loop resumes (``describe``: the stalled
    code has returned by then, but a tick's task is usually the same),
    which the dashboard turns into an alert (alerts.loop_stall). A
    suspended process reads as a stall too. The worst lag seen is kept in
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
                parameters = inspect.signature(recorder).parameters.values()
            except (TypeError, ValueError):
                parameters = ()
            timed = any(parameter.name == "save_timeout" or
                        parameter.kind is parameter.VAR_KEYWORD
                        for parameter in parameters)
            options = {"save_timeout": self.SAVE_TIMEOUT} if timed else {}
            try:
                recorder(STALL_ACTION, detail, **options)
            except Exception:
                log.exception("event loop stall not recorded")
        return True

    async def run(self) -> None:
        while True:
            started = self.clock()
            await asyncio.sleep(self.interval)
            self.observe(max(0.0, self.clock() - started - self.interval))
