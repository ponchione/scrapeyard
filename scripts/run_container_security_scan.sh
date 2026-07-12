#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

IMAGE="${1:-scrapeyard:security-scan}"
OUTPUT_DIR="${SCRAPEYARD_SECURITY_REPORT_DIR:-$ROOT_DIR/artifacts/container-security}"
SYFT_IMAGE="anchore/syft:v1.27.1@sha256:844ed6a928ef9396fac26d1de374e71dcaf80df14f05841670ed41619c5a718f"
TRIVY_IMAGE="aquasec/trivy:0.65.0@sha256:a22415a38938a56c379387a8163fcb0ce38b10ace73e593475d3658d578b2436"

for command in docker mkdir python3; do
  command -v "$command" >/dev/null 2>&1 || {
    echo "container security scan: $command is required" >&2
    exit 2
  }
done
docker image inspect "$IMAGE" >/dev/null 2>&1 || {
  echo "container security scan: image not found: $IMAGE" >&2
  exit 2
}

mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"

docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  -v "$OUTPUT_DIR:/out" \
  "$SYFT_IMAGE" \
  "docker:$IMAGE" -o cyclonedx-json=/out/sbom.cdx.json

docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  -v "$OUTPUT_DIR:/out" \
  "$TRIVY_IMAGE" image \
  --cache-dir /tmp/trivy-cache \
  --scanners vuln \
  --ignore-unfixed \
  --severity HIGH,CRITICAL \
  --format json \
  --output /out/vulnerabilities.json \
  --exit-code 1 \
  "$IMAGE"

docker run --rm \
  -v "$ROOT_DIR:/workspace:ro" \
  -v "$OUTPUT_DIR:/out" \
  "$TRIVY_IMAGE" config \
  --severity HIGH,CRITICAL \
  --format json \
  --output /out/configuration.json \
  --exit-code 1 \
  /workspace

python3 - "$OUTPUT_DIR/sbom.cdx.json" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
components = payload.get("components", [])
if not components:
    raise SystemExit("generated SBOM contains no components")
print(f"SBOM components: {len(components)}")
PY

echo "Container SBOM, vulnerability scan, and configuration scan passed: $OUTPUT_DIR"
