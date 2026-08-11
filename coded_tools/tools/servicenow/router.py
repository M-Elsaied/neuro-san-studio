"""
Package-level endpoint routing.

The registry HOCON never names a table, a path or a host. An agent pins only a
logical ``operation`` and ``entity``; this module turns that pair into a fully
bound route using the deployment profile.

The split that makes it work:

  * **Shapes are code.** There are four request shapes in this API surface, and
    each is a class in this package. They do not vary by deployment.
  * **Destinations are data.** Method, path and table come from the profile.

So adding an endpoint is a profile edit. Only a genuinely new *shape* requires
Python, and then it is one subclass.
"""

import threading
from dataclasses import dataclass
from dataclasses import replace
from enum import Enum
from typing import Mapping
from typing import Optional
from typing import Tuple
from urllib.parse import quote

from coded_tools.tools.servicenow.errors import PolicyDenied
from coded_tools.tools.servicenow.errors import ProfileError
from coded_tools.tools.servicenow.profile import EntityConfig
from coded_tools.tools.servicenow.profile import Limits
from coded_tools.tools.servicenow.profile import OperationConfig
from coded_tools.tools.servicenow.profile import Profile
from coded_tools.tools.servicenow.profile import TABLE_PLACEHOLDER
from coded_tools.tools.servicenow.profile import get_profile


#: Optional placeholder in a path template. When present, the record identifier is
#: addressed in the URL; when absent, it travels in the request body.
RECORD_PLACEHOLDER: str = "{record}"


class Shape(Enum):
    """The wire shapes this package knows how to build and parse."""

    QUERY = "query"
    BODY = "body"
    MULTIPART = "multipart"
    BINARY = "binary"


@dataclass(frozen=True)
class OperationSpec:
    """
    What an operation *is*, derived from its profile entry at resolve time.

    Operations are declared in the deployment profile rather than fixed in code, so
    adding an endpoint is a configuration change. Code supplies only the *shape*
    implementations, not the list of operations.

    :param retryable: True only for operations safe to repeat. Writes are never
                      auto-retried: an asynchronous create that returns no reliable
                      reference could duplicate records under blind retry.
    :param entity_scoped: True when the path addresses a table (`{table}` present);
                          False for fixed paths that do not target a table.
    :param query_params: For query-shape operations, exactly which logical params
                         to send.
    """

    name: str
    shape: Shape
    gated: bool
    retryable: bool
    entity_scoped: bool
    query_params: Tuple[str, ...]

    @classmethod
    def from_config(cls, name: str, config: OperationConfig) -> "OperationSpec":
        """
        :param name: Logical operation name.
        :param config: Its profile entry.
        :return: The derived spec.
        """
        return cls(
            name=name,
            shape=Shape(config.shape),
            gated=config.is_gated,
            retryable=config.is_retryable,
            entity_scoped=TABLE_PLACEHOLDER in config.path,
            query_params=config.query_params,
        )


@dataclass(frozen=True)
class BoundRoute:
    """A logical (operation, entity) pair resolved against this deployment."""

    spec: OperationSpec
    entity_name: str
    entity: EntityConfig
    method: str
    url: str
    params: Mapping[str, str]
    limits: Limits
    #: True when the path template addresses the record itself. Gateways differ:
    #: some take the identifier in the path, others expect it in the body. The
    #: presence of the placeholder in the profile decides, so neither is assumed.
    record_in_path: bool = False

    def bind_record(self, record_id: str) -> "BoundRoute":
        """
        :param record_id: The record identifier.
        :return: A route with the record placeholder substituted, or self when the
                 deployment carries the identifier in the body instead.
        """
        if not self.record_in_path:
            return self
        return replace(self, url=self.url.replace(RECORD_PLACEHOLDER,
                                                  quote(str(record_id), safe="")))

    @property
    def route_key(self) -> str:
        """
        :return: A stable, non-identifying key for breaker and metric bucketing.
                 Deliberately logical (operation + entity), so it never leaks a
                 host or path into a log line.
        """
        return f"{self.spec.name}:{self.entity_name}"


