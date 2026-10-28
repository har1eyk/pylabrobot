# Background calibration from camera images

`calculate_background_calibration()` generates new background offsets and slopes from
water-plate camera images and explicitly selected ROI masks. It works offline without vendor
DLLs, previous background coefficients or instrument communication. Its output is accepted by
PLR's fluorescence, optical-calibration and pure-dye processing.

Supply all five physical filters, each with complete groups at **2, 1 and 0.5 seconds**.
Each group requires its independently recorded planned image count, either one or sixteen.
The reference collection contains 225 images: one image for physical filter 1 at 2 seconds,
then sixteen for each remaining group. Preserve acquisition order within each group; exclude
startup, search, incomplete and uncertain acquisitions. File presence alone does not establish
that an acquisition completed.

## Calculate and preserve the result

```python
import json
from pathlib import Path

from pylabrobot.thermo_fisher.abi7500_fast_dx import (
  BackgroundCalibrationFilter,
  BackgroundCalibrationGroup,
  CameraExposure,
  CameraFrame,
  CameraGeometry,
  FluorescenceFrame,
  PlatePixelMask,
  calculate_background_calibration,
)

# confirmed_paths[(physical_filter, exposure_seconds)] contains the original camera
# files in acquisition order, selected from the completed acquisition records.
# planned_counts is read from the recorded collection plan, not len(confirmed_paths).
filters = []
for physical in range(1, 6):
  mask = PlatePixelMask.from_file(
    Path("calibration") / f"physical-filter-{physical}.plrroi", filter=physical
  )
  groups = []
  for exposure in (2.0, 1.0, 0.5):
    frames = tuple(
      FluorescenceFrame(
        CameraFrame(CameraGeometry(321, 245), path.read_bytes()),
        CameraExposure(exposure),
        mask,
      )
      for path in confirmed_paths[physical, exposure]
    )
    groups.append(BackgroundCalibrationGroup(frames, planned_counts[physical, exposure]))
  filters.append(BackgroundCalibrationFilter(tuple(groups)))

result = calculate_background_calibration(
  tuple(filters),
  instrument_id=recorded_instrument_id,
  scanner_id=recorded_camera_usb_serial,
  timestamp=recorded_acquisition_timestamp_ms,
)
Path("background-report.json").write_text(json.dumps(result.report(), indent=2))
if result.passed:
  background = result.to_background()
  Path("Background.csv").write_bytes(background.source_bytes)
else:
  print(result.failure_reasons)
```

Native `.aoi` masks and PLR-generated `.plrroi` masks are supported. Recalculate background
when masks change: coefficients extracted with different regions are not automatically
interchangeable. Keep the exact masks, original frames, completed acquisition records and
`background-report.json` beside the generated file. The CSV layout has no identity or provenance
fields; the report records instrument/camera identifiers, acquisition timestamp, mask and frame
SHA-256 hashes, intermediate counts, all coefficients and the output file's SHA-256. Identifiers
are caller declarations and do not independently establish hardware association.

## Calculation and quality checks

The supported 321 × 245, 12-bit, OPB=0 path crops and rotates each frame into mask coordinates.
It excludes zero pixels, subtracts each well's surrounding one-pixel-ring mean, and averages
those untruncated signals in acquisition order. Clipping below zero and integer truncation happen
**after** averaging. Truncating or clipping each image first changes the fit.

Each averaged signal is normalized using the recovered effective integration time
`ceil((requested_ms + 0.2746) / 0.1702) * 0.1702`, truncated to uint32, stored as binary32 and
scaled back by the requested exposure. A three-point linear fit then produces signed offsets
and slopes per requested millisecond. The resulting coefficients retain binary32 precision;
the nine-significant-digit CSV representation round-trips without losing any coefficient bits.
Arrays use saved channels A–E, the reverse of physical wheel positions 1–5, and row-major wells
A1–H12.

The result retains failed data for inspection. Export is blocked when any well is saturated
in any input image (more than six selected pixels above 4000), or when its fitted 1000 ms
background exceeds the reference limit: 72,000 FSU for saved A–D, 90,000 FSU for saved E.
The report identifies affected wells by one-based row-major index. No well is silently removed,
replaced or compensated. A threshold screen pass is **not** the complete vendor calibration
validity/compensation workflow and does not certify current instrument or assay performance.

## Validation

Bounded captured patches test the entire image-to-fit calculation, including averaging before
truncation, physical/saved filter mapping and coefficient precision. Independent full-precision
vendor coefficients test the recovered numerical fit. Tests also cover missing images, mixed
settings and masks, duplicate/missing filters or exposures, saturated images, excessive background,
provenance and lossless export/reload.

Full archived camera collections and comparison reports remain outside Git. See the
[hardware and processing report](hardware-validation.md) for recorded comparisons. Generating
coefficients does not change vendor files, calibration validity flags, exposure tables or firmware.
