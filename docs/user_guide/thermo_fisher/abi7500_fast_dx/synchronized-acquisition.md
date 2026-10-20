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
  park_between_collections=True,
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
A subsequent approved 30 °C run with position-0 parking reduced these declines to about
0.8% and 0.9%. It confirmed camera-path suppression with the lamp on, but neither proves
photobleaching nor tests the vendor’s home operation. See the
{ref}`parking comparison <abi7500-parked-validation>`.

Evaluate control completion and measurement quality separately. Retain all saturation flags;
compare exposure-normalized candidates only when their source exposures are unsaturated, and
do not automatically retry if saturation persists. The existing sealed plate can support this
engineering check. A separate {ref}`known-pattern plate session <abi7500-wellmap-validation>`
verified physical orientation and six labels in filter 5. Broader label/channel coverage,
current calibration validity and vendor comparison remain separate questions.

### Green/reference timing check

Physical filter 5 corresponds to the vendor's green reporter channel; physical filter 2
corresponds to its ROX-associated channel. Collecting both requires six images per collection,
two filter moves and collection-end closure. On C9254-02, the approved `plr-u2dual-001` session
completed three such collections in **18–19 instrument seconds each**, including native homing,
with **6 seconds remaining** in each 30-second hold. All eighteen images were complete and
associated with their intended holds. See the {ref}`two-filter timing report <abi7500-dual-timing-validation>`.

The following configuration reproduces that timing check without a PCR reaction. It uses three
60 °C/30-second holds followed by 30 °C/60-second cooling, with a 25-second read window and native
home after each burst. The existing sealed dye plate supported the timing check. A later
{ref}`known ROX/fluorescein layout <abi7500-rox-layout-validation>` supported ROX detection and
24 filter-2 physical locations during one 30 °C hold; quantitative region boundaries and
calibrated reporter/reference processing remain unverified.
Filter 2 exposures below come from this unit's saved table, not a verified setting for the
user's reaction. Its 540 and 180 ms frames contained clipped pixels; 60 ms did not. The table
was read without modification.

```python
program = ThermalProgram(reference_metadata, (
  ThermalCycle(3, (ThermalStep(60, 30),)),
  ThermalCycle(1, (ThermalStep(30, 60),)),
))
plan = CollectionPlan(
  steps=((0, 0),),
  filters=(
    CollectionFilter(2, (0.540, 0.180, 0.060)),
    CollectionFilter(5, (0.030, 0.010, 0.005)),
  ),
  read_window=25,
  park_between_collections=True,
  home_after_collections=(1, 2, 3),
)
assert len(plan.points(program)) == 3  # 18 images across the three collection holds.
```

Constructing this plan does not start the instrument. The session met the prepared acceptance
criterion of eighteen complete images in their intended holds and at least three reported hold
seconds remaining after each home. Countdown differences have integer-second resolution; these
three observations do not establish a worst-case bound or a full forty-cycle two-filter method.
The 1,800-second observation budget separately covered cover warmup, ramps and cooling.
No hold is extended if the optical work runs late. After a failure, preserve the original data,
reconcile/cancel only the owned run, and restore idle optics using the normal recovery procedure.

Scripted USB tests cover ordered two-filter acquisition, concurrent duplicate requests and cached
retrieval after restart. They also cover insufficient time before/after the second filter move,
incorrect filter readback, a frame crossing the hold boundary, late homing and a lost fourth
exposure response. These tests establish failure behavior, **not physical throughput**.
No raw filter ratio is labeled Rn; a filter-5 diagnostic region map must not be reassigned to
filter 2 without independent evidence.

### Collection window behavior

The default window is 45 seconds. Collection starts when the remaining hold time reaches 45–43
seconds. Every selected effective hold must be at least the full window, including hold-time
increments over repetitions. A missed window fails instead of extending the hold or silently
skipping data. Filter moves, exposure time, transfers, status queries and local journal writes all
consume the window. The exposure-duration sum is only a minimum feasibility check; the window
must be validated on the control host. It is not a measured worst-case bound.

`park_between_collections=True` returns to absolute filter position 0 after every burst,
including the last, while retaining lamp power 1. It verifies that the wheel is stationary and
records the native replies in `collection.record.parked_optics` before accepting the collection.
The parking move and final status check must fit inside the same hold. The default is `False`
for compatibility with existing plans; older journal records retain that behavior. Changing
the setting requires a new run identity. Parking does not home the wheel or change calibration.

