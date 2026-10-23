#!/usr/bin/env bash
set -euo pipefail
export DOTNET_ROOT=/workspace/.dotnet
export DOTNET_CLI_HOME=/workspace/.dotnet-home
export NUGET_PACKAGES=/workspace/.nuget/packages
export DOTNET_CLI_TELEMETRY_OPTOUT=1
export DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1
export PATH="$DOTNET_ROOT:$PATH"
if [[ ! -d "$DOTNET_ROOT/sdk/8.0.426" ]]; then
    installer_file=$(mktemp /tmp/flowdesk-dotnet-install-XXXXXX.sh)
    trap 'rm -f "$installer_file"' EXIT
    curl --fail --show-error --silent --location --proto '=https' --tlsv1.2 \
        https://dot.net/v1/dotnet-install.sh -o "$installer_file"
    bash "$installer_file" --version 8.0.426 --install-dir "$DOTNET_ROOT" --no-path
fi
cd /workspace/py
bash scripts/validate.sh
