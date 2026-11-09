# Runs everything in Docker: agent tests, integration tests on the real
# Home Assistant image, hassfest and, with -E2E, the full stack.
param([switch]$E2E)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$docker = Join-Path $PSScriptRoot 'docker'
$failed = @()

function Invoke-Step($name, [scriptblock]$action) {
    Write-Host "=== $name"
    & $action
    if ($LASTEXITCODE -ne 0) { $script:failed += $name }
}

docker pull -q ghcr.io/home-assistant/home-assistant:stable | Out-Null
docker build -q -f "$docker\agent.Dockerfile" -t ha-vmware-test-agent $docker | Out-Null
docker build -q -f "$docker\integration.Dockerfile" -t ha-vmware-test-ha $docker | Out-Null

Invoke-Step 'Agent tests' {
    docker run --rm -v "${root}:/src" -w /src -e PYTHONPATH=/src/agent:/src/tests/agent `
        -e PYTHONDONTWRITEBYTECODE=1 ha-vmware-test-agent pytest tests/agent -q -p no:cacheprovider
}
Invoke-Step 'Integration tests' {
    docker run --rm -v "${root}:/src" -w /src -e PYTHONPATH=/src:/src/tests/integration `
        -e PYTHONDONTWRITEBYTECODE=1 ha-vmware-test-ha python3 -c `
        'import homeassistant, pytest, sys; sys.exit(pytest.main(sys.argv[1:]))' `
        tests/integration -q -p no:cacheprovider --asyncio-mode=auto --tb=short --show-capture=no
}
Invoke-Step 'hassfest' {
    docker run --rm -v "$root\custom_components:/github/workspace/custom_components:ro" `
        -w /github/workspace -e GITHUB_WORKSPACE=/github/workspace ghcr.io/home-assistant/hassfest:latest
}
if ($E2E) {
    $compose = Join-Path $PSScriptRoot 'e2e\docker-compose.yml'
    docker compose -f $compose down -v 2>&1 | Out-Null
    docker compose -f $compose up -d --build 2>&1 | Out-Null
    Invoke-Step 'End-to-end' { docker compose -f $compose run --rm driver 2>&1 }
    docker compose -f $compose down -v 2>&1 | Out-Null
}

if ($failed) { Write-Host "FAILED: $($failed -join ', ')"; exit 1 }
Write-Host 'ALL PASSED'
