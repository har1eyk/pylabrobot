# Pure-dye calibration

`calculate_pure_dye_calibration()` calculates a single dye's five-channel relative spectrum
from final fixed-exposure camera collections. It retains original frames, ROI masks,
background provenance, integer signals, normalized spectra and correction decisions.
No vendor runtime or instrument I/O is used by the calculation.

```{warning}
This remains experimental. One archived ROX calibration on C9254-02/G2.10 was replayed from
65 original frames: all 480 saved binary32 values matched the vendor's `puredye.ini` exactly.
One fresh Ubuntu session also completed all 65 fixed-exposure images, cooling and plate release.
Its calculation passed the recovered spectral checks, but A10 and B10 exceeded the saturation
threshold in the main ROX channel and remained unreplaced, so PLR refused calibration-file
export. Automatic exposure selection reproduces all 35 archived vendor probes and all five
chosen exposures offline. A fresh automatic scan using the cover-preserving Start completed
35 probes and 65 final images with the cover at 89.3–90.5 °C during final imaging. No final
well exceeded the saturation threshold, and PLR generated a ROX interchange file after correcting
G1. A fresh 160 nM fluorescein scan also passed all spectral and export checks with no
flagged or substituted wells, using PLR-generated masks and matching background.
Cross-run vendor agreement, additional dyes and assay performance remain unverified.
See the [hardware evidence](hardware-validation.md).
```

## Inputs and processing

Supply exactly one `PureDyeCalibrationFilter` per physical filter, each containing one or
sixteen `FluorescenceFrame` objects at the same requested exposure and using the same mask.
The expected count is explicit so a partial collection cannot appear complete. Use only the
final fixed-exposure collections; exclude startup and exposure-search images.

The processing path crops OPB=0 camera data, rotates into the verified mask coordinates,
extracts well and ring sums, averages ring-subtracted signals before truncation, then applies
native background offsets/slopes and effective integration timing. Physical filters 1–5 map
to saved channels 5–1. Each well's spectrum is divided by its strongest channel. Existing
uniformity coefficients are not inputs to this calculation. Off-peak zeros are valid.

The vendor has two different quality-processing paths:

- Native acquisition screening iterates its spectral-outlier test. In the reference it flagged
  G1 and B10, matching the vendor log.
- The Java calculation that saves `puredye.ini` makes one pass. It flagged and replaced G1 only;
  B10 remained unchanged in the saved file. Replacement averages the neighbors' normalized
  binary32 spectra using binary64 accumulation and rounds once to binary32.

PLR reports both decisions. It does not silently equate a native correction message with a
changed value in the saved Java calibration. Four or fewer flagged wells need at least two good
neighbors each. PLR additionally rejects a calculation when native iterative screening finds
more than four bad wells. All-zero spectra retain an empty flag and finite zeros before repair.

Clipping flags remain attached to the source collections. The reference had clipping at G1
in saved channel 1; the primary ROX channel 4 was not clipped. A numerical pass with unreplaced
clipping cannot be serialized as an accepted calibration.

## Offline calculation

```python
from pathlib import Path
import json
from pylabrobot.thermo_fisher.abi7500_fast_dx import (
  PureDyeCalibrationFilter,
  calculate_pure_dye_calibration,
)

# final_frames_by_filter contains previously saved FluorescenceFrame objects,
# using explicitly associated ROI masks. background is a BackgroundCalibration.
expected_counts = {1: 1, 2: 16, 3: 16, 4: 16, 5: 16}
filters = tuple(
  PureDyeCalibrationFilter(tuple(final_frames_by_filter[f]), expected_counts[f])
  for f in range(1, 6)
)
result = calculate_pure_dye_calibration(
  filters, background, dye="ROX", instrument_id=instrument_serial,
  scanner_id=camera_serial, timestamp=acquisition_timestamp_ms, temperature=60,
)
Path("report.json").write_text(json.dumps(result.report(), indent=2, allow_nan=False))
# Raises on failed processing or unreplaced clipping. Keep full inputs alongside this file.
Path("plr-rox.ini").write_bytes(result.to_ini())
```

