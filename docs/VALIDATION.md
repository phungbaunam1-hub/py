# Validation record

Current machine: Linux x64; SDK 8.0.426; target .NET 8 WPF Windows x64.

| Check | Outcome |
| --- | --- |
| Locked solution restore | Passed |
| Release solution build, including Windows UI-test sources | Passed, 0 warnings / 0 errors |
| Core analytics suite | 22 passed, 0 failed, 0 skipped |
| Infrastructure suite, including actual demo-to-processor integration | 12 passed, 0 failed, 0 skipped |
| Windows x64 self-contained single-file publish | Passed |
| Repeat publish using separate frozen Windows lockfiles | Passed |
| Six Windows WPF tests | Compiled; **not executed**, Windows required |
| Opening the published EXE on Windows | **Not executed**, Windows required |
| Official Rithmic plugin/account/market data | **Not implemented or validated**, licensed SDK and versioned adapter missing |
| GitHub Actions workflow | Added, **not executed** from this environment |

Commands used:

```sh
bash scripts/setup-cloud.sh
# Runs restore --locked-mode, Release build and the two cross-platform test suites.
```

Windows publishing was exercised with the equivalent `dotnet publish` command used by
`scripts/Publish-Windows.ps1`, including `RestoreLockedMode=true` and
`NuGetLockFilePath=packages.win-x64.lock.json`. PowerShell itself was unavailable on this
Linux machine; the PowerShell script and Windows-only branch were not executed here.

Separate build and publish lockfiles are intentional: self-contained single-file publishing
adds runtime identifiers and the SDK's ILLink build tooling. Reusing the build-only lockfiles
for that restore correctly fails NU1004. No verification or assertion was disabled to bypass it.

The integration test feeds the real demo callbacks into `OrderFlowProcessor`, waits for the
generated replay, joins the worker on disconnect, and checks candles, big trades, bid/ask depth,
wall/sweep signals and exact signed-size delta totals. Callback exceptions are collected and
required to remain empty. This validates the simulated analysis pipeline, not Rithmic behavior.

All source files were created in the previously empty checkout. No initial application code,
dependency declarations or user changes existed there. The user subsequently authorized publishing
the source and demo ZIPs to phungbaunam1-hub/py to enable downloading. See README and
RITHMIC-INTEGRATION.md for the remaining live-integration boundary.
