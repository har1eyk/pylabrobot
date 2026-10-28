# Calculating optical calibration

`calculate_optical_calibration()` generates new optical uniformity coefficients from raw
camera images, the matching ROI masks, and matching background coefficients. Native `Calibration.sda` and
[PLR-generated background CSV](background-calibration.md) preserve full binary32 precision;
rounded vendor CSV exports can change an integer signal by one count. It runs in Python on
Ubuntu without Windows or vendor DLLs. Existing uniformity coefficients and expected vendor
results are **not** inputs to the calculation.

```{warning}
The calculation runs offline. The journaled collection API has also completed one fresh
Ubuntu optical calibration, including automatic exposure selection, thermal control and cleanup,
on C9254-02/G2.10. Installation into the vendor software and its validity workflow are not
implemented. A passing calculation does not establish independent repeatability or assay
performance.
```

## Inputs and usage

Use a uniform mixed-dye plate's **final fixed-exposure collections**, excluding startup and
exposure-search images. Preserve their order. Supply each collection's independently recorded
expected count: the reference session used one frame at physical filter 1 and sixteen frames
at each remaining filter. A missing frame, mixed exposure/mask, or saturated final image is an
error. Selecting the correct acquisition phase remains the caller's responsibility.

Each frame needs its original camera bytes, commanded exposure in seconds, and matching
`PlatePixelMask`. Physical wheel positions run 1–5; saved calibration channels run in reverse
order. Do not rename a mask from one filter to use it for another.

```python
from pathlib import Path
import json

from pylabrobot.thermo_fisher.abi7500_fast_dx import (
  BackgroundCalibration,
  CameraExposure,
  CameraFrame,
  CameraGeometry,
  FluorescenceFrame,
  OpticalCalibrationFilter,
  PlatePixelMask,
  calculate_optical_calibration,
)

# Fill this manifest from the acquisition record, not from assumed default exposures.
# Each item has physical_filter, exposure_seconds, expected_frame_count,
# mask_path, and frame_paths (final frames only, in acquisition order).
manifest = json.loads(Path("optical-acquisition.json").read_text())
collections = []
for item in manifest["filters"]:
  mask = PlatePixelMask.from_file(item["mask_path"], filter=item["physical_filter"])
  frames = tuple(
    FluorescenceFrame(
      CameraFrame(CameraGeometry(321, 245), Path(path).read_bytes()),
      CameraExposure(item["exposure_seconds"]),
      mask,
    )
    for path in item["frame_paths"]
  )
  collections.append(OpticalCalibrationFilter(frames, item["expected_frame_count"]))

result = calculate_optical_calibration(
  tuple(collections),
  BackgroundCalibration.from_file(manifest["background_path"]),
  instrument_id=manifest["instrument_id"],
  scanner_id=manifest["scanner_id"],
  timestamp=manifest["timestamp_ms"],
)
Path("optical-report.json").write_text(json.dumps(result.report(), indent=2))

if result.analysis.passed:
  calibration = result.to_uniformity()
  Path("plr-uniformity.ini").write_bytes(calibration.source_bytes)
else:
  print(result.analysis.failure_reasons)
```

The generated `UniformityCalibration` can be supplied to `CameraFilterResult` or
`ABI7500FastDx.export_run()`. Serialization retains the original integer spectra, six-significant-digit
coefficients, declared instrument/camera identities, timestamp, and background/mask hashes.
The report also retains each frame's hash, exposure, expected frame count, replaced wells,
corrected spectra and full binary32 coefficients. Keep the original frames, ROI masks, background,
report and generated file together outside Git. Failed data remain inspectable and cannot be
exported as a successful calibration.

## Acquisition configuration

A uniform mixed-dye plate can use the shared automatic-exposure collection API. Supply the
same masks used to generate the background that will enter the optical calculation:

```python
from pylabrobot.thermo_fisher.abi7500_fast_dx import CollectionFilter, CollectionPlan

plan = CollectionPlan(
  steps=((0, 0),),
  filters=tuple(
    CollectionFilter(position, (2.0,) * count, mode="automatic")
    for position, count in enumerate((1, 16, 16, 16, 16), start=1)
  ),
  read_window=584,
  exposure_search_masks=tuple(masks[position] for position in range(1, 6)),
  park_after_exposure_search=True,
  park_between_collections=True,
  home_after_collections=(1,),
)
```

This plan belongs to a 60 °C/600-second hold followed by a 30 °C/30-second cooldown. Its first
collection begins after 16 reported hold seconds. The 2-second entries are search ceilings,
not fixed final exposures. At most 55 probes are retained separately from the 65 final images;
all searches finish before final-image collection begins. The light path closes after searching,
and the filter wheel homes after the complete final collection.

Use the captured calibration configuration and persistent run journal, with standby lamp warm-up
at stationary filter 0 before `start_run(..., calibration_startup=True, cover_temperature=90)`.
The prepared session uses 270 seconds of standby and a 1200-second collection observation budget.
These durations are explicit choices for this method, not universal instrument guarantees.
The [calibration startup guide](pure-dye-calibration.md)
describes the separate controller sequence and volume setting. Use the
[run cleanup and recovery rules](thermal-runs.md) for faults, cancellation and tray release;
never repeat Start or an uncertain exposure to fill a missing collection.

Only confirmed final acquisitions belong in `OpticalCalibrationFilter`. Original search probes
remain available through each collection's `exposure_probes` and are excluded from the average.
Keep the generated uniformity file with its exact masks and background; do not silently combine
it with a different calibration set.

### Fresh Ubuntu acquisition

Session `plr-u2opt-001` on Fast Dx **275030206**, camera **0000000747**, completed this
configuration with the user-confirmed sealed mixed-dye ROI plate. All **55 exposure probes**
and **65 final images** completed. The search selected these exposures from fresh pixels:

