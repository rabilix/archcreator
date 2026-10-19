"""libclang front end: parses App translation units into the IR.

Only cursors located in App files are recorded. Everything they reference
outside the App folder is captured as a reference with its declaring file,
which later becomes an EXTERNAL module.

Known blind spots (reported in the run report, not hidden):
- type-only dependencies (typedef/struct use without a symbol reference)
- references in code disabled by the active preprocessor configuration
- write classification is syntactic: an index expression inside the LHS of
  an assignment counts as part of the write target
"""

import ctypes
import re

import clang.cindex as ci

CK = ci.CursorKind

# CXBinaryOperatorKind / CXUnaryOperatorKind values, verified against libclang 18.1.1.
BINOP_ASSIGN = 22
UNOP_WRITE = {1, 2, 3, 4}  # post/pre increment/decrement
UNOP_ADDROF = 5
UNOP_DEREF = 6

_TRANSPARENT = {CK.UNEXPOSED_EXPR, CK.PAREN_EXPR, CK.CSTYLE_CAST_EXPR}

# USRs of file-local entities (statics, macros, anonymous enums) embed the file name as
# spelled in the #include, e.g. "c:dcdc_fsm.h@967@macro@X" vs "c:DCDC_FSM.h@967@macro@X".
_FILE_USR = re.compile(r"^c:([^@]+)@")
# The byte offset libclang puts after the file for macros and anonymous entities ("@967@macro@X"): it changes with
# every edit above the definition, so it is dropped from identities.
_OFFSET = re.compile(r"^\d+@")

_PREPROC = {CK.INCLUSION_DIRECTIVE, CK.MACRO_DEFINITION, CK.MACRO_INSTANTIATION}

_bound = False


def _bind_operator_kinds() -> None:
    global _bound
    if _bound:
        return
    lib = ci.conf.lib
    for name in ("clang_getCursorBinaryOperatorKind", "clang_getCursorUnaryOperatorKind",
                 "clang_Cursor_isMacroFunctionLike"):
        fn = getattr(lib, name)
        fn.argtypes = [ci.Cursor]
        fn.restype = ctypes.c_int
    _bound = True


def _is_function_like(macro_cursor) -> bool:
    return bool(ci.conf.lib.clang_Cursor_isMacroFunctionLike(macro_cursor))


def _binop(c) -> int:
    return ci.conf.lib.clang_getCursorBinaryOperatorKind(c)


def _unop(c) -> int:
    return ci.conf.lib.clang_getCursorUnaryOperatorKind(c)


def _is_file_scope_var(ref) -> bool:
    return ref.kind == CK.VAR_DECL and ref.semantic_parent is not None and ref.semantic_parent.kind == CK.TRANSLATION_UNIT


def _classify_access(path, var) -> str:
    """Classify a global variable reference as read / write / addr from its ancestors."""
    var_is_pointer = var.type.get_canonical().kind == ci.TypeKind.POINTER
    through_pointer = False
    result = "global_read"
    for anc, idx in reversed(path):
        k = anc.kind
        if k in _TRANSPARENT:
            continue
        if k == CK.ARRAY_SUBSCRIPT_EXPR:
            if idx != 0:
                break
            through_pointer = through_pointer or var_is_pointer
            continue
        if k == CK.MEMBER_REF_EXPR:
            through_pointer = through_pointer or var_is_pointer
            continue
        if k == CK.BINARY_OPERATOR:
            if _binop(anc) == BINOP_ASSIGN and idx == 0:
                result = "global_write"
            break
        if k == CK.COMPOUND_ASSIGNMENT_OPERATOR:
            if idx == 0:
                result = "global_write"
            break
        if k == CK.UNARY_OPERATOR:
            op = _unop(anc)
            if op == UNOP_DEREF:
                through_pointer = True
                continue
            if op in UNOP_WRITE:
                result = "global_write"
            elif op == UNOP_ADDROF:
                result = "global_addr"
            break
        break
    if through_pointer and result == "global_write":
        return "global_read"
    return result


def _callee_declref(call):
    children = list(call.get_children())
    node = children[0] if children else None
    while node is not None and node.kind in _TRANSPARENT:
        sub = list(node.get_children())
        node = sub[0] if sub else None
    if node is not None and node.kind == CK.DECL_REF_EXPR:
        return node
    return None


