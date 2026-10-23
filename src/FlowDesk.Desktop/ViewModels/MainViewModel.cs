using System.Collections.ObjectModel;
using System.Globalization;
using System.Windows.Media;
using System.Windows.Threading;
using FlowDesk.Core;
using FlowDesk.Infrastructure.Services;

namespace FlowDesk.Desktop.ViewModels;

public sealed record TradeDisplay(string Time, string Side, string Price, string Size);
public sealed record DepthDisplay(string Side, string Price, string Size, bool IsWall);
public sealed record SignalDisplay(string Time, string Kind, string Price, string Size, string Description);

/// <summary>
/// SDK callbacks feed the locked processor directly. Only the UI timer and dispatched
/// status notifications touch bindable properties or ObservableCollections.
/// </summary>
public sealed class MainViewModel : ObservableObject, IAsyncDisposable
{
    private readonly Dispatcher _dispatcher;
    private readonly DispatcherTimer _refreshTimer;
    private readonly SemaphoreSlim _lifecycle = new(1, 1);
    private readonly OrderFlowProcessor _processor = new(OrderFlowSettings.Default);
    private readonly object _depthLock = new();
    private readonly Queue<DepthSnapshot> _depthHistory = new();
    private IRithmicPluginService? _service;
    private CancellationTokenSource? _connectCancellation;
    private DepthSnapshot? _pendingDepth;
    private DateTimeOffset? _latestDepthTimestamp;
    private OrderFlowSettings _appliedSettings = OrderFlowSettings.Default;
    private Instrument? _activeInstrument;
    private bool _disposed;
    private ConnectionState _state;
    private FeedMode _selectedMode = FeedMode.Demo;
    private string _username = "";
    private string _symbol = "ES-DEMO";
    private string _exchange = "CME";
    private decimal _tickSize = 0.25m;
    private decimal _minimumTradeSize = 40;
    private decimal _minimumWallSize = 400;
    private decimal _minimumSweepVolume = 120;
    private int _minimumSweepLevels = 3;
    private int _sweepWindowMilliseconds = 750;
    private int _candleSeconds = 60;
    private string _statusText = "Disconnected";
    private Brush _statusColor = Brushes.SlateGray;
    private string _lastPriceText = "—";
    private string _deltaText = "0";
    private string _feedTimeText = "No ticks received";
    private string _accountText = "No account connected";
    private IReadOnlyList<Candle> _candles = Array.Empty<Candle>();
    private IReadOnlyList<TradeTick> _chartTrades = Array.Empty<TradeTick>();
    private IReadOnlyList<DepthSnapshot> _depthFrames = Array.Empty<DepthSnapshot>();

    public MainViewModel(Dispatcher dispatcher)
    {
        _dispatcher = dispatcher;
        ConnectCommand = new AsyncRelayCommand(ConnectAsync, ReportError, () => IsDisconnected && !_disposed);
        DisconnectCommand = new AsyncRelayCommand(DisconnectAsync, ReportError, () => !IsDisconnected && !_disposed);
        ApplySettingsCommand = new RelayCommand(ApplySettings);
        _refreshTimer = new DispatcherTimer(TimeSpan.FromMilliseconds(100), DispatcherPriority.Background,
            (_, _) => RefreshMarket(), dispatcher);
        Log("FlowDesk started. Demo uses generated data. Rithmic requires the official SDK and a verified adapter.");
        ApplySettings();
    }

