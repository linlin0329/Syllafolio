# Build the Windows distribution

The build tool copies this checkout's application sources and configuration into a Windows x64 portable distribution. It reads the binary dependencies from an external runtime seed. A seed can be a verified Syllafolio Lite release ZIP or an extracted directory containing `_runtime` and `site-packages`.

## Portable ZIP

Run the build driver with Python 3.10 or later. The distributed interpreter is CPython **3.12.10 for Windows x64**. The tool verifies the 12,473 vendor files listed in `tools/runtime-lock.json` and generates the interpreter's path configuration for the packaged layout. The wheel names, versions, lengths and SHA-256 hashes for the 60 pinned distributions are in `tools/dependencies.lock.json`. Keep the seed outside the source tree.

```powershell
python -B tools/build_windows.py --runtime-seed ../release/Syllafolio-Windows-x64-Lite.zip --check-runtime-only
python -B tools/build_windows.py --runtime-seed ../release/Syllafolio-Windows-x64-Lite.zip --output ../new-build/Syllafolio-Windows-x64-Lite.zip --receipt ../new-build/receipt.json
Get-FileHash -Algorithm SHA256 ../new-build/Syllafolio-Windows-x64-Lite.zip
```

Use a new destination for each build. The ZIP and its receipt identify that build's bytes. The portable ZIP includes the current `src`, `configs` and acoustic import workflow, the English startup scripts and the verified runtime. The seed's application code and metadata are excluded. This procedure reuses compiled dependencies; it does not rebuild CPython or Qt from source.

After extraction, `Run_Checks.cmd --static-only` verifies installed files and imports. Running `Run_Checks.cmd` also opens and closes a blank Windows window. Use a separate test project to check real playback and catalogue delivery.

## Setup EXE

The installer uses the verified portable ZIP as its offline payload and adds a Windows GUI launcher. Build the launcher and Setup using the [Windows installer build instructions](tools/windows-installer/README.md). Publish checksums for the exact ZIP and EXE that you tested. The installer is intended for the current Windows user.
