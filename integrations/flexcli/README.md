# ArchCreator plugin for flexcli

flexcli's agent **ArchCreator** (`/archcreator <Module>`) runs the `archcreator` tool and explains its result. It adds the
tools `archcreator_analyze` and `archcreator_modules`. The plugin has no analysis logic: findings, severities and the exit
code are archcreator's. User documentation: `WINDOWS-CMD-section.md` (paste it into flexcli's `WINDOWS-CMD.md`).

This folder is the source of truth for the plugin; it came from flexcli branch `feature/archrev` (see `files/SOURCE.txt`).

## It is a built-in plugin

The plugin uses flexcli's internal packages (`@copilotcli/plugin/effect`, `FileAccess`, `Location`, `Permission`,
`AppProcess`), so it is compiled into `packages/core` and registered in `internal.ts`. It cannot be loaded from a config
`plugin` entry or a `.flexcli/plugin` folder: those load plugins that use only the public `@copilotcli/plugin` API. Turning it
into such an external plugin means rewriting the permission and file-access parts against that API; not done.

## Install into flexcli (one time per flexcli branch)

1. Close any session working in that checkout, and use a branch of your own (not another session's open tree).
2. Check what would change:

   ```powershell
   F:\github\ArchCreator\integrations\flexcli\install.ps1 -Flexcli <flexcli checkout> -Check
   ```

3. Install. It copies three files into `packages/core` and adds two lines to `src/plugin/internal.ts` (the import after
   `CDebugPlugin` and `ArchCreatorPlugin.Plugin,` after `CDebugPlugin.Plugin,`); running it again changes nothing:

   ```powershell
   F:\github\ArchCreator\integrations\flexcli\install.ps1 -Flexcli <flexcli checkout>
   ```

4. Verify in flexcli (20 tests for the plugin):

   ```bat
   cd <flexcli checkout>\packages\core
   bun run typecheck
   bun test test/plugin
   ```

5. Review `git diff`, add the section of `WINDOWS-CMD-section.md` to flexcli's `WINDOWS-CMD.md`, commit on your branch.

If `internal.ts` has changed so much that an anchor line (`CDebugPlugin`) is gone, the script stops and says which of the two
lines to add by hand.

## On each machine

Install archcreator outside any project (the `archcreator.exe` folder from `build.bat`, or the wheel) and set:

```bat
set "ARCHCREATOR_BIN=C:\tools\archcreator\archcreator.exe"
set "ARCHCREATOR_PROFILES=\\server\share\archcreator\profiles"
```

Then in flexcli: `/archcreator DCDC_FSM` or headless
`flexcli run --standalone --agent arch-creator "Review DCDC_FSM and explain the complexity findings."`.
Outputs go to `.flexcli\archcreator\<session>\<Module>\`.

## Updating

Change the plugin in flexcli, run its tests, copy the three files back into `files\` (and update `SOURCE.txt`). A change of
archcreator's CLI or output files needs the plugin and its tests changed together.
