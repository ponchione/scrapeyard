"""Root test configuration — sets safe temp directories for all tests."""

import ipaddress
import socket
import warnings

import pytest

_real_getaddrinfo = socket.getaddrinfo
# Reserved documentation names (RFC 2606) that resolve publicly in real DNS.
_PUBLIC_TEST_HOSTS = frozenset(
    f"{prefix}example.{tld}" for prefix in ("", "www.") for tld in ("com", "net", "org")
)
_PUBLIC_TEST_ADDRESS = "93.184.216.34"


def _offline_getaddrinfo(host, *args, **kwargs):
    """Answer like real DNS for the names tests use, without touching the network.

    The ``example`` domains resolve to a public address; every other name
    (``.test``, ``.example``, other subdomains) is NXDOMAIN, as it is upstream.
    IP literals and localhost still go to the real resolver, which answers
    them locally. Tests that patch ``getaddrinfo`` themselves take precedence.
    """
    name = host.decode() if isinstance(host, bytes) else host
    if name is None or name == "localhost" or name.endswith(".localhost"):
        return _real_getaddrinfo(host, *args, **kwargs)
    try:
        ipaddress.ip_address(name)
    except ValueError:
        pass
    else:
        return _real_getaddrinfo(host, *args, **kwargs)
    if name.rstrip(".").lower() in _PUBLIC_TEST_HOSTS:
        return _real_getaddrinfo(_PUBLIC_TEST_ADDRESS, *args, **kwargs)
    raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")


@pytest.fixture(autouse=True)
def _offline_dns(monkeypatch):
    """Keep hostname resolution deterministic and off the network."""
    monkeypatch.setattr(socket, "getaddrinfo", _offline_getaddrinfo)


@pytest.fixture(autouse=True)
async def _scrapeyard_temp_dirs(tmp_path, monkeypatch):
    """Point all data directories to temp paths for every test."""
    with warnings.catch_warnings():
        # Scrapling 0.2.x passes this no-op option to lxml 6.1.x. The dependency
        # bounds in pyproject.toml make this exact suppression temporary and
        # version-bounded; all other warnings remain errors in CI.
        warnings.filterwarnings(
            "ignore",
            message=(
                r"The 'strip_cdata' option of HTMLParser\(\) has never done "
                r"anything and will eventually be removed\."
            ),
            category=DeprecationWarning,
        )
        # uvicorn 0.34 calls asyncio.iscoroutinefunction, which warns from
        # Python 3.14 and goes in 3.16 (TD-117). Ruff bans it in this code.
        warnings.filterwarnings(
            "ignore",
            message=r"'asyncio\.iscoroutinefunction' is deprecated",
            category=DeprecationWarning,
        )

        monkeypatch.setenv("SCRAPEYARD_DB_DIR", str(tmp_path / "db"))
        monkeypatch.setenv("SCRAPEYARD_LOG_DIR", str(tmp_path / "logs"))
        monkeypatch.setenv("SCRAPEYARD_STORAGE_RESULTS_DIR", str(tmp_path / "results"))
        monkeypatch.setenv("SCRAPEYARD_ADAPTIVE_DIR", str(tmp_path / "adaptive"))
        monkeypatch.setenv(
            "SCRAPEYARD_ENCRYPTION_KEYS",
            '{"test-v1":"MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="}',
        )
        monkeypatch.setenv("SCRAPEYARD_ENCRYPTION_ACTIVE_KEY_ID", "test-v1")
        # Admission otherwise samples the test runner's real cgroup, so a busy
        # developer host refuses every scrape. Memory tests pass explicit limits.
        monkeypatch.setenv("SCRAPEYARD_WORKERS_MEMORY_LIMIT_MB", "0")

        from scrapeyard.api.dependencies import reset_cached_dependencies
        from scrapeyard.common.settings import get_settings
        from scrapeyard.storage.database import close_db

        get_settings.cache_clear()
        reset_cached_dependencies()

        yield

        await close_db()
        get_settings.cache_clear()
        reset_cached_dependencies()
