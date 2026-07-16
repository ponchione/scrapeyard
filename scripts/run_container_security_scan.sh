#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

IMAGE="${1:-scrapeyard:security-scan}"
OUTPUT_DIR="${SCRAPEYARD_SECURITY_REPORT_DIR:-$ROOT_DIR/artifacts/container-security}"
SYFT_IMAGE="anchore/syft:v1.44.0@sha256:86fde6445b483d902fe011dd9f68c4987dd94e07da1e9edc004e3c2422650de6"
TRIVY_IMAGE="aquasec/trivy:0.72.0@sha256:cffe3f5161a47a6823fbd23d985795b3ed72a4c806da4c4df16266c02accdd6f"
REDIS_IMAGE="redis:7.4.9-alpine3.21@sha256:6ab0b6e7381779332f97b8ca76193e45b0756f38d4c0dcda72dbb3c32061ab99"
SCANNER_NETWORK="${SCRAPEYARD_SCANNER_NETWORK:-}"
SCANNER_NETWORK_ARGS=()
if [[ -n "$SCANNER_NETWORK" ]]; then
  SCANNER_NETWORK_ARGS=(--network "$SCANNER_NETWORK")
fi
SCANNER_USER="$(id -u):$(id -g)"
DOCKER_SOCKET_GID="$(stat -c '%g' /var/run/docker.sock)"

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
IMAGE_ID="$(docker image inspect --format '{{.Id}}' "$IMAGE")"

mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"
SYFT_TMP="$OUTPUT_DIR/.syft-tmp"
TRIVY_TMP="$OUTPUT_DIR/.trivy-tmp"
manifest_container=""
cleanup() {
  if [[ -n "$manifest_container" ]]; then
    docker rm -f "$manifest_container" >/dev/null 2>&1 || true
  fi
  rm -rf "$SYFT_TMP" "$TRIVY_TMP"
}
trap cleanup EXIT
rm -f \
  "$OUTPUT_DIR/sbom.cdx.json" \
  "$OUTPUT_DIR/browser-runtime.json" \
  "$OUTPUT_DIR/vulnerabilities.json" \
  "$OUTPUT_DIR/redis-vulnerabilities.json" \
  "$OUTPUT_DIR/configuration.json"
rm -rf "$SYFT_TMP" "$TRIVY_TMP"
mkdir -p "$SYFT_TMP" "$TRIVY_TMP"

docker run --rm \
  "${SCANNER_NETWORK_ARGS[@]}" \
  --user "$SCANNER_USER" \
  --group-add "$DOCKER_SOCKET_GID" \
  --env HOME=/tmp \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  -v "$OUTPUT_DIR:/out" \
  -v "$SYFT_TMP:/tmp" \
  "$SYFT_IMAGE" \
  "docker:$IMAGE_ID" -o cyclonedx-json=/out/sbom.cdx.json

manifest_container="$(docker create "$IMAGE_ID" true)"
docker cp \
  "$manifest_container:/usr/share/scrapeyard-browser-runtime.json" \
  "$OUTPUT_DIR/browser-runtime.json"
docker rm "$manifest_container" >/dev/null
manifest_container=""

python3 scripts/audit_browser_security.py \
  --policy security/browser-policy.json \
  --dockerfile Dockerfile \
  --manifest "$OUTPUT_DIR/browser-runtime.json"
python3 scripts/augment_browser_sbom.py \
  "$OUTPUT_DIR/sbom.cdx.json" \
  "$OUTPUT_DIR/browser-runtime.json"

scan_image() {
  local target=$1
  local output=$2
  docker run --rm \
    "${SCANNER_NETWORK_ARGS[@]}" \
    --user "$SCANNER_USER" \
    --group-add "$DOCKER_SOCKET_GID" \
    --env HOME=/tmp \
    -v /var/run/docker.sock:/var/run/docker.sock:ro \
    -v "$OUTPUT_DIR:/out" \
    -v "$TRIVY_TMP:/tmp" \
    "$TRIVY_IMAGE" image \
    --cache-dir /tmp/trivy-cache \
    --scanners vuln,secret \
    --severity MEDIUM,HIGH,CRITICAL \
    --format json \
    --output "/out/$output" \
    --exit-code 0 \
    "$target"
}

scan_image "$IMAGE_ID" vulnerabilities.json
docker pull "$REDIS_IMAGE" >/dev/null
REDIS_IMAGE_ID="$(docker image inspect --format '{{.Id}}' "$REDIS_IMAGE")"
scan_image "$REDIS_IMAGE_ID" redis-vulnerabilities.json

docker run --rm \
  "${SCANNER_NETWORK_ARGS[@]}" \
  --user "$SCANNER_USER" \
  --env HOME=/tmp \
  -v "$ROOT_DIR:/workspace:ro" \
  -v "$OUTPUT_DIR:/out" \
  -v "$TRIVY_TMP:/tmp" \
  "$TRIVY_IMAGE" config \
  --severity MEDIUM,HIGH,CRITICAL \
  --format json \
  --output /out/configuration.json \
  --exit-code 0 \
  /workspace

python3 scripts/evaluate_container_scan.py \
  --exceptions security/container-scan-exceptions.json \
  "$OUTPUT_DIR/vulnerabilities.json" \
  "$OUTPUT_DIR/redis-vulnerabilities.json" \
  "$OUTPUT_DIR/configuration.json"

python3 - "$OUTPUT_DIR/sbom.cdx.json" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
components = payload.get("components", [])
if not components:
    raise SystemExit("generated SBOM contains no components")
browser_components = {
    component.get("name"): component.get("version")
    for component in components
    if isinstance(component, dict) and component.get("name") in {
        "scrapeyard-chromium-browser-binary",
        "scrapeyard-camoufox-browser-binary",
    }
}
if set(browser_components) != {
    "scrapeyard-chromium-browser-binary",
    "scrapeyard-camoufox-browser-binary",
}:
    raise SystemExit("generated SBOM omitted executed browser binaries")
print(f"SBOM components: {len(components)}")
print(f"SBOM browser components: {browser_components}")
PY

sha256sum "$OUTPUT_DIR/sbom.cdx.json" > "$OUTPUT_DIR/sbom.cdx.json.sha256"
docker image inspect "$IMAGE_ID" > "$OUTPUT_DIR/image-inspect.json"
docker history --no-trunc "$IMAGE_ID" > "$OUTPUT_DIR/image-history.txt"
printf 'image_id=%s\n' "$IMAGE_ID" > "$OUTPUT_DIR/candidate.txt"
printf 'egress_probe_image=%s\nredis_image=%s\n' \
  "$IMAGE_ID" "$REDIS_IMAGE" > "$OUTPUT_DIR/dependencies.txt"

cleanup
trap - EXIT

echo "Container SBOM, vulnerability scan, and configuration scan passed: $OUTPUT_DIR"
