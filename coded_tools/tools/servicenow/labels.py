"""
Translation between stored codes and human labels.

The problem this exists to solve is not cosmetic. When a coded value such as
``state='1'`` reaches a language model without its label, the model does not report
it as a code — it invents a meaning, confidently and inconsistently. Observed
directly: the same ``state='1'`` was rendered "New" on one run and "Open" on the
next, from identical data. In an access-management context a model narrating a state
code as "Approved" when it means something else is the kind of error nobody catches
until afterwards.

Two directions, both needed, and they must agree:

  * **Outbound (read).** A code becomes its label, so there is nothing left to guess.
    A code with no configured label is *tagged* rather than passed through bare —
    an explicit "unmapped code 7" is something a model will repeat verbatim, whereas
    a naked "7" is something it will explain.
  * **Inbound (write).** A label the model echoes back becomes the code again.
    Without this, translating on read would silently break writes: the model would
    read "In Progress" and try to write "In Progress" into a field that stores "2".

Both directions run before the approval payload is hashed, so a proposal and its
commit normalise identically and the signature still matches.
"""

from typing import Any
from typing import Dict
from typing import Mapping
from typing import Optional

from coded_tools.tools.servicenow.profile import EntityConfig

#: Wrapper for a code the deployment has not mapped. Deliberately unnatural
#: language: a model repeats it rather than smoothing it into a plausible word.
UNMAPPED_TEMPLATE: str = "<unmapped code {code}>"


def _looks_unmapped(value: str) -> bool:
    """
    :param value: A field value.
    :return: True when the value is already an unmapped-code marker.
    """
    return value.startswith("<unmapped code ") and value.endswith(">")


def to_label(entity: EntityConfig, field: str, value: Any) -> Any:
    """
    Convert one stored value to its human label.

    :param entity: The entity configuration carrying the code maps.
    :param field: The field name.
    :param value: The stored value.
    :return: The label, an explicit unmapped marker, or the value unchanged when the
             field is not declared coded (in which case it is assumed to be a
             display value already, which is what a gateway honouring display-value
             requests returns).
    """
    mapping: Optional[Mapping[str, str]] = entity.coded_fields.get(field)
    if mapping is None or value is None:
        return value
    text: str = str(value)
    if text in mapping:
        return mapping[text]
    if text in mapping.values() or _looks_unmapped(text):
        # Already a label (a gateway that honours display values will send one).
        return text
    return UNMAPPED_TEMPLATE.format(code=text)


def to_code(entity: EntityConfig, field: str, value: Any) -> Any:
    """
    Convert one supplied value back to the code the system stores.

    Codes are matched before labels, so a deployment whose labels happen to look
    like codes still round-trips predictably.

    :param entity: The entity configuration carrying the code maps.
    :param field: The field name.
    :param value: The supplied value, which may be a code or a label.
    :return: The stored code, or the value unchanged when nothing matches.
    """
    mapping: Optional[Mapping[str, str]] = entity.coded_fields.get(field)
    if mapping is None or value is None:
        return value
    text: str = str(value)
    if text in mapping:
        return text
    for code, label in mapping.items():
        if text.casefold() == label.casefold():
            return code
    return value


def decode_record(entity: EntityConfig, record: Mapping[str, Any]) -> Dict[str, Any]:
    """
    :param entity: The entity configuration.
    :param record: A record as returned by the gateway.
    :return: The record with coded fields rendered as labels.
    """
    return {name: to_label(entity, name, value) for name, value in record.items()}


def encode_fields(entity: EntityConfig, fields: Mapping[str, Any]) -> Dict[str, Any]:
    """
    :param entity: The entity configuration.
    :param fields: Field values as supplied by the caller.
    :return: The fields with labels converted back to stored codes.
    """
    return {name: to_code(entity, name, value) for name, value in fields.items()}


def describe(entity: EntityConfig, field: str) -> Optional[str]:
    """
    :param entity: The entity configuration.
    :param field: The field name.
    :return: A short description of the permitted labels for a coded field, for
             inclusion in an error message, or None when the field is not coded.
    """
    mapping: Optional[Mapping[str, str]] = entity.coded_fields.get(field)
    if not mapping:
        return None
    return ", ".join(sorted(mapping.values()))
