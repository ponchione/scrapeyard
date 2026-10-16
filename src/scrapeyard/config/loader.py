"""YAML configuration loader."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Iterator
from typing import Any
from urllib.parse import quote, quote_plus

from pydantic import ValidationError

from scrapeyard.common.paths import safe_path_part
from scrapeyard.common.settings import get_settings
from scrapeyard.common.yaml import load_yaml_mapping
from scrapeyard.config.schema import ScrapeConfig
from scrapeyard.config.transforms import config_regex_budget

_SECRET_REFERENCE_RE = re.compile(r"\$\{(SCRAPEYARD_SECRET_[A-Z0-9_]+)\}")
# YAML's \uD800-style escapes load as surrogate code points, which no UTF-8
# encoder accepts, so a string holding one breaks every response it reaches.
_SURROGATE_RE = re.compile("[\ud800-\udfff]")
MIN_DEPLOYMENT_SECRET_LENGTH = 8
_WITHHELD_MESSAGE = "invalid; the message is withheld because the value holds a deployment secret"


def _surrogate_location(value: Any, location: tuple[str, ...] = ()) -> str | None:
    """Return the dotted location of the first key or string with a surrogate."""

    if isinstance(value, str):
        if _SURROGATE_RE.search(value):
            return ".".join(location) or "config"
        return None
    if isinstance(value, dict):
        for key, item in value.items():
            # Escaped, so the error message itself stays encodable.
            part = str(key).encode("utf-8", "backslashreplace").decode("utf-8")
            if _SURROGATE_RE.search(str(key)):
                return ".".join((*location, part))
            found = _surrogate_location(item, (*location, part))
            if found is not None:
                return found
    if isinstance(value, list):
        for index, item in enumerate(value):
            found = _surrogate_location(item, (*location, str(index)))
            if found is not None:
                return found
    return None


def _reject_surrogates(data: dict[str, Any]) -> None:
    location = _surrogate_location(data)
    if location is not None:
        raise ValueError(
            f"Config strings must not contain surrogate code points (U+D800 to U+DFFF): "
            f"{location}"
        )


def _secret_references(value: Any) -> set[str]:
    if isinstance(value, dict):
        references: set[str] = set()
        for item in value.values():
            references.update(_secret_references(item))
        return references
    if isinstance(value, list):
        references = set()
        for item in value:
            references.update(_secret_references(item))
        return references
    if isinstance(value, str):
        return {match.group(1) for match in _SECRET_REFERENCE_RE.finditer(value)}
    return set()


def _browser_header_secret_reference(data: dict[str, Any]) -> str | None:
    """Return a secret reference that a target's browser `extra_headers` hold.

    A browser follows a server's redirect itself, past the route that keeps
    these headers to the target's origin, and sends them on to wherever it
    points (TD-102).
    """
    targets: list[Any] = [data.get("target")]
    if isinstance(data.get("targets"), list):
        targets.extend(data["targets"])
    for target in targets:
        browser = target.get("browser") if isinstance(target, dict) else None
        headers = browser.get("extra_headers") if isinstance(browser, dict) else None
        if not isinstance(headers, dict):
            continue
        for value in headers.values():
            match = _SECRET_REFERENCE_RE.search(value) if isinstance(value, str) else None
            if match is not None:
                return match.group(1)
    return None


def _resolve_secret_references(value: Any, resolved_secrets: dict[str, str]) -> Any:
    """Resolve deployment secret references without changing persisted YAML."""

    if isinstance(value, dict):
        return {
            key: _resolve_secret_references(item, resolved_secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_resolve_secret_references(item, resolved_secrets) for item in value]
    if not isinstance(value, str):
        return value

    def _replace(match: re.Match[str]) -> str:
        variable = match.group(1)
        return resolved_secrets[variable]

    return _SECRET_REFERENCE_RE.sub(_replace, value)


def _literal_project(data: dict[str, Any]) -> str:
    project = data.get("project")
    if not isinstance(project, str) or _SECRET_REFERENCE_RE.search(project):
        raise ValueError(
            "Config project must be a literal string before secret references are resolved"
        )
    return safe_path_part(project, label="project")


def _require_literal_name(data: dict[str, Any]) -> None:
    name = data.get("name")
    if not isinstance(name, str) or _SECRET_REFERENCE_RE.search(name):
        raise ValueError(
            "Config name must be a literal string before secret references are resolved"
        )


def parse_config_project(yaml_str: str) -> tuple[dict[str, Any], str]:
    """Parse a config once and read its literal project namespace.

    No secret reference is resolved, so the caller can authorize the project
    before validating the mapping with :func:`load_config_mapping`.
    """

    data = load_yaml_mapping(yaml_str)
    return data, _literal_project(data)


def load_config(yaml_str: str) -> ScrapeConfig:
    """Parse a YAML string into a validated ScrapeConfig."""
    return load_config_mapping(load_yaml_mapping(yaml_str))


def _secret_paths(value: Any, path: tuple[Any, ...] = ()) -> Iterator[tuple[Any, ...]]:
    """Yield the path of each string in *value* that holds a secret reference."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _secret_paths(item, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _secret_paths(item, (*path, index))
    elif isinstance(value, str) and _SECRET_REFERENCE_RE.search(value):
        yield path


