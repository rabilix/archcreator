"""Increment 0 of ea-apply (docs/plans/2026-11-05-ea-apply.md): prove on a real EA 16 what the writer will rely on.

Through EA's Automation Interface it creates, in a scratch COPY of EA's own empty model (EABase.qea) inside its run
folder, one of every object ea-apply writes, changes and finds one again by GUID, then reads the .qea back (SQLite,
read-only) and checks what EA really stored. Nothing else is opened or written.

    python tools/ea_probe.py --go            run the probe (EA must not be running)
    python tools/ea_probe.py --dry-run       write job and driver, start no EA

Steps: P0 EA not running, E1 COM start, E2 open scratch copy, C* creates and lookups in EA (driver lines), S1..S9 what
the .qea holds, X1 how EA was started (command line of the COM server: does it carry -Embedding?).
"""

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ea_verify import BASE_MODEL, RUN_ROOT, Report, ea_windows, guard_model, kill_pids, running_ea  # noqa: E402

PACKAGE = "ArchCreatorProbe"

DRIVER = r'''
# Written by tools/ea_probe.py. Opens ONLY the model named in the job file. One JSON line per step.
param($Job)
$ErrorActionPreference = "Stop"
$j = Get-Content -Raw -Encoding UTF8 $Job | ConvertFrom-Json
function Say($step, $ok, $detail) { (@{step=$step; ok=$ok; detail="$detail"} | ConvertTo-Json -Compress) }
function Try($step, [scriptblock]$body) {
    try { $d = & $body; Say $step $true $d } catch { Say $step $false $_.Exception.Message }
}
try {
    $ea = New-Object -ComObject EA.App
    Say "E1" $true "EA.App created"
    $procs = @(Get-CimInstance Win32_Process -Filter "Name='EA.exe'" | Where-Object { $j.before -notcontains $_.ProcessId })
    (@{step="PIDS"; ok=$true; detail=(($procs | ForEach-Object { $_.ProcessId }) -join ",")} | ConvertTo-Json -Compress)
    (@{step="X1"; ok=$true; detail=(($procs | ForEach-Object { "$($_.ProcessId): $($_.CommandLine)" }) -join " | ")} | ConvertTo-Json -Compress)
} catch { Say "E1" $false $_.Exception.Message; exit 0 }
$r = $ea.Repository
try {
    if (-not $r.OpenFile($j.model)) { Say "E2" $false "OpenFile returned false"; $r.Exit(); exit 0 }
    Say "E2" $true "opened $($j.model)"
} catch { Say "E2" $false $_.Exception.Message; try { $r.Exit() } catch {}; exit 0 }

function Tag($owner, $name, $value) { $t = $owner.TaggedValues.AddNew($name, $value); $t.Update(); $owner.TaggedValues.Refresh() }

$root = $r.Models.GetAt(0)
Try "C1" { $script:pk = $root.Packages.AddNew($j.package, ""); $pk.Update(); $root.Packages.Refresh()
           "package $($pk.PackageGUID)" }
Try "C2" { Tag $pk.Element "archcreator_id" "pkg:probe"; "package tag via Package.Element" }
Try "C3" { $script:comp = $pk.Elements.AddNew("ProbeComp", "Component"); $comp.Stereotype = "as-built"
           $comp.Notes = "Files:`nsrc/App/probe.c"; $comp.Update(); Tag $comp "archcreator_id" "comp:probe"
           Tag $comp "first_evidence" ("x" * 300); "component $($comp.ElementGUID)" }
Try "C4" { $script:fn = $pk.Elements.AddNew("ProbeFn", "Interface"); $fn.Stereotype = "Function"; $fn.Update()
           $m = $fn.Methods.AddNew("ProbeFn", ""); $m.Update(); $fn.Methods.Refresh(); Tag $fn "archcreator_id" "prov:fn"
           "interface with operation" }
Try "C5" { $script:var = $pk.Elements.AddNew("ProbeVar", "Interface"); $var.Stereotype = "Variable"; $var.Update()
           $a = $var.Attributes.AddNew("ProbeVar", ""); $a.Update(); $var.Attributes.Refresh()
           "interface with attribute" }
Try "C6" { $script:other = $pk.Elements.AddNew("ProbeOther", "Component"); $other.Stereotype = "c_module"; $other.Update()
           "context component" }
foreach ($c in $j.connectors) {
    Try "C7:$($c.type)" {
        $client = if ($c.client -eq "comp") { $comp } else { $other }
        $supplier = switch ($c.supplier) { "fn" { $fn } "var" { $var } default { $other } }
        $k = $client.Connectors.AddNew($c.name, $c.type); $k.SupplierID = $supplier.ElementID
        if ($c.stereotype) { $k.Stereotype = $c.stereotype }
        $k.Notes = "src/App/probe.c:12 in probe_init"; $k.Update(); $client.Connectors.Refresh()
        $t = $k.TaggedValues.AddNew("archcreator_id", "conn:$($c.name)"); $t.Update()
        "$($c.type) -> stored type $($k.Type) guid $($k.ConnectorGUID)"
    }
}
Try "C8" { $script:d = $pk.Diagrams.AddNew("ProbeComp (as-built)", "Component"); $d.Update()
           $o = $d.DiagramObjects.AddNew("l=600;r=820;t=40;b=100;", ""); $o.ElementID = $comp.ElementID; $o.Update()
           $o = $d.DiagramObjects.AddNew("l=320;r=540;t=40;b=100;", ""); $o.ElementID = $fn.ElementID; $o.Update()
           $d.DiagramObjects.Refresh(); "diagram with $($d.DiagramObjects.Count) objects" }
Try "C9" { $e = $r.GetElementByGuid($comp.ElementGUID); $p = $r.GetPackageByGuid($pk.PackageGUID)
           "found again by GUID: $($e.Name) in $($p.Name)" }
Try "C10" { $e = $r.GetElementByGuid($comp.ElementGUID); $e.Notes = "changed"; $e.Name = "ProbeComp [stale]"
            $e.Stereotype = "stale"; $e.Update(); "updated name, notes, stereotype" }
Try "C11" { $id = $comp.ElementID; $found = $r.GetElementByID($id); "GetElementByID $id -> $($found.Name)" }
try { $r.CloseFile() } catch {}
try { $r.Exit() } catch {}
'''

