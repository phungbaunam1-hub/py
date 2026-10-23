using FlowDesk.Core;

namespace FlowDesk.Infrastructure.Services;

/// <summary>
/// An explicitly simulated feed with seeded prices, ten minutes of generated history and a
/// 150 ms real-time cadence. It does not authenticate with Rithmic or read a real account.
/// Operations are serialized; feed events run on the worker thread and must be marshalled by UI.
/// </summary>
public sealed class DemoRithmicPluginService(int seed = 73021) : IRithmicPluginService
{
    private readonly SemaphoreSlim _gate = new(1, 1);
    private CancellationTokenSource? _pumpCancellation;
    private Task? _pump;
    private volatile bool _connected;
    private int _disposed;
    private readonly TaskCompletionSource _disposalCompletion = new(TaskCreationOptions.RunContinuationsAsynchronously);

    public event EventHandler<ConnectionStatus>? StatusChanged;
    public event EventHandler<TradeTick>? TradeReceived;
    public event EventHandler<DepthSnapshot>? DepthReceived;
    public event EventHandler<AccountInfo>? AccountReceived;

    public async Task ConnectAsync(ConnectionOptions options, CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(options);
        ThrowIfDisposed();
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            ThrowIfDisposed();
            if (_connected)
                return;

            PublishStatus(ConnectionState.Connecting, "DEMO — starting simulated feed; no Rithmic authentication.");
            try
            {
                await Task.Delay(150, cancellationToken).ConfigureAwait(false);
                cancellationToken.ThrowIfCancellationRequested();
                ThrowIfDisposed();
                _connected = true;
                PublishStatus(ConnectionState.Connected, "DEMO connected — generated prices and simulated account only.");
                Publish(AccountReceived, new AccountInfo("DEMO-SIMULATED", "DEMO — simulated, no broker account", null));
            }
            catch (OperationCanceledException)
            {
                _connected = false;
                PublishStatus(ConnectionState.Disconnected, "DEMO connection canceled.");
                throw;
            }
        }
        finally
        {
            _gate.Release();
        }
    }

    public async Task SubscribeAsync(Instrument instrument, CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(instrument);
        ValidateInstrument(instrument);
        ThrowIfDisposed();
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            ThrowIfDisposed();
            if (!_connected)
                throw new InvalidOperationException("Connect to the Demo feed before subscribing.");

            await StopPumpAsync().ConfigureAwait(false);
            cancellationToken.ThrowIfCancellationRequested();
            _pumpCancellation = new CancellationTokenSource();
            var token = _pumpCancellation.Token;
            _pump = Task.Run(() => RunPumpAsync(instrument, token), CancellationToken.None);
        }
        finally
        {
            _gate.Release();
        }
    }

    public async Task DisconnectAsync(CancellationToken cancellationToken)
    {
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            // Once cleanup starts, finish it even if the caller cancels: no orphan feed survives.
            await StopPumpAsync().ConfigureAwait(false);
            _connected = false;
            if (Volatile.Read(ref _disposed) == 0)
                PublishStatus(ConnectionState.Disconnected, "DEMO disconnected.");
        }
        finally
        {
            _gate.Release();
        }
    }

    public async ValueTask DisposeAsync()
    {
        if (Interlocked.Exchange(ref _disposed, 1) != 0)
        {
            await _disposalCompletion.Task.ConfigureAwait(false);
            return;
        }

        try
        {
            await _gate.WaitAsync().ConfigureAwait(false);
            try
            {
                await StopPumpAsync().ConfigureAwait(false);
                _connected = false;
                PublishStatus(ConnectionState.Disconnected, "DEMO feed disposed.");
                StatusChanged = null;
                TradeReceived = null;
                DepthReceived = null;
                AccountReceived = null;
            }
            finally
            {
                _gate.Release();
            }
            _disposalCompletion.TrySetResult();
        }
        catch (Exception exception)
        {
            _disposalCompletion.TrySetException(exception);
            throw;
        }
    }

    private async Task StopPumpAsync()
    {
        if (_pumpCancellation is null)
            return;

        _pumpCancellation.Cancel();
        try
        {
            if (_pump is not null)
                await _pump.ConfigureAwait(false);
        }
        catch (OperationCanceledException) when (_pumpCancellation.IsCancellationRequested)
        {
        }
        finally
        {
            _pumpCancellation.Dispose();
            _pumpCancellation = null;
            _pump = null;
        }
    }

    private async Task RunPumpAsync(Instrument instrument, CancellationToken token)
    {
        try
        {
            var random = new Random(seed);
            var tick = instrument.TickSize;
            var basePrice = Math.Max(tick * 200, 6000m - 6000m % tick);
            var mid = basePrice;
            var index = 0;
            var historyStart = DateTimeOffset.UtcNow.AddMinutes(-10);
            for (; index < 600; index++)
            {
                token.ThrowIfCancellationRequested();
                EmitFrame(instrument, historyStart.AddSeconds(index), random, index, basePrice, ref mid, token);
                if (index % 32 == 0)
                    await Task.Yield();
            }

            token.ThrowIfCancellationRequested();
            using var timer = new PeriodicTimer(TimeSpan.FromMilliseconds(150));
            while (await timer.WaitForNextTickAsync(token).ConfigureAwait(false))
                EmitFrame(instrument, DateTimeOffset.UtcNow, random, index++, basePrice, ref mid, token);
        }
        catch (OperationCanceledException) when (token.IsCancellationRequested)
        {
        }
        catch (Exception exception)
        {
            _connected = false;
            PublishStatus(ConnectionState.Faulted, "DEMO feed failed: " + exception.GetType().Name + ". Reconnect to restart.");
        }
    }

    private void EmitFrame(Instrument instrument, DateTimeOffset timestamp, Random random, int index,
                           decimal basePrice, ref decimal mid, CancellationToken token)
    {
        var tick = instrument.TickSize;
        var direction = random.Next(2) == 0 ? -1 : 1;
        var step = random.Next(0, 3) * direction;
        // Mean reversion keeps even long sessions bounded and every price on the tick lattice.
        mid = Math.Clamp(mid + step * tick, basePrice - 60 * tick, basePrice + 60 * tick);

        var bids = new List<BookLevel>(24);
        for (var level = 0; level < 12; level++)
        {
            bids.Add(new BookLevel(BookSide.Bid, mid - (level + 1) * tick,
                                  level == 4 ? 1200 : random.Next(10, 220)));
            bids.Add(new BookLevel(BookSide.Ask, mid + level * tick,
                                  level == 6 ? 1800 : random.Next(10, 220)));
        }
        token.ThrowIfCancellationRequested();
        Publish(DepthReceived, new DepthSnapshot(instrument, timestamp, bids.ToArray()));

        // At regular intervals, generate a burst across four prices on one aggressor side.
        // This is observable sweep-like demo data, never a claim about a real market order.
        var burst = index % 31 == 0 ? 4 : 1;
        var aggressor = direction > 0 ? AggressorSide.Buy : AggressorSide.Sell;
        for (var print = 0; print < burst; print++)
        {
            token.ThrowIfCancellationRequested();
            var price = direction > 0 ? mid + print * tick : mid - (print + 1) * tick;
            var size = burst > 1 ? random.Next(90, 180)
                                : index % 13 == 0 ? random.Next(100, 650) : random.Next(1, 45);
            Publish(TradeReceived, new TradeTick(instrument, timestamp.AddMilliseconds(print * 8), price, size, aggressor));
        }
    }

    private void PublishStatus(ConnectionState state, string message) =>
        Publish(StatusChanged, new ConnectionStatus(state, message));

    private void Publish<T>(EventHandler<T>? handlers, T value)
    {
        if (handlers is null)
            return;
        foreach (EventHandler<T> subscriber in handlers.GetInvocationList())
        {
            try { subscriber(this, value); }
            catch (Exception) { /* Isolate observers so one UI callback cannot terminate the feed. */ }
        }
    }

    private void ThrowIfDisposed() => ObjectDisposedException.ThrowIf(Volatile.Read(ref _disposed) != 0, this);

    private static void ValidateInstrument(Instrument instrument)
    {
        if (string.IsNullOrWhiteSpace(instrument.Symbol) || string.IsNullOrWhiteSpace(instrument.Exchange))
            throw new ArgumentException("Instrument symbol and exchange are required.", nameof(instrument));
        if (instrument.TickSize <= 0 || instrument.TickSize > 1_000_000m)
            throw new ArgumentOutOfRangeException(nameof(instrument), "Tick size must be greater than zero and at most 1,000,000.");
    }
}
