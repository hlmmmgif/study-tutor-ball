' 启动答疑悬浮球（不显示黑色控制台窗口）
Option Explicit
Dim sh, fso, here, py, pyw, exe
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)

py = "C:\Users\helim\AppData\Local\Programs\Python\Python314\python.exe"
pyw = "C:\Users\helim\AppData\Local\Programs\Python\Python314\pythonw.exe"
exe = py
If fso.FileExists(pyw) Then exe = pyw
If Not fso.FileExists(exe) Then exe = py

sh.CurrentDirectory = here
sh.Run """" & exe & """ """ & here & "\tutor_ball.py""", 0, False
