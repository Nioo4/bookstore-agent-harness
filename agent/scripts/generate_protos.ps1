param([string]$Python)

$ErrorActionPreference = "Stop"
$AgentRoot = Split-Path -Parent $PSScriptRoot
$RepositoryRoot = Split-Path -Parent $AgentRoot
if ([string]::IsNullOrWhiteSpace($Python)) {
    $Python = Join-Path $AgentRoot ".venv\Scripts\python.exe"
}

Push-Location $RepositoryRoot
try {
    & $Python -m grpc_tools.protoc -I . --python_out=agent --grpc_python_out=agent rpc/check/check.proto rpc/order/bookstore_order.proto
    if ($LASTEXITCODE -ne 0) {
        throw "gRPC Python code generation failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}
