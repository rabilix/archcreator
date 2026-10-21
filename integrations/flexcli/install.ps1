<#
Installs the ArchCreator plugin into a flexcli checkout (copies 3 files, registers it in internal.ts). Idempotent.

  install.ps1 -Flexcli F:\github\flexcli            # install
  install.ps1 -Flexcli F:\github\flexcli -Check      # only report what is missing / different, change nothing

Run it on the branch you want the plugin on (a feature branch, not the parallel session's tree while it is open).
#>
param([Parameter(Mandatory)][string]$Flexcli, [switch]$Check)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$core = Join-Path $Flexcli "packages\core"
if (-not (Test-Path (Join-Path $core "src\plugin\internal.ts"))) { throw "not a flexcli checkout: $Flexcli" }

$copies = @(
  @("files\plugin\arch-creator.ts",          "src\plugin\arch-creator.ts"),
  @("files\plugin\arch-creator\method.txt",  "src\plugin\arch-creator\method.txt"),
  @("files\test\arch-creator.test.ts",       "test\plugin\arch-creator.test.ts"))
$import   = 'import { ArchCreatorPlugin } from "./arch-creator.js"'
$register = '  ArchCreatorPlugin.Plugin,'
$internal = Join-Path $core "src\plugin\internal.ts"
$text     = Get-Content -Raw $internal

foreach ($c in $copies) {
  $from, $to = (Join-Path $here $c[0]), (Join-Path $core $c[1])
  $state = if (-not (Test-Path $to)) { "missing" } elseif ((Get-FileHash $from).Hash -ne (Get-FileHash $to).Hash) { "different" } else { "same" }
  "{0,-10} {1}" -f $state, $c[1]
  if (-not $Check -and $state -ne "same") { New-Item -ItemType Directory -Force (Split-Path $to) | Out-Null; Copy-Item $from $to -Force }
}
$hasImport, $hasReg = $text.Contains($import), $text.Contains($register.Trim())
"{0,-10} internal.ts import" -f $(if ($hasImport) { "same" } else { "missing" })
"{0,-10} internal.ts registration" -f $(if ($hasReg) { "same" } else { "missing" })
if ($Check) { return }

if (-not $hasImport) {   # after the c-debug import, which every flexcli with this plugin system has
  if ($text -notmatch 'import \{ CDebugPlugin \} from "\./c-debug\.js"') { throw "anchor 'CDebugPlugin' import not found in internal.ts: add the import by hand" }
  $text = $text -replace '(import \{ CDebugPlugin \} from "\./c-debug\.js"\r?\n)', "`$1$import`n"
}
if (-not $hasReg) {
  if ($text -notmatch '\r?\n\s*CDebugPlugin\.Plugin,') { throw "anchor 'CDebugPlugin.Plugin,' not found in internal.ts: add the registration by hand" }
  $text = $text -replace '(\r?\n)(\s*)CDebugPlugin\.Plugin,', "`$1`$2CDebugPlugin.Plugin,`$1`$2ArchCreatorPlugin.Plugin,"
}
Set-Content $internal $text -NoNewline
"done. Next: cd $core ; bun run typecheck ; bun test test/plugin"
