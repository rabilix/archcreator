# Live check of the EA 16 import (prepared 2026-10-20, run 2026-10-21)

What the offline tests cannot prove: that Enterprise Architect 16 really accepts `model.xmi` and stores what the file
promises. One script, `tools/ea_verify.py`, imports into a scratch copy of EA's own empty model (`EABase.qea`) through EA's
automation interface and reads the resulting `.qea` back (SQLite, read-only).

Hard limits: the only model it opens is a `.qea` copy inside its own run folder (`%TEMP%\archcreator_ea_verify\<time>`).
No project model, no UML/.qea/.eap of the repositories is read or written. The target repository is only analysed.

## Before the go (you)

1. Enterprise network / VPN up (EA seems to check its licence there: without it EA.exe starts without a window and COM hangs).
2. EA is not running (the script kills only EA processes it started itself, after a timeout).

## Run (repo root, venv)

| Phase | Command | Does |
|---|---|---|
| check | `.venv\Scripts\python tools\ea_verify.py check` | EA starts, answers over COM, opens the scratch copy |
| import | `.venv\Scripts\python tools\ea_verify.py import --go` | generates SEV + DCDC_FSM XMI, imports, compares |
| dry run | `... import --dry-run` | everything except starting EA |

Options: `--repo`, `--modules SEV,DCDC_FSM`, `--timeout 240`, `--archcreator <exe>`. Report: `report.txt` / `report.json` in the
run folder, with `driver_stdout.txt`, `job.json`, `scratch.qea` and the XMI files.

## Steps

| Step | Question | If it fails |
|---|---|---|
| E0 | EA.exe is installed | repair the install |
| E1 | EA.App COM object starts | timeout = EA waits for licence / network / dialog |
| E2 | EA opens the scratch copy | see driver_stdout.txt |
| X1 | archcreator writes the XMI | the error text is in the report |
| I1 / VI1 | first import; EA stored every element, connector type, stereotype, tag, operation, attribute, diagram object the XMI promises | the FAIL line names the kind and the two counts |
| I2 / VI2 | the same file again replaces, does not duplicate (ids stay) | duplicate GUIDs are reported |
| I3 / VI3 | the second module next to the first | counts are the sum |
| V7 | the modules share no element id | export key problem |

PASS / FAIL / INFO / SKIP / ERROR per line; a failing step does not stop the next. Still manual afterwards: open
`scratch.qea` in EA and look at the diagram (layout, labels).

Note on cleanup: EA runs as one process, so the script ends the EA instance that appeared during its run (found by process id
before and after). Do not start EA by hand while it runs.

## Result of the run on 2026-10-21 (EA 16.0 build 1605, Windows 11): all steps PASS

- SEV and DCDC_FSM import into a fresh model: every element type, connector type (`Realisation`, `Usage`, `Dependency`),
  stereotype, tagged value, operation, attribute, diagram object and element GUID the XMI promises is stored by EA, none
  renewed, none extra. The two modules share no element id and sit side by side.
- `Project.ImportPackageXMI` returns the GUID of the imported package on success.
- Re-import: calling it again for a package whose ids already exist opens EA's own *Import Package from Native/XMI File*
  dialog and waits for a click, so it cannot be scripted. The script deletes the old as-built package first and imports again;
  the result is identical to the first import. In the GUI expect that dialog.
- EA start: with the enterprise network up it answered within 13 s on the last run; earlier starts showed a
  *Licence Management* dialog and took up to 6 minutes (use `--wait 900`).
