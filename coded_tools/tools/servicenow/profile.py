"""
Deployment profile: everything this package must not know at build time.

The package ships with no base URL, no endpoint path, no table name and no
credential. Those arrive at runtime from:

  1. A mounted profile document named by ``SN_PROFILE_FILE`` (JSON, or HOCON when
     the filename ends in ``.hocon``). Normally a ConfigMap.
  2. Environment overrides using the ``SN_PROFILE__`` prefix and ``__`` as a path
     separator, so any single key can be overridden without reissuing the file::

         SN_PROFILE__base_url=...
         SN_PROFILE__operations__read__path=...

     Values parse as JSON when possible, else as plain strings.

Secrets are never stored in the profile. The profile names the *environment
variables* that hold them (``client_id_env`` and friends), so the ConfigMap stays
free of secret material and the Secret stays the only place values live.

Loading happens once and is cached, so no request ever performs profile I/O.
Validation is fail-fast: a misconfigured deployment raises at first use with the
offending key named, rather than surfacing as a confusing 404 mid-conversation.
"""

import json
import os
import threading
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import Dict
from typing import List
from typing import Mapping
from typing import Optional
from typing import Tuple

from coded_tools.tools.servicenow.errors import ProfileError

ENV_PROFILE_FILE: str = "SN_PROFILE_FILE"
ENV_OVERRIDE_PREFIX: str = "SN_PROFILE__"
ENV_PATH_SEPARATOR: str = "__"

# The placeholder an entity-scoped path template must contain.
TABLE_PLACEHOLDER: str = "{table}"

VALID_AUTH_STYLES: Tuple[str, ...] = ("standard", "preencoded")


@dataclass(frozen=True)
class Limits:
    """Bounds that protect the event loop, the gateway and the context window."""

    default_page: int = 10
    max_page: int = 25
    timeout_seconds: float = 30.0
    connect_timeout_seconds: float = 10.0
    max_retries: int = 2
    backoff_base_seconds: float = 0.5
    breaker_threshold: int = 5
    breaker_reset_seconds: float = 30.0
    max_concurrent: int = 8


@dataclass(frozen=True)
class AuthConfig:
    """
    How to obtain a bearer token, and what extra headers business calls need.

    Only environment-variable *names* live here. Values are read from the process
    environment at call time and never retained on the profile.
    """

    style: str
    token_url: str
    client_id_env: str = "SN_CLIENT_ID"
    client_secret_env: str = "SN_CLIENT_SECRET"
    preencoded_credential_env: str = "SN_GW_CREDENTIAL"
    # The variant flow sends the secret under a non-standard body key; the key
    # name is configuration, not an assumption baked into code.
    secret_body_key: str = "client_secret"
    send_id_in_body: bool = False
    # RFC 6749 §2.3.1 requires percent-encoding id and secret before base64. It is
    # a no-op for opaque hex credentials and is the spec, so it defaults on; a
    # gateway that ignores the rule can switch it off without a code change.
    percent_encode_credentials: bool = True
    scope: Optional[str] = None
    # Header name -> literal value, or "env:VAR_NAME" to resolve from the
    # environment. Covers gateway-specific per-call headers without naming any.
    extra_headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class OperationConfig:
    """Where a logical operation goes in *this* deployment."""

    method: str
    path: str
    enabled: bool = True
    # Optional override of the profile-level base URL, for the gateway topology
    # that actually exists in the field: the one proven create endpoint lives on a
    # different host AND a different API version than the reads. Without this, the
    # real inventory cannot be expressed at all — a gap found by asking "can this
    # onboard the documented endpoints", not by any test, because every test
    # inherited the single-base assumption.
    base_url: Optional[str] = None


@dataclass(frozen=True)
class EntityConfig:
    """
    A logical entity name mapped to a concrete table plus its field allow-lists.

    Deny-by-default: a field absent from ``read_fields`` is never requested, and a
    field absent from ``write_fields`` can never be written.
    """

    table: str
    read_fields: Tuple[str, ...]
    write_fields: Tuple[str, ...] = ()
    identifier_field: str = "sys_id"
    display_field: str = "number"
    # Field name -> {code: human label}. Declared per deployment because these
    # vocabularies are instance-specific. Used in both directions: codes become
    # labels on the way out, labels become codes on the way in. Without a map, a
    # bare code is tagged rather than passed through bare — an unlabelled code is
    # the thing a language model will confidently invent a meaning for.
    coded_fields: Mapping[str, Mapping[str, str]] = field(default_factory=dict)


