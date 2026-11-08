void -> Windows installer, all from the terminal

1. Put your page in this folder and name it  original.html
2. Open PowerShell in this folder (Shift + right-click -> "Open PowerShell window here")
3. Run:
     powershell -ExecutionPolicy Bypass -File .\build-all.ps1
   It installs the C++ build tools, Rust, Python and the Tauri tool if missing,
   then builds the app. Accept the admin (UAC) prompt when it appears.
   If it says to open a new terminal, do that and run the same command again.
4. When it finishes, the installer folder opens. Double-click Glassplayer_..._setup.exe

Uses only your Windows system WebView2 - no Edge or other browser is opened.
or use the installer easy know?
