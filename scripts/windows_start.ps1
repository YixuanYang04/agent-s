param(
    [ValidateSet("run", "test", "setup", "tunnel", "watch", "single", "feishu")]
    [string]$Mode = "run",
    [string]$Task = "",
    [int]$MaxSteps = 0,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RemainingArgs
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir
$EnvFile = Join-Path $Root ".env"
$ExampleEnvFile = Join-Path $Root ".env.windows.example"
$VenvDir = Join-Path $Root ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$MarkerFile = Join-Path $VenvDir ".agent_s_windows_ready"

try {
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [Console]::OutputEncoding = $utf8NoBom
    $script:OutputEncoding = $utf8NoBom
} catch {
}

function Read-DotEnv {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $trimmed = $line.Trim()
        if ([string]::IsNullOrWhiteSpace($trimmed) -or $trimmed.StartsWith("#")) {
            continue
        }
        if ($trimmed -match "^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$") {
            $name = $Matches[1]
            $value = $Matches[2].Trim()
            if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            [Environment]::SetEnvironmentVariable($name, $value, "Process")
        }
    }
}

function Get-EnvValue {
    param([string[]]$Names, [string]$Default = "")
    foreach ($name in $Names) {
        $value = [Environment]::GetEnvironmentVariable($name, "Process")
        if (-not [string]::IsNullOrWhiteSpace($value)) {
            return $value.Trim()
        }
    }
    return $Default
}

function Set-DotEnvValue {
    param([string]$Path, [string]$Name, [string]$Value)
    $lines = @()
    if (Test-Path -LiteralPath $Path) {
        $lines = @(Get-Content -LiteralPath $Path -Encoding UTF8)
    }
    $pattern = "^\s*$([regex]::Escape($Name))\s*="
    $updated = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match $pattern) {
            $lines[$i] = "$Name=$Value"
            $updated = $true
        }
    }
    if (-not $updated) {
        $lines += "$Name=$Value"
    }
    Set-Content -LiteralPath $Path -Value $lines -Encoding UTF8
    [Environment]::SetEnvironmentVariable($Name, $Value, "Process")
}

function Ensure-EnvFile {
    if (-not (Test-Path -LiteralPath $EnvFile)) {
        if (Test-Path -LiteralPath $ExampleEnvFile) {
            Copy-Item -LiteralPath $ExampleEnvFile -Destination $EnvFile
            Write-Host "[config] Created .env from .env.windows.example"
        } else {
            New-Item -ItemType File -Path $EnvFile | Out-Null
            Write-Host "[config] Created empty .env"
        }
    }
    Read-DotEnv -Path $EnvFile
}

function Ensure-MainModelConfig {
    $modelUrl = Get-EnvValue -Names @("AGENT_S_MODEL_URL", "OPENAI_BASE_URL") -Default "https://aihubmix.com/v1"
    if ([string]::IsNullOrWhiteSpace((Get-EnvValue -Names @("AGENT_S_MODEL_URL", "OPENAI_BASE_URL")))) {
        Set-DotEnvValue -Path $EnvFile -Name "AGENT_S_MODEL_URL" -Value $modelUrl
    }

    $apiKey = Get-EnvValue -Names @("AGENT_S_MODEL_API_KEY", "OPENAI_API_KEY")
    if ([string]::IsNullOrWhiteSpace($apiKey)) {
        Write-Host "[config] Main model API key is missing."
        $apiKey = Read-Host "Enter AGENT_S_MODEL_API_KEY for this Windows client"
        if ([string]::IsNullOrWhiteSpace($apiKey)) {
            throw "Main model API key is required. Put it in .env as AGENT_S_MODEL_API_KEY."
        }
        Set-DotEnvValue -Path $EnvFile -Name "AGENT_S_MODEL_API_KEY" -Value $apiKey
    }
}

