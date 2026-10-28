# Generating ROI masks

`generate_roi_mask()` finds 96 well regions from an original ROI image. It needs no existing
mask, vendor DLL, Windows process, background coefficients or uniformity factors. The resulting
`PlatePixelMask` works with PLR's fluorescence extraction, calibration calculations and collection
plan serialization. The implementation uses the Python standard library.

```{warning}
This is image-based mask generation, not a complete instrument calibration procedure. Its geometry
checks do not establish plate chemistry, physical A1 orientation, appropriate exposure, or current
calibration validity. Fresh Ubuntu images have validated mask generation for all five physical
filters across two sessions. Automatic ROI exposure search and the complete calibration workflow
still require validation. The device integration remains experimental.
```

## Generate and save a mask

Use one final ROI image for each physical filter, preserving the original acquisition metadata.
The supported camera transfer is 321 × 245 with 12-bit pixel values. The conversion uses the
OPB=0 crop, including its one-pixel source offset, then rotates 180° into vendor TIFF coordinates.
It does not subtract optical black or apply fluorescence corrections.

```python
from pathlib import Path
import json

from pylabrobot.thermo_fisher.abi7500_fast_dx import (
  CameraFrame,
  CameraGeometry,
  PlatePixelMask,
  ROICalibrationImage,
  generate_roi_mask,
)

# Original bytes from a completed, recorded ROI acquisition at physical filter 1.
frame = CameraFrame(CameraGeometry(321, 245), Path("roi-filter-1-wire.bin").read_bytes())
image = ROICalibrationImage.from_camera(frame, filter=1)
result = generate_roi_mask(image)
Path("roi-filter-1-report.json").write_text(json.dumps(result.report(), indent=2))

if result.passed:
  Path("roi-filter-1.plrroi").write_bytes(result.mask.source_bytes)
  mask = PlatePixelMask.from_file(Path("roi-filter-1.plrroi"), filter=1)
else:
  print(result.failure_reasons)
```

For a saved vendor TIFF, decode its pixels first. `ROICalibrationImage(image_bytes, filter)`
accepts exactly 320 × 240 row-major **little-endian uint16 pixel bytes**, not TIFF container bytes.
Those pixels must already be in vendor TIFF coordinates; do not rotate them again. For example,
with the optional Pillow package:

```python
from PIL import Image
import struct

with Image.open("AutoROI_F0.tif") as tiff:
  if tiff.size != (320, 240) or tiff.mode not in ("I;16", "I;16L", "I;16B", "I"):
    raise ValueError("Expected an original 320 by 240 integer ROI TIFF")
  image = ROICalibrationImage(struct.pack("<76800H", *tiff.getdata()), filter=1)
result = generate_roi_mask(image)
```

Physical filters are numbered 1–5. The saved vendor filenames `AutoROI_F0.tif` through
`AutoROI_F4.tif` correspond to those physical filters in that order. Image array coordinates
are `(row, column)`; regions are ordered A1–H12. This ordering depends on the established camera
orientation and does not detect whether the physical plate was loaded rotated.

The report preserves the image hash, global threshold, 10 × 10 threshold grid, region bounds,
pixel counts, clipped-pixel count and any failure reasons. It includes the generated mask hash
only when geometry checks pass. The immutable input image remains available in the result.
Keep images, reports, exposures, instrument/camera identities and masks together outside Git.

## Calculation and rejection checks

The image supplies every well boundary; a fixed nominal plate grid is not substituted.

1. Calculate the global Otsu threshold on a histogram quantized by two bits, retaining the
   last maximum and restoring the bin midpoint. Segmentation maps counts at or below 10 to zero.
2. Calculate full-resolution Otsu thresholds over a three-level quadtree: 85 image rectangles.
   Apply the native local histogram balance test, including its one-bin peak-index convention.
   Reject threshold outliers relative to the median and replace missing child thresholds from
   their three retained siblings or parent.
3. Adjust the 8 × 8 leaf thresholds to the global threshold, extrapolate a border and use integer
   bilinear interpolation. Preserve native uint16 wrapping at extrapolated grid boundaries.
4. Select pixels strictly above their local thresholds, fill single horizontal gaps, remove
   isolated foreground pixels in row-major passes and expand regions twice by a 3 × 3 neighborhood.
   Fill enclosed holes within each detected outer boundary.
5. Require exactly 96 regions, then order eight rows of twelve. PLR also requires separated
   neighboring bounds, an unambiguous row layout, a four-pixel image margin and a bounded region
   perimeter. These geometry checks are deliberately conservative, not a reproduction of every
   vendor acceptance decision. Failed results retain diagnostics but cannot export a mask.

Clipped pixels are reported separately: geometric success does not imply that an image is
unsaturated or suitable for quantitative fluorescence. ROI calibration uses different exposure
criteria from ordinary well measurements.