    public FeedMode[] ModeOptions { get; } = Enum.GetValues<FeedMode>();
    public FeedMode SelectedMode
    {
        get => _selectedMode;
        set
        {
            if (!IsDisconnected || !Set(ref _selectedMode, value)) return;
            ResetDisplay();
            Notify(nameof(ModeDescription));
            Notify(nameof(FeedLabel));
        }
    }
    public string Username { get => _username; set => Set(ref _username, value); }
    public string Symbol { get => _symbol; set => Set(ref _symbol, value); }
    public string Exchange { get => _exchange; set => Set(ref _exchange, value); }
    public decimal TickSize { get => _tickSize; set => Set(ref _tickSize, value); }
    public decimal MinimumTradeSize { get => _minimumTradeSize; set => Set(ref _minimumTradeSize, value); }
    public decimal MinimumWallSize { get => _minimumWallSize; set => Set(ref _minimumWallSize, value); }
    public decimal MinimumSweepVolume { get => _minimumSweepVolume; set => Set(ref _minimumSweepVolume, value); }
    public int MinimumSweepLevels { get => _minimumSweepLevels; set => Set(ref _minimumSweepLevels, value); }
    public int SweepWindowMilliseconds { get => _sweepWindowMilliseconds; set => Set(ref _sweepWindowMilliseconds, value); }
    public int CandleSeconds { get => _candleSeconds; set => Set(ref _candleSeconds, value); }
    public bool IsDisconnected => _state is ConnectionState.Disconnected or ConnectionState.Faulted;
    public bool IsConnected => _state == ConnectionState.Connected;
    public string StatusText { get => _statusText; private set => Set(ref _statusText, value); }
    public Brush StatusColor { get => _statusColor; private set => Set(ref _statusColor, value); }
    public string LastPriceText { get => _lastPriceText; private set => Set(ref _lastPriceText, value); }
    public string DeltaText { get => _deltaText; private set => Set(ref _deltaText, value); }
    public string FeedTimeText { get => _feedTimeText; private set => Set(ref _feedTimeText, value); }
    public string AccountText { get => _accountText; private set => Set(ref _accountText, value); }
    public string FeedLabel => SelectedMode == FeedMode.Demo ? "DEMO · SYNTHETIC DATA" : "RITHMIC · ADAPTER REQUIRED";
    public string ModeDescription => SelectedMode == FeedMode.Demo
        ? "Generated replay + streaming ticks. No broker connection, account or orders."
        : "Live connection unavailable in this build: official Rithmic SDK and verified plugin adapter required.";
    public IReadOnlyList<Candle> Candles { get => _candles; private set => Set(ref _candles, value); }
    public IReadOnlyList<TradeTick> ChartTrades { get => _chartTrades; private set => Set(ref _chartTrades, value); }
    public IReadOnlyList<DepthSnapshot> DepthHistory { get => _depthFrames; private set => Set(ref _depthFrames, value); }
    public ObservableCollection<TradeDisplay> BigTrades { get; } = new();
    public ObservableCollection<DepthDisplay> DepthRows { get; } = new();
    public ObservableCollection<SignalDisplay> Signals { get; } = new();
    public ObservableCollection<string> Logs { get; } = new();
    public AsyncRelayCommand ConnectCommand { get; }
    public AsyncRelayCommand DisconnectCommand { get; }
    public RelayCommand ApplySettingsCommand { get; }

    private OrderFlowSettings ReadSettings() => new(MinimumTradeSize, MinimumWallSize,
        MinimumSweepVolume, MinimumSweepLevels, SweepWindowMilliseconds, CandleSeconds);

    private bool TryApplySettings()
    {
        try
        {
            var settings = ReadSettings();
            _processor.UpdateSettings(settings);
            _appliedSettings = settings;
            return true;
        }
        catch (ArgumentException exception)
        {
            Log($"Settings rejected: {exception.Message}");
            return false;
        }
    }

    private void ApplySettings()
    {
        if (!TryApplySettings()) return;
        Log($"Filters applied: trades ≥ {MinimumTradeSize}, walls ≥ {MinimumWallSize}, " +
            $"sweep ≥ {MinimumSweepVolume} across {MinimumSweepLevels} prices / {SweepWindowMilliseconds} ms; {CandleSeconds}s candles.");
        RefreshMarket();
    }

