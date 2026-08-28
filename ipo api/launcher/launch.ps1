<#
.SYNOPSIS
    Start the IPO Copilot backend and dashboard, wait for both, open it in an app window.

.DESCRIPTION
    The double-clickable entry point. Two servers have to come up before the dashboard
    is usable — uvicorn on 8000 and Next on 3000 — which takes long enough that a bare
    double-click with no feedback feels broken. So this shows a small splash and narrates
    each step while it waits.

    Both servers are started hidden with their output redirected to launcher\logs, so a
    failure is diagnosable afterwards instead of vanishing with the console window.

    Idempotent on purpose: if both ports already answer, nothing is started and the
    browser is simply opened. Double-clicking the icon twice does not spawn a second
    pair of servers.

    The frontend runs as a production build by default, because that is what makes an
    icon-launched app feel instant. A stale build is detected by comparing BUILD_ID's
    timestamp against the newest file under frontend\src, so editing code and relaunching
    rebuilds rather than silently serving yesterday's page.

.PARAMETER Dev
    Run the frontend with `next dev` (hot reload, slower first paint) instead of a
    production build.

.PARAMETER Rebuild
    Force `next build` even when the existing build looks current.

.PARAMETER NoBrowser
    Start the servers but do not open a window. Useful when the app is already open.

.NOTES
    Paths are derived from this script's own location, so the launcher survives the
    project folder being moved — but the Desktop and Start Menu shortcuts do not.
    Re-run install-shortcuts.ps1 after moving anything.

    localhost only, and the backend has no authentication. Do not expose either port.
#>
[CmdletBinding()]
param(
    [switch]$Dev,
    [switch]$Rebuild,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$BackendDir = Join-Path $Root 'backend'
$FrontendDir = Join-Path $Root 'frontend'
$LogDir = Join-Path $PSScriptRoot 'logs'
$RunDir = Join-Path $PSScriptRoot 'run'
$IcoPath = Join-Path $PSScriptRoot 'ipo-copilot.ico'

$BackendPort = 8000
$FrontendPort = 3000
$Url = "http://localhost:$FrontendPort"

New-Item -ItemType Directory -Force -Path $LogDir, $RunDir | Out-Null

# ───────────────────────────────────────────────────────────────────── the splash
#
# WinForms rather than a console window: the whole point of the .vbs wrapper is that
# no black terminal sits on the desktop for as long as the app runs.

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$script:Splash = $null
$script:Status = $null

function Show-Splash {
    $form = New-Object System.Windows.Forms.Form
    $form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
    $form.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen
    $form.Size = New-Object System.Drawing.Size(440, 132)
    $form.BackColor = [System.Drawing.Color]::FromArgb(11, 18, 32)
    $form.TopMost = $true
    $form.ShowInTaskbar = $false

    if (Test-Path $IcoPath) {
        $picture = New-Object System.Windows.Forms.PictureBox
        $picture.Location = New-Object System.Drawing.Point(24, 34)
        $picture.Size = New-Object System.Drawing.Size(48, 48)
        # 48 explicitly: System.Drawing's icon reader predates 256 px entries and will
        # not return the largest image even when the file has one.
        $picture.Image = (New-Object System.Drawing.Icon($IcoPath, 48, 48)).ToBitmap()
        $form.Controls.Add($picture)
    }

    $title = New-Object System.Windows.Forms.Label
    $title.Text = 'IPO Copilot'
    $title.Font = New-Object System.Drawing.Font('Segoe UI', 13, [System.Drawing.FontStyle]::Regular)
    $title.ForeColor = [System.Drawing.Color]::FromArgb(226, 232, 240)
    $title.Location = New-Object System.Drawing.Point(92, 36)
    $title.Size = New-Object System.Drawing.Size(320, 26)
    $form.Controls.Add($title)

    $script:Status = New-Object System.Windows.Forms.Label
    $script:Status.Text = 'Starting...'
    $script:Status.Font = New-Object System.Drawing.Font('Segoe UI', 9)
    $script:Status.ForeColor = [System.Drawing.Color]::FromArgb(148, 163, 184)
    $script:Status.Location = New-Object System.Drawing.Point(94, 64)
    $script:Status.Size = New-Object System.Drawing.Size(330, 40)
    $form.Controls.Add($script:Status)

    $script:Splash = $form
    $form.Show()
    [System.Windows.Forms.Application]::DoEvents()
}

function Set-Status {
    param([string]$Text)
    if ($script:Status) {
        $script:Status.Text = $Text
        [System.Windows.Forms.Application]::DoEvents()
    }
}

function Close-Splash {
    if ($script:Splash) {
        $script:Splash.Close()
        $script:Splash.Dispose()
        $script:Splash = $null
    }
}

function Stop-WithError {
    param([string]$Message, [string]$LogFile)

    $detail = $Message
    if ($LogFile -and (Test-Path $LogFile)) {
        $tail = (Get-Content -Path $LogFile -Tail 12 -ErrorAction SilentlyContinue) -join [Environment]::NewLine
        if ($tail) { $detail = "$Message`r`n`r`nLast lines of $(Split-Path -Leaf $LogFile):`r`n$tail" }
    }
    Close-Splash
    [System.Windows.Forms.MessageBox]::Show(
        $detail, 'IPO Copilot could not start',
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Error) | Out-Null
    exit 1
}

# ─────────────────────────────────────────────────────────────────────── helpers

function Test-Port {
    param([int]$Port)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        # A connect with a short deadline, rather than Test-NetConnection, which takes
        # about a second per failed attempt and makes the poll loop feel sluggish.
        $handle = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        if (-not $handle.AsyncWaitHandle.WaitOne(300)) { return $false }
        $client.EndConnect($handle)
        return $true
    }
    catch { return $false }
    finally { $client.Close() }
}