class Router:
    """
    Resolves logical operation/entity pairs to bound routes.

    Construction validates the whole profile against the known operation specs, so
    a deployment error surfaces at startup with the offending key named rather
    than as a puzzling 404 in the middle of a conversation.
    """

    def __init__(self, profile: Profile):
        """
        :param profile: The validated deployment profile.
        :raises ProfileError: if the profile disagrees with the known shapes.
        """
        self.profile: Profile = profile
        self._validate()

    def _validate(self) -> None:
        """
        Validate every enabled operation builds a spec (shape is known).
        """
        for name, config in self.profile.operations.items():
            if not config.enabled:
                continue
            # Building the spec resolves the shape; an unknown shape raises here at
            # startup rather than mid-request.
            OperationSpec.from_config(name, config)

        if self.profile.gate.auto_approve_fields:
            # An auto-approve field that no entity can write is a silent misconfiguration.
            writable = {field
                        for entity in self.profile.entities.values()
                        for field in entity.write_fields}
            unknown = sorted(set(self.profile.gate.auto_approve_fields) - writable)
            if unknown:
                raise ProfileError(
                    f"gate.auto_approve_fields names fields no entity can write: {unknown}.",
                    invalid_key="gate.auto_approve_fields")

    def resolve(self, operation: str, entity: str) -> BoundRoute:
        """
        Resolve a logical pair into a bound route.

        :param operation: Logical operation name, pinned in the agent's args.
        :param entity: Logical entity name, pinned in the agent's args.
        :return: The bound route.
        :raises PolicyDenied: if the entity does not permit this operation.
        :raises ProfileError: if the operation or entity is unknown or disabled.
        """
        operation_config: OperationConfig = self.profile.operation(operation)
        entity_config: EntityConfig = self.profile.entity(entity)
        spec: OperationSpec = OperationSpec.from_config(operation, operation_config)

        if spec.shape is Shape.BODY and not entity_config.write_fields:
            raise PolicyDenied(
                f"Entity '{entity}' is read-only in this deployment.",
                entity=entity, operation=operation)

        path: str = operation_config.path
        if spec.entity_scoped:
            path = path.replace(TABLE_PLACEHOLDER, entity_config.table)
        # An operation may live on a different host or API version than the rest of
        # the gateway; the optional per-operation override expresses that. Never
        # assumed — it applies only when a profile sets it.
        base: str = operation_config.base_url or self.profile.base_url
        url: str = f"{base}/{path.lstrip('/')}"

        return BoundRoute(
            spec=spec,
            entity_name=entity,
            entity=entity_config,
            method=operation_config.method,
            url=url,
            params=self.profile.params,
            limits=self.profile.limits,
            record_in_path=RECORD_PLACEHOLDER in url,
        )


_ROUTER_LOCK = threading.Lock()
_ROUTER: Optional[Router] = None
_ROUTER_FOR: Optional[int] = None


def get_router() -> Router:
    """
    Return the process-wide Router, building it at most once per profile.

    :return: The cached Router.
    """
    global _ROUTER, _ROUTER_FOR  # pylint: disable=global-statement
    profile: Profile = get_profile()
    if _ROUTER is not None and _ROUTER_FOR == id(profile):
        return _ROUTER
    with _ROUTER_LOCK:
        if _ROUTER is None or _ROUTER_FOR != id(profile):
            _ROUTER = Router(profile)
            _ROUTER_FOR = id(profile)
    return _ROUTER


def reset_router() -> None:
    """Drop the cached Router. Intended for tests and deliberate reloads."""
    global _ROUTER, _ROUTER_FOR  # pylint: disable=global-statement
    with _ROUTER_LOCK:
        _ROUTER = None
        _ROUTER_FOR = None
