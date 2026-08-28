<#
.SYNOPSIS
    Shut down the IPO Copilot backend and dashboard.

.DESCRIPTION
    Closing the app window does not stop anything — the two servers were started
    detached and keep running, holding ports 8000 and 3000. This stops them.

    Processes are found by the ports they are listening on rather than by name, so a
    node process belonging to something else entirely is never killed. The recorded
    PIDs from launch.ps1 are stopped too, because the thing holding the port is a child
    (uv spawns python; npm spawns node through cmd) and killing only the listener would
    leave its parent behind.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'

$RunDir = Join-Path $PSScriptRoot 'run'
$IcoPath = Join-Path $PSScriptRoot 'ipo-copilot-stop.ico'
$Ports = @{ 'engine' = 8000; 'dashboard' = 3000 }

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

function Get-ListenerPid {
    param([int]$Port)
    try {
        Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
            Select-Object -ExpandProperty OwningProcess -Unique
    }
    catch {
        # Get-NetTCPConnection needs the NetTCPIP module; netstat is always there.
        netstat -ano |
            Select-String -Pattern ":$Port\s.*LISTENING" |
            ForEach-Object { ($_.ToString().Trim() -split '\s+')[-1] } |
            Sort-Object -Unique
    }
}

function Stop-Tree {
    param([int]$ProcessId)
    if ($ProcessId -le 4) { return }  # never touch System/Idle
    foreach ($child in @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $ProcessId" -ErrorAction SilentlyContinue)) {
        Stop-Tree -ProcessId ([int]$child.ProcessId)
    }
    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}

$stopped = New-Object System.Collections.Generic.List[string]

# Recorded parents first: killing them takes their listening children with them.
foreach ($pidFile in @(Get-ChildItem -Path $RunDir -Filter '*.pid' -ErrorAction SilentlyContinue)) {
    $recorded = 0
    if ([int]::TryParse((Get-Content $pidFile.FullName -ErrorAction SilentlyContinue), [ref]$recorded)) {
        if (Get-Process -Id $recorded -ErrorAction SilentlyContinue) {
            Stop-Tree -ProcessId $recorded
        }
    }
    Remove-Item $pidFile.FullName -Force -ErrorAction SilentlyContinue
}

# Then anything still holding a port — covers servers started by hand from a terminal.
foreach ($name in $Ports.Keys) {
    $port = $Ports[$name]
    foreach ($owner in @(Get-ListenerPid -Port $port)) {
        $owningPid = 0
        if (-not [int]::TryParse("$owner", [ref]$owningPid)) { continue }
        Stop-Tree -ProcessId $owningPid
    }
    Start-Sleep -Milliseconds 250
    if (-not (Get-ListenerPid -Port $port)) { $stopped.Add("$name (port $port)") }
}

$message = if ($stopped.Count -gt 0) {
    'Stopped ' + ($stopped -join ' and ') + '.'
}
else {
    'Nothing was running.'
}

# A short, self-closing confirmation. A MessageBox would demand a click for something
# that needs no decision; silence would leave you wondering whether it worked.
$form = New-Object System.Windows.Forms.Form
$form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
$form.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen
$form.Size = New-Object System.Drawing.Size(400, 108)
$form.BackColor = [System.Drawing.Color]::FromArgb(11, 18, 32)
$form.TopMost = $true
$form.ShowInTaskbar = $false

if (Test-Path $IcoPath) {
    $picture = New-Object System.Windows.Forms.PictureBox
    $picture.Location = New-Object System.Drawing.Point(22, 30)
    $picture.Size = New-Object System.Drawing.Size(48, 48)
    $picture.Image = (New-Object System.Drawing.Icon($IcoPath, 48, 48)).ToBitmap()
    $form.Controls.Add($picture)
}

$label = New-Object System.Windows.Forms.Label
$label.Text = "IPO Copilot`r`n$message"
$label.Font = New-Object System.Drawing.Font('Segoe UI', 10)
$label.ForeColor = [System.Drawing.Color]::FromArgb(203, 213, 225)
$label.Location = New-Object System.Drawing.Point(88, 32)
$label.Size = New-Object System.Drawing.Size(295, 48)
$form.Controls.Add($label)

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 1600
$timer.Add_Tick({ $timer.Stop(); $form.Close() })
$timer.Start()
$form.ShowDialog() | Out-Null
