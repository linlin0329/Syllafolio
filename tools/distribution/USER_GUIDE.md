# Syllafolio user guide

Version **1.0.2** uses the supplied application source and hash-locked dependencies. It starts with an empty workspace and includes no business samples, demonstration projects or acceptance datasets. Build evidence and actual Windows acceptance are separate. Audibility, human type criteria and scientific validity require the actual user to check them.

## Start and use your own project

Fully extract the package and run **Start.cmd**. In Project, create a new project or open an existing project copy. **New project from syllables…** accepts a folder of segmented audio. **New project from recording and annotations…** accepts one recording with an annotation table defining the syllable intervals.

Both routes accept mono or stereo WAV, FLAC, MP3, Ogg Vorbis and AIFF (`.aif`/`.aiff`) at 32,000 Hz or higher. Stereo input is mixed to mono by averaging the left and right samples at each frame. The preview, optional band-pass filter, spectrogram and acoustic features use that mix at the original sample rate. If the two channels contain separate microphone signals and you want to annotate just one, export that channel as a mono file before import.

Segmented files in one batch must share the same sample rate. Each imported syllable interval can be up to 30 seconds long, while a whole recording can be longer. For segmented input, use segments.json to declare parent recordings and frame boundaries explicitly. Unknown origins remain unknown; separate audio files are not automatically independent recordings.

Import freezes the input and computes the fixed features. It does not assign scientific human types. Use the actual operator for a daily project; retain the engineering designation for engineering work. Store projects separately from original audio. Close normally before backing up the complete project. Do not replace a project's database with another project's database or overwrite an older installation.

## Import one recording with annotations

Choose **Project → New project from recording and annotations…**, then select one audio recording and one of these annotation files:

- **Praat TextGrid:** exported text in long or short format. Select the intended **IntervalTier**. Blank intervals are excluded by default; include them only if you intend to review them as syllables. Point tiers do not define syllable intervals.
- **Audacity labels:** tab-separated regions with start and end times in seconds; label text is optional. Point labels are skipped rather than converted into syllables.
- **Raven selections:** a selection table for the chosen recording only. Start and end times are sufficient. For a stereo recording, selections may refer to channel 1 or 2. Preview and review use the stereo mix. Rows for the same selection with matching boundaries across views or channels represent one interval.

For Audacity and Raven, the start and end times define the segment. Table frequency bounds do not crop the audio. With the optional band-pass filter off, audition keeps the recording's full frequency band.

An empty label remains a valid region in Audacity and Raven tables; the TextGrid blank-interval option does not filter these regions.

Run the preview and check that the table belongs to the chosen recording. Inspect the interval count, time boundaries and reference labels, then select rows to inspect and audition their audio. Review the refreshed preview after changing files, tier or selection options. Resolve validation errors before continuing; a preview is not a created project or a saved annotation decision.

Confirm the preview, choose a new project directory, and complete the existing purpose/operator form. The application freezes the selected input and runs the fixed feature pipeline. Keep the original recording and table as your source archive. External labels remain source references: they do not create human families, assign syllables or complete reviews.

With a small input such as the optional three-chirp example, the syllables may all enter **Global pool**. Right-click **Candidate Groups → Open candidate workspace…**, preview and explicitly apply the organization, then select **Global pool → Prepare group** to open the audition pages. An empty candidate-group list does not mean that the import failed.

## Optional import band-pass filtering

**Band-pass filter** is off by default in both new-project routes. If enabled, its starting settings are a **500 Hz low cutoff**, **15000 Hz high cutoff** and Butterworth design order **4**. Both cutoffs are adjustable; order can be **1–8**. The low cutoff must be above zero and below the high cutoff, and the high cutoff must be below half the audio sample rate. Invalid settings must be corrected before import.

For **New project from syllables…**, choose the option in the input-review dialog before selecting **Import this input**. The complete declared parent source is filtered before its syllable intervals are cut. Where no larger parent source was provided, the complete syllable file is the source.

For **New project from recording and annotations…**, choose the settings before running the preview. Changing the filter invalidates the old preview; run it again and check the selected audio before creating the project. Filtering applies to the complete decoded recording, not separately to each selected interval. Original recording files and annotation tables remain unchanged.

When the option is on, the project freezes filtered PCM, and audition, review spectrograms and acoustic features use that PCM. Its recorded settings are shown in **Help → Project details…**. Existing projects are not silently reprocessed; create a new project from the originals to use a different import filter.

This is audio preprocessing, separate from **View → Display settings…** and from frequency bounds written in an external annotation table. Display changes do not alter PCM; table frequency bounds do not apply an audio filter. Band-pass filtering does not assign families or confirm human decisions.

## Listen, inspect and save

Right-click **Candidate Groups → Open candidate workspace…**. Preview first, then explicitly apply the selected organization. After a parameter or scope change, review a fresh preview and its actual impact. Machine groups organize listening; they do not establish human types.

Expand Candidate Groups, choose a group and click **Prepare group**, then select a page. Select a human family under **Family reviews** and prepare it in the same way. Right-click to create a family; Rename or F2 changes its display name. Continue an existing valid round when available. When items overlap another formal round, distinguish the read-only listening preview from an editable review.

