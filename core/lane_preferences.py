"""
core/lane_preferences.py -- SUBSCRIPTION-PLAN-BACKENDS-01B.

A distinct, NON-CREDENTIAL model slot for backend lanes that cannot share
the legacy ``cloud_credentials[provider]`` record. Pure dict helpers in the
same style as core/reasoning_preferences.py: no I/O (callers bundle the
change into their own persistence.update()/save()), no validation against
live capability data.

Persisted shape -- a new top-level key inside the same prefs dict
core/persistence.py already reads/writes:

    prefs["backend_models"][lane] = "<exact model id>"

Why a separate slot: the persisted key "openai" means the OpenAI API-key
lane and keeps ``cloud_credentials["openai"]["default_model"]`` exactly as
it is today. A future "openai_chatgpt_plan" lane shares a provider FAMILY
with it but no state -- if it reused the family's record, choosing a plan
model would overwrite the API lane's model (and vice versa). Existing
records are not migrated; the legacy lanes keep their legacy slots, which is
why this module refuses to store a model for them (two sources of truth
for one lane is how a stale value wins).

What may be stored here: a model id string, nothing else. Never OAuth
tokens, issued client ids, account/subject/workspace ids, host ids or
session material -- prefs.json is portable and backed up (Agent Backup
copies the whole file), so anything placed in it travels. Those belong to a
host-local session store in a later slice. set_lane_model() refuses
secret-shaped values as defense in depth; it is not a redaction feature.

Sibling per-lane state already keyed by lane and untouched by this module:
``backend_context[lane]`` and ``backend_reasoning[lane][model]``.
"""
import re
from typing import Optional

from core.backend_identity import AuthSource, lane_descriptor
from core.redaction import SECRET_VALUE_RE

_PREFS_KEY = "backend_models"
_MAX_MODEL_ID_CHARS = 200
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]*$")


def get_lane_model(prefs: dict, lane: str) -> Optional[str]:
    """Pure read. A missing key, a missing lane, or a malformed stored value
    all return None -- a hand-edited prefs file never raises here."""
    lane_map = prefs.get(_PREFS_KEY)
    if not isinstance(lane_map, dict):
        return None
    value = lane_map.get(lane)
    return value if isinstance(value, str) and value else None


def set_lane_model(prefs: dict, lane: str, model: Optional[str]) -> dict:
    """Mutate ``prefs`` in place (and return it) -- creating
    ``prefs["backend_models"]`` as needed. ``model=None`` removes the lane's
    entry. Does NOT persist; see module docstring.

    Raises ValueError for: a lane that is not a known subscription lane
    (legacy lanes keep their own slots), or a model value that is not a
    plain model id (empty, over-long, whitespace/control characters, or
    shaped like a credential).
    """
    descriptor = lane_descriptor(lane)
    if descriptor is None or descriptor.auth_source is not AuthSource.OAUTH_SUBSCRIPTION:
        raise ValueError(
            f"backend_models holds the model of subscription lanes only; "
            f"{lane!r} keeps its existing preference slot."
        )
    if model is not None:
        if not isinstance(model, str):
            raise ValueError("model must be a string or None")
        model = model.strip()
        if (
            not model
            or len(model) > _MAX_MODEL_ID_CHARS
            or not _MODEL_ID_RE.match(model)
            or SECRET_VALUE_RE.search(model)
        ):
            raise ValueError("model is not a plain model id")
    # Validation is complete: no failed call has touched `prefs`.
    lane_map = prefs.get(_PREFS_KEY)
    if not isinstance(lane_map, dict):
        lane_map = prefs[_PREFS_KEY] = {}
    if model is None:
        lane_map.pop(descriptor.lane, None)
    else:
        lane_map[descriptor.lane] = model
    return prefs
