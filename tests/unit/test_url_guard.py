from __future__ import annotations

import os
import random
import re
import socket
import subprocess
import sys
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, quote_plus, urlparse

import httpx
import pytest

from scrapeyard.engine.url_guard import (
    UnsafeURLError,
    URLResolutionError,
    _variants_longest_first as variants_longest_first,
    activate_deployment_secret_redaction,
    assert_public_url,
    canonical_url_origin,
    public_dns_hostname,
    redact_deployment_secrets,
    redact_deployment_secrets_in_value,
    redact_deployment_secrets_in_value_with_count,
    redact_sensitive_config_text,
    redact_sensitive_mapping,
    redact_userinfo_in_text,
    redact_userinfo_in_url,
    reset_deployment_secret_redaction,
    resolve_public_url,
    url_host_label,
)


def test_counted_secret_redaction_handles_encoded_values_and_key_collisions() -> None:
    secret = "Vendor Value/91"
    value = {
        f"prefix-{secret}": "raw Vendor Value/91",
        "prefix-<redacted>": "Vendor%20Value%2F91 and Vendor+Value%2F91",
    }

    redacted, count = redact_deployment_secrets_in_value_with_count(
        value,
        secret_values=(secret,),
    )

    assert redacted == {
        "prefix-<redacted>": "raw <redacted>",
        "prefix-<redacted>#2": "<redacted> and <redacted>",
    }
    assert count == 4


def test_assert_public_url_rejects_non_global_cgnat_address() -> None:
    with pytest.raises(UnsafeURLError, match="non-public"):
        assert_public_url("http://100.64.0.1/resource", resolve_dns=False)


def test_assert_public_url_allows_global_literal_address_without_dns() -> None:
    assert_public_url("http://8.8.8.8/resource", resolve_dns=False)


@pytest.mark.parametrize(
    "url",
    [
        "http://[64:ff9b::a9fe:a9fe]/resource",
        "http://[64:ff9b:1::c0a8:1]/resource",
    ],
)
def test_assert_public_url_rejects_nat64_private_ipv4_literals(url: str) -> None:
    with pytest.raises(UnsafeURLError, match="non-public"):
        assert_public_url(url, resolve_dns=False)


def test_assert_public_url_allows_nat64_global_ipv4_literal_without_dns() -> None:
    assert_public_url("http://[64:ff9b::808:808]/resource", resolve_dns=False)


def test_assert_public_url_rejects_invalid_port() -> None:
    with pytest.raises(UnsafeURLError, match="port"):
        assert_public_url("https://example.com:99999/resource", resolve_dns=False)


def test_assert_public_url_rejects_malformed_ipv6_url() -> None:
    with pytest.raises(UnsafeURLError, match="malformed"):
        assert_public_url("http://[::1", resolve_dns=False)


def test_assert_public_url_rejects_empty_dns_root_hostname() -> None:
    with pytest.raises(UnsafeURLError, match="hostname"):
        assert_public_url("http://.", resolve_dns=False)


def test_assert_public_url_rejects_raw_backslash() -> None:
    with pytest.raises(UnsafeURLError, match="backslashes"):
        assert_public_url("http://127.0.0.1\\@example.com/resource", resolve_dns=False)


def test_assert_public_url_rejects_raw_whitespace() -> None:
    with pytest.raises(UnsafeURLError, match="whitespace"):
        assert_public_url("http://127.0.0.1\t@example.com/resource", resolve_dns=False)


def test_assert_public_url_rejects_raw_control_characters() -> None:
    with pytest.raises(UnsafeURLError, match="control characters"):
        assert_public_url("https://example.com/path\x00secret", resolve_dns=False)


@pytest.mark.parametrize(
    "host",
    ["localhost", "localhost.", "localhost.localdomain", "ip6-localhost", "broadcasthost"],
)
def test_assert_public_url_rejects_known_local_hostnames_without_dns(host: str) -> None:
    with pytest.raises(UnsafeURLError, match="blocked"):
        assert_public_url(f"http://{host}/resource", resolve_dns=False)


@pytest.mark.parametrize(
    "host",
    [
        "localhost。",
        "localhost．",
        "localhost｡",
        "ⓛⓞⓒⓐⓛⓗⓞⓢⓣ",
        "ℓocalhost",
        "ｌｏｃａｌｈｏｓｔ",
    ],
)
def test_assert_public_url_rejects_idna_localhost_variants_without_dns(host: str) -> None:
    with pytest.raises(UnsafeURLError, match="blocked"):
        assert_public_url(f"http://{host}/resource", resolve_dns=False)


