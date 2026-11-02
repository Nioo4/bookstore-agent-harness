[CmdletBinding()]
param(
    [string]$Project = 'bookstore-stage11'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# This launcher is deliberately limited to an isolated Compose project.  It
# never performs volume removal, table reset, cache flush, or any other
# destructive operation. Existing rows and volumes are therefore retained; the
# seed command below is append-only and skips existing ISBNs.
if ($Project -notmatch '^bookstore-(?:stage11|test|v2-fixture)[a-z0-9-]*$') {
    throw 'TEST_PROJECT_REQUIRED: use bookstore-stage11, bookstore-test..., or bookstore-v2-fixture...'
}

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repoRoot

# The overlay must be self-contained and must never inherit a real provider
# key or endpoint from .env.  These are local fixture values, not credentials.
$fixtureEnvironmentNames = @('MODEL_BASE_URL', 'MODEL_API_KEY', 'EMBEDDING_BASE_URL', 'EMBEDDING_API_KEY', 'APP_PORT')
$previousEnvironment = @{}
foreach ($name in $fixtureEnvironmentNames) {
    $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
$env:MODEL_BASE_URL = 'http://model-fixture:8010/v1'
$env:MODEL_API_KEY = 'local-e2e-fixture-not-a-provider-secret'
$env:EMBEDDING_BASE_URL = 'http://model-fixture:8010/v1'
$env:EMBEDDING_API_KEY = 'local-e2e-embedding-not-a-provider-secret'
$env:APP_PORT = '8088'

try {
$compose = @(
    'compose', '--profile', 'v2-app', '-p', $Project,
    '-f', 'compose.yaml', '-f', 'compose.e2e.yaml'
)

function Invoke-Compose {
    param([Parameter(Mandatory = $true)][string[]]$CommandArgs)

    & docker @compose @CommandArgs
    if ($LASTEXITCODE -ne 0) {
        throw 'DOCKER_COMPOSE_COMMAND_FAILED'
    }
}

function Invoke-HostPython {
    param([Parameter(Mandatory = $true)][string[]]$CommandArgs)

    & $script:python @CommandArgs
    if ($LASTEXITCODE -ne 0) {
        throw 'HOST_PYTHON_COMMAND_FAILED'
    }
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'DOCKER_REQUIRED'
}

$script:python = Join-Path $repoRoot 'agent\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $script:python -PathType Leaf)) {
    $script:python = 'python'
}

# Parse and validate the merged graph before creating or starting anything.
Invoke-Compose @('config', '--quiet')
$serviceText = (& docker @compose config --services | Out-String)
if ($LASTEXITCODE -ne 0) {
    throw 'COMPOSE_SERVICE_GRAPH_INVALID'
}
$services = @($serviceText -split "\r?\n" | Where-Object { $_ -ne '' })
foreach ($required in @('mysql', 'redis', 'qdrant', 'mailpit', 'migrate', 'bootstrap', 'bookstore-service', 'model-fixture', 'agent-api', 'agent-worker', 'nginx')) {
    if ($services -notcontains $required) {
        throw "V2_SERVICE_MISSING:$required"
    }
}
if ($services -contains 'knowledge-index') {
    throw 'FORBIDDEN_KNOWLEDGE_INDEX_SERVICE'
}

# A second fixture project cannot bind the same fixed test subnet.  Fail before
# Compose creates any new volume rather than leaving a partially initialized
# project behind.  The existing owner is never inspected beyond its network
# name/subnet and is never stopped or removed by this script.
$fixtureNetwork = "${Project}_bookstore"
$fixtureSubnet = '172.31.241.0/24'
$networkNames = @((& docker network ls --format '{{.Name}}') | Where-Object { $_ })
foreach ($networkName in $networkNames) {
    $subnets = @((& docker network inspect $networkName --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}' 2>$null) | Where-Object { $_ })
    if (($subnets -contains $fixtureSubnet) -and ($networkName -ne $fixtureNetwork)) {
        throw "FIXTURE_NETWORK_BUSY:$networkName"
    }
}

Write-Output "V2_FIXTURE_PHASE_1 project=$Project"
Invoke-Compose @(
    'up', '-d', '--build',
    'mysql', 'redis', 'qdrant', 'mailpit', 'migrate', 'bootstrap', 'bookstore-service', 'model-fixture'
)

Write-Output 'V2_FIXTURE_APPEND_ONLY_SEED'
$dataset = Get-Content -LiteralPath (Join-Path $repoRoot 'deploy\datasets\manual-v1.json') -Raw | ConvertFrom-Json
if ($dataset.books.Count -ne 100) {
    throw 'MANUAL_DATASET_MUST_CONTAIN_100_BOOKS'
}
Invoke-HostPython @('deploy/seed_manual_dataset.py', '--project', $Project, '--apply')

Write-Output 'V2_FIXTURE_PHASE_2_RAG_INDEX'
# The command is explicit and fixture-backed.  The RAG manifest is written to
# the shared named volume; app startup is intentionally not used as an index
# side effect.  These frozen chunk/threshold values prove the protocol only;
# they are not tuning evidence for the real Qwen embedding space.
Invoke-Compose @(
    'run', '--rm', '--no-deps', 'agent-api',
    'python', '-m', 'app.rag_index', 'build', '--corpus', 'all',
    '--fixture-parameters',
    '--chunk-target', '500', '--chunk-overlap', '80',
    '--policy-threshold', '0.01', '--book-threshold', '0.01',
    '--allow-provider-calls'
)

Write-Output 'V2_FIXTURE_PHASE_2_SERVICES'
Invoke-Compose @('up', '-d', 'agent-api', 'agent-worker', 'nginx')
Invoke-Compose @('ps', '-a')
Write-Output 'V2_FIXTURE_READY (fixture provider; real local infrastructure; no paid model calls)'
}
finally {
    foreach ($name in $fixtureEnvironmentNames) {
        $value = $previousEnvironment[$name]
        if ($null -eq $value) {
            Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
        } else {
            [Environment]::SetEnvironmentVariable($name, $value, 'Process')
        }
    }
}
