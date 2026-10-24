"""Bounded background dispatch; terminal RPCs remain synchronous and serialized.

This is an application queue, not MQL5 ``CTrade.SetAsyncMode``/``OrderSendAsync``.
An operation must establish its captured broker/account session on the dispatch thread.
``completed`` means that operation returned; its native broker acknowledgment remains
in ``value`` and is the authority for acceptance/fill status. Lost replies are ``unknown``
and are never replayed automatically. Queued operations can be cancelled; an operation
already sent to MT5 cannot be rolled back by cancelling its application task.
"""
from collections import Counter, OrderedDict, deque
from concurrent.futures import Future
import copy
from dataclasses import dataclass
import math
import threading
import time
import uuid
import weakref


class DispatchError(RuntimeError):
    pass


class DispatchQueueFull(DispatchError):
    pass


class DispatchClosed(DispatchError):
    pass


class DuplicateRequestError(DispatchError):
    pass


class DispatchRejectedError(DispatchError):
    pass


class IndeterminateExecutionError(DispatchError):
    """MT5 may have acted, but an authoritative result was not received."""


def _text(value, limit=500):
    return ''.join(char for char in str(value) if char.isprintable())[:limit]


def _metadata(value, depth=0):
    """Copy bounded plain data; task result metadata must not retain UI objects/secrets."""
    if depth > 5:
        raise ValueError('Dispatch metadata nesting exceeds its limit.')
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError('Dispatch metadata must contain finite numbers.')
        return value
    if isinstance(value, str):
        return _text(value, 2048)
    if isinstance(value, dict):
        if len(value) > 64:
            raise ValueError('Dispatch metadata contains too many fields.')
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError('Dispatch metadata keys must be strings.')
            key = _text(key, 128)
            if any(secret in key.lower() for secret in ('password', 'secret', 'api_key', 'apikey', 'access_token')):
                continue
            result[key] = _metadata(item, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > 128:
            raise ValueError('Dispatch metadata list exceeds its limit.')
        return [_metadata(item, depth + 1) for item in value]
    raise ValueError('Dispatch metadata must be plain Python data.')


def check_guard(guard):
    """A guard returns True, or (True, reason); all other outcomes reject the task."""
    if guard is None:
        return
    outcome = guard()
    reason = 'Execution context changed before dispatch.'
    if isinstance(outcome, tuple) and outcome:
        allowed = outcome[0] is True
        if len(outcome) > 1 and outcome[1]:
            reason = _text(outcome[1])
    else:
        allowed = outcome is True
    if not allowed:
        raise DispatchRejectedError(reason)


@dataclass(frozen=True, slots=True)
class DispatchResult:
    request_id: str
    terminal_key: str
    state: str
    value: object
    error: str
    metadata: dict
    submitted_at_ms: int
    started_at_ms: int | None
    finished_at_ms: int | None

    def as_dict(self, include_value=False):
        result = dict(request_id=self.request_id, terminal_key=self.terminal_key, state=self.state,
                      error=self.error, metadata=copy.deepcopy(self.metadata),
                      submitted_at_ms=self.submitted_at_ms, started_at_ms=self.started_at_ms,
                      finished_at_ms=self.finished_at_ms)
        if include_value:
            result['value'] = self.value
        return result


class DispatchHandle:
    def __init__(self, dispatcher, job):
        self._dispatcher = weakref.ref(dispatcher)
        self._job = job

    @property
    def request_id(self):
        return self._job.request_id

    @property
    def id(self):
        return self.request_id

    @property
    def state(self):
        owner = self._dispatcher()
        if owner is None:
            return self._job.state
        with owner._condition:
            return self._job.state

    def done(self):
        return self._job.future.done()

    def result(self, timeout=None):
        return self._job.future.result(timeout)

    def cancel(self, reason='Cancelled before dispatch.'):
        owner = self._dispatcher()
        return bool(owner and owner.cancel(self.request_id, reason=reason))

    def add_done_callback(self, callback):
        def completed(_future):
            try:
                callback(self)
            except BaseException:
                owner = self._dispatcher()
                if owner is not None:
                    with owner._condition:
                        owner.callback_errors += 1
        self._job.future.add_done_callback(completed)


@dataclass
class _Job:
    request_id: str
    terminal_key: str
    operation: object
    guard: object
    metadata: dict
    callback: object
    submitted_at_ms: int
    future: Future
    state: str = 'queued'
    started_at_ms: int | None = None
    finished_at_ms: int | None = None
    value: object = None
    error: str = ''
    handle: object = None

    def snapshot(self):
        return DispatchResult(self.request_id, self.terminal_key, self.state, self.value, self.error,
                              copy.deepcopy(self.metadata), self.submitted_at_ms,
                              self.started_at_ms, self.finished_at_ms)


class AsyncOrderDispatcher:
    """Fixed worker pool, FIFO per terminal, bounded pending work and result history.

    Callbacks receive one terminal DispatchResult, outside scheduler locks. They may run
    on a worker or the thread cancelling a queued task; use a Qt signal to update UI.
    IDs are generated here. Reusing a returned ID retrieves its original handle; even
    after history eviction the same issued ID is rejected, never sent again.
    Mark new-entry jobs with metadata['entry']=True so unknown outcomes pause subsequent
    entries until explicit resume; closing/managing existing positions can still proceed.
    """
    def __init__(self, max_workers=4, max_pending=128, max_per_key=32, history_limit=1024, on_result=None):
        for name, value, upper in (('max_workers', max_workers, 32), ('max_pending', max_pending, 4096),
                                   ('max_per_key', max_per_key, 4096), ('history_limit', history_limit, 10000)):
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= upper:
                raise ValueError(f'{name} must be an integer from 1 to {upper}.')
        if on_result is not None and not callable(on_result):
            raise TypeError('on_result must be callable.')
        self.max_workers, self.max_pending = max_workers, max_pending
        self.max_per_key, self.history_limit = max_per_key, history_limit
        self._on_result = on_result
        self._condition = threading.Condition(threading.RLock())
        self._jobs = OrderedDict()
        self._queues = {}
        self._ready, self._ready_set, self._active = deque(), set(), set()
        self._paused_keys = set()
        self._per_key = Counter()
        self._pending, self._counter = 0, 0
        self._prefix = uuid.uuid4().hex
        self._closed = False
        self._totals = Counter()
        self.callback_errors = 0
        self._threads = [threading.Thread(target=self._work, daemon=True, name=f'MT5-Dispatch-{index + 1}')
                         for index in range(max_workers)]
        for worker in self._threads:
            worker.start()

    @staticmethod
    def _now():
        return int(time.time() * 1000)

    def submit(self, terminal_key, operation, *, guard=None, metadata=None, request_id=None, on_result=None):
        if not isinstance(terminal_key, str) or not terminal_key or len(terminal_key) > 1024 or not all(c.isprintable() for c in terminal_key):
            raise ValueError('A printable terminal key is required.')
        if not callable(operation) or guard is not None and not callable(guard):
            raise TypeError('operation and optional guard must be callable.')
        if on_result is not None and not callable(on_result):
            raise TypeError('on_result must be callable.')
        safe_metadata = _metadata({} if metadata is None else metadata)
        if not isinstance(safe_metadata, dict):
            raise ValueError('Dispatch metadata must be a dictionary.')
        with self._condition:
            if request_id is not None:
                previous = self._jobs.get(request_id)
                if previous is not None and previous.terminal_key == terminal_key:
                    return previous.handle
                raise DuplicateRequestError('An existing or expired request ID cannot create a new operation.')
            if self._closed:
                raise DispatchClosed('Order dispatcher has closed.')
            if terminal_key in self._paused_keys and safe_metadata.get('entry') is True:
                raise DispatchRejectedError('Terminal có yêu cầu chưa xác định; kiểm tra MT5 và resume trước khi mở lệnh mới.')
            if self._pending >= self.max_pending or self._per_key[terminal_key] >= self.max_per_key:
                raise DispatchQueueFull('Order queue is full; no operation was sent.')
            self._counter += 1
            identifier = f'{self._prefix}:{self._counter}'
            job = _Job(identifier, terminal_key, operation, guard, safe_metadata, on_result, self._now(), Future())
            job.handle = DispatchHandle(self, job)
            self._jobs[identifier] = job
            self._queues.setdefault(terminal_key, deque()).append(job)
            self._pending += 1
            self._per_key[terminal_key] += 1
            if terminal_key not in self._active and terminal_key not in self._ready_set:
                self._ready.append(terminal_key)
                self._ready_set.add(terminal_key)
            self._condition.notify_all()
            return job.handle

    def _work(self):
        while True:
            with self._condition:
                while not self._ready:
                    if self._closed and not self._pending:
                        return
                    self._condition.wait()
                key = self._ready.popleft()
                self._ready_set.discard(key)
                queue = self._queues.get(key)
                if not queue:
                    continue
                job = queue.popleft()
                self._active.add(key)
                job.state, job.started_at_ms = 'running', self._now()
            state, value, error = 'completed', None, ''
            try:
                with self._condition:
                    if key in self._paused_keys and job.metadata.get('entry') is True:
                        raise DispatchRejectedError('Terminal có yêu cầu chưa xác định; lệnh mới bị tạm dừng cho tới khi resume.')
                try:
                    check_guard(job.guard)
                except DispatchRejectedError:
                    raise
                except BaseException as failure:
                    raise DispatchRejectedError('Scope validator failed before dispatch: ' + type(failure).__name__) from failure
                value = job.operation()
            except DispatchRejectedError as failure:
                state, error = 'rejected', _text(failure)
            except (IndeterminateExecutionError, TimeoutError, EOFError, OSError) as failure:
                state, error = 'unknown', _text(type(failure).__name__ + ': ' + str(failure))
            except BaseException as failure:
                state, error = 'failed', _text(type(failure).__name__ + ': ' + str(failure))
            with self._condition:
                if state == 'unknown':
                    # Latch before releasing this key, so a queued new ID cannot race an
                    # asynchronous GUI status callback and duplicate an unacknowledged entry.
                    self._paused_keys.add(key)
                self._active.discard(key)
                result, callbacks = self._finish(job, state, value, error)
                if self._queues.get(key):
                    self._ready.append(key)
                    self._ready_set.add(key)
                else:
                    self._queues.pop(key, None)
                self._condition.notify_all()
            self._publish(job, result, callbacks)

    def _finish(self, job, state, value, error):
        job.state, job.value, job.error = state, value, error
        job.finished_at_ms = self._now()
        self._pending -= 1
        self._per_key[job.terminal_key] -= 1
        if not self._per_key[job.terminal_key]:
            del self._per_key[job.terminal_key]
        self._totals[state] += 1
        callbacks = (self._on_result, job.callback)
        job.operation = job.guard = job.callback = None
        result = job.snapshot()
        finished = sum(item.state not in ('queued', 'running') for item in self._jobs.values())
        if finished > self.history_limit:
            for identifier, item in list(self._jobs.items()):
                if item.state not in ('queued', 'running'):
                    del self._jobs[identifier]
                    finished -= 1
                    if finished <= self.history_limit:
                        break
        return result, callbacks

    def _publish(self, job, result, callbacks):
        job.future.set_result(result)
        for callback in callbacks:
            if callback is not None:
                try:
                    callback(result)
                except BaseException:
                    with self._condition:
                        self.callback_errors += 1

    def cancel(self, request_id, reason='Cancelled before dispatch.'):
        with self._condition:
            job = self._jobs.get(request_id)
            if job is None or job.state != 'queued':
                return False
            result, callbacks = self._cancel_queued(job, reason)
            self._condition.notify_all()
        self._publish(job, result, callbacks)
        return True

    def _cancel_queued(self, job, reason):
        queue = self._queues[job.terminal_key]
        queue.remove(job)
        if not queue and job.terminal_key not in self._active:
            self._queues.pop(job.terminal_key, None)
            self._ready_set.discard(job.terminal_key)
            self._ready = deque(key for key in self._ready if key != job.terminal_key)
        return self._finish(job, 'cancelled', None, _text(reason))

    def cancel_pending(self, reason='Cancelled before dispatch.', predicate=None):
        if predicate is not None and not callable(predicate):
            raise TypeError('predicate must be callable.')
        with self._condition:
            candidates = [(job.request_id, copy.deepcopy(job.metadata)) for job in self._jobs.values() if job.state == 'queued']
        cancelled = 0
        for request_id, metadata in candidates:
            if predicate is None or predicate(metadata):
                cancelled += int(self.cancel(request_id, reason=reason))
        return cancelled

    def status(self, request_id):
        with self._condition:
            job = self._jobs.get(request_id)
            return job.snapshot() if job is not None else None

    def pause_entries(self, terminal_key):
        with self._condition:
            self._paused_keys.add(terminal_key)

    def entries_paused(self, terminal_key):
        with self._condition:
            return terminal_key in self._paused_keys

    def resume_entries(self, key=None):
        with self._condition:
            before = len(self._paused_keys)
            if key is None:
                self._paused_keys.clear()
            else:
                self._paused_keys.discard(key)
            return before - len(self._paused_keys)

    def snapshot(self):
        with self._condition:
            return {'closed': self._closed, 'pending': self._pending, 'running': len(self._active),
                    'queued': self._pending - len(self._active), 'max_workers': self.max_workers,
                    'max_pending': self.max_pending, 'totals': dict(self._totals),
                    'paused_keys': sorted(self._paused_keys),
                    'jobs': [job.snapshot().as_dict() for job in self._jobs.values()]}

    def shutdown(self, wait=False, cancel_pending=True, timeout=2):
        publications = []
        with self._condition:
            self._closed = True
            if cancel_pending:
                for job in list(self._jobs.values()):
                    if job.state == 'queued':
                        result, callbacks = self._cancel_queued(job, 'Dispatcher closed before operation was sent.')
                        publications.append((job, result, callbacks))
            self._condition.notify_all()
        for job, result, callbacks in publications:
            self._publish(job, result, callbacks)
        if wait:
            deadline = time.monotonic() + max(0.0, float(timeout))
            for worker in self._threads:
                if worker is not threading.current_thread():
                    worker.join(max(0.0, deadline - time.monotonic()))
        return not any(worker.is_alive() for worker in self._threads)
