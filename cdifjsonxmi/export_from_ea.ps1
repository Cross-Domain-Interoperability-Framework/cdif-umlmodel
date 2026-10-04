<#
.SYNOPSIS
Export a package of the Enterprise Architect project that is open right now as UML 2.5 /
XMI 2.5.1, the input to_canonical.py (Achim Wackerow's to-canonical-xmi) expects.

.DESCRIPTION
Attaches to the running EA and calls Project.ExportPackageXMI on the root package named
-Package: XMI type 22 (UML 2.5 / XMI 2.5.1 in EA 16; 17 and 18 give XMI 2.4.1), no diagrams,
formatted XML, no DTD. The same as Publish > Publish As > 'UML 2.5 (XMI 2.5.1)' with
'Export Diagrams' unchecked and EA extensions kept. Exporting only reads the model.

EA's COM object is only reachable from Windows PowerShell 5.1; started from PowerShell 7 the
script relaunches itself in powershell.exe.

.PARAMETER Package
Name of the root package to export. Default: linkTest.

.PARAMETER Out
The XMI file to write. Default: roundtrip\from-ea\<Package>.xmi beside this script.

.EXAMPLE
.\export_from_ea.ps1 -Package linkTestEA4
#>
param(
    [string]$Package = "linkTest",
    [string]$Out = ""
)
$ErrorActionPreference = "Stop"
if (-not $Out) { $Out = Join-Path $PSScriptRoot "roundtrip\from-ea\$Package.xmi" }
# EA resolves relative paths against its own working directory
$Out = [System.IO.Path]::GetFullPath($Out)

if ($PSVersionTable.PSEdition -eq "Core") {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath -Package $Package -Out $Out
    exit $LASTEXITCODE
}

New-Item -ItemType Directory -Force (Split-Path $Out) | Out-Null
$repo = ([System.Runtime.InteropServices.Marshal]::GetActiveObject("EA.App")).Repository
"EA project: $($repo.ConnectionString)"
$root = $repo.Models.GetAt(0)
$pkg = $null
for ($i = 0; $i -lt $root.Packages.Count; $i++) {
    if ($root.Packages.GetAt($i).Name -eq $Package) { $pkg = $root.Packages.GetAt($i) }
}
if (-not $pkg) { throw "No package '$Package' under '$($root.Name)'." }
$project = $repo.GetProjectInterface()
# ExportPackageXMI(package GUID, XMI type 22 = UML 2.5 / XMI 2.5.1, diagrams 0, diagram images -1
# (none), format XML 1, use DTD 0, file)
$result = $project.ExportPackageXMI($pkg.PackageGUID, 22, 0, -1, 1, 0, $Out)
if (-not (Test-Path $Out)) { throw "Export failed: $result" }
"exported '$Package' to $Out ($((Get-Item $Out).Length) bytes)"
