# Fluorescence collection during thermal cycling

```{warning}
This API is experimental. Ubuntu PLR completed **two 35 °C/30 °C cycles normally with 30 images**,
with five filters and three exposures per filter during each 30 °C hold. The approved session used
a 900-second observation budget and matching completed-run history, then restored lamp off and
filter 0. A shorter-exposure repeat removed selected saturation flags on the tested plate, but
signals at filters 4 and 5 declined between cycles. Broader thermal programs, quantitative
stability and fault recovery remain unverified. These are engineering results, not validated
assay measurements.
See the [hardware report](hardware-validation.md).
Each new hardware run requires explicit approval.
```

`CollectionPlan` schedules camera reads from the thermal controller's hold countdown. The thermal
program runs on the instrument; PLR moves the filters and obtains three exposures per selected
filter during designated holds. The vendor also schedules from status, rather than using a
hardware camera trigger. This establishes a recorded association with a hold, not subsecond
synchronization or simultaneous readings across filters.

## Specify collection points offline

```python
from pylabrobot.thermo_fisher.abi7500_fast_dx import (
  CollectionFilter, CollectionPlan, ThermalCycle, ThermalProgram, ThermalStep,
)

# reference_metadata is the supported header from the hello-world notebook.
program = ThermalProgram(reference_metadata, (
  ThermalCycle(2, (ThermalStep(35, 15), ThermalStep(30, 60))),
))
plan = CollectionPlan(
  steps=((0, 1),),  # Zero-based cycle group and step; collect on every repetition.
  filters=(
    CollectionFilter(1, (0.540, 0.180, 0.060)),
    CollectionFilter(2, (0.540, 0.180, 0.060)),
    CollectionFilter(3, (0.900, 0.300, 0.100)),
    CollectionFilter(4, (0.540, 0.255, 0.085)),
    CollectionFilter(5, (1.048, 0.090, 0.030)),
  ),
  read_window=45,
)
print(plan.points(program))
```

This engineering example requests two collections, each with 15 original camera frames. It is
not an amplification protocol. Exposure times are seconds and filter positions are physical
indices, not dye names. The settings reproduce the reference acquisition's exposure choices;
they are not automatically optimized for a new plate and can produce saturated values.

The completed `plr-sync-003` session produced 578 saturated selections. Every one of those
well/cycle measurements was saturated at **all three** recorded exposure times, so selecting
another saved frame cannot recover it. Across the two cycles, each physical filter contributes
192 well measurements:

| Physical filter | Checked exposures (ms) | Saturated at shortest exposure / 192 |
|---|---|---|
| 1 | 540 / 180 / 60 | 0 |
| 2 | 540 / 180 / 60 | 170 |
| 3 | 900 / 300 / 100 | 117 |
| 4 | 540 / 255 / 85 | 111 |
| 5 | 1048 / 90 / 30 | 180 |

The completed `plr-lowexp-001` session kept filter 1 unchanged and requested
**30 / 10 / 5 ms** at filters 2–5. It retained the same two 35 °C/15 s then 30 °C/60 s cycles,
30-frame limit, lamp power 1, 45-second collection window and 900-second observation budget.
Both cycles completed normally, and **all 960 selections were unflagged**. Some 30 ms and 10 ms
frames still saturated wells; filter 5 required the 5 ms exposure for eight well/cycle results.
All 5 ms well extractions were unflagged. These plate-specific settings do not guarantee
unsaturated measurements on a different plate. Every additional hardware run needs approval.

Reduced saturation does not establish quantitative stability. At fixed, unsaturated 5 ms
exposures, median cycle-2/cycle-1 signal ratios were approximately **0.898 at filter 4** and
**0.923 at filter 5** (96 wells each). The decline also appears at other unsaturated exposures,
so it is not explained solely by exposure selection. An offline audit found the decline in
uncorrected pixels and a similar pattern in the earlier vendor-controlled acquisition, where
longer exposures limited the number of unclipped wells. Its cause remains unresolved. See the
{ref}`offline drift audit <abi7500-offline-cycle-drift-audit>` for sample counts,
processing-stage comparisons. A separately approved constant-30 °C run also completed with
30 images and no selected saturation flags. At fixed 5 ms exposure, median signals still fell
about 6.1% in vendor channel 1 (SYBR/FAM, physical position 5) and 7.5% in vendor channel 2
(physical position 4). The 35 °C excursions are therefore not required for the decline; its
cause remains unresolved. See the {ref}`constant-temperature comparison <abi7500-constant30-validation>`.

Evaluate control completion and measurement quality separately. Retain all saturation flags;
compare exposure-normalized candidates only when their source exposures are unsaturated, and
do not automatically retry if saturation persists. The existing sealed plate can support this
engineering check. Independent physical well orientation requires a known plate pattern, and
current calibration validity and vendor comparison remain separate questions.