function Get-PythonLauncher {
    $candidates = @(
        @{ Exe = "py"; Args = @("-3.11") },
        @{ Exe = "py"; Args = @("-3.10") },
        @{ Exe = "py"; Args = @("-3.12") },
        @{ Exe = "py"; Args = @("-3.9") },
        @{ Exe = "py"; Args = @("-3") },
        @{ Exe = "python"; Args = @() },
        @{ Exe = "python3"; Args = @() }
    )

    $probe = "import sys; ok=(3,9) <= sys.version_info[:2] <= (3,12); print(sys.executable if ok else ''); raise SystemExit(0 if ok else 1)"
    foreach ($candidate in $candidates) {
        try {
            $output = & $candidate.Exe @($candidate.Args + @("-c", $probe)) 2>$null
            if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($output)) {
                return $output.Trim()
            }
        } catch {
            continue
        }
    }
    throw "Python 3.9-3.12 was not found. Install Python 3.10 or 3.11, then rerun this script."
}

function Ensure-Venv {
    $forceInstall = (Get-EnvValue -Names @("AGENT_S_FORCE_INSTALL") -Default "0").ToLower() -in @("1", "true", "yes", "on")
    if (-not (Test-Path -LiteralPath $VenvPython)) {
        $basePython = Get-PythonLauncher
        Write-Host "[setup] Creating virtual environment with $basePython"
        & $basePython -m venv $VenvDir
    }

    $needsInstall = $forceInstall -or -not (Test-Path -LiteralPath $MarkerFile)
    if (-not $needsInstall) {
        $oldErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            & $VenvPython -c "import backoff, dotenv, openai, paramiko" *> $null
            if ($LASTEXITCODE -ne 0) {
                $needsInstall = $true
            }
        } finally {
            $ErrorActionPreference = $oldErrorActionPreference
        }
    }

    if ($needsInstall) {
        Write-Host "[setup] Installing Python dependencies. First run can take a while..."
        & $VenvPython -m pip install --upgrade pip
        & $VenvPython -m pip install -r (Join-Path $Root "requirements.txt")
        & $VenvPython -m pip install -e $Root
        Set-Content -LiteralPath $MarkerFile -Value (Get-Date).ToString("s") -Encoding ASCII
    } else {
        Write-Host "[setup] Virtual environment is ready."
    }
}

