# Experimental thermal-only runs

```{warning}
One 35 °C/60-second hold completed normally and a separate start/cancel succeeded from Ubuntu on
G2.10; see the [hardware report](hardware-validation.md). A later two-cycle 35 °C/30 °C optical
session completed normally with 30 images. Wider programs, cancellation during
startup and recovery after communication faults remain unverified.
This API can move the internal plate-loading mechanism and heat the block and cover. It does not
collect camera frames or fluorescence, produce an EDS experiment, or perform Ct analysis.
```

## Supported scope

`ABI7500FastDx.start_run(run_id, program)` accepts the captured G2.10 firmware configuration:
Standard mode, 20 µL reaction volume, block ID 2, tube type 0, and finite `ThermalProgram` cycle/hold
records. Other header configurations are rejected. The reference header's date records are retained;
they do not set the instrument clock. The explicit run ID replaces the method name and the user name
becomes `PLR`. The controller keeps step timing on the instrument.

The driver checks inactivity, prepares/uploads the program, loads the plate if needed, waits for
the loaded state, checks inactivity again, then sends Start once. Ensure the sealed plate is
physically ready for loading before this operation. It uses the instrument's existing cover
behavior, which the reference run observed around 105 °C; a low sample setpoint does not limit the
cover to that temperature. Custom cover settings and Fast mode are not implemented.
After a finished run, `release_plate(run_id)` can release the internal plate mechanism;
it does not open the external drawer or cool the cover.
An explicit optional `collection_plan` enables [scheduled optical reads](synchronized-acquisition.md);
without that plan this API remains thermal-only.

The [hello-world notebook](hello-world.ipynb) includes a 35 °C, 60-second engineering example and
separate start, observation, cancellation and reconnect cells. These hardware cells must not be
executed as part of offline notebook validation.

## One durable journal per instrument

Pass a stable local path as `ABI7500FastDx(run_journal=Path(...))`, such as
`~/.local/state/pylabrobot/abi7500/runs.sqlite3`. Keep this journal outside Git and outside Seafile
or another network/synchronization folder. Back up a closed journal if needed. Reuse it across
driver instances and process restarts; do not delete, restore an older copy, or switch paths to
retry an uncertain run. The `.lock` SQLite sidecar provides exclusive controller ownership and is
released on `stop()` or process exit. Another driver using the same journal cannot control it
concurrently. Separate journal paths and other control software bypass that boundary: retain
exclusive access to the physical instrument, with Windows disconnected.

Run IDs contain one to sixteen ASCII letters, digits, underscores or hyphens. A repeated
`start_run()` with the same ID and program returns its checkpoint without sending commands. The
same ID with a different program or collection plan is rejected. Every distinct ID represents a separate intended
experiment. An unresolved checkpoint blocks starting another ID. A matching method name in the
last instrument history also blocks a fresh start when the local record is absent, but it cannot
replace a complete journal of earlier runs.

The journal commits a `starting` checkpoint **before** transmitting Start. A lost acknowledgement,
Python task cancellation or process failure therefore cannot cause an automatic Start replay. If
failure occurs before Start was attempted, the checkpoint is `not_started`; that same ID is still
not retried automatically. Inspect the evidence before deliberately scheduling another experiment.

## Observe and reconcile

`get_run(run_id)` returns a `ThermalRun` with the original program, instrument identity, state,
latest `ABI7500Status` and `ABI7500History`. Terminal results are read from the journal. Unresolved
runs are checked against fresh identity and status/history/status exchanges.

| State | Meaning |
|---|---|
| `preparing` | Durable intent; Start has not yet been attempted |
| `starting` | Start is being submitted or acknowledged; fresh sequence evidence is pending |
| `running` | A fresh active sequence has been associated with the run |
| `cancelling` | Cancellation intent is recorded; stop/idle-target confirmation is pending |
| `uncertain` | Evidence is missing, inconsistent, unknown, or belongs to another run |
| `completed` | Inactive status and matching history report normal thermal completion |
| `cancelled` | Matching history reports abort after cancellation, and the 25 °C idle target was acknowledged |
| `aborted` | Matching history reports abort without a PLR cancellation request |
| `not_started` | PLR did not attempt Start |

After a communication fault, close and reopen with `stop()`/`setup()` and the same journal, then call
`get_run()`. Reconciliation never starts or stops a sequence. An acknowledged start can be bound to
a fresh active start-date record under exclusive control. After a lost acknowledgement, binding
requires the run's method/user labels in history and a changed start-date record matching status.
If history has not caught up, the result remains `uncertain`; elapsed time or a plausible temperature
does not establish ownership. Timestamp reuse, reset, conflicting labels, or a status transition
across the history read likewise must not be treated as proof of success.

Startup can take minutes while the cover warms. The approved 35 °C test remained `starting` for
about 273 seconds, with the sample at 25 °C and the previous run's history still visible. The
`command_timeout` limits an individual USB exchange, not this startup interval. Keep observing the
same ID; an observation timeout does not cancel the instrument or authorize another Start.
`cancel_run()` currently requires a fresh owned run record, so it cannot cancel this startup phase
before that evidence appears. History outcome `no_result` (253) can appear during an active run;
it does not mean that no run started.

`completed` reports thermal history only. It does not establish fluorescence acquisition, calibrated
temperatures, assay success, or that the plate and cover are cool.

