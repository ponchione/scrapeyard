"""Named API credentials, authorization scopes, and project boundaries."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from fastapi import Request

from scrapeyard.api.response_utils import raise_json_error
from scrapeyard.common.paths import safe_path_part


_CREDENTIAL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class AuthScope(str, Enum):
    submit = "submit"
    read = "read"
    schedule_admin = "schedule-admin"
    delete = "delete"
    health_detail = "health-detail"


ALL_AUTH_SCOPES = frozenset(AuthScope)


@dataclass(frozen=True, slots=True)
class APICredential:
    name: str
    identity: str
    secret: str = field(repr=False)
    scopes: frozenset[AuthScope]
    projects: frozenset[str] | None = None


@dataclass(frozen=True, slots=True)
class AuthenticatedCaller:
    identity: str
    credential_name: str | None
    scopes: frozenset[AuthScope]
    projects: frozenset[str] | None
    authentication_enabled: bool

    def permits_project(self, project: str) -> bool:
        return self.projects is None or project in self.projects


LOCAL_DEVELOPMENT_CALLER = AuthenticatedCaller(
    identity="local-development",
    credential_name=None,
    scopes=ALL_AUTH_SCOPES,
    projects=None,
    authentication_enabled=False,
)
PUBLIC_CALLER = AuthenticatedCaller(
    identity="public",
    credential_name=None,
    scopes=frozenset(),
    projects=None,
    authentication_enabled=True,
)


def parse_api_credentials(
    raw: str,
    *,
    legacy_keys: set[str] | None = None,
) -> tuple[APICredential, ...]:
    """Parse the named JSON credential map and optional migration-only keys."""

    credentials: list[APICredential] = []
    if raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("SCRAPEYARD_API_CREDENTIALS must be valid JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("SCRAPEYARD_API_CREDENTIALS must be a JSON object")
        for name, spec in parsed.items():
            if not isinstance(name, str) or _CREDENTIAL_NAME_RE.fullmatch(name) is None:
                raise ValueError(f"Invalid API credential name: {name!r}")
            if not isinstance(spec, dict):
                raise ValueError(f"Credential {name!r} must be a JSON object")
            credentials.append(_parse_named_credential(name, spec))

    for index, secret in enumerate(sorted(legacy_keys or set())):
        digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
        credentials.append(
            APICredential(
                name=f"legacy-{index + 1}",
                identity=f"legacy-sha256:{digest}",
                secret=secret,
                scopes=ALL_AUTH_SCOPES,
                projects=None,
            )
        )

    digests: set[str] = set()
    names: set[str] = set()
    for credential in credentials:
        digest = hashlib.sha256(credential.secret.encode("utf-8")).hexdigest()
        if digest in digests:
            raise ValueError("API credential secrets must be unique")
        if credential.name in names:
            raise ValueError(f"Duplicate API credential name: {credential.name!r}")
        digests.add(digest)
        names.add(credential.name)
    return tuple(credentials)


def _parse_named_credential(name: str, spec: dict[str, Any]) -> APICredential:
    allowed = {"secret", "identity", "scopes", "projects"}
    unknown = set(spec) - allowed
    if unknown:
        raise ValueError(f"Credential {name!r} has unknown fields: {sorted(unknown)}")
    secret = spec.get("secret")
    if (
        not isinstance(secret, str)
        or not 16 <= len(secret.encode("utf-8")) <= 512
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in secret)
    ):
        raise ValueError(
            f"Credential {name!r} secret must contain 16 to 512 visible ASCII bytes"
        )
    identity = spec.get("identity", name)
    if not isinstance(identity, str) or _CREDENTIAL_NAME_RE.fullmatch(identity) is None:
        raise ValueError(f"Credential {name!r} has an invalid identity")
    raw_scopes = spec.get("scopes")
    if not isinstance(raw_scopes, list) or not raw_scopes:
        raise ValueError(f"Credential {name!r} must declare at least one scope")
    try:
        scopes = frozenset(AuthScope(value) for value in raw_scopes)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Credential {name!r} contains an invalid scope") from exc
    raw_projects = spec.get("projects")
    projects: frozenset[str] | None = None
    if raw_projects is not None:
        if not isinstance(raw_projects, list) or not raw_projects:
            raise ValueError(f"Credential {name!r} projects must be a non-empty list")
        if not all(isinstance(project, str) for project in raw_projects):
            raise ValueError(f"Credential {name!r} contains an invalid project")
        projects = frozenset(
            safe_path_part(project, label="credential project")
            for project in raw_projects
        )
    return APICredential(name, identity, secret, scopes, projects)


def authenticate_api_key(
    provided: str,
    credentials: tuple[APICredential, ...],
) -> AuthenticatedCaller | None:
    """Compare against every configured secret before selecting a caller."""

    matched: APICredential | None = None
    for credential in credentials:
        try:
            is_match = secrets.compare_digest(provided, credential.secret)
        except TypeError:
            is_match = False
        if is_match:
            matched = credential
    if matched is None:
        return None
    return AuthenticatedCaller(
        identity=matched.identity,
        credential_name=matched.name,
        scopes=matched.scopes,
        projects=matched.projects,
        authentication_enabled=True,
    )


def get_authenticated_caller(request: Request) -> AuthenticatedCaller:
    caller = getattr(request.state, "caller", None)
    if not isinstance(caller, AuthenticatedCaller):
        raise RuntimeError("Authentication middleware did not attach a caller")
    return caller


def authorize_request(
    request: Request,
    scope: AuthScope,
    *,
    project: str | None = None,
) -> AuthenticatedCaller:
    caller = get_authenticated_caller(request)
    if scope not in caller.scopes:
        raise_json_error(403, f"Caller lacks required scope {scope.value!r}")
    if project is not None and not caller.permits_project(project):
        raise_json_error(403, f"Caller is not authorized for project {project!r}")
    return caller
