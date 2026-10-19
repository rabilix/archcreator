"""Macro provenance of references: which macro produced a token, if any, and where that macro is defined."""

from pathlib import Path

from archcreator.discovery import PathMapper
from archcreator.parser import parse_units

HEADER = """#ifndef H_H
#define H_H
extern int target(void);
extern int par;
#define HDR_CALL() target()
#define HDR_PAR par
#define SAT(v, max) ((v) > (max) ? (max) : (v))
#define WRAP(x) (x)
#endif
"""

SOURCE = """#include "h.h"
#define LOCAL_CALL() target()
#define TWO_LINES() \\
    target()
int direct(void) { return target() + par; }
int header_body(void) { return HDR_CALL(); }
int same_file_body(void) { return LOCAL_CALL(); }
int multi_line_body(void) { return TWO_LINES(); }
int as_argument(void) { return WRAP(target()) + SAT(par, 3); }
int inner_in_outer_argument(void) { return WRAP(HDR_CALL()) + SAT(HDR_PAR, 3); }
"""


def _refs(tmp_path: Path) -> dict[tuple[str, str], list[tuple[str, str]]]:
    (tmp_path / "h.h").write_text(HEADER, encoding="utf-8")
    (tmp_path / "u.c").write_text(SOURCE, encoding="utf-8")
    ir = parse_units([("u.c", str(tmp_path / "u.c"))], ["-std=c11", "-I", str(tmp_path)], PathMapper(tmp_path, []),
                     {"u.c", "h.h"})
    out: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for r in ir["references"]:
        if r["kind"] in ("call", "global_read"):
            out.setdefault((r["in_function"], r["name"]), []).append((r["via_macro"], r["via_macro_file"]))
    return out


def test_macro_provenance_cases(tmp_path):
    refs = _refs(tmp_path)
    direct = ("", "")
    assert refs[("direct", "target")] == [direct] and refs[("direct", "par")] == [direct]
    assert refs[("header_body", "target")] == [("HDR_CALL", "h.h")]
    assert refs[("same_file_body", "target")] == [("LOCAL_CALL", "u.c")]
    assert refs[("multi_line_body", "target")] == [("TWO_LINES", "u.c")]
    # Written by the author, only passed through a macro: not produced by one, even when the body repeats it.
    assert refs[("as_argument", "target")] == [direct] and refs[("as_argument", "par")] == [direct]
    # Produced by the inner macro's body, inside the outer macro's argument: the inner macro is the producer.
    assert refs[("inner_in_outer_argument", "target")] == [("HDR_CALL", "h.h")]
    assert refs[("inner_in_outer_argument", "par")] == [("HDR_PAR", "h.h")]


def test_macro_body_with_windows_1252_bytes_is_tolerated(tmp_path):
    # A German comment and string inside a #define, encoded as Windows-1252 as the 100554 sources are.
    src = b'#define GREET() "Gr\xfc\xdfe" /* f\xfcr alle */\n#define TARGET() target()\nextern int target(void);\n' \
          b'int f(void) { (void) GREET(); return TARGET(); }\n'
    (tmp_path / "w.c").write_bytes(src)
    ir = parse_units([("w.c", str(tmp_path / "w.c"))], ["-std=c11"], PathMapper(tmp_path, []), {"w.c"})
    bodies = {m["name"]: m["identifiers"] for m in ir["macro_bodies"]}
    assert bodies["TARGET"] == ["target"] and bodies["GREET"] == []
    assert [r["via_macro"] for r in ir["references"] if r["kind"] == "call"] == ["TARGET"]


def test_macro_identities_carry_no_offsets(tmp_path):
    (tmp_path / "h.h").write_text(HEADER, encoding="utf-8")
    (tmp_path / "u.c").write_text(SOURCE, encoding="utf-8")
    ir = parse_units([("u.c", str(tmp_path / "u.c"))], ["-std=c11", "-I", str(tmp_path)], PathMapper(tmp_path, []),
                     {"u.c", "h.h"})
    usrs = {d["name"]: d["usr"] for d in ir["declarations"] if d["kind"] == "macro"}
    assert usrs["HDR_CALL"] == "c:h.h@macro@HDR_CALL"
    assert usrs["LOCAL_CALL"] == "c:u.c@macro@LOCAL_CALL"