    private async Task ConnectAsync()
    {
        await _lifecycle.WaitAsync();
        try
        {
            if (_disposed || !IsDisconnected || !TryApplySettings()) return;
            if (string.IsNullOrWhiteSpace(Symbol) || string.IsNullOrWhiteSpace(Exchange) || TickSize <= 0)
            {
                Log("Enter a symbol, exchange and positive tick size.");
                return;
            }
            if (SelectedMode == FeedMode.RithmicPlugin && string.IsNullOrWhiteSpace(Username))
            {
                Log("Enter your Rithmic username. This build has no verified live adapter; no login is sent.");
                return;
            }
            await ReleaseServiceAsync();
            ResetDisplay();
            _activeInstrument = new Instrument(Symbol.Trim(), Exchange.Trim(), TickSize);
            _service = SelectedMode == FeedMode.Demo
                ? new DemoRithmicPluginService()
                : new RithmicPluginService();
            _service.StatusChanged += OnStatus;
            _service.TradeReceived += OnTrade;
            _service.DepthReceived += OnDepth;
            _service.AccountReceived += OnAccount;
            using var cancellation = new CancellationTokenSource(TimeSpan.FromSeconds(20));
            _connectCancellation = cancellation;
            SetStatus(new ConnectionStatus(ConnectionState.Connecting, "Starting selected data source…"));
            try
            {
                await _service.ConnectAsync(new ConnectionOptions(Username.Trim()), cancellation.Token);
                await _service.SubscribeAsync(_activeInstrument, cancellation.Token);
            }
            catch (OperationCanceledException)
            {
                await ReleaseServiceAsync();
                SetStatus(new ConnectionStatus(ConnectionState.Disconnected, "Connection cancelled or timed out."));
            }
            catch (Exception exception)
            {
                await ReleaseServiceAsync();
                SetStatus(new ConnectionStatus(ConnectionState.Faulted, exception.Message));
            }
            finally { _connectCancellation = null; }
        }
        finally { _lifecycle.Release(); }
    }

    private async Task DisconnectAsync()
    {
        _connectCancellation?.Cancel();
        await _lifecycle.WaitAsync();
        try
        {
            await ReleaseServiceAsync();
            SetStatus(new ConnectionStatus(ConnectionState.Disconnected, "Disconnected. Display retains the last received snapshot."));
        }
        finally { _lifecycle.Release(); }
    }

    private async Task ReleaseServiceAsync()
    {
        var service = _service;
        _service = null; // Queued callbacks from the old session are rejected by reference.
        if (service is null) return;
        service.StatusChanged -= OnStatus;
        service.TradeReceived -= OnTrade;
        service.DepthReceived -= OnDepth;
        service.AccountReceived -= OnAccount;
        try { await service.DisconnectAsync(CancellationToken.None); }
        finally { await service.DisposeAsync(); }
    }

    private void OnStatus(object? sender, ConnectionStatus status) => Dispatch(() =>
    {
        if (ReferenceEquals(sender, _service)) SetStatus(status);
    });

    private void OnAccount(object? sender, AccountInfo account) => Dispatch(() =>
    {
        if (!ReferenceEquals(sender, _service)) return;
        AccountText = $"{account.AccountId} · {account.Status}" +
            (account.CashBalance is decimal balance ? $" · {balance:N2}" : "");
    });

    private void OnTrade(object? sender, TradeTick trade)
    {
        if (!ReferenceEquals(sender, _service) || trade.Instrument != _activeInstrument) return;
        _processor.OnTrade(trade);
    }

    private void OnDepth(object? sender, DepthSnapshot depth)
    {
        if (!ReferenceEquals(sender, _service) || depth.Instrument != _activeInstrument) return;
        _processor.OnDepth(depth);
        var canonical = _processor.GetDepthSnapshot();
        if (canonical is null) return;
        lock (_depthLock)
        {
            if (_latestDepthTimestamp is null || canonical.Timestamp >= _latestDepthTimestamp)
            {
                _latestDepthTimestamp = canonical.Timestamp;
                _pendingDepth = canonical;
            }
        }
    }

    private void Dispatch(Action action)
    {
        if (_disposed || _dispatcher.HasShutdownStarted) return;
        if (_dispatcher.CheckAccess()) action();
        else _dispatcher.BeginInvoke(DispatcherPriority.DataBind, new Action(() =>
        {
            if (!_disposed) action();
        }));
    }

    private void SetStatus(ConnectionStatus status)
    {
        _state = status.State;
        StatusText = $"{status.State} · {status.Message}";
        StatusColor = status.State switch
        {
            ConnectionState.Connected => Brushes.MediumAquamarine,
            ConnectionState.Connecting => Brushes.Goldenrod,
            ConnectionState.Faulted => Brushes.IndianRed,
            _ => Brushes.SlateGray
        };
        Notify(nameof(IsDisconnected));
        Notify(nameof(IsConnected));
        ConnectCommand.RaiseCanExecuteChanged();
        DisconnectCommand.RaiseCanExecuteChanged();
        if (status.State is ConnectionState.Disconnected or ConnectionState.Faulted)
            AccountText = "No active account connection";
        Log(StatusText);
    }