For a new round under **Family reviews → Prepare group**, families of 1–10 syllables play in stable ID order. For 11–30 syllables, the application orders all members by acoustic similarity and divides that sequence into consecutive pages. For 31 or more, it clusters the family first and prepares pages within each cluster, retaining unclustered syllables for review. All three use the time scale and automatic or fixed page settings below. Changing the window size can change pagination for a new round; the family's listening order and acoustic groups depend on its members.

**Play** plays the current range; **Loop** repeats it. The active button becomes Pause. With no selection, play the page; with one item, play that item; use Ctrl to add or remove items from a selection. Multiple items play in page order. Selection or page changes stop the previous stream. **View → Display settings…** sets waveform Auto or a fixed symmetric range and shared spectrogram settings. The defaults are 45 dB, Hann 512 and hop 96. Display changes do not change PCM or assignments.

In **View → Display settings…**, choose a **Time scale** for new review pages, in milliseconds per logical pixel. A smaller value makes a syllable wider. At 5 ms/pixel, for example, a 300 ms syllable spans 60 pixels on any page of the round. **Automatic** pagination fits whole syllables, their silent gaps and their labels to the available width when you prepare a group. **Fixed count** sets the maximum number per page (1–40).

Apply the settings before preparing a new round. The round saves its pages and scale so you can return to the same syllables and drafts. A short page leaves blank space after the audio. Use **Fit** to return to the round's scale after zooming. When the audio extends past the visible area, scroll horizontally or choose **Show entire page** for an overview. Rounds prepared with an earlier version retain their original pages and viewing behavior.

Choose the current human family for review. A star marks a pending decision, not a saved judgment. Select one item to change Outcome. Other family requires an existing target; New family requires a shared basis. Pool/discard decisions use an explicit reason category. Routine notes are optional. **Save and next** saves one item. The adjacent menu also offers Save item. **Save page** submits the page's eligible pending items. Listening, selecting or restoring a draft does not submit a decision. Directed review has additional evidence and confirmation requirements.

## The current pool and new members

Select **Global pool → Prepare group** to organize the current non-empty pool using the fixed acoustic route. Eligible items can be saved before all candidate first rounds finish. Real overlap locks only the corresponding stable IDs. An item saved out of the pool remains readable in its original frozen round.

Items sent into the pool later do not silently alter an open frozen page. Use **More → Prepare again…**, give a reason, and explicitly prepare the latest whole pool. Historical records remain available. Pool details shows provenance and coverage. A temporarily empty pool or one closed early pool round does not prove final completion.

## Checks, sources, assessment and delivery

Workspace contains **Final checks**, **Type definitions and directed review**, **Internal assessment** and **Catalogue delivery**. Declare the scope in Final checks and locate missing requirements. Type cards require human criteria and selected examples; draft is not reviewed. Check the complete affected family scope before explicitly preparing a directed issue. It does not automatically reassign items.

**Project → Recording sources…** registers actual raw-recording mappings. Validate, inspect conflicts/impact, then explicitly register. Internal assessment uses the fixed protocol and fold-local processing. Balanced accuracy and macro-F1 describe internal acoustic separability of the current human labels. A complete not_estimable report preserves the limits when support is insufficient; do not substitute old scores or silently rerun.

**Project → Export saved records…** exports saved JSON and does not declare completion. In **Catalogue delivery**, explicitly choose Draft or Complete and an assessment version, generate the job, then save its ZIP/manifest or open offline HTML. Complete requires current scope/definition checks and a matching complete report. Missing, failed, cancelled and stale jobs do not count as completion. Keep the report with its CSS, PNG and WAV resources.

## Drafts, history, tasks and closing

Matching ordinary page drafts reconnect as unsubmitted input. If a draft is damaged, preserve the original and follow the displayed error. **Project → Checkpoints and history…** restores submitted label checkpoints while preserving subsequent history; this differs from form drafts.

**View → Tasks…** is available even with no active jobs. The top status entry appears for active work, new failures or close waiting. A cancellation request and a settled worker are different states. On close or project switch, choose wait, cancel or return; the owner is released after work and resources settle. Do not force-kill the application as a substitute for saving.

## Self-checks, diagnostics and feedback

**Run_Checks.cmd** checks package integrity, imports and a blank native Windows startup. It creates no business project, runs no omitted material suite and makes no audibility claim. `--static-only` skips the window. run_p1_checks.py explains that the acceptance suite is absent and returns nonzero; it must not be reported as that suite passing.

Startup and checks print the actual current-run diagnostic path. Cache, reports and UI settings use validated writable locations. Consult the receipt rather than assuming reports are under the installation folder. **Help → Project details…** shows project identity and location. Record_Feedback.cmd records actual checks and diagnostics; unperformed listening, viewing or editing remains untested.

The primary self-check report is `self_check.json`. A blank-window startup check also writes `blank_startup.json` and `blank_startup.log` in that run. `RUNTIME_PROFILE.json` describes the shipped runtime subset; it is separate from a user project or a scientific result.

Qt is trimmed for this application. Graphics/audio backends, FFmpeg, required runtime code, scientific resources and license texts are retained; application samples and clear third-party test sample data are omitted. Package checks do not replace human listening, type judgments or a second independent environment test.