`home_after_collections=(2, 4)` requests native homing after the second and fourth collection
of a four-collection plan, with absolute parking after the others. These are **one-based
collection ordinals**, not thermal step indices. The immutable, sorted tuple requires parking
enabled and may contain only existing ordinals. An empty tuple preserves ordinary parking;
older journals never acquire homing implicitly. Each collection's `homing_requested` intent is
saved before its first movement. The final `parked_optics` readback must show stationary
position 0 and unchanged lamp power after either operation. A lost acknowledgement, motion
timeout or failed readback cannot trigger a repeated home, even after reconnecting. Changing
homing ordinals requires a new run identity.

The vendor's recorded collection-end path sends asynchronous home command `0x67`.
Native code and the original USB capture confirm that difference. Homing could change optical
alignment as well as the idle position; position readback alone cannot establish either physical
alignment or sample illumination. A four-collection Ubuntu session verified two native homes,
60 images and normal completion; the observed signal changes did not establish an additional
stability benefit over parking. See the {ref}`homing comparison <abi7500-homing-validation>`
and [recovered command paths](protocol.md). A subsequent approved two-cycle 35 °C/30 °C session
combined short exposures, homing after both collections and checkpoint-only progress reads.
It completed normally with 30 frames and 960 unflagged selections; the SYBR-channel decline
was 0.95%, compared with 7.72% in the earlier cycling session that left the last filter selected.
Different lamp warmup and plate history prevent a causal interpretation. See the
{ref}`combined cycling/homing validation <abi7500-cycling-home-validation>`. A later ten-cycle SYBR-channel-only session completed with
30 images, ten verified homes and a lossless offline export. It used three exposures per
collection at physical filter 5; it does not establish ten-cycle performance across all five
channels. See the {ref}`ten-cycle report <abi7500-sybr10-validation>`.

## Execute an approved session

Use the same local journal as earlier runs. Begin with a sealed, loaded-ready plate, inactive
thermal controller, reference camera geometry, stationary filter 0 and lamp enabled at power 1.
Allow the lamp to settle. The two-collection examples used five seconds; the four-collection
parking/homing comparison used 30 seconds at position 0 before Start. Neither duration alone
establishes constant optical output. The code below shows the
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

### Normal Start from lamp standby

`start_run(..., collection_plan=plan, standby_startup=True)` reproduces the captured normal
experiment's upload → lamp power 1 → exposure-table read → plate load → wheel home → Start `0x13`
order. Before calling it, enable the lamp at standby power 0 behind stationary filter 0 and allow
the explicitly planned settling time. This mode requires a collection plan and cannot be combined
with `calibration_startup` or `cover_temperature`. Normal Start uses the instrument's cover
behavior, approximately 105 °C in the reference; it does not preserve a saved 90 °C XML setting.
The default startup remains unchanged.

This mode accepts the captured 20 or 30 µL Standard/block-2/tube-0 controller header. Record the
actual reaction volume separately when it differs. Startup intent is part of the run identity
and journal format 6; older controllers refuse that journal. An interrupted lamp transition,
home or Start is never automatically repeated. Legacy checkpoints restore with
`standby_startup=False`.

