#!/usr/bin/env sh
# Builds the PatchProof execution sandbox image (macOS/Linux).
set -eu
root="$(cd "$(dirname "$0")/.." && pwd)"
docker build -t patchproof-sandbox:0.1 "$root/backend/src/patchproof/sandbox/image"