`to_ini()` generates a single-dye interchange file with the supplied identities. It does not
merge dyes, overwrite the control PC's files, or certify the instrument. Full source data and
calibrations belong outside Git. This is not spectral unmixing, Rn, ΔRn, Ct or concentration.
Use the separate [spectral measurement tools](spectral-measurements.md) to apply selected
pure-dye spectra to corrected filter measurements and calculate a guarded reporter/reference ratio.

## Fixed-exposure acquisition

### Matching a captured camera cadence

`CollectionPlan(..., cadence=CollectionCadence(offsets, max_lateness=0.5))` opts fixed
calibration images into scheduled filter bursts. `offsets` is an immutable tuple of tuples:
one group per physical filter, one value per image, in seconds relative to the first
exposure's host `CC` submission. The first value must be zero and all values must increase.
Use offsets measured from a compatible vendor acquisition, together with its exact exposures;
cadence is not inferred from a dye name. Ordinary PCR brackets and exposure searches cannot
use this mode.

Each target uses the same monotonic time origin, avoiding accumulated delays. If a start
is more than `max_lateness` late, PLR records an optical failure before issuing that `CC`.
It does not compress the remaining schedule, extend the thermal hold or retry an image.
The tolerance is positive and at most one second. It bounds host submission lateness,
not the camera's internal exposure trigger or the initial settling delay.

The driver verifies the run token, intended hold, cover and stationary illuminated filter
at burst boundaries. It also checks a conservative remaining-hold budget before every
exposure. Each original image is durable before the next one starts, but all images in a
filter remain uncertain until the final boundary observations pass. Partial images and
failed bursts remain retrievable; they cannot be resumed. No controller queries are sent
between images within a burst of at most sixteen frames.

Between filters, cadence mode reuses the preceding burst's verified final thermal status
to bound the next movement. Its query-start timestamp discounts time spent saving images;
an expired hold budget prevents movement. After motion, the successful final motor poll
supplies the stationary observation without immediately querying it again. Filter position,
lamp and thermal state are read afresh before the next burst. A collection whose first
filter is already selected still reads motor state afresh. These optimizations neither
increase the lateness tolerance nor change ordinary per-frame acquisition.

Within a filter burst, the downloaded image and the next exposure intent share one
FULL synchronous SQLite transaction, committed before the next camera command. The last
image is committed before final status queries; after those pass, all frames become
complete in one atomic transaction. When another filter follows, this final transaction
runs after its move acknowledgement and before motor polling, so persistence can overlap
physical movement. Raw bytes are already durable before that move. The movement deadline
includes the commit time; fresh motor, position, lamp and thermal checks still precede the
next exposure. The last burst commits before collection cleanup. A failed move or commit
retains the affected burst as uncertain and blocks further exposures and replay.
This uses 18 durable acquisition commits for sixteen images, without changing the journal
format or FULL synchronous mode. Ordinary per-frame acquisition retains its separate
checkpoints.

Timed calibration images use bounded 16 KiB host reads: a full 157,312-byte transfer takes
ten reads when responses fill the requested buffers, rather than 39 at 4 KiB. `CD`, the
requested image length and the original bytes are unchanged. Short and odd fragments are
assembled without reading beyond the image, using one shared transfer deadline. Cancellation
waits for an in-flight read and retains its returned bytes before propagating. Other image
acquisition paths retain 4 KiB reads. The larger reads and overlapping completion commit
have offline regression coverage; their hardware timing benefit remains unverified.

This deliberately changes observation frequency. Every frame is labelled
`observation_scope="filter_burst"` and shares the burst's before/after observations;
transient state changes inside that interval are not observed. `exposure_start_offset`
retains the actual host submission offset. Journal format 7 prevents older controllers
from silently treating these records as ordinary per-frame checks. Without a cadence,
the existing per-frame checks and legacy serialization remain unchanged.

