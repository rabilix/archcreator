"""The MCP server: protocol behaviour, tool results, and the limits that keep a model inside the configured scope."""

import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from archcreator import mcp
from archcreator.mcp import PROTOCOL_VERSIONS, TOOLS, Server, serve

FIXTURE = Path(__file__).parent / "fixtures" / "fx01"
PROFILE = str(FIXTURE / "profile.yaml")


def _rpc(server, method, params=None, ident=1):
    message = {"jsonrpc": "2.0", "id": ident, "method": method}
    if params is not None:
        message["params"] = params
    return server.handle(message)


@pytest.fixture
def raw_server(tmp_path):
    return Server(FIXTURE, tmp_path / "out", profile=PROFILE)


@pytest.fixture
def server(raw_server):
    assert "result" in _rpc(raw_server, "initialize", {"protocolVersion": "2025-06-18"})
    raw_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return raw_server


def _call(server, name, arguments=None):
    response = _rpc(server, "tools/call", {"name": name, "arguments": {} if arguments is None else arguments})
    return response["result"]


def _payload(result):
    return json.loads(result["content"][0]["text"])


def _text(result):
    return result["content"][0]["text"]


# ---- protocol ---------------------------------------------------------------------------------------------------

def test_initialize_negotiates_the_version_and_declares_tools(raw_server):
    result = _rpc(raw_server, "initialize", {"protocolVersion": "2025-03-26"})["result"]
    assert result["protocolVersion"] == "2025-03-26"
    assert result["serverInfo"]["name"] == "archcreator" and "tools" in result["capabilities"]
    assert result["instructions"]


def test_unknown_protocol_version_gets_the_latest_one(raw_server):
    assert _rpc(raw_server, "initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"] == \
        PROTOCOL_VERSIONS[0]


def test_lifecycle_is_enforced(raw_server):
    assert _rpc(raw_server, "ping")["result"] == {}
    for method, params in (("tools/list", None), ("tools/call", {"name": "archcreator_list_modules"})):
        assert _rpc(raw_server, method, params)["error"]["code"] == -32002
    assert _rpc(raw_server, "initialize", {})["error"]["code"] == -32602          # protocolVersion is required
    assert _rpc(raw_server, "initialize", {"protocolVersion": "2025-06-18"})["result"]
    assert _rpc(raw_server, "initialize", {"protocolVersion": "2025-06-18"})["error"]["code"] == -32600  # only once
    assert _rpc(raw_server, "tools/list")["result"]["tools"]


def test_notifications_get_no_response(server):
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}}) is None


