namespace FlowDesk.Infrastructure.Services;

/// <summary>The requested official live adapter cannot be loaded or verified.</summary>
public sealed class RithmicSdkUnavailableException : InvalidOperationException
{
    public RithmicSdkUnavailableException()
        : base("Rithmic connection is unavailable: this distribution does not include the licensed " +
               "official R|API+ SDK or a version-verified R|Trader Pro plugin adapter. " +
               "Obtain the supported .NET SDK, plugin sample and application entitlement from Rithmic. " +
               "Copying a DLL alone does not implement the adapter. No connection or authentication " +
               "was attempted. Choose Demo explicitly to use simulated data.")
    {
    }
}