    private void RefreshMarket()
    {
        if (_disposed) return;
        var snapshot = _processor.GetSnapshot();
        Candles = snapshot.Candles;
        ChartTrades = snapshot.BigTrades;
        if (snapshot.Candles.Count > 0) LastPriceText = Price(snapshot.Candles[^1].Close);
        DeltaText = snapshot.CumulativeDelta.ToString("+0;-0;0", CultureInfo.InvariantCulture);
        FeedTimeText = snapshot.LastEventTime is { } time
            ? $"Last event {time.UtcDateTime:HH:mm:ss.fff} UTC · {(DateTimeOffset.UtcNow - time).TotalSeconds:F1}s ago"
            : "No ticks received";
        Synchronize(BigTrades, snapshot.BigTrades.Take(100).Select(trade =>
            new TradeDisplay(Time(trade.Timestamp), trade.Aggressor.ToString(), Price(trade.Price), Number(trade.Size))));
        var visibleDepth = snapshot.Book.Where(x => x.Side == BookSide.Bid).Take(40)
            .Concat(snapshot.Book.Where(x => x.Side == BookSide.Ask).Take(40));
        Synchronize(DepthRows, visibleDepth.Select(level =>
            new DepthDisplay(level.Side.ToString(), Price(level.Price), Number(level.Size), level.Size >= _appliedSettings.MinimumWallSize)));
        Synchronize(Signals, snapshot.Signals.Take(100).Select(signal =>
            new SignalDisplay(Time(signal.Timestamp), signal.Kind, Price(signal.Price), Number(signal.Size), signal.Description)));
        lock (_depthLock)
        {
            if (_pendingDepth is not null)
            {
                _depthHistory.Enqueue(_pendingDepth);
                _pendingDepth = null;
                while (_depthHistory.Count > 240) _depthHistory.Dequeue();
                DepthHistory = _depthHistory.ToArray();
            }
        }
    }

    private void ResetDisplay()
    {
        _processor.Reset();
        _activeInstrument = null;
        lock (_depthLock) { _depthHistory.Clear(); _pendingDepth = null; _latestDepthTimestamp = null; }
        Candles = Array.Empty<Candle>();
        ChartTrades = Array.Empty<TradeTick>();
        DepthHistory = Array.Empty<DepthSnapshot>();
        BigTrades.Clear();
        DepthRows.Clear();
        Signals.Clear();
        LastPriceText = "—";
        DeltaText = "0";
        AccountText = "No account connected";
        FeedTimeText = "No ticks received";
    }

    private static void Synchronize<T>(ObservableCollection<T> target, IEnumerable<T> source)
    {
        var items = source.ToArray();
        if (target.SequenceEqual(items)) return;
        // Collections are small bounded view projections, refreshed at most 10 times/sec.
        target.Clear();
        foreach (var item in items) target.Add(item);
    }
    private static string Time(DateTimeOffset time) => time.UtcDateTime.ToString("HH:mm:ss.fff", CultureInfo.InvariantCulture);
    private static string Price(decimal value) => value.ToString("0.########", CultureInfo.InvariantCulture);
    private static string Number(decimal value) => value.ToString("0.##", CultureInfo.InvariantCulture);
    private void Log(string message)
    {
        Logs.Insert(0, $"{DateTimeOffset.UtcNow:HH:mm:ss} UTC  {message}");
        while (Logs.Count > 200) Logs.RemoveAt(Logs.Count - 1);
    }
    private void ReportError(Exception exception) => Log($"Operation failed: {exception.Message}");

    public async ValueTask DisposeAsync()
    {
        if (_disposed) return;
        _disposed = true;
        _refreshTimer.Stop();
        _connectCancellation?.Cancel();
        await _lifecycle.WaitAsync();
        try { await ReleaseServiceAsync(); }
        catch (Exception exception) { Log($"Shutdown: {exception.Message}"); }
        finally { _lifecycle.Release(); }
    }
}