Cadence control has offline transport/timing and fault tests but **has not been validated
on hardware**. A simulated replay or exported plan does not establish that a particular
computer can meet its targets. Audit the fresh USB `CC` timestamps, first-exposure settling
and all filter bursts in a separately approved comparison before claiming timing agreement.

### Fixed image groups

`CollectionFilter(..., mode="fixed")` supports one or sixteen identical exposures. Its durable
plan records every exposure identity before collection, and interrupted collections cannot be
resumed or repeated under that identity. `mode="bracket"` remains the default and requires three
different exposures for ordinary measurement processing. Existing journals retain their meaning.

A `CollectionPlan` can schedule fixed collections during a thermal hold with the same ownership,
window, parking and homing checks as bracketed collections. Obtain images using
`get_collections()`; the ordinary bracketed measurement `export_run()` deliberately rejects
fixed calibration collections. Preserve their acquisition metadata and original wire bytes.

The reference ROX final exposures were 1800, 720, 1719, 1800 and 558 ms in physical filter order,
with counts 1, 16, 16, 16 and 16 at 60 °C. These are observed settings for that plate and camera,
not general defaults or a substitute for automatic exposure selection. Follow the thermal-run
and synchronized-acquisition guides for setup, explicit run approval and cleanup.

The fresh Ubuntu check used these settings with the same plate, masks and background. Its
main ROX signal was a median 14.4% higher than the earlier vendor acquisition. The sample stayed
at 60.0 °C, but the cover was 104.4–105.5 °C, versus approximately 90 °C for the vendor
calibration. Separate acquisition times and illumination history also differ; the cause of the
brightness change is unresolved. Reusing recorded exposures does not replace checking saturation
on each new collection. `analysis.passed` describes the spectral checks; `to_ini()` also requires
that no saturated well remains unreplaced.

## Automatic exposure acquisition

`CalibrationExposureSearch` replays the native `OptimizeExposureTable` algorithm. It begins at
2000 ms, bisects against a 10 ms lower bound, and treats a probe as saturated if **any well** has
more than six ROI pixels above 4000. It rounds the selected safe bound down to 10 ms and applies
a 10% reduction. PLR refuses a search with no usable safe exposure. The native implementation
can produce zero in that case; PLR does not send a zero-duration acquisition.

The archived ROX search reproduced 1800/720/1719/1800/558 ms from its 35 original probes and ROI
masks. A fresh Ubuntu scan selected **1800/594/1593/1800/486 ms** with 35 probes and
completed all 65 final images without saturated wells. Its spectral calculation corrected G1
and passed the export guard. The exposure-corrected main ROX signal was a median 24.2% higher
than the earlier vendor run; the 480 corrected normalized values had RMS difference 0.01896.
Matching the 90 °C cover setting did not eliminate the cross-run discrepancy. Acquisition times,
illumination history and selected exposures also differ, so the cause remains unresolved.
This is one plate and one C9254-02/G2.10 instrument; later images can still saturate, and the
final-image export checks remain in force.

An immediate same-plate repeat also passed acquisition and export checks, selecting
1800/612/1800/1800/504 ms with 25 probes. Its main ROX signal was a median **5.02% lower** than
the preceding PLR scan, with per-well decreases of 1.64–10.23%, despite no final clipping in
either run. The repeat remained 18.55% above the earlier vendor acquisition. These two scans
establish observed variation, not a validated repeatability limit. The
[hardware evidence](hardware-validation.md) includes per-well comparisons, changes within
individual collections and the changing B10 exposure-search constraint.

A subsequent fixed-exposure stability session retained all three main ROX bursts before a local
journal-lock error interrupted the final filter. Main-channel ring-subtracted signal increased
by median **7.98% and 8.97%** in sets 2 and 3 versus set 1, with no saturation or intervening
wheel home. Fixed exposure alone therefore did not eliminate within-session drift. The third
filter-5 group is incomplete and excluded; the full session is not validated. The journal's
brief-reader contention fix passed offline tests; the updated driver completed the
longer-settling hardware run below without deliberately introducing a competing reader.
See the [hardware evidence](hardware-validation.md) for the partial-data boundaries and cleanup.

