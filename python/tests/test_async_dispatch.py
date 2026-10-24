from collections import deque
import threading
from types import SimpleNamespace

import pytest

from aimt_orderflow.async_dispatch import (AsyncOrderDispatcher, DispatchClosed, DispatchQueueFull,
    DispatchRejectedError, DuplicateRequestError, IndeterminateExecutionError)
from aimt_orderflow.mt5_bridge import install
from original import AI_MT_Trading as legacy


@pytest.fixture
def dispatchers():
    created = []

    def make(**kwargs):
        value = AsyncOrderDispatcher(**kwargs)
        created.append(value)
        return value

    yield make
    for value in created:
        value.shutdown(wait=True, timeout=3)


def test_submit_returns_without_waiting_for_native_request_and_preserves_native_ack(dispatchers):
    pool = dispatchers(max_workers=1)
    entered, release = threading.Event(), threading.Event()

    def send():
        entered.set()
        assert release.wait(3)
        return False, 'broker rejected; not a fill'

    handle = pool.submit('terminal-a', send)
    assert entered.wait(2) and not handle.done()
    assert pool.status(handle.id).state == 'running'
    release.set()
    result = handle.result(2)
    assert result.state == 'completed'
    assert result.value == (False, 'broker rejected; not a fill')
    assert result.submitted_at_ms <= result.started_at_ms <= result.finished_at_ms


def test_same_terminal_fifo_and_independent_terminals_execute_concurrently(dispatchers):
    pool = dispatchers(max_workers=2)
    a_started, b_started, release = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def first():
        calls.append('a1')
        a_started.set()
        assert release.wait(3)

    a1 = pool.submit('a', first)
    assert a_started.wait(2)
    a2 = pool.submit('a', lambda: calls.append('a2'))
    b = pool.submit('b', lambda: (b_started.set(), release.wait(3)))
    assert b_started.wait(2)
    assert calls == ['a1'] and a2.state == 'queued'
    release.set()
    assert all(handle.result(2).state == 'completed' for handle in (a1, a2, b))
    assert calls == ['a1', 'a2']


def test_queue_and_terminal_limits_reject_without_dispatching(dispatchers):
    pool = dispatchers(max_workers=1, max_pending=2, max_per_key=1)
    entered, release = threading.Event(), threading.Event()
    running = pool.submit('a', lambda: (entered.set(), release.wait(3)))
    assert entered.wait(2)
    with pytest.raises(DispatchQueueFull):
        pool.submit('a', lambda: pytest.fail('per-terminal overflow executed'))
    queued = pool.submit('b', lambda: True)
    with pytest.raises(DispatchQueueFull):
        pool.submit('c', lambda: pytest.fail('global overflow executed'))
    release.set()
    running.result(2)
    assert queued.result(2).value is True


def test_cancel_only_queued_entries_leaves_existing_position_management(dispatchers):
    pool = dispatchers(max_workers=1)
    started, release = threading.Event(), threading.Event()
    active = pool.submit('a', lambda: (started.set(), release.wait(3)))
    assert started.wait(2)
    entry = pool.submit('a', lambda: pytest.fail('cancelled entry executed'), metadata={'entry': True})
    manage = pool.submit('a', lambda: 'closed existing position', metadata={'entry': False})
    assert pool.cancel_pending('selection changed', predicate=lambda metadata: metadata.get('entry')) == 1
    assert entry.result(1).state == 'cancelled'
    assert not active.cancel() and not entry.cancel()
    release.set()
    active.result(2)
    assert manage.result(2).value == 'closed existing position'


def test_shutdown_atomically_cancels_queue_without_resending_running_request(dispatchers):
    pool = dispatchers(max_workers=1)
    started, release = threading.Event(), threading.Event()
    active = pool.submit('a', lambda: (started.set(), release.wait(3)))
    assert started.wait(2)
    queued = [pool.submit('a', lambda: pytest.fail('shutdown queue executed')) for _ in range(10)]
    assert not pool.shutdown(wait=False)
    assert all(handle.result(1).state == 'cancelled' for handle in queued)
    with pytest.raises(DispatchClosed):
        pool.submit('b', lambda: None)
    release.set()
    active.result(2)
    assert pool.shutdown(wait=True)


