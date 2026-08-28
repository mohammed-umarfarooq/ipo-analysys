<#
.SYNOPSIS
    Create (or remove) the Desktop and Start Menu shortcuts for IPO Copilot.

.DESCRIPTION
    Run once. Two shortcuts are created in each place — Start and Stop — both pointing at
    wscript.exe with the matching .vbs as its argument, which is what keeps a console
    window from appearing.

    The .lnk files store an absolute path, so they break if the project folder moves.
    Re-run this script after moving it; nothing else needs changing.

.PARAMETER Remove
    Delete the shortcuts instead of creating them. Leaves the launcher itself alone.
#>
[CmdletBinding()]
param([switch]$Remove)

$ErrorActionPreference = 'Stop'

$Desktop = [Environment]::GetFolderPath('Desktop')
$StartMenu = Join-Path ([Environment]::GetFolderPath('Programs')) 'IPO Copilot'

$Shortcuts = @(
    @{
        Name        = 'IPO Copilot'
        Script      = 'IPO Copilot.vbs'
        Icon        = 'ipo-copilot.ico'
        Description = 'Start the IPO Copilot dashboard'
    },
    @{
        Name        = 'Stop IPO Copilot'
        Script      = 'Stop IPO Copilot.vbs'
        Icon        = 'ipo-copilot-stop.ico'
        Description = 'Shut down the IPO Copilot servers'
    }
)

if ($Remove) {
    foreach ($entry in $Shortcuts) {
        $path = Join-Path $Desktop "$($entry.Name).lnk"
        if (Test-Path $path) { Remove-Item $path -Force; Write-Host "removed $path" }
    }
    if (Test-Path $StartMenu) { Remove-Item $StartMenu -Recurse -Force; Write-Host "removed $StartMenu" }
    return
}

# The icons are generated, not committed as binaries, so build them if they are missing.
foreach ($entry in $Shortcuts) {
    if (-not (Test-Path (Join-Path $PSScriptRoot $entry.Icon))) {
        Write-Host 'Generating icons...'
        Push-Location (Join-Path (Split-Path -Parent $PSScriptRoot) 'backend')
        try { & uv run python (Join-Path $PSScriptRoot 'make_icon.py') }
        finally { Pop-Location }
        break
    }
}

New-Item -ItemType Directory -Force -Path $StartMenu | Out-Null

$shell = New-Object -ComObject WScript.Shell
try {
    foreach ($entry in $Shortcuts) {
        $vbs = Join-Path $PSScriptRoot $entry.Script
        $icon = Join-Path $PSScriptRoot $entry.Icon
        if (-not (Test-Path $vbs)) { throw "missing $vbs" }

        foreach ($folder in @($Desktop, $StartMenu)) {
            $linkPath = Join-Path $folder "$($entry.Name).lnk"
            $link = $shell.CreateShortcut($linkPath)
            # wscript.exe, not powershell.exe: the .vbs is the thing that suppresses the
            # console window, so it has to be the process that runs.
            $link.TargetPath = Join-Path $env:WINDIR 'System32\wscript.exe'
            $link.Arguments = '"{0}"' -f $vbs
            $link.WorkingDirectory = $PSScriptRoot
            $link.IconLocation = "$icon,0"
            $link.Description = $entry.Description
            $link.Save()
            Write-Host "created $linkPath"
        }
    }
}
finally {
    [System.Runtime.InteropServices.Marshal]::ReleaseComObject($shell) | Out-Null
}

Write-Host ''
Write-Host 'Done. Double-click "IPO Copilot" on the Desktop, or press Start and type "IPO".'
