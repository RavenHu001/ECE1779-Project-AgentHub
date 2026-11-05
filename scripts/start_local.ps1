# Start the same Compose application used in Linux CI without requiring host Python.
# 启动与 Linux CI 一致的 Compose 应用，主机无需安装 Python。
$ErrorActionPreference = 'Stop'
# Resolve paths from the script so invocation works from any current directory.
# 基于脚本所在位置定位仓库，保证从其他目录调用时也使用正确的配置与构建上下文。
$repositoryPath = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $repositoryPath
try {
    # Prefer PATH, then try standard Docker Desktop locations for older terminal sessions.
    # 优先使用 PATH 中的 Docker；旧终端未刷新 PATH 时，再查找常见的安装位置。
    $dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
    $dockerExecutable = if ($dockerCommand) { $dockerCommand.Source } else {
        $candidates = @(
            "$env:ProgramFiles\Docker\Docker\resources\bin\docker.exe",
            "$env:LOCALAPPDATA\Programs\DockerDesktop\resources\bin\docker.exe"
        )
        $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    }
    if (-not $dockerExecutable) { throw 'Install and start Docker Desktop, then run this script again.' }
    # Finding the CLI is not enough: check that the Linux-container engine is running.
    # 找到命令行程序并不代表引擎可用，因此先确认 Linux 容器引擎已经启动。
    & $dockerExecutable info --format '{{.ServerVersion}}'
    if ($LASTEXITCODE -ne 0) {
        throw 'Docker Engine is unavailable. Start Docker Desktop. Restart Windows if WSL setup requested it.'
    }
    if (-not (Test-Path -LiteralPath '.env')) {
        # Run only the standard-library initializer in a disposable container; preserve existing settings.
        # 使用一次性容器运行仅依赖标准库的初始化脚本，并保留已有的私有配置。
        & $dockerExecutable run --rm --mount "type=bind,source=$repositoryPath,target=/workspace" --workdir /workspace python:3.13-slim-bookworm python scripts/init_env.py
        if ($LASTEXITCODE -ne 0) { throw 'Could not initialize local configuration.' }
    }
    # Validate quietly so resolved credentials never appear in the terminal output.
    # 静默验证 Compose 配置，避免把解析后的私有凭据打印到终端。
    & $dockerExecutable compose config --quiet
    if ($LASTEXITCODE -ne 0) { throw 'Compose configuration is invalid.' }
    # Rebuild source into the shared image and wait for dependencies and API health.
    # 将代码重新构建到共用镜像中，并等待依赖服务和 API 健康检查就绪。
    & $dockerExecutable compose up -d --build --wait
    if ($LASTEXITCODE -ne 0) { throw 'Application startup failed. Inspect docker compose logs.' }
    # Exercise the real API-to-worker path without copying credentials onto the command line.
    # 验证真实 API 到独立 Worker 的链路，同时不把凭据复制到命令行参数中。
    & $dockerExecutable compose exec -T api python -m app.smoke
    if ($LASTEXITCODE -ne 0) { throw 'Live API/worker smoke check failed.' }
    # Read only the public port for the final URL; do not print bootstrap passwords.
    # 只读取公开端口以生成最终地址，不输出初始化账号的密码。
    $portLine = Get-Content -LiteralPath '.env' | Where-Object { $_ -match '^API_PORT=' } | Select-Object -First 1
    $apiPort = if ($portLine) { $portLine.Substring('API_PORT='.Length) } else { '8000' }
    Write-Output "AgentHub is ready at http://127.0.0.1:$apiPort/"
    Write-Output 'Use the bootstrap username and password in your ignored .env file to sign in.'
} finally {
    # Restore the caller's directory even when initialization, startup, or smoke checks fail.
    # 即使初始化、启动或冒烟检查失败，也恢复调用者原来的工作目录。
    Pop-Location
}
