# ABI 7500 Fast Dx

```{device-card} thermo-fisher-abi7500-fast-dx
```

```{warning}
This integration remains experimental. One 35 °C/60-second hold completed and a separate
start/cancel succeeded on G2.10. Direct Ubuntu sessions verified snapshots, filter 0/1/5 and
lamp enable/power/disable; illuminated well patterns were observed at filters 1 and 5.
Offline processing reconstructs all 960 vendor integers and their uniformity-corrected values
from one recorded acquisition. A two-cycle 35 °C/30 °C run completed normally with 30 images
acquired within its scheduled holds. Broader thermal programs and fault recovery remain
unverified. These results do not renew expired calibration or validate an assay.
See the [hardware report](hardware-validation.md) and [thermal-run limits](thermal-runs.md).
```

The driver communicates through `pylabrobot.io.usb.USB` without vendor DLLs. Thermal runs
use the captured Standard-mode, 20 µL, block-2 configuration and a persistent local journal.
Raw snapshots preserve original images and metadata. Offline processing uses explicitly selected
pixel masks, native background coefficients and uniformity factors, retaining intermediate
statistics, exposure selection and saturation flags alongside corrected filter measurements.
An optional `CollectionPlan` schedules those reads during selected thermal holds. Two 15-frame
collections were checked directly from Ubuntu; original frames and 960 derived well values were
saved, and the entire optical run reached normal completion with matching instrument history.

A completed two-cycle vendor acquisition supplies matching USB frames and saved well outputs
for exact numerical comparison. This supports the recorded processing configuration; the new
PLR run has no matching vendor output. Dye unmixing, Ct analysis and assay interpretation are
not implemented.
Firmware updates and recalibration remain outside the integration's scope.

```{toctree}
:maxdepth: 1

hello-world
protocol
thermal-runs
synchronized-acquisition
raw-snapshots
optics-and-measurements
hardware-validation
capture
```