A separately approved repeat added a 780-second image-free hold at 60 °C before the first
collection hold's 120-second wait: **15 minutes of settling**, with lamp power 1 on behind
filter 0 and the cover at 90 °C. All **195 fixed-exposure images** completed. Main ROX signal
changed by median **+0.073% and −0.198%** in sets 2 and 3 versus set 1, substantially less
than the earlier 7.98% and 8.97%. All three five-filter numerical calculations passed with
G1 corrected. Weaker channels still declined by up to a median 2.00%; no calibration was
installed. This single comparison changes lamp warmup and thermal equilibration together,
so it supports longer settling for further validation without establishing a general default
or proving absolute vendor agreement. See the [longer-settling evidence](
hardware-validation.md#longer-lamp-and-thermal-settling-before-fixed-rox-collections-2026-10-19).

```python
from pylabrobot.thermo_fisher.abi7500_fast_dx import CollectionFilter, CollectionPlan

# masks contains the five explicitly associated PlatePixelMask objects, physical filters 1–5.
# Counts reproduce the reference ROX acquisition; they are explicit, not automatically inferred.
counts = (1, 16, 16, 16, 16)
plan = CollectionPlan(
  steps=((0, 0),),
  filters=tuple(
    CollectionFilter(f, (2.0,) * count, mode="automatic")
    for f, count in enumerate(counts, 1)
  ),
  read_window=600,
  park_between_collections=True,
  home_after_collections=(1,),
  exposure_search_masks=tuple(masks),
)
# For an approved, prepared session only: program must provide a sufficiently long 60 °C hold.
# await instrument.start_run(run_id, program, collection_plan=plan, cover_temperature=90)
# collections = await instrument.collect_fluorescence(run_id, timeout=1800)
```

The immutable plan stores complete mask bytes. Search probes use separate `-pN` identities;
final images use `-eN`. Each exposure is reserved before its command is sent. All filter searches
finish before final-image acquisition starts. `get_collections()` returns final images in
`acquisitions` and search images in `exposure_probes`, preserving incomplete data after failure.
Neither an interrupted search nor an uncertain final collection is automatically resumed.
Ordinary `export_run()` rejects automatic calibration collections, just as it rejects fixed ones.

`start_run(..., cover_temperature=90)` is limited to the captured calibration setpoint. Before
preparation and upload, PLR queries command `0x36`, writes `0x35` with `005a0001` only if needed,
and verifies the setpoint and mode. It verifies again before and after the vendor's
`StartMethodNoCoverSetpoint` command (`0x7E`). Standard runs retain command `0x13`.
The request and Start command are part of run identity; repeating the ID cannot reissue them.
A failed readback before Start prevents Start entirely. Failure after Start leaves an uncertain
owned run for reconciliation/cancellation, with collection blocked. There is no fallback Start.
Measured temperature from the established `0x29` status must stay within 88–92 °C at collection
and exposure boundaries. Allow settling time before the collection window. The 0x36 query was
checked on C9254-02/G2.10 at the 105 °C and 90 °C settings; its final measurement word remains exposed as
`ABI7500CoverState.measured_raw`, without assuming its units across the operating range.

The cover setting is not automatically restored on stop. It does not cool the cover immediately.
Omitting it retains the existing run behavior. Journals using automatic search or an explicit
cover setting require journal format 3; the cover-preserving Start requires format 4. Older
controllers refuse these journals. Legacy runs without a Start-command field retain `0x13`;
the driver never relabels a failed standard Start as a cover-preserving Start.

## Captured calibration startup

The `abi7500-unit2-dye-rox-002` capture uses a 60 °C/312-second hold with a **30 µL
controller volume setting**, a 90 °C cover, and a 252-second acquisition window (250 seconds
of estimated reading time plus a two-second polling margin). The
plate contained 20 µL per well; the method setting is not a measurement of that volume.
Filter selection starts after approximately 60 seconds of holding. The software closes
the light path at filter 0 after the searches, then collects final images at filters 1–5
and homes the wheel.

For an explicitly approved calibration run, `start_run(..., calibration_startup=True,
cover_temperature=90)` requires the lamp already enabled at standby power 0 and stationary
filter 0. PLR uploads the method, switches to lamp power 1, reads the exposure table, loads
the plate, homes the wheel once, verifies the cover, and sends Start `0x7E`. This mode
permits the captured 30 µL Standard/block-2 configuration. The default startup retains its
20 µL restriction. The separate normal `standby_startup=True` path also accepts 30 µL,
using normal Start `0x13` without a cover override; see
[normal optical startup](synchronized-acquisition.md#normal-start-from-lamp-standby).
A run identity includes the startup mode;
an interrupted home or Start cannot be repeated under that identity.

Use `park_after_exposure_search=True` in the automatic `CollectionPlan` to reproduce the
closed transition before final images. With the captured 312-second acquisition hold,
`read_window=252` provides the 60-second delay. The capture's last two status replies before
the first filter move report 253 and 251 seconds remaining, consistent with the vendor log's
250,000 ms read estimate and 2,000 ms polling margin. PLR still checks thermal ownership, remaining
hold time, cover temperature, and optical state around exposures. These checks add time;
the window is a deadline, not a claim of identical frame timestamps. An overrun fails instead
of extending the hold or resuming the acquisition.

The startup option and search parking require journal format 5, which older controllers
refuse. Bounded caches reuse decoded immutable masks and probe calculations; original
image bytes, exposure, and mask bytes determine each probe cache entry. Every reservation
still checks the saved run, collection, and probe evidence. No hardware result or operation
is cached in place of a required state query.
Historical acquisition ownership is audited once per connection and rechecked after external
database commits; locally written acquisition identities are checked without reparsing every
old image. Thermal run ownership is checked on every reservation.

This sequence has offline transport and interruption coverage and completed one authorized
Ubuntu acquisition on C9254-02/G2.10. Session `abi7500-plr-unit2-dye-rox-007` retained all
25 probes and 65 final images, including the final home, with 16 seconds of hold time remaining.
Its ROX calculation passed, with G1 corrected and no final-image clipping. Main-channel
background/exposure-corrected signal was a median 3.10% above the Windows reference,
ranging from −2.83% to +7.55% across wells. The RMS difference across 480 corrected,
peak-normalized coefficients was 0.001696; this is an absolute coefficient difference,
not a percentage of the weak-channel signal.

This establishes execution of the captured sequence on this instrument, not repeatability
or calibration equivalence. Application launch self-tests, operator delays, and the lamp's
earlier history are not reproduced by `start_run()`. Initial cover temperature differed,
and PLR's additional per-exposure checks lengthened collection. A separately specified
cooldown after acquisition is not part of the captured vendor method. See the
[hardware comparison](hardware-validation.md#captured-startup-and-rox-acquisition-in-plr-2026-10-20)
for timing, drift and validation limits. The generated calibration file was retained offline;
it was not installed in the vendor configuration.

The separately approved repeat, `abi7500-plr-unit2-dye-rox-008`, stopped during exposure
search when a post-download filter-position query received no reply within five seconds.
It preserved eight full probe images, with the last acquisition uncertain, and acquired
no final images. Cleanup cancelled the run and closed USB. This attempt supplies failure
recovery evidence but does not establish repeatability; see the
[interrupted-repeat audit](hardware-validation.md#interrupted-repeat-of-the-captured-rox-sequence-2026-10-20).

The separately approved retry `abi7500-plr-unit2-dye-rox-009` completed all 25 probes
and 65 final images with the same production code, program and processing inputs
as `007`. Main ROX signal was a median **5.46% higher than `007`** and **8.78% higher
than Windows `002`**, after background/exposure correction. Within its main ROX
burst, median last-four/first-four drift was +0.544%. The calculation passed with
G1 corrected; coefficients were retained offline and not installed. The successful
pair provides a repeat measurement, but the between-run shift remains unresolved.
Exposure selection and prior illumination history differed, including the interrupted
search between completed runs. See the
[completed-repeat comparison](hardware-validation.md#completed-repeat-of-the-captured-rox-sequence-2026-10-20)
for timing, normalized coefficient differences and limits.

A further authorized diagnostic, `abi7500-plr-unit2-dye-rox-010`, repeated preset
300/450/600 ms exposures on physical filter 2 after 15 minutes of thermal settling.
All 24 images completed without saturation. Corrected 300 and 450 ms signals were
only a median +0.103% and +0.115% relative to 600 ms; median last-two/first-two drift
at 600 ms was −0.181%. The settled signal still exceeded the preceding PLR `009`
scan by a median 8.78%. This narrows the exposure-response question over 300–600 ms,
but acquisition history and between-run agreement remain unresolved. It does not
directly test the earlier 639–702 ms exposures or generate a complete calibration.
See the [settled-response diagnostic](hardware-validation.md#settled-rox-exposure-response-diagnostic-2026-10-20).

## Fluorescein with PLR-generated calibration inputs

Authorized session `plr-u2dfl-010` acquired the user-declared **160 nM fluorescein,
20 µL per well** plate on Fast Dx 275030206 / C9254-02. It used the five PLR-generated
masks and their matching full-precision background, with 270 seconds of lamp standby,
a 90 °C cover and 15 minutes of settling at 60 °C before exposure selection.
Physical filters 1–5 selected **1800, 1800, 1800, 1800 and 162 ms**. All 15 search
probes and 65 final images completed.

All 96 wells had their strongest response in saved channel A / physical filter 5.
The recovered native and Java checks found no empty wells, spectral outliers or
substitutions, and no final well exceeded the saturation threshold. PLR generated a
single-dye `plr-fluorescein.ini`; reloading it preserved all **480 coefficients**.
Uniformity factors were not applied to pure-dye extraction. The median per-well CV
within the 16-image green-channel burst was **0.067%**, with a median last-four versus
first-four change of **−0.088%**. This describes one burst, not independent repeatability.

The dye identity remains `fluorescein`. This acquisition does not establish equivalence
to FAM, SYBR or EvaGreen, and creates no aliases. The interchange file and matching
inputs remain outside Git; no vendor calibration or validity flags were overwritten.
See the [acquisition and audit evidence](
hardware-validation.md#fresh-fluorescein-calibration-with-plr-inputs-2026-10-24).

The independently acquired repeat `plr-u2dfl-011` retained the same method and selected
the same exposures. All 15 probes and 65 final images completed without empty wells,
spectral outliers or saturated final wells. Its original relative spectra were compared
with the unchanged `010` file: no coefficients were refitted and no wells were replaced.
The RMS difference across 480 values was **0.0001415**, with a maximum absolute difference
of **0.001366** at B12 in saved channel B. These are absolute differences on the scale
where a well's strongest channel is 1, not percentages of weak-channel signal.

Green brightness changed by a median **+1.43%**, ranging from **−3.71% at D4 to +10.60%
at B12**. Thus a small overall spectral difference does not establish unchanged per-well
brightness. The green burst's median per-well CV was **0.0662%** and its median last-four
versus first-four change was **−0.0722%**. The between-run B12 change remains unexplained.
This is one repeat of the same plate, without a vendor repeatability tolerance or matched
vendor fluorescein acquisition. See the [independent repeat evidence](
hardware-validation.md#independent-fluorescein-repeat-with-frozen-coefficients-2026-10-24).

## Explicit SYBR-labelled fluorescein reference

Session `plr-u2dsy-001` acquired fresh images of the same **160 nM fluorescein,
20 µL per well** plate and calculated a separate `SYBR` entry at the user's explicit
request, following their notebook's fluorescein-surrogate workflow. The physical reference
remains fluorescein. The INI comments and companion provenance report retain that distinction;
no automatic dye alias or measured SYBR/EvaGreen chemical or assay equivalence is implied.

All 15 search probes and 65 final images completed, using the same selected exposures as
the preceding fluorescein scans. The recovered numerical checks passed without empty wells,
spectral outliers, substitutions or saturated final wells. All 96 wells peaked in saved
channel A / physical filter 5. The separate `plr-sybr-surrogate.ini` preserved all 480
coefficients on reload under the exact label `SYBR`; the `010` fluorescein file is unchanged.

Relative to the immediately preceding `011` scan, the RMS difference across original
normalized spectra was **0.00015075** and median green brightness increased **0.8106%**.
These comparisons describe repeated measurements of fluorescein, not a comparison of
fluorescein with actual SYBR. Within the green burst, median per-well CV was **0.0697%**.
The generated entry remains an offline PLR artifact; it was not installed in the vendor
configuration. See the [acquisition and provenance evidence](
hardware-validation.md#sybr-labelled-fluorescein-surrogate-acquisition-2026-10-24).

## ROX acquisition with the same PLR inputs

Session `plr-u2drx-013` attempted a fresh ROX calibration using the same PLR-generated
masks, matching background and settled acquisition method. It retained 24 search probes
and 62 completed final images before a filter-position readback timed out after an image
download. That additional image remains uncertain; two further final images were not started.
The run was cancelled, the filter parked and lamp turned off, and USB closed. The plate
was not released.

No ROX calibration was exported from this incomplete acquisition. The completed 16-image
ROX burst at physical filter 2 / saved channel D had no saturated wells and a median
per-well CV of 0.0632%, but this does not establish a five-filter calibration pass.
See the [interruption audit and retained measurements](
hardware-validation.md#interrupted-rox-acquisition-with-plr-generated-inputs-2026-10-24).

The separately authorized repeat, `plr-u2drx-014`, completed all 25 search probes and
65 final images with the same plate and processing inputs. All 96 wells peaked in saved
channel D / physical filter 2, without empty wells, spectral outliers, substitutions,
native flags or saturated final wells. The resulting `plr-rox.ini` preserved all 480
coefficients on reload. This is an offline PLR artifact, not an installed vendor calibration.

Within its 16-image ROX burst, median per-well CV was **0.0819%**. Compared with the
completed ROX burst of interrupted scan `013`, median background/exposure-corrected signal
increased **5.6054%**, ranging from **−1.7639% at F10 to +14.2336% at G6**. This comparison
does not establish full five-filter repeatability or explain the brightness change.
See the [completed repeat evidence](
hardware-validation.md#completed-rox-repeat-with-plr-generated-inputs-2026-10-24).

## 6-HEX acquisition and channel review

Authorized session `plr-u2dhx-001` completed **25 exposure probes and 65 final images**
using the same PLR masks, background and settled acquisition method. The user reported
the HEX plate loaded immediately after receiving the 50 nM, 20 µL per well preparation
instructions; those recipe and orientation details are inferred from that exchange.

The recovered native and Java checks passed without empty wells, spectral outliers,
substitutions or saturated final wells. Saved channel B / physical filter 4 was strongest
in 85 wells; saved channel C / physical filter 3 was strongest in 11 perimeter wells.
In those wells, C exceeded B by **0.2–9.2%**. Median per-well CV within the 16-image
B-channel burst was **0.08175%**, with median last-four/first-four drift **−0.04812%**.

The session's additional expectation that every unrepaired HEX well peak in B blocked
INI export. This is an extra review gate, **not a recovered vendor failure criterion**;
it does not establish that those wells or the plate failed calibration. Original spectra
and all 480 calculated coefficients are retained in the report, with no replacements.
The spatial channel response remains unexplained. This PLR session installed no vendor
calibration or `VIC` alias; the subsequent vendor comparison below used custom `6-HEX`.
See the [acquisition and channel-review evidence](
hardware-validation.md#hex-acquisition-and-channel-review-2026-10-26).

### Vendor HEX comparison

The same-plate vendor session `abi7500-unit2-dye-hex-vendor-001` completed as custom
**6-HEX**, with v2.3 reporting **Passed / Current**. PLR recalculated all **480 saved
binary32 coefficients exactly** from the 65 final captured camera frames and the vendor's
associated masks/background. No well was substituted, and no final sensor pixel clipped.

The vendor acquisition peaked in B at 75 wells and C at 21, including all 11 C-peaking
wells from the earlier PLR acquisition. An all-B expectation therefore cannot serve as a
vendor-equivalent pass criterion. Neither observation proves chemical identity or assay
performance.

The two acquisitions used different masks/backgrounds, exposures and settling histories.
Offline reprocessing of each image set with both calibration pairs retained its original
C-peak count (11 or 21). Changing the calibration pair shifted `100 * (C/B - 1)` by less
than 0.88 percentage points at every well; changing acquisitions with either pair held
constant shifted it by a median 3.48–3.49 points. This localizes most of that difference
to the acquisitions but does not distinguish exposure effects from thermal/illumination
history. The vendor's first final image followed the first observed hold by about 111
seconds; PLR used 900 seconds of settling before imaging. Independent repeatability under
matched conditions remains untested. See the [vendor comparison evidence](
hardware-validation.md#vendor-hex-comparison-and-exact-replay-2026-10-26).

### Fixed-exposure HEX comparison

The authorized PLR scan `plr-u2dhx-002` used the same sealed HEX plate with A1 explicitly
confirmed at the back-left. It acquired all 65 final images using the vendor's exact
exposures, masks and background. The first exposure began **110.023 seconds** after the
first observed 60 °C hold, versus **109.187 seconds** for the vendor: a **0.835-second**
difference, within the prepared ±3-second target. Later filter timing remained slower;
the first-to-last exposure-start span was **151.930 seconds**, versus **107.709 seconds**.

The recovered dye checks passed without flagged, substituted or saturated final wells.
PLR exported a local `plr-6-hex.ini`; it was not installed on the vendor PC. Saved channel
B peaked at 77 wells and C at 19, versus 75 and 21 for the vendor. Only A2 and F1 changed
their strongest channel. No all-B gate was applied.

Across all 480 normalized coefficients, RMS difference from the vendor was **0.005551**,
with maximum absolute difference **0.022815** on a 0–1 scale. With the same vendor
calibration inputs used for both comparisons, RMS disagreement decreased **69.5%** from
the earlier PLR acquisition. This is a descriptive improvement, not a predefined
agreement pass or evidence isolating exposure from settling effects. Timing of later
filters, exposure-search illumination and prior lamp history remain different.
Independent repeatability under identical conditions is still untested. See the
[fixed-exposure evidence](
hardware-validation.md#fixed-exposure-hex-comparison-2026-10-26).

The subsequent cadence attempt `plr-u2dhx-003` stopped after one image when the first
filter transition exceeded the second exposure's timing deadline. The driver blocked
that exposure, cancelled the run and preserved the incomplete collection. Thus the
65-image schedule remains unverified on hardware, and this attempt supplies no new
five-filter fluorescence comparison. See the [interruption evidence](
hardware-validation.md#interrupted-hex-cadence-validation-2026-10-26).

Attempt `plr-u2dhx-004`, using reduced boundary queries, passed the first filter
transition and downloaded five images. Delay then accumulated within filter 2;
the sixth start was blocked at least **27.944 ms** beyond the unchanged tolerance.
Four downloaded frames remain uncertain because their burst did not finish its
status checks. This isolates further work to acquisition and local per-image
throughput; the full cadence and matched fluorescence comparison remain unvalidated.
See the [follow-up timing audit](
hardware-validation.md#hex-cadence-after-boundary-query-reduction-2026-10-26).

Instrumented attempt `plr-u2dhx-005` downloaded six frames before blocking the seventh
start. Combined image/next-intent checkpoints took a median **58.146 ms**, accounting
for **99.58%** of the measured gaps before the next same-filter configuration. These
are whole journal calls, not isolated disk-flush times. Cleanup completed without
releasing the tray. The incomplete burst remains uncertain and produces no new
calibration or vendor fluorescence agreement result. See the [checkpoint timing evidence](
hardware-validation.md#instrumented-hex-checkpoint-timing-2026-10-27).
