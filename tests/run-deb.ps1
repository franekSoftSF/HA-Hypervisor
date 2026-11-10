# Builds the .deb and tests it on clean systems against the e2e stack.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$dist = Join-Path $root 'dist'
$compose = Join-Path $PSScriptRoot 'e2e\docker-compose.yml'
$network = 'ha-hypervisor-e2e_default'
$failed = @()

New-Item -ItemType Directory -Force $dist | Out-Null
docker run --rm -v "${root}:/src:ro" -v "${dist}:/out" python:3.14-slim sh /src/packaging/deb/build.sh
if ($LASTEXITCODE -ne 0) { Write-Host 'BUILD FAILED'; exit 1 }

docker compose -f $compose up -d --build mosquitto vcsim esxi pve 2>&1 | Out-Null
Start-Sleep -Seconds 3

foreach ($image in 'debian:12', 'debian:13', 'ubuntu:24.04') {
    Write-Host "=== $image"
    docker run --rm --network $network -v "${dist}:/debs:ro" -v "${root}\tests\e2e:/t:ro" $image sh /t/deb_test.sh
    if ($LASTEXITCODE -ne 0) { $failed += $image }
}

Write-Host '=== systemd'
docker build -q -f "$PSScriptRoot\docker\systemd.Dockerfile" -t ha-hypervisor-test-systemd "$PSScriptRoot\docker" | Out-Null
$name = 'ha-hypervisor-systemd-test'
docker rm -f $name 2>&1 | Out-Null
docker run -d --name $name --privileged --cgroupns=host --network $network -v "${dist}:/debs:ro" -v "${root}\tests\e2e:/t:ro" ha-hypervisor-test-systemd | Out-Null
Start-Sleep -Seconds 5
docker exec $name sh /t/deb_systemd_test.sh
if ($LASTEXITCODE -ne 0) { $failed += 'systemd' }
docker rm -f $name 2>&1 | Out-Null

docker compose -f $compose down -v 2>&1 | Out-Null
if ($failed) { Write-Host "FAILED: $($failed -join ', ')"; exit 1 }
Write-Host 'ALL DEB TESTS PASSED'