@pytest.mark.parametrize("host", ["127。0。0。1", "127．0．0．1", "127｡0｡0｡1"])
def test_assert_public_url_rejects_idna_ipv4_literals_without_dns(host: str) -> None:
    with pytest.raises(UnsafeURLError, match="non-public"):
        assert_public_url(f"http://{host}/resource", resolve_dns=False)


def test_assert_public_url_allows_public_idna_hostname_without_dns() -> None:
    assert_public_url("https://bücher.example/resource", resolve_dns=False)


def _answer_public(calls: list[str]):
    def getaddrinfo(host, *_args, **_kwargs):
        calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    return getaddrinfo


@pytest.mark.parametrize(
    "host",
    [
        "straße.test",  # IDNA 2003 strasse.test, IDNA 2008 xn--strae-oqa.test
        "ς.test",  # xn--4xa.test against xn--3xa.test
        "STRASSE.ẞ.test",
        # httpx refuses these, but a browser maps them to a name other than the
        # IDNA 2003 one.
        "ｓｔｒａße.test",
        "my_site.straße.test",
        "☃.straße.test",
        "straße" + "\u00ad" * 1100 + ".test",
        "a\u1806b.test",
        "Ꭰ.test",  # Cherokee: mapped by browsers, kept by Python's Unicode 3.2 tables
        # One name to a browser, but not valid IDNA 2008, so httpx sends nothing.
        "☃.test",
        "ｅｘａｍｐｌｅ.com",
    ],
)
def test_hostname_idna_versions_read_differently_is_refused(monkeypatch, host: str) -> None:
    lookups: list[str] = []
    monkeypatch.setattr(
        "scrapeyard.engine.url_guard.socket.getaddrinfo", _answer_public(lookups)
    )
    url = f"https://{host}/resource"

    for check in (public_dns_hostname, assert_public_url, resolve_public_url, url_host_label):
        with pytest.raises(UnsafeURLError, match="read differently by IDNA 2003 and IDNA 2008"):
            check(url)
    assert canonical_url_origin(url) is None
    assert lookups == []


@pytest.mark.parametrize(
    "host",
    [
        "xn--strae-oqa.test",  # The ASCII spelling of straße.test names one site.
        "xn--3xa.test",
        "xn--n3h.test",  # ☃.test
        "bücher.example",
        "BÜCHER.example",
        "bücher。example",
        "münchen.example",
        "日本語.example",
        "İstanbul.example",
    ],
)
def test_guard_resolves_and_pins_the_hostname_httpx_requests(monkeypatch, host: str) -> None:
    lookups: list[str] = []
    monkeypatch.setattr(
        "scrapeyard.engine.url_guard.socket.getaddrinfo", _answer_public(lookups)
    )
    url = f"https://{host}/resource"
    requested = httpx.URL(url).raw_host.decode("ascii")

    assert public_dns_hostname(url) == requested
    assert_public_url(url)
    resolved = resolve_public_url(url)

    assert lookups == [requested, requested]
    assert resolved.host_header == requested
    assert resolved.sni_hostname == requested
    assert url_host_label(url) == requested


def test_every_accepted_non_ascii_hostname_is_the_one_httpx_requests() -> None:
    # One host per 61 code points in four positions (about 13,000 names), plus
    # code points the two IDNA versions are known to treat differently. The
    # full sweep (1.36 million names in seven positions, 40 s) found no
    # disagreement with httpx, nor with Node 24's WHATWG parser, on 2026-10-10.
    known_to_differ = {
        0xDF, 0x3C2, 0x10FC, 0x13A0, 0x1806, 0x1C80, 0x1E9E, 0x212A, 0x1F130, 0xE0100
    }
    checked = 0
    for code_point in sorted(known_to_differ | set(range(0x80, 0x30000, 61))):
        if 0xD800 <= code_point <= 0xDFFF:
            continue
        for template in ("a{}b.test", "{}.test", "{}a.test", "my_site.{}.test"):
            url = f"https://{template.format(chr(code_point))}/resource"
            try:
                guarded = public_dns_hostname(url)
            except UnsafeURLError:
                continue
            try:
                requested = httpx.URL(url).raw_host.decode("ascii")
            except httpx.InvalidURL:
                # Only when lowercasing made the host ASCII before the guard
                # saw it (the Kelvin sign): httpx then sends nothing.
                assert (urlparse(url).hostname or "").isascii(), hex(code_point)
                continue
            assert guarded == requested.rstrip("."), hex(code_point)
            checked += 1
    assert checked > 5000


