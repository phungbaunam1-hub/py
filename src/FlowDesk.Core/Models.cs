namespace FlowDesk.Core;

public enum ConnectionState { Disconnected, Connecting, Connected, Faulted }
public enum AggressorSide { Unknown, Buy, Sell }
public enum BookSide { Bid, Ask }
public enum FeedMode { Demo, RithmicPlugin }

public sealed record ConnectionOptions(string Username);
public sealed record Instrument(string Symbol, string Exchange, decimal TickSize);
public sealed record TradeTick(Instrument Instrument, DateTimeOffset Timestamp, decimal Price,
    decimal Size, AggressorSide Aggressor);
public sealed record BookLevel(BookSide Side, decimal Price, decimal Size);

/// <summary>A complete replacement of the subscribed instrument's displayed depth, not a delta.</summary>
public sealed record DepthSnapshot(Instrument Instrument, DateTimeOffset Timestamp,
    IReadOnlyList<BookLevel> Levels);
public sealed record ConnectionStatus(ConnectionState State, string Message);
public sealed record AccountInfo(string AccountId, string Status, decimal? CashBalance);

/// <summary>
/// Adapter boundary for the demo feed or a version-matched official Rithmic plugin adapter.
/// Callbacks may arrive on background threads. The caller must dispatch UI mutations.
/// This read-only contract deliberately contains no order-entry methods.
/// </summary>
public interface IRithmicPluginService : IAsyncDisposable
{
    event EventHandler<ConnectionStatus>? StatusChanged;
    event EventHandler<TradeTick>? TradeReceived;
    event EventHandler<DepthSnapshot>? DepthReceived;
    event EventHandler<AccountInfo>? AccountReceived;
    Task ConnectAsync(ConnectionOptions options, CancellationToken cancellationToken);
    Task SubscribeAsync(Instrument instrument, CancellationToken cancellationToken);
    Task DisconnectAsync(CancellationToken cancellationToken);
}

public sealed record Candle(DateTimeOffset OpenTime, decimal Open, decimal High, decimal Low,
    decimal Close, decimal Volume, decimal Delta);
public sealed record FlowSignal(DateTimeOffset Timestamp, string Kind, decimal Price,
    decimal Size, string Description);

public sealed record OrderFlowSettings(decimal MinimumTradeSize, decimal MinimumWallSize,
    decimal MinimumSweepVolume, int MinimumSweepLevels, int SweepWindowMilliseconds,
    int CandleSeconds)
{
    public static OrderFlowSettings Default { get; } = new(25m, 150m, 100m, 3, 1000, 60);

    public void Validate()
    {
        if (MinimumTradeSize < 0) throw new ArgumentOutOfRangeException(nameof(MinimumTradeSize));
        if (MinimumWallSize < 0) throw new ArgumentOutOfRangeException(nameof(MinimumWallSize));
        if (MinimumSweepVolume < 0) throw new ArgumentOutOfRangeException(nameof(MinimumSweepVolume));
        if (MinimumSweepLevels is < 2 or > 1000)
            throw new ArgumentOutOfRangeException(nameof(MinimumSweepLevels), "Use 2–1000 distinct traded prices.");
        if (SweepWindowMilliseconds is < 1 or > 60000)
            throw new ArgumentOutOfRangeException(nameof(SweepWindowMilliseconds), "Use a window of 1–60000 milliseconds.");
        if (CandleSeconds is < 1 or > 86400)
            throw new ArgumentOutOfRangeException(nameof(CandleSeconds), "Use a timeframe of 1–86400 seconds.");
    }
}

public sealed record OrderFlowSnapshot(IReadOnlyList<Candle> Candles,
    IReadOnlyList<TradeTick> BigTrades, IReadOnlyList<BookLevel> Book,
    IReadOnlyList<FlowSignal> Signals, decimal CumulativeDelta, DateTimeOffset? LastEventTime);
