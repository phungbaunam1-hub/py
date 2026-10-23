namespace FlowDesk.Core;

/// <summary>
/// Thread-safe, bounded, instrument-scoped order-flow accumulator. Aggressor classification must
/// come from the feed; unknown trades contribute volume but no delta or sweep evidence.
/// No historical candles, resting-order identities, hidden liquidity or trade deduplication are inferred.
/// </summary>
public sealed class OrderFlowProcessor
{
    public const int MaximumCandles = 600;
    public const int MaximumRecentTrades = 4000;
    public const int MaximumSignals = 300;
    public const int MaximumBookLevelsPerSide = 1000;
    private const int MaximumSweepTrades = 10000;

    private readonly object _gate = new();
    private readonly SortedDictionary<long, CandleState> _candles = new();
    private readonly Queue<TradeTick> _recentTrades = new();
    private readonly Queue<FlowSignal> _signals = new();
    private readonly Dictionary<(BookSide, decimal), decimal> _walls = new();
    private readonly SweepWindow _buySweep = new();
    private readonly SweepWindow _sellSweep = new();
    private BookLevel[] _book = [];
    private OrderFlowSettings _settings;
    private Instrument? _instrument;
    private DateTimeOffset? _lastEventTime;
    private DateTimeOffset? _lastTradeTime;
    private DateTimeOffset? _lastDepthTime;
    private decimal _cumulativeDelta;

    public OrderFlowProcessor(OrderFlowSettings settings)
    {
        ArgumentNullException.ThrowIfNull(settings);
        settings.Validate();
        _settings = settings;
    }

    /// <summary>
    /// Changes filters immediately. A different timeframe clears candles because retained OHLC bars
    /// cannot be losslessly resampled; new candles start with the next trade. Sweep state restarts.
    /// Existing historical signals keep the thresholds in force when they were detected.
    /// </summary>
    public void UpdateSettings(OrderFlowSettings settings)
    {
        ArgumentNullException.ThrowIfNull(settings);
        settings.Validate();
        lock (_gate)
        {
            if (settings.CandleSeconds != _settings.CandleSeconds) _candles.Clear();
            _settings = settings;
            _buySweep.Clear();
            _sellSweep.Clear();
            _walls.Clear();
        }
    }

    public void OnTrade(TradeTick trade)
    {
        ArgumentNullException.ThrowIfNull(trade);
        ValidateInstrument(trade.Instrument);
        if (trade.Size <= 0) throw new ArgumentOutOfRangeException(nameof(trade), "Trade size must be positive.");
        if (!Enum.IsDefined(trade.Aggressor)) throw new ArgumentOutOfRangeException(nameof(trade));
        lock (_gate)
        {
            BindInstrument(trade.Instrument);
            var bucket = BucketStart(trade.Timestamp, _settings.CandleSeconds);
            // Beyond the retained history, a late trade cannot be represented consistently.
            if (_candles.Count == MaximumCandles && bucket < _candles.First().Key) return;

            var delta = trade.Aggressor switch
            {
                AggressorSide.Buy => trade.Size,
                AggressorSide.Sell => -trade.Size,
                _ => 0m
            };
            _cumulativeDelta += delta;
            if (_candles.TryGetValue(bucket, out var candle)) candle.Add(trade, delta);
            else _candles.Add(bucket, new CandleState(bucket, trade, delta));
            while (_candles.Count > MaximumCandles) _candles.Remove(_candles.First().Key);
            _recentTrades.Enqueue(trade);
            while (_recentTrades.Count > MaximumRecentTrades) _recentTrades.Dequeue();

            // Late prints still repair their retained bar, but never create a "current" sweep.
            if (_lastTradeTime is null || trade.Timestamp >= _lastTradeTime)
            {
                DetectSweep(trade);
                _lastTradeTime = trade.Timestamp;
            }
            AdvanceEventTime(trade.Timestamp);
        }
    }

