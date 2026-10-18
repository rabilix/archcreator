"""Accepted findings: one human decision per fingerprint, kept next to the central profile.

A baseline never removes a finding. It gives it a status:

- new: not in the baseline (the only status --fail-on counts, together with regressed)
- accepted: in the baseline, within its measured_max if one is given
- regressed: in the baseline, but the measured value grew past measured_max (ratchet)

Entries whose fingerprint no longer occurs are reported as stale so the file can be cleaned. The file is validated
strictly; placeholders left by `archrev baseline-draft` are rejected until a person fills them in.
"""

import datetime
import math
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .canonical import dumps, sha256_text
from .rules import RULE_TEXT


class BaselineError(Exception):
    pass


BASELINE_VERSION = 1
STATUS_NEW, STATUS_ACCEPTED, STATUS_REGRESSED = "new", "accepted", "regressed"

_TOP_KEYS = {"baseline_version", "accepted"}
_ENTRY_KEYS = {"fingerprint", "rule", "module", "symbol", "file", "reason", "approved_by", "date", "measured_max",
               "ticket"}
_REQUIRED = ("fingerprint", "rule", "reason", "approved_by", "date")
_FINGERPRINT = re.compile(r"^[0-9a-f]{20}$")
_PLACEHOLDER = re.compile(r"^\s*(todo|tbd)?\s*$", re.IGNORECASE)
PLACEHOLDER = "TODO"


@dataclass(frozen=True)
class Entry:
    fingerprint: str
    rule: str
    reason: str
    approved_by: str
    date: str
    measured_max: float | None = None
    # The module the finding belongs to. One baseline serves every module of a repository, so a single-module run
    # can only judge the entries of its own module as stale; entries without module are judged by `scan` only.
    module: str | None = None
    symbol: str | None = None
    file: str | None = None
    ticket: str | None = None

    def public(self) -> dict:
        return {"reason": self.reason, "approved_by": self.approved_by, "date": self.date,
                "measured_max": self.measured_max, "ticket": self.ticket}


@dataclass
class Baseline:
    entries: dict[str, Entry]
    source_hash: str
    file_name: str