function Test-OpenAIModelsEndpoint {
    param([string]$BaseUrl)
    if ([string]::IsNullOrWhiteSpace($BaseUrl)) {
        return $false
    }
    $url = $BaseUrl.TrimEnd("/") + "/models"
    try {
        Invoke-RestMethod -Uri $url -Headers @{ Authorization = "Bearer dummy-key" } -TimeoutSec 3 | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Get-OpenAIModelIds {
    param([string]$BaseUrl)
    if ([string]::IsNullOrWhiteSpace($BaseUrl)) {
        return @()
    }
    $url = $BaseUrl.TrimEnd("/") + "/models"
    try {
        $response = Invoke-RestMethod -Uri $url -Headers @{ Authorization = "Bearer dummy-key" } -TimeoutSec 3
        return @($response.data | ForEach-Object { "$($_.id)".Trim() } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    } catch {
        return @()
    }
}

function Write-GroundModelWarning {
    param([string]$GroundUrl, [string]$Model)
    if ([string]::IsNullOrWhiteSpace($Model)) {
        return
    }
    $ids = @(Get-OpenAIModelIds -BaseUrl $GroundUrl)
    if ($ids.Count -gt 0 -and ($ids -notcontains $Model)) {
        Write-Host "[ssh] Warning: /models does not list configured grounding model '$Model'."
        Write-Host "[ssh] /models returned: $($ids -join ', ')"
        Write-Host "[ssh] Continuing; chat/completions will be the authoritative check."
    }
}

function Test-LocalTcpPortOpen {
    param([int]$Port)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        $connected = $iar.AsyncWaitHandle.WaitOne(300, $false)
        if ($connected) {
            $client.EndConnect($iar)
            return $true
        }
        return $false
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Get-FreeLocalPort {
    param([int]$PreferredPort)
    if (-not (Test-LocalTcpPortOpen -Port $PreferredPort)) {
        return $PreferredPort
    }
    for ($port = 18000; $port -le 18100; $port++) {
        if (-not (Test-LocalTcpPortOpen -Port $port)) {
            return $port
        }
    }
    throw "No free local port found in 18000-18100 for the SSH tunnel."
}

function Wait-GroundEndpoint {
    param([string]$GroundUrl, [string]$Model = "")
    $modelText = if ([string]::IsNullOrWhiteSpace($Model)) { "" } else { " model=$Model" }
    Write-Host "[ssh] Waiting for grounding endpoint: $GroundUrl$modelText"
    for ($i = 1; $i -le 300; $i++) {
        if (Test-OpenAIModelsEndpoint -BaseUrl $GroundUrl) {
            Write-GroundModelWarning -GroundUrl $GroundUrl -Model $Model
            Write-Host "[ssh] Grounding endpoint is ready."
            return
        }
        Start-Sleep -Seconds 1
    }
    throw "Timed out waiting for $GroundUrl. Check .windows tunnel logs and ~/agent_s_vllm.log on the server."
}

function Set-GroundEndpointForProcess {
    param([string]$GroundUrl)
    [Environment]::SetEnvironmentVariable("AGENT_S_GROUND_URL", $GroundUrl, "Process")
    [Environment]::SetEnvironmentVariable("HF_ENDPOINT_URL", $GroundUrl, "Process")
}

function Start-RemoteTunnelIfNeeded {
    $useTunnel = (Get-EnvValue -Names @("AGENT_S_USE_SSH_TUNNEL") -Default "1").ToLower() -in @("1", "true", "yes", "on")
    if (-not $useTunnel) {
        return
    }

    $localPort = Get-EnvValue -Names @("AGENT_S_LOCAL_TUNNEL_PORT") -Default "8000"
    $servedModel = Get-EnvValue -Names @("AGENT_S_GROUND_MODEL", "GROUND_MODEL") -Default "UI-TARS-1.5-7B"
    $groundUrl = Get-EnvValue -Names @("AGENT_S_GROUND_URL", "HF_ENDPOINT_URL") -Default "http://127.0.0.1:$localPort/v1"
    Set-GroundEndpointForProcess -GroundUrl $groundUrl

    if (Test-OpenAIModelsEndpoint -BaseUrl $groundUrl) {
        Write-GroundModelWarning -GroundUrl $groundUrl -Model $servedModel
        Write-Host "[ssh] Grounding endpoint already reachable: $groundUrl"
        return
    }

    $portForTunnel = [int]$localPort
    if (Test-LocalTcpPortOpen -Port $portForTunnel) {
        $portForTunnel = Get-FreeLocalPort -PreferredPort $portForTunnel
        $groundUrl = "http://127.0.0.1:$portForTunnel/v1"
        Set-GroundEndpointForProcess -GroundUrl $groundUrl
        Write-Host "[ssh] Local port $localPort is busy and not an OpenAI /v1 endpoint; using $portForTunnel for this run."
    }

    $workDir = Join-Path $Root ".windows"
    New-Item -ItemType Directory -Force -Path $workDir | Out-Null

    $remoteHost = Get-EnvValue -Names @("AGENT_S_REMOTE_HOST") -Default "111.0.130.56"
    $remoteUser = Get-EnvValue -Names @("AGENT_S_REMOTE_USER") -Default "lcwt"
    $sshPort = Get-EnvValue -Names @("AGENT_S_REMOTE_SSH_PORT") -Default "10023"
    $remoteVllmPort = Get-EnvValue -Names @("AGENT_S_REMOTE_VLLM_PORT") -Default "8000"
    $autoStart = (Get-EnvValue -Names @("AGENT_S_REMOTE_AUTO_START") -Default "1").ToLower() -in @("1", "true", "yes", "on")
    $condaEnv = Get-EnvValue -Names @("AGENT_S_REMOTE_CONDA_ENV") -Default "vllm"
    $cudaDevices = Get-EnvValue -Names @("AGENT_S_REMOTE_CUDA_VISIBLE_DEVICES") -Default "1,3"
    $modelPath = Get-EnvValue -Names @("AGENT_S_REMOTE_MODEL_PATH") -Default "/mnt/data/Models/UI-TARS-1.5-7B"
    $tpSize = Get-EnvValue -Names @("AGENT_S_REMOTE_TP_SIZE") -Default "2"
    $remotePassword = Get-EnvValue -Names @("AGENT_S_REMOTE_PASSWORD")

    $remoteScriptPath = Join-Path $workDir "remote_vllm_tunnel.sh"
    $tunnelCmdPath = Join-Path $workDir "agent_s_tunnel.cmd"

    if (-not [string]::IsNullOrWhiteSpace($remotePassword)) {
        $tunnelOut = Join-Path $workDir "agent_s_paramiko_tunnel.out.log"
        $tunnelErr = Join-Path $workDir "agent_s_paramiko_tunnel.err.log"
        $tunnelArgs = @(
            (Join-Path $Root "scripts\ssh_vllm_tunnel.py"),
            "--local-port", "$portForTunnel",
            "--remote-host", "$remoteHost",
            "--remote-ssh-port", "$sshPort",
            "--remote-user", "$remoteUser",
            "--remote-password", "$remotePassword",
            "--remote-vllm-port", "$remoteVllmPort",
            "--conda-env", "$condaEnv",
            "--cuda-devices", "$cudaDevices",
            "--model-path", "$modelPath",
            "--served-model", "$servedModel",
            "--tp-size", "$tpSize"
        )
        if ($autoStart) {
            $tunnelArgs += "--auto-start"
        }
        Write-Host "[ssh] Starting password-based SSH tunnel in the background."
        Write-Host "[ssh] Logs: $tunnelOut ; $tunnelErr"
        Start-Process -FilePath $VenvPython -ArgumentList $tunnelArgs -WorkingDirectory $Root -RedirectStandardOutput $tunnelOut -RedirectStandardError $tunnelErr -WindowStyle Hidden | Out-Null
        Wait-GroundEndpoint -GroundUrl $groundUrl -Model $servedModel
        return
    }

    $ssh = (Get-Command ssh.exe -ErrorAction SilentlyContinue).Source
    if ([string]::IsNullOrWhiteSpace($ssh)) {
        throw "ssh.exe was not found. Install/enable Windows OpenSSH Client, or set AGENT_S_USE_SSH_TUNNEL=0 and provide a reachable AGENT_S_GROUND_URL."
    }

    $autoStartValue = if ($autoStart) { "1" } else { "0" }
    $remoteScript = @"
set -e
AUTO_START="$autoStartValue"
CONDA_ENV="$condaEnv"
CUDA_DEVICES="$cudaDevices"
MODEL_PATH="$modelPath"
SERVED_MODEL="$servedModel"
TP_SIZE="$tpSize"
VLLM_PORT="$remoteVllmPort"

if command -v conda >/dev/null 2>&1; then
  eval "`$(conda shell.bash hook)"
  conda activate "`$CONDA_ENV"
elif [ -f "`$HOME/miniconda3/etc/profile.d/conda.sh" ]; then
  . "`$HOME/miniconda3/etc/profile.d/conda.sh"
  conda activate "`$CONDA_ENV"
elif [ -f "`$HOME/anaconda3/etc/profile.d/conda.sh" ]; then
  . "`$HOME/anaconda3/etc/profile.d/conda.sh"
  conda activate "`$CONDA_ENV"
fi

if curl -fsS --max-time 3 "http://127.0.0.1:`$VLLM_PORT/v1/models" >/dev/null; then
  echo "[remote] vLLM is already ready on port `$VLLM_PORT."
elif [ "`$AUTO_START" = "1" ]; then
  if pgrep -af "vllm.entrypoints.openai.api_server.*`$SERVED_MODEL" >/dev/null 2>&1; then
    echo "[remote] vLLM process exists; waiting for API..."
  else
    echo "[remote] starting vLLM: `$SERVED_MODEL"
    nohup env CUDA_VISIBLE_DEVICES="`$CUDA_DEVICES" python -m vllm.entrypoints.openai.api_server \
      --model "`$MODEL_PATH" \
      --tensor-parallel-size "`$TP_SIZE" \
      --served-model-name "`$SERVED_MODEL" \
      --trust-remote-code \
      --host 0.0.0.0 \
      --port "`$VLLM_PORT" > "`$HOME/agent_s_vllm.log" 2>&1 &
  fi

  for i in `$(seq 1 120); do
    if curl -fsS --max-time 3 "http://127.0.0.1:`$VLLM_PORT/v1/models" >/dev/null; then
      echo "[remote] vLLM API is ready."
      break
    fi
    sleep 2
  done
else
  echo "[remote] vLLM is not ready and AGENT_S_REMOTE_AUTO_START=0."
fi

echo "[remote] SSH tunnel is open. Keep this window running."
while true; do sleep 3600; done
"@
    Set-Content -LiteralPath $remoteScriptPath -Value $remoteScript -Encoding ASCII

    $tunnelCmd = @"
@echo off
chcp 65001 >nul
cd /d "$Root"
echo [SSH] Enter the server password if prompted. Keep this window open while Agent-S3 is running.
echo [SSH] $remoteUser@${remoteHost}:$sshPort  localhost:$portForTunnel -^> remote:$remoteVllmPort
"$ssh" -tt -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -L 127.0.0.1:$portForTunnel`:127.0.0.1:$remoteVllmPort -p $sshPort $remoteUser@$remoteHost "bash -s" ^< "$remoteScriptPath"
echo.
echo [SSH] Tunnel closed.
pause
"@
    Set-Content -LiteralPath $tunnelCmdPath -Value $tunnelCmd -Encoding ASCII

    Write-Host "[ssh] Opening tunnel window. Type the server password in that window if prompted."
    Start-Process -FilePath $tunnelCmdPath -WorkingDirectory $Root | Out-Null

    Wait-GroundEndpoint -GroundUrl $groundUrl -Model $servedModel
}

function Test-ArchiveFeishuEnabled {
    return (Get-EnvValue -Names @("ARCHIVE_ENABLE_FEISHU_AGENT") -Default "0").ToLower() -in @("1", "true", "yes", "on")
}

Set-Location $Root
Ensure-EnvFile
Ensure-MainModelConfig
Ensure-Venv

if ($Mode -eq "tunnel") {
    Start-RemoteTunnelIfNeeded
    Write-Host "[ssh] Tunnel check complete."
    exit 0
}

if ($Mode -in @("run", "test", "feishu") -or (($Mode -in @("watch", "single")) -and (Test-ArchiveFeishuEnabled))) {
    Start-RemoteTunnelIfNeeded
}

if ($Mode -eq "setup") {
    Write-Host "[setup] Done."
    exit 0
}

if ($Mode -eq "test") {
    & $VenvPython (Join-Path $Root "scripts\test_endpoints.py")
    exit $LASTEXITCODE
}

if ($Mode -eq "watch") {
    & $VenvPython (Join-Path $Root "file_archive_watcher.py")
    exit $LASTEXITCODE
}

if ($Mode -eq "single") {
    & $VenvPython (Join-Path $Root "archive_feishu_once.py") @RemainingArgs
    exit $LASTEXITCODE
}

if ($Mode -eq "feishu") {
    & $VenvPython (Join-Path $Root "scripts\test_feishu_only.py") @RemainingArgs
    exit $LASTEXITCODE
}

$argsForCli = @()
if (-not [string]::IsNullOrWhiteSpace($Task)) {
    $argsForCli += @("--task", $Task)
}
if ($MaxSteps -gt 0) {
    $argsForCli += @("--max-agent-steps", "$MaxSteps")
}

& $VenvPython (Join-Path $Root "run_cli.py") @argsForCli
exit $LASTEXITCODE