def test_every_accepted_ascii_hostname_is_the_one_httpx_requests() -> None:
    refused = set()
    for code_point in range(0x21, 0x7F):
        url = f"http://a{chr(code_point)}b.example.test/resource"
        try:
            guarded = public_dns_hostname(url)
        except UnsafeURLError:
            refused.add(chr(code_point))
            continue
        try:
            requested = httpx.URL(url).raw_host.decode("ascii")
        except httpx.InvalidURL:
            continue
        assert guarded == requested, chr(code_point)
    # httpx percent-encodes these three, so it would request another name.
    assert {"<", ">", "^"} <= refused


@pytest.mark.parametrize("url", ["http://a[b.test/", "https://\uff03.test/"])
def test_url_host_label_reports_a_malformed_url_as_unsafe(url: str) -> None:
    with pytest.raises(UnsafeURLError, match="malformed"):
        url_host_label(url)


def test_assert_public_url_can_require_dns_resolution(monkeypatch) -> None:
    def _raise_gaierror(*_args, **_kwargs):
        raise socket.gaierror

    monkeypatch.setattr("scrapeyard.engine.url_guard.socket.getaddrinfo", _raise_gaierror)

    with pytest.raises(URLResolutionError, match="could not be resolved"):
        assert_public_url("https://unresolved.example/resource", allow_unresolved=False)