@pytest.mark.parametrize("message, code", [
    ({"jsonrpc": "2.0"}, -32600),                                               # neither request nor notification
    ({"jsonrpc": "2.0", "id": 1}, -32600),                                      # no method
    ({"jsonrpc": "2.0", "id": None, "method": "ping"}, -32600),                 # id must be a string or an integer
    ({"jsonrpc": "2.0", "id": True, "method": "ping"}, -32600),
    ({"jsonrpc": "2.0", "id": [1], "method": "ping"}, -32600),
    ({"jsonrpc": "1.0", "id": 1, "method": "ping"}, -32600),
    ({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": []}, -32602),      # params: an object or nothing
    ({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": False}, -32602),
    ({"jsonrpc": "2.0", "id": 1, "method": "no/such/method"}, -32601),
])
def test_invalid_envelopes(server, message, code):
    assert server.handle(message)["error"]["code"] == code


@pytest.mark.parametrize("params", [
    {"name": "rm_rf", "arguments": {}}, {"name": []}, {"name": {}}, {"name": None}, {},
    {"name": "archcreator_list_modules", "arguments": []},                      # arguments: an object or nothing
    {"name": "archcreator_list_modules", "arguments": "x"},
    {"name": "archcreator_analyze", "arguments": 0},
])
def test_bad_tool_calls_are_protocol_errors_and_the_session_survives(server, params):
    assert _rpc(server, "tools/call", params)["error"]["code"] == -32602
    assert _rpc(server, "ping")["result"] == {}


def test_malformed_input_gets_protocol_errors_and_never_ends_the_session(server):
    deep = b"[" * 20000 + b"]" * 20000
    surrogate = b'{"jsonrpc":"2.0","id":"\\ud800","method":"ping"}'
    unknown = b'{"jsonrpc":"2.0","id":2,"method":"\\ud800"}'
    stream = b"not json\n" + b"\xff\xfe\n" + deep + b"\n[1]\n\n{\"id\":3}\n" + surrogate + b"\n" + unknown + \
        b'\n{"jsonrpc":"2.0","id":9,"method":"ping"}\n'
    sink = io.BytesIO()
    serve(server, io.BytesIO(stream), sink)
    replies = [json.loads(line) for line in sink.getvalue().splitlines()]
    assert [r.get("error", {}).get("code") for r in replies[:5]] == [-32700, -32700, -32700, -32600, -32600]
    assert replies[5]["id"] == "\ud800" and replies[6]["error"]["code"] == -32601
    assert replies[-1] == {"jsonrpc": "2.0", "id": 9, "result": {}}   # still serving after all of that
    assert sink.getvalue().isascii()


def test_an_oversized_line_is_refused_and_skipped(server, monkeypatch):
    monkeypatch.setattr(mcp, "MAX_LINE_BYTES", 64)
    big = b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"pad":"' + b"x" * 500 + b'"}}\n'
    sink = io.BytesIO()
    serve(server, io.BytesIO(big + b'{"jsonrpc":"2.0","id":2,"method":"ping"}\n'), sink)
    replies = [json.loads(line) for line in sink.getvalue().splitlines()]
    assert replies[0]["error"]["code"] == -32600 and replies[1] == {"jsonrpc": "2.0", "id": 2, "result": {}}


def test_tool_list_is_closed_and_described(server):
    tools = _rpc(server, "tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == ["archcreator_list_modules", "archcreator_analyze", "archcreator_scan",
                                          "archcreator_read"]
    assert all(t["inputSchema"]["additionalProperties"] is False and t["description"] for t in TOOLS)
    # None of the tools can be given a path, a profile or a command.
    properties = {p for t in TOOLS for p in t["inputSchema"]["properties"]}
    assert properties == {"module", "context", "xmi", "modules", "all", "file"}


# ---- tools ------------------------------------------------------------------------------------------------------

def test_list_modules(server):
    data = _payload(_call(server, "archcreator_list_modules"))
    names = [m["name"] for scope in data["scopes"] for m in scope["modules"]]
    assert "alpha" in names and "beta" in names


def test_analyze_writes_below_the_out_root_and_read_returns_the_files(server, tmp_path):
    result = _call(server, "archcreator_analyze", {"module": "alpha", "context": "all", "xmi": True})
    summary = _payload(result)
    assert result["isError"] is False and summary["exit_code"] == 0 and summary["module"] == "alpha"
    assert summary["output_folder"] == str(tmp_path / "out" / "modules" / "alpha")
    assert {"summary.json", "report.md", "findings.json", "model.json", "model.xmi"} <= set(summary["readable_files"])
    report = _call(server, "archcreator_read", {"module": "alpha", "file": "report.md"})
    assert report["isError"] is False and "alpha" in _text(report)
    assert _text(_call(server, "archcreator_read", {"module": "alpha", "file": "model.xmi"})).lstrip().startswith("<?xml")


def test_context_can_be_a_list(server):
    summary = _payload(_call(server, "archcreator_analyze", {"module": "alpha", "context": ["beta"]}))
    assert summary["exit_code"] == 0 and "alpha" in summary["analyzed_modules"]


@pytest.mark.parametrize("arguments", [
    {"module": "../x"}, {"module": "a/b"}, {"module": ""}, {"module": 7}, {},
    {"module": "alpha", "profile": "evil.yaml"}, {"module": "alpha", "out": "/tmp/x"},
    {"module": "alpha", "context": "alpha; rm -rf"}, {"module": "alpha", "context": []},
    {"module": "alpha", "context": [3]}, {"module": "alpha", "xmi": "true"}, {"module": "alpha", "xmi": 1},
])
def test_analyze_rejects_anything_but_names_and_switches(server, tmp_path, arguments):
    result = _call(server, "archcreator_analyze", arguments)
    assert result["isError"] is True and "error" in _payload(result)
    # Rejected before anything ran: no output folder was created.
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").iterdir())


def test_read_is_limited_to_known_files_of_modules_analyzed_in_this_session(server, tmp_path):
    for arguments in ({"module": "alpha", "file": "report.md"},               # not analyzed yet
                      {"module": "alpha", "file": "../../profile.yaml"},
                      {"module": "alpha", "file": "ir.json"},                   # exists later but is not offered
                      {"module": "..", "file": "report.md"},
                      {"file": "model.json"},                                   # the scan has other files
                      {"file": "scan_summary.json"}):                           # no scan yet
        assert _call(server, "archcreator_read", arguments)["isError"] is True
    _call(server, "archcreator_analyze", {"module": "alpha"})
    assert (tmp_path / "out" / "modules" / "alpha" / "ir.json").is_file()
    assert _call(server, "archcreator_read", {"module": "alpha", "file": "ir.json"})["isError"] is True
    # A second session over the same output root has not analyzed anything, so it reads nothing.
    other = Server(FIXTURE, tmp_path / "out", profile=PROFILE)
    _rpc(other, "initialize", {"protocolVersion": "2025-06-18"})
    assert _call(other, "archcreator_read", {"module": "alpha", "file": "report.md"})["isError"] is True


def test_a_rerun_offers_only_files_of_that_run(server):
    first = _payload(_call(server, "archcreator_analyze", {"module": "alpha", "xmi": True}))
    assert "model.xmi" in first["readable_files"]
    second = _payload(_call(server, "archcreator_analyze", {"module": "alpha"}))
    assert "model.xmi" not in second["readable_files"]
    assert _call(server, "archcreator_read", {"module": "alpha", "file": "model.xmi"})["isError"] is True


def test_scan_needs_exactly_one_of_modules_or_all(server):
    for arguments in ({}, {"all": True, "modules": ["alpha"]}, {"modules": ["alpha;x"]}, {"all": "yes"},
                      {"modules": "alpha"}):
        assert _call(server, "archcreator_scan", arguments)["isError"] is True


def test_scan_results_and_its_modules_are_readable(server):
    result = _call(server, "archcreator_scan", {"modules": ["alpha", "beta"]})
    summary = _payload(result)
    assert result["isError"] is False and {"graph.json", "scan_report.md", "scan_findings.json",
                                           "scan_summary.json"} <= set(summary["readable_files"])
    assert json.loads(_text(_call(server, "archcreator_read", {"file": "graph.json"})))["schema"].endswith("graph/1")
    assert "# " in _text(_call(server, "archcreator_read", {"file": "scan_report.md"}))
    assert _call(server, "archcreator_read", {"file": "scan_findings.json"})["isError"] is False
    assert "beta" in _text(_call(server, "archcreator_read", {"module": "beta", "file": "report.md"}))


def test_parse_gate_failure_is_reported_as_an_error_result(tmp_path):
    repo = tmp_path / "fx01"
    shutil.copytree(FIXTURE, repo)
    beta = repo / "src" / "App" / "beta.c"
    beta.write_text(beta.read_text(encoding="utf-8") + "\nthis is not C\n", encoding="utf-8")
    server = Server(repo, tmp_path / "out", profile=str(repo / "profile.yaml"))
    _rpc(server, "initialize", {"protocolVersion": "2025-06-18"})
    result = _call(server, "archcreator_analyze", {"module": "alpha", "context": "all"})
    summary = _payload(result)
    assert result["isError"] is True and summary["exit_code"] == 3 and "parse coverage" in summary["error"]


def test_a_bad_repository_is_an_error_result_not_a_crash(tmp_path):
    server = Server(tmp_path / "missing", tmp_path / "out")
    _rpc(server, "initialize", {"protocolVersion": "2025-06-18"})
    assert _call(server, "archcreator_list_modules")["isError"] is True
    assert _call(server, "archcreator_analyze", {"module": "alpha"})["isError"] is True


def test_a_result_file_is_read_bounded_and_truncated(server, monkeypatch):
    _call(server, "archcreator_analyze", {"module": "alpha"})
    monkeypatch.setattr(mcp, "MAX_FILE_BYTES", 100)
    text = _text(_call(server, "archcreator_read", {"module": "alpha", "file": "report.md"}))
    assert "[truncated at 100 bytes" in text and len(text) < 400


def _make_link(link: Path, target: Path) -> bool:
    """A directory junction on Windows, a symlink elsewhere; False when the machine does not allow either."""
    try:
        if sys.platform == "win32":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
        else:
            link.symlink_to(target, target_is_directory=True)
        return link.exists()
    except (OSError, subprocess.CalledProcessError):
        return False


def test_an_output_folder_that_leads_outside_the_root_is_never_written_or_read(server, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "report.md").write_text("secret", encoding="utf-8")
    (tmp_path / "out" / "modules").mkdir(parents=True)
    if not _make_link(tmp_path / "out" / "modules" / "alpha", outside):
        pytest.skip("no junction or symlink available")
    result = _call(server, "archcreator_analyze", {"module": "alpha"})
    assert result["isError"] is True and "outside the output root" in _payload(result)["error"]
    assert sorted(p.name for p in outside.iterdir()) == ["report.md"] and (outside / "report.md").read_text() == "secret"
    assert _call(server, "archcreator_read", {"module": "alpha", "file": "report.md"})["isError"] is True


# ---- the real process -------------------------------------------------------------------------------------------

def test_stdio_process_keeps_stdout_for_the_protocol_only(tmp_path):
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "archcreator_analyze", "arguments": {"module": "alpha", "context": "all"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "archcreator_analyze", "arguments": {"module": "../x"}}},
    ]
    proc = subprocess.run(
        [sys.executable, "-m", "archcreator.cli", "mcp", "--repo", str(FIXTURE), "--profile", PROFILE,
         "--out-root", str(tmp_path / "out")],
        input="".join(json.dumps(r) + "\n" for r in requests).encode("utf-8"), capture_output=True, timeout=120)
    assert proc.returncode == 0
    lines = proc.stdout.decode("utf-8").splitlines()
    assert [json.loads(line)["id"] for line in lines] == [1, 2, 3]   # every stdout line is a response
    assert b"serving" in proc.stderr                                  # the log goes to stderr
    assert (tmp_path / "out" / "modules" / "alpha" / "summary.json").is_file()


