"""Explicit read-only loading of a closed set of tracked, inert manifests."""
from pathlib import Path

from .model import CapabilityManifest, ContractError, MAX_JSON_BYTES, decode_json


# Source-owned allowlist, never expanded by owner/model/page input or discovery.
_MANIFEST_FILES = (
    ("reddit.reply", "reddit.reply.json"),
    ("reddit.create_post", "reddit.create_post.json"),
)
CAPABILITY_IDS = tuple(capability for capability, _ in _MANIFEST_FILES)


def load_manifest(capability_id: str) -> CapabilityManifest:
    """Read only a fixed bundled file; loading data does not grant authority."""
    if type(capability_id) is not str or capability_id not in CAPABILITY_IDS:
        raise ContractError("unknown capability")
    filename = next(filename for capability, filename in _MANIFEST_FILES if capability == capability_id)
    path = Path(__file__).with_name("manifests") / filename
    with path.open("rb") as stream:
        raw = stream.read(MAX_JSON_BYTES + 1)
    manifest = CapabilityManifest.from_data(decode_json(raw))
    if manifest.capability_id != capability_id:
        raise ContractError("bundled capability identity mismatch")
    return manifest