@pytest.mark.parametrize('guard', [lambda: False, lambda: (False, 'context changed'),
    lambda: (_ for _ in ()).throw(TimeoutError('validator failed before send'))])
def test_failed_scope_validation_rejects_before_operation_not_unknown(dispatchers, guard):
    pool = dispatchers(max_workers=1)
    result = pool.submit('a', lambda: pytest.fail('rejected guard sent order'), guard=guard).result(2)
    assert result.state == 'rejected'
    assert not pool.snapshot()['paused_keys']


def test_guard_is_rechecked_after_waiting_in_queue(dispatchers):
    pool = dispatchers(max_workers=1)
    started, release = threading.Event(), threading.Event()
    valid = [True]
    active = pool.submit('a', lambda: (started.set(), release.wait(3)))
    assert started.wait(2)
    queued = pool.submit('a', lambda: pytest.fail('stale entry sent'), guard=lambda: valid[0])
    valid[0] = False
    release.set()
    active.result(2)
    assert queued.result(2).state == 'rejected'


def test_unknown_outcome_pauses_key_before_next_job_and_requires_explicit_resume(dispatchers):
    callback_started, release_callback = threading.Event(), threading.Event()

    def callback(result):
        if result.state == 'unknown':
            callback_started.set()
            assert release_callback.wait(3)

    pool = dispatchers(max_workers=2, on_result=callback)
    started, release_send = threading.Event(), threading.Event()

    def uncertain():
        started.set()
        assert release_send.wait(3)
        raise TimeoutError('no authoritative reply')

    first = pool.submit('a', uncertain, metadata={'entry': True})
    assert started.wait(2)
    next_entry = pool.submit('a', lambda: pytest.fail('unknown request replayed under new ID'), metadata={'entry': True})
    management = pool.submit('a', lambda: 'reconcile existing positions', metadata={'entry': False})
    release_send.set()
    assert first.result(2).state == 'unknown'
    assert callback_started.wait(2)
    assert next_entry.result(2).state == 'rejected'
    assert management.result(2).value == 'reconcile existing positions'
    assert pool.snapshot()['paused_keys'] == ['a']
    assert pool.entries_paused('a') and not pool.entries_paused('b')
    with pytest.raises(DispatchRejectedError):
        pool.submit('a', lambda: None, metadata={'entry': True})
    release_callback.set()
    assert pool.resume_entries('a') == 1
    assert not pool.entries_paused('a')
    assert pool.submit('a', lambda: 'new explicit request', metadata={'entry': True}).result(2).state == 'completed'


def test_idempotency_does_not_replay_even_after_bounded_history_eviction(dispatchers):
    pool = dispatchers(max_workers=1, history_limit=1)
    sent = []
    first = pool.submit('a', lambda: sent.append('one'))
    first.result(2)
    duplicate = pool.submit('a', lambda: sent.append('duplicate'), request_id=first.id)
    assert duplicate is first and sent == ['one']
    second = pool.submit('a', lambda: sent.append('two'))
    second.result(2)
    assert len(pool.snapshot()['jobs']) == 1
    with pytest.raises(DuplicateRequestError):
        pool.submit('a', lambda: sent.append('expired replay'), request_id=first.id)
    with pytest.raises(DuplicateRequestError):
        pool.submit('a', lambda: sent.append('foreign ID'), request_id='external-nonce')
    assert sent == ['one', 'two']


def test_callbacks_are_isolated_and_metadata_is_an_owned_sanitized_snapshot(dispatchers):
    entered, release = threading.Event(), threading.Event()

    def broken(_result):
        raise SystemExit('bad callback')

    pool = dispatchers(max_workers=1, on_result=broken)
    metadata = {'entry': True, 'account': {'name': 'A', 'password': 'DO_NOT_EXPORT'}}
    first = pool.submit('a', lambda: (entered.set(), release.wait(3)), metadata=metadata)
    assert entered.wait(2)
    first.add_done_callback(broken)
    metadata['account']['name'] = 'changed'
    release.set()
    assert first.result(2).metadata == {'entry': True, 'account': {'name': 'A'}}
    assert pool.submit('a', lambda: 123).result(2).value == 123
    assert pool.callback_errors >= 2
    with pytest.raises(ValueError):
        pool.submit('a', lambda: None, metadata={'widget': object()})


