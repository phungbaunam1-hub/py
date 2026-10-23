# Rithmic / R|Trader Pro integration status

## Delivery boundary

The runnable application, demo feed, MVVM lifecycle and order-flow engine are implemented. The official Rithmic engine adapter is **not implemented**. `RithmicPluginService` is an explicit unavailable-capability implementation, not a wrapper around `REngine`. It can only publish Connecting and Faulted on a connection attempt, then throw `RithmicSdkUnavailableException`. It never reports Connected or returns market/account data.

This distinction is intentional: the supplied attachment integrates MetaTrader 5, and no licensed Rithmic SDK, versioned plugin sample, credentials or Windows R|Trader Pro runtime were supplied. A working local-proxy login cannot be synthesized from those inputs. There is no fake `com.rithmic.api` namespace or lookalike DLL in this repository.

## Required to complete the adapter

Obtain from Rithmic or the account provider:

1. The officially licensed .NET SDK distribution, matching x64 dependencies, XML/API docs and a working plugin-mode sample. Confirm .NET 8 compatibility; an assembly requiring a different runtime may need an out-of-process bridge rather than an unsupported reference.
2. The actual plugin handshake contract and which channels use the local R|Trader Pro session: market data, history, trading/account/PnL. Do not assume one loopback endpoint provides them all.
3. Required application name/version or approval, paper-trading/Chicago connection settings, entitlements, concurrent-session rules and any fees. Provider rules must confirm these; the app cannot guarantee zero fees.
4. A Windows paper session with R|Trader Pro logged in and its supported plugin mode enabled. Enter credentials in the provider-supported authentication flow, not in source files or this chat.

Do not acquire proprietary DLLs from random GitHub mirrors. Native/managed assembly names and versions differ across distributions. In particular, `rapiplus.dll` cannot safely be assumed to be native simply from its filename. Inspect the official package before adding an assembly reference or copy-to-output rule; all native dependencies must match the process architecture.

## Implementation contract

Implement the existing `IRithmicPluginService` using the exact types from the supplied SDK, then select that implementation in `MainViewModel`. No chart/UI logic needs to depend on vendor classes.

- Marshal SDK callback data into owned immutable snapshots. `DepthSnapshot` represents the **complete** current book, so merge SDK depth deltas and deletes before publishing it.
- Preserve exchange timestamps, size units and aggressor semantics. Use Unknown if the API cannot identify the aggressor reliably. Prevent duplicate trades during replay/resubscription using vendor sequence identifiers where available.
- Report Connected only when the required login/subscription acknowledgements succeeded. Distinguish market-data state from account/trading state; do not mark account data available merely because MD connected.
- Serialize login/logout/subscription operations. Apply a bounded timeout and cancellation. Quiesce all callbacks before `DisconnectAsync`/`DisposeAsync` completes. The WPF VM ignores callbacks from previous service objects.
- Handle R|Trader Pro absent, plugins disabled, login rejected, session dropped, unsupported symbol, unavailable entitlement and recovery/reconnect with explicit states. Avoid credential values in exceptions/logs.
- Keep order routing outside this read-only feed contract. Trading needs a separate reviewed service, acknowledgement/order state machine and paper validation.
- Add the **actual** official managed DLL reference and copy matching native assets using the vendor instructions. Do not blindly add the unverified `RApiDotNet.dll` + native `rapiplus.dll` pair from the initial specification.

Required acceptance checks on Windows: plugin login, close R|Trader Pro, disable plugins, restore it, cancellation during login, reconnect, real paper account callback, trades/bid/ask/depth acknowledgement, unsubscribing, accurate exchange timestamps and cleanup without stale callbacks. None of those live checks has run in the current Linux environment.

## Research evidence and limits

The following are **community examples, not official API contracts**:

- [robook Client.cs](https://github.com/bukowa/robook/blob/master/Rithmic/Client.cs) uses `com.omnesys.rapi`, `REngineParams`, `RCallbacks` and `AdmCallbacks`. Its plugin MD endpoint is `127.0.0.1:3010` with encoding settings, while other channels have separate configuration. It passes configured credentials; it does not establish that an empty password works universally.
- [robook project reference](https://github.com/bukowa/robook/blob/master/Rithmic/Rithmic.csproj) references managed `rapiplus.dll`.
- [RapiBarFetch](https://github.com/squideyes/RapiBarFetch) directs developers to obtain the SDK DLL from Rithmic.

Official Rithmic, Quantower and NinjaTrader documentation hosts returned proxy CONNECT 403 during this task. The SDK contract, plugin fee policy and exact account authentication behavior could therefore not be independently verified. No network allowlist was broadened. Public NuGet searches for `RApiDotNet` and `rapiplus` returned no package; this is not proof that no official distribution exists through another channel.

These findings explain why inventing `RClientListener.loginResponse` or forcing an empty-password socket login would produce misleading code. Use the official versioned SDK and sample to resolve the remaining integration work.
