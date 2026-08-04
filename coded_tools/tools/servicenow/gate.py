"""
The two-phase approval gate.

A write is proposed, a human approves, and only then does anything mutate. The
enforcement lives in code rather than in prompt instructions, which is the whole
point: a model that is confused, jailbroken, or reading an injected instruction out
of a ticket body cannot talk its way past a signature check.

Shape of the control:

  * **Phase 1 (propose)** computes a hash over the exact intended change, mints an
    HMAC-signed token binding that hash to one record, and parks the token on
    sly_data. Only the human-readable diff is returned to the model.
  * **Phase 2 (commit)** re-derives the hash from the arguments actually presented
    and refuses unless it matches the signed one.

Two properties follow, and both are worth stating to a reviewer:

  * The token never enters model context, so it cannot be fabricated or read out.
  * The token is bound to a payload hash, so an approval for one change cannot be
    re-aimed at a different record or a different field set.

Tokens are stateless and signed rather than stored, so a token minted on one
replica verifies on another. The cost of that choice is that they cannot be
globally single-use; see ``verify`` for the bounds that replace it.
"""

import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any
from typing import Dict
from typing import List
from typing import Mapping
from typing import Optional
from typing import Sequence
from typing import Tuple

from coded_tools.tools.servicenow.context import SLY_CONSUMED_KEY
from coded_tools.tools.servicenow.errors import GateError
from coded_tools.tools.servicenow.profile import Profile

TOKEN_VERSION: str = "v1"

#: Sentinel record id for a creation, which has no identifier until it exists. The
#: gate binds a token to (entity, record, payload); this keeps that binding total.
#: Lives here because both the proposal that mints for it and the creation that
#: verifies against it need the same value.
NEW_RECORD: str = "@new"

#: The forms a caller may use to ask for a new record.
NEW_RECORD_ALIASES: Tuple[str, ...] = ("new", "@new")


def canonical_payload(entity: str, record_id: str, fields: Mapping[str, Any]) -> str:
    """
    Render the intended change in a stable, order-independent form.

    :param entity: Logical entity name.
    :param record_id: The record being changed.
    :param fields: The field/value pairs to write.
    :return: Canonical JSON.
    """
    return json.dumps(
        {"entity": entity, "record": record_id, "fields": dict(fields)},
        sort_keys=True, separators=(",", ":"), default=str)


def payload_hash(entity: str, record_id: str, fields: Mapping[str, Any]) -> str:
    """
    :param entity: Logical entity name.
    :param record_id: The record being changed.
    :param fields: The field/value pairs to write.
    :return: Hex SHA-256 of the canonical payload.
    """
    return hashlib.sha256(canonical_payload(entity, record_id, fields).encode("utf-8")).hexdigest()


def _signing_keys(profile: Profile) -> List[bytes]:
    """
    Read the ordered signing keys from the environment.

    The first key signs; every key verifies. That is what makes rotation
    zero-downtime, and with a five-minute token lifetime the overlap window is
    trivially short.

    :param profile: The deployment profile.
    :return: Key material, first key first.
    :raises GateError: if no key is configured.
    """
    raw: Optional[str] = os.environ.get(profile.gate.keys_env)
    keys: List[bytes] = [part.strip().encode("utf-8")
                         for part in (raw or "").replace("\n", ",").split(",")
                         if part.strip()]
    if not keys:
        raise GateError(
            f"No approval-gate signing key is configured (expected environment variable "
            f"'{profile.gate.keys_env}'). Gated operations are disabled until one is "
            "supplied from the secret manager. Generating a key per process is "
            "explicitly not done: it appears to work on a single replica and silently "
            "fails whenever a change is proposed on one pod and committed on another.",
            reason="gate_unconfigured")
    return keys


