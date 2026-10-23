using FlowDesk.Core;

namespace FlowDesk.Infrastructure.Services;

/// <summary>
/// Fails closed when official Rithmic integration is requested. A live connector is deliberately
/// not shipped without the licensed SDK and its version-specific plugin contract. This class
/// never opens a socket, stores credentials, emits simulated account data or falls back to Demo.
/// </summary>
public sealed class RithmicPluginService : IRithmicPluginService
{
    private readonly SemaphoreSlim _gate = new(1, 1);
    private int _disposed;
    private readonly TaskCompletionSource _disposalCompletion = new(TaskCreationOptions.RunContinuationsAsynchronously);

    public event EventHandler<ConnectionStatus>? StatusChanged;

    // Explicit accessors document that these events can never be raised by the blocked connector.
    public event EventHandler<TradeTick>? TradeReceived { add { } remove { } }
    public event EventHandler<DepthSnapshot>? DepthReceived { add { } remove { } }
    public event EventHandler<AccountInfo>? AccountReceived { add { } remove { } }

    public async Task ConnectAsync(ConnectionOptions options, CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(options);
        ObjectDisposedException.ThrowIf(Volatile.Read(ref _disposed) != 0, this);
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            ObjectDisposedException.ThrowIf(Volatile.Read(ref _disposed) != 0, this);
            cancellationToken.ThrowIfCancellationRequested();
            PublishStatus(ConnectionState.Connecting, "Checking official Rithmic plugin adapter availability.");
            var exception = new RithmicSdkUnavailableException();
            PublishStatus(ConnectionState.Faulted, exception.Message);
            throw exception;
        }
        finally
        {
            _gate.Release();
        }
    }

    public Task SubscribeAsync(Instrument instrument, CancellationToken cancellationToken)
    {
        ObjectDisposedException.ThrowIf(Volatile.Read(ref _disposed) != 0, this);
        cancellationToken.ThrowIfCancellationRequested();
        throw new InvalidOperationException("A verified Rithmic plugin session is required before subscribing. " +
                                            "The official adapter is not installed in this distribution.");
    }

    public async Task DisconnectAsync(CancellationToken cancellationToken)
    {
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            if (Volatile.Read(ref _disposed) == 0)
                PublishStatus(ConnectionState.Disconnected, "Rithmic disconnected; no session was opened.");
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
                PublishStatus(ConnectionState.Disconnected, "Rithmic service disposed; no session was opened.");
                StatusChanged = null;
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

    private void PublishStatus(ConnectionState state, string message)
    {
        var handlers = StatusChanged;
        if (handlers is null)
            return;
        var status = new ConnectionStatus(state, message);
        foreach (EventHandler<ConnectionStatus> subscriber in handlers.GetInvocationList())
        {
            try { subscriber(this, status); }
            catch (Exception) { /* UI/log observers cannot change connection outcome. */ }
        }
    }
}
