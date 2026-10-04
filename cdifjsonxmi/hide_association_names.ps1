<#
.SYNOPSIS
Hide the name labels of association connectors on the diagrams under a package of the
Enterprise Architect project that is open right now; role names and multiplicities stay.

.DESCRIPTION
The associations linked_to_ea.py writes are named (Owner_role_Target), because
to-canonical-xmi derives association identifiers from their names. On a diagram the name is
the connector's middle-top label (LMT in DiagramLink.Geometry); this sets its HDN (hidden)
flag on every association connector of every diagram under -Package, recursively. Run it
again after adding classes to a diagram: EA shows new connectors' names.

EA's COM object is only reachable from Windows PowerShell 5.1; started from PowerShell 7 the
script relaunches itself in powershell.exe.

.PARAMETER Package
Name of the root package whose diagrams (and its sub-packages' diagrams) to update.

.PARAMETER DryRun
Report what would change without updating anything.

.EXAMPLE
.\hide_association_names.ps1 -Package linkTestEA4 -DryRun
#>
param(
    [Parameter(Mandatory = $true)][string]$Package,
    [switch]$DryRun
)
$ErrorActionPreference = "Stop"
if ($PSVersionTable.PSEdition -eq "Core") {
    $relaunch = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $PSCommandPath, "-Package", $Package)
    if ($DryRun) { $relaunch += "-DryRun" }
    & powershell.exe @relaunch
    exit $LASTEXITCODE
}

$LabelDefault = "CX=0:CY=0:OX=0:OY=0:HDN=1:BLD=0:ITA=0:UND=0:CLR=-1:ALN=1:DIR=0:ROT=0"

function Hide-MiddleTopLabel([string]$geometry) {
    # Geometry: "<position>;$<label>=<k=v:...>;..." -- the labels follow the '$'
    if ($geometry -notmatch '\$') { $geometry = $geometry.TrimEnd(";") + ";$" }
    $head, $labels = $geometry -split '\$', 2
    if ($labels -match '(^|;)LMT=([^;]*);') {
        $value = $Matches[2]
        if (-not $value) { $new = $LabelDefault }
        elseif ($value -match 'HDN=\d') { $new = $value -replace 'HDN=\d', 'HDN=1' }
        else { $new = "$value`:HDN=1" }
        $labels = $labels -replace '(^|;)LMT=[^;]*;', "`$1LMT=$new;"
    } else {
        $labels = "LMT=$LabelDefault;" + $labels
    }
    return "$head`$$labels"
}

$repo = ([System.Runtime.InteropServices.Marshal]::GetActiveObject("EA.App")).Repository
"EA project: $($repo.ConnectionString)"
$root = $repo.Models.GetAt(0)
$top = $null
for ($i = 0; $i -lt $root.Packages.Count; $i++) {
    if ($root.Packages.GetAt($i).Name -eq $Package) { $top = $root.Packages.GetAt($i) }
}
if (-not $top) { throw "No package '$Package' under '$($root.Name)'." }

$stats = @{ diagrams = 0; hidden = 0; already = 0 }
function Visit($pkg) {
    for ($d = 0; $d -lt $pkg.Diagrams.Count; $d++) {
        $diagram = $pkg.Diagrams.GetAt($d)
        $stats.diagrams++
        $changed = $false
        for ($l = 0; $l -lt $diagram.DiagramLinks.Count; $l++) {
            $link = $diagram.DiagramLinks.GetAt($l)
            $connector = $repo.GetConnectorByID($link.ConnectorID)
            if ($connector.Type -ne "Association" -or -not $connector.Name) { continue }
            $new = Hide-MiddleTopLabel $link.Geometry
            if ($new -eq $link.Geometry) { $stats.already++; continue }
            $stats.hidden++
            if (-not $DryRun) {
                $link.Geometry = $new
                [void]$link.Update()
                $changed = $true
            }
        }
        if ($changed) { $repo.ReloadDiagram($diagram.DiagramID) }
    }
    for ($p = 0; $p -lt $pkg.Packages.Count; $p++) { Visit $pkg.Packages.GetAt($p) }
}
Visit $top
$verb = if ($DryRun) { "would hide" } else { "hid" }
"$($stats.diagrams) diagram(s): $verb $($stats.hidden) association name label(s); $($stats.already) already hidden"