CONNECTORS = [  # what the writer needs; "Realization" probes whether EA also accepts the UML spelling
    {"name": "real", "type": "Realisation", "client": "comp", "supplier": "fn", "stereotype": ""},
    {"name": "real_uml", "type": "Realization", "client": "comp", "supplier": "var", "stereotype": ""},
    {"name": "use", "type": "Usage", "client": "comp", "supplier": "fn", "stereotype": "call"},
    {"name": "dep", "type": "Dependency", "client": "comp", "supplier": "other", "stereotype": "as-built"},
]


def run_driver(folder: Path, job: dict, timeout: int, rep: Report) -> list[dict]:
    job_file, driver = folder / "job.json", folder / "driver.ps1"
    before = running_ea()
    job_file.write_text(json.dumps({**job, "before": sorted(before)}, indent=2), encoding="utf-8")
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
    steps = [json.loads(line) for line in (out or "").splitlines() if line.startswith("{")]
    mine = {int(x) for s in steps if s["step"] == "PIDS" for x in s["detail"].split(",") if x.isdigit()}
    targets = (mine or (running_ea() - before)) & running_ea()
    windows = ea_windows(targets) if timed_out else []
    killed = kill_pids(targets)
    (folder / "driver_stdout.txt").write_text(out or "", encoding="utf-8")
    (folder / "driver_stderr.txt").write_text(err or "", encoding="utf-8")
    if timed_out:
        rep.add("E1", "ERROR", f"EA did not finish within {timeout}s (licence / network / dialog?). Ended: "
                f"{killed or 'none'}. Windows: {windows or 'none'}")
    elif err.strip():
        rep.add("E1", "INFO", "driver stderr: " + err.strip().splitlines()[0][:300])
    return steps


def read_back(qea: Path) -> dict:
    """What EA stored under the probe package (read-only, immutable)."""
    db = sqlite3.connect(f"file:{qea.resolve().as_posix()}?mode=ro&immutable=1", uri=True)
    try:
        pid = db.execute("select Package_ID from t_package where Name = ?", (PACKAGE,)).fetchone()
        if not pid:
            return {}
        pid = pid[0]
        objs = db.execute("select Object_ID, Object_Type, Name, Stereotype, Note from t_object where Package_ID = ? "
                          "order by Name", (pid,)).fetchall()
        by_id = {o[0]: o for o in objs}
        ids = ",".join(str(i) for i in by_id) or "-1"
        pkg_obj = db.execute("select Object_ID from t_object where Object_Type = 'Package' and PDATA1 = ?",
                             (str(pid),)).fetchone()
        conns = db.execute(f"select Connector_ID, Connector_Type, Stereotype, Start_Object_ID, End_Object_ID, Notes "
                           f"from t_connector where Start_Object_ID in ({ids})").fetchall()
        cids = ",".join(str(c[0]) for c in conns) or "-1"
        return {
            "objects": [{"type": o[1], "name": o[2], "stereotype": o[3], "note": o[4]} for o in objs],
            "package_tags": db.execute("select Property, Value from t_objectproperties where Object_ID = ?",
                                       (pkg_obj[0] if pkg_obj else -1,)).fetchall(),
            "element_tags": db.execute(f"select Object_ID, Property, length(Value), length(Notes) from "
                                       f"t_objectproperties where Object_ID in ({ids})").fetchall(),
            "connectors": [{"type": c[1], "stereotype": c[2], "client": by_id.get(c[3], [None] * 3)[2],
                            "supplier": by_id.get(c[4], [None] * 3)[2], "notes": c[5]} for c in conns],
            "connector_tags": db.execute(f"select ElementID, Property, VALUE from t_connectortag where ElementID in "
                                         f"({cids})").fetchall(),
            "operations": db.execute(f"select Name from t_operation where Object_ID in ({ids})").fetchall(),
            "attributes": db.execute(f"select Name from t_attribute where Object_ID in ({ids})").fetchall(),
            "diagram_objects": db.execute(
                "select o.RectLeft, o.RectTop, o.RectRight, o.RectBottom, t.Name from t_diagramobjects o "
                "join t_object t on t.Object_ID = o.Object_ID join t_diagram d on d.Diagram_ID = o.Diagram_ID "
                "where d.Package_ID = ?", (pid,)).fetchall(),
        }
    finally:
        db.close()


