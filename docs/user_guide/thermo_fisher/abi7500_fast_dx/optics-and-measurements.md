# Optical control and corrected well measurements

```{warning}
Direct Ubuntu sessions verified lamp/filter control, illuminated well patterns at filters 1 and 5,
and lamp-off/filter-0 cleanup. Filter 5 saturated at the tested exposure. Offline processing
reconstructs every saved well value from one recorded vendor acquisition. This establishes the
reference algorithm, not present calibration validity or assay performance. Thirty scheduled images
were captured within two holds, followed by normal thermal completion. Optical fault recovery
and broader programs remain unverified. Obtain approval for each hardware session.
See the [hardware report](hardware-validation.md).
```

## Lamp and filter control

Use the same persistent journal as thermal runs and snapshots. Both setters check the reference
C9254-03/2.20 and G2.10 identities, journal ownership, camera readiness and thermal inactivity.
They serialize with runs, snapshots and keepalives. Setup and stop do not actuate the optics.

```python
state = await instrument.get_optical_state()
print(state.filter_position, state.is_stationary, state.lamp_enabled, state.lamp_power)

# Execute these mutations only within an approved optical session.
state = await instrument.set_filter_position(1)
state = await instrument.set_lamp(True, power=0)
# Take separately identified raw snapshots as authorized for the session.
state = await instrument.set_lamp(False)
```

Filter positions are absolute firmware indices 0–5. The captured vendor acquisition uses positions
1–5 for saved filters **5–1**, respectively. Position 0 occurs at idle. Native `OpenShutter(true)`
is a no-op; `OpenShutter(false)` moves to position 0. These are not dye labels.

