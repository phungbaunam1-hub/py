using FlowDesk.Core;
using Xunit;

namespace FlowDesk.Core.Tests;

public sealed class OrderFlowProcessorTests
{
    private static readonly Instrument Es = new("ESZ6", "CME", 0.25m);
    private static readonly DateTimeOffset Start = new(2026, 10, 23, 12, 0, 0, TimeSpan.Zero);

    private static TradeTick Tick(int milliseconds, decimal price = 6000m, decimal size = 10m,
        AggressorSide side = AggressorSide.Buy) => new(Es, Start.AddMilliseconds(milliseconds), price, size, side);

    [Fact]
    public void TradesBuildUtcCandlesWithCorrectBoundaryVolumeAndDelta()
    {
        var processor = new OrderFlowProcessor(OrderFlowSettings.Default);
        processor.OnTrade(Tick(0, 100m, 5m));
        processor.OnTrade(Tick(10_000, 102m, 3m, AggressorSide.Sell));
        processor.OnTrade(Tick(59_999, 99m, 4m, AggressorSide.Unknown));
        processor.OnTrade(Tick(60_000, 101m, 7m));
        var data = processor.GetSnapshot();
        Assert.Equal(2, data.Candles.Count);
        Assert.Equal(new Candle(Start, 100m, 102m, 99m, 99m, 12m, 2m), data.Candles[0]);
        Assert.Equal(Start.AddMinutes(1), data.Candles[1].OpenTime);
        Assert.Equal(9m, data.CumulativeDelta);
    }

    [Fact]
    public void EquivalentOffsetTimestampsShareOneUtcCandle()
    {
        var processor = new OrderFlowProcessor(OrderFlowSettings.Default);
        processor.OnTrade(new(Es, Start.ToOffset(TimeSpan.FromHours(7)), 100m, 1m, AggressorSide.Buy));
        processor.OnTrade(new(Es, Start.AddSeconds(10).ToOffset(TimeSpan.FromHours(-5)), 101m, 2m, AggressorSide.Sell));
        var candle = Assert.Single(processor.GetSnapshot().Candles);
        Assert.Equal(Start, candle.OpenTime);
        Assert.Equal(TimeSpan.Zero, candle.OpenTime.Offset);
        Assert.Equal(3m, candle.Volume);
        Assert.Equal(-1m, candle.Delta);
    }

    [Fact]
    public void LateTradesRepairChronologicalOpenCloseWithoutRegressingClock()
    {
        var processor = new OrderFlowProcessor(OrderFlowSettings.Default);
        processor.OnTrade(Tick(20_000, 102m));
        processor.OnTrade(Tick(50_000, 105m));
        processor.OnTrade(Tick(10_000, 99m, side: AggressorSide.Sell));
        processor.OnTrade(Tick(30_000, 103m));
        var result = processor.GetSnapshot();
        var candle = Assert.Single(result.Candles);
        Assert.Equal(99m, candle.Open);
        Assert.Equal(105m, candle.Close);
        Assert.Equal(Start.AddSeconds(50), result.LastEventTime);
    }

    [Fact]
    public void ChangingBigTradeThresholdFiltersRetainedTradesImmediately()
    {
        var processor = new OrderFlowProcessor(OrderFlowSettings.Default);
        processor.OnTrade(Tick(0, size: 24));
        processor.OnTrade(Tick(1, size: 25));
        processor.OnTrade(Tick(2, size: 100));
        Assert.Equal(2, processor.GetSnapshot().BigTrades.Count);
        processor.UpdateSettings(OrderFlowSettings.Default with { MinimumTradeSize = 100 });
        Assert.Equal(100m, Assert.Single(processor.GetSnapshot().BigTrades).Size);
        Assert.Equal(149m, Assert.Single(processor.GetSnapshot().Candles).Volume);
    }

    [Fact]
    public void SweepRequiresSameAggressorDistinctLevelsAndVolume()
    {
        var processor = new OrderFlowProcessor(OrderFlowSettings.Default);
        processor.OnTrade(Tick(0, 100m, 40m));
        processor.OnTrade(Tick(100, 100.25m, 40m));
        processor.OnTrade(Tick(200, 100.5m, 20m));
        var signal = Assert.Single(processor.GetSnapshot().Signals);
        Assert.Equal("Buy sweep", signal.Kind);
        Assert.Equal(100m, signal.Size);
        processor.OnTrade(Tick(250, 100.75m, 50m));
        Assert.Single(processor.GetSnapshot().Signals);
    }