    public void OnDepth(DepthSnapshot snapshot)
    {
        ArgumentNullException.ThrowIfNull(snapshot);
        ValidateInstrument(snapshot.Instrument);
        ArgumentNullException.ThrowIfNull(snapshot.Levels);
        // Validate and copy before taking the state lock. Snapshots must be owned/immutable at dispatch.
        var levels = new Dictionary<(BookSide, decimal), decimal>();
        foreach (var level in snapshot.Levels)
        {
            ArgumentNullException.ThrowIfNull(level);
            if (!Enum.IsDefined(level.Side) || level.Size < 0)
                throw new ArgumentOutOfRangeException(nameof(snapshot), "Depth requires a valid side and nonnegative size.");
            // Last occurrence wins; a zero size removes an earlier occurrence in this snapshot.
            if (level.Size == 0) levels.Remove((level.Side, level.Price));
            else levels[(level.Side, level.Price)] = level.Size;
        }
        lock (_gate)
        {
            BindInstrument(snapshot.Instrument);
            // An out-of-order full snapshot must not restore stale resting liquidity.
            if (_lastDepthTime is not null && snapshot.Timestamp < _lastDepthTime) return;
            _lastDepthTime = snapshot.Timestamp;
            _book = levels.Where(x => x.Key.Item1 == BookSide.Bid)
                .OrderByDescending(x => x.Key.Item2).Take(MaximumBookLevelsPerSide)
                .Concat(levels.Where(x => x.Key.Item1 == BookSide.Ask)
                    .OrderBy(x => x.Key.Item2).Take(MaximumBookLevelsPerSide))
                .Select(x => new BookLevel(x.Key.Item1, x.Key.Item2, x.Value)).ToArray();

            var currentWalls = _book.Where(x => x.Size >= _settings.MinimumWallSize).ToArray();
            foreach (var wall in currentWalls)
            {
                var key = (wall.Side, wall.Price);
                if (!_walls.TryGetValue(key, out var previous) || wall.Size >= previous * 1.25m)
                {
                    AddSignal(new FlowSignal(snapshot.Timestamp, $"{wall.Side} wall", wall.Price, wall.Size,
                        "Displayed depth above the wall threshold; liquidity may change or be cancelled."));
                }
            }
            _walls.Clear();
            foreach (var wall in currentWalls) _walls[(wall.Side, wall.Price)] = wall.Size;
            AdvanceEventTime(snapshot.Timestamp);
        }
    }

    /// <summary>Copies only the canonical bounded depth, without sorting the trade tape.</summary>
    public DepthSnapshot? GetDepthSnapshot()
    {
        lock (_gate)
        {
            return _instrument is not null && _lastDepthTime is { } time
                ? new DepthSnapshot(_instrument, time, _book.ToArray())
                : null;
        }
    }

    public OrderFlowSnapshot GetSnapshot()
    {
        lock (_gate)
        {
            return new OrderFlowSnapshot(_candles.Values.Select(x => x.Snapshot()).ToArray(),
                _recentTrades.Where(x => x.Size >= _settings.MinimumTradeSize)
                    .OrderByDescending(x => x.Timestamp).ToArray(),
                _book.ToArray(), _signals.Reverse().ToArray(), _cumulativeDelta, _lastEventTime);
        }
    }

    /// <summary>Clears the current instrument, chart, tape, depth and all derived session state.</summary>
    public void Reset()
    {
        lock (_gate)
        {
            _instrument = null;
            _candles.Clear();
            _recentTrades.Clear();
            _signals.Clear();
            _walls.Clear();
            _book = [];
            _buySweep.Clear();
            _sellSweep.Clear();
            _lastEventTime = _lastTradeTime = _lastDepthTime = null;
            _cumulativeDelta = 0;
        }
    }

    private void DetectSweep(TradeTick trade)
    {
        var oldest = trade.Timestamp.AddMilliseconds(-_settings.SweepWindowMilliseconds);
        _buySweep.Trim(oldest);
        _sellSweep.Trim(oldest);
        if (trade.Aggressor == AggressorSide.Unknown) return;
        var queue = trade.Aggressor == AggressorSide.Buy ? _buySweep : _sellSweep;
        queue.Add(trade);
        if (queue.Count < _settings.MinimumSweepLevels) return;
        var first = queue.First;
        var directional = trade.Aggressor == AggressorSide.Buy
            ? trade.Price > first.Price : trade.Price < first.Price;
        if (!directional) return;
        var volume = queue.Volume;
        var priceLevels = queue.PriceLevelCount;
        if (volume < _settings.MinimumSweepVolume || priceLevels < _settings.MinimumSweepLevels) return;
        AddSignal(new FlowSignal(trade.Timestamp, $"{trade.Aggressor} sweep", trade.Price, volume,
            $"Aggressive {trade.Aggressor.ToString().ToLowerInvariant()} prints across {priceLevels} prices in " +
            $"{_settings.SweepWindowMilliseconds} ms; trade-print heuristic, not proof of stop orders."));
        queue.Clear(); // Consume this detection window to avoid overlapping duplicate alerts.
    }

