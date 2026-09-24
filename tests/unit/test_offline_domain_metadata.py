"""Cold-start metadata must not download outside the source request budget."""

import os
import subprocess
import sys


def test_scrapling_domain_metadata_uses_only_the_bundled_suffix_list(tmp_path):
    result = subprocess.run(
        [sys.executable, "-c", """
import requests
def deny(*args, **kwargs):
    raise AssertionError('Domain metadata attempted a network request')
requests.Session.send = deny

from scrapeyard.engine.basic_fetch import _request_headers
from scrapeyard.engine.browser_session import generate_convincing_referer
from scrapling.core.storage_adaptors import SQLiteStorageSystem

url = 'https://shop.example.co.uk/products'
assert _request_headers(url, {}, stealthy=True)['referer'] == 'https://www.google.com/search?q=example'
assert generate_convincing_referer(url) == 'https://www.google.com/search?q=example'
storage = SQLiteStorageSystem(':memory:', url=url)
assert storage._get_base_url() == 'example.co.uk'
"""],
        env={**os.environ, "TLDEXTRACT_CACHE": str(tmp_path / "empty-cache")},
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