def judge(rep: Report, got: dict) -> None:
    if not got:
        rep.add("S1", "FAIL", f"package {PACKAGE} not found in the .qea")
        return
    names = {o["name"]: o for o in got["objects"]}
    rep.add("S1", "PASS" if {"ProbeFn", "ProbeVar", "ProbeOther"} <= set(names) else "FAIL",
            f"objects: {[(o['type'], o['name'], o['stereotype']) for o in got['objects']]}")
    comp = names.get("ProbeComp [stale]") or names.get("ProbeComp")
    rep.add("S2", "PASS" if comp and comp["name"].endswith("[stale]") and comp["stereotype"] == "stale"
            and comp["note"] == "changed" else "FAIL", f"update by GUID stored: {comp}")
    rep.add("S3", "PASS" if ("archcreator_id", "pkg:probe") in got["package_tags"] else "FAIL",
            f"package tag on its element: {got['package_tags']}")
    long_tag = [t for t in got["element_tags"] if t[1] == "first_evidence"]
    rep.add("S4", "INFO", f"300-character tag stored as (value length, notes length): {long_tag}")
    for c in got["connectors"]:
        rep.add("S5", "INFO", f"connector {c['type']} «{c['stereotype']}» {c['client']} -> {c['supplier']}, "
                f"notes {c['notes']!r}")
    types = {c["type"] for c in got["connectors"]}
    rep.add("S6", "PASS" if {"Realisation", "Usage", "Dependency"} <= types else "FAIL",
            f"stored connector types: {sorted(types)}")
    rep.add("S7", "PASS" if len(got["connector_tags"]) == len(got["connectors"]) else "FAIL",
            f"connector tags: {got['connector_tags']}")
    rep.add("S8", "PASS" if got["operations"] and got["attributes"] else "FAIL",
            f"operations {got['operations']}, attributes {got['attributes']}")
    rep.add("S9", "PASS" if len(got["diagram_objects"]) == 2 else "FAIL",
            f"diagram objects (left, top, right, bottom, element): {got['diagram_objects']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--go", action="store_true", help="start EA and write the scratch copy")
    ap.add_argument("--dry-run", action="store_true", help="write job and driver, start no EA")
    ap.add_argument("--timeout", type=int, default=900, help="seconds EA may take (licence check can take minutes)")
    ap.add_argument("--out", type=Path, default=RUN_ROOT)
    a = ap.parse_args(argv)
    if not a.go and not a.dry_run:
        ap.error("pass --go to run the probe (or --dry-run)")
    folder = a.out / ("probe_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    folder.mkdir(parents=True)
    rep = Report(folder)
    qea = folder / "scratch.qea"
    job = {"model": str(qea), "package": PACKAGE, "connectors": CONNECTORS}
    if a.dry_run:
        (folder / "job.json").write_text(json.dumps(job, indent=2), encoding="utf-8")
        (folder / "driver.ps1").write_text(DRIVER, encoding="utf-8-sig")
        rep.add("P0", "INFO", f"dry run: job and driver in {folder}")
        rep.write()
        return 0
    if running_ea():
        rep.add("P0", "FAIL", "Enterprise Architect is running: close it first (the probe never touches a person's EA)")
        rep.write()
        return 2
    if not BASE_MODEL.is_file():
        rep.add("E0", "FAIL", f"{BASE_MODEL} not found: is EA 16 installed?")
        rep.write()
        return 2
    shutil.copyfile(BASE_MODEL, qea)
    guard_model(qea, folder)
    for s in run_driver(folder, job, a.timeout, rep):
        if s["step"] == "PIDS":
            continue
        rep.add(s["step"], "INFO" if s["step"] == "X1" else "PASS" if s["ok"] else "FAIL", s["detail"])
    judge(rep, read_back(qea))
    rep.write()
    return 1 if any(r["status"] in ("FAIL", "ERROR") for r in rep.rows) else 0


if __name__ == "__main__":
    sys.exit(main())