| Physical filter | Saved channel | Final exposure, ms | Final images |
| --- | --- | ---: | ---: |
| 1 | E | 1,782 | 1 |
| 2 | D | 711 | 16 |
| 3 | C | 1,188 | 16 |
| 4 | B | 603 | 16 |
| 5 | A | 567 | 16 |

The calculation used the five PLR-generated masks and the matching full-precision background
from `plr-u2bg-002`. It produced **480 coefficients**, with no empty wells, spectral outliers,
substitutions or saturated final wells. The generated file, matching masks/background and
provenance reports are retained together in the session's `calibration/` directory outside Git.

An independent audit reconstructed all 120 original camera transfers and replayed every
exposure-search decision. Collection and homing finished with 296 seconds remaining in the
60 °C hold. Normal completion, cooldown, tray release, lamp-off and USB closure were confirmed.
See the [hardware report](hardware-validation.md) for the complete acquisition and cleanup record.
Normalizing the fitting images is an internal consistency check. The separate acquisition below
tests transfer to new images. There is no matching vendor result for either fresh acquisition.

### Independent repeat with unchanged coefficients

Separately authorized `plr-u2opt-002` repeated the same method and plate with 45 search probes
and 65 final images. The prior coefficients, masks and background were frozen before acquisition;
no coefficient refit, well exclusion or substitution occurred. Across-well sample CV fell from
**17.49–20.23% before correction to 0.87–2.58% after correction**, across the five channels.
No saturated final wells, empty wells or spectral outliers were found.

Median brightness changes relative to the calibration acquisition were **+1.22%, −7.36%,
−1.60%, +3.89% and −1.12%** in saved channels A–E. After removing one median scale ratio per
channel for comparison, the largest individual spatial change was **7.63% at H12 in physical
filter 2**. That scale removal is a diagnostic, not a change to the applied coefficients.
The [hardware report](hardware-validation.md) retains all channel statistics, temporal variation
and transport/cleanup checks. This supports uniformity correction on another acquisition of
the same plate; absolute brightness drift, independent-plate reproducibility and assay performance
remain separate validation questions. No vendor repeatability tolerance is assumed.

## Calculation

1. Apply the supported OPB=0 crop and 180° rotation. For each well, sum nonzero ROI pixels
   and subtract the nonzero adjacent-ring mean multiplied by the ROI pixel count.
2. Average these **untruncated** signals across the final repeated images, then truncate once
   into the raw run buffer. Averaging individually truncated well signals gives different results.
3. Apply native background offset/slope subtraction and exposure normalization:

   ```text
   effective_ms = ceil((requested_ms + 0.2746) / 0.1702) × 0.1702
   signal = truncate(clamp(1000 × (raw − offset) / effective_ms − 1000 × slope))
   ```

   The clamp is to uint32 range. These integer signals are the uncorrected calibration spectra.
4. Identify empty wells using their strongest filter and twice its background slope at the
   native 1000 ms virtual exposure. Normalize each well's five-filter spectrum by its largest
   signal. Compare its summed absolute spectral difference from the other retained wells to
   a leave-one-out mean plus six sample standard deviations. Repeat after excluding outliers.
5. Reject five or more flagged wells. For one to four, require at least two unflagged adjacent
   wells, then substitute their per-filter means. Neighbors never include other flagged wells.
   PLR additionally rejects nonpositive retained or replacement filter signals instead of
   producing undefined factors.
6. Calculate `coefficient = plate_mean / well_signal` for each filter and well. Preserve native
   row-major binary32 accumulation, division and six-significant-digit output formatting.

`OpticalCalibrationAnalysis` exposes this last statistical/coefficient stage for already
extracted integer spectra. Its well indices are **one-based**: H5 is 89. It reports original
spectra, empty/outlier indices, accepted substitutions and failure reasons separately.
No dye concentrations, spectral unmixing, passive-reference normalization or Ct calculation
are implied by these optical uniformity factors.

## Reference evidence and limits

An offline replay of `abi7500-unit2-optical-001` on instrument 275030206 / camera 0000000747
reconstructs all **480 original integer spectra** from **65 final full camera frames**. It
independently flags **H5**, substitutes its five adjacent wells, and reproduces all **480 vendor
coefficient strings and reloaded binary32 factors exactly**. The first 45 startup/search frames
are excluded. No expected output is supplied to PLR during this calculation.

The recorded final exposures were 1.800, 0.945, 1.350, 0.603 and 0.450 seconds at physical
filters 1–5. These are observations for that plate, **not universal calibration settings**.
The camera exposure type accepts up to the captured 2-second search exposure; the shared `CalibrationExposureSearch` reproduces all **44 original-image optical probes**
and these five independently recorded final settings. This is separate from its pure-dye
reference validation; an arbitrary exposure list is not a replacement for a fresh search.

The numerical rules were checked against the native optical coefficient calculation,
`CPureDyeDataListMgr` empty/outlier/local-neighbor routines, and the saved results. Regression
fixtures contain all 480 spectra/coefficient strings and small A1 image patches from each
final collection. Full images, vendor binaries and calibration backups remain outside Git.
Agreement with one recorded vendor session establishes this processing path. The fresh Ubuntu
session above additionally validates acquisition and coefficient generation on one plate;
independent plates and additional repeat acquisitions are still needed for broader validation.

The captured vendor calibration used a 60 °C hold, a 90 °C cover setting and a distinct calibration
start sequence. The regular Standard-mode Start is not interchangeable with calibration Start. The explicit
`calibration_startup=True` and `cover_temperature=90` options use the captured calibration
sequence. Do not substitute the recorded exposure list into a regular PCR run and call it an
optical calibration.
