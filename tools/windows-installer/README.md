# Windows installer

Run the following from the repository root on Windows x64. Use PowerShell, Python 3.10 or later for the build driver, and the local 64-bit .NET Framework C# compiler. Keep generated executables outside the source tree.

1. Prepare the Windows portable ZIP as described in [BUILDING.md](../../BUILDING.md). Compare it with the SHA-256 in its release manifest.
2. Compile the Windows GUI launcher and build Setup. Use new output and work directories for each build.

```powershell
New-Item -ItemType Directory ../installer-build -ErrorAction Stop
.\tools\windows-installer\launcher\build.ps1 -Output ../installer-build/Syllafolio.exe
python -B tools/windows-installer/tools/build_installer.py --payload ../release/Syllafolio-Windows-x64-Lite.zip --launcher ../installer-build/Syllafolio.exe --icon tools/windows-installer/branding/Syllafolio.ico --work ../installer-build/obj --output ../installer-build/Syllafolio-Setup-0.1.0-lite1-x64.exe --receipt ../installer-build/build-receipt.json
Get-FileHash -Algorithm SHA256 ../installer-build/Syllafolio-Setup-0.1.0-lite1-x64.exe
```

The reviewed portable ZIP is embedded unchanged. If you build a different ZIP from an edited checkout, review its contents, then pass its actual digest using `--expected-payload-sha256`. Setup verifies the payload, extracts the registered files, and adds the GUI launcher, an uninstall entry and Start menu shortcuts. The GUI launcher uses the installed private runtime. The checked-in icon can be regenerated with `branding/make_icon.py` and Pillow.

For testing, install into a new folder and close Syllafolio before uninstalling. Setup registers the program for the current account. The uninstaller verifies the installation, removes matching program files, and preserves user-added and modified files.