@pytest.fixture
def bridge():
    install(legacy)
    return legacy


class Connection:
    def __init__(self, reply=(True, (True, 'ack ticket 123'), []), responds=True):
        self.reply, self.responds, self.sent = reply, responds, []

    def send(self, message):
        self.sent.append(message)

    def poll(self, timeout):
        return self.responds

    def recv(self):
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def worker(bridge, connection=None):
    hub = bridge.MT5Hub({'mt5_path': ''}, lambda _: None)
    value = bridge.MT5Worker(hub, 'test-terminal')
    value.conn = connection or Connection()
    value.alive = lambda: True
    value.killed = False

    def kill():
        value.killed = True

    value.kill = kill
    return hub, value


def test_scope_guard_is_checked_after_rpc_lock_wait_immediately_before_send(bridge):
    hub, rpc = worker(bridge)
    entered, done = threading.Event(), threading.Event()
    allowed, errors = [True], []
    rpc.lock.acquire()

    def send():
        try:
            with hub.order_send_guard(lambda: (allowed[0], 'selected chart changed')):
                entered.set()
                rpc.call('order', 'XAUUSD', 'BUY', .01, 0, 0)
        except Exception as error:
            errors.append(error)
        finally:
            done.set()

    thread = threading.Thread(target=send)
    thread.start()
    assert entered.wait(2)
    allowed[0] = False
    rpc.lock.release()
    assert done.wait(2)
    thread.join(2)
    assert len(errors) == 1 and isinstance(errors[0], DispatchRejectedError)
    assert not rpc.conn.sent and not rpc.killed


def test_guard_is_checked_after_worker_startup_and_does_not_block_management(bridge):
    hub, rpc = worker(bridge)
    allowed = [True]
    rpc.alive = lambda: False
    rpc.start = lambda: allowed.__setitem__(0, False)
    with hub.order_send_guard(lambda: allowed[0]):
        with pytest.raises(DispatchRejectedError):
            rpc.call('pending', 'XAUUSD')
        assert rpc.call('close_pos', {'ticket': 1}) == (True, 'ack ticket 123')
    assert [call[0] for call in rpc.conn.sent] == ['close_pos']


@pytest.mark.parametrize('connection', [Connection(responds=False), Connection(reply=EOFError('pipe lost'))])
def test_lost_mutating_rpc_reply_is_unknown_and_is_never_resent(bridge, connection):
    _hub, rpc = worker(bridge, connection)
    with pytest.raises(IndeterminateExecutionError):
        rpc.call('order', 'XAUUSD')
    assert len(connection.sent) == 1 and rpc.killed


def test_native_read_error_remains_normal_error_and_broker_ack_is_unchanged(bridge):
    _hub, rpc = worker(bridge)
    assert rpc.call('order', 'XAUUSD') == (True, 'ack ticket 123')
    rpc.conn = Connection(reply=(False, 'bad symbol', []))
    with pytest.raises(RuntimeError, match='bad symbol'):
        rpc.call('rates', 'INVALID')


def api(responses):
    replies, calls = deque(responses), []

    def send(request):
        calls.append(request)
        value = replies.popleft()
        if isinstance(value, Exception):
            raise value
        return value

    return SimpleNamespace(order_send=send, TRADE_ACTION_DEAL=1, TRADE_ACTION_PENDING=5, calls=calls)


def reply(code, **extra):
    fields = dict(retcode=code, order=123, deal=456, request_id=789, volume=.01, price=100,
                  bid=99.9, ask=100, comment='broker\nmessage')
    fields.update(extra)
    return SimpleNamespace(**fields)


@pytest.mark.parametrize('response', [None, TimeoutError('SDK timed out'), OSError('connection lost'),
                                    reply(10012), reply(10031), reply(10009.5)])
def test_native_unknown_or_missing_result_has_one_attempt_and_unknown_receipt(bridge, monkeypatch, response):
    native = api([response, reply(10009)])
    monkeypatch.setattr(bridge, 'mt5', native, raising=False)
    local = bridge.MT5Local({}, lambda _: None)
    with pytest.raises(IndeterminateExecutionError):
        local._send({'action': 1, 'symbol': 'XAUUSD', 'volume': .01}, (1, 2, 3))
    assert len(native.calls) == 1
    receipt = local.trade_receipt()
    assert receipt['state'] == 'unknown' and receipt['accepted'] is False


