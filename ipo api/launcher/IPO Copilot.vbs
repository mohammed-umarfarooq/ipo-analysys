' Hidden wrapper for launch.ps1.
'
' A shortcut straight to powershell.exe leaves a console window on screen for as long
' as the app runs, and -WindowStyle Hidden still flashes one on creation. WScript with
' a window style of 0 never creates one at all, which is why this file exists.
'
' -ExecutionPolicy Bypass applies to this launch only; it changes no machine policy.

Option Explicit

Dim shell, here, command
Set shell = CreateObject("WScript.Shell")
here = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & here & "launch.ps1"""
shell.Run command, 0, False
