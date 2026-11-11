# Syllafolio Windows x64 Lite 0.1.0-lite1

Fully extract this package, then double-click **Start.cmd**. It opens an empty workspace. No audio samples, demonstration projects, user projects or application acceptance datasets are included. Python and the fixed dependencies are private to this package; no Python/Conda installation or network download is needed.

Use **Project → New project from syllables…** to import your own segmented mono WAV files, or **Open project…** for an existing, normally closed project copy. `Start.cmd --project "FULL_PROJECT_PATH"` explicitly opens a project; `--blank` is the same as the default empty startup. Preserve older installations and original projects. Make an official offline project backup before migration.

The sidebar contains **Candidate Groups**, **Global pool** and **Family reviews**. Right-click Candidate Groups to open candidate organization. Workspace contains **Final checks**, **Type definitions and directed review**, **Internal assessment** and **Catalogue delivery**. Recording sources and checkpoints/history are in Project. **View → Tasks…** remains available with no active tasks.

**Run_Checks.cmd** checks this package's integrity, dependency imports and a blank Windows window. It does not run the omitted material acceptance suite or certify audibility, annotation decisions or the complete scientific workflow. `Run_Checks.cmd --static-only` skips the window. The command prints the actual report location, including any fallback used for a read-only installation. **Record_Feedback.cmd** records only checks you actually performed.

See [USER_GUIDE.md](USER_GUIDE.md). Versions and identities are in BUILD_INFO.json, APP_SOURCE_MANIFEST.json and INTEGRITY.json. LITE_PROFILE.json describes the runtime subset. Required rendering/audio backends, FFmpeg, scientific runtime resources and third-party license texts are retained. This package is an application runtime, not a general Qt SDK.