def test_only_explicit_invalid_fill_retries_and_native_receipt_is_preserved(bridge, monkeypatch):
    native = api([reply(10030), reply(10009)])
    monkeypatch.setattr(bridge, 'mt5', native, raising=False)
    local = bridge.MT5Local({}, lambda _: None)
    ok, result = local._send({'action': 1, 'symbol': 'XAUUSD', 'volume': .01}, (1, 2, 3))
    assert ok and result.retcode == 10009
    assert [call['type_filling'] for call in native.calls] == [1, 2]
    receipt = local.trade_receipt()
    assert receipt['state'] == 'filled' and receipt['fully_filled']
    assert (receipt['retcode'], receipt['order'], receipt['deal'], receipt['request_id']) == (10009, 123, 456, 789)
    assert receipt['comment'] == 'brokermessage'
    receipt['attempts'].clear()
    assert len(local.trade_receipt()['attempts']) == 2


@pytest.mark.parametrize('code,action,state,fully_filled', [(10008, 1, 'placed', False),
    (10010, 1, 'partially_filled', False), (10009, 5, 'placed', False)])
def test_placed_partial_and_pending_done_are_accepted_without_claiming_full_fill(bridge, monkeypatch, code, action, state, fully_filled):
    native = api([reply(code)])
    monkeypatch.setattr(bridge, 'mt5', native, raising=False)
    local = bridge.MT5Local({}, lambda _: None)
    assert local._send({'action': action, 'symbol': 'XAUUSD'}, (1, 2, 3))[0]
    assert local.trade_receipt()['state'] == state
    assert local.trade_receipt()['fully_filled'] is fully_filled
    assert len(native.calls) == 1


def test_invalid_fill_followed_by_no_reply_becomes_unknown_not_rejected(bridge, monkeypatch):
    native = api([reply(10030), None, reply(10009)])
    monkeypatch.setattr(bridge, 'mt5', native, raising=False)
    local = bridge.MT5Local({}, lambda _: None)
    with pytest.raises(IndeterminateExecutionError):
        local._send({'action': 1}, (1, 2, 3))
    receipt = local.trade_receipt()
    assert receipt['state'] == 'unknown' and receipt['retcode'] is None and len(native.calls) == 2


def test_explicit_native_rejection_does_not_try_a_different_fill(bridge, monkeypatch):
    native = api([reply(10006), reply(10009)])
    monkeypatch.setattr(bridge, 'mt5', native, raising=False)
    local = bridge.MT5Local({}, lambda _: None)
    assert local._send({'action': 1}, (1, 2, 3))[0] is False
    assert local.trade_receipt()['state'] == 'rejected' and len(native.calls) == 1


def test_terminal_path_aliases_share_one_worker_key(bridge, tmp_path):
    hub = bridge.MT5Hub({'mt5_path': ''}, lambda _: None)
    assert hub.key_for({'path': r'C:\Terminal\terminal64.exe'}) == hub.key_for({'path': r'c:\Terminal\.\terminal64.exe'})
    terminal = tmp_path / 'terminal64.exe'
    terminal.write_text('fixture')
    alias = tmp_path / 'alias.exe'
    alias.symlink_to(terminal)
    assert hub.key_for({'path': str(terminal)}) == hub.key_for({'path': str(alias)})
    assert hub.key_for({}) == 'default'


def test_demo_guard_runs_under_session_lock_and_receipts_are_explicitly_simulated(bridge):
    broker = bridge.DemoBroker(bridge.load_cfg(), lambda _: None)
    with broker.session({'name': 'A'}):
        with broker.order_send_guard(lambda: False):
            with pytest.raises(DispatchRejectedError):
                broker.order('XAUUSD', 'BUY', .01, 0, 0)
        assert not broker.st['pos']
        with broker.order_send_guard(lambda: True):
            assert broker.order('XAUUSD', 'BUY', .01, 0, 0)[0]
        receipt = broker.trade_receipt()
        assert receipt['mode'] == 'demo' and receipt['retcode'] is None
        assert receipt['state'] == 'filled' and len(broker.st['pos']) == 1
    assert 'trade_receipt' in bridge.RPC_METHODS
