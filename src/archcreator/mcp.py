"""MCP server (stdio) for archcreator: the deterministic analysis as tools for any MCP client.

    archcreator mcp --repo <target repository> [--profile P | --profiles-dir D] [--out-root DIR]

No SDK and no network: the protocol is newline-delimited JSON-RPC 2.0 on stdin/stdout, handled one request at a time.
The repository, the profile and the output root are fixed when the server starts. A model calling the tools can name
only modules and a few switches, never a path, a profile or a command, and it can read back only the fixed result files
of modules this session analyzed. archcreator itself uses no language model; the client's model only explains.
"""

import contextlib
import io
import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path
from typing import IO, Any

from . import __version__

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MODULE_NAME = re.compile(r"[A-Za-z0-9_]+")  # always used with fullmatch: "$" would accept a trailing newline
# Result files a client may read back, per kind of run.
READABLE = {
    "module": ("summary.json", "report.md", "findings.json", "model.json", "design_compare.json", "model.xmi"),
    "scan": ("scan_summary.json", "scan_report.md", "scan_findings.json", "graph.json"),
}
ALL_FILES = sorted({f for names in READABLE.values() for f in names})
MAX_FILE_BYTES = 200_000
MAX_LINE_BYTES = 8 * 1024 * 1024
GATE_ERROR_CODES = (2, 3, 4, 5)
OUTPUT_MARKER = ".archcreator-output"
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, NOT_INITIALIZED = -32700, -32600, -32601, -32602, -32002

INSTRUCTIONS = (
    "archcreator analyzes embedded C modules deterministically (no model): findings, the module's provided and required "
    "interfaces, signals and a UML-ready model. Work in this order: archcreator_list_modules, then archcreator_analyze "
    "for one module (add context to learn callers and signal producers) or archcreator_scan for several, then "
    "archcreator_read to open report.md or findings.json. Explain and prioritise the findings, but never invent, drop "
    "or re-rate one: the tool's result and exit code are the gate. Never edit source code, the profile or the baseline "
    "on your own; a correction belongs in the code or, by a human decision, in the profile."
)

_NAMES = {"type": "array", "items": {"type": "string", "pattern": "^[A-Za-z0-9_]+$"}, "minItems": 1}
TOOLS: list[dict[str, Any]] = [
    {
        "name": "archcreator_list_modules",
        "description": "List the modules (C files) in the configured scope and whether each is part of the build.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"title": "List modules", "readOnlyHint": True, "idempotentHint": True,
                        "openWorldHint": False},
    },
    {
        "name": "archcreator_analyze",
        "description": (
            "Analyze one module: parse it, build its interface and dependency model, evaluate the rules and write "
            "report.md, findings.json, model.json and a summary. Returns the summary (exit code, parse coverage, "
            "finding counts, top findings). Exit code 0 = ok, 1 = unaccepted findings at or above the gate."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "module": {"type": "string", "pattern": "^[A-Za-z0-9_]+$",
                           "description": "module name = C file name without .c"},
                "context": {"description": "in-build modules to parse alongside (callers, signal producers and "
                                           "consumers are known among these only), or the string \"all\"",
                            "oneOf": [_NAMES, {"const": "all"}]},
                "xmi": {"type": "boolean", "description": "also write model.xmi (XMI 2.1 for Enterprise Architect 16)"},
            },
            "required": ["module"], "additionalProperties": False,
        },
        "annotations": {"title": "Analyze a module", "readOnlyHint": False, "destructiveHint": False,
                        "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "archcreator_scan",
        "description": (
            "Analyze several modules from one parse and add the module graph and the rules over them (cycles, layers, "
            "unused exports). Name the modules, or pass all = true for every in-build module of the scope (slow on a "
            "large scope). Returns the scan summary; the modules of the scan can be read with archcreator_read."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "modules": _NAMES,
                "all": {"type": "boolean", "description": "every in-build module of the scope"},
                "xmi": {"type": "boolean", "description": "also write one model.xmi per module"},
            },
            "additionalProperties": False,
        },
        "annotations": {"title": "Scan modules", "readOnlyHint": False, "destructiveHint": False,
                        "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "archcreator_read",
        "description": (
            "Read one result file of a module this session analyzed (archcreator_analyze, or a module of "
            "archcreator_scan), or of the scan itself when module is omitted. Module files: "
            + ", ".join(READABLE["module"]) + ". Scan files: " + ", ".join(READABLE["scan"]) + "."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "module": {"type": "string", "pattern": "^[A-Za-z0-9_]+$",
                           "description": "module analyzed earlier; omit for the scan result"},
                "file": {"type": "string", "enum": ALL_FILES},
            },
            "required": ["file"], "additionalProperties": False,
        },
        "annotations": {"title": "Read a result file", "readOnlyHint": True, "idempotentHint": True,
                        "openWorldHint": False},
    },
]