def test_resolve_public_url_pins_validated_ip_and_preserves_host_and_sni(monkeypatch) -> None:
    monkeypatch.setattr(
        "scrapeyard.engine.url_guard.socket.getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )

    resolved = resolve_public_url("https://user:pass@example.com:8443/path?q=1")

    assert resolved.connect_url == "https://user:pass@93.184.216.34:8443/path?q=1"
    assert resolved.host_header == "example.com:8443"
    assert resolved.sni_hostname == "example.com"


def test_resolve_public_url_rejects_mixed_public_and_private_dns(monkeypatch) -> None:
    monkeypatch.setattr(
        "scrapeyard.engine.url_guard.socket.getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ],
    )

    with pytest.raises(UnsafeURLError, match="non-public"):
        resolve_public_url("https://example.com/path")


def test_resolve_public_url_classifies_temporary_dns_failure_as_transport(
    monkeypatch,
) -> None:
    def _raise_temporary_failure(*_args, **_kwargs):
        raise socket.gaierror(socket.EAI_AGAIN, "temporary failure")

    monkeypatch.setattr(
        "scrapeyard.engine.url_guard.socket.getaddrinfo",
        _raise_temporary_failure,
    )

    with pytest.raises(URLResolutionError, match="could not be resolved"):
        resolve_public_url("https://example.com/path")


def test_assert_public_url_rejects_percent_encoded_hostname() -> None:
    with pytest.raises(UnsafeURLError, match="percent escapes"):
        assert_public_url("http://%31%32%37.0.0.1/resource", resolve_dns=False)


def test_assert_public_url_rejects_legacy_ipv4_loopback_without_dns() -> None:
    with pytest.raises(UnsafeURLError, match="non-public"):
        assert_public_url("http://2130706433/resource", resolve_dns=False)


def test_assert_public_url_rejects_trailing_dot_ipv4_loopback_without_dns() -> None:
    with pytest.raises(UnsafeURLError, match="non-public"):
        assert_public_url("http://127.0.0.1./resource", resolve_dns=False)


def test_assert_public_url_rejects_trailing_dot_legacy_ipv4_without_dns() -> None:
    with pytest.raises(UnsafeURLError, match="non-public"):
        assert_public_url("http://2130706433./resource", resolve_dns=False)


def test_assert_public_url_rejects_legacy_ipv4_private_octal_without_dns() -> None:
    with pytest.raises(UnsafeURLError, match="non-public"):
        assert_public_url("http://0300.0250.0001.0001/resource", resolve_dns=False)


@pytest.mark.parametrize("host", ["a{}b.test", "{}", "1.2.3.{}"])
def test_lone_surrogate_hostname_is_an_unsafe_url(host: str) -> None:
    # Built in code: an escaped surrogate in source can arrive as another character.
    url = f"https://{host.format(chr(0xD800))}/resource"

    with pytest.raises(UnsafeURLError, match="hostname is invalid"):
        public_dns_hostname(url)
    with pytest.raises(UnsafeURLError, match="hostname is invalid"):
        assert_public_url(url, resolve_dns=False)
    with pytest.raises(UnsafeURLError, match="hostname is invalid"):
        resolve_public_url(url)


@pytest.mark.parametrize("host", ["[::7f00:1]", "[::ffff:0:7f00:1]", "[::a9fe:a9fe]", "[fec0::1]"])
def test_assert_public_url_rejects_ipv6_spellings_of_local_addresses(host: str) -> None:
    with pytest.raises(UnsafeURLError, match="non-public"):
        assert_public_url(f"http://{host}/resource", resolve_dns=False)


def test_redact_userinfo_in_text_handles_passwordless_userinfo() -> None:
    text = "proxy: http://token@gate.example.com:7777"

    assert redact_userinfo_in_text(text) == "proxy: http://gate.example.com:7777"


def test_redact_userinfo_in_text_handles_non_http_schemes() -> None:
    text = (
        "proxy: socks5://user:pass@gate.example.com:1080 cdp: ws://token@browser.example/devtools"
    )

    assert redact_userinfo_in_text(text) == (
        "proxy: socks5://gate.example.com:1080 cdp: ws://browser.example/devtools"
    )


def test_redact_userinfo_in_url_preserves_ipv6_brackets() -> None:
    assert redact_userinfo_in_url("https://user:pass@[2001:4860:4860::8888]:443/a") == (
        "https://[2001:4860:4860::8888]:443/a"
    )


def test_redact_userinfo_in_url_handles_invalid_port_without_raising() -> None:
    assert redact_userinfo_in_url("https://user:pass@example.com:bad/a") == (
        "https://example.com/a"
    )


def test_redact_userinfo_in_text_handles_malformed_ipv6_without_raising() -> None:
    text = "failed at https://user:pass@[::1/private?api_key=secret"

    assert redact_userinfo_in_text(text) == ("failed at https://[::1/private?api_key=<redacted>")


def test_redact_userinfo_in_url_masks_sensitive_query_values() -> None:
    assert (
        redact_userinfo_in_url(
            "https://user:pass@example.com/path?api_key=secret&page=2&session_id=abc"
        )
        == "https://example.com/path?api_key=<redacted>&page=<redacted>&session_id=<redacted>"
    )


def test_redact_userinfo_in_url_masks_semicolon_query_values() -> None:
    assert (
        redact_userinfo_in_url("https://example.com/path?page=2;api_key=secret;session_id=abc")
        == "https://example.com/path?page=<redacted>;api_key=<redacted>;session_id=<redacted>"
    )


def test_redact_userinfo_in_url_masks_sensitive_path_params() -> None:
    assert (
        redact_userinfo_in_url(
            "https://example.com/products;jsessionid=abc/page;api_key=secret?ok=1"
        )
        == "https://example.com/products;jsessionid=<redacted>/page;api_key=<redacted>?ok=<redacted>"
    )


def test_redact_userinfo_in_url_masks_sensitive_fragment_values() -> None:
    assert (
        redact_userinfo_in_url("https://example.com/callback#access_token=secret&state=ok")
        == "https://example.com/callback#<redacted>"
    )


def test_redact_userinfo_in_url_masks_fragment_query_values() -> None:
    assert (
        redact_userinfo_in_url("https://example.com/app#/callback?session_id=abc&page=2")
        == "https://example.com/app#<redacted>"
    )


def test_redact_userinfo_in_url_masks_signed_url_query_values() -> None:
    assert redact_userinfo_in_url(
        "https://example.com/path?AWSAccessKeyId=akia&sig=abc&key=map-key&page=2"
    ) == (
        "https://example.com/path?"
        "AWSAccessKeyId=<redacted>&sig=<redacted>&key=<redacted>&page=<redacted>"
    )


def test_redact_userinfo_in_text_masks_sensitive_query_values() -> None:
    text = "failed at https://example.com/path?access_token=secret&page=2"

    assert redact_userinfo_in_text(text) == (
        "failed at https://example.com/path?access_token=<redacted>&page=<redacted>"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("9+https://user:pass@h.example/", "9+https://h.example/"),
        ("x_https://user:pass@h.example/", "x_https://h.example/"),
        ("proxyhttps://user:pass@h.example/", "proxyhttps://h.example/"),
        ("a.b-c+d://user:pass@h.example/", "a.b-c+d://h.example/"),
        ("'ftp://user:pass@h.example' ws://t@h2.example", "'ftp://h.example' ws://h2.example"),
        ("https:// user:pass@h.example/", "https:// user:pass@h.example/"),
    ],
)
def test_redact_userinfo_in_text_finds_urls_inside_scheme_character_runs(
    text: str, expected: str
) -> None:
    assert redact_userinfo_in_text(text) == expected


