#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
dotnet restore FlowDesk.sln --locked-mode
dotnet build FlowDesk.sln -c Release --no-restore
dotnet test tests/FlowDesk.Core.Tests -c Release --no-build --no-restore
dotnet test tests/FlowDesk.Infrastructure.Tests -c Release --no-build --no-restore
# WPF runtime tests must be executed on Windows; Linux can compile their sources only.