_IDENT = re.compile(rb"[A-Za-z_][A-Za-z0-9_]*")
_IDENT_STR = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _spellings(tokens) -> list[str]:
    """Token spellings; a token the binding cannot decode (a Windows-1252 byte in a string or comment inside a
    #define) is no identifier and is dropped."""
    out = []
    for t in tokens:
        try:
            out.append(t.spelling)
        except UnicodeDecodeError:
            out.append("")
    return out


def _body_identifiers(tokens: list[str], function_like: bool) -> tuple[str, ...]:
    """Identifiers of a macro body: the tokens after the name and, for a function-like macro, its parameter list.

    `#define WRAP (x)` is object-like with a parenthesised body; only libclang can tell that from `#define WRAP(x)`.
    """
    body = tokens[1:]
    params: set[str] = set()
    if function_like and body and body[0] == "(":
        close = body.index(")") if ")" in body else len(body)
        params = {t for t in body[1:close] if _IDENT_STR.match(t)}
        body = body[close + 1:]
    return tuple(sorted({t for t in body if _IDENT_STR.match(t) and t not in params}))


class _Collector:
    def __init__(self, mapper, app_labels: set[str]):
        self.mapper = mapper
        self.app = app_labels
        self.decls: set[tuple] = set()
        # (file, macro) -> identifiers in the body of an App macro: a wrapper around a signal accessor is found here.
        self.macro_bodies: dict[tuple[str, str], tuple[str, ...]] = {}
        # (name, file) of every macro definition, in any file, whose name occurs in an App macro body: tells the
        # model which identifiers of a body are macros and where they come from.
        self.body_macro_defs: set[tuple[str, str]] = set()
        self.functions: set[tuple] = set()
        self.refs: set[tuple] = set()
        self.includes: set[tuple] = set()
        self._file_bytes: dict[str, bytes] = {}
        # Macro instantiations of the current unit by (site, line, column): the outermost macro a cursor at that
        # location was expanded from, and the file that defines it.
        self._inst: dict[tuple[str, int, int], tuple[str, str]] = {}
        self._tu = None

    def begin_unit(self, tu) -> None:
        self._inst = {}
        self._tu = tu

    def via_macro(self, node, site: str) -> tuple[str, str]:
        """(macro, defining file) when the reference was produced by a macro body; ("", "") when written directly.

        libclang reports expansion locations for cursors, so the tokens of the
        cursor's extent are the only trace of where a token was written. For a
        reference produced by a macro body the first token lies on a #define
        line, which names the producing macro (the innermost one, also when it
        was expanded inside another macro's argument), or there are no tokens at
        all; then the outermost macro invoked at the cursor's start, recorded
        as a MACRO_INSTANTIATION, is the best information. A reference written
        directly, also as a macro argument, starts with its own token in
        ordinary code.
        """
        first = next(iter(node.get_tokens()), None)
        if first is not None:
            # The cursor at the token's own position: a MACRO_DEFINITION when the token is a macro body's.
            definition = ci.Cursor.from_location(self._tu, first.location)
            if definition is None or definition.kind != CK.MACRO_DEFINITION:
                return "", ""
            return definition.spelling, self.label_of(definition)
        start = node.extent.start
        inst = self._inst.get((site, start.line, start.column))
        if inst is not None:
            return inst
        # No instantiation recorded at the start (not expected); name the identifier written there.
        if start.file is None:  # pragma: no cover
            return "<macro>", ""
        path = start.file.name
        data = self._file_bytes.get(path)
        if data is None:
            with open(path, "rb") as fh:
                data = fh.read()
            self._file_bytes[path] = data
        m = _IDENT.match(data, start.offset)
        return (m.group(0).decode("latin-1") if m else "<macro>"), ""

    def label_of(self, cursor) -> str:
        f = cursor.location.file
        return self.mapper.label(f.name if f else None)

    def usr(self, cursor, fallback: str = "") -> str:
        raw = cursor.get_usr()
        if not raw:
            return fallback
        m = _FILE_USR.match(raw)
        if m:
            return f"c:{self.label_of(cursor)}@{_OFFSET.sub('', raw[m.end():])}"
        return raw

    def add_ref(self, site, line, func, kind, name, usr, decl_file, via="", via_file=""):
        self.refs.add((site, line, func or "<file-scope>", kind, name, usr or "", decl_file, via, via_file))

    def symbol_ref(self, node, ref, site, func, kind):
        via, via_file = self.via_macro(node, site)
        self.add_ref(site, node.location.line, func, kind, ref.spelling, self.usr(ref), self.label_of(ref), via, via_file)

    def walk_expr(self, node, path, site, func, skip):
        k = node.kind
        if k == CK.CALL_EXPR:
            ref = node.referenced
            if ref is not None and ref.kind == CK.FUNCTION_DECL:
                callee = _callee_declref(node)
                if callee is not None:
                    skip.add(callee.hash)
                self.symbol_ref(node, ref, site, func, "call")
            else:
                target = ref.spelling if ref is not None else "<expr>"
                self.add_ref(site, node.location.line, func, "fn_pointer_call", target, "", "<unresolved>")
        elif k == CK.DECL_REF_EXPR and node.hash not in skip:
            ref = node.referenced
            if ref is not None:
                if ref.kind == CK.FUNCTION_DECL:
                    self.symbol_ref(node, ref, site, func, "fn_address")
                elif _is_file_scope_var(ref):
                    self.symbol_ref(node, ref, site, func, _classify_access(path, ref))
                elif ref.kind == CK.ENUM_CONSTANT_DECL:
                    self.symbol_ref(node, ref, site, func, "enum_use")
        for i, child in enumerate(node.get_children()):
            path.append((node, i))
            self.walk_expr(child, path, site, func, skip)
            path.pop()

    def top_level(self, c):
        site = self.label_of(c)
        if site not in self.app:
            return
        k = c.kind
        line = c.location.line
        if k == CK.INCLUSION_DIRECTIVE:
            # The binding asserts instead of returning None when the header was not found.
            try:
                inc = c.get_included_file()
            except AssertionError:
                inc = None
            target = self.mapper.label(inc.name) if inc is not None else f"<unresolved>/{c.spelling}"
            self.includes.add((site, line, target, c.spelling))
        elif k == CK.MACRO_DEFINITION:
            self.decls.add(("macro", c.spelling, self.usr(c, f"macro:{c.spelling}"), site, line, True, "EXTERNAL"))
            if (site, c.spelling) not in self.macro_bodies:
                self.macro_bodies[(site, c.spelling)] = _body_identifiers(_spellings(c.get_tokens()),
                                                                          _is_function_like(c))
        elif k == CK.MACRO_INSTANTIATION:
            ref = c.referenced
            if ref is not None and ref.location.file is not None:
                decl = self.label_of(ref)
                self._inst[(site, line, c.location.column)] = (c.spelling, decl)
                self.add_ref(site, line, None, "macro_use", c.spelling, self.usr(ref, f"macro:{c.spelling}"), decl)
        elif k == CK.FUNCTION_DECL:
            is_def = c.is_definition()
            self.decls.add(("function", c.spelling, self.usr(c), site, line, is_def, c.linkage.name))
            if is_def:
                self.functions.add((c.spelling, self.usr(c), site, c.extent.start.line, c.extent.end.line, c.linkage.name))
                self.walk_expr(c, [], site, c.spelling, set())
        elif k == CK.VAR_DECL:
            # A file-scope "int x;" is a tentative definition in C; libclang reports it as a declaration.
            is_def = c.is_definition() or c.storage_class != ci.StorageClass.EXTERN
            self.decls.add(("variable", c.spelling, self.usr(c), site, line, is_def, c.linkage.name))
            self.walk_expr(c, [], site, None, set())
        elif k == CK.ENUM_DECL:
            for e in c.get_children():
                if e.kind == CK.ENUM_CONSTANT_DECL:
                    self.decls.add(("enum_constant", e.spelling, self.usr(e), site, e.location.line, True, "NO_LINKAGE"))


