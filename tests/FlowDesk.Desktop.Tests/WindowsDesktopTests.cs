using System.Collections.Concurrent;
using System.ComponentModel;
using System.Diagnostics;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using FlowDesk.Core;
using FlowDesk.Desktop.Controls;
using FlowDesk.Desktop.ViewModels;
using FlowDesk.Desktop.Views;
using Xunit;

[assembly: CollectionBehavior(DisableTestParallelization = true)]

namespace FlowDesk.Desktop.Tests;

/// <summary>
/// These tests require a Windows WPF runtime. They run real window initialization,
/// dispatcher callbacks and custom-control rendering rather than replacing the UI with mocks.
/// </summary>
public sealed class WindowsDesktopTests
{
    [Fact]
    public Task DemoDashboardBindsRendersAndStopsItsFeedOnDisconnect() => StaDispatcher.RunAsync(async () =>
    {
        var dispatcher = Dispatcher.CurrentDispatcher;
        var callbackThreads = new ConcurrentBag<bool>();
        await using var viewModel = new MainViewModel(dispatcher);
        using var bindingErrors = new BindingErrorCapture();
        var window = CreateWindow(viewModel);
        try
        {
            viewModel.PropertyChanged += ObservePropertyChange;
            viewModel.BigTrades.CollectionChanged += (_, _) => callbackThreads.Add(dispatcher.CheckAccess());
            viewModel.DepthRows.CollectionChanged += (_, _) => callbackThreads.Add(dispatcher.CheckAccess());
            viewModel.Signals.CollectionChanged += (_, _) => callbackThreads.Add(dispatcher.CheckAccess());

            window.Show();
            await UiSettledAsync(window);
            Assert.True(viewModel.IsDisconnected);
            Assert.True(viewModel.ConnectCommand.CanExecute(null));
            Assert.False(viewModel.DisconnectCommand.CanExecute(null));
            Assert.Contains("DEMO", viewModel.FeedLabel, StringComparison.Ordinal);

            viewModel.ConnectCommand.Execute(null);
            await WaitUntilAsync(() => viewModel.IsConnected && viewModel.Candles.Count > 0 &&
                                      viewModel.DepthRows.Count > 0 && viewModel.DepthHistory.Count > 0,
                "Demo connection did not deliver candles and depth to the WPF dispatcher.");
            await UiSettledAsync(window);

            Assert.Contains("DEMO-SIMULATED", viewModel.AccountText, StringComparison.Ordinal);
            Assert.False(viewModel.ConnectCommand.CanExecute(null));
            Assert.True(viewModel.DisconnectCommand.CanExecute(null));
            Assert.NotEmpty(callbackThreads);
            Assert.All(callbackThreads, Assert.True);

            var chart = Assert.Single(VisualDescendants<CandlestickChart>(window));
            Assert.Same(viewModel.Candles, chart.Candles);
            Assert.Same(viewModel.ChartTrades, chart.BigTrades);
            Assert.True(chart.ActualWidth > 200);
            Assert.True(chart.ActualHeight > 100);
            AssertHasRenderedContent(chart);

            var chartTabs = VisualDescendants<TabControl>(window).Single(control =>
                control.Items.OfType<TabItem>().Any(item => Equals(item.Header, "DEPTH HEATMAP")));
            chartTabs.SelectedIndex = 1;
            await UiSettledAsync(window);
            var heatmap = Assert.Single(VisualDescendants<DepthHeatmap>(window));
            Assert.Same(viewModel.DepthHistory, heatmap.History);
            AssertHasRenderedContent(heatmap);

            viewModel.DisconnectCommand.Execute(null);
            await WaitUntilAsync(() => viewModel.IsDisconnected && viewModel.ConnectCommand.CanExecute(null),
                "Disconnect did not restore the disconnected command state.");
            await Task.Delay(250); // Let any already-queued display refresh finish.
            var candles = viewModel.Candles.ToArray();
            var trades = viewModel.ChartTrades.ToArray();
            var depth = viewModel.DepthHistory.ToArray();
            var lastPrice = viewModel.LastPriceText;
            await Task.Delay(450);

            Assert.False(viewModel.IsConnected);
            Assert.False(viewModel.DisconnectCommand.CanExecute(null));
            Assert.Equal(candles, viewModel.Candles);
            Assert.Equal(trades, viewModel.ChartTrades);
            Assert.Equal(depth, viewModel.DepthHistory);
            Assert.Equal(lastPrice, viewModel.LastPriceText);
            Assert.Equal("No active account connection", viewModel.AccountText);
            Assert.Empty(bindingErrors.Errors);
        }
        finally
        {
            viewModel.PropertyChanged -= ObservePropertyChange;
            await viewModel.DisposeAsync();
            window.Close();
        }

        void ObservePropertyChange(object? sender, PropertyChangedEventArgs args) =>
            callbackThreads.Add(dispatcher.CheckAccess());
    });

