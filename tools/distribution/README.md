# Syllafolio Windows x64 1.0.1

Syllafolio's original application code is licensed under the [MIT License](LICENSE). Copyright (c) 2026 Zhaocun Lin. Bundled third-party components retain their respective licenses.

Fully extract this package, then double-click **Start.cmd**. It opens an empty workspace. No audio samples, demonstration projects, user projects or application acceptance datasets are included. Python and the fixed dependencies are private to this package; no Python/Conda installation or network download is needed.

Use **Project → New project from syllables…** to import segmented mono or stereo audio in WAV, FLAC, MP3, Ogg Vorbis or AIFF (`.aif`/`.aiff`) format, or **Open project…** for an existing, normally closed project copy. All files in a batch must use the same sample rate of at least 32000 Hz. Each segment can be up to 30 seconds long. Syllafolio averages the left and right channels of stereo files for listening and analysis at the original sample rate. `Start.cmd --project "FULL_PROJECT_PATH"` explicitly opens a project; `--blank` is the same as the default empty startup. Preserve older installations and original projects. Make an official offline project backup before migration.

For one recording with an existing annotation table, choose **Project → New project from recording and annotations…**. Select the recording and a text TextGrid (long or short format), Audacity region-label text file, or single-recording Raven selection table. Choose the intended TextGrid IntervalTier, preview the intervals and audition selections, then continue to the project purpose/operator form. The whole recording may exceed 30 seconds; each imported syllable interval must meet the 30-second limit. External labels are retained only as source references and do not create human families or saved decisions.

New projects can optionally use **Band-pass filter** during import. It is off by default; the starting settings are **500–15000 Hz**, Butterworth order **4**, and are adjustable. Enabling it filters the complete source before slicing, and both audition and features use the filtered PCM. Original source files are kept unchanged. This import option changes project audio; **View → Display settings…** changes only its presentation.

The sidebar contains **Candidate Groups**, **Global pool** and **Family reviews**. Right-click Candidate Groups to open candidate organization. Workspace contains **Final checks**, **Type definitions and directed review**, **Internal assessment** and **Catalogue delivery**. Recording sources and checkpoints/history are in Project. **View → Tasks…** remains available with no active tasks.

**Run_Checks.cmd** checks this package's integrity, dependency imports and a blank Windows window. It does not run the omitted material acceptance suite or certify audibility, annotation decisions or the complete scientific workflow. `Run_Checks.cmd --static-only` skips the window. The command prints the actual report location, including any fallback used for a read-only installation. **Record_Feedback.cmd** records only checks you actually performed.

The current-run report is `self_check.json`; the optional blank-window result is `blank_startup.json` in the same directory.

See [USER_GUIDE.md](USER_GUIDE.md). Versions and identities are in BUILD_INFO.json, APP_SOURCE_MANIFEST.json and INTEGRITY.json. RUNTIME_PROFILE.json describes the runtime subset. Required rendering/audio backends, FFmpeg, scientific runtime resources and third-party license texts are retained. This package is an application runtime, not a general Qt SDK.