    private void AddSignal(FlowSignal signal)
    {
        _signals.Enqueue(signal);
        while (_signals.Count > MaximumSignals) _signals.Dequeue();
    }

    private void BindInstrument(Instrument instrument)
    {
        if (_instrument is null) _instrument = instrument;
        else if (_instrument != instrument)
            throw new InvalidOperationException("Reset the processor before changing instruments.");
    }

    private static void ValidateInstrument(Instrument instrument)
    {
        ArgumentNullException.ThrowIfNull(instrument);
        if (string.IsNullOrWhiteSpace(instrument.Symbol) || string.IsNullOrWhiteSpace(instrument.Exchange) ||
            instrument.TickSize <= 0)
            throw new ArgumentException("An instrument needs a symbol, exchange and positive tick size.", nameof(instrument));
    }

    private void AdvanceEventTime(DateTimeOffset timestamp)
    {
        if (_lastEventTime is null || timestamp > _lastEventTime) _lastEventTime = timestamp;
    }

    private static long BucketStart(DateTimeOffset timestamp, int seconds)
    {
        var unixTicks = timestamp.UtcTicks - DateTimeOffset.UnixEpoch.Ticks;
        var spanTicks = checked((long)seconds * TimeSpan.TicksPerSecond);
        var bucket = Math.DivRem(unixTicks, spanTicks, out var remainder);
        if (remainder < 0) bucket--;
        return checked(DateTimeOffset.UnixEpoch.Ticks + bucket * spanTicks);
    }

    private sealed class CandleState
    {
        private readonly long _openTime;
        private DateTimeOffset _firstTrade;
        private DateTimeOffset _lastTrade;
        private decimal _open, _high, _low, _close, _volume, _delta;

        public CandleState(long openTime, TradeTick trade, decimal delta)
        {
            _openTime = openTime;
            _firstTrade = _lastTrade = trade.Timestamp;
            _open = _high = _low = _close = trade.Price;
            _volume = trade.Size;
            _delta = delta;
        }

        public void Add(TradeTick trade, decimal delta)
        {
            _high = Math.Max(_high, trade.Price);
            _low = Math.Min(_low, trade.Price);
            if (trade.Timestamp < _firstTrade) { _firstTrade = trade.Timestamp; _open = trade.Price; }
            if (trade.Timestamp >= _lastTrade) { _lastTrade = trade.Timestamp; _close = trade.Price; }
            _volume += trade.Size;
            _delta += delta;
        }

        public Candle Snapshot() => new(new DateTimeOffset(_openTime, TimeSpan.Zero),
            _open, _high, _low, _close, _volume, _delta);
    }

    // Incremental totals keep the hot path amortized O(1), even at high print rates.
    private sealed class SweepWindow
    {
        private readonly Queue<TradeTick> _trades = new();
        private readonly Dictionary<decimal, int> _priceCounts = new();
        public decimal Volume { get; private set; }
        public int Count => _trades.Count;
        public int PriceLevelCount => _priceCounts.Count;
        public TradeTick First => _trades.Peek();

        public void Add(TradeTick trade)
        {
            _trades.Enqueue(trade);
            Volume += trade.Size;
            _priceCounts.TryGetValue(trade.Price, out var count);
            _priceCounts[trade.Price] = count + 1;
            while (_trades.Count > MaximumSweepTrades) RemoveFirst();
        }

        public void Trim(DateTimeOffset oldest)
        {
            while (_trades.TryPeek(out var trade) && trade.Timestamp < oldest) RemoveFirst();
        }

        public void Clear()
        {
            _trades.Clear();
            _priceCounts.Clear();
            Volume = 0;
        }

        private void RemoveFirst()
        {
            var trade = _trades.Dequeue();
            Volume -= trade.Size;
            if (_priceCounts[trade.Price] == 1) _priceCounts.Remove(trade.Price);
            else _priceCounts[trade.Price]--;
        }
    }
}
