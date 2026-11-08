param(
    [string]$GpuPython = '.venv-train/Scripts/python.exe',
    [string]$BackendPython = '.venv-dev/Scripts/python.exe'
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$activePorts = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners().Port
if (($activePorts -contains 8001) -or ($activePorts -contains 8002)) {
    throw 'Port 8001 or 8002 is occupied. Inspect the existing service before replacing it.'
}
if (-not (Test-Path -LiteralPath '.env')) { throw 'Create the local .env from .env.example first.' }
New-Item -ItemType Directory -Path data -Force | Out-Null
# These overrides are inherited by both child processes and take precedence over .env.
$env:CQA_DATABASE_PATH = 'data/local-ai-service.sqlite3'
$env:CQA_AI_PROVIDER = 'local-http'
$env:CQA_LOCAL_AI_URL = 'http://127.0.0.1:8002'
$env:CQA_LLM_API_ENABLED = '0'
$env:CQA_AI_CONCURRENCY = '1'
$env:HF_HUB_OFFLINE = '1'
$env:HF_HUB_DISABLE_TELEMETRY = '1'
$env:CQA_KEEP_AWAKE = '1'
$gpuProcess = Start-Process -FilePath (Join-Path $projectRoot $GpuPython) -ArgumentList @('-m','uvicorn','ai.worker:app','--env-file','.env','--host','127.0.0.1','--port','8002','--workers','1') -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $projectRoot 'data/local-worker.stdout.log') -RedirectStandardError (Join-Path $projectRoot 'data/local-worker.stderr.log')
$gpuProcess.Id | Set-Content -LiteralPath data/local-worker.pid
$ready = $false
for ($attempt = 0; $attempt -lt 90; $attempt++) {
    Start-Sleep -Seconds 1
    try {
        $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8002/health' -TimeoutSec 2
        if ($health.ai_configured -and $health.usage_is_local) { $ready = $true; break }
    } catch { }
    if ($gpuProcess.HasExited) { throw 'GPU worker exited. Inspect data/local-worker.stderr.log.' }
}
if (-not $ready) { throw 'Worker did not become ready; backend was not started.' }
$backendProcess = Start-Process -FilePath (Join-Path $projectRoot $BackendPython) -ArgumentList @('-m','uvicorn','ai.app:app','--env-file','.env','--host','127.0.0.1','--port','8001','--workers','1') -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $projectRoot 'data/local-api.stdout.log') -RedirectStandardError (Join-Path $projectRoot 'data/local-api.stderr.log')
$backendProcess.Id | Set-Content -LiteralPath data/local-api.pid
Write-Output ('Started GPU worker ' + $gpuProcess.Id + ' and backend ' + $backendProcess.Id)
