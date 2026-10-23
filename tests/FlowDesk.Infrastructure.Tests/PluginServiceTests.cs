using System.Collections.Concurrent;
using FlowDesk.Core;
using FlowDesk.Infrastructure.Services;
using Xunit;

namespace FlowDesk.Infrastructure.Tests;

public sealed class PluginServiceTests
{
    private static readonly Instrument Es = new("ESZ6", "CME", 0.25m);
    private static readonly ConnectionOptions Options = new("not-a-real-user");

    [Fact]
    public async Task DemoClearlyLabelsConnectionAndAccountWithoutCopyingUsername()
    {
        await using var service = new DemoRithmicPluginService();
        var states = new List<ConnectionStatus>();
        AccountInfo? account = null;
        service.StatusChanged += (_, value) => states.Add(value);
        service.AccountReceived += (_, value) => account = value;
        await service.ConnectAsync(Options, CancellationToken.None);
        Assert.Equal(new[] { ConnectionState.Connecting, ConnectionState.Connected }, states.Select(x => x.State));
        Assert.All(states, status => Assert.Contains("DEMO", status.Message));
        Assert.NotNull(account);
        Assert.Equal("DEMO-SIMULATED", account.AccountId);
        Assert.Null(account.CashBalance);
        Assert.DoesNotContain(Options.Username, account.AccountId);
    }

