# Syllafolio

Syllafolio is a Windows desktop application for listening to syllables, comparing spectrograms, annotating human-defined families and exporting a catalogue. Its interface, installer, system messages and new catalogue reports use English. Project names, family names, filenames and operator notes retain their original language.

## Install on Windows x64

Open the **v0.1.0-lite1** pre-release in this repository's **Releases** section. Download **Syllafolio-Setup-0.1.0-lite1-x64.exe** and compare its SHA-256 with the entry in **SHA256SUMS.txt**.

```powershell
Get-FileHash -Algorithm SHA256 .\Syllafolio-Setup-0.1.0-lite1-x64.exe
```

Double-click the installer, choose **Install**, and open **Syllafolio** from the Start menu. The installation is for the current Windows account. You can also download the portable ZIP, extract the whole folder and double-click `Start.cmd`.

## Review a project

1. Choose **Project → New project from syllables…** and select your segmented mono WAV input. To inspect existing work, choose **Open project…** and select a properly closed project copy.
2. Select a scope in **Candidate Groups**, **Global pool** or **Family reviews**, then click **Prepare group**.
3. Use **Play** to hear the page once or **Loop** to repeat it. Click a syllable to listen to it alone, or Ctrl-click several syllables to compare them. **Fit** restores the full page and frequency range.
4. Select a human family and edit any exceptions. **Save and next** saves the current item and advances. **Save page** submits all eligible pending items on that page.
5. Choose **Project → Export saved records…** for JSON, or **Workspace → Catalogue delivery** for a catalogue with a manifest and an offline report.

See the [user guide](tools/distribution/USER_GUIDE.md) for type definitions, checkpoints, source declarations and internal assessment.

## Run the source checkout

Use Windows x64 with CPython 3.12.10. Run these commands from the repository root:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --only-binary=:all: --require-hashes -r requirements.txt
.\.venv\Scripts\python.exe -X utf8 run.py
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests
```

The tests generate a signal in memory to exercise acoustic feature extraction. See [BUILDING.md](BUILDING.md) to rebuild the Windows distribution.