    [Fact]
    public Task RithmicModeFailsClosedWithoutPublishingDemoData() => StaDispatcher.RunAsync(async () =>
    {
        await using var viewModel = new MainViewModel(Dispatcher.CurrentDispatcher);
        using var bindingErrors = new BindingErrorCapture();
        viewModel.SelectedMode = FeedMode.RithmicPlugin;
        viewModel.Username = "test";
        var window = CreateWindow(viewModel);
        try
        {
            window.Show();
            await UiSettledAsync(window);
            viewModel.ConnectCommand.Execute(null);
            await WaitUntilAsync(() => viewModel.StatusText.StartsWith("Faulted", StringComparison.Ordinal) &&
                                      viewModel.ConnectCommand.CanExecute(null),
                "Rithmic mode did not report the missing verified SDK adapter.");
            await Task.Delay(300);
            await UiSettledAsync(window);

            Assert.True(viewModel.IsDisconnected);
            Assert.False(viewModel.IsConnected);
            Assert.Contains("SDK", viewModel.StatusText, StringComparison.OrdinalIgnoreCase);
            Assert.Empty(viewModel.Candles);
            Assert.Empty(viewModel.ChartTrades);
            Assert.Empty(viewModel.DepthRows);
            Assert.Empty(viewModel.DepthHistory);
            Assert.Empty(viewModel.BigTrades);
            Assert.Equal("No active account connection", viewModel.AccountText);
            Assert.Equal("—", viewModel.LastPriceText);
            Assert.DoesNotContain("DEMO", viewModel.FeedLabel, StringComparison.Ordinal);
            Assert.Empty(bindingErrors.Errors);
        }
        finally
        {
            await viewModel.DisposeAsync();
            window.Close();
        }
    });

    [Theory]
    [InlineData("symbol")]
    [InlineData("exchange")]
    [InlineData("tick")]
    [InlineData("settings")]
    public Task InvalidInputsDoNotStartTheDemoFeed(string invalidField) => StaDispatcher.RunAsync(async () =>
    {
        await using var viewModel = new MainViewModel(Dispatcher.CurrentDispatcher);
        switch (invalidField)
        {
            case "symbol": viewModel.Symbol = " "; break;
            case "exchange": viewModel.Exchange = ""; break;
            case "tick": viewModel.TickSize = 0; break;
            case "settings": viewModel.MinimumSweepLevels = 1; break;
            default: throw new ArgumentOutOfRangeException(nameof(invalidField));
        }

        viewModel.ConnectCommand.Execute(null);
        await WaitUntilAsync(() => viewModel.ConnectCommand.CanExecute(null),
            "The invalid connection command failed to complete.");
        await Task.Delay(250);

        Assert.True(viewModel.IsDisconnected);
        Assert.False(viewModel.IsConnected);
        Assert.Empty(viewModel.Candles);
        Assert.Empty(viewModel.DepthRows);
        Assert.Equal("No account connected", viewModel.AccountText);
        Assert.Contains(viewModel.Logs, message => invalidField == "settings"
            ? message.Contains("Settings rejected:", StringComparison.Ordinal)
            : message.Contains("positive tick size", StringComparison.Ordinal));
    });