class ToolError(Exception):
    """A failed tool call: reported to the client as a result with isError, not as a protocol error."""


class ProtocolError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _text_result(payload: Any, is_error: bool = False) -> dict:
    text = payload if isinstance(payload, str) else json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": is_error}
    if isinstance(payload, dict):
        result["structuredContent"] = payload
    return result


def _is_id(value: Any) -> bool:
    return isinstance(value, (str, int)) and not isinstance(value, bool)


class Server:
    """One archcreator MCP session for one repository."""

    def __init__(self, repo: Path, out_root: Path, profile: str | None = None,
                 profiles_dirs: list[str] | None = None, scopes: list[str] | None = None) -> None:
        self.repo = repo
        self.scopes = scopes or []
        self.out_root = out_root
        self.profile = profile
        self.profiles_dirs = profiles_dirs or []
        self.initialized = False
        self.version = PROTOCOL_VERSIONS[0]
        # module -> folder of its latest result in this session; "" stands for the scan itself.
        self.results: dict[str, Path] = {}

    # ---- protocol -------------------------------------------------------------------------------------------

    def handle(self, message: Any) -> dict | None:
        """The response to one JSON-RPC message, or None for a notification."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return self._error(None, INVALID_REQUEST, "invalid request")
        method, has_id = message.get("method"), "id" in message
        ident = message.get("id")
        if has_id and not _is_id(ident):
            return self._error(None, INVALID_REQUEST, "the id of a request must be a string or an integer")
        if not isinstance(method, str):
            return self._error(ident if has_id else None, INVALID_REQUEST, "invalid request: no method")
        if not has_id:
            return None  # a notification (notifications/initialized, notifications/cancelled, ...): nothing to answer
        params = message.get("params", {})
        try:
            if not isinstance(params, dict):
                raise ProtocolError(INVALID_PARAMS, "params must be an object")
            return self._result(ident, self._dispatch(method, params))
        except ProtocolError as exc:
            return self._error(ident, exc.code, str(exc))

    def _dispatch(self, method: str, params: dict) -> dict:
        if method == "ping":
            return {}
        if method == "initialize":
            if self.initialized:
                raise ProtocolError(INVALID_REQUEST, "already initialized")
            asked = params.get("protocolVersion")
            if not isinstance(asked, str):
                raise ProtocolError(INVALID_PARAMS, "protocolVersion is required")
            self.initialized = True
            self.version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return {"protocolVersion": self.version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "archcreator", "version": __version__},
                    "instructions": INSTRUCTIONS}
        if not self.initialized:
            raise ProtocolError(NOT_INITIALIZED, "server not initialized: send initialize first")
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            return self._call(params)
        raise ProtocolError(METHOD_NOT_FOUND, f"method not found: {method}")

    @staticmethod
    def _result(ident: Any, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": ident, "result": result}

    @staticmethod
    def _error(ident: Any, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}

    def _call(self, params: dict) -> dict:
        name, args = params.get("name"), params.get("arguments", {})
        if not isinstance(name, str) or name not in {t["name"] for t in TOOLS}:
            raise ProtocolError(INVALID_PARAMS, "unknown tool")
        if not isinstance(args, dict):
            raise ProtocolError(INVALID_PARAMS, "arguments must be an object")
        try:
            return getattr(self, "_tool_" + name[len("archcreator_"):])(args)
        except ToolError as exc:
            return _text_result({"error": str(exc)}, is_error=True)
        except Exception as exc:  # noqa: BLE001 - a bug in a tool must not end the session
            print(f"archcreator mcp: {type(exc).__name__}: {exc}", file=sys.stderr)
            return _text_result({"error": f"internal error: {type(exc).__name__}: {exc}"}, is_error=True)

    # ---- helpers --------------------------------------------------------------------------------------------

    def _base(self) -> list[str]:
        argv = ["--repo", str(self.repo)]
        if self.profile:
            argv += ["--profile", self.profile]
        for d in self.profiles_dirs:
            argv += ["--profiles-dir", d]
        for s in self.scopes:
            argv += ["--scope", s]
        return argv

    @staticmethod
    def _run(argv: list[str]) -> tuple[int, str]:
        """The CLI in this process; nothing of it may reach the protocol stream, so stdout is captured."""
        from .cli import main
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        if err.getvalue():
            print(err.getvalue(), end="", file=sys.stderr)
        return code, out.getvalue()

    @staticmethod
    def _check_args(args: dict, allowed: set[str]) -> None:
        extra = sorted(set(args) - allowed)
        if extra:
            raise ToolError(f"unknown argument(s): {', '.join(extra)}")

    @staticmethod
    def _flag(args: dict, key: str) -> bool:
        value = args.get(key, False)
        if not isinstance(value, bool):
            raise ToolError(f"{key} must be true or false")
        return value

    @staticmethod
    def _name(value: Any, what: str = "module") -> str:
        if not isinstance(value, str) or not MODULE_NAME.fullmatch(value):
            raise ToolError(f"{what} must be a name of letters, digits and _")
        return value

    def _names(self, value: Any, what: str) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ToolError(f"{what} must be a non-empty list of module names")
        names = [self._name(v, f"{what} entries") for v in value]
        if "all" in names:
            raise ToolError(f"'all' is reserved in {what}: use the all switch (or context \"all\") instead")
        return names

    def _folder(self, *parts: str) -> Path:
        """A result folder below the output root, refused if it leads anywhere else (symlink, junction)."""
        root = self.out_root.resolve()
        folder = self.out_root.joinpath(*parts)
        if not folder.resolve().is_relative_to(root):
            raise ToolError("the output folder lies outside the output root")
        return folder

    @staticmethod
    def _marker_text(parts: tuple[str, ...]) -> str:
        return "archcreator mcp result folder: " + "/".join(parts)

    def _marked(self, marker: Path, parts: tuple[str, ...]) -> bool:
        """The marker of exactly this result folder (a copied or planted marker of another folder does not count)."""
        try:
            return marker.is_file() and not marker.is_symlink() and                 marker.read_text(encoding="ascii") == self._marker_text(parts)
        except (OSError, ValueError):
            return False

    def _fresh(self, *parts: str) -> Path:
        """An empty result folder below the output root for a new run.

        An earlier result is deleted as a whole (stale files, planted links, special files go with it), but only a
        folder that archcreator itself marked, or an empty one: --out-root may sit in a folder with other content.
        """
        folder = self._folder(*parts)
        if folder.exists():
            marker = folder / OUTPUT_MARKER
            if not self._marked(marker, parts) and any(folder.iterdir()):
                raise ToolError(f"{folder.name} below the output root is not an archcreator result folder; "
                                "choose another --out-root")
            shutil.rmtree(folder)
        folder.mkdir(parents=True)
        (folder / OUTPUT_MARKER).write_text(self._marker_text(parts), encoding="ascii")
        return self._folder(*parts)

    # ---- tools ----------------------------------------------------------------------------------------------

    def _tool_list_modules(self, args: dict) -> dict:
        self._check_args(args, set())
        code, text = self._run(["list-modules", *self._base(), "--json"])
        if code != 0:
            raise ToolError("could not list the modules (see the server log for the message)")
        return _text_result(json.loads(text))

    def _finish(self, code: int, folder: Path, summary_name: str) -> dict:
        try:
            summary = json.loads((folder / summary_name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ToolError(f"the run ended with exit code {code} and wrote no readable {summary_name}") from None
        kind = "scan" if summary_name == "scan_summary.json" else "module"
        summary = {**summary, "output_folder": str(folder),
                   "readable_files": [f for f in READABLE[kind] if (folder / f).is_file()]}
        return _text_result(summary, is_error=code in GATE_ERROR_CODES)

    def _tool_analyze(self, args: dict) -> dict:
        self._check_args(args, {"module", "context", "xmi"})
        module = self._name(args.get("module"))
        context = args.get("context")
        context_text = None if context is None else "all" if context == "all" else ",".join(self._names(context, "context"))
        xmi = self._flag(args, "xmi")
        folder = self._fresh("modules", module)
        argv = ["analyze", *self._base(), "--module", module, "--out", str(folder)]
        if context_text is not None:
            argv += ["--context", context_text]
        if xmi:
            argv.append("--xmi")
        code, _ = self._run(argv)
        self.results[module] = folder
        return self._finish(code, folder, "summary.json")

    def _tool_scan(self, args: dict) -> dict:
        self._check_args(args, {"modules", "all", "xmi"})
        every, modules = self._flag(args, "all"), args.get("modules")
        if every == (modules is not None):
            raise ToolError("pass either modules (a list of names) or all = true")
        names = None if every else self._names(modules, "modules")
        xmi = self._flag(args, "xmi")
        scan_folder = self._folder("scan")
        self.results = {k: v for k, v in self.results.items() if k and not v.is_relative_to(scan_folder)}
        folder = self._fresh("scan")
        argv = ["scan", *self._base(), "--out", str(folder)]
        argv += ["--all"] if names is None else ["--modules", ",".join(names)]
        if xmi:
            argv.append("--xmi")
        code, _ = self._run(argv)
        self.results[""] = folder
        result = self._finish(code, folder, "scan_summary.json")
        for entry in (result.get("structuredContent") or {}).get("modules", []):
            if isinstance(entry.get("name"), str) and MODULE_NAME.fullmatch(entry["name"]):
                self.results[entry["name"]] = folder / entry["name"]
        return result

    def _tool_read(self, args: dict) -> dict:
        self._check_args(args, {"module", "file"})
        module, name = args.get("module"), args.get("file")
        kind = "scan" if module is None else "module"
        if module is not None:
            self._name(module)
        if name not in READABLE[kind]:
            raise ToolError(f"file must be one of: {', '.join(READABLE[kind])}")
        folder = self.results.get(module or "")
        if folder is None:
            raise ToolError(f"{module or 'the scan'} has not been analyzed in this session: run "
                            f"{'archcreator_analyze' if module else 'archcreator_scan'} first")
        return _text_result(self._read_file(folder, name))

    def _read_file(self, folder: Path, name: str) -> str:
        root = self.out_root.resolve()
        path = folder / name
        try:
            real = path.resolve(strict=True)
            if not real.is_relative_to(root) or not stat.S_ISREG(os.stat(real).st_mode):
                raise ToolError(f"{name} is not a regular result file below the output root")
            with open(real, "rb") as handle:
                data = handle.read(MAX_FILE_BYTES + 1)
        except OSError:
            raise ToolError(f"{name} does not exist in the latest result of this session") from None
        text = data[:MAX_FILE_BYTES].decode("utf-8", errors="replace")
        if len(data) > MAX_FILE_BYTES:
            text += f"\n[truncated at {MAX_FILE_BYTES} bytes; the file is on disk: {real}]"
        return text


def _reject_constant(name: str) -> None:
    raise ValueError(f"not JSON: {name}")


def _write(sink: IO[bytes], response: dict) -> None:
    # ASCII-only JSON: escaped surrogates or odd characters in an id or a name can never break the encoding.
    sink.write(json.dumps(response, separators=(",", ":")).encode("ascii") + b"\n")
    sink.flush()


def _lines(source: IO[bytes]):
    """Lines of the input; a line over the limit is skipped to its end and reported as None."""
    while True:
        raw = source.readline(MAX_LINE_BYTES + 1)
        if not raw:
            return
        if len(raw) > MAX_LINE_BYTES and not raw.endswith(b"\n"):
            while (rest := source.readline(MAX_LINE_BYTES)) and not rest.endswith(b"\n"):
                pass
            yield None
            continue
        yield raw


def _batch(server: Server, batch: list) -> Any:
    """A JSON-RPC batch: required by the 2025-03-26 and 2024-11-05 revisions, removed in 2025-06-18."""
    if server.version == PROTOCOL_VERSIONS[0] or not batch:
        return Server._error(None, INVALID_REQUEST, "batches are not supported" if batch else "empty batch")
    replies = [r for r in (_single(server, m) for m in batch) if r is not None]
    return replies or None


def _single(server: Server, message: Any) -> dict | None:
    try:
        return server.handle(message)
    except Exception as exc:  # noqa: BLE001 - one bad message must not end the session
        print(f"archcreator mcp: {type(exc).__name__}: {exc}", file=sys.stderr)
        return Server._error(None, -32603, "internal error")


def serve(server: Server, source: IO[bytes], sink: IO[bytes]) -> None:
    """Handle newline-delimited JSON-RPC messages until the input ends."""
    for raw in _lines(source):
        if raw is None:
            _write(sink, Server._error(None, INVALID_REQUEST, "request too large"))
            continue
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line.decode("utf-8"), parse_constant=_reject_constant)
        except (ValueError, UnicodeDecodeError, RecursionError):
            _write(sink, Server._error(None, PARSE_ERROR, "parse error"))
            continue
        try:
            response = _batch(server, message) if isinstance(message, list) else server.handle(message)
        except Exception as exc:  # noqa: BLE001 - one bad message must not end the session
            print(f"archcreator mcp: {type(exc).__name__}: {exc}", file=sys.stderr)
            ident = message.get("id") if isinstance(message, dict) and _is_id(message.get("id")) else None
            response = Server._error(ident, -32603, "internal error")
        if response is not None:
            _write(sink, response)


def run_stdio(repo: Path, out_root: Path, profile: str | None, profiles_dirs: list[str] | None,
              scopes: list[str] | None = None) -> int:
    server = Server(repo.absolute(), out_root.absolute(), profile, profiles_dirs, scopes)
    print(f"archcreator mcp: serving {server.repo}, results under {server.out_root}", file=sys.stderr)
    serve(server, sys.stdin.buffer, sys.stdout.buffer)
    return 0