function Wait-Port {
    param([int]$Port, [string]$What, [int]$TimeoutSeconds = 90)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $dots = 0
    while ((Get-Date) -lt $deadline) {
        if (Test-Port -Port $Port) { return $true }
        $dots = ($dots + 1) % 4
        Set-Status ("{0}{1}" -f $What, ('.' * $dots))
        Start-Sleep -Milliseconds 400
    }
    return $false
}

function Resolve-Tool {
    param([string]$Name, [string[]]$Fallbacks = @())
    $found = Get-Command $Name -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    foreach ($candidate in $Fallbacks) {
        if ($candidate -and (Test-Path $candidate)) { return $candidate }
    }
    return $null
}

function Start-Hidden {
    param([string]$FilePath, [string[]]$Arguments, [string]$WorkingDirectory, [string]$LogName)
    $process = Start-Process -FilePath $FilePath -ArgumentList $Arguments `
        -WorkingDirectory $WorkingDirectory -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $LogDir "$LogName.log") `
        -RedirectStandardError (Join-Path $LogDir "$LogName.err.log")
    Set-Content -Path (Join-Path $RunDir "$LogName.pid") -Value $process.Id
    return $process
}

function Test-BuildStale {
    $buildId = Join-Path $FrontendDir '.next\BUILD_ID'
    if (-not (Test-Path $buildId)) { return $true }
    $builtAt = (Get-Item $buildId).LastWriteTime
    foreach ($path in @(
            (Join-Path $FrontendDir 'src'),
            (Join-Path $FrontendDir 'package.json'),
            (Join-Path $FrontendDir 'next.config.ts'),
            (Join-Path $FrontendDir '.env.local'))) {
        if (-not (Test-Path $path)) { continue }
        $newest = Get-ChildItem -Path $path -Recurse -File -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($newest -and $newest.LastWriteTime -gt $builtAt) { return $true }
    }
    return $false
}