def parse_units(units: list[tuple[str, str]], args: list[str], mapper, app_labels: set[str]) -> dict:
    """units: (label, absolute path) of every App .c to parse."""
    _bind_operator_kinds()
    index = ci.Index.create()
    col = _Collector(mapper, app_labels)
    unit_reports = []
    for label, path in units:
        try:
            tu = index.parse(path, args=args, options=ci.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD)
        except ci.TranslationUnitLoadError as exc:
            unit_reports.append({"file": label, "parsed": False, "errors_app": 1, "errors_external": 0,
                                 "warnings": 0, "error_messages": [f"load error: {exc}"]})
            continue
        errors_app, errors_ext, warnings = 0, 0, 0
        app_msgs, ext_msgs = [], []
        for d in tu.diagnostics:
            if d.severity >= ci.Diagnostic.Error:
                loc = mapper.label(d.location.file.name) if d.location.file else "<unknown>"
                msg = f"{loc}:{d.location.line}: {d.spelling}"
                if loc in app_labels:
                    errors_app += 1
                    app_msgs.append(msg)
                else:
                    errors_ext += 1
                    ext_msgs.append(msg)
            elif d.severity == ci.Diagnostic.Warning:
                warnings += 1
        unit_reports.append({"file": label, "parsed": True, "errors_app": errors_app,
                             "errors_external": errors_ext, "warnings": warnings,
                             "error_messages": app_msgs[:25] + ext_msgs[:25]})
        # Preprocessing cursors first, so every expansion is known before the declarations that contain it.
        col.begin_unit(tu)
        children = list(tu.cursor.get_children())
        for c in children:
            if c.kind in _PREPROC:
                col.top_level(c)
        for c in children:
            if c.kind not in _PREPROC:
                col.top_level(c)
        # Which body identifiers are macros, and where: known only once every App body of this unit is recorded.
        wanted = {ident for ids in col.macro_bodies.values() for ident in ids}
        if wanted:
            for c in children:
                if c.kind == CK.MACRO_DEFINITION and c.spelling in wanted:
                    col.body_macro_defs.add((c.spelling, col.label_of(c)))

    functions = sorted(col.functions)
    macro_files: dict[str, set[str]] = {}
    for r in col.refs:
        if r[3] == "macro_use":
            macro_files.setdefault(r[4], set()).add(r[6])
    refs = []
    for site, line, func, kind, name, usr, decl_file, via, via_file in sorted(col.refs):
        if func == "<file-scope>" and kind == "macro_use":
            func = _enclosing_function(functions, site, line) or func
        if via and not via_file:
            # No instantiation recorded at the site (should not happen); fall back to the name, first file wins.
            via_file = sorted(macro_files.get(via, {""}))[0]
        refs.append({"site_file": site, "line": line, "in_function": func, "kind": kind,
                     "name": name, "usr": usr, "decl_file": decl_file,
                     "via_macro": via, "via_macro_file": via_file})
    refs.sort(key=lambda r: (r["site_file"], r["line"], r["in_function"], r["kind"], r["name"], r["usr"],
                             r["decl_file"], r["via_macro"]))

    return {
        "schema": "archcreator.ir/1",
        "units": sorted(unit_reports, key=lambda u: u["file"]),
        "declarations": [
            {"kind": k, "name": n, "usr": u, "file": f, "line": l, "is_definition": d, "linkage": lk}
            for k, n, u, f, l, d, lk in sorted(col.decls)
        ],
        "functions": [
            {"name": n, "usr": u, "file": f, "start_line": s, "end_line": e, "linkage": lk}
            for n, u, f, s, e, lk in functions
        ],
        "references": refs,
        "macro_bodies": [{"file": f, "name": n, "identifiers": list(ids)}
                         for (f, n), ids in sorted(col.macro_bodies.items())],
        "body_macro_definitions": [{"name": n, "file": f} for n, f in sorted(col.body_macro_defs)],
        "includes": [
            {"site_file": s, "line": l, "included_file": t, "spelling": sp}
            for s, l, t, sp in sorted(col.includes)
        ],
    }


def _enclosing_function(functions, site, line):
    best = None
    for name, _usr, f, start, end, _lk in functions:
        if f == site and start <= line <= end:
            if best is None or start > best[0]:
                best = (start, name)
    return best[1] if best else None


def libclang_version() -> str:
    # clang_getClangVersion is not bound in these Python bindings; calling it raw crashes.
    from importlib.metadata import version
    return version("libclang")