def _sign(key: bytes, body: bytes) -> str:
    """
    :param key: Signing key.
    :param body: The encoded token body.
    :return: URL-safe base64 signature.
    """
    digest: bytes = hmac.new(key, body, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _b64(raw: bytes) -> str:
    """:param raw: Bytes to encode. :return: Unpadded URL-safe base64."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    """:param text: Unpadded URL-safe base64. :return: The decoded bytes."""
    padding: str = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def mint(profile: Profile, entity: str, record_id: str, fields: Mapping[str, Any],
         correlation_id: str, now: Optional[float] = None) -> Tuple[str, Dict[str, Any]]:
    """
    Mint a commit token for one exact change.

    :param profile: The deployment profile.
    :param entity: Logical entity name.
    :param record_id: The record to be changed.
    :param fields: The field/value pairs to write.
    :param correlation_id: The request's correlation id, carried inside the token so
                           the propose and commit turns reconcile to one identifier.
    :param now: Epoch seconds; defaults to time.time().
    :return: (token, claims) where claims are safe to log.
    """
    keys: List[bytes] = _signing_keys(profile)
    moment: float = time.time() if now is None else now
    claims: Dict[str, Any] = {
        "v": TOKEN_VERSION,
        "h": payload_hash(entity, record_id, fields),
        "r": record_id,
        "e": entity,
        "c": correlation_id,
        "x": int(moment + profile.gate.ttl_seconds),
        "n": uuid.uuid4().hex,
    }
    body: bytes = json.dumps(claims, sort_keys=True, separators=(",", ":")).encode("utf-8")
    token: str = f"{_b64(body)}.{_sign(keys[0], body)}"
    return token, claims


@dataclass(frozen=True)
class Verdict:
    """The outcome of presenting a token for a specific change."""

    ok: bool
    result: str
    reason: str = ""
    correlation_id: Optional[str] = None
    nonce: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        """:return: A JSON-serializable summary safe for an audit marker."""
        payload: Dict[str, Any] = {"approved": self.ok, "result": self.result}
        if self.reason:
            payload["reason"] = self.reason
        if self.nonce:
            payload["token_id"] = self.nonce
        return payload


def is_auto_approved(profile: Profile, fields: Mapping[str, Any]) -> bool:
    """
    :param profile: The deployment profile.
    :param fields: The fields about to be written.
    :return: True when every field is on the deployment's auto-approve list.
             Empty by default: nothing bypasses the gate unless opted in.
    """
    allowed: Sequence[str] = profile.gate.auto_approve_fields
    return bool(fields) and bool(allowed) and all(name in allowed for name in fields)


def verify(profile: Profile, token: Optional[str], entity: str, record_id: str,
           fields: Mapping[str, Any], sly_data: Optional[Dict[str, Any]] = None,
           now: Optional[float] = None) -> Verdict:
    """
    Check a presented token against the change actually being attempted.

    Replay bounds, in place of global single-use (which would require shared state
    this design deliberately avoids):

      * a short expiry, from ``gate.ttl_seconds``;
      * binding to one exact payload, so a replay can only repeat the identical,
        already-approved change — idempotent by construction;
      * within a request, consumed nonces are recorded on sly_data and refused.

    The accepted residual risk is a replay of the same approved change, in its
    window, across separate requests.

    :param profile: The deployment profile.
    :param token: The token presented, read from sly_data by the caller.
    :param entity: Logical entity name.
    :param record_id: The record being changed.
    :param fields: The field/value pairs actually about to be written.
    :param sly_data: The request's sly_data, used to record consumption.
    :param now: Epoch seconds; defaults to time.time().
    :return: A Verdict. Never raises for an invalid token; only for misconfiguration.
    """
    if is_auto_approved(profile, fields):
        return Verdict(True, "auto_approved",
                       reason="all fields are on the deployment auto-approve list")

    if not token:
        return Verdict(False, "missing_token",
                       reason="no approval token was presented on the sly_data channel")

    keys: List[bytes] = _signing_keys(profile)

    try:
        encoded_body, signature = str(token).split(".", 1)
        body: bytes = _unb64(encoded_body)
        claims: Mapping[str, Any] = json.loads(body)
    except (ValueError, TypeError):
        return Verdict(False, "malformed_token", reason="token could not be decoded")

    if claims.get("v") != TOKEN_VERSION:
        return Verdict(False, "malformed_token", reason="unrecognized token version")

    if not any(hmac.compare_digest(signature, _sign(key, body)) for key in keys):
        return Verdict(False, "bad_signature", reason="token signature did not verify")

    moment: float = time.time() if now is None else now
    if moment > float(claims.get("x", 0)):
        return Verdict(False, "expired", reason="approval token has expired",
                       correlation_id=claims.get("c"), nonce=claims.get("n"))

    nonce: Optional[str] = claims.get("n")
    consumed: Any = (sly_data or {}).get(SLY_CONSUMED_KEY) or []
    if nonce and nonce in consumed:
        return Verdict(False, "already_consumed",
                       reason="this approval has already been used in this request",
                       correlation_id=claims.get("c"), nonce=nonce)

    if claims.get("e") != entity or claims.get("r") != record_id:
        return Verdict(False, "record_mismatch",
                       reason="the approval was issued for a different record",
                       correlation_id=claims.get("c"), nonce=nonce)

    if claims.get("h") != payload_hash(entity, record_id, fields):
        return Verdict(False, "payload_mismatch",
                       reason="the change presented differs from the change approved",
                       correlation_id=claims.get("c"), nonce=nonce)

    if sly_data is not None and nonce:
        sly_data[SLY_CONSUMED_KEY] = list(consumed) + [nonce]

    return Verdict(True, "approved", correlation_id=claims.get("c"), nonce=nonce)