    [Fact]
    public void RepeatedSinglePriceAndMixedAggressorsDoNotCreateSweep()
    {
        var processor = new OrderFlowProcessor(OrderFlowSettings.Default);
        processor.OnTrade(Tick(0, 100m, 100m));
        processor.OnTrade(Tick(100, 100m, 100m));
        processor.OnTrade(Tick(200, 100m, 100m));
        processor.OnTrade(Tick(300, 99.75m, 100m, AggressorSide.Sell));
        processor.OnTrade(Tick(400, 99.5m, 100m, AggressorSide.Unknown));
        Assert.Empty(processor.GetSnapshot().Signals);
    }

    [Fact]
    public void SweepDoesNotCombinePrintsOutsideWindowOrRetroactivePrints()
    {
        var processor = new OrderFlowProcessor(OrderFlowSettings.Default);
        processor.OnTrade(Tick(0, 100m, 40m));
        processor.OnTrade(Tick(100, 100.25m, 40m));
        processor.OnTrade(Tick(1101, 100.5m, 40m));
        processor.OnTrade(Tick(200, 100.75m, 100m));
        Assert.Empty(processor.GetSnapshot().Signals);
    }

    [Fact]
    public void SweepWindowIncludesExactBoundaryButExcludesOlderPrints()
    {
        var processor = new OrderFlowProcessor(OrderFlowSettings.Default);
        processor.OnTrade(Tick(0, 100m, 40m));
        processor.OnTrade(Tick(500, 100.25m, 40m));
        processor.OnTrade(Tick(1000, 100.5m, 20m));
        Assert.Equal("Buy sweep", Assert.Single(processor.GetSnapshot().Signals).Kind);
    }

    [Fact]
    public void SellSweepMovesDownAcrossPriceLevels()
    {
        var processor = new OrderFlowProcessor(OrderFlowSettings.Default);
        processor.OnTrade(Tick(0, 100m, 40m, AggressorSide.Sell));
        processor.OnTrade(Tick(100, 99.75m, 40m, AggressorSide.Sell));
        processor.OnTrade(Tick(200, 99.5m, 40m, AggressorSide.Sell));
        Assert.Equal("Sell sweep", Assert.Single(processor.GetSnapshot().Signals).Kind);
        Assert.Equal(-120m, processor.GetSnapshot().CumulativeDelta);
    }

    [Fact]
    public void FullDepthReplacesBookAndDeduplicatesWallsUntilTheyDisappear()
    {
        var processor = new OrderFlowProcessor(OrderFlowSettings.Default);
        BookLevel[] levels = [new(BookSide.Bid, 100m, 150m), new(BookSide.Ask, 100.25m, 80m)];
        processor.OnDepth(new(Es, Start, levels));
        processor.OnDepth(new(Es, Start.AddSeconds(1), levels));
        Assert.Equal(2, processor.GetSnapshot().Book.Count);
        Assert.Equal("Bid wall", Assert.Single(processor.GetSnapshot().Signals).Kind);
        processor.OnDepth(new(Es, Start.AddSeconds(2), [new(BookSide.Bid, 100m, 0m)]));
        Assert.Empty(processor.GetSnapshot().Book);
        processor.OnDepth(new(Es, Start.AddSeconds(3), levels));
        Assert.Equal(2, processor.GetSnapshot().Signals.Count);
    }

    [Fact]
    public void StaleDepthSnapshotCannotRestoreRemovedLiquidity()
    {
        var processor = new OrderFlowProcessor(OrderFlowSettings.Default);
        processor.OnDepth(new(Es, Start.AddSeconds(2), []));
        processor.OnDepth(new(Es, Start, [new(BookSide.Bid, 100m, 200m)]));
        Assert.Empty(processor.GetSnapshot().Book);
        Assert.Empty(processor.GetSnapshot().Signals);
        var depth = Assert.IsType<DepthSnapshot>(processor.GetDepthSnapshot());
        Assert.Equal(Start.AddSeconds(2), depth.Timestamp);
        Assert.Empty(depth.Levels);
    }

    [Fact]
    public void DepthNormalizesOrderingAndLastDuplicateZeroRemovesLevel()
    {
        var processor = new OrderFlowProcessor(OrderFlowSettings.Default);
        processor.OnDepth(new(Es, Start, [new(BookSide.Ask, 102m, 1m), new(BookSide.Bid, 99m, 2m),
            new(BookSide.Bid, 100m, 3m), new(BookSide.Ask, 101m, 4m),
            new(BookSide.Bid, 99m, 0m)]));
        Assert.Equal(new decimal[] { 100m, 101m, 102m }, processor.GetSnapshot().Book.Select(x => x.Price));
    }

