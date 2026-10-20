# ABI 7500 Fast Dx

```{device-card} thermo-fisher-abi7500-fast-dx
```

```{warning}
This integration remains experimental. One 35 °C/60-second hold completed and a separate
start/cancel succeeded on G2.10. Direct Ubuntu sessions verified snapshots, filter 0/1/5 and
lamp enable/power/disable; illuminated well patterns were observed at filters 1 and 5.
Offline processing reconstructs all 960 vendor integers and their uniformity-corrected values
from one recorded acquisition. A two-cycle 35 °C/30 °C run completed normally with 30 images
acquired within its scheduled holds. A five-cycle 95 °C/60 °C water-plate method also
completed on C9254-02/G2.10 with 15 images and final cooling. A forty-cycle 95 °C/60 °C
fluorescein-plate run completed on the same unit with all 120 scheduled images, cooling and
tray release. Five cycles with shorter 30-second collection holds also completed with all
15 images and homing finishing 7 seconds before each hold ended.
A separate six-marker scan verified orientation and six physical well locations on that second
unit; its exploratory image grid does not establish calibrated per-well fluorescence.
A two-filter timing check captured eighteen images within three 30-second holds. A known
ROX/fluorescein plate then supported 24 ROX-channel locations and a six-frame raw well export;
green-channel discrepancies and quantitative region boundaries remain unresolved.
Amplification, broader thermal programs and fault recovery remain unverified.
These results do not renew expired calibration or validate an assay.
See the [hardware report](hardware-validation.md) and [thermal-run limits](thermal-runs.md).
```

The driver communicates through `pylabrobot.io.usb.USB` without vendor DLLs. Thermal runs
use the captured Standard-mode, 20 µL, block-2 configuration and a persistent local journal.
Raw snapshots preserve original images and metadata. Offline processing uses explicitly selected
pixel masks, native background coefficients and uniformity factors, retaining intermediate
statistics, exposure selection and saturation flags alongside corrected filter measurements.
When matching vendor masks are unavailable, explicitly supplied diagnostic regions support
{ref}`raw per-well exports <abi7500-raw-regions>`, with
each exposure, clipping flags and provisional region provenance retained. These values are
uncalibrated camera counts.
An optional `CollectionPlan` schedules those reads during selected thermal holds. Two 15-frame
collections were checked directly from Ubuntu; original frames and 960 derived well values were
saved, and the entire optical run reached normal completion with matching instrument history.

A completed two-cycle vendor acquisition supplies matching USB frames and saved well outputs
for exact numerical comparison. This supports the recorded processing configuration; the new
PLR run has no matching vendor output. Dye unmixing, Ct analysis and assay interpretation are
not implemented.
An [offline optical-calibration calculation](optical-calibration.md) generates new uniformity
coefficients from final camera collections, matching ROI masks and background calibration.
An [offline pure-dye calculation](pure-dye-calibration.md) reproduces the saved ROX spectrum
and distinguishes native diagnostics from Java correction decisions. A fresh Ubuntu ROX session
completed all 65 fixed-exposure images and cleanup; unreplaced saturation at A10 and B10 blocked
calibration-file export. Automatic exposure selection reproduces all 35 vendor search probes
offline; its acquisition path and the optional 90 °C cover setting await hardware validation.
Firmware updates remain outside scope.

```{toctree}
:maxdepth: 1

hello-world
pcr-run
protocol
thermal-runs
synchronized-acquisition
raw-snapshots
optics-and-measurements
optical-calibration
pure-dye-calibration
hardware-validation
capture
```
