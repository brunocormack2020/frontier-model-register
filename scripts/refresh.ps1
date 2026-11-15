param(
    [Parameter(Mandatory=$true)][string]$PythonPath,
    [Parameter(Mandatory=$true)][string]$ConfigPath,
    [switch]$Discover,
    [switch]$Publish
)
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$EntryPoint = Join-Path $ProjectRoot "run_register.py"
$RefreshArgs = @($EntryPoint, "--config", $ConfigPath, "refresh")
if ($Discover) { $RefreshArgs += "--discover" }
& $PythonPath @RefreshArgs
$RefreshExit = $LASTEXITCODE
if ($RefreshExit -notin @(0, 2)) { exit $RefreshExit }
if ($RefreshExit -eq 2) { Write-Warning "Some source checks failed. Retained findings and review flags are in the outputs." }
if ($Publish) {
    & $PythonPath $EntryPoint --config $ConfigPath publish --apply
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
exit $RefreshExit
