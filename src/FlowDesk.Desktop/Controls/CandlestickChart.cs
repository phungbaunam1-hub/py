using System.Globalization;
using System.Windows;
using System.Windows.Input;
using System.Windows.Media;
using FlowDesk.Core;

namespace FlowDesk.Desktop.Controls;

/// <summary>
/// Draws bounded snapshots on the WPF render thread. It owns no subscriptions or timers;
/// callers publish immutable snapshots at their chosen display refresh rate.
/// </summary>
public sealed class CandlestickChart : FrameworkElement
{
    public static readonly DependencyProperty CandlesProperty = DependencyProperty.Register(
        nameof(Candles), typeof(IReadOnlyList<Candle>), typeof(CandlestickChart),
        new FrameworkPropertyMetadata(null, FrameworkPropertyMetadataOptions.AffectsRender));

    public static readonly DependencyProperty BigTradesProperty = DependencyProperty.Register(
        nameof(BigTrades), typeof(IReadOnlyList<TradeTick>), typeof(CandlestickChart),
        new FrameworkPropertyMetadata(null, FrameworkPropertyMetadataOptions.AffectsRender));

    private static readonly Brush BackgroundBrush = FrozenBrush("#0B111C");
    private static readonly Brush GridBrush = FrozenBrush("#1D293C");
    private static readonly Brush LabelBrush = FrozenBrush("#8296AE");
    private static readonly Brush UpBrush = FrozenBrush("#35CFAB");
    private static readonly Brush DownBrush = FrozenBrush("#F27483");
    private static readonly Brush UnknownBrush = FrozenBrush("#B0BCCB");
    private static readonly Brush TextBrush = FrozenBrush("#DDE7F3");
    private static readonly Typeface Typeface = new("Consolas");
    private int _visibleBars = 70;

    public IReadOnlyList<Candle>? Candles
    {
        get => (IReadOnlyList<Candle>?)GetValue(CandlesProperty);
        set => SetValue(CandlesProperty, value);
    }

    public IReadOnlyList<TradeTick>? BigTrades
    {
        get => (IReadOnlyList<TradeTick>?)GetValue(BigTradesProperty);
        set => SetValue(BigTradesProperty, value);
    }

    public CandlestickChart()
    {
        ClipToBounds = true;
        Focusable = true;
        MouseWheel += OnMouseWheel;
    }

    private void OnMouseWheel(object sender, MouseWheelEventArgs args)
    {
        _visibleBars = Math.Clamp(_visibleBars + (args.Delta > 0 ? -10 : 10), 20, 240);
        InvalidateVisual();
        args.Handled = true;
    }

    protected override void OnRender(DrawingContext drawing)
    {
        base.OnRender(drawing);
        drawing.DrawRectangle(BackgroundBrush, null, new Rect(RenderSize));
        if (ActualWidth < 190 || ActualHeight < 150)
            return;

        var candles = Candles;
        if (candles is null || candles.Count == 0)
        {
            DrawText(drawing, "Waiting for trades", 18, TextBrush, 30, Math.Max(30, ActualHeight / 2 - 30));
            DrawText(drawing, "Connect a feed to begin building candles.", 11, LabelBrush, 30, Math.Max(56, ActualHeight / 2));
            return;
        }

        const double left = 14;
        const double right = 91;
        const double top = 17;
        const double bottom = 28;
        var volumeHeight = Math.Clamp(ActualHeight * 0.17, 36, 70);
        var plot = new Rect(left, top, ActualWidth - left - right, ActualHeight - top - bottom - volumeHeight - 19);
        if (plot.Height <= 20 || plot.Width <= 20)
            return;

        var first = Math.Max(0, candles.Count - _visibleBars);
        var count = candles.Count - first;
        var low = candles[first].Low;
        var high = candles[first].High;
        var maxVolume = 0m;
        for (var index = first; index < candles.Count; index++)
        {
            low = Math.Min(low, candles[index].Low);
            high = Math.Max(high, candles[index].High);
            maxVolume = Math.Max(maxVolume, candles[index].Volume);
        }

        var span = high - low;
        if (span <= 0)
            span = Math.Max(Math.Abs(high) * 0.001m, 1m);
        low -= span * 0.10m;
        high += span * 0.10m;
        span = high - low;
        var step = plot.Width / Math.Max(count, 15);
        var bodyWidth = Math.Clamp(step * 0.64, 1.5, 14);
        var priceFormat = span < 0.1m ? "0.00000" : span < 2m ? "0.0000" : "0.00";
        double PriceY(decimal price) => plot.Bottom - (double)((price - low) / span) * plot.Height;
        double CandleX(int index) => plot.Left + (index - first + 0.5) * step;

        var gridPen = new Pen(GridBrush, 1);
        for (var index = 0; index <= 5; index++)
        {
            var fraction = index / 5.0;
            var y = plot.Top + plot.Height * fraction;
            drawing.DrawLine(gridPen, new Point(plot.Left, y), new Point(plot.Right, y));
            var price = high - span * (decimal)fraction;
            DrawText(drawing, price.ToString(priceFormat, CultureInfo.InvariantCulture), 10, LabelBrush, plot.Right + 8, y - 7);
        }

        var labelEvery = Math.Max(1, (int)Math.Ceiling(count / Math.Max(1.0, plot.Width / 95)));
        var volumeTop = plot.Bottom + 17;
        for (var index = first; index < candles.Count; index++)
        {
            var bar = candles[index];
            var x = CandleX(index);
            if ((index - first) % labelEvery == 0)
            {
                drawing.DrawLine(gridPen, new Point(x, plot.Top), new Point(x, volumeTop + volumeHeight));
                DrawText(drawing, bar.OpenTime.UtcDateTime.ToString("HH:mm:ss", CultureInfo.InvariantCulture), 9,
                    LabelBrush, Math.Max(plot.Left, x - 22), ActualHeight - 18);
            }

            var brush = bar.Close >= bar.Open ? UpBrush : DownBrush;
            var openY = PriceY(bar.Open);
            var closeY = PriceY(bar.Close);
            drawing.DrawLine(new Pen(brush, 1), new Point(x, PriceY(bar.High)), new Point(x, PriceY(bar.Low)));
            drawing.DrawRectangle(brush, null,
                new Rect(x - bodyWidth / 2, Math.Min(openY, closeY), bodyWidth, Math.Max(1.2, Math.Abs(closeY - openY))));

            var volumePixels = maxVolume <= 0 ? 0 : (double)(bar.Volume / maxVolume) * volumeHeight;
            drawing.PushOpacity(0.40);
            drawing.DrawRectangle(brush, null, new Rect(x - bodyWidth / 2,
                volumeTop + volumeHeight - volumePixels, bodyWidth, volumePixels));
            drawing.Pop();
        }

        DrawText(drawing, "VOL", 9, LabelBrush, plot.Right + 8, volumeTop + 2);
        DrawText(drawing, maxVolume.ToString("0.##", CultureInfo.InvariantCulture), 9, LabelBrush, plot.Right + 8, volumeTop + 17);

        drawing.PushClip(new RectangleGeometry(plot));
        DrawTradeBubbles(drawing, candles, first, plot, CandleX, PriceY);
        drawing.Pop();

        var lastClose = candles[^1].Close;
        var lastY = Math.Clamp(PriceY(lastClose), plot.Top, plot.Bottom);
        var lastBrush = candles[^1].Close >= candles[^1].Open ? UpBrush : DownBrush;
        var currentPen = new Pen(lastBrush, 1) { DashStyle = DashStyles.Dash };
        drawing.PushOpacity(0.60);
        drawing.DrawLine(currentPen, new Point(plot.Left, lastY), new Point(plot.Right, lastY));
        drawing.Pop();
        drawing.DrawRoundedRectangle(lastBrush, null, new Rect(plot.Right + 2, lastY - 10, right - 7, 20), 3, 3);
        DrawText(drawing, lastClose.ToString(priceFormat, CultureInfo.InvariantCulture), 10, BackgroundBrush, plot.Right + 8, lastY - 7);
    }

