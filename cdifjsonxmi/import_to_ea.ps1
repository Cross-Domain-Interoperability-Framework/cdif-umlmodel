<#
.SYNOPSIS
Import a linked set of EA XMI 1.1 files (written by linked_to_ea.py) into the Enterprise
Architect project that is open right now, in dependency order, keeping their GUIDs.

.DESCRIPTION
Attaches to the running EA, creates package -Package under the model root, and imports every
file listed in <Dir>\import_order.txt into it with EA's Project.ImportPackageXMI: the same as
Publish > Import > XMI File, with diagrams imported and GUIDs not stripped. Each block goes
into a package named after its _sources subdirectory (schemaorgProperties, skosProperties, ...),
created under -Package; the common types, shared types and shared unions go in -Package itself.

The GUIDs are the same on every run, so a set can be in a project only once: EA refuses (with
a modal dialog, which blocks this script) a package whose GUID exists elsewhere in the project.

EA's COM object is only reachable from Windows PowerShell 5.1; started from PowerShell 7 the
script relaunches itself in powershell.exe.

.PARAMETER Dir
Folder holding import_order.txt and the .xml files. Default: roundtrip\linked-ea beside this script.

.PARAMETER Package
Name of the package to create under the model root. Default: linkTest.

.PARAMETER Replace
If a package of that name already exists under the model root, delete it first. Without this
switch the script stops instead.

.EXAMPLE
.\import_to_ea.ps1 -Package linkTestEA4
.\import_to_ea.ps1 -Package linkTestEA4 -Replace
#>
param(
    [string]$Dir = (Join-Path $PSScriptRoot "roundtrip\linked-ea"),
    [string]$Package = "linkTest",
    [switch]$Replace
)
$ErrorActionPreference = "Stop"
# EA resolves relative paths against its own working directory
$Dir = (Resolve-Path $Dir).Path

if ($PSVersionTable.PSEdition -eq "Core") {
    $relaunch = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $PSCommandPath, "-Dir", $Dir, "-Package", $Package)
    if ($Replace) { $relaunch += "-Replace" }
    & powershell.exe @relaunch
    exit $LASTEXITCODE
}

$orderFile = Join-Path $Dir "import_order.txt"
if (-not (Test-Path $orderFile)) { throw "No $orderFile - run linked_to_ea.py first." }
$files = Get-Content $orderFile | Where-Object { $_.Trim() }

$repo = ([System.Runtime.InteropServices.Marshal]::GetActiveObject("EA.App")).Repository
"EA project: $($repo.ConnectionString)"
$root = $repo.Models.GetAt(0)

for ($i = $root.Packages.Count - 1; $i -ge 0; $i--) {
    if ($root.Packages.GetAt($i).Name -eq $Package) {
        if (-not $Replace) { throw "Package '$Package' already exists under '$($root.Name)'; pass -Replace to delete it first." }
        "deleting existing package '$Package'"
        [void]$root.Packages.DeleteAt($i, $true)
    }
}
$root.Packages.Refresh()
$pkg = $root.Packages.AddNew($Package, "Package")
$pkg.Update()
$root.Packages.Refresh()
"created package '$Package' under '$($root.Name)'"

$project = $repo.GetProjectInterface()
$top = $project.GUIDtoXML($pkg.PackageGUID)
# one package per _sources subdirectory (e.g. schemaorgProperties), holding its blocks;
# files at the top of $Dir (common types, shared types, shared unions) go in $Package itself
$folders = @{}
$failed = 0
foreach ($rel in $files) {
    $file = Join-Path $Dir $rel
    $target = $top
    $parts = $rel -split '[\\/]'
    if ($parts.Count -gt 1) {
        $folder = $parts[0]
        if (-not $folders.ContainsKey($folder)) {
            $sub = $pkg.Packages.AddNew($folder, "Package")
            [void]$sub.Update()
            $pkg.Packages.Refresh()
            $folders[$folder] = $project.GUIDtoXML($sub.PackageGUID)
        }
        $target = $folders[$folder]
    }
    # ImportPackageXMI(target package, file, import diagrams = 1, strip GUIDs = 0)
    $result = $project.ImportPackageXMI($target, $file, 1, 0)
    if ($result -and $result -notmatch '^\{[0-9A-Fa-f-]+\}$') { $failed++; "FAILED  $rel : $result" }
    else { "ok      $rel" }
}
$repo.RefreshModelView($pkg.PackageID)
"$($files.Count - $failed) of $($files.Count) imported into '$Package'"
if ($failed) { exit 1 }