def test_a_link_planted_inside_the_output_root_cannot_redirect_a_scan(server, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    first = _call(server, "archcreator_scan", {"modules": ["alpha"]})
    assert first["isError"] is False
    scan = tmp_path / "out" / "scan"
    shutil.rmtree(scan / "alpha")
    if not _make_link(scan / "alpha", outside):
        pytest.skip("no junction or symlink available")
    again = _call(server, "archcreator_scan", {"modules": ["alpha"]})
    assert again["isError"] is False
    assert sorted(p.name for p in outside.iterdir()) == ["keep.txt"]      # nothing written through the link
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"   # and nothing deleted through it
    assert (scan / "alpha" / "report.md").is_file() and not (scan / "alpha").is_symlink()


def test_a_folder_that_is_not_archcreator_output_is_never_deleted(server, tmp_path):
    foreign = tmp_path / "out" / "modules" / "alpha"
    foreign.mkdir(parents=True)
    (foreign / "mine.txt").write_text("not yours", encoding="utf-8")
    result = _call(server, "archcreator_analyze", {"module": "alpha"})
    assert result["isError"] is True and "not an archcreator result folder" in _payload(result)["error"]
    assert (foreign / "mine.txt").read_text(encoding="utf-8") == "not yours"
    (foreign / "mine.txt").unlink()                                          # an empty folder is fine
    assert _call(server, "archcreator_analyze", {"module": "alpha"})["isError"] is False
    assert _call(server, "archcreator_analyze", {"module": "alpha"})["isError"] is False   # and so is a marked one


def test_a_rerun_removes_leftovers_of_the_earlier_run(server, tmp_path):
    _call(server, "archcreator_analyze", {"module": "alpha"})
    leftover = tmp_path / "out" / "modules" / "alpha" / "ir.json"
    assert leftover.is_file()
    (tmp_path / "out" / "modules" / "alpha" / "stray.txt").write_text("x", encoding="utf-8")
    _call(server, "archcreator_analyze", {"module": "alpha"})
    assert not (tmp_path / "out" / "modules" / "alpha" / "stray.txt").exists()


def test_batches_only_where_the_negotiated_version_requires_them(raw_server):
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    _rpc(raw_server, "initialize", {"protocolVersion": "2025-03-26"})
    sink = io.BytesIO()
    serve(raw_server, io.BytesIO(json.dumps([ping, {**ping, "id": 2}, note]).encode() + b"\n[]\n" +
                                 json.dumps([note]).encode() + b"\n"), sink)
    replies = [json.loads(line) for line in sink.getvalue().splitlines()]
    assert replies[0] == [{"jsonrpc": "2.0", "id": 1, "result": {}}, {"jsonrpc": "2.0", "id": 2, "result": {}}]
    assert replies[1]["error"]["code"] == -32600 and len(replies) == 2   # empty batch invalid; all-notifications: silent


def test_batches_are_refused_under_the_latest_version(server):
    sink = io.BytesIO()
    serve(server, io.BytesIO(b'[{"jsonrpc":"2.0","id":1,"method":"ping"}]\n'), sink)
    assert json.loads(sink.getvalue())["error"]["code"] == -32600


def test_a_module_named_scan_and_the_scan_do_not_share_a_folder(server):
    assert _call(server, "archcreator_scan", {"modules": ["alpha"]})["isError"] is False
    assert _call(server, "archcreator_analyze", {"module": "scan"})["isError"] is True      # no such module
    assert _call(server, "archcreator_read", {"file": "scan_summary.json"})["isError"] is False


def test_a_narrower_scan_forgets_the_modules_it_replaced(server):
    _call(server, "archcreator_scan", {"modules": ["alpha", "beta"]})
    assert _call(server, "archcreator_read", {"module": "alpha", "file": "report.md"})["isError"] is False
    _call(server, "archcreator_scan", {"modules": ["beta"]})
    gone = _call(server, "archcreator_read", {"module": "alpha", "file": "report.md"})
    assert gone["isError"] is True and "has not been analyzed in this session" in _payload(gone)["error"]
    assert _call(server, "archcreator_read", {"module": "beta", "file": "report.md"})["isError"] is False


def test_all_is_reserved_in_name_lists(server):
    assert _call(server, "archcreator_scan", {"modules": ["all"]})["isError"] is True
    assert _call(server, "archcreator_analyze", {"module": "alpha", "context": ["all"]})["isError"] is True
    assert _call(server, "archcreator_analyze", {"module": "alpha", "context": "all"})["isError"] is False


def test_a_marker_of_another_folder_does_not_authorize_deletion(server, tmp_path):
    foreign = tmp_path / "out" / "modules" / "alpha"
    foreign.mkdir(parents=True)
    (foreign / mcp.OUTPUT_MARKER).write_text("archcreator mcp result folder: modules/beta", encoding="ascii")
    (foreign / "mine.txt").write_text("not yours", encoding="utf-8")
    assert _call(server, "archcreator_analyze", {"module": "alpha"})["isError"] is True
    assert (foreign / "mine.txt").is_file()


def test_a_trailing_newline_does_not_get_past_the_name_checks(server):
    assert _call(server, "archcreator_scan", {"modules": ["all\n"]})["isError"] is True
    assert _call(server, "archcreator_analyze", {"module": "alpha", "context": ["all\n"]})["isError"] is True
    assert _call(server, "archcreator_analyze", {"module": "alpha\n"})["isError"] is True


def test_json_constants_that_are_not_json_are_a_parse_error(server):
    sink = io.BytesIO()
    serve(server, io.BytesIO(b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":NaN}}\n'), sink)
    assert json.loads(sink.getvalue())["error"]["code"] == -32700


def test_native_writes_to_stdout_never_reach_the_protocol_stream(tmp_path):
    script = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "from archcreator import mcp\n"
        "def noisy(self, args):\n"
        "    os.write(1, b'NATIVE NOISE ')" + chr(10) +    # what a C library writing to stdout would do
        "    print('PYTHON NOISE')\n"
        "    return mcp._text_result({'ok': True})\n"
        "mcp.Server._tool_list_modules = noisy\n"
        f"mcp.run_stdio(Path({str(FIXTURE)!r}), Path({str(tmp_path / 'out')!r}), {PROFILE!r}, None)\n")
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "archcreator_list_modules"}},
    ]
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=60,
                          input="".join(json.dumps(r) + "\n" for r in requests).encode("utf-8"))
    assert proc.returncode == 0, proc.stderr
    assert [json.loads(line)["id"] for line in proc.stdout.decode("utf-8").splitlines()] == [1, 2]
    assert b"NATIVE NOISE" in proc.stderr and b"PYTHON NOISE" in proc.stderr