def test_redact_userinfo_in_text_matches_the_backtracking_pattern() -> None:
    # The pattern before TD-55, which was quadratic on long scheme-like runs.
    backtracking = re.compile(r"[a-z][a-z0-9+.-]*://[^\s\"']+", re.IGNORECASE)
    alphabet = [*"aZh1+.-:/ \"'@?=&#;_", "://", "u:p@", "?token=", "\n", "é"]
    rng = random.Random(55)
    for _ in range(20_000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 24)))
        expected = backtracking.sub(lambda match: redact_userinfo_in_url(match.group(0)), text)
        assert redact_userinfo_in_text(text) == expected, text


@pytest.mark.parametrize(
    "token",
    ["a" * 262_144, "a." * 131_072, "1a" * 131_072, ("a" * 64 + ":/") * 3_971],
    ids=["letters", "dotted", "digit-letter", "almost-scheme"],
)
def test_redact_userinfo_in_text_is_linear_on_long_scheme_like_tokens(token: str) -> None:
    started = time.perf_counter()
    assert redact_userinfo_in_text(token) == token
    assert time.perf_counter() - started < 0.1


def test_url_host_label_strips_userinfo_and_preserves_port() -> None:
    assert url_host_label("https://user:pass@Example.COM:8443/products") == "example.com:8443"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://EXAMPLE.com./path", "example.com"),
        ("http://example.com:80/path", "example.com"),
        ("https://example.com:443/path", "example.com"),
        ("https://example.com:8443/path", "example.com:8443"),
        ("https://bücher.example/path", "xn--bcher-kva.example"),
        ("https://xn--bcher-kva.example/path", "xn--bcher-kva.example"),
        ("https://[2001:0db8::1]:443/path", "[2001:db8::1]"),
        ("https://[2001:db8::1]:8443/path", "[2001:db8::1]:8443"),
    ],
)
def test_url_host_label_canonicalizes_logical_origin(url: str, expected: str) -> None:
    assert url_host_label(url) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://BÜCHER.example/path", ("https", "xn--bcher-kva.example", 443)),
        ("https://xn--bcher-kva.example:443/path", ("https", "xn--bcher-kva.example", 443)),
        ("https://[2001:0db8::1]/path", ("https", "2001:db8::1", 443)),
        ("http://example.com:0/path", ("http", "example.com", 0)),
        ("https://example.com:invalid/path", None),
    ],
)
def test_canonical_url_origin_normalizes_browser_origin_identity(url: str, expected) -> None:
    assert canonical_url_origin(url) == expected


def test_redact_sensitive_mapping_masks_secret_keys_and_url_userinfo() -> None:
    value = {
        "headers": {"Authorization": "Bearer secret", "X-Test": "visible"},
        "nested": {"api_token": "secret"},
        "proxy": "socks5://user:pass@gate.example.com:7777",
    }

    redacted = redact_sensitive_mapping(value)

    assert redacted["headers"] == {
        "Authorization": "<redacted>",
        "X-Test": "<redacted>",
    }
    assert redacted["nested"] == {"api_token": "<redacted>"}
    assert redacted["proxy"] == "socks5://gate.example.com:7777"


def test_run_scoped_redaction_masks_raw_encoded_and_nested_tuple_secrets() -> None:
    secret = "Vendor Value/91"
    token = activate_deployment_secret_redaction((secret,))
    try:
        assert redact_deployment_secrets(f"raw={secret}") == "raw=<redacted>"
        assert redact_deployment_secrets("path=Vendor%20Value%2F91") == ("path=<redacted>")
        assert redact_deployment_secrets("query=Vendor+Value%2F91") == ("query=<redacted>")
        assert redact_sensitive_mapping({secret: (secret,), f"x-{secret}": secret}) == {
            "<redacted>": ("<redacted>",),
            "x-<redacted>": "<redacted>",
        }
        assert redact_deployment_secrets_in_value(
            {
                "api_token": "public-value",
                "url": "https://example.com?q=public#public",
                "secret": (secret, "Vendor%20Value%2F91"),
            }
        ) == {
            "api_token": "public-value",
            "url": "https://example.com?q=public#public",
            "secret": ("<redacted>", "<redacted>"),
        }
    finally:
        reset_deployment_secret_redaction(token)


