# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

ARG PYTHON_IMAGE="python:3.14.8-slim-bookworm@sha256:48b13b003dda20b16f9442b8475aa05fe21bf6579a8c881db92ffb4d8fd20f83"

# Export the production lock with the same pinned toolchain used by CI.
FROM ${PYTHON_IMAGE} AS builder

ARG PIP_VERSION=26.2.2
ARG POETRY_VERSION=2.3.4
ARG POETRY_EXPORT_VERSION=1.10.0
RUN pip install --no-cache-dir --upgrade "pip==${PIP_VERSION}" && \
    pip install --no-cache-dir \
      "poetry==${POETRY_VERSION}" \
      "poetry-plugin-export==${POETRY_EXPORT_VERSION}"

WORKDIR /build
COPY scripts/check_packaging_toolchain.py scripts/check_packaging_toolchain.py
RUN python scripts/check_packaging_toolchain.py
COPY pyproject.toml poetry.lock ./
RUN poetry export --only main -f requirements.txt -o requirements.txt

# Runtime inputs are immutable or explicitly recorded in OCI labels/SBOM.
FROM ${PYTHON_IMAGE} AS runtime

ARG PIP_VERSION=26.2.2
ARG PLAYWRIGHT_VERSION=1.58.0
ARG PLAYWRIGHT_CHROMIUM_REVISION=1208
ARG REBROWSER_PLAYWRIGHT_VERSION=1.52.0
ARG REBROWSER_CHROMIUM_REVISION=1169
ARG CAMOUFOX_PACKAGE_VERSION=0.4.11
ARG CAMOUFOX_BROWSER_VERSION=135.0.1
ARG CAMOUFOX_BROWSER_RELEASE=beta.24
ARG CAMOUFOX_BROWSER_SHA256=61e1ec455e021720af38a5cc5ff7566121363cb5b82b72f24e381ba2676a4888
ARG UBO_VERSION=1.72.2
ARG UBO_FILE_ID=4888680
ARG UBO_SHA256=40c315b0da7871868155ecfae7a50a58dfa0920aebd865e008214986f1b7c578
# Both Python drivers ship their own Node (playwright 1.58.0: 24.13.0,
# rebrowser-playwright 1.52.0: 22.14.0), and no release of either carries a
# Node with the October 21, 2026 security fixes. The image runs both on this
# Node instead. The hash is the one in the release's SHASUMS256.txt, whose
# signature verified against releaser key
# 5BE8A3F6C8A5C01D106C0AD820B1A390B168D356 in github.com/nodejs/release-keys.
ARG NODE_VERSION=24.22.1
ARG NODE_SHA256=eb6728e77c576a35162c122c45ccadf224d89ff00abc8dae46cc2897c6002d7c
ARG SCRAPEYARD_UID=10001
ARG SCRAPEYARD_GID=10001
ARG TARGETARCH
ARG DEBIAN_SNAPSHOT=20261022T135713Z

# The python images set LANG only up to 3.12. unzip needs a UTF-8 locale for
# Camoufox's font names, and the browsers keep the locale they ran with.
ENV LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/scrapeyard \
    TMPDIR=/tmp \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PLAYWRIGHT_NODEJS_PATH=/opt/node/bin/node \
    XDG_CACHE_HOME=/opt/scrapeyard-cache

LABEL org.opencontainers.image.base.name="python:3.14.8-slim-bookworm" \
      org.opencontainers.image.base.digest="sha256:48b13b003dda20b16f9442b8475aa05fe21bf6579a8c881db92ffb4d8fd20f83" \
      org.scrapeyard.os.snapshot="${DEBIAN_SNAPSHOT}" \
      org.scrapeyard.runtime.uid="10001" \
      org.scrapeyard.browser.playwright.version="${PLAYWRIGHT_VERSION}" \
      org.scrapeyard.browser.playwright.chromium-revision="${PLAYWRIGHT_CHROMIUM_REVISION}" \
      org.scrapeyard.browser.rebrowser-playwright.version="${REBROWSER_PLAYWRIGHT_VERSION}" \
      org.scrapeyard.browser.rebrowser-playwright.chromium-revision="${REBROWSER_CHROMIUM_REVISION}" \
      org.scrapeyard.browser.camoufox-package.version="${CAMOUFOX_PACKAGE_VERSION}" \
      org.scrapeyard.browser.camoufox.version="${CAMOUFOX_BROWSER_VERSION}" \
      org.scrapeyard.browser.camoufox.release="${CAMOUFOX_BROWSER_RELEASE}" \
      org.scrapeyard.browser.camoufox.sha256="${CAMOUFOX_BROWSER_SHA256}" \
      org.scrapeyard.browser.ubo.version="${UBO_VERSION}" \
      org.scrapeyard.browser.ubo.sha256="${UBO_SHA256}" \
      org.scrapeyard.browser.driver-node.version="${NODE_VERSION}" \
      org.scrapeyard.browser.driver-node.sha256="${NODE_SHA256}"

