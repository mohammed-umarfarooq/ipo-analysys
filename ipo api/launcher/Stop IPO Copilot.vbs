' Hidden wrapper for stop.ps1. See "IPO Copilot.vbs" for why WScript rather than a
' direct shortcut to powershell.exe.

Option Explicit

Dim shell, here, command
Set shell = CreateObject("WScript.Shell")
here = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & here & "stop.ps1"""
shell.Run command, 0, False