def test_a_rerun_with_the_module_spelled_in_other_case_reuses_the_folder(server, tmp_path):
    assert _call(server, "archcreator_analyze", {"module": "alpha"})["isError"] is False
    again = _call(server, "archcreator_analyze", {"module": "ALPHA"})
    assert "not an archcreator result folder" not in json.dumps(_payload(again))
    # A marker of another module still does not count, whatever its case.
    other = tmp_path / "out" / "modules" / "gamma"
    other.mkdir(parents=True)
    (other / mcp.OUTPUT_MARKER).write_text("ARCHCREATOR MCP RESULT FOLDER: MODULES/BETA", encoding="ascii")
    (other / "mine.txt").write_text("x", encoding="utf-8")
    assert "not an archcreator result folder" in _payload(_call(server, "archcreator_analyze", {"module": "gamma"}))["error"]


def test_where_case_tells_folders_apart_another_spellings_marker_does_not_count(server, tmp_path, monkeypatch):
    # On a case-sensitive file system modules/ALPHA and modules/alpha are two folders; samefile says so.
    folder = tmp_path / "out" / "modules" / "ALPHA"
    folder.mkdir(parents=True)
    (folder / mcp.OUTPUT_MARKER).write_text("archcreator mcp result folder: modules/alpha", encoding="ascii")
    (folder / "mine.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(mcp.os.path, "samefile", lambda a, b: False)
    result = _call(server, "archcreator_analyze", {"module": "ALPHA"})
    assert "not an archcreator result folder" in _payload(result)["error"]
    assert (folder / "mine.txt").is_file()