def _reference_redact_text(text: str, secret_values: tuple[str, ...]) -> tuple[str, int]:
    """Redaction as written before its variants were cached, rebuilt per string."""
    variants: set[str] = set()
    for value in (value for value in secret_values if value):
        variants.update((value, quote(value, safe=""), quote_plus(value, safe="")))
    count = 0
    for variant in sorted(variants, key=len, reverse=True):
        if variant:
            count += text.count(variant)
            text = text.replace(variant, "<redacted>")
    return text, count


def _reference_redact_value(value: Any, secret_values: tuple[str, ...]) -> tuple[Any, int]:
    if isinstance(value, Mapping):
        redacted: dict[Any, Any] = {}
        count = 0
        for key, item in value.items():
            redacted_key: Any = key
            if isinstance(key, str):
                redacted_key, key_count = _reference_redact_text(key, secret_values)
                count += key_count
                base_key, suffix = redacted_key, 2
                while redacted_key in redacted:
                    redacted_key = f"{base_key}#{suffix}"
                    suffix += 1
            redacted[redacted_key], item_count = _reference_redact_value(item, secret_values)
            count += item_count
        return redacted, count
    if isinstance(value, (list, tuple)):
        pairs = [_reference_redact_value(item, secret_values) for item in value]
        items = [item for item, _count in pairs]
        return (tuple(items) if isinstance(value, tuple) else items), sum(c for _i, c in pairs)
    if isinstance(value, str):
        return _reference_redact_text(value, secret_values)
    return value, 0


_TRICKY_SECRET_SETS = [
    (),
    ("tok",),
    ("tok", "tok123"),
    ("a b/c+d", "a b"),
    ("p@ss:w0rd&x=1", "ss:w"),
    ("Ü-ß/€", "€"),
]
_TRICKY_PIECES = [
    "tok", "tok123", "a b/c+d", "a%20b%2Fc%2Bd", "a+b%2Fc%2Bd", "a b", "p@ss:w0rd&x=1",
    "p%40ss%3Aw0rd%26x%3D1", "Ü-ß/€", "%E2%82%AC", "x", " ", "/",
]


def _tricky_value(rng: random.Random, depth: int = 0) -> Any:
    roll = rng.random()
    if depth < 3 and roll < 0.2:
        return {_tricky_text(rng) or "k": _tricky_value(rng, depth + 1) for _ in range(rng.randrange(4))}
    if depth < 3 and roll < 0.35:
        return [_tricky_value(rng, depth + 1) for _ in range(rng.randrange(4))]
    if depth < 3 and roll < 0.4:
        return tuple(_tricky_value(rng, depth + 1) for _ in range(rng.randrange(3)))
    if roll < 0.5:
        return rng.choice([None, 1, 2.5, True])
    return _tricky_text(rng)


def _tricky_text(rng: random.Random) -> str:
    return "".join(rng.choice(_TRICKY_PIECES) for _ in range(rng.randrange(6)))


@pytest.mark.parametrize("secret_values", _TRICKY_SECRET_SETS)
def test_cached_secret_redaction_matches_per_string_redaction(secret_values) -> None:
    rng = random.Random(len(secret_values))

    for _ in range(300):
        value = {"record": _tricky_value(rng), "text": _tricky_text(rng)}
        assert redact_deployment_secrets_in_value_with_count(
            value, secret_values=secret_values
        ) == _reference_redact_value(value, secret_values)
        text = _tricky_text(rng)
        assert redact_deployment_secrets(text, secret_values) == _reference_redact_text(
            text, secret_values
        )[0]


def test_secret_variants_are_built_once_per_secret_set() -> None:
    variants_longest_first.cache_clear()
    record = {"title": "a Vendor Value/91 b", "tags": ["Vendor%20Value%2F91", "plain"]}

    for _ in range(100):
        redact_deployment_secrets_in_value_with_count(record, secret_values=["Vendor Value/91"])

    info = variants_longest_first.cache_info()
    assert (info.misses, info.hits) == (1, 99)