@dataclass(frozen=True)
class GateConfig:
    """Approval-gate parameters. Signing keys are read from the environment."""

    keys_env: str = "SN_GATE_KEYS"
    ttl_seconds: int = 300
    # Journal-style fields that may be configured to bypass the gate. Empty by
    # default: nothing bypasses unless a deployment opts in.
    auto_approve_fields: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Profile:
    """The whole immutable deployment profile."""

    base_url: str
    auth: AuthConfig
    operations: Mapping[str, OperationConfig]
    entities: Mapping[str, EntityConfig]
    params: Mapping[str, str]
    limits: Limits
    gate: GateConfig
    correlation_field: Optional[str] = None
    verify_tls: bool = True

    def operation(self, name: str) -> OperationConfig:
        """
        :param name: The logical operation name.
        :return: Its configuration.
        :raises ProfileError: if the operation is unknown or disabled.
        """
        config: Optional[OperationConfig] = self.operations.get(name)
        if config is None:
            raise ProfileError(f"Operation '{name}' is not configured for this deployment.",
                               operation=name)
        if not config.enabled:
            raise ProfileError(f"Operation '{name}' is disabled for this deployment.",
                               reason="operation_disabled", operation=name)
        return config

    def entity(self, name: str) -> EntityConfig:
        """
        :param name: The logical entity name.
        :return: Its configuration.
        :raises ProfileError: if the entity is not on the allow-list.
        """
        config: Optional[EntityConfig] = self.entities.get(name)
        if config is None:
            raise ProfileError(f"Entity '{name}' is not configured for this deployment.",
                               entity=name)
        return config


DEFAULT_PARAMS: Dict[str, str] = {
    "query": "sysparm_query",
    "fields": "sysparm_fields",
    "limit": "sysparm_limit",
    "offset": "sysparm_offset",
    "display": "sysparm_display_value",
    "exclude_reference_link": "sysparm_exclude_reference_link",
}


def _read_document(path: str) -> Dict[str, Any]:
    """
    Read the profile document from disk.

    :param path: Filesystem path to a JSON or HOCON document.
    :return: The parsed document as a dictionary.
    """
    if not os.path.isfile(path):
        raise ProfileError(f"Profile file named by {ENV_PROFILE_FILE} does not exist.",
                           reason="profile_missing")
    try:
        if path.endswith((".hocon", ".conf")):
            # pyhocon ships with neuro-san, so this costs no new dependency.
            from pyhocon import ConfigFactory  # pylint: disable=import-outside-toplevel
            return ConfigFactory.parse_file(path).as_plain_ordered_dict()
        with open(path, "r", encoding="utf-8") as document:
            return json.load(document)
    except ProfileError:
        raise
    except Exception as exception:  # pylint: disable=broad-exception-caught
        # Deliberately does not echo the file body: a malformed profile may
        # contain half-written secret material.
        raise ProfileError(f"Profile file could not be parsed: {type(exception).__name__}",
                           reason="profile_unparseable") from exception


def _strip_comments(value: Any) -> Any:
    """
    Drop keys beginning with an underscore, recursively.

    Profile documents are read by humans as often as by code, so they carry inline
    commentary. JSON has no comment syntax, so underscore-prefixed keys serve, and
    they are removed before validation rather than tripping it.

    :param value: Any node of the document.
    :return: The node with commentary keys removed.
    """
    if isinstance(value, Mapping):
        return {key: _strip_comments(item) for key, item in value.items()
                if not str(key).startswith("_")}
    if isinstance(value, list):
        return [_strip_comments(item) for item in value]
    return value