## Mask format and use

Generated masks use a distinct, versioned PLR format (`.plrroi`), not the vendor MFC `.aoi`
archive. They embed the physical filter, SHA-256 of the original image and the 96 ordered pixel
lists. The decoder rejects incomplete data, overlap, duplicates, unordered coordinates, filter
mismatch and trailing bytes. `PlatePixelMask.generated` distinguishes formats;
`source_image_sha256` is available for generated masks and is `None` for vendor masks.
The bytes are retained through collection-plan serialization. Run archives preserve generated
masks with the `.plrroi` extension and vendor masks with `.aoi`.

The generator does not write to `Calibration.sda`, replace vendor files or mark calibration
valid. It also does not bind a file to a device automatically: retain and check the recorded
instrument and camera identities. When new region boundaries differ, background and uniformity
coefficients need matching calibration provenance. Do not silently reuse coefficients derived
with different regions.

## Validation evidence

Ten saved final ROI images from `abi7500-unit2-roi-001` and `abi7500-unit2-roi-002` on Fast Dx
275030206 / camera 0000000747 cover two independent five-filter calibrations. PLR generates all
**960 well regions with exactly the same selected pixels and order as the saved vendor masks**.
All ten global thresholds match. Expected masks are comparison outputs, never generator inputs.

For the earlier calibration, five original USB camera transfers also reproduce the saved TIFF
pixels exactly, and generated masks reproduce every vendor-mask well/ring extraction statistic.
Regression fixtures retain three bounded image inputs and independent per-well coordinate
fingerprints, thresholds and extraction statistics. They exercise local histogram rejection,
mask boundaries, camera crop/orientation, missing/extra wells, flat/weak images and mask
serialization through a collection plan.

This establishes offline agreement for the recorded camera configuration and images, not
performance on other cameras.

### Fresh PLR images

Session `plr-u2roi-001` acquired three confirmed images of the same sealed ROI plate directly
from Ubuntu at physical filter 1, during a 60 °C hold with the cover near 90 °C. Each image
independently yielded 96 regions without clipped pixels:

| Exposure, ms | Global threshold | Regions |
| --- | --- | --- |
| 1024 | 874 | 96 |
| 512 | 502 | 96 |
| 256 | 314 | 96 |

The prespecified selection rule chose the highest-threshold geometrically passing unclipped
image: 1024 ms. Its generated mask had **99.64% median per-well intersection-over-union** with
the saved vendor mask, and a maximum centroid displacement of **0.294 pixels**. The vendor
mask was loaded only after generation and selection. These are separate acquisitions, so
pixel equality is not expected or used as an acceptance requirement.

The first filter-2 image transferred completely, but its following lamp-status query timed out.
That acquisition remains uncertain and is excluded from selected masks. PLR cancelled the run;
filters 3–5 were not collected. This partial session validates fresh image-to-mask processing
for filter 1, not a completed five-filter ROI calibration or session-to-session repeatability.
See [hardware validation](hardware-validation.md) for the retained failure and cleanup evidence.

A separately authorized session, `plr-u2roi-002`, completed the remaining twelve images for
physical filters 2–5, using the same plate and thermal settings. It completed cooldown and
plate release without a timeout. All twelve images yielded 96 regions. The 1024 ms images in
filters 2, 4 and 5 contained clipped pixels; the selection rule chose their unclipped 512 ms
images. Filter 3 used 1024 ms.

The combined set contains **480 well regions generated from fresh PLR images**, with no clipped
pixels in any selected image:

| Physical filter | Source run | Exposure, ms | Median per-well pixel overlap | Maximum centroid shift, pixels |
| --- | --- | --- | --- | --- |
| 1 | `plr-u2roi-001` | 1024 | 99.64% | 0.294 |
| 2 | `plr-u2roi-002` | 512 | 99.68% | 0.236 |
| 3 | `plr-u2roi-002` | 1024 | 99.70% | 0.192 |
| 4 | `plr-u2roi-002` | 512 | 99.70% | 0.179 |
| 5 | `plr-u2roi-002` | 512 | 99.69% | 0.220 |

Overlap and centroid shifts compare against the independently saved vendor masks. Generation
and exposure selection use only the fresh images. Every selected mask was reloaded through
`PlatePixelMask` and used to extract 96 well/ring measurements from its original camera frame.
The combined export preserves instrument/camera identity, each source run and acquisition,
exposure, original wire-image hash and generated mask hash. It contains no uncertain acquisition.

The five-filter artifact combines two sessions; it is not evidence of an uninterrupted
five-filter ROI run, automatic exposure search, repeatability or a vendor calibration pass.
Original images, `.plrroi` masks, reports and the comparison figure remain outside Git under
`abi7500-research/captures/abi7500-plr-unit2-roi-002/generated-masks/`.
