"""Live check of the EA 16 XMI export against a real Enterprise Architect (what the offline tests cannot prove).

The XMI is imported into a scratch COPY of EA's own empty model (EABase.qea) through EA's automation interface, and the
resulting .qea (SQLite) is read back and compared with what the XMI promises. Nothing else is ever opened or written:
the model, the XMI and every report live in one run folder, and the script refuses a model outside it.

    python tools/ea_verify.py check                 EA starts, answers over COM, opens the scratch copy (no import)
    python tools/ea_verify.py import --go           check + import SEV and DCDC_FSM, import SEV again, compare
    python tools/ea_verify.py all --go              same as import
    python tools/ea_verify.py import --go --dry-run build everything, start no EA

Steps in the report: E0 install, E1 COM start, E2 open scratch copy, X1 XMI generation, I1 first import, V1..V6 compare,
I2 import again (same ids replace, not duplicate), I3 second module next to the first (no shared element), V7 final.
Each step says PASS / FAIL / INFO / SKIP / ERROR; a failing step does not stop the next.

Needs the Enterprise network only if EA checks its licence there: when EA waits for something, E1 times out (--timeout),
the EA processes this run started are killed, and the report says so.
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

EA_HOME = Path(r"C:\Program Files\Sparx Systems\EA")
BASE_MODEL = EA_HOME / "EABase.qea"
DEFAULT_REPO = Path(r"F:\Projekte\100554_BMW_PCU48\100554_mc_appl")
DEFAULT_MODULES = ("SEV", "DCDC_FSM")
TARGET_PACKAGE = "ArchCreatorImportTest"
XMI_NS = "http://schema.omg.org/spec/XMI/2.1"
NS = {"xmi": XMI_NS}
RUN_ROOT = Path(os.environ.get("TEMP", ".")) / "archcreator_ea_verify"

DRIVER = r'''
# Written by tools/ea_verify.py. Opens ONLY the model named in the job file, imports, closes. One JSON line per step.
param($Job)
$ErrorActionPreference = "Stop"
$j = Get-Content -Raw -Encoding UTF8 $Job | ConvertFrom-Json
function Say($step, $ok, $detail) { (@{step=$step; ok=$ok; detail="$detail"} | ConvertTo-Json -Compress) }
try {
    $ea = New-Object -ComObject EA.App
    Say "E1" $true "EA.App created, version $($ea.Version)"
    $new = @(Get-Process EA -ErrorAction SilentlyContinue | Where-Object { $j.before -notcontains $_.Id } | ForEach-Object { $_.Id })
    (@{step="PIDS"; ok=$true; detail=($new -join ",")} | ConvertTo-Json -Compress)
} catch { Say "E1" $false $_.Exception.Message; exit 0 }
$r = $ea.Repository
try {
    if (-not $r.OpenFile($j.model)) { Say "E2" $false "OpenFile returned false"; $r.Exit(); exit 0 }
    Say "E2" $true "opened $($j.model)"
} catch { Say "E2" $false $_.Exception.Message; try { $r.Exit() } catch {}; exit 0 }
try {
    $root = $r.Models.GetAt(0)
    $pk = $null
    foreach ($p in $root.Packages) { if ($p.Name -eq $j.package) { $pk = $p } }
    if (-not $pk) { $pk = $root.Packages.AddNew($j.package, ""); $pk.Update(); $root.Packages.Refresh() }
    $proj = $r.GetProjectInterface()
    foreach ($imp in $j.imports) {
        if ($imp.replace) {
            for ($i = $pk.Packages.Count - 1; $i -ge 0; $i--) {
                if ($pk.Packages.GetAt($i).Name -eq $imp.replace) { $pk.Packages.DeleteAt($i, $false) }
            }
            $pk.Packages.Refresh()
        }
        $res = $proj.ImportPackageXMI($pk.PackageGUID, $imp.xmi, 1, 0)
        $log = ""; try { $log = $r.GetLastError() } catch {}
        Say $imp.step ([string]::IsNullOrEmpty("$res") -or "$res" -match "^\{[0-9A-Fa-f-]{36}\}$") "ImportPackageXMI($($imp.xmi)) -> [$res] $log"
    }
} catch { Say "I" $false $_.Exception.Message }
try { $r.CloseFile() } catch {}
try { $r.Exit() } catch {}
'''


class Report:
    def __init__(self, folder: Path):
        self.folder, self.rows = folder, []

    def add(self, step: str, status: str, text: str) -> None:
        self.rows.append({"step": step, "status": status, "text": text})
        print(f"{step:<4} {status:<6} {text}", flush=True)

    def write(self) -> None:
        (self.folder / "report.json").write_text(json.dumps(self.rows, indent=2), encoding="utf-8")
        lines = [f"{r['step']:<4} {r['status']:<6} {r['text']}" for r in self.rows]
        (self.folder / "report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"report: {self.folder / 'report.txt'}")


def guard_model(path: Path, folder: Path) -> None:
    """The only model this script opens is a copy inside its own run folder."""
    p, f = path.resolve(), folder.resolve()
    if f not in p.parents or p.suffix.lower() != ".qea":
        raise SystemExit(f"refusing to open {path}: not a .qea inside the run folder {folder}")


def running_ea() -> set[int]:
    out = subprocess.run(["powershell", "-NoProfile", "-Command", "(Get-Process EA -ErrorAction SilentlyContinue).Id"],
                         capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout
    return {int(x) for x in out.split() if x.isdigit()}


def ea_windows(pids) -> list[str]:
    """Visible top-level windows of the given processes: title [class] - tells which dialog EA is waiting on."""
    import ctypes
    from ctypes import wintypes
    user32, found = ctypes.windll.user32, []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            title, cls = ctypes.create_unicode_buffer(300), ctypes.create_unicode_buffer(300)
            user32.GetWindowTextW(hwnd, title, 300)
            user32.GetClassNameW(hwnd, cls, 300)
            found.append(f"pid {pid.value} visible={bool(user32.IsWindowVisible(hwnd))} [{cls.value}] {title.value!r}")
        return True

    user32.EnumWindows(visit, 0)
    return found


def kill_pids(pids) -> list[int]:
    killed = []
    for pid in sorted(pids):
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, stdin=subprocess.DEVNULL)
        killed.append(pid)
    return killed


# ---- expectations from the XMI ------------------------------------------------------------------------------------

def expected(xmi: Path) -> dict:
    """What the XMI promises, counted from the file itself."""
    root = ET.parse(xmi).getroot()
    ext = root.find("xmi:Extension", NS)
    elements = [e for e in ext.find("elements") if e.get(f"{{{XMI_NS}}}type") != "uml:Package"]
    packages = [e for e in ext.find("elements") if e.get(f"{{{XMI_NS}}}type") == "uml:Package"]
    connectors = list(ext.find("connectors"))
    diagrams = list(ext.find("diagrams"))
    ops = len(root.findall(".//ownedOperation"))
    attrs = len(root.findall(".//ownedAttribute"))
    tag_ids = [t.get(f"{{{XMI_NS}}}id") for e in elements for t in e.iter("tag")]
    ctag_ids = [t.get(f"{{{XMI_NS}}}id") for c in connectors for t in c.iter("tag")]
    return {
        "root": next(e.get("name") for e in packages if e.get("package") is None),
        "packages": len(packages),
        "element_types": _count(e.get(f"{{{XMI_NS}}}type")[4:] for e in elements),
        "element_stereos": _count(e.find("properties").get("stereotype") for e in elements
                                  if e.find("properties").get("stereotype")),
        "connector_types": _count(c.find("properties").get("ea_type") for c in connectors),
        "connector_stereos": _count(c.find("properties").get("stereotype") for c in connectors
                                    if c.find("properties").get("stereotype")),
        "element_tags": len(tag_ids), "connector_tags": len(ctag_ids),
        "operations": ops, "attributes": attrs,
        "diagrams": len(diagrams),
        "diagram_objects": sum(len(d.find("elements")) for d in diagrams),
        "element_ids": {e.get(f"{{{XMI_NS}}}idref") for e in elements},
    }


def _count(values) -> dict:
    out: dict = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out


# ---- what EA stored ------------------------------------------------------------------------------------------------

def stored(qea: Path, root_names: list[str]) -> dict:
    """Counts under the packages named root_names, from the .qea EA wrote (read-only, immutable)."""
    db = sqlite3.connect(f"file:{qea.resolve().as_posix()}?mode=ro&immutable=1", uri=True)
    try:
        pk = {pid: (name, parent) for pid, name, parent in db.execute("select Package_ID, Name, Parent_ID from t_package")}
        roots = [pid for pid, (n, _) in pk.items() if n in root_names]
        sub = set(roots)
        grew = True
        while grew:
            grew = False
            for pid, (_, parent) in pk.items():
                if parent in sub and pid not in sub:
                    sub.add(pid)
                    grew = True
        marks = ",".join(map(str, sub)) or "-1"
        # EA also keeps every package as a row of t_object; the XMI counts packages separately
        objs = db.execute(f"select Object_ID, Object_Type, Stereotype, ea_guid from t_object where Package_ID in ({marks}) "
                          f"and Object_Type <> 'Package'").fetchall()
        ids = ",".join(str(o[0]) for o in objs) or "-1"
        conns = db.execute(f"select Connector_Type, Stereotype from t_connector where Start_Object_ID in ({ids}) "
                           f"or End_Object_ID in ({ids})").fetchall()
        return {
            "roots": len(roots), "packages": len(sub),
            "element_types": _count(o[1] for o in objs),
            "element_stereos": _count(o[2] for o in objs if o[2]),
            "connector_types": _count(c[0] for c in conns),
            "connector_stereos": _count(c[1] for c in conns if c[1]),
            "element_tags": db.execute(f"select count(*) from t_objectproperties where Object_ID in ({ids})").fetchone()[0],
            "connector_tags": db.execute(
                f"select count(*) from t_connectortag where ElementID in "
                f"(select Connector_ID from t_connector where Start_Object_ID in ({ids}) or End_Object_ID in ({ids}))"
            ).fetchone()[0],
            "operations": db.execute(f"select count(*) from t_operation where Object_ID in ({ids})").fetchone()[0],
            "attributes": db.execute(f"select count(*) from t_attribute where Object_ID in ({ids})").fetchone()[0],
            "diagrams": db.execute(f"select count(*) from t_diagram where Package_ID in ({marks})").fetchone()[0],
            "diagram_objects": db.execute(f"select count(*) from t_diagramobjects where Diagram_ID in "
                                          f"(select Diagram_ID from t_diagram where Package_ID in ({marks}))").fetchone()[0],
            "guids": [o[3] for o in objs],
            "element_guids": {_norm(o[3]) for o in objs},
        }
    finally:
        db.close()


def _norm(guid: str) -> str:
    return "EAID_" + guid.strip("{}").upper().replace("-", "_")


def _merge(counts: list[dict]) -> dict:
    out: dict = {}
    for c in counts:
        for k, v in c.items():
            out[k] = out.get(k, 0) + v
    return out


def compare(rep: Report, step: str, want: list[dict], got: dict) -> bool:
    """One PASS / FAIL line per measured thing; want is the list of XMI expectations imported so far.

    Counts are exact (a second import must not leave extra elements); EA may add stereotypes of its own, which is only
    reported. Element identity: the GUIDs EA stored must be exactly the ids of the XMI (nothing renewed, nothing extra)."""
    ok_all = True
    for key in ("element_types", "connector_types"):
        merged = _merge([w[key] for w in want])
        if got[key] == merged:
            rep.add(step, "PASS", f"{key}: {merged}")
        else:
            ok_all = False
            rep.add(step, "FAIL", f"{key}: XMI {merged}, EA stored {got[key]}")
    for key in ("element_stereos", "connector_stereos"):
        merged = _merge([w[key] for w in want])
        short = {k: (n, got[key].get(k, 0)) for k, n in merged.items() if got[key].get(k, 0) != n}
        extra = {k: n for k, n in got[key].items() if k not in merged}
        if short:
            ok_all = False
            rep.add(step, "FAIL", f"{key} (kind: XMI, stored): {short}")
        else:
            rep.add(step, "PASS", f"{key}: {merged}" + (f"  (EA added: {extra})" if extra else ""))
    for key in ("operations", "attributes", "element_tags", "connector_tags", "diagrams", "diagram_objects"):
        w = sum(x[key] for x in want)
        if got[key] == w:
            rep.add(step, "PASS", f"{key}: {w}")
        else:
            ok_all = False
            rep.add(step, "FAIL", f"{key}: XMI {w}, EA stored {got[key]}")
    ids = set().union(*[w["element_ids"] for w in want])
    ids = {i.upper() for i in ids}
    if got["element_guids"] == ids:
        rep.add(step, "PASS", f"element GUIDs: the {len(ids)} ids of the XMI, none renewed, none extra")
    else:
        ok_all = False
        rep.add(step, "FAIL", f"element GUIDs: {len(ids - got['element_guids'])} of the XMI missing in EA, "
                              f"{len(got['element_guids'] - ids)} stored that the XMI does not have")
    dup = len(got["guids"]) - len(set(got["guids"]))
    if dup:
        ok_all = False
        rep.add(step, "FAIL", f"{dup} duplicate element GUIDs")
    return ok_all


# ---- the run -------------------------------------------------------------------------------------------------------

def run_ea(folder: Path, job: dict, timeout: int, rep: Report) -> list[dict]:
    job_file, driver = folder / "job.json", folder / "driver.ps1"
    before = running_ea()
    job = {**job, "before": sorted(before)}
    job_file.write_text(json.dumps(job, indent=2), encoding="utf-8")
    driver.write_text(DRIVER, encoding="utf-8-sig")
    proc = subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(driver), str(job_file)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, stdin=subprocess.DEVNULL)
    try:
        out, err = proc.communicate(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, stdin=subprocess.DEVNULL)
        out, err = proc.communicate()
        timed_out = True
    steps = [json.loads(l) for l in (out or "").splitlines() if l.startswith("{")]
    # Only the EA instance this run created is ended (its pid is reported right after COM answered); if COM never
    # answered, any EA that appeared since the snapshot is the one we started. Do not open EA by hand meanwhile.
    mine = {int(x) for s in steps if s["step"] == "PIDS" for x in s["detail"].split(",") if x.isdigit()}
    targets = (mine or (running_ea() - before)) & running_ea()
    windows = ea_windows(targets) if timed_out else []
    killed = kill_pids(targets)
    (folder / "driver_stdout.txt").write_text(out or "", encoding="utf-8")
    (folder / "driver_stderr.txt").write_text(err or "", encoding="utf-8")
    if timed_out:
        rep.add("E1", "ERROR", f"EA did not finish within {timeout}s: it is probably waiting for a licence / login / dialog "
                f"(network?). Killed the EA processes of this run: {killed or 'none'}. Steps seen: "
                f"{[s['step'] for s in steps]}. Windows of EA at that moment: {windows or 'none'}")
    elif proc.returncode != 0:
        rep.add("E1", "ERROR", f"driver exited with {proc.returncode}: {(err or out).strip()[:300]}")
    elif err.strip():
        rep.add("E1", "INFO", "driver stderr: " + err.strip().splitlines()[0][:300])
    return steps


def wait_for_ea(folder: Path, qea: Path, wait: int, rep: Report) -> None:
    """Until EA answers over COM and opens the scratch copy (it needs the enterprise network for its licence): short
    attempts every 20 s, at most `wait` seconds, then the normal steps run and report whatever happens."""
    start, attempt = time.time(), 0
    while True:
        attempt += 1
        probe = Report(folder)
        steps = {s["step"]: s for s in run_ea(folder, {"model": str(qea), "package": TARGET_PACKAGE, "imports": []}, 90, probe)}
        if steps.get("E1", {}).get("ok") and steps.get("E2", {}).get("ok"):
            rep.add("E1", "INFO", f"EA answered on attempt {attempt} after {int(time.time() - start)} s")
            return
        left = wait - (time.time() - start)
        print(f"waiting for EA / network: attempt {attempt} failed ({steps.get('E1', {}).get('detail', 'no answer')[:90]}); "
              f"{int(max(left, 0))} s left", flush=True)
        if left <= 20:
            rep.add("E1", "INFO", f"gave up waiting after {int(time.time() - start)} s and {attempt} attempts")
            return
        time.sleep(20)


def generate(rep: Report, folder: Path, repo: Path, modules, exe: str) -> dict[str, Path]:
    out = {}
    for m in modules:
        d = folder / "xmi" / m
        r = subprocess.run([exe, "analyze", "--module", m, "--context", "all", "--repo", str(repo), "--xmi", "--no-design",
                            "--out", str(d)], capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if r.returncode == 0 and (d / "model.xmi").is_file():
            out[m] = d / "model.xmi"
            rep.add("X1", "PASS", f"{m}: {(d / 'model.xmi').stat().st_size} bytes")
        else:
            rep.add("X1", "ERROR", f"{m}: archcreator exit {r.returncode}: {(r.stderr or r.stdout).strip()[-300:]}")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("phase", choices=("check", "import", "all"))
    ap.add_argument("--go", action="store_true", help="required for import / all (starts EA and writes the scratch model)")
    ap.add_argument("--dry-run", action="store_true", help="build everything but start no EA")
    ap.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    ap.add_argument("--modules", default=",".join(DEFAULT_MODULES))
    ap.add_argument("--archcreator", default=None, help="archcreator executable (default: next to this python)")
    ap.add_argument("--timeout", type=int, default=240, help="seconds EA may take before it is declared stuck")
    ap.add_argument("--out", type=Path, default=RUN_ROOT)
    ap.add_argument("--wait", type=int, default=0, help="seconds to keep retrying until EA answers (network not up yet)")
    a = ap.parse_args(argv)
    if a.phase != "check" and not a.go and not a.dry_run:
        ap.error("import / all need --go (or --dry-run)")

    folder = a.out / datetime.now().strftime("%Y%m%d_%H%M%S")
    folder.mkdir(parents=True)
    rep = Report(folder)
    exe = a.archcreator or str(Path(sys.executable).with_name("archcreator.exe"))

    if (EA_HOME / "EA.exe").is_file():
        rep.add("E0", "PASS", f"EA.exe {EA_HOME / 'EA.exe'}")
    else:
        rep.add("E0", "ERROR", "EA.exe missing")
        rep.write()
        return 2
    qea = folder / "scratch.qea"
    shutil.copyfile(BASE_MODEL, qea)
    guard_model(qea, folder)

    modules = [m for m in a.modules.split(",") if m]
    xmis: dict[str, Path] = {}
    if a.phase != "check":
        xmis = generate(rep, folder, a.repo, modules, exe)
    imports = []
    if a.phase != "check" and xmis:
        first, *rest = list(xmis)
        imports = [{"step": "I1", "xmi": str(xmis[first])}]
        # I2: re-import = delete the old as-built package, import again (EA opens its import dialog if the ids exist); I3: the others
        imports.append({"step": "I2", "xmi": str(xmis[first]), "replace": expected(xmis[first])["root"]})
        imports += [{"step": "I3", "xmi": str(xmis[m])} for m in rest]
    if a.dry_run:
        rep.add("E1", "SKIP", "dry run: EA not started; job.json and driver.ps1 are in the run folder")
        (folder / "job.json").write_text(json.dumps({"model": str(qea), "package": TARGET_PACKAGE, "imports": imports},
                                                    indent=2), encoding="utf-8")
        (folder / "driver.ps1").write_text(DRIVER, encoding="utf-8-sig")
        rep.write()
        return 0

    if a.wait:
        wait_for_ea(folder, qea, a.wait, rep)
    # Import steps run in separate EA sessions so each can be measured on the stored model: I1 | I2 | I3.
    sessions = [imports[:1], imports[1:2], imports[2:]] if imports else [[]]
    want_after = {"I1": xmis and [expected(xmis[list(xmis)[0]])], "I2": xmis and [expected(xmis[list(xmis)[0]])],
                  "I3": [expected(p) for p in xmis.values()] if xmis else []}
    roots_after = {"I1": [w["root"] for w in want_after["I1"]] if xmis else [], "I2": [w["root"] for w in want_after["I2"]] if xmis else [],
                   "I3": [w["root"] for w in want_after["I3"]]}
    for session in sessions:
        if not session and imports:
            continue
        steps = run_ea(folder, {"model": str(qea), "package": TARGET_PACKAGE, "imports": session}, a.timeout, rep)
        seen = {s["step"]: s for s in steps}
        for key in ("E1", "E2"):
            if key in seen:
                if session is sessions[0] or not seen[key]["ok"]:
                    rep.add(key, "PASS" if seen[key]["ok"] else "FAIL", seen[key]["detail"])
        if "E1" not in seen or not seen.get("E2", {}).get("ok", False):
            rep.add("E2", "ERROR", "EA did not open the scratch model (or the driver died); later steps not run")
            break
        for imp in session:
            s = seen.get(imp["step"])
            rep.add(imp["step"], "PASS" if s and s["ok"] else "FAIL", s["detail"] if s else "no answer from the driver")
            if s:  # measure even after a reported error: the stored model is the evidence
                got = stored(qea, roots_after[imp["step"]])
                compare(rep, "V" + imp["step"][1:], want_after[imp["step"]], got)
    # coexistence: element GUIDs of different modules must be disjoint (also in the XMI itself)
    if len(xmis) > 1:
        seen_ids: dict = {}
        for p in xmis.values():
            for i in expected(p)["element_ids"]:
                seen_ids[i] = seen_ids.get(i, 0) + 1
        shared = {i for i, n in seen_ids.items() if n > 1}
        rep.add("V7", "PASS" if not shared else "FAIL", f"element ids shared between modules: {len(shared)}")
    rep.write()
    return 1 if any(r["status"] in ("FAIL", "ERROR") for r in rep.rows) else 0


if __name__ == "__main__":
    sys.exit(main())