def test_overlapping_equal_length_secrets_redact_the_same_under_every_hash_seed() -> None:
    script = (
        "from scrapeyard.engine.url_guard import redact_deployment_secrets as redact; "
        "print(redact('abab baba a b+a', ['ab', 'ba', 'a b', 'b+a']))"
    )
    outputs = {
        subprocess.run(
            [sys.executable, "-c", script],
            env={**os.environ, "PYTHONHASHSEED": str(seed)},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for seed in range(12)
    }

    assert outputs == {"<redacted><redacted> b<redacted>a <redacted>+a\n"}


def test_redaction_without_secrets_returns_the_value_uncopied() -> None:
    record = {"title": "visible", "tags": ["a", ("b",)], "nested": {"k": "v"}}

    redacted, count = redact_deployment_secrets_in_value_with_count(record, secret_values=())

    assert redacted is record
    assert count == 0


def test_redact_sensitive_config_text_masks_yaml_secrets() -> None:
    config_yaml = """
project: demo
name: secret-job
proxy:
  url: socks5://user:pass@gate.example.com:7777
webhook:
  url: https://example.com/hook
  headers:
    Authorization: Bearer webhook-secret
target:
  url: https://example.com?api_key=target-secret&page=2
  browser:
    extra_headers:
      X-API-Key: browser-secret
  selectors:
    title: h1
"""

    redacted = redact_sensitive_config_text(config_yaml)

    assert "webhook-secret" not in redacted
    assert "browser-secret" not in redacted
    assert "target-secret" not in redacted
    assert "user:pass" not in redacted
    assert redacted.count("<redacted>") == 4


def test_redact_sensitive_config_text_rejects_yaml_alias_expansion() -> None:
    config_yaml = "proxy: http://user:pass@example.com\ncopy: &copy [1]\nref: *copy\n"

    redacted = redact_sensitive_config_text(config_yaml)

    assert "user:pass" not in redacted
    assert "*copy" in redacted


def test_redact_sensitive_config_text_masks_secret_keys_when_yaml_is_invalid() -> None:
    config_yaml = """
webhook:
  headers:
    Authorization: Bearer webhook-secret
    X-Test: visible
target:
  url: https://example.com?api_key=target-secret&page=2
copy: &copy [1]
ref: *copy
"""

    redacted = redact_sensitive_config_text(config_yaml)

    assert "webhook-secret" not in redacted
    assert "target-secret" not in redacted
    assert "Authorization: <redacted>" in redacted
    assert "X-Test: visible" in redacted
    assert "*copy" in redacted


def test_redact_sensitive_config_text_masks_invalid_yaml_secret_block_values() -> None:
    config_yaml = """
headers:
  Authorization: |
    Bearer line one
    Bearer line two
copy: &copy [1]
ref: *copy
"""

    redacted = redact_sensitive_config_text(config_yaml)

    assert "Bearer line" not in redacted
    assert redacted.count("<redacted>") >= 2


def test_redact_sensitive_config_text_masks_invalid_yaml_list_item_secret_keys() -> None:
    config_yaml = """
headers:
  - Authorization: Bearer webhook-secret
  - X-Test: visible
copy: &copy [1]
ref: *copy
"""

    redacted = redact_sensitive_config_text(config_yaml)

    assert "webhook-secret" not in redacted
    assert "- Authorization: <redacted>" in redacted
    assert "- X-Test: visible" in redacted


def test_redact_sensitive_config_text_masks_invalid_yaml_explicit_secret_keys() -> None:
    config_yaml = """
headers:
  ? Authorization
  : Bearer webhook-secret
copy: &copy [1]
ref: *copy
"""

    redacted = redact_sensitive_config_text(config_yaml)

    assert "webhook-secret" not in redacted
    assert ": <redacted>" in redacted


def test_redact_sensitive_config_text_masks_invalid_yaml_list_explicit_secret_keys() -> None:
    config_yaml = """
headers:
  - ? Authorization
    : Bearer webhook-secret
copy: &copy [1]
ref: *copy
"""

    redacted = redact_sensitive_config_text(config_yaml)

    assert "webhook-secret" not in redacted
    assert ": <redacted>" in redacted


def test_redact_sensitive_config_text_handles_unhashable_yaml_keys() -> None:
    config_yaml = "? [a]\n: http://user:pass@example.com\n"

    redacted = redact_sensitive_config_text(config_yaml)

    assert "user:pass" not in redacted
    assert "http://example.com" in redacted
