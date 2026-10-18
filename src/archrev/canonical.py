"""Canonical serialization and stable identities.

Every artifact written by the pipeline goes through `write_json`, so identical
inputs produce byte-identical files. Lists must be sorted by the producer;
this module only sorts dictionary keys.
"""

import hashlib
import json
import uuid
from pathlib import Path

# Fixed namespace: changing it changes every GUID ever emitted.
GUID_NAMESPACE = uuid.UUID("b1e6f3a2-5c4d-4e8f-9a17-3d2c0b9e7f41")


def guid(canonical_id: str) -> str:
    return str(uuid.uuid5(GUID_NAMESPACE, canonical_id))


def dumps(obj) -> str:
    return json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_json(path: Path, obj) -> str:
    text = dumps(obj)
    path.write_bytes(text.encode("utf-8"))
    return sha256_text(text)


def write_text(path: Path, text: str) -> str:
    path.write_bytes(text.encode("utf-8"))
    return sha256_text(text)


def fingerprint(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:20]