    [Fact]
    public async Task CancelDuringDemoHandshakeDoesNotConnectAndCanRetry()
    {
        await using var service = new DemoRithmicPluginService();
        var states = new List<ConnectionState>();
        var accountCount = 0;
        service.StatusChanged += (_, value) => states.Add(value.State);
        service.AccountReceived += (_, _) => accountCount++;
        using var cancellation = new CancellationTokenSource(TimeSpan.FromMilliseconds(10));
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => service.ConnectAsync(Options, cancellation.Token));
        Assert.DoesNotContain(ConnectionState.Connected, states);
        Assert.Equal(ConnectionState.Disconnected, states[^1]);
        Assert.Equal(0, accountCount);
        await service.ConnectAsync(Options, CancellationToken.None);
        Assert.Equal(ConnectionState.Connected, states[^1]);
        Assert.Equal(1, accountCount);
    }

    [Fact]
    public async Task SubscribeRequiresSessionAndValidInstrument()
    {
        await using var service = new DemoRithmicPluginService();
        await Assert.ThrowsAsync<InvalidOperationException>(() => service.SubscribeAsync(Es, CancellationToken.None));
        await Assert.ThrowsAsync<ArgumentException>(() => service.SubscribeAsync(new Instrument("", "CME", .25m), CancellationToken.None));
        await Assert.ThrowsAsync<ArgumentOutOfRangeException>(() => service.SubscribeAsync(Es with { TickSize = 0 }, CancellationToken.None));
        await service.ConnectAsync(Options, CancellationToken.None);
        using var cancellation = new CancellationTokenSource();
        cancellation.Cancel();
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => service.SubscribeAsync(Es, cancellation.Token));
    }

    [Fact]
    public async Task SeededDemoGeneratesTickAlignedReplayDepthAndOrderFlow()
    {
        await using var service = new DemoRithmicPluginService(123);
        var replay = new ConcurrentQueue<TradeTick>();
        var books = new ConcurrentQueue<DepthSnapshot>();
        var completed = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        service.TradeReceived += (_, trade) =>
        {
            replay.Enqueue(trade);
            if (replay.Count >= 660) completed.TrySetResult();
        };
        service.DepthReceived += (_, depth) => books.Enqueue(depth);
        await service.ConnectAsync(Options, CancellationToken.None);
        var start = DateTimeOffset.UtcNow;
        await service.SubscribeAsync(Es, CancellationToken.None);
        await completed.Task.WaitAsync(TimeSpan.FromSeconds(5));
        await service.DisconnectAsync(CancellationToken.None);
        var ticks = replay.ToArray();
        Assert.True(ticks[0].Timestamp < start.AddMinutes(-9));
        Assert.True(ticks[^1].Timestamp > start.AddSeconds(-5));
        Assert.All(ticks, trade =>
        {
            Assert.True(trade.Price > 0);
            Assert.Equal(0m, trade.Price % Es.TickSize);
            Assert.True(trade.Size > 0);
            Assert.Equal(Es, trade.Instrument);
            Assert.NotEqual(AggressorSide.Unknown, trade.Aggressor);
        });
        Assert.All(books, depth =>
        {
            Assert.Equal(24, depth.Levels.Count);
            Assert.All(depth.Levels, level =>
            {
                Assert.True(level.Price > 0 && level.Size > 0);
                Assert.Equal(0m, level.Price % Es.TickSize);
            });
            Assert.True(depth.Levels.Where(x => x.Side == BookSide.Bid).Max(x => x.Price) <
                        depth.Levels.Where(x => x.Side == BookSide.Ask).Min(x => x.Price));
        });
        var processor = new OrderFlowProcessor(OrderFlowSettings.Default);
        foreach (var trade in ticks) processor.OnTrade(trade);
        foreach (var depth in books) processor.OnDepth(depth);
        var result = processor.GetSnapshot();
        Assert.True(result.Candles.Count >= 10);
        Assert.NotEmpty(result.BigTrades);
        Assert.NotEmpty(result.Signals);
        Assert.Contains(result.Book, level => level.Size >= 1200);
    }

    [Fact]
    public async Task DemoCallbacksDriveCompleteOrderFlowPipelineAndPreserveExactDelta()
    {
        await using var service = new DemoRithmicPluginService(73021);
        var processor = new OrderFlowProcessor(OrderFlowSettings.Default);
        var callbackErrors = new ConcurrentQueue<Exception>();
        var replayCompleted = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var printCount = 0;
        decimal expectedDelta = 0;

        service.DepthReceived += (_, depth) =>
        {
            try { processor.OnDepth(depth); }
            catch (Exception exception) { callbackErrors.Enqueue(exception); }
        };
        service.TradeReceived += (_, trade) =>
        {
            try
            {
                processor.OnTrade(trade);
                expectedDelta += trade.Aggressor switch
                {
                    AggressorSide.Buy => trade.Size,
                    AggressorSide.Sell => -trade.Size,
                    _ => 0m
                };
            }
            catch (Exception exception) { callbackErrors.Enqueue(exception); }

            if (Interlocked.Increment(ref printCount) >= 660)
                replayCompleted.TrySetResult();
        };

        await service.ConnectAsync(Options, CancellationToken.None);
        await service.SubscribeAsync(Es, CancellationToken.None);
        await replayCompleted.Task.WaitAsync(TimeSpan.FromSeconds(10));
        // Joining the worker makes the independent delta total and processor snapshot quiescent.
        await service.DisconnectAsync(CancellationToken.None);

        Assert.Empty(callbackErrors);
        Assert.True(Volatile.Read(ref printCount) >= 660);
        var snapshot = processor.GetSnapshot();
        Assert.True(snapshot.Candles.Count >= 10);
        Assert.NotEmpty(snapshot.BigTrades);
        Assert.Contains(snapshot.Book, level => level.Side == BookSide.Bid);
        Assert.Contains(snapshot.Book, level => level.Side == BookSide.Ask);
        Assert.Contains(snapshot.Signals, signal => signal.Kind.EndsWith(" wall", StringComparison.Ordinal));
        Assert.Contains(snapshot.Signals, signal => signal.Kind.EndsWith(" sweep", StringComparison.Ordinal));
        Assert.Equal(expectedDelta, snapshot.CumulativeDelta);
        Assert.Equal(expectedDelta, snapshot.Candles.Sum(candle => candle.Delta));
    }

    [Fact]
    public async Task SameSeedReplaysSamePricesAndSizes()
    {
        static async Task<(decimal Price, decimal Size, AggressorSide Side)[]> ReadSample()
        {
            await using var feed = new DemoRithmicPluginService(987);
            var sample = new ConcurrentQueue<(decimal, decimal, AggressorSide)>();
            var ready = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
            feed.TradeReceived += (_, trade) =>
            {
                sample.Enqueue((trade.Price, trade.Size, trade.Aggressor));
                if (sample.Count >= 80) ready.TrySetResult();
            };
            await feed.ConnectAsync(Options, CancellationToken.None);
            await feed.SubscribeAsync(Es, CancellationToken.None);
            await ready.Task.WaitAsync(TimeSpan.FromSeconds(5));
            await feed.DisconnectAsync(CancellationToken.None);
            return sample.Take(80).ToArray();
        }
        Assert.Equal(await ReadSample(), await ReadSample());
    }

    [Fact]
    public async Task DisconnectJoinsPumpAndReconnectCanStreamAgain()
    {
        await using var service = new DemoRithmicPluginService();
        var count = 0;
        var first = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        service.TradeReceived += (_, _) => { Interlocked.Increment(ref count); first.TrySetResult(); };
        await service.ConnectAsync(Options, CancellationToken.None);
        await service.SubscribeAsync(Es, CancellationToken.None);
        await first.Task.WaitAsync(TimeSpan.FromSeconds(5));
        await service.DisconnectAsync(CancellationToken.None);
        var disconnectedCount = Volatile.Read(ref count);
        await Task.Delay(200);
        Assert.Equal(disconnectedCount, Volatile.Read(ref count));
        var resumed = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        service.TradeReceived += (_, _) => resumed.TrySetResult();
        await service.ConnectAsync(Options, CancellationToken.None);
        await service.SubscribeAsync(Es, CancellationToken.None);
        await resumed.Task.WaitAsync(TimeSpan.FromSeconds(5));
        Assert.True(Volatile.Read(ref count) > disconnectedCount);
    }

    [Fact]
    public async Task SubscriberExceptionsAreIsolatedFromOtherObserversAndLifecycle()
    {
        await using var service = new DemoRithmicPluginService();
        service.StatusChanged += (_, _) => throw new InvalidOperationException("broken observer");
        service.AccountReceived += (_, _) => throw new InvalidOperationException("broken observer");
        service.TradeReceived += (_, _) => throw new InvalidOperationException("broken observer");
        service.DepthReceived += (_, _) => throw new InvalidOperationException("broken observer");
        var first = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        service.TradeReceived += (_, _) => first.TrySetResult();
        await service.ConnectAsync(Options, CancellationToken.None);
        await service.SubscribeAsync(Es, CancellationToken.None);
        await first.Task.WaitAsync(TimeSpan.FromSeconds(5));
        await service.DisconnectAsync(CancellationToken.None);
    }

    [Fact]
    public async Task DisposeStopsFeedIsRepeatableAndRejectsNewConnections()
    {
        var service = new DemoRithmicPluginService();
        var count = 0;
        var first = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        service.TradeReceived += (_, _) => { Interlocked.Increment(ref count); first.TrySetResult(); };
        await service.ConnectAsync(Options, CancellationToken.None);
        await service.SubscribeAsync(Es, CancellationToken.None);
        await first.Task.WaitAsync(TimeSpan.FromSeconds(5));
        await Task.WhenAll(service.DisposeAsync().AsTask(), service.DisposeAsync().AsTask());
        var disposedCount = Volatile.Read(ref count);
        await Task.Delay(200);
        Assert.Equal(disposedCount, Volatile.Read(ref count));
        await service.DisposeAsync();
        await Assert.ThrowsAsync<ObjectDisposedException>(() => service.ConnectAsync(Options, CancellationToken.None));
        await Assert.ThrowsAsync<ObjectDisposedException>(() => service.SubscribeAsync(Es, CancellationToken.None));
    }

    [Fact]
    public async Task ReplacingSubscriptionJoinsPreviousInstrumentPump()
    {
        await using var service = new DemoRithmicPluginService();
        var trades = new ConcurrentQueue<TradeTick>();
        var first = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        service.TradeReceived += (_, value) => { trades.Enqueue(value); first.TrySetResult(); };
        await service.ConnectAsync(Options, CancellationToken.None);
        await service.SubscribeAsync(Es, CancellationToken.None);
        await first.Task.WaitAsync(TimeSpan.FromSeconds(5));
        var nq = new Instrument("NQZ6", "CME", .25m);
        await service.SubscribeAsync(nq, CancellationToken.None);
        var boundary = trades.Count;
        await Task.Delay(200);
        await service.DisconnectAsync(CancellationToken.None);
        Assert.NotEmpty(trades.Skip(boundary));
        Assert.All(trades.Skip(boundary), value => Assert.Equal(nq, value.Instrument));
    }

    [Fact]
    public async Task LiveConnectorCannotReportConnectedOrEmitFabricatedMarketAndAccountData()
    {
        await using var service = new RithmicPluginService();
        var states = new List<ConnectionStatus>();
        var fabricatedEvents = 0;
        service.StatusChanged += (_, value) => states.Add(value);
        service.TradeReceived += (_, _) => fabricatedEvents++;
        service.DepthReceived += (_, _) => fabricatedEvents++;
        service.AccountReceived += (_, _) => fabricatedEvents++;
        var error = await Assert.ThrowsAsync<RithmicSdkUnavailableException>(() => service.ConnectAsync(Options, CancellationToken.None));
        Assert.Contains("No connection or authentication was attempted", error.Message);
        Assert.Equal(new[] { ConnectionState.Connecting, ConnectionState.Faulted }, states.Select(x => x.State));
        Assert.DoesNotContain(states, x => x.State == ConnectionState.Connected);
        await Assert.ThrowsAsync<InvalidOperationException>(() => service.SubscribeAsync(Es, CancellationToken.None));
        await service.DisconnectAsync(CancellationToken.None);
        Assert.Equal(ConnectionState.Disconnected, states[^1].State);
        Assert.Equal(0, fabricatedEvents);
    }

    [Fact]
    public async Task CanceledLiveConnectDoesNotTransitionAndObserverCannotMaskSdkError()
    {
        await using var service = new RithmicPluginService();
        var states = new List<ConnectionState>();
        service.StatusChanged += (_, _) => throw new Exception("observer error");
        service.StatusChanged += (_, value) => states.Add(value.State);
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => service.ConnectAsync(Options, new CancellationToken(true)));
        Assert.Empty(states);
        await Assert.ThrowsAsync<RithmicSdkUnavailableException>(() => service.ConnectAsync(Options, CancellationToken.None));
        Assert.Equal(ConnectionState.Faulted, states[^1]);
    }
}