    private void DrawTradeBubbles(
        DrawingContext drawing,
        IReadOnlyList<Candle> candles,
        int first,
        Rect plot,
        Func<int, double> candleX,
        Func<decimal, double> priceY)
    {
        var trades = BigTrades;
        if (trades is null || trades.Count == 0)
            return;

        var firstTime = candles[first].OpenTime;
        var maxSize = 1m;
        foreach (var trade in trades)
            if (trade.Timestamp >= firstTime)
                maxSize = Math.Max(maxSize, trade.Size);

        foreach (var trade in trades)
        {
            if (trade.Timestamp < firstTime || trade.Size <= 0)
                continue;

            var index = FindCandle(candles, first, trade.Timestamp);
            var x = candleX(index);
            var y = priceY(trade.Price);
            if (!plot.Contains(new Point(x, y)))
                continue;

            var radius = 5 + 14 * Math.Sqrt((double)(trade.Size / maxSize));
            var brush = trade.Aggressor switch
            {
                AggressorSide.Buy => UpBrush,
                AggressorSide.Sell => DownBrush,
                _ => UnknownBrush
            };
            drawing.PushOpacity(0.25);
            drawing.DrawEllipse(brush, null, new Point(x, y), radius, radius);
            drawing.Pop();
            drawing.PushOpacity(0.85);
            drawing.DrawEllipse(null, new Pen(brush, 1.2), new Point(x, y), radius, radius);
            drawing.Pop();
            if (radius >= 10)
            {
                var text = MakeText(trade.Size.ToString("0.#", CultureInfo.InvariantCulture), 9, TextBrush);
                drawing.DrawText(text, new Point(x - text.Width / 2, y - text.Height / 2));
            }
        }
    }

    private static int FindCandle(IReadOnlyList<Candle> candles, int first, DateTimeOffset timestamp)
    {
        var lower = first;
        var upper = candles.Count - 1;
        while (lower < upper)
        {
            var middle = lower + (upper - lower + 1) / 2;
            if (candles[middle].OpenTime <= timestamp)
                lower = middle;
            else
                upper = middle - 1;
        }
        return lower;
    }

    private FormattedText MakeText(string text, double size, Brush brush) => new(
        text, CultureInfo.InvariantCulture, FlowDirection.LeftToRight, Typeface, size, brush,
        VisualTreeHelper.GetDpi(this).PixelsPerDip);

    private void DrawText(DrawingContext drawing, string text, double size, Brush brush, double x, double y) =>
        drawing.DrawText(MakeText(text, size, brush), new Point(x, y));

    private static Brush FrozenBrush(string color)
    {
        var brush = (SolidColorBrush)new BrushConverter().ConvertFromInvariantString(color)!;
        brush.Freeze();
        return brush;
    }
}
