# Windows installer

Run the following from the repository root on Windows x64. Use PowerShell, Python 3.10 or later for the build driver, and the local 64-bit .NET Framework C# compiler. Keep generated executables outside the source tree.

1. Prepare the Windows portable ZIP and its matching build receipt as described in [BUILDING.md](../../BUILDING.md).
2. Verify the payload against that receipt, then compile the Windows GUI launcher and build Setup. Use new output and work directories for each build. The expected payload SHA-256 argument is required.

```powershell
New-Item -ItemType Directory ../installer-build -ErrorAction Stop
$payload = (Resolve-Path ../new-build/Syllafolio-Windows-x64.zip).Path
$payloadReceipt = Get-Content ../new-build/receipt.json -Raw | ConvertFrom-Json
$payloadFile = Get-Item -LiteralPath $payload
$payloadSha = (Get-FileHash -LiteralPath $payload -Algorithm SHA256).Hash.ToLowerInvariant()
if ($payloadReceipt.status -ne 'passed' -or $payloadReceipt.version -ne '1.0.1' -or $payloadReceipt.sha256 -ne $payloadSha -or [long]$payloadReceipt.bytes -ne $payloadFile.Length) {
    throw 'The portable ZIP does not match its successful 1.0.1 build receipt.'
}
.\tools\windows-installer\launcher\build.ps1 -Output ../installer-build/Syllafolio.exe
python -B tools/windows-installer/tools/build_installer.py --payload $payload --expected-payload-sha256 $payloadSha --launcher ../installer-build/Syllafolio.exe --icon tools/windows-installer/branding/Syllafolio.ico --work ../installer-build/obj --output ../installer-build/Syllafolio-Setup-1.0.1-x64.exe --receipt ../installer-build/build-receipt.json
Get-FileHash -Algorithm SHA256 ../installer-build/Syllafolio-Setup-1.0.1-x64.exe
```

The receipt-verified portable ZIP is embedded unchanged. For every build, review its contents and pass its actual verified digest using `--expected-payload-sha256`; there is no default payload digest. Setup verifies the payload, extracts the registered files, and adds the GUI launcher, an uninstall entry and Start menu shortcuts. The GUI launcher uses the installed private runtime. The checked-in icon can be regenerated with `branding/make_icon.py` and Pillow.

For testing, install into a new folder and close Syllafolio before uninstalling. Setup registers the program for the current account. The uninstaller verifies the installation, removes matching program files, and preserves user-added and modified files.

Setup selects **Create a desktop shortcut** by default. Clear it before choosing **Install**, or pass `--no-desktop-shortcut` for an unattended installation. Setup uses the current account's Windows desktop folder. If the shortcut name is taken, it uses a numbered name. Uninstall removes the desktop shortcut it created if it is still in place and unchanged.

To check dialog layout without installing, run the following on a Windows desktop. Compile the probe with Setup's manifest so that text and control sizes are measured at the monitor's actual DPI. The optional second argument checks the expected DPI (for example, `192` at 200%).

```powershell
$compiler = "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
$manifest = (Resolve-Path tools/windows-installer/installer/installer.manifest).Path
$test = (Resolve-Path tools/windows-installer/tests/WizardLayoutCheck.cs).Path
$probe = Join-Path (Resolve-Path ../installer-build).Path 'WizardLayoutCheck.exe'
& $compiler /nologo /target:exe /platform:x64 "/win32manifest:$manifest" "/out:$probe" /reference:System.Drawing.dll /reference:System.Windows.Forms.dll $test
& $probe ..\installer-build\Syllafolio-Setup-1.0.1-x64.exe
```