WORKDIR /app
COPY --from=builder /build/requirements.txt .

# Browser binaries are installed at build time. Runtime sandboxing uses
# unprivileged user namespaces instead of a revision-specific setuid helper.
# Every locked dependency ships a wheel, so nothing is compiled. Scrapling
# launches the full Chromium build (channel "chromium"), never the headless
# shell, so the shells are not installed.
RUN test "${TARGETARCH:-amd64}" = "amd64" && \
    printf '%s\n' \
      'Types: deb' \
      "URIs: https://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}" \
      'Suites: bookworm bookworm-updates' \
      'Components: main' \
      'Check-Valid-Until: no' \
      '' \
      'Types: deb' \
      "URIs: https://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}" \
      'Suites: bookworm-security' \
      'Components: main' \
      'Check-Valid-Until: no' \
      > /etc/apt/sources.list.d/debian.sources && \
    apt-get update && \
    apt-get upgrade -y --no-install-recommends && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        libdbus-glib-1-2 \
        libgtk-3-0 \
        unzip \
    && pip install --no-cache-dir --upgrade "pip==${PIP_VERSION}" \
    && pip install --no-cache-dir --only-binary=:all: -r requirements.txt \
    && groupadd --gid "${SCRAPEYARD_GID}" scrapeyard \
    && useradd --uid "${SCRAPEYARD_UID}" --gid "${SCRAPEYARD_GID}" \
        --create-home --home-dir /home/scrapeyard --shell /usr/sbin/nologin scrapeyard \
    && mkdir -p \
        /data/db /data/results /data/adaptive /data/logs \
        "${PLAYWRIGHT_BROWSERS_PATH}" "${XDG_CACHE_HOME}/camoufox" \
    && curl --fail --location --retry 3 \
        --output /tmp/node.tar.gz \
        "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.gz" \
    && echo "${NODE_SHA256}  /tmp/node.tar.gz" | sha256sum --check --strict \
    && mkdir -p "$(dirname "${PLAYWRIGHT_NODEJS_PATH}")" \
    && tar -xzf /tmp/node.tar.gz --no-same-owner --strip-components=2 \
        -C "$(dirname "${PLAYWRIGHT_NODEJS_PATH}")" "node-v${NODE_VERSION}-linux-x64/bin/node" \
    && rm /tmp/node.tar.gz \
    && test "$("${PLAYWRIGHT_NODEJS_PATH}" --version)" = "v${NODE_VERSION}" \
    && for driver in playwright rebrowser_playwright; do \
        bundled="$(python -c "import pathlib, ${driver}; print(pathlib.Path(${driver}.__file__).parent/'driver'/'node')")" \
        && rm "${bundled}" \
        && ln -s "${PLAYWRIGHT_NODEJS_PATH}" "${bundled}" \
        && test "$("${bundled}" --version)" = "v${NODE_VERSION}" \
        || exit 1; \
    done \
    && python -m playwright install --with-deps --no-shell chromium \
    && python -m rebrowser_playwright install --no-shell chromium \
    && curl --fail --location --retry 3 \
        --output /tmp/camoufox.zip \
        "https://github.com/daijro/camoufox/releases/download/v${CAMOUFOX_BROWSER_VERSION}-${CAMOUFOX_BROWSER_RELEASE}/camoufox-${CAMOUFOX_BROWSER_VERSION}-${CAMOUFOX_BROWSER_RELEASE}-lin.x86_64.zip" \
    && echo "${CAMOUFOX_BROWSER_SHA256}  /tmp/camoufox.zip" | sha256sum --check --strict \
    && unzip -q /tmp/camoufox.zip -d "${XDG_CACHE_HOME}/camoufox" \
    && curl --fail --location --retry 3 \
        --output /tmp/ublock-origin.xpi \
        "https://addons.mozilla.org/firefox/downloads/file/${UBO_FILE_ID}/ublock_origin-${UBO_VERSION}.xpi" \
    && echo "${UBO_SHA256}  /tmp/ublock-origin.xpi" | sha256sum --check --strict \
    && mkdir -p "${XDG_CACHE_HOME}/camoufox/addons/UBO" \
    && unzip -q /tmp/ublock-origin.xpi -d "${XDG_CACHE_HOME}/camoufox/addons/UBO" \
    && printf '{"version":"%s","release":"%s"}\n' \
        "${CAMOUFOX_BROWSER_VERSION}" "${CAMOUFOX_BROWSER_RELEASE}" \
        > "${XDG_CACHE_HOME}/camoufox/version.json" \
    && rm /tmp/camoufox.zip /tmp/ublock-origin.xpi \
    && test "$(python -c 'import json, os, pathlib; p=pathlib.Path(os.environ["XDG_CACHE_HOME"])/"camoufox"/"addons"/"UBO"/"manifest.json"; print(json.loads(p.read_text())["version"])')" = "${UBO_VERSION}" \
    && test "$(python -c 'import importlib.metadata as m; print(m.version("playwright"))')" = "${PLAYWRIGHT_VERSION}" \
    && test "$(python -c 'import importlib.metadata as m; print(m.version("rebrowser-playwright"))')" = "${REBROWSER_PLAYWRIGHT_VERSION}" \
    && test "$(python -c 'import importlib.metadata as m; print(m.version("camoufox"))')" = "${CAMOUFOX_PACKAGE_VERSION}" \
    && test "$(python -c 'import json, pathlib, playwright; p=pathlib.Path(playwright.__file__).parent/"driver"/"package"/"browsers.json"; print(next(x["revision"] for x in json.loads(p.read_text())["browsers"] if x["name"] == "chromium"))')" = "${PLAYWRIGHT_CHROMIUM_REVISION}" \
    && test "$(python -c 'import json, pathlib, rebrowser_playwright; p=pathlib.Path(rebrowser_playwright.__file__).parent/"driver"/"package"/"browsers.json"; print(next(x["revision"] for x in json.loads(p.read_text())["browsers"] if x["name"] == "chromium"))')" = "${REBROWSER_CHROMIUM_REVISION}" \
    && python -m camoufox path \
    && chown -R "${SCRAPEYARD_UID}:${SCRAPEYARD_GID}" /data /home/scrapeyard \
    && chmod 0750 /data /data/db /data/results /data/adaptive /data/logs \
    && chmod -R a-w "${PLAYWRIGHT_BROWSERS_PATH}" "${XDG_CACHE_HOME}" /opt/node \
    && apt-get purge -y unzip \
    && apt-get autoremove -y \
    && dpkg-query -W -f='${Package}=${Version}\n' | sort \
        > /usr/share/scrapeyard-os-packages.txt \
    && rm -rf /var/lib/apt/lists/*

COPY src/ src/
COPY sql/ sql/
COPY pyproject.toml README.md ./
COPY scripts/container-entrypoint.sh /usr/local/bin/scrapeyard-entrypoint
# Nothing at runtime installs packages, so pip leaves the image after the last
# install instead of shipping as attack surface and scanner noise.
RUN pip install --no-cache-dir --no-deps . && \
    python -c 'import importlib.metadata as m, scrapeyard; assert scrapeyard.__version__ == m.version("scrapeyard")' && \
    python -m pip uninstall --yes pip && \
    ! python -c 'import pip' 2>/dev/null && \
    chmod 0555 /usr/local/bin/scrapeyard-entrypoint && \
    chmod -R a-w /app

USER 10001:10001
EXPOSE 8420

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD ["python", "-m", "scrapeyard.runtime.healthcheck"]

ENTRYPOINT ["/usr/local/bin/scrapeyard-entrypoint"]
CMD ["uvicorn", "scrapeyard.main:app", "--host", "0.0.0.0", "--port", "8420", "--timeout-graceful-shutdown", "5", "--workers", "1"]
