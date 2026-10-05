; la musica's NSIS installer/uninstaller hooks.
;
; Why they exist: the shell (`mlo-desktop.exe`) spawns the bundled backend
; (`mlo-server.exe`) as a CHILD process, and Windows does not end a child with
; its parent. Tauri's own `CheckIfAppIsRunning` only knows the shell, so on
; uninstall the backend kept running and held its own files in
; %LOCALAPPDATA%\la musica\mlo-server locked: the uninstaller could not delete
; them and left the backend tree — a running server — behind in AppData.
;
; Two things are needed for the uninstall to actually finish:
;
;  * stop the backend, and WAIT for it to be gone. A "kill and return" is not
;    enough: the uninstaller starts deleting the moment this hook returns, and
;    a process still tearing down keeps python3XX.dll and the other mapped
;    modules locked, so those files survived even though the kill was issued.
;    The shell is stopped FIRST because a live supervisor respawns a killed
;    child within a few seconds; both are polled until `FindProcessCurrentUser`
;    stops seeing them. When no backend is running the shell is left alone, so
;    Tauri's normal "close the running app" prompt still guards a plain
;    uninstall.
;  * remove the backend tree recursively. The template's per-file delete list
;    only knows the files THIS build installed; a bundle whose hashed assets
;    (or Python version) changed left the previous version's copies behind, and
;    the final `RMDir "$INSTDIR\mlo-server"` could not empty the folder. Nothing
;    user-owned lives under it: state in AppData is config.json, shell.json,
;    logs and server/data, all BESIDE this folder (backend_launcher redirects
;    mlo.paths before the engine is imported).

!macro MLO_STOP_BACKEND
  nsis_tauri_utils::FindProcessCurrentUser "mlo-server.exe"
  Pop $R0
  ${If} $R0 = 0
    nsis_tauri_utils::KillProcessCurrentUser "mlo-desktop.exe"
    Pop $R0
    nsis_tauri_utils::KillProcessCurrentUser "mlo-server.exe"
    Pop $R0
    StrCpy $R1 0
    ${Do}
      Sleep 250
      nsis_tauri_utils::FindProcessCurrentUser "mlo-server.exe"
      Pop $R0
      nsis_tauri_utils::FindProcessCurrentUser "mlo-desktop.exe"
      Pop $R2
      ${If} $R0 != 0
      ${AndIf} $R2 != 0
        ${ExitDo}
      ${EndIf}
      IntOp $R1 $R1 + 1
    ${LoopWhile} $R1 < 40
  ${EndIf}
!macroend

!macro NSIS_HOOK_PREINSTALL
  !insertmacro MLO_STOP_BACKEND
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  !insertmacro MLO_STOP_BACKEND
  ${If} $INSTDIR != ""
    RMDir /r "$INSTDIR\mlo-server"
  ${EndIf}
!macroend