The default window is 45 seconds. Collection starts when the remaining hold time reaches 45–43
seconds. Every selected effective hold must be at least the full window, including hold-time
increments over repetitions. A missed window fails instead of extending the hold or silently
skipping data. Filter moves, exposure time, transfers, status queries and local journal writes all
consume the window. The exposure-duration sum is only a minimum feasibility check; the window
must be validated on the control host. It is not a measured worst-case bound.

## Execute an approved session

Use the same local journal as earlier runs. Begin with a sealed, loaded-ready plate, inactive
thermal controller, reference camera geometry, stationary filter 0 and lamp enabled at power 1.
Allow the lamp to settle; the prepared validation uses five seconds. The code below shows the
collection portion of a session, after those prerequisites have been established:

```python
optical_run_id = "plr-optical-001"  # A new ID represents a separate intended experiment.
await instrument.start_run(optical_run_id, program, collection_plan=plan)
collections = await instrument.collect_fluorescence(optical_run_id, timeout=900)
```

Run these two calls together. `start_run()` uploads only the thermal method; it does not start a
background camera task. `collect_fluorescence()` must remain running until thermal completion.
The [hello-world notebook](hello-world.ipynb) includes lamp preparation and cleanup around both
calls. An overall timeout is checked between observations/collection bursts; an in-progress
burst still uses bounded per-command deadlines. The timeout includes **cover warmup, ramps,
all holds, acquisitions and completion reconciliation**. One session's 450-second limit
was too short after 263 seconds of cover warmup, despite fitting every image inside its hold.
The successful repeat used 900 seconds and began with an already-warm cover. Budget for the
entire session; 900 seconds is not a universal bound for other programs or startup states. Expiry does not imply
that the thermal controller completed or stopped.

The scheduler reconciles identity and independent status/history/status observations. A read
straddling startup or completion can be temporarily `uncertain`; it continues observing without
actuation. Only coherent evidence of the owned, active run permits a collection. Each frame has
fresh status immediately before exposure and after transfer/readback; run token, group, step,
repetition, holding phase and optical state must agree. The whole burst is checked too. Crossing
a boundary retains the image but marks the acquisition and collection uncertain.

## Preserve identities and recover explicitly

The journal stores the program and optical plan under the run ID. Reusing an ID with a changed
program or plan is rejected; repeating it with identical settings returns its checkpoint without
another Start. Each point has a deterministic ID such as `plr-optical-001-c0-r1-s1`, and its
frames add `-f1-e0` through the configured filter/exposure indices.

A collection intent is committed before filter motion, and an exposure intent before CJ/CC.
Missing acknowledgements, interruption, timeout or a missed hold cannot cause automatic exposure
replay. Complete points survive reconnects; an uncertain point or recorded `collection_error`
blocks resuming acquisition. A completed run with all points present returns its saved results
without USB I/O. `get_collections(run_id)` retrieves partial and complete evidence offline.

Neither a failed collector nor `stop()` cancels the instrument or disables the lamp. End the
session by reconciling the same run ID, calling `cancel_run()` if that run is still owned and
active, and disabling the lamp/restoring filter 0 after confirmed inactivity. After a communication
fault, explicitly close/reopen with the same journal before reconciliation. Do not repeat Start
or switch journals. A cancellation request can obtain the run lock between complete bursts;
cancelling the Python collector task interrupts its current operation and records uncertainty.
Hardware recovery after an ambiguous exposure remains unverified.

## Process saved collections offline

```python
from pathlib import Path
from pylabrobot.thermo_fisher.abi7500_fast_dx import (
  BackgroundCalibration, PlatePixelMask, UniformityCalibration,
)

# Select the calibration backup belonging to this instrument/acquisition explicitly.
backup = Path("/path/to/matching/calibration-backup")
background = BackgroundCalibration.from_file(backup / "Background.csv")
uniformity = UniformityCalibration.from_file(backup / "uniformity.ini")
masks = tuple(
  PlatePixelMask.from_file(backup / f"ROIMask_W96_F{p-1}_I809_Commit.aoi", filter=p)
  for p in range(1, 6)
)
collections = await instrument.get_collections(optical_run_id)
for collection in collections:
  for result in collection.filter_results(background, uniformity, masks):
    for value in result.measurements:
      print(result.stage, result.cycle, result.collection_point, result.filter,
            value.label, value.raw_value, value.corrected_value, value.selected_saturated)
```

Only complete, correctly ordered and consistently associated frames can produce filter results.
Stage is the one-based cycle group, cycle is its repetition, and collection point is the plan's
one-based ordinal. Physical filters 1–5 map to saved filters 5–1. Every original image, exposure,
status payload and optical readback is retained. Processing retains masks, coefficient sources,
intermediate values and saturation selection; it does not overwrite the originals.

`raw_value` is the vendor-compatible pre-uniformity integer, not an uncorrected pixel sum.
`corrected_value` applies the selected uniformity coefficients. This API produces no EDS, dye
unmixing, Ct or assay interpretation. Matching an old vendor acquisition does not establish current
calibration validity or make saturated measurements quantitative. See
[optics and measurements](optics-and-measurements.md) for the processing evidence and limitations.
