using System.Windows;
using FlowDesk.Desktop.ViewModels;
using FlowDesk.Desktop.Views;

namespace FlowDesk.Desktop;

public partial class App : Application
{
    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        var viewModel = new MainViewModel(Dispatcher);
        var window = new MainWindow { DataContext = viewModel };
        var shuttingDown = false;
        var closing = false;
        window.Closing += async (_, args) =>
        {
            if (shuttingDown) return;
            args.Cancel = true;
            if (closing) return;
            closing = true;
            window.IsEnabled = false;
            await viewModel.DisposeAsync();
            shuttingDown = true;
            window.Close();
        };
        MainWindow = window;
        window.Show();
    }
}
