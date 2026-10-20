# Calculating optical calibration

`calculate_optical_calibration()` generates new optical uniformity coefficients from raw
camera images, the matching ROI masks, and native background coefficients. Use `Calibration.sda`
for full binary32 precision; the supported version-5 `Background.csv` export can round
coefficients enough to change an integer signal by one count. It runs in Python on
Ubuntu without Windows or vendor DLLs. Existing uniformity coefficients and expected vendor
results are **not** inputs to the calculation.

```{warning}
This is an offline calculation routine. It does not yet automate the complete optical-calibration
acquisition, exposure search, temperature control, or installation on the instrument/vendor PC.
Its acceptance result describes the supplied data, not current instrument validity or assay
performance. Direct Ubuntu acquisition of a fresh calibration remains unverified.
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
The camera exposure type accepts up to the captured 2-second search exposure; the complete
search/control sequence still needs its own implementation and hardware validation.

The numerical rules were checked against the native optical coefficient calculation,
`CPureDyeDataListMgr` empty/outlier/local-neighbor routines, and the saved results. Regression
fixtures contain all 480 spectra/coefficient strings and small A1 image patches from each
final collection. Full images, vendor binaries and calibration backups remain outside Git.
Agreement with one recorded vendor session establishes this processing path; independent plates
and a fresh Ubuntu-only calibration are still needed for broader validation.

The captured vendor calibration used a 60 °C hold, a 90 °C cover setting and a distinct calibration
start sequence. The regular PLR Standard-mode thermal-run API does not yet reproduce that entire
workflow. Do not substitute the recorded exposure list into a regular run and call it a validated
optical calibration.
