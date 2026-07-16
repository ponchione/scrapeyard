# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

ARG PYTHON_IMAGE="python:3.12.13-slim-trixie@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de"
ARG RUNTIME_IMAGE="ubuntu:24.04@sha256:4fbb8e6a8395de5a7550b33509421a2bafbc0aab6c06ba2cef9ebffbc7092d90"

# Export the production lock with the same pinned toolchain used by CI.
FROM ${PYTHON_IMAGE} AS builder

ARG PIP_VERSION=26.1.2
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
FROM ${RUNTIME_IMAGE} AS runtime

ARG PIP_VERSION=26.1.2
ARG SCRAPLING_VERSION=0.4.11
ARG PLAYWRIGHT_VERSION=1.61.0
ARG PATCHRIGHT_VERSION=1.61.2
ARG CHROMIUM_VERSION=149.0.7827.55
ARG CHROMIUM_REVISION=1228
ARG CAMOUFOX_PACKAGE_VERSION=0.6.0
ARG CAMOUFOX_BROWSER_VERSION=150.0.2
ARG CAMOUFOX_BROWSER_RELEASE=beta.25
ARG CAMOUFOX_BROWSER_ASSET=camoufox-150.0.2-alpha.26-lin.x86_64.zip
ARG CAMOUFOX_BROWSER_SHA256=b146b98b0c2c41023716feef36451f319a534309f72c54584a4b0b88670f510b
ARG UBO_VERSION=1.72.2
ARG UBO_FILE_ID=4888680
ARG UBO_SHA256=40c315b0da7871868155ecfae7a50a58dfa0920aebd865e008214986f1b7c578
ARG APP_VERSION=0.7.0
ARG SOURCE_REVISION=development
ARG BUILD_CREATED=1970-01-01T00:00:00Z
ARG SOURCE_URL=https://github.com/ponchione/scrapeyard
ARG DOCUMENTATION_URL=https://github.com/ponchione/scrapeyard/blob/main/README.md
ARG SCRAPEYARD_UID=10001
ARG SCRAPEYARD_GID=10001
ARG TARGETARCH
ARG UBUNTU_SNAPSHOT=20260715T000000Z

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    HOME=/home/scrapeyard \
    TMPDIR=/tmp \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    XDG_CACHE_HOME=/opt/scrapeyard-cache

LABEL org.opencontainers.image.base.name="ubuntu:24.04" \
      org.opencontainers.image.base.digest="sha256:4fbb8e6a8395de5a7550b33509421a2bafbc0aab6c06ba2cef9ebffbc7092d90" \
      org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      org.opencontainers.image.created="${BUILD_CREATED}" \
      org.opencontainers.image.documentation="${DOCUMENTATION_URL}" \
      org.scrapeyard.os.snapshot="${UBUNTU_SNAPSHOT}" \
      org.scrapeyard.runtime.uid="10001" \
      org.scrapeyard.browser.scrapling.version="${SCRAPLING_VERSION}" \
      org.scrapeyard.browser.playwright.version="${PLAYWRIGHT_VERSION}" \
      org.scrapeyard.browser.patchright.version="${PATCHRIGHT_VERSION}" \
      org.scrapeyard.browser.chromium.version="${CHROMIUM_VERSION}" \
      org.scrapeyard.browser.chromium.revision="${CHROMIUM_REVISION}" \
      org.scrapeyard.browser.camoufox-package.version="${CAMOUFOX_PACKAGE_VERSION}" \
      org.scrapeyard.browser.camoufox.version="${CAMOUFOX_BROWSER_VERSION}" \
      org.scrapeyard.browser.camoufox.release="${CAMOUFOX_BROWSER_RELEASE}" \
      org.scrapeyard.browser.camoufox.asset="${CAMOUFOX_BROWSER_ASSET}" \
      org.scrapeyard.browser.camoufox.sha256="${CAMOUFOX_BROWSER_SHA256}" \
      org.scrapeyard.browser.ubo.version="${UBO_VERSION}" \
      org.scrapeyard.browser.ubo.sha256="${UBO_SHA256}"