Use `home_after_collections=tuple(range(1, collection_count + 1))` with
`park_between_collections=True` when the reference homes after every collection. The entire
collection window must include those moves and PLR's per-image status checks. The native vendor
window is not automatically a sufficient PLR budget. No thermal hold is extended to finish an
overrun. Application startup images, prior illumination and operator delays are not reproduced
by this API. Offline transport tests cover the startup ordering and ambiguous acknowledgements.
Direct Ubuntu validation on C9254-02/G2.10 completed eight five-filter, three-exposure collections
with normal Start, approximately 105 °C cover behavior and home after every collection.
The 55-second window left 20 reported seconds after each complete burst and home. The ROX signal
differed from the separate Windows reference by a median −1.21% at 540 ms; timing and illumination
history still differed. See the
[hardware comparison](hardware-validation.md#normal-start-five-filter-rox-comparison-2026-10-20)
for per-well variation and the limits of this single-session validation.

## Read saved collection progress

`get_collection_records(run_id)` returns the typed checkpoints without USB traffic or loading
image payloads. It works without `setup()` and includes both complete and uncertain collection
intents in planned order. Missing points have not been reserved. For progress during a run, use
the same driver instance as the collector; the run lock may delay the call until the current
burst finishes or fails. A second controller cannot open the same journal concurrently.

```python
records = await instrument.get_collection_records(optical_run_id)
print("Completed collections:", sum(record.state == "complete" for record in records))
for record in records:
  print(record.ordinal, record.point, record.state, record.error)
```

These are the last committed checkpoints, not fresh instrument observations or an integrity
check of the original images. Use `get_run()` for thermal reconciliation and `get_collections()`
for complete or partial frames. Thermal completion and complete optical collection are separate
outcomes; inspect `collection_error` as well. Polling in `collect_fluorescence()` reads these
small records and only loads the saved images when returning results after normal completion.
It rejects gaps and uncertain checkpoints instead of filling them with repeated acquisitions.
This avoids repeatedly decoding the accumulated images while waiting for the next read window;
USB exchanges, exposure reservation and other journal work still consume host time.

(abi7500-export)=
## Export a completed run offline

After the instrument session and cleanup, `export_run()` reads the saved local journal without
calling `setup()` or sending USB commands. Supply the background, uniformity and masks belonging
to the acquisition, as in the processing example below. Choose a **new file outside Git** in an
existing directory:

```python
from pathlib import Path

archive = await instrument.export_run(
  optical_run_id,
  Path.home() / "Downloads" / f"{optical_run_id}-results.zip",
  background=background,
  uniformity=uniformity,
  masks=masks,
)
print(archive)
```

It requires a saved, completed thermal run with matching history, no collection error, and every
planned collection and image in order. It rejects missing, uncertain or mismatched data rather
than exporting a partial table as a successful experiment. Use `get_collections()` to inspect
and retain evidence from incomplete sessions. Export does not reconcile current hardware state
or perform cleanup. Processing and file generation run synchronously, so use it after acquisition.

The ZIP contains:

- `measurements.csv`: one selected result per well/filter/collection, with run and frame identities,
  physical and vendor filter indices, exposure choice, saturation flag, pre-uniformity integer,
  corrected value and its exact binary32 bits.
- `exposures.csv`: all three candidates for every well, including unselected or saturated ones,
  original pixel/ring counts, background coefficients, exposure-normalized integers, host times
  and before/after sample-temperature estimates.
- Original camera `.bin` transfers and acquisition JSON, collection checkpoints, and `run.json`
  with the thermal program, optical plan, instrument identity and completion evidence.
- Exact supplied calibration files and masks, `README.txt` describing field meanings and units,
  and a versioned `manifest.json` with the size and SHA-256 of every other member.

`pre_uniformity_value` is already background/exposure corrected; it is not an uncorrected pixel
sum. CSV values retain round-trip precision, and saturated fallbacks stay explicitly flagged.
Stage and cycle are one-based; step and exposure indices are zero-based. The archive retains
vendor well labels A1–H12. Physical validation is limited to the
{ref}`six-marker filter-5 check <abi7500-wellmap-validation>`; export itself does not certify
orientation or current calibration validity. It adds no drift correction, dye unmixing,
Rn or Ct analysis and is not a vendor EDS file.

Existing destinations are never overwritten. Validation and processing finish before creation;
ordinary write failures remove the new partial file. A process or power failure can still leave
an incomplete ZIP: verify the manifest and member hashes before treating it as a completed
export. These hashes detect content changes, not authenticity. The archive is portable; the live
journal must remain on local, unsynchronized storage.

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
When parking is requested, a failed park or home also makes the collection uncertain, while preserving
all completed frames. A failed or interrupted burst does not attempt an automatic park through
a possibly faulted connection; use the explicit recovery and cleanup below.

Neither a failed collector nor `stop()` cancels the instrument or disables the lamp. End the
session by reconciling the same run ID, calling `cancel_run()` if that run is still owned and
active, and disabling the lamp/restoring filter 0 after confirmed inactivity. After a communication
fault, explicitly close/reopen with the same journal before reconciliation. Do not repeat Start
or switch journals. A cancellation request can obtain the run lock between complete bursts;
cancelling the Python collector task interrupts its current operation and records uncertainty.
Hardware recovery after an ambiguous exposure remains unverified.

After terminal cleanup, call `release_plate(run_id)` to confirm the internal plate mechanism is
unloaded, then close USB in a `finally` block. Release independently checks fresh matching history
and inactivity; a cached completed result cannot authorize another run's plate. An uncertain
release must be reconciled with the same ID before other actuation. See
{ref}`plate release and recovery <abi7500-plate-release>` and the complete cleanup example in the
[notebook](hello-world.ipynb). The public release path and duplicate checks after clean reconnect
were verified in a separate {ref}`lamp-off hold session <abi7500-trayapi-validation>`; recovery
after ambiguous communication failure remains unverified. Releasing the plate does not cool the
heated cover.

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