def _text(index: int, entry: dict, key: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or _PLACEHOLDER.match(value):
        raise BaselineError(f"accepted[{index}] ({entry.get('fingerprint', '?')}): '{key}' must be filled in")
    return value.strip()


def _optional_text(index: int, entry: dict, key: str) -> str | None:
    value = entry.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise BaselineError(f"accepted[{index}]: '{key}' must be a string")
    return value


def load_baseline(path: Path) -> Baseline:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BaselineError(f"cannot read baseline {path}: {exc.strerror or exc}") from None
    except UnicodeDecodeError as exc:
        raise BaselineError(f"baseline {path.name} is not UTF-8: {exc}") from None
    try:
        data = yaml.safe_load(text) or {}
    except (yaml.YAMLError, ValueError) as exc:  # ValueError: an unquoted date that does not exist, e.g. 2026-02-30
        raise BaselineError(f"baseline {path.name} is not valid YAML: {exc}") from None
    if not isinstance(data, dict) or not all(isinstance(k, str) for k in data):
        raise BaselineError("baseline must be a mapping with string keys")
    unknown = sorted(set(data) - _TOP_KEYS)
    if unknown:
        raise BaselineError(f"unknown key(s) in baseline: {', '.join(unknown)}")
    if data.get("baseline_version") != BASELINE_VERSION:
        raise BaselineError(f"baseline_version must be {BASELINE_VERSION}")
    accepted = data.get("accepted", [])
    if not isinstance(accepted, list):
        raise BaselineError("'accepted' must be a list")

    entries: dict[str, Entry] = {}
    for i, raw in enumerate(accepted):
        if not isinstance(raw, dict) or not all(isinstance(k, str) for k in raw):
            raise BaselineError(f"accepted[{i}] must be a mapping")
        unknown = sorted(set(raw) - _ENTRY_KEYS)
        if unknown:
            raise BaselineError(f"accepted[{i}]: unknown key(s) {', '.join(unknown)}")
        missing = [k for k in _REQUIRED if k not in raw]
        if missing:
            raise BaselineError(f"accepted[{i}]: missing {', '.join(missing)}")
        fp = raw["fingerprint"]
        if not isinstance(fp, str) or not _FINGERPRINT.match(fp):
            raise BaselineError(f"accepted[{i}]: fingerprint must be 20 hex digits, got {fp!r}")
        if fp in entries:
            raise BaselineError(f"accepted[{i}]: fingerprint {fp} is listed twice")
        rule = raw["rule"]
        if not isinstance(rule, str) or rule not in RULE_TEXT:
            raise BaselineError(f"accepted[{i}] ({fp}): unknown rule {rule!r}")
        # YAML already turns an unquoted 2026-10-18 into a date; a quoted one stays a string.
        date = raw["date"].isoformat() if isinstance(raw["date"], datetime.date) else _text(i, raw, "date")
        try:
            datetime.date.fromisoformat(date)
        except ValueError:
            raise BaselineError(f"accepted[{i}] ({fp}): date must be YYYY-MM-DD, got {date!r}") from None
        measured_max = raw.get("measured_max")
        if measured_max is not None and (isinstance(measured_max, bool) or not isinstance(measured_max, (int, float))
                                         or not math.isfinite(measured_max)):
            raise BaselineError(f"accepted[{i}] ({fp}): measured_max must be a finite number")
        entries[fp] = Entry(fp, rule, _text(i, raw, "reason"), _text(i, raw, "approved_by"), date, measured_max,
                            _optional_text(i, raw, "module"), _optional_text(i, raw, "symbol"),
                            _optional_text(i, raw, "file"), _optional_text(i, raw, "ticket"))
    return Baseline(entries, sha256_text(text), path.name)


def apply_baseline(findings: list[dict], baseline: Baseline | None, modules: set[str] | None = None) -> list[dict]:
    """Sets `status` (and `baseline`) on every finding; returns the stale entries.

    An entry is stale when no finding matched it and the run could have produced it: `modules` names the modules the
    findings cover (a single module for `analyze`, every module for `scan`); None means every entry is in scope.
    """
    seen: set[str] = set()
    for f in findings:
        entry = baseline.entries.get(f["fingerprint"]) if baseline else None
        if entry is None:
            f["status"], f["baseline"] = STATUS_NEW, None
            continue
        if entry.rule != f["rule_id"]:
            raise BaselineError(f"baseline entry {entry.fingerprint} says rule {entry.rule}, "
                                f"the finding is {f['rule_id']}")
        seen.add(entry.fingerprint)
        measured = f.get("measured_value")
        regressed = (entry.measured_max is not None and isinstance(measured, (int, float))
                     and not isinstance(measured, bool) and measured > entry.measured_max)
        f["status"] = STATUS_REGRESSED if regressed else STATUS_ACCEPTED
        f["baseline"] = entry.public()
    if baseline is None:
        return []
    return [{"fingerprint": e.fingerprint, "rule": e.rule, "module": e.module, "symbol": e.symbol, "file": e.file}
            for fp, e in sorted(baseline.entries.items())
            if fp not in seen and (modules is None or e.module in modules)]


def gating(findings: list[dict]) -> list[dict]:
    """The findings --fail-on counts: everything a person has not accepted, plus what got worse."""
    return [f for f in findings if f["status"] != STATUS_ACCEPTED]


def draft(findings: list[dict]) -> str:
    """A baseline with every current finding and placeholders a person must replace before it loads."""
    entries = []
    for f in findings:
        entry = {"fingerprint": f["fingerprint"], "rule": f["rule_id"], "module": f["module"], "symbol": f["symbol"],
                 "file": f["file"], "reason": PLACEHOLDER, "approved_by": PLACEHOLDER, "date": PLACEHOLDER}
        measured = f.get("measured_value")
        if isinstance(measured, (int, float)) and not isinstance(measured, bool):
            entry["measured_max"] = measured
        entries.append(entry)
    header = ("# archrev baseline draft: every entry needs reason, approved_by and date before archrev accepts it.\n"
              "# measured_max is the ratchet for numeric findings: a larger value counts as regressed.\n")
    return header + yaml.safe_dump({"baseline_version": BASELINE_VERSION, "accepted": entries},
                                   sort_keys=False, allow_unicode=True, width=120)


__all__ = ["Baseline", "BaselineError", "Entry", "apply_baseline", "draft", "dumps", "gating", "load_baseline"]