    [Fact]
    public void ChangingTimeframeClearsBarsWithoutChangingSessionDelta()
    {
        var processor = new OrderFlowProcessor(OrderFlowSettings.Default);
        processor.OnTrade(Tick(0));
        processor.UpdateSettings(OrderFlowSettings.Default with { CandleSeconds = 5 });
        Assert.Empty(processor.GetSnapshot().Candles);
        Assert.Equal(10m, processor.GetSnapshot().CumulativeDelta);
        processor.OnTrade(Tick(7000));
        Assert.Equal(Start.AddSeconds(5), Assert.Single(processor.GetSnapshot().Candles).OpenTime);
    }

    [Fact]
    public void SymbolChangesRequireExplicitReset()
    {
        var processor = new OrderFlowProcessor(OrderFlowSettings.Default);
        processor.OnTrade(Tick(0));
        var nq = new Instrument("NQZ6", "CME", 0.25m);
        Assert.Throws<InvalidOperationException>(() => processor.OnTrade(new(nq, Start, 20000m, 10m, AggressorSide.Buy)));
        processor.Reset();
        processor.OnTrade(new(nq, Start, 20000m, 10m, AggressorSide.Buy));
        Assert.Equal(20000m, Assert.Single(processor.GetSnapshot().Candles).Close);
    }

    [Fact]
    public void ResetClearsEveryDerivedStateAndInstrument()
    {
        var processor = new OrderFlowProcessor(OrderFlowSettings.Default);
        processor.OnTrade(Tick(0, size: 200));
        processor.OnDepth(new(Es, Start, [new(BookSide.Bid, 100m, 200m)]));
        processor.Reset();
        var result = processor.GetSnapshot();
        Assert.Empty(result.Candles);
        Assert.Empty(result.BigTrades);
        Assert.Empty(result.Book);
        Assert.Empty(result.Signals);
        Assert.Equal(0m, result.CumulativeDelta);
        Assert.Null(result.LastEventTime);
        Assert.Null(processor.GetDepthSnapshot());
    }

    [Theory]
    [InlineData(0, 1000, 60)]
    [InlineData(1, 1000, 60)]
    [InlineData(3, 0, 60)]
    [InlineData(3, 1000, 0)]
    public void InvalidSettingsAreRejected(int levels, int window, int seconds)
    {
        Assert.Throws<ArgumentOutOfRangeException>(() => new OrderFlowProcessor(
            OrderFlowSettings.Default with { MinimumSweepLevels = levels, SweepWindowMilliseconds = window, CandleSeconds = seconds }));
    }

    [Fact]
    public void ParallelTradeCallbacksPreserveAllVolumeAndSnapshotIsolation()
    {
        var processor = new OrderFlowProcessor(OrderFlowSettings.Default);
        Parallel.For(0, 2000, _ => processor.OnTrade(Tick(0, size: 1m)));
        var snapshot = processor.GetSnapshot();
        Assert.Equal(2000m, Assert.Single(snapshot.Candles).Volume);
        Assert.Equal(2000m, snapshot.CumulativeDelta);
        processor.Reset();
        Assert.Equal(2000m, Assert.Single(snapshot.Candles).Volume);
    }

    [Fact]
    public void RetentionIsBoundedAndVeryOldTradesAreDropped()
    {
        var processor = new OrderFlowProcessor(OrderFlowSettings.Default with { CandleSeconds = 1 });
        for (var i = 0; i < 5000; i++) processor.OnTrade(Tick(i * 1000, size: 30));
        var result = processor.GetSnapshot();
        Assert.Equal(OrderFlowProcessor.MaximumCandles, result.Candles.Count);
        Assert.Equal(OrderFlowProcessor.MaximumRecentTrades, result.BigTrades.Count);
        var before = result.CumulativeDelta;
        processor.OnTrade(Tick(0, size: 1000));
        Assert.Equal(before, processor.GetSnapshot().CumulativeDelta);
    }

    [Fact]
    public void BookRetentionKeepsNearestPricesToMarket()
    {
        var processor = new OrderFlowProcessor(OrderFlowSettings.Default);
        var bids = Enumerable.Range(1, 1200).Select(i => new BookLevel(BookSide.Bid, i, 1m));
        var asks = Enumerable.Range(1201, 1200).Select(i => new BookLevel(BookSide.Ask, i, 1m));
        processor.OnDepth(new(Es, Start, bids.Concat(asks).ToArray()));
        var data = processor.GetSnapshot().Book;
        Assert.Equal(2000, data.Count);
        Assert.Equal(1200m, data[0].Price);
        Assert.Equal(1201m, data[1000].Price);
    }
}