WORKDIR /app
COPY --from=builder /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=builder /build/requirements.txt .
COPY security/browser-policy.json /usr/share/scrapeyard-browser-policy.json
COPY scripts/audit_browser_security.py scripts/inspect_browser_runtime.py /usr/local/lib/scrapeyard/

# Browser binaries are installed at build time. Runtime sandboxing uses
# unprivileged user namespaces instead of a revision-specific setuid helper.
RUN test "${TARGETARCH:-amd64}" = "amd64" && \
    printf '%s\n' \
      'Types: deb' \
      "URIs: https://snapshot.ubuntu.com/ubuntu/${UBUNTU_SNAPSHOT}" \
      'Suites: noble noble-updates noble-backports noble-security' \
      'Components: main universe restricted multiverse' \
      'Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg' \
      > /etc/apt/sources.list.d/ubuntu.sources && \
    apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get upgrade -y --no-install-recommends && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        libdbus-glib-1-2 \
        libgtk-3-0t64 \
        libxml2-dev \
        libxslt1-dev \
        python3 \
        python3-venv \
        unzip \
    && python3 -m venv "${VIRTUAL_ENV}" \
    && python -m pip install --no-cache-dir --upgrade "pip==${PIP_VERSION}" \
    && pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid "${SCRAPEYARD_GID}" scrapeyard \
    && useradd --uid "${SCRAPEYARD_UID}" --gid "${SCRAPEYARD_GID}" \
        --create-home --home-dir /home/scrapeyard --shell /usr/sbin/nologin scrapeyard \
    && mkdir -p \
        /data/db /data/results /data/adaptive /data/logs \
        "${PLAYWRIGHT_BROWSERS_PATH}" "${XDG_CACHE_HOME}/camoufox" \
    && python -m playwright install --with-deps chromium \
    && python -m patchright install chromium \
    && curl --fail --location --retry 3 \
        --output /tmp/camoufox.zip \
        "https://github.com/daijro/camoufox/releases/download/v${CAMOUFOX_BROWSER_VERSION}-${CAMOUFOX_BROWSER_RELEASE}/${CAMOUFOX_BROWSER_ASSET}" \
    && echo "${CAMOUFOX_BROWSER_SHA256}  /tmp/camoufox.zip" | sha256sum --check --strict \
    && mkdir -p "${XDG_CACHE_HOME}/camoufox/browsers/official/${CAMOUFOX_BROWSER_VERSION}-${CAMOUFOX_BROWSER_RELEASE}" \
    && unzip -q /tmp/camoufox.zip -d "${XDG_CACHE_HOME}/camoufox/browsers/official/${CAMOUFOX_BROWSER_VERSION}-${CAMOUFOX_BROWSER_RELEASE}" \
    && curl --fail --location --retry 3 \
        --output /tmp/ublock-origin.xpi \
        "https://addons.mozilla.org/firefox/downloads/file/${UBO_FILE_ID}/ublock_origin-${UBO_VERSION}.xpi" \
    && echo "${UBO_SHA256}  /tmp/ublock-origin.xpi" | sha256sum --check --strict \
    && mkdir -p "${XDG_CACHE_HOME}/camoufox/addons/UBO" \
    && unzip -q /tmp/ublock-origin.xpi -d "${XDG_CACHE_HOME}/camoufox/addons/UBO" \
    && printf '{"version":"%s","build":"%s","prerelease":false}\n' \
        "${CAMOUFOX_BROWSER_VERSION}" "${CAMOUFOX_BROWSER_RELEASE}" \
        > "${XDG_CACHE_HOME}/camoufox/browsers/official/${CAMOUFOX_BROWSER_VERSION}-${CAMOUFOX_BROWSER_RELEASE}/version.json" \
    && printf '{"active_version":"browsers/official/%s-%s"}\n' \
        "${CAMOUFOX_BROWSER_VERSION}" "${CAMOUFOX_BROWSER_RELEASE}" \
        > "${XDG_CACHE_HOME}/camoufox/config.json" \
    && touch "${XDG_CACHE_HOME}/camoufox/.0.5_FLAG" \
    && rm /tmp/camoufox.zip /tmp/ublock-origin.xpi \
    && test "$(python -c 'import json, os, pathlib; p=pathlib.Path(os.environ["XDG_CACHE_HOME"])/"camoufox"/"addons"/"UBO"/"manifest.json"; print(json.loads(p.read_text())["version"])')" = "${UBO_VERSION}" \
    && test "$(python -c 'import importlib.metadata as m; print(m.version("scrapling"))')" = "${SCRAPLING_VERSION}" \
    && test "$(python -c 'import importlib.metadata as m; print(m.version("playwright"))')" = "${PLAYWRIGHT_VERSION}" \
    && test "$(python -c 'import importlib.metadata as m; print(m.version("patchright"))')" = "${PATCHRIGHT_VERSION}" \
    && test "$(python -c 'import importlib.metadata as m; print(m.version("cloverlabs-camoufox"))')" = "${CAMOUFOX_PACKAGE_VERSION}" \
    && test "$(python -c 'import json, pathlib, playwright; p=pathlib.Path(playwright.__file__).parent/"driver"/"package"/"browsers.json"; b=next(x for x in json.loads(p.read_text())["browsers"] if x["name"] == "chromium"); print(b["revision"], b["browserVersion"])')" = "${CHROMIUM_REVISION} ${CHROMIUM_VERSION}" \
    && python /usr/local/lib/scrapeyard/inspect_browser_runtime.py --output /usr/share/scrapeyard-browser-runtime.json \
    && python /usr/local/lib/scrapeyard/audit_browser_security.py \
        --policy /usr/share/scrapeyard-browser-policy.json \
        --manifest /usr/share/scrapeyard-browser-runtime.json \
    && chmod u-s /usr/bin/mount /usr/bin/umount \
    && rm -f \
        /usr/lib/x86_64-linux-gnu/gconv/IBM1390.so \
        /usr/lib/x86_64-linux-gnu/gconv/IBM1399.so \
    && ! test -u /usr/bin/mount \
    && ! test -u /usr/bin/umount \
    && ! test -e /usr/lib/x86_64-linux-gnu/gconv/IBM1390.so \
    && ! test -e /usr/lib/x86_64-linux-gnu/gconv/IBM1399.so \
    && ! dpkg-query -W avahi-daemon >/dev/null 2>&1 \
    && ! dpkg-query -W p11-kit >/dev/null 2>&1 \
    && chown -R "${SCRAPEYARD_UID}:${SCRAPEYARD_GID}" /data /home/scrapeyard \
    && chmod 0750 /data /data/db /data/results /data/adaptive /data/logs \
    && chmod -R a-w "${PLAYWRIGHT_BROWSERS_PATH}" "${XDG_CACHE_HOME}" \
    && apt-get purge -y \
        build-essential curl libxml2-dev libxslt1-dev \
        python3-venv python3.12-venv unzip \
    && apt-get autoremove -y \
    && dpkg-query -W -f='${Package}=${Version}\n' | sort \
        > /usr/share/scrapeyard-os-packages.txt \
    && rm -rf /var/lib/apt/lists/*

COPY src/ src/
COPY sql/ sql/
COPY pyproject.toml README.md ./
COPY scripts/container-entrypoint.sh /usr/local/bin/scrapeyard-entrypoint
RUN pip install --no-cache-dir --no-deps . && \
    test "$(python -c 'import importlib.metadata as m; print(m.version("scrapeyard"))')" = "${APP_VERSION}" && \
    python -c 'import importlib.metadata as m, scrapeyard; assert scrapeyard.__version__ == m.version("scrapeyard")' && \
    chmod 0555 /usr/local/bin/scrapeyard-entrypoint && \
    chmod -R a-w /app

USER 10001:10001
EXPOSE 8420

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["/usr/local/bin/scrapeyard-entrypoint", "healthcheck"]

ENTRYPOINT ["/usr/local/bin/scrapeyard-entrypoint"]
CMD ["uvicorn", "scrapeyard.main:app", "--host", "0.0.0.0", "--port", "8420", "--workers", "1"]