**Physical wheel positions and vendor channel numbers run in opposite orders.** For the
reference instrument, combine the captured physical-to-saved mapping with the vendor's
[channel assignments](https://documents.thermofisher.com/TFS-Assets/LSG/manuals/4387777d.pdf):

| Physical position passed to PLR | Saved/vendor channel | Principal dye channel |
|---|---|---|
| 5 | 1 / A | FAM / SYBR Green |
| 4 | 2 / B | VIC / JOE |
| 3 | 3 / C | NED / TAMRA / Cy3 |
| 2 | 4 / D | ROX / Texas Red |
| 1 | 5 / E | Cy5 |

The backed-up `puredye.ini` independently supports the saved-channel assignments: SYBR and
FAM peak in channel 1, VIC in 2, ROX in 4 and Cy5 in 5. These are spectral associations, not
exclusive dye measurements; fluorescence can contribute to more than one channel. Spectral
unmixing is not implemented. The current validity of that calibration is not established.

In the hardware reports, **physical filter 5 means vendor channel 1 (SYBR/FAM)**, and physical
filter 4 means vendor channel 2. The reported declines at physical positions 4/5 must not be
interpreted as changes in the vendor's red channels 4/5. Knowing that a plate contains SYBR
Green does not establish that physical position 5 is outside its detection band.

The setter waits for an existing move before deciding whether another move is necessary, sends
at most one move, then waits for stationary position readback. Its default motion timeout is 15
seconds; a pending command can additionally take up to `command_timeout` seconds. It never homes
the wheel or changes its calibration position.

Lamp enable and power are separate commands. Enabling requires an explicit native power code 0 or
1. The setter waits 250 ms between enable and power, then 250 ms before state readback, following
the recovered native sequence. The illumination session additionally allowed five seconds of
settling before lamp-on exposures; setter completion does not certify stable brightness. Power
is not a calibrated intensity. The
four-byte readback preserves both native words; the observed disabled reply is `0000 0002`.
A successful setter confirms controller readback, not lamp brightness or optical stabilization.
Disabling uses `set_lamp(False)` without a power argument.

Repeated requests query the current state and skip mutations already satisfied. An error,
readback disagreement or interrupted mutation faults the connection and does not automatically
repeat a command. After `stop()` / `setup()`, read the actual state before deciding what to do.
Closing USB does **not** switch off the lamp, cancel a thermal run, or cool the heated cover.

## Uniformity-corrected values from saved experiments

The EDS well arrays are already vendor outputs, despite their `.tiff` names. They retain signed
64-bit values and acquisition indices. Correct them with the calibration snapshot **inside the
same EDS**, rather than silently substituting the control PC's current calibration:

```python
from pathlib import Path
from pylabrobot.thermo_fisher.abi7500_fast_dx import (
  apply_uniformity,
  read_eds_filter_data,
  read_eds_uniformity,
)

experiment = Path("/path/to/experiment.eds")
calibration = read_eds_uniformity(experiment)
for source in read_eds_filter_data(experiment):
  result = apply_uniformity(source, calibration)
  for value in result.measurements:
    print(source.stage, source.cycle, source.collection_point, source.filter,
          value.label, value.raw_value, value.corrected_value)
```

`UniformityCalibration.from_file(path)` also accepts a separately selected `uniformity.ini`.
The supported format is version 1.0.0, `sds7500fast`, `96fast`, eight rows, twelve columns, and
filters 1–5 in that order, with a successful saved result and finite positive factors. Missing,
duplicate, failed or malformed calibration data raises an error. No file is changed.

`UniformityCorrectedData` retains the source acquisition and the original calibration bytes. Its
measurements contain the original integer, uniformity coefficient and corrected binary32 value.
The calibration exposes its SHA-256, UTC epoch-millisecond timestamp and saved instrument/scanner
identifiers. The reference files use `null` and `*`; these do not prove a match to a camera.
A saved pass flag does not establish current calibration validity. This API reports provenance
without extending the calibration's validity or overriding the vendor's expiration warning.

The verified operation is Java's integer-to-float conversion followed by float multiplication:
`float32(float32(saved_value) * coefficient)`. Direct int64-to-binary32 rounding matters near large
integer boundaries; PLR avoids rounding first through binary64. Well labels follow the vendor
Java matrix indexing: A1–A12, B1–B12, through H12. They do not independently establish the physical
orientation of a camera image. Negative results are retained, with no clipping.

These outputs are **uniformity-corrected filter signals**. They are not dye concentrations, Rn,
ΔRn or Ct. The function does not add background subtraction, exposure normalization or spectral
unmixing. Passing a `CameraFrame` as a saved well array is not supported.

## Reconstruct measurements from camera frames

`CameraFilterResult` implements the reference **OPB=0, 12-bit, sum-extraction, one-pixel-ring**
configuration. Supply three original frames from the same physical filter and collection point,
their commanded exposures, the matching pixel mask, native `Background.csv`, and the acquisition's
uniformity calibration. It does not open USB or trigger an exposure.

```python
from pathlib import Path
from pylabrobot.thermo_fisher.abi7500_fast_dx import (
  BackgroundCalibration, CameraExposure, CameraFilterResult, CameraFrame, CameraGeometry,
  FluorescenceFrame, PlatePixelMask, read_eds_uniformity,
)

# Example: recorded cycle 1, physical filter 1; replace these paths with matching files.
folder = Path("/path/to/reference-acquisition")
mask = PlatePixelMask.from_file(folder / "ROIMask_W96_F0_I809_Commit.aoi", filter=1)

def load_frame(name: str, exposure: float) -> FluorescenceFrame:
  """Associate an original USB frame with its recorded exposure and mask."""
  source = CameraFrame(CameraGeometry(321, 245), (folder / name).read_bytes())
  return FluorescenceFrame(source, CameraExposure(exposure), mask)

result = CameraFilterResult(
  frames=(load_frame("camera-016.bin", 0.540), load_frame("camera-017.bin", 0.180),
          load_frame("camera-018.bin", 0.060)),
  background=BackgroundCalibration.from_file(folder / "Background.csv"),
  uniformity=read_eds_uniformity(folder / "acquire-complete.eds"),
  stage=2, cycle=1, collection_point=1,
)
for value in result.measurements:
  print(result.filter, value.label, value.raw_value, value.corrected_value,
        value.selection.selected_index, value.selected_saturated)
```

The caller associates frames, calibration and stage/cycle/collection-point metadata. Mixed masks,
duplicate exposure times, unsupported geometry, pixels above 4095 and rings outside the image
are rejected. Original frame, mask and calibration bytes remain available; no calibration file
is changed. `BackgroundCalibration` accepts only the recovered version-5 native CSV layout.
Its coefficients retain more decimal precision than the EDS `background.ini`; substituting the
INI failed exact reconstruction for ten reference values. The CSV has no identity or expiry field,
so select it from the matching backup explicitly.

Processing follows these recovered steps:

1. Copy the OPB=0 image, rotate it 180° into the vendor mask coordinates, and select the native
   mask's nonzero entries. Physical filter 1 uses native F0, through physical filter 5/native F4.
2. Sum nonzero image pixels in each well. Subtract the mean of nonzero pixels in the adjacent
   eight-neighbor ring, multiplied by the selected nonzero-pixel count; clip below zero and truncate.
3. Apply the saved-filter background offset, native exposure scaling and slope correction.
   The saved-filter index is `6 - physical_filter`. Native effective milliseconds are
   `ceil((commanded_ms + 0.2746) / 0.1702) * 0.1702`; the normalization uses a 1000 ms reference.
4. Flag an exposure when more than six well pixels exceed 4000. Select the uniquely longest
   unflagged exposure. When no unique maximum exists, use the uniquely shortest original exposure;
   a further tie selects the first. This preserves the vendor fallback even when every exposure
   is saturated. **Check `selected_saturated` before treating a value as quantitative.**
5. Apply the saved-filter uniformity coefficient using the vendor's binary32 arithmetic.

Each measurement retains three `ExtractedWell` records (original pixel sums/counts, ring
sums/counts and saturation counts), normalized exposure candidates and the selected index.
`raw_value` means the **pre-uniformity vendor integer**, not an uncorrected camera sum.
`corrected_value` is the uniformity-corrected filter signal; neither is dye concentration, Rn or Ct.
No optical-black correction or dark-reference lookup is added to this reference path. The separately
available optical-black routine is not interchangeable with the recorded OPB=0 configuration.

All **960 integers** (96 wells × five filters × two cycles) exactly match the original saved EDS
arrays from the same 30 camera frames. Their **960 corrected binary32 values** match the separately
vendor-validated Java uniformity stage. Tests retain ten small recorded image patches, including
asymmetric orientation, precision-sensitive coefficients and saturated fallback cases. An 80-case
native oracle covers exposure choice, ties and disabled exposures. Full frames, calibration
backups and vendor binaries remain outside Git.

The [collection scheduler](synchronized-acquisition.md) now associates optical reads with thermal
holds. Two 15-frame collections and normal thermal completion were checked directly from Ubuntu.
Broader programs and matched vendor comparisons of unsaturated acquisitions still require
validation. This processing work does not require recalibration and does not renew the
instrument's expired calibration.

A subsequent shorter-exposure session produced 960 selections without saturation flags, using
30/10/5 ms at filters 2–5. Filter 5 required the 5 ms exposure for eight well/cycle results.
Fixed-exposure signals at filters 4 and 5 nevertheless declined by roughly 10% and 8% between
cycles; their cause remains unresolved. An unflagged result is not a certificate of quantitative
stability. See the [hardware report](hardware-validation.md) for the comparisons and limits.

## Diagnostic camera-to-well processing

Process an existing acquisition without opening USB:

```python
from pathlib import Path
from pylabrobot.thermo_fisher.abi7500_fast_dx import CameraWellData, PlatePixelMask

acquisition = await instrument.get_acquisition("plr-optics-001-f1")
mask = PlatePixelMask.from_file(Path("/path/to/ROIMask_W96_F0_I809_Commit.aoi"), filter=1)
pixels = CameraWellData(acquisition, mask, correction="optical_black")
for well in pixels.measurements:
    print(well.label, well.pixel_count, well.pixel_sum, well.mean)
```

Select the mask from the matching instrument calibration backup. The native F0 file corresponds
to acquisition filter 1, through F4/filter 5; the caller supplies this association explicitly.
The API verifies a complete, stationary, unchanged acquisition and matching filter indices.
It retains the entire acquisition, original mask bytes and their SHA-256. It cannot verify an
active instrument/mask association or physical A1 orientation from these files alone.

The {ref}`known-pattern plate validation <abi7500-wellmap-validation>` independently checked
physical orientation and labels A1, A4, C9, F3, H7 and H12 in filter 5 using three images of
fluorescein wells. The existing crop, rotation and masks matched without modification.
That experiment does not establish every well/channel or certify current calibration.

The parser supports the reference 96-well, uint16 matrix format and rejects unsupported schemas,
references, sizes, out-of-image coordinates, empty masks, truncation and trailing bytes. It uses
every nonzero mask entry once, rather than generating circles or weighting pixels by mask values.
Labels follow saved mask order. Statistics include zero-valued selected pixels.

Choose the image-processing mode explicitly:

- `correction="none"` uses the ArpLib OPB=0 crop, rotated 180° into mask coordinates.
- `correction="optical_black"` uses the ArpLib OPB=1 image, removes five rows, subtracts the
  truncated mean of the first 640 pixels, and adds 128. Lower values clip to zero; uint16 overflow
  wraps as in the vendor routine. The result is rotated 180° before mask extraction. The OPB=1
  and OPB=0 copy origins differ by one column.

`OpticalBlackImage(frame)` exposes this separate corrected image and its offset while preserving
the original frame. Its pixels match isolated native execution on 48 saved frames and six
rounding/clipping/overflow cases. Native mask iteration matches all 480 regions in the five saved
masks. The native comparison reconstructs AOI memory from the decoded file; it does not exercise
the vendor's MFC deserializer or establish end-to-end fluorescence accuracy.

`CameraWellData` reports **diagnostic pixel sums and means**, not `SavedFilterData`. It omits the
vendor's surrounding-pixel correction, threshold/saturation handling, exposure scaling/splicing,
background calibration and uniformity correction. `apply_uniformity` rejects these results.
Use `CameraFilterResult` for the reference processing path described above. Diagnostic sums and
means retain their separate meaning and are not interchangeable with reconstructed filter signals.
