' Trae AutoCheckin GUI launcher. Double-click to open the panel.
'
' Flow:
'   1. Locate python.exe / py.exe.
'   2. Start trae_gui.py with a hidden console (the tkinter window is still visible).

Option Explicit

Dim fso, sh, here, pyExe, pyArgs, guiPath, cmd

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")

here = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = here

guiPath = fso.BuildPath(here, "trae_gui.py")
If Not fso.FileExists(guiPath) Then
  MsgBox "Missing trae_gui.py. Put it next to this launcher:" & vbCrLf & vbCrLf & here, 48, "Trae AutoCheckin"
  WScript.Quit 1
End If

If Not FindPython(pyExe, pyArgs) Then
  MsgBox "Python was not found." & vbCrLf & vbCrLf & _
         "Install Python 3.8+ and enable 'Add Python to PATH'," & vbCrLf & _
         "then double-click this launcher again.", 48, "Trae AutoCheckin"
  WScript.Quit 1
End If

cmd = """" & pyExe & """ " & pyArgs & """" & guiPath & """"

' 0 = hidden console window; False = do not wait for exit.
sh.Run cmd, 0, False

' ------------------------------------------------------------------
' Find Python: common install paths first, then python / py on PATH.
' ------------------------------------------------------------------
Function FindPython(ByRef exePath, ByRef extraArgs)
  Dim bases, j, tmp, ts, line
  FindPython = False
  exePath = ""
  extraArgs = ""

  bases = Array( _
    sh.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python313\python.exe"), _
    sh.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python312\python.exe"), _
    sh.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python311\python.exe"), _
    sh.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python310\python.exe"), _
    sh.ExpandEnvironmentStrings("%ProgramFiles%\Python313\python.exe"), _
    sh.ExpandEnvironmentStrings("%ProgramFiles%\Python312\python.exe"), _
    sh.ExpandEnvironmentStrings("%ProgramFiles%\Python311\python.exe"), _
    sh.ExpandEnvironmentStrings("%ProgramFiles%\Python310\python.exe"), _
    "C:\Python313\python.exe", "C:\Python312\python.exe" _
  )
  For j = 0 To UBound(bases)
    If fso.FileExists(bases(j)) Then
      exePath = bases(j)
      FindPython = True
      Exit Function
    End If
  Next

  ' Non-standard installs such as Store, winget, or virtual environments.
  tmp = fso.BuildPath(fso.GetSpecialFolder(2), "traegui_where_py.txt")
  On Error Resume Next
  sh.Run sh.ExpandEnvironmentStrings("%ComSpec%") & " /c where python > """ & tmp & """ 2>nul", 0, True
  On Error GoTo 0

  If fso.FileExists(tmp) Then
    On Error Resume Next
    Set ts = fso.OpenTextFile(tmp, 1, False)
    If Err.Number = 0 Then
      Do While Not ts.AtEndOfStream
        line = Trim(ts.ReadLine)
        If Len(line) > 0 And fso.FileExists(line) Then
          exePath = line
          FindPython = True
          Exit Do
        End If
      Loop
      ts.Close
    End If
    Err.Clear
    fso.DeleteFile tmp, True
    On Error GoTo 0
  End If

  If FindPython Then Exit Function

  ' Last resort: the Python launcher.
  tmp = fso.BuildPath(fso.GetSpecialFolder(2), "traegui_where_pylauncher.txt")
  On Error Resume Next
  sh.Run sh.ExpandEnvironmentStrings("%ComSpec%") & " /c where py > """ & tmp & """ 2>nul", 0, True
  On Error GoTo 0
  If fso.FileExists(tmp) Then
    On Error Resume Next
    Set ts = fso.OpenTextFile(tmp, 1, False)
    If Err.Number = 0 Then
      Do While Not ts.AtEndOfStream
        line = Trim(ts.ReadLine)
        If Len(line) > 0 And fso.FileExists(line) Then
          exePath = line
          extraArgs = "-3 "
          FindPython = True
          Exit Do
        End If
      Loop
      ts.Close
    End If
    Err.Clear
    fso.DeleteFile tmp, True
    On Error GoTo 0
  End If
End Function