Normal completion does not send the cancellation path's 25 °C idle request. After the checked
35 °C/60-second hold, inactive status still reported sample/block at 35 °C and cover near 105 °C.
Neither `stop()` nor `cancel_run()` on an already completed checkpoint changes those setpoints.

## Explicit cancellation

`cancel_run(run_id)` reconciles identity first. It sends Stop only when the active sequence is
positively associated with that run, polls until inactive, then requests the captured 25 °C idle
target. It refuses to stop an unidentified active sequence. Repeating cancellation after an uncertain
response checks state again: if already stopped, it skips Stop and completes the idle-target step.
An unresolved timeout is retained in the journal and does not imply physical cancellation.

`stop()` only closes USB and releases journal ownership. Cancelling an asyncio task only interrupts
the host operation; it does not request instrument cancellation. `cancel_run()` does not eject the
plate or set a cooler cover temperature, and its idle-target acknowledgement does not mean the
sample has reached 25 °C. Poll status for the actual reported temperatures.

(abi7500-plate-release)=
## Plate status and explicit release

`get_plate_status()` returns `ABI7500PlateStatus` with the complete two-byte payload, `state_code`
and `state`: `unloaded` (0), `moving` (1), `loaded` (2), or `unknown`. This is the internal mechanism;
state 0 does not prove that the external drawer is open, that a plate is present, or that it is cool.
This query requires a connection but no run journal and does not move the mechanism.

`release_plate(run_id, timeout=30)` requires a journaled `completed`, `cancelled` or `aborted` run on
G2.10. It checks the connected identity and fresh status/history against the exact run ID, `PLR`
user label, start record and outcome. The thermal controller must be inactive, the lamp off and
filter 0 stationary. These checks apply even when `get_run()` returned a cached terminal result.
An unfinished or `not_started` checkpoint cannot authorize release. Reconcile/cancel an owned
active run first; ambiguous ownership or failed loading requires inspection, not a release bypass.

For an approved thermal session, after observations or explicit cancellation:

```python
try:
    run = await instrument.get_run(run_id)
    if run.state not in ("completed", "cancelled", "aborted"):
        raise RuntimeError("Resolve the run before cleanup")
    if run.plate_release_state != "uncertain":
        await instrument.set_lamp(False)
        await instrument.set_filter_position(0)
    plate = await instrument.release_plate(run_id, timeout=30)
    print(plate.state)
finally:
    await instrument.stop()
```

Keep hands clear during release. A loaded mechanism receives one empty `0x72` command; the driver
then polls `0x73` until unloaded and repeats the ownership/optics checks. Already unloaded needs
no motion. The method does not cool the sample or cover, cancel a run, or acquire images.
`stop()` only closes communication; it never performs this cleanup implicitly.

`ThermalRun.plate_release_state` is `not_requested`, `uncertain`, or `released`. The driver commits
`uncertain` **before** dispatch. Lost acknowledgement, timeout, Python cancellation or process
failure cannot cause a second release command. After a communication fault, call `stop()`/`setup()`
with the same journal, then `release_plate()` with the same run ID. This only observes the existing
attempt. Do not repeat the lamp/filter setters while a release is uncertain: the journal blocks
other actuation until release is confirmed. A still-loaded/moving mechanism times out without
replaying the command. If the plate moves after confirmed release, the old run ID cannot release
it again. Do not change IDs or restore an older journal to bypass this guard.

The polling budget starts after acknowledgement (or during reconciliation); each in-flight
exchange can additionally take up to `command_timeout`. Timeout does not imply that motion stopped.
Current status and history must still match before an unloaded result is accepted. The journal
reads format 1 and 2; committing release state upgrades format 1 to 2 in the same transaction so
older drivers cannot ignore it. Reading an existing format-1 journal alone does not upgrade it.

Original vendor release packets and Ubuntu loaded → moving → unloaded transfers are retained in
`plate_release_vectors.json`. The {ref}`known-pattern report <abi7500-wellmap-validation>` records
successful research-runner release and the preceding operator-confirmed drawer opening. Public
API guards, restart behavior and injected failures are tested offline; the public method itself
and ambiguous-failure recovery still need a separately approved hardware session.

## Remaining hardware validation

Session `abi7500-plr-cancel-001` established loading, acknowledged Start, delayed startup, fresh
active progress, cancellation with the plate loaded, tagged aborted history and the idle-target
acknowledgement. A clean reconnect during startup reused the existing journal without another
Start. Cancellation occurred before the sample reached 35 °C, so this did not validate a complete
hold, temperature accuracy or normal completion.

Session `abi7500-plr-hold-001` subsequently completed the same program without a Stop command.
The hold countdown ran from 60 to 1 seconds, and matching completed history recorded 73 seconds
including the ramp. Holding-phase sample estimates were 34.6–35.2 °C, including initial settling.
These readings do not provide independent temperature calibration.

A two-cycle 35 °C/30 °C optical session captured all 30 planned frames within its designated holds,
but its host observation deadline cancelled the final hold with eight seconds remaining. A fresh
approved session with a 900-second budget completed normally with 30 frames and a matching
198-second completed history. No Stop or idle-target write was sent; the sample remained at 30 °C.
Broader programs, startup cancellation and ambiguous-failure recovery require further validation. See the [collection scheduler](synchronized-acquisition.md). Mocked fault tests do not establish hardware recovery behavior.