function Open-AppWindow {
    # A chrome-less window, so it reads as an application rather than a browser tab.
    # Falls back through Edge to the default browser, which still works, just with an
    # address bar.
    #
    # The dedicated --user-data-dir matters more than it looks. Without it, Chrome hands
    # the request to whatever instance already owns the default profile: the window opens
    # but belongs to the browser you were reading the news in, so quitting that takes the
    # app with it, your extensions run against the dashboard, and there is no process to
    # point at when something misbehaves. Its own profile makes the app a separate,
    # inspectable process. The cost is a folder of a few tens of MB and no shared logins,
    # neither of which matters for a localhost page.
    $profileDir = Join-Path $PSScriptRoot 'browser-profile'
    $browsers = @(
        (Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Google\Chrome\Application\chrome.exe'),
        (Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe'),
        (Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe')
    )
    foreach ($browser in $browsers) {
        if ($browser -and (Test-Path $browser)) {
            Start-Process -FilePath $browser -ArgumentList @(
                "--app=$Url",
                "--user-data-dir=$profileDir",
                '--no-first-run',
                '--no-default-browser-check',
                '--window-size=1500,950'
            )
            return
        }
    }
    Start-Process $Url
}

# ────────────────────────────────────────────────────────────────────────── main

Show-Splash

try {
    if ((Test-Port -Port $BackendPort) -and (Test-Port -Port $FrontendPort)) {
        Set-Status 'Already running - opening the dashboard'
        if (-not $NoBrowser) { Open-AppWindow }
        Start-Sleep -Milliseconds 700
        Close-Splash
        exit 0
    }

    # ── backend
    if (Test-Port -Port $BackendPort) {
        Set-Status 'Backend already listening on 8000'
    }
    else {
        $uv = Resolve-Tool -Name 'uv' -Fallbacks @(
            (Join-Path $env:USERPROFILE '.local\bin\uv.exe'),
            (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\uv.exe')
        )
        if (-not $uv) {
            Stop-WithError "uv is not on PATH, so the backend cannot start. Install it from https://docs.astral.sh/uv/ and try again."
        }
        Set-Status 'Starting the scheduling engine'
        Start-Hidden -FilePath $uv -WorkingDirectory $BackendDir -LogName 'backend' -Arguments @(
            'run', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$BackendPort"
        ) | Out-Null
    }

    # ── frontend
    if (Test-Port -Port $FrontendPort) {
        Set-Status 'Dashboard already listening on 3000'
    }
    else {
        $npm = Resolve-Tool -Name 'npm.cmd' -Fallbacks @(
            (Join-Path $env:ProgramFiles 'nodejs\npm.cmd'),
            (Join-Path $env:APPDATA 'npm\npm.cmd')
        )
        if (-not $npm) {
            Stop-WithError "npm is not on PATH, so the dashboard cannot start. Install Node.js and try again."
        }

        if ($Dev) {
            Set-Status 'Starting the dashboard (dev mode)'
            Start-Hidden -FilePath $npm -WorkingDirectory $FrontendDir -LogName 'frontend' -Arguments @(
                'run', 'dev', '--', '--port', "$FrontendPort"
            ) | Out-Null
        }
        else {
            if ($Rebuild -or (Test-BuildStale)) {
                # Synchronous: `next start` refuses to serve without a build, and a
                # first build takes long enough that the splash must say why.
                Set-Status "Building the dashboard - this takes a minute, only after code changes"
                $build = Start-Process -FilePath $npm -ArgumentList @('run', 'build') `
                    -WorkingDirectory $FrontendDir -WindowStyle Hidden -PassThru -Wait `
                    -RedirectStandardOutput (Join-Path $LogDir 'build.log') `
                    -RedirectStandardError (Join-Path $LogDir 'build.err.log')
                if ($build.ExitCode -ne 0) {
                    Stop-WithError "The dashboard failed to build (exit code $($build.ExitCode))." (Join-Path $LogDir 'build.log')
                }
            }
            Set-Status 'Starting the dashboard'
            Start-Hidden -FilePath $npm -WorkingDirectory $FrontendDir -LogName 'frontend' -Arguments @(
                'run', 'start', '--', '--port', "$FrontendPort"
            ) | Out-Null
        }
    }

    if (-not (Wait-Port -Port $BackendPort -What 'Waiting for the engine')) {
        Stop-WithError "The backend never started listening on port $BackendPort." (Join-Path $LogDir 'backend.err.log')
    }
    if (-not (Wait-Port -Port $FrontendPort -What 'Waiting for the dashboard')) {
        Stop-WithError "The dashboard never started listening on port $FrontendPort." (Join-Path $LogDir 'frontend.err.log')
    }

    Set-Status 'Opening the dashboard'
    if (-not $NoBrowser) { Open-AppWindow }
    Start-Sleep -Milliseconds 900
    Close-Splash
}
catch {
    Stop-WithError "Unexpected failure: $($_.Exception.Message)"
}