def _input_path(data: Any, loc: tuple[int | str, ...]) -> tuple[Any, ...]:
    """Follow an error's location into the input as far as the input has it.

    A location also names union members, which the input has no key for. The
    walk stops at the first such part, so the path it gives holds at least
    the value the error was raised for.
    """
    path: list[Any] = []
    node = data
    for part in loc:
        in_mapping = isinstance(node, dict) and part in node
        in_list = isinstance(node, list) and isinstance(part, int) and 0 <= part < len(node)
        if not (in_mapping or in_list):
            break
        node = node[part]
        path.append(part)
    return tuple(path)


def _holds_secret(value: Any, secrets: Iterable[str]) -> bool:
    """Whether any string in *value* contains a resolved secret, in any case."""
    if isinstance(value, dict):
        return any(_holds_secret(item, secrets) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_holds_secret(item, secrets) for item in value)
    if isinstance(value, str):
        folded = value.casefold()
        return any(secret.casefold() in folded for secret in secrets)
    return False


def _redact_secret_spellings(text: str, secrets: Iterable[str]) -> str:
    """Redact each secret as written, URL-quoted or escaped, in any letter case."""
    spellings: set[str] = set()
    for secret in secrets:
        spellings.update((
            secret,
            quote(secret, safe=""),
            quote_plus(secret, safe=""),
            repr(secret)[1:-1],
            json.dumps(secret)[1:-1],
        ))
    for spelling in sorted(spellings, key=lambda value: (-len(value), value)):
        text = re.sub(re.escape(spelling), "<redacted>", text, flags=re.IGNORECASE)
    return text


def load_config_mapping(data: dict[str, Any]) -> ScrapeConfig:
    """Validate a parsed YAML mapping into a ScrapeConfig."""
    _reject_surrogates(data)
    references = _secret_references(data)
    resolved_secrets: dict[str, str] = {}
    if references:
        in_browser_headers = _browser_header_secret_reference(data)
        if in_browser_headers is not None:
            raise ValueError(
                f"Deployment secret reference {in_browser_headers} is not allowed in "
                "browser.extra_headers: a browser sends them on through a redirect to "
                "any origin"
            )
        project = _literal_project(data)
        _require_literal_name(data)
        policy = get_settings().parsed_secret_reference_allowlist()
        allowed = policy.get(project, frozenset()) | policy.get("*", frozenset())
        denied = sorted(references - allowed)
        if denied:
            raise ValueError(
                f"Deployment secret reference {denied[0]} is not allowed for project {project!r}"
            )
        for reference in sorted(references):
            resolved = os.environ.get(reference)
            if resolved is None:
                raise ValueError(f"Missing deployment secret reference {reference}")
            if len(resolved) < MIN_DEPLOYMENT_SECRET_LENGTH:
                raise ValueError(
                    f"Deployment secret reference {reference} must contain at least "
                    f"{MIN_DEPLOYMENT_SECRET_LENGTH} characters"
                )
            resolved_secrets[reference] = resolved
    try:
        with config_regex_budget():
            config = ScrapeConfig(**_resolve_secret_references(data, resolved_secrets))
    except Exception as exc:
        if not resolved_secrets:
            raise
        # Never let a resolved deployment secret escape merely because its
        # destination is invalid. Pydantic's own text quotes the input, cut
        # short or escaped where no exact match finds it, so only its messages
        # are kept. A message about a value that held a secret is withheld: a
        # validator's may quote a piece of it (a transform pipeline splits at
        # `:` and `|`) or give it lower-cased (a URL scheme). The chained
        # exception, which holds the input, is dropped for any log.
        if not isinstance(exc, ValidationError):
            raise ValueError(
                f"Config is invalid ({type(exc).__name__}); the message is withheld "
                "because the config holds a deployment secret"
            ) from None
        #
        # A location's union-member names are not input keys, but an input
        # can hold a key spelled like one, which the walk then follows away
        # from the secret. So a message is also withheld when the value it
        # was raised for holds a secret: every message that quotes a value
        # is raised on one that still holds the whole of it.
        secret_paths = list(_secret_paths(data))
        secrets = [secret for secret in resolved_secrets.values() if secret]
        messages = []
        for error in exc.errors(include_url=False):
            path = _input_path(data, error["loc"])
            message = error["msg"]
            if any(secret[: len(path)] == path for secret in secret_paths) or _holds_secret(
                error.get("input"), secrets
            ):
                message = _WITHHELD_MESSAGE
            location = ".".join(str(part) for part in error["loc"]) or "config"
            messages.append(f"{location}: {message}")
        detail = "; ".join(messages)
        raise ValueError(_redact_secret_spellings(detail, resolved_secrets.values())) from None
    config._resolved_secret_values = tuple(
        value for value in dict.fromkeys(resolved_secrets.values()) if value
    )
    return config
