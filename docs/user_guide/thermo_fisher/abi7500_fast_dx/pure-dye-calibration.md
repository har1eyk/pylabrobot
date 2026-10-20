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
G1. Cross-run vendor agreement, other dyes and assay performance remain unverified.
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

## Fixed-exposure acquisition

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