def _coerce(raw: str) -> Any:
    """
    :param raw: A raw environment-variable value.
    :return: The value parsed as JSON when possible, else the original string.
    """
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def _apply_env_overrides(document: Dict[str, Any],
                         environ: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """
    Overlay ``SN_PROFILE__`` environment overrides onto a profile document.

    :param document: The document parsed from file (may be empty).
    :param environ: Environment mapping; defaults to os.environ.
    :return: The document with overrides applied.
    """
    source: Mapping[str, str] = os.environ if environ is None else environ
    for key, value in sorted(source.items()):
        if not key.startswith(ENV_OVERRIDE_PREFIX):
            continue
        trail: List[str] = [part for part in
                            key[len(ENV_OVERRIDE_PREFIX):].split(ENV_PATH_SEPARATOR) if part]
        if not trail:
            continue
        cursor: Dict[str, Any] = document
        for step in trail[:-1]:
            existing: Any = cursor.get(step)
            if not isinstance(existing, dict):
                existing = {}
                cursor[step] = existing
            cursor = existing
        cursor[trail[-1]] = _coerce(value)
    return document


def _require(document: Mapping[str, Any], key: str, where: str) -> Any:
    """
    :param document: The mapping to read.
    :param key: The required key.
    :param where: Human-readable location, used in the error message.
    :return: The value.
    :raises ProfileError: naming the missing key.
    """
    if key not in document or document[key] in (None, ""):
        raise ProfileError(f"Profile is missing required key '{key}' in {where}.",
                           missing_key=f"{where}.{key}")
    return document[key]


def _build_auth(document: Mapping[str, Any]) -> AuthConfig:
    """
    :param document: The ``auth`` sub-document.
    :return: A validated AuthConfig.
    """
    style: str = str(_require(document, "style", "auth"))
    if style not in VALID_AUTH_STYLES:
        raise ProfileError(f"auth.style must be one of {list(VALID_AUTH_STYLES)}, got '{style}'.",
                           invalid_key="auth.style")
    extra_headers: Any = document.get("extra_headers", {})
    if not isinstance(extra_headers, Mapping):
        raise ProfileError("auth.extra_headers must be a mapping of header name to value.",
                           invalid_key="auth.extra_headers")
    defaults = AuthConfig(style=style, token_url="")
    return AuthConfig(
        style=style,
        token_url=str(_require(document, "token_url", "auth")),
        client_id_env=str(document.get("client_id_env", defaults.client_id_env)),
        client_secret_env=str(document.get("client_secret_env", defaults.client_secret_env)),
        preencoded_credential_env=str(document.get("preencoded_credential_env",
                                                   defaults.preencoded_credential_env)),
        secret_body_key=str(document.get("secret_body_key", defaults.secret_body_key)),
        send_id_in_body=bool(document.get("send_id_in_body", style == "preencoded")),
        percent_encode_credentials=bool(document.get("percent_encode_credentials",
                                                     defaults.percent_encode_credentials)),
        scope=document.get("scope") or None,
        extra_headers=dict(extra_headers),
    )


def _build_operations(document: Mapping[str, Any]) -> Dict[str, OperationConfig]:
    """
    :param document: The ``operations`` sub-document.
    :return: Validated operation configurations keyed by logical name.
    """
    if not isinstance(document, Mapping) or not document:
        raise ProfileError("Profile must define at least one entry under 'operations'.",
                           missing_key="operations")
    operations: Dict[str, OperationConfig] = {}
    for name, raw in document.items():
        if not isinstance(raw, Mapping):
            raise ProfileError(f"operations.{name} must be a mapping.",
                               invalid_key=f"operations.{name}")
        enabled: bool = bool(raw.get("enabled", True))
        if not enabled:
            # A disabled operation need not be fully specified; it can never route.
            operations[name] = OperationConfig(method=str(raw.get("method", "")),
                                               path=str(raw.get("path", "")),
                                               enabled=False)
            continue
        override: Any = raw.get("base_url")
        if override is not None and (not isinstance(override, str) or not override.strip()):
            raise ProfileError(f"operations.{name}.base_url must be a non-empty string "
                               "when present.",
                               invalid_key=f"operations.{name}.base_url")
        operations[name] = OperationConfig(
            method=str(_require(raw, "method", f"operations.{name}")).upper(),
            path=str(_require(raw, "path", f"operations.{name}")),
            enabled=True,
            base_url=override.rstrip("/") if override else None,
        )
    return operations


def _build_entities(document: Mapping[str, Any]) -> Dict[str, EntityConfig]:
    """
    :param document: The ``entities`` sub-document.
    :return: Validated entity configurations keyed by logical name.
    """
    if not isinstance(document, Mapping) or not document:
        raise ProfileError("Profile must define at least one entry under 'entities'.",
                           missing_key="entities")
    entities: Dict[str, EntityConfig] = {}
    for name, raw in document.items():
        if not isinstance(raw, Mapping):
            raise ProfileError(f"entities.{name} must be a mapping.",
                               invalid_key=f"entities.{name}")
        read_fields: Any = raw.get("read_fields") or []
        if not isinstance(read_fields, (list, tuple)) or not read_fields:
            raise ProfileError(f"entities.{name}.read_fields must be a non-empty list. "
                               "Deny-by-default means an entity with no readable fields "
                               "could never be used.",
                               invalid_key=f"entities.{name}.read_fields")
        write_fields: Any = raw.get("write_fields") or []
        if not isinstance(write_fields, (list, tuple)):
            raise ProfileError(f"entities.{name}.write_fields must be a list.",
                               invalid_key=f"entities.{name}.write_fields")
        coded: Any = raw.get("coded_fields") or {}
        if not isinstance(coded, Mapping):
            raise ProfileError(f"entities.{name}.coded_fields must be a mapping of "
                               "field name to {code: label}.",
                               invalid_key=f"entities.{name}.coded_fields")
        coded_fields: Dict[str, Dict[str, str]] = {}
        for field_name, mapping in coded.items():
            if not isinstance(mapping, Mapping) or not mapping:
                raise ProfileError(
                    f"entities.{name}.coded_fields.{field_name} must be a non-empty "
                    "mapping of code to label.",
                    invalid_key=f"entities.{name}.coded_fields.{field_name}")
            coded_fields[str(field_name)] = {str(code): str(label)
                                             for code, label in mapping.items()}

        defaults = EntityConfig(table="", read_fields=())
        entities[name] = EntityConfig(
            table=str(_require(raw, "table", f"entities.{name}")),
            read_fields=tuple(str(item) for item in read_fields),
            write_fields=tuple(str(item) for item in write_fields),
            identifier_field=str(raw.get("identifier_field", defaults.identifier_field)),
            display_field=str(raw.get("display_field", defaults.display_field)),
            coded_fields=coded_fields,
        )
    return entities


def _build_limits(document: Mapping[str, Any]) -> Limits:
    """
    :param document: The ``limits`` sub-document.
    :return: A validated Limits, falling back to safe code defaults.
    """
    defaults = Limits()
    limits = Limits(
        default_page=int(document.get("default_page", defaults.default_page)),
        max_page=int(document.get("max_page", defaults.max_page)),
        timeout_seconds=float(document.get("timeout_seconds", defaults.timeout_seconds)),
        connect_timeout_seconds=float(document.get("connect_timeout_seconds",
                                                   defaults.connect_timeout_seconds)),
        max_retries=int(document.get("max_retries", defaults.max_retries)),
        backoff_base_seconds=float(document.get("backoff_base_seconds",
                                                defaults.backoff_base_seconds)),
        breaker_threshold=int(document.get("breaker_threshold", defaults.breaker_threshold)),
        breaker_reset_seconds=float(document.get("breaker_reset_seconds",
                                                 defaults.breaker_reset_seconds)),
        max_concurrent=int(document.get("max_concurrent", defaults.max_concurrent)),
    )
    if limits.default_page < 1 or limits.max_page < 1:
        raise ProfileError("limits.default_page and limits.max_page must be positive.",
                           invalid_key="limits")
    if limits.default_page > limits.max_page:
        raise ProfileError("limits.default_page cannot exceed limits.max_page.",
                           invalid_key="limits.default_page")
    if limits.timeout_seconds <= 0 or limits.max_concurrent < 1:
        raise ProfileError("limits.timeout_seconds and limits.max_concurrent must be positive.",
                           invalid_key="limits")
    return limits


def build_profile(document: Mapping[str, Any]) -> Profile:
    """
    Validate a raw profile document and freeze it into a Profile.

    :param document: The merged profile document.
    :return: An immutable, validated Profile.
    :raises ProfileError: naming the first offending key.
    """
    if not isinstance(document, Mapping) or not document:
        raise ProfileError(
            f"No deployment profile found. Set {ENV_PROFILE_FILE} to a mounted profile "
            f"document, or supply {ENV_OVERRIDE_PREFIX}* environment overrides.",
            reason="profile_missing")

    document = _strip_comments(document)

    params: Dict[str, str] = dict(DEFAULT_PARAMS)
    supplied_params: Any = document.get("params", {})
    if supplied_params:
        if not isinstance(supplied_params, Mapping):
            raise ProfileError("params must be a mapping of logical name to wire name.",
                               invalid_key="params")
        params.update({str(key): str(value) for key, value in supplied_params.items()})

    gate_document: Mapping[str, Any] = document.get("gate", {}) or {}
    gate_defaults = GateConfig()
    gate = GateConfig(
        keys_env=str(gate_document.get("keys_env", gate_defaults.keys_env)),
        ttl_seconds=int(gate_document.get("ttl_seconds", gate_defaults.ttl_seconds)),
        auto_approve_fields=tuple(str(item) for item in
                                  (gate_document.get("auto_approve_fields") or ())),
    )
    if gate.ttl_seconds < 1:
        raise ProfileError("gate.ttl_seconds must be positive.", invalid_key="gate.ttl_seconds")

    base_url: str = str(_require(document, "base_url", "profile")).rstrip("/")
    entities: Dict[str, EntityConfig] = _build_entities(document.get("entities", {}))
    operations: Dict[str, OperationConfig] = _build_operations(document.get("operations", {}))

    profile = Profile(
        base_url=base_url,
        auth=_build_auth(document.get("auth", {}) or {}),
        operations=operations,
        entities=entities,
        params=params,
        limits=_build_limits(document.get("limits", {}) or {}),
        gate=gate,
        correlation_field=document.get("correlation_field") or None,
        verify_tls=bool(document.get("verify_tls", True)),
    )

    if not profile.verify_tls:
        # Allowed only as an explicit, visible deployment choice. It is never the
        # default and is never silently inherited.
        raise ProfileError(
            "verify_tls=false is refused. TLS verification is not negotiable in this package; "
            "supply the gateway's CA bundle via the standard REQUESTS_CA_BUNDLE instead.",
            invalid_key="verify_tls")

    return profile


_PROFILE_LOCK = threading.Lock()
_PROFILE: Optional[Profile] = None


def load_profile(environ: Optional[Mapping[str, str]] = None) -> Profile:
    """
    Build a Profile from file plus environment overrides, without caching.

    :param environ: Environment mapping; defaults to os.environ.
    :return: A validated Profile.
    """
    source: Mapping[str, str] = os.environ if environ is None else environ
    document: Dict[str, Any] = {}
    profile_file: Optional[str] = source.get(ENV_PROFILE_FILE)
    if profile_file:
        document = _read_document(profile_file)
    document = _apply_env_overrides(document, source)
    return build_profile(document)


def get_profile() -> Profile:
    """
    Return the process-wide Profile, loading it exactly once.

    Loading is lazy but happens at most once, so no request performs profile I/O
    and every request sees the same immutable object.

    :return: The cached Profile.
    """
    global _PROFILE  # pylint: disable=global-statement
    if _PROFILE is not None:
        return _PROFILE
    with _PROFILE_LOCK:
        if _PROFILE is None:
            _PROFILE = load_profile()
    return _PROFILE


def set_profile(profile: Optional[Profile]) -> None:
    """
    Install or clear the cached Profile. Intended for tests and for a deliberate
    reload; production code should rely on get_profile().

    :param profile: The Profile to install, or None to clear the cache.
    """
    global _PROFILE  # pylint: disable=global-statement
    with _PROFILE_LOCK:
        _PROFILE = profile
