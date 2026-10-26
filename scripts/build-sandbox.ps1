# Builds the PatchProof execution sandbox image (Windows PowerShell).
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
docker build -t patchproof-sandbox:0.1 (Join-Path $root "backend/src/patchproof/sandbox/image")
