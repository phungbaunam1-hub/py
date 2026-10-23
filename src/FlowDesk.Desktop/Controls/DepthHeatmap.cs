using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Windows;
using System.Windows.Media;
using FlowDesk.Core;

namespace FlowDesk.Desktop.Controls;

/// <summary>Renders retained depth samples from this process; it does not infer missing history.</summary>
public sealed class DepthHeatmap : FrameworkElement
{
    public static readonly DependencyProperty HistoryProperty = DependencyProperty.Register(
        nameof(History), typeof(IReadOnlyList<DepthSnapshot>), typeof(DepthHeatmap),
        new FrameworkPropertyMetadata(null, FrameworkPropertyMetadataOptions.AffectsRender));

    private static readonly Brush BackgroundBrush = MakeBrush(11, 17, 28);
    private static readonly Brush LabelBrush = MakeBrush(130, 150, 174);
    private static readonly Brush GridBrush = MakeBrush(29, 41, 60);
    private static readonly Brush[] BidBrushes = CreateRamp(39, 216, 174);
    private static readonly Brush[] AskBrushes = CreateRamp(255, 168, 66);
    private static readonly Typeface Font = new("Consolas");

    public IReadOnlyList<DepthSnapshot>? History
    {
        get => (IReadOnlyList<DepthSnapshot>?)GetValue(HistoryProperty);
        set => SetValue(HistoryProperty, value);
    }

    public DepthHeatmap()
    {
        ClipToBounds = true;
        IsHitTestVisible = false;
    }

    protected override void OnRender(DrawingContext drawing)
    {
        base.OnRender(drawing);
        drawing.DrawRectangle(BackgroundBrush, null, new Rect(RenderSize));
        if (ActualWidth < 200 || ActualHeight < 140)
            return;

        DrawText(drawing, "SAMPLED DEPTH · CURRENT SESSION ONLY", 10, 14, 10);
        var snapshots = History?.TakeLast(200).ToArray();
        if (snapshots is null || snapshots.Length == 0)
        {
            DrawText(drawing, "Waiting for depth snapshots", 14, 24, ActualHeight / 2 - 15);
            DrawText(drawing, "Teal = bid · Amber = ask · Intensity = displayed size", 10, 24, ActualHeight / 2 + 12);
            return;
        }

        var latestLevels = snapshots[^1].Levels.Where(level => level.Size > 0).ToArray();
        if (latestLevels.Length == 0)
        {
            DrawText(drawing, "The current book is empty", 13, 24, ActualHeight / 2);
            return;
        }

        var bestBid = latestLevels.Where(level => level.Side == BookSide.Bid)
            .Select(level => (decimal?)level.Price).Max();
        var bestAsk = latestLevels.Where(level => level.Side == BookSide.Ask)
            .Select(level => (decimal?)level.Price).Min();
        var reference = bestBid.HasValue && bestAsk.HasValue
            ? bestBid.Value / 2 + bestAsk.Value / 2
            : bestBid ?? bestAsk ?? latestLevels[0].Price;

        var prices = snapshots.SelectMany(snapshot => snapshot.Levels)
            .Where(level => level.Size > 0)
            .Select(level => level.Price).Distinct()
            .OrderBy(price => Math.Abs(price - reference)).Take(60)
            .OrderByDescending(price => price).ToArray();
        if (prices.Length == 0)
            return;

        var rows = new Dictionary<decimal, int>(prices.Length);
        for (var row = 0; row < prices.Length; row++)
            rows.Add(prices[row], row);

        var maximumSize = snapshots.SelectMany(snapshot => snapshot.Levels)
            .Where(level => level.Size > 0 && rows.ContainsKey(level.Price))
            .Select(level => level.Size).DefaultIfEmpty(1m).Max();
        var plot = new Rect(14, 36, ActualWidth - 106, ActualHeight - 70);
        var cellWidth = plot.Width / snapshots.Length;
        var cellHeight = plot.Height / prices.Length;

        drawing.PushClip(new RectangleGeometry(plot));
        for (var column = 0; column < snapshots.Length; column++)
        {
            foreach (var level in snapshots[column].Levels)
            {
                if (level.Size <= 0 || !rows.TryGetValue(level.Price, out var row))
                    continue;

                var intensity = Math.Clamp((int)(Math.Sqrt((double)(level.Size / maximumSize)) * 63), 1, 63);
                var brush = level.Side == BookSide.Bid ? BidBrushes[intensity] : AskBrushes[intensity];
                drawing.DrawRectangle(brush, null,
                    new Rect(plot.Left + column * cellWidth, plot.Top + row * cellHeight,
                        Math.Max(0.1, cellWidth), Math.Max(0.1, cellHeight)));
            }
        }
        drawing.Pop();

        var gridPen = new Pen(GridBrush, 1);
        var rowStep = Math.Max(1, (int)Math.Ceiling(prices.Length / Math.Max(1.0, plot.Height / 36)));
        for (var row = 0; row < prices.Length; row += rowStep)
        {
            var y = plot.Top + (row + 0.5) * cellHeight;
            drawing.DrawLine(gridPen, new Point(plot.Right, y), new Point(plot.Right + 4, y));
            DrawText(drawing, prices[row].ToString("0.#####", CultureInfo.InvariantCulture), 10, plot.Right + 8, y - 7);
        }

        var timeStep = Math.Max(1, (int)Math.Ceiling(snapshots.Length / Math.Max(1.0, plot.Width / 100)));
        for (var column = 0; column < snapshots.Length; column += timeStep)
        {
            var x = plot.Left + column * cellWidth;
            DrawText(drawing, snapshots[column].Timestamp.UtcDateTime.ToString("HH:mm:ss", CultureInfo.InvariantCulture),
                9, x, plot.Bottom + 8);
        }

        DrawText(drawing, "BID", 9, ActualWidth - 86, 11, BidBrushes[63]);
        DrawText(drawing, "ASK", 9, ActualWidth - 48, 11, AskBrushes[63]);
    }

    private void DrawText(DrawingContext drawing, string text, double size, double x, double y, Brush? brush = null)
    {
        var formatted = new FormattedText(text, CultureInfo.InvariantCulture, FlowDirection.LeftToRight,
            Font, size, brush ?? LabelBrush, VisualTreeHelper.GetDpi(this).PixelsPerDip);
        drawing.DrawText(formatted, new Point(x, y));
    }

    private static Brush MakeBrush(byte red, byte green, byte blue)
    {
        var brush = new SolidColorBrush(Color.FromRgb(red, green, blue));
        brush.Freeze();
        return brush;
    }

    private static Brush[] CreateRamp(byte red, byte green, byte blue)
    {
        var brushes = new Brush[64];
        for (var index = 0; index < brushes.Length; index++)
        {
            var fraction = 0.10 + 0.90 * index / 63.0;
            brushes[index] = MakeBrush(
                (byte)(11 + (red - 11) * fraction),
                (byte)(17 + (green - 17) * fraction),
                (byte)(28 + (blue - 28) * fraction));
        }
        return brushes;
    }
}
