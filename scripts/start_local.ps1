# Start the same Compose application used in Linux CI without requiring host Python.
$ErrorActionPreference = 'Stop'
$repositoryPath = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $repositoryPath
try {
    $dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
    $dockerExecutable = if ($dockerCommand) { $dockerCommand.Source } else {
        $candidates = @(
            "$env:ProgramFiles\Docker\Docker\resources\bin\docker.exe",
            "$env:LOCALAPPDATA\Programs\DockerDesktop\resources\bin\docker.exe"
        )
        $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    }
    if (-not $dockerExecutable) { throw 'Install and start Docker Desktop, then run this script again.' }
    & $dockerExecutable info --format '{{.ServerVersion}}'
    if ($LASTEXITCODE -ne 0) {
        throw 'Docker Engine is unavailable. Start Docker Desktop. Restart Windows if WSL setup requested it.'
    }
    if (-not (Test-Path -LiteralPath '.env')) {
        & $dockerExecutable run --rm --mount "type=bind,source=$repositoryPath,target=/workspace" --workdir /workspace python:3.13-slim-bookworm python scripts/init_env.py
        if ($LASTEXITCODE -ne 0) { throw 'Could not initialize local configuration.' }
    }
    & $dockerExecutable compose config --quiet
    if ($LASTEXITCODE -ne 0) { throw 'Compose configuration is invalid.' }
    & $dockerExecutable compose up -d --build --wait
    if ($LASTEXITCODE -ne 0) { throw 'Application startup failed. Inspect docker compose logs.' }
    & $dockerExecutable compose exec -T api python -m app.smoke
    if ($LASTEXITCODE -ne 0) { throw 'Live API/worker smoke check failed.' }
    $portLine = Get-Content -LiteralPath '.env' | Where-Object { $_ -match '^API_PORT=' } | Select-Object -First 1
    $apiPort = if ($portLine) { $portLine.Substring('API_PORT='.Length) } else { '8000' }
    Write-Output "AgentHub is ready at http://127.0.0.1:$apiPort/"
    Write-Output 'Use the bootstrap username and password in your ignored .env file to sign in.'
} finally {
    Pop-Location
}