    private static MainWindow CreateWindow(MainViewModel viewModel) => new()
    {
        DataContext = viewModel,
        Width = 1560,
        Height = 980,
        WindowStartupLocation = WindowStartupLocation.Manual,
        Left = -10000,
        Top = -10000,
        ShowActivated = false,
        ShowInTaskbar = false
    };

    private static async Task UiSettledAsync(Window window)
    {
        await Dispatcher.Yield(DispatcherPriority.ApplicationIdle);
        window.UpdateLayout();
        await Dispatcher.Yield(DispatcherPriority.ApplicationIdle);
    }

    private static async Task WaitUntilAsync(Func<bool> predicate, string failure)
    {
        var elapsed = Stopwatch.StartNew();
        while (!predicate())
        {
            Assert.True(elapsed.Elapsed < TimeSpan.FromSeconds(10), failure);
            await Task.Delay(25);
        }
    }

    private static IEnumerable<T> VisualDescendants<T>(DependencyObject root) where T : DependencyObject
    {
        for (var index = 0; index < VisualTreeHelper.GetChildrenCount(root); index++)
        {
            var child = VisualTreeHelper.GetChild(root, index);
            if (child is T match) yield return match;
            foreach (var descendant in VisualDescendants<T>(child)) yield return descendant;
        }
    }

    private static void AssertHasRenderedContent(FrameworkElement element)
    {
        var width = checked((int)Math.Ceiling(element.ActualWidth));
        var height = checked((int)Math.Ceiling(element.ActualHeight));
        Assert.True(width > 100 && height > 80);
        var bitmap = new RenderTargetBitmap(width, height, 96, 96, PixelFormats.Pbgra32);
        bitmap.Render(element);
        var pixels = new byte[width * height * 4];
        bitmap.CopyPixels(pixels, width * 4, 0);
        var colors = new HashSet<int>();
        for (var offset = 0; offset < pixels.Length; offset += 4)
        {
            colors.Add(pixels[offset] | pixels[offset + 1] << 8 |
                       pixels[offset + 2] << 16 | pixels[offset + 3] << 24);
            if (colors.Count > 10) break;
        }
        Assert.True(colors.Count > 10, "The control rendered a blank or uniform surface.");
    }

    private sealed class BindingErrorCapture : TraceListener
    {
        private readonly SourceLevels _previousLevel;
        public ConcurrentQueue<string> Errors { get; } = new();

        public BindingErrorCapture()
        {
            _previousLevel = PresentationTraceSources.DataBindingSource.Switch.Level;
            PresentationTraceSources.DataBindingSource.Switch.Level = SourceLevels.Error;
            PresentationTraceSources.DataBindingSource.Listeners.Add(this);
        }

        public override void Write(string? message)
        {
            if (!string.IsNullOrWhiteSpace(message)) Errors.Enqueue(message);
        }

        public override void WriteLine(string? message) => Write(message);

        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                PresentationTraceSources.DataBindingSource.Listeners.Remove(this);
                PresentationTraceSources.DataBindingSource.Switch.Level = _previousLevel;
            }
            base.Dispose(disposing);
        }
    }

    private static class StaDispatcher
    {
        public static async Task RunAsync(Func<Task> test)
        {
            var completion = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
            var thread = new Thread(() =>
            {
                var dispatcher = Dispatcher.CurrentDispatcher;
                SynchronizationContext.SetSynchronizationContext(new DispatcherSynchronizationContext(dispatcher));
                dispatcher.BeginInvoke(DispatcherPriority.Normal, new Action(async () =>
                {
                    try
                    {
                        await test();
                        completion.TrySetResult();
                    }
                    catch (Exception exception)
                    {
                        completion.TrySetException(exception);
                    }
                    finally
                    {
                        dispatcher.BeginInvokeShutdown(DispatcherPriority.Background);
                    }
                }));
                Dispatcher.Run();
            })
            {
                IsBackground = true,
                Name = "FlowDesk WPF test dispatcher"
            };
            thread.SetApartmentState(ApartmentState.STA);
            thread.Start();
            await completion.Task.WaitAsync(TimeSpan.FromSeconds(30));
            Assert.True(thread.Join(TimeSpan.FromSeconds(5)), "The WPF dispatcher did not shut down.");
        }
    }
}
