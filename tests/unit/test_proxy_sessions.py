"""Per-run {session} tokens in proxy URLs."""

from __future__ import annotations

import dataclasses
import re
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from scrapeyard.config.schema import ProxyConfig
from scrapeyard.engine.proxy import (
    PROXY_SESSION_PLACEHOLDER,
    apply_proxy_session,
    new_proxy_session_token,
    normalize_public_proxy_url,
    redact_proxy_url,
)
from scrapeyard.engine.url_guard import redact_userinfo_in_text
from scrapeyard.queue.target_execution import resolve_target_runtime_context
from scrapeyard.queue.worker import TargetProcessingContext

_TEMPLATE = "http://customer-acme-session-{session}:secret@gate.example.test:7000"


@pytest.mark.parametrize(
    "url",
    [
        _TEMPLATE,
        "http://customer:secret_session-{session}_lifetime-30m@gate.example.test:7000",
        "http://u-{session}:p-{session}@gate.example.test:7000",
    ],
)
def test_session_placeholder_is_accepted_in_credentials(url: str):
    assert normalize_public_proxy_url(url) == url
    assert ProxyConfig(url=url).url == url


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("http://u:p@gate-{session}.example.test:7000", "only supported in proxy credentials"),
        ("http://u:p@gate.example.test:7000/{session}", "only supported in proxy credentials"),
        ("http://u-{sess}:p@gate.example.test:7000", "only allowed in the {session} placeholder"),
    ],
)
def test_session_placeholder_outside_credentials_is_rejected(url: str, message: str):
    with pytest.raises(ValueError, match=re.escape(message)):
        normalize_public_proxy_url(url)
    with pytest.raises(ValidationError):
        ProxyConfig(url=url)


def test_apply_proxy_session_substitutes_every_placeholder():
    url = "http://u-{session}:p-{session}@gate.example.test:7000"

    assert apply_proxy_session(url, "abc123") == "http://u-abc123:p-abc123@gate.example.test:7000"
    assert apply_proxy_session("http://u:p@gate.example.test:7000", "abc123") == (
        "http://u:p@gate.example.test:7000"
    )
    assert apply_proxy_session(None, "abc123") is None


def test_session_tokens_are_random_credential_safe_hex():
    tokens = {new_proxy_session_token() for _ in range(50)}

    assert len(tokens) == 50
    assert all(re.fullmatch(r"[0-9a-f]{16}", token) for token in tokens)


def _processing_context() -> TargetProcessingContext:
    required = {
        item.name: MagicMock()
        for item in dataclasses.fields(TargetProcessingContext)
        if item.default is dataclasses.MISSING and item.default_factory is dataclasses.MISSING
    }
    return TargetProcessingContext(**required)


def test_each_run_context_gets_one_stable_session_token():
    first_run = _processing_context()
    second_run = _processing_context()

    assert first_run.proxy_session == first_run.proxy_session
    assert first_run.proxy_session != second_run.proxy_session
    assert first_run.proxy_session not in repr(first_run)


@pytest.mark.parametrize("source", ["target", "job", "service"])
def test_runtime_context_applies_the_run_token_for_every_proxy_source(source: str):
    target_cfg = MagicMock(url="https://shop.example.test/list", proxy=None)
    config = MagicMock(adaptive=False, schedule=None, proxy=None)
    settings = MagicMock(proxy_url="")
    if source == "target":
        target_cfg.proxy = ProxyConfig(url=_TEMPLATE)
    elif source == "job":
        config.proxy = ProxyConfig(url=_TEMPLATE)
    else:
        settings.proxy_url = _TEMPLATE

    contexts = [
        resolve_target_runtime_context(
            target_cfg=target_cfg,
            config=config,
            settings=settings,
            run_artifacts_dir=None,
            target_index=index,
            proxy_session="run0token",
        )
        for index in range(2)
    ]

    assert {context.proxy_url for context in contexts} == {
        "http://customer-acme-session-run0token:secret@gate.example.test:7000"
    }


def test_runtime_context_without_a_session_leaves_the_proxy_unchanged():
    context = resolve_target_runtime_context(
        target_cfg=MagicMock(url="https://shop.example.test/list", proxy=None),
        config=MagicMock(adaptive=False, schedule=None, proxy=None),
        settings=MagicMock(proxy_url="http://u:p@gate.example.test:7000"),
        run_artifacts_dir=None,
    )

    assert context.proxy_url == "http://u:p@gate.example.test:7000"
    assert PROXY_SESSION_PLACEHOLDER not in context.proxy_url


def test_substituted_proxy_url_redacts_the_session_token():
    token = new_proxy_session_token()
    resolved = apply_proxy_session(_TEMPLATE, token)
    assert resolved is not None

    assert redact_proxy_url(resolved) == "gate.example.test:7000"
    message = redact_userinfo_in_text(f"proxy connect failed via {resolved}: 407")
    assert token not in message
    assert "secret" not in message
