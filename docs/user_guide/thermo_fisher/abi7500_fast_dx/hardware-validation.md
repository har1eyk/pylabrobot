# ABI 7500 Fast Dx hardware validation

## Direct Ubuntu connection and status

Session `abi7500-plr-observe-001` exercised the PLR USB driver on one ABI 7500 Fast Dx on
2026-10-09, from 17:50:35.999 to 17:50:42.196 UTC. The USB cable was connected directly to Ubuntu
24.04, with the Windows control computer disconnected. No vendor DLL or Windows process was used.

| Component | Observed value |
|---|---|
| Python | 3.13.5 |
| Thermal firmware | `G2.10` |
| Camera identification reply | `Hamamatsu,C9254-03,2.20,1.05,2.19,007006` |
| USB product descriptor | `Hamamatsu C9254-01` |
| USB VID:PID | `0c2b:0300` |
| Endpoints | Bulk OUT `0x06`, bulk IN `0x85` |
| Wire image geometry | 321 × 245 |
| Source base | `0356137641d37e82be38306b1c8ecb3d348482e2` plus local driver changes |

The camera reply and USB product descriptor identify different interfaces and are preserved
separately. The complete source used for the test is archived alongside its I/O transcript;
`audit.json` records source-file hashes. Later warning/docstring changes do not alter the tested
command sequence.

| Operation | Expected result | Observed result |
|---|---|---|
| `setup()` | Ready camera, application-mode thermal controller, valid identity | Succeeded |
| `get_identity()` | Camera description and thermal version | Values above, with full replies retained |
| Five `get_status()` calls | Complete, matched 50-byte status payloads | All succeeded; sequence inactive |
| Keepalives | `C:` writes serialized with queries | Ten writes recorded |
| `stop()` | Close USB and terminate keepalives | Returned; process exited with code 0 |
| Thermal-run start/cancellation | Not tested | Outside this connection/status session |
| Live image acquisition and well fluorescence | Not tested | Outside this connection/status session |

The status replies reported sample temperature 25.0 °C, block temperature 24.9–25.0 °C, ambient
temperature 24.5 °C, and cover temperature 105.3–104.8 °C. These are controller readings, not
independent thermometer measurements. The session sent no program upload, heating, lamp, filter,
plate-motion, or image-acquisition command. An inactive sequence did not imply a cool cover.

Elapsed time advanced from 11557 to 11562 seconds while the sequence remained inactive. Remaining
hold time stayed at 14 seconds. These counters alone cannot establish run activity or completion.

## Evidence and limits

The saved transcript contains 364 PLR transport records: 193 writes and 171 reads. All eight thermal
requests have matched replies with valid lengths, checksums, command/address/sequence fields and
zero firmware error fields. The only thermal commands were application-mode query `0x2f`, version
query `0x0a`, and extended status `0x29`. Their replies required 167 counted serial reads, including
zero-count reads and one-byte fragments with stale, nonzero padding.

Small exact boot/status exchanges and the five status payloads are retained in
`pylabrobot/thermo_fisher/abi7500_fast_dx/test_data/ubuntu_observation_vectors.json`. Offline tests
replay the USB transactions and check the inactive-state interpretation. Full artifacts remain
outside Git under `abi7500-research/captures/abi7500-plr-observe-001/`:

| Artifact | SHA-256 |
|---|---|
| `plr-io.json` | `bdc34452ac57fa272ba1edb1bce74a8f14da4964410c3d8163e1a7f4a99003c7` |
| `result.json` | `e27118bdc933a21c55047aecd77785f46bfa9645e5eea77a55f81d90497c66a0` |
| `observe_plr_connection.py` | `23e865376d629b42c5056db532148b046c1a06d276803fb77340909c4a1972e6` |

This is a PLR transport transcript, not a USBPcap or usbmon capture. Its byte reconstruction was
checked against expected lengths, packet checksums, and separately saved result payloads. The
session log file is empty; lifecycle completion was reported by the process console and exit code.

This test establishes connection and status communication on this instrument and firmware only.
It does not validate thermal control, cancellation, recovery after ambiguous failures, duplicate-run
prevention, other firmware, live acquisition, physical well orientation, calibration, or assay
performance. The device remains marked as incomplete and needing hardware testing.

## Direct Ubuntu thermal start and cancellation

Session `abi7500-plr-cancel-001` exercised one approved thermal-only run on the same instrument on
2026-10-09, from 19:10:47.720 to 19:15:37.985 UTC. The uploaded program contained one 35 °C,
60-second hold using the captured Standard/20 µL/block-2/tube-0 configuration. Windows remained
physically disconnected. No optical acquisition, firmware change or recalibration was performed.
The source base was `fa267dd35b453ada6f2a3c96d93cae62f3193717` plus local run-control changes;
the executed sources and their hashes are archived with the session.

| Operation | Observed result |
|---|---|
| Prepare `0x1b` and upload `0x12` | Each sent once and acknowledged |
| Plate query/load `0x73` / `0x71` | Unloaded 0, moving 1, loaded 2; one load, about 5.7 seconds to confirmed loading |
| Start `0x13` | Sent once; acknowledged at 19:10:57.103 UTC |
| Startup | State 3, inactive sequence phase, old run timestamp/history; sample 25 °C while cover warmed |
| Reconnection | Closed/reopened PLR USB during startup; same journal and run ID, no repeated upload or Start |
| Active progress | Fresh start record and ramping phase observed at 19:15:29.657 UTC, about 273 seconds after Start acknowledgement |
| Stop `0x07` | Sent once at 19:15:30.265 UTC; acknowledged, followed by confirmed inactivity |
| Idle target `0x22` | `00fa` (25 °C) sent once after inactivity and acknowledged |
| History | Method `plr-cancel-001`, user `PLR`, matching fresh start record, aborted outcome, duration 2 seconds |
| Final checkpoint | `cancelled`, cancellation requested, idle target acknowledged; retained in the persistent journal |
| Final status and close | Inactive, sample 25.2 °C, block 24.5 °C, cover 103.6 °C; USB closed cleanly |

The first research runner imposed a 25-second observation window after Start, which was too short
for cover warmup. It recorded an observation error, retained the acknowledged `starting` checkpoint,
and closed USB at 19:11:23.254 UTC. A continuation opened USB at 19:11:47 UTC and observed/cancelled
**the same run**, without a new start call or run ID. Both original result files are retained; the
initial file reports incomplete validation, while the continuation records the successful outcome.
This was a clean process/connection replacement, not recovery from a lost acknowledgement or
broken USB transfer. The driver needed no lifecycle changes to reconcile it.

The native run start record changed from `1a0a09000910010000` to `1a0a09010135220000`.
The new history labels and start record were visible during active ramping, with outcome code 253.
That observation disproves interpreting 253 as proof that no run started: the API reports
`no_result` until a final history outcome exists. During warmup, the old aborted history remained
visible and did not prematurely resolve the new run.

Cancellation occurred before the sample reached 35 °C. Across the captured queries, sample readings
were 25.0–25.4 °C, block readings 24.5–26.4 °C and cover readings 59.2–103.6 °C. These are controller
readings. This session does not verify setpoint attainment, the 60-second hold, temperature accuracy,
normal completion, startup cancellation, or recovery from communication faults. The plate was not
ejected, and cancellation did not cool the cover.

### Thermal-session evidence

The two transcripts contain 22,068 completed USB writes and 20,861 reads. All 701 thermal exchanges
have matched command/address/sequence fields, valid lengths/checksums and zero firmware errors.
There are exactly one each of prepare, upload, load, Start, Stop and idle-target commands. No optical
acquisition command was sent. A research recorder saved bytes directly as hex at the PLR USB
boundary; these are transport transcripts, not USBPcap or usbmon captures.

Full logs, hex transcripts, source snapshots, the initial and final closed-journal copies, and the
independent byte audit remain outside Git in
`abi7500-research/captures/abi7500-plr-cancel-001/`. An archive is saved in Seafile
`Temp/ABI-7500/abi7500-plr-cancel-001.zip`. The canonical live journal remains on local disk;
archived copies must not be restored to retry an experiment.

| Artifact | SHA-256 |
|---|---|
| `usb-hex.jsonl` | `06b755d6fa69bfc469990a31b692e4e5fe045386932b4531d4e7317382e13afb` |
| `continuation/usb-hex.jsonl` | `3f96fda0ff8decdd358bb4fc4aee769ed28b2d6b58fbe52a6ada9b79c326fe88` |
| `result.json` | `0297fa50028f12e987a3a9fa451525d7428201daea47e51ba9e3eb5b3914fbe2` |
| `continuation/result.json` | `5d2e69c3b90c1d23488cb087e1bb9d5db2332b10c27849871cc0950e6baacd8f` |

The small `test_data/ubuntu_run_vectors.json` fixture retains exact selected exchanges and
checkpoints. Regression tests replay their fragmented responses, preserve `starting` across a
journal reopen, reconcile the real custom labels, and distinguish active code 253 from a final
outcome. The device retains its incomplete status and hardware-testing flag for the remaining scope.

## Direct Ubuntu 35 °C, 60-second completion

Session `abi7500-plr-hold-001` executed the separately approved complete hold on 2026-10-09, from
20:40:00.118 to 20:41:32.583 UTC. It used the same G2.10 instrument, Standard/20 µL/block-2/tube-0
configuration and sealed plate, with Windows disconnected and no optics. The journal retained the
previous cancelled run and reserved the distinct ID `plr-hold-001`. The executed sources and their
hashes are preserved with the capture; no run-lifecycle code change was needed for this test.

| Operation | Observed result |
|---|---|
| Initial state | Inactive; sample/block 25.0 °C, cover 105.2 °C; prior cancelled history matched the journal |
| Plate state | Already loaded (2); no loading or ejection command sent |
| Program | One repetition of one 35 °C, 60-second hold; verified in the uploaded bytes |
| Start | One `0x13`, acknowledged at 20:40:03.804 UTC |
| Ramp | Fresh active status appeared at 20:40:04.798 UTC; the first bracketing observation crossed the startup boundary and remained `uncertain` |
| Hold entry | Holding phase at elapsed 13 seconds, 60 seconds remaining; sample estimate 34.6 °C |
| Hold progress | Remaining time counted down from 60 to 1; holding samples spanned 59.038 host seconds, ending at elapsed 72 |
| Completion | Transient inactive/state 6, then inactive/state 2 with matching completed history; `completed` checkpoint at 20:41:19.746 UTC |
| History | Method `plr-hold-001`, user `PLR`, outcome 0 (normal completion), duration 73 seconds including ramp |
| Final temperatures | Sample/block 35.0 °C, cover 104.7 °C; still inactive, USB closed cleanly |

The holding-phase sample estimates ranged from 34.6 to 35.2 °C, including initial settling, then
settled near 35.0 °C. Block readings ranged from 24.9 to 36.4 °C across ramp and hold, and cover
readings ranged from 104.6 to 105.4 °C. These controller readings and the 60-second programmed
countdown validate this thermal sequence's control and reporting, not independent temperature
accuracy or uniformity across wells. Polling does not resolve the exact subsecond hold boundaries.

The fresh run record was `1a0a09010312090000`, distinct from the previous run. History retained the
correct NUL-terminated method name even though bytes after the terminator contained part of the
longer preceding label. During completion, state 6 was treated as transitional/uncertain; no
unverified idle-state mapping was added. A later coherent status/history observation established
normal completion.

There was one prepare, one upload and one Start, with **no Stop or idle-target command**. Normal
completion retained the last 35 °C target during the subsequent observation period; it did not
request 25 °C or cool the cover. The completed checkpoint has `cancel_requested=False` and
`idle_temperature_set=False`. This differs from the explicit cancellation sequence's idle request.

### Completion-session evidence

All 213 thermal exchanges have valid lengths/checksums, matched command/address/sequence fields
and zero firmware error fields. The exact-hex transport transcript contains 6,877 completed USB
writes and 6,486 reads. The full evidence remains outside Git in
`abi7500-research/captures/abi7500-plr-hold-001/`, including `temperature-trajectory.json`, PNG/SVG
plots, logs, source snapshots, closed journals before/after, and `audit.json`. Its archive is saved
in Seafile `Temp/ABI-7500/abi7500-plr-hold-001.zip`.

| Artifact | SHA-256 |
|---|---|
| `usb-hex.jsonl` | `9992a2ee6533a4dd76cd579f41d05981104c9d5ecf9d5b6172c04b7478810e07` |
| `result.json` | `2bccdb76d1cfe5605827d1bf9cb4d9ea5adc91731feff0d6c2dd6899af04dcff` |

`test_data/ubuntu_hold_vectors.json` keeps selected original exchanges and observations for offline
regression tests. They cover startup/completion transitions, hold progress, matching completed
history and a durable result that cannot trigger another Start after reopening the journal.
Multi-step/repeated-cycle timing, startup cancellation, ambiguous-failure recovery, live optical
acquisition and calibrated well measurements remain outside the tested scope.

## Single raw snapshot from Ubuntu

Session `abi7500-plr-snapshot-001` ran on 2026-10-09 from 21:05:04.145 to 21:05:06.386 UTC,
with explicit approval for one 0.256-second snapshot. It used the same C9254-03/2.20 camera and
G2.10 controller directly through Ubuntu PLR USB, with Windows disconnected. The existing sealed
plate and lamp/filter settings were retained. No thermal run, filter/plate movement, lamp command,
recalibration or firmware change was performed.

| Observation | Result |
|---|---|
| Acquisition ID | `plr-snapshot-001`, durably reserved before exposure |
| Exposure setting | One `CJ   2,  256`; reply `00 01` confirms the 256 ms setting |
| Capture | Exactly one CC, at 21:05:05.323 UTC |
| Readiness | 27 `not r` replies followed by one `ready` |
| Image read | One `CD  64,2458 `; 157,312 bytes in 38 × 4,096-byte reads plus 1,664 bytes |
| Timing | 0.307 seconds from CC to CD; image transfer took 0.357 seconds |
| Native crop | All 76,800 pixels matched isolated ArpLib image-copy execution exactly |
| Optical observations | Filter `00 00`, stationary motor `00 00`, opaque lamp reply `00 00 00 02`, unchanged |
| Thermal observations | Inactive before/after; sample/block 25.0 °C; cover 105.1→105.3 °C |
| Repeated API call | Same ID/exposure returned the saved result with zero additional USB I/O |
| Teardown | USB closed cleanly; no further instrument commands |

The raw crop spans 115–419 counts (median 129); no pixel is zero or at the uint16 maximum. These
statistics do not establish fluorescence, signal quality or optical calibration. Filter index 0
is not assigned a dye. The timing above includes polling and transport overhead and does not
independently measure sensor integration time. The cover remained hot when USB was closed.

All 13 thermal exchanges matched command/address/sequence and passed length/checksum checks with
zero firmware errors. The transcript contains 304 completed writes and 323 reads. Thermal commands
were limited to application-mode, identity, status and optical-state queries. The journal's existing
thermal run records were unchanged. No snapshot driver correction was required for the hardware run.

Full evidence remains outside Git in `abi7500-research/captures/abi7500-plr-snapshot-001/`: exact USB
traffic, source snapshots, journals, acquisition metadata, original/cropped pixels, native crop
validation, pixel statistics and a labeled preview. The archive is stored in Seafile
`Temp/ABI-7500/abi7500-plr-snapshot-001.zip`.

| Artifact | SHA-256 |
|---|---|
| `usb-hex.jsonl` | `5400f94b7bba755a714d9619048633d22c087c5e60ae3961ae593b4c820b8bd7` |
| `camera-original.bin` | `5b4cf75ab76b92fd7cff07a8c2e95d8ac2b925495d6496b775aee6fdf904e1ba` |
| `camera-image-u16le.bin` | `a7382d3665c965c2f28fe5042cacd8d4c2db9cc96f5c8e53849dd1b5b225dfa4` |

`test_data/ubuntu_snapshot_vector.json` preserves a compressed transcript of the complete snapshot
method and its original frame for offline replay. It checks actual serial fragmentation, readiness,
native optical observations, raw image fidelity and the independently emulated crop hash. Other
exposure settings, lamp/filter control, optical fault recovery, physical well mapping, synchronized
acquisition and calibrated per-well measurements remain unverified.

## Lamp/filter control and one filter-1 snapshot

Session `abi7500-plr-optics-001` ran on 2026-10-09 from 23:59:11.049 to 23:59:22.226 UTC with
explicit approval for the prepared optical session. The same instrument was controlled directly
from Ubuntu through PLR USB. The executed sources, approval, persistent-journal snapshots and
full hex transcript are archived outside Git with the capture.

| Operation | Observed result |
|---|---|
| Initial readback | Inactive, filter 0, stationary motor, lamp `0000 0002` |
| Filter selection | One absolute move `0x69/0001`, confirmed stationary at filter 1 |
| Lamp selection | One enable `0x64/0001`, then one power `0x65/0000`; readback `0001 0000` |
| Repeated setters | Queries only, no duplicate mutations |
| Acquisition | `plr-optics-001-f1`; one CJ 256, one CC and one CD |
| Readiness/transfer | 27 `not r`, then `ready`; all 157,312 bytes received |
| Native image copy | All 76,800 cropped pixels matched isolated ArpLib execution |
| Repeated acquisition ID | Cached result, zero additional USB traffic |
| Restoration | One lamp disable `0x64/0000`, one filter move `0x69/0000`; original state confirmed |
| Final status | Inactive, sample/block 25.0 °C, cover 104.6 °C; USB closed |

No thermal start/upload, temperature setpoint, plate motion, homing, recalibration or firmware
command was sent. No cleanup reconnect was needed. All 101 thermal exchanges had valid framing,
checksums, matched command/sequence/address fields and zero firmware errors. The transcript
contains 1,749 completed writes and 1,673 reads. CC-to-CD took 0.308 seconds and image transfer
took 0.356 seconds. This is a PLR transport transcript, not an independent USB analyzer capture.

The raw image ranges from 116 to 411 counts, with median 129 and mean 129.411. This is close to
the earlier lamp-off snapshot (mean 129.572); no usable fluorescence was established. The session
validates desired-state commands, readback, duplicate prevention and image transfer, **not optical
output or stabilization**. Lamp faults, other filters/power settings and recovery remain untested.
The cover remained hot. The runner's successful flag refers to its control/transfer checks.

Full evidence, original and separately corrected images, diagnostics, byte audit and source
snapshots are in `abi7500-research/captures/abi7500-plr-optics-001/`. The diagnostic well output uses
the backed-up F0 mask with an explicit filter-1 association; it is not calibrated fluorescence.
The `ubuntu_optics_vector.json` regression fixture replays the complete optical sequence and
frame with original serial fragmentation. Only between-method `C:` keepalives are omitted.
The evidence archive is saved in Seafile `Temp/ABI-7500/abi7500-plr-optics-001.zip`; a downloaded
server copy matched the local SHA-256 `160689e2d2d361f242a4e9d306150d6dae2a07586af0e8b5952a344a3ee23580`.

| Artifact | SHA-256 |
|---|---|
| `usb-hex.jsonl` | `3e9db8f657391d134e0c795b03b8b1fdfbd34d9ff63fad2e6421bdcf2da8d7b7` |
| `camera-original.bin` | `ce754a30fc4c5263f63b4515c528be62621dd8bb9fda6a0549237ff02d8a1ea5` |
| `camera-image-u16le.bin` | `e42c276a691da5f05bcd0764b6b60f023a5e367f3af7c33fb0f585c0cff87560` |

## Ubuntu illumination session

Session `abi7500-plr-illumination-001` ran on 2026-10-10 from **00:21:32.679 to 00:22:09.076 UTC**
(2026-10-09 locally), with explicit approval to resolve illumination. PLR controlled the same
C9254-03/2.20 and G2.10 instrument through USB on Ubuntu, using the existing persistent journal.
The driver applied the native 250 ms enable-to-power and post-mutation delays. The runner also
waited five seconds before each filter-1 lamp-on exposure and one second after lamp-off before
the final dark frame.

| Frame ID suffix | Physical filter | Lamp / power | Exposure | Crop mean | Crop maximum | Pixels at 4095 |
|---|---|---|---|---|---|---|
| `off-f1` | 1 | off | 256 ms | 129.470 | 412 | 0 |
| `p0-f1` | 1 | on / 0 | 256 ms | 154.658 | 433 | 0 |
| `p1-f1` | 1 | on / 1 | 256 ms | 160.091 | 440 | 0 |
| `p1-f5` | 5 | on / 1 | 90 ms | 1436.659 | 4095 | 19487 |
| `off-f5` | 5 | off | 90 ms | 129.282 | 228 | 0 |

All IDs start `plr-illumination-001-`. Lamp-on frames visibly resolve the 96-well pattern; lamp-off
frames return to the dark background. This resolves the missing optical signal for the tested
sequence. It does not isolate the contribution of the native delays versus the extra settling
interval or establish calibrated lamp intensity. Filter 5 is saturated at this exposure.

The byte audit confirms exactly five CJ/CC/CD acquisitions and these seven mutations in order:
`69/0001`, `64/0001`, `65/0000`, `65/0001`, `69/0005`, `64/0000`, `69/0000`.
Final readback is lamp `0000 0002`, stationary motor `0000`, filter `0000`. No reconnect was needed.
Repeating the last acquisition ID returned its cached result with **zero additional USB I/O**.
The journal retained prior thermal runs and records all five acquisitions complete.

No thermal upload/start/setpoint, plate motion, homing, firmware, recalibration or counter-reset
command was sent. Sample/block readings were 24.9–25.0 °C; the already heated cover remained
104.6–105.3 °C. USB closure does not cool the cover. The transcript contains 3,186 completed writes
and 3,151 reads; it records PLR transport calls rather than an independent analyzer capture.

| Original camera frame | SHA-256 |
|---|---|
| `off-f1` | `41601fa6da6f689dfd3314763f9f681e062e472632f0d8f24d00d55e2b12c3a5` |
| `p0-f1` | `f62b8c5479e9183305f220e5b8126fbee9c5b1e24e0aed5312c18375264b47f5` |
| `p1-f1` | `8a88253f2db482fc0d6388548a8c8ae881c20d273442059cf94eb85a3a17eb40` |
| `p1-f5` | `fd12595f6d96c33334f37cee58ed70083aeaff9b365625dc866d1c3ba1bfec35` |
| `off-f5` | `f77804d88fb2ae92c14ba039d5104b4dc40cf864d9475295a858ccbd06e96213` |

Full frames, metadata, the comparison image, audited USB transcript and executed-source/journal
snapshots are preserved outside Git in `abi7500-research/captures/abi7500-plr-illumination-001/`.
The USB transcript SHA-256 is
`d7fe085924a3a678b16352c9c64ebacb12fac4d0950a7fb8f9204f595051bbb3`.
Before execution, the runner passed seven offline mock scenarios: success, unexpected initial
optics, and failure at each of the five acquisitions. Cleanup never repeats an exposure.

The evidence archive is saved in Seafile `Temp/ABI-7500/abi7500-plr-illumination-001.zip`.
A downloaded server copy matched SHA-256
`f6690b92f7a7aa8208fbc87331ae3a94133149dcb6120d1b1ef5b42bcd4373d9`.
It includes the offline comparison report and source snapshots in addition to the session evidence.

## Camera processing compared with the same vendor acquisition

This separate offline comparison uses the original completed vendor run, not the five illumination
frames above. It reconstructs all 30 collection frames into 96 wells × five saved filters × two
cycles, with explicit frame/exposure associations, native masks, matching `Background.csv` and the
EDS's uniformity snapshot. **All 960 original integers match exactly.** All **960 corrected binary32
values** also match the separately vendor-validated Java uniformity operation.

The verified chain includes the OPB=0 crop, 180° rotation into mask coordinates, nonzero-pixel
sum, adjacent-ring correction, native exposure normalization, background offsets/slopes,
saturation-dependent exposure choice, reversed physical/saved filter order and uniformity.
See [protocol evidence](protocol.md) for native routine addresses and calibration hashes and
[the processing API](optics-and-measurements.md) for original/intermediate data preservation.

The version-5 native background CSV retains enough coefficient precision for exact reconstruction;
the EDS INI alternative misses ten integers. No coefficients were fitted to vendor results.
The full comparison report is `abi7500-research/captures/abi7500-acquire-001/camera-to-vendor-validation.json`.
Ten small recorded image patches and an 80-case native exposure-selection oracle provide portable
regression fixtures. Saturated fallback results are explicitly flagged. This verifies the recorded
configuration and its saved well ordering, not independent physical A1 orientation, current
calibration validity, dye unmixing, Ct, or assay accuracy. A successful synchronized PLR run remains unverified.

## First synchronized-acquisition attempt

Session `abi7500-plr-sync-001` ran on 2026-10-10 from **01:03:47.269 to 01:04:05.416 UTC**
(2026-10-09 locally). It attempted two repetitions of 35 °C/15 s then 30 °C/60 s, with collection
planned during each 30 °C hold. Lamp power 1 was enabled with five seconds of settling.
**This session did not validate synchronized acquisition: no camera exposure occurred.**

The first status/history/status observation straddled thermal startup: the first status still
belonged to the preceding run, whereas history and the second status identified the new run.
The scheduler incorrectly treated that temporary `uncertain` result as a permanent optical error.
Separately, the validation script tried to display a nonexistent serialized `last_status` field
instead of `status_hex`, masking the collector error. The script's cleanup reconciled fresh owned
active status, cancelled the same run, confirmed tagged aborted history, and requested 25 °C idle.
It then disabled the lamp; the filter was already at 0.

The byte audit establishes one prepare/upload/Start, one Stop and one idle-target write; lamp
mutations were `64/0001`, `65/0001`, `64/0000`. There were **zero CJ, CC or CD commands**, no filter
motion, no second Start, no reconnect, and no firmware/calibration writes. The persistent record
is `cancelled`, with the optical failure retained. Final sample/block/cover readings were
25.8/24.6/104.9 °C, phase inactive, lamp `0000 0002`, stationary filter `0000`. The cover remains hot.

The scheduler now waits for coherent ownership evidence after a transient uncertain observation,
without acquiring in an uncertain state. The runner now displays the actual serialized field,
reports a completed collector's exception first, and always retrieves its task result. The new
`ubuntu_sync_startup_vector.json` retains the original startup and cleanup observations. Offline
regression tests replay that transition, complete two synthetic scheduled collections, exercise
fragmented images and interrupted/cross-boundary exposures, and verify duplicate prevention.
Runner mocks now use actual `ThermalRun.to_dict()` records and exercise progress reporting.
**The subsequent session below exercised these fixes and acquired all 30 planned images.**

Original executed source, before/after journals, all USB transfers, result, and audit are retained
outside Git under `abi7500-research/captures/abi7500-plr-sync-001/`. Transcript SHA-256:
`ec159a986f464f81490b61697b3061b8e1614fc08991d5ae4c9ab1fee3fcad49`.
The separately approved session below used a new identity for the same two-cycle test; the
cancelled identity was not restarted.

## Thirty synchronized images; final hold interrupted by host timeout

Session `abi7500-plr-sync-002` ran on **2026-10-10, 11:02:37.882–11:10:28.791 UTC**, with explicit
approval for two 35 °C/30 °C cycles and 30 images. Ubuntu PLR used the existing journal and native
USB transport. The instrument performed two repetitions of 35 °C/15 s then 30 °C/60 s. Both
scheduled collections completed, each using five physical filters and three exposures per filter.
**Optical collection succeeded, but the full thermal program did not complete normally.**

| Collection | Original frames | Hold remaining before/after collection | First-frame to last-frame interval | Reported sample at frame boundaries |
|---|---|---|---|---|
| Cycle 1, group 0/step 1 | 15 | 44 → 14 s | 28.201 s | 30.0 °C throughout |
| Cycle 2, group 0/step 1 | 15 | 44 → 13 s | 27.595 s | 30.0 °C throughout |

Each original camera transfer contains 157,312 bytes. Every frame's pre-exposure and post-transfer
status identifies the same run token, group, step, repetition and holding phase; lamp/filter
readbacks agree before and after. All 30 exposure records and both collection records are
`complete`. No image was retried, and no collection crossed a thermal boundary. These are
controller-reported temperatures and hold associations, not independent temperature calibration
or hardware-trigger/subsecond synchronization measurements.

The cover began around 60 °C. Start acknowledgement preceded the first active thermal observation
by **263.245 seconds**. The runner's **450-second total observation budget included this warmup**.
It expired after both collections, before the final hold ended. Cleanup reconciled the owned run
and sent Stop with **eight reported seconds remaining** in that hold, then requested the captured
25 °C idle target. The final run checkpoint is `cancelled`, its history is tagged `plr-sync-002` /
`PLR` and `aborted`, and the timeout is retained independently of the successful optical records.
This was a host observation timeout; the byte audit found no USB transfer errors or firmware errors.

The audit confirms one prepare/upload/Start, exactly **30 CJ / 30 CC / 30 CD** commands, one Stop
and one idle-target command. Optical mutations enabled lamp power 1, moved through filters 1–5
twice, disabled the lamp, and restored filter 0. There was no homing, plate-loading movement,
reconnect, firmware/calibration write or second Start. At disconnect, status was inactive with
sample/block/cover at **29.2/27.5/105.3 °C**, lamp `0000 0002`, motor `0000`, filter `0000`.
The 25 °C setpoint had been acknowledged; the sample had not yet reached it, and the cover was hot.

Offline processing produced **960 well/filter/cycle values** from these new frames using the
explicitly associated reference-PC mask, native background and uniformity backups. Original
frames, extraction statistics, all exposure candidates, selected indices and calibration hashes
are preserved. **630 of 960 selected values are flagged saturated**; none of the filter-1 values
selected a saturated exposure. These flagged values are not quantitative assay results. The
previous exact vendor comparison is separate evidence; there is no matching vendor output for
this new PLR acquisition, and current calibration validity has not been established.

The subsequent approved session below uses a **900-second** observation budget while retaining the same
thermal program and exposure plan. Timeout messages now distinguish the overall fluorescence
observation deadline (including startup) from a USB command timeout. Regression tests verify
that an outer timeout after complete collections preserves all original frames, prevents another
read, and does not relabel thermal cancellation as success. Original frame-boundary metadata is
retained in `ubuntu_sync_vectors.json`. The separately approved repeat is reported below.

Evidence is outside Git in `abi7500-research/captures/abi7500-plr-sync-002/`, including
`well-measurements.json`, the temperature trajectory and image plots, byte audit, original frames,
and executed-source and before/after journal snapshots. USB transcript SHA-256:
`8f15785313a96cb9a43ab9f10cab5ae214282057b07b84775930cf1eb7c336ab`.

## Normally completed two-cycle optical run

Session `abi7500-plr-sync-003` ran on **2026-10-10, 11:31:45.298–11:35:21.475 UTC**, with fresh
approval for the same two-cycle, 30-image program and a **900-second observation budget**. Ubuntu
PLR controlled the instrument directly through USB with Windows disconnected, retaining the
existing journal and using a new run identity. Both repetitions of 35 °C/15 s then 30 °C/60 s
completed normally. Matching history identifies `plr-sync-003` / `PLR`, outcome `completed`,
and a 198-second thermal duration. The run and optical records have no errors.

| Collection | Original frames | Hold remaining before/after collection | First-frame to last-frame interval | Reported sample at frame boundaries |
|---|---|---|---|---|
| Cycle 1, group 0/step 1 | 15 | 44 → 14 s | 28.149 s | 30.0 °C throughout |
| Cycle 2, group 0/step 1 | 15 | 45 → 13 s | 28.124 s | 30.0 °C throughout |

All 30 original frames contain 157,312 bytes and reconstruct the captured 321 × 245 wire geometry
and 320 × 240 image crop. Pre-exposure and post-transfer status agree on the run token, holding
phase, group, step and repetition. Lamp/filter readbacks also agree. Original transfers, cropped
images and metadata are retained. Repeating `collect_fluorescence()` after completion returned
identical saved results with **zero USB operations**.

The complete byte audit found one prepare/upload/Start, exactly **30 CJ / 30 CC / 30 CD** commands,
and **no Stop or idle-target write**. Lamp power was 1; filters traversed 1–5 twice, followed by
lamp disable and restoration of filter 0. All thermal checksums, sequence IDs, reply error codes
and serial-read counts validated. No USB errors, exposure retries, homing, plate-loading movement,
reconnect, firmware/calibration writes or second Start occurred.

The cover was already **104.7 °C** at connection, and the first active thermal observation followed
Start acknowledgement by 1.057 seconds. This validates normal completion using the longer host
budget with a warm cover; it does not establish the required time for cold-cover startup or other
programs. Collection intervals are controller-observed hold associations, not independent
temperature calibration or hardware-trigger/subsecond timing measurements.

Cleanup verified inactive state 2, stationary filter `0000`, and lamp `0000 0002`. Final
sample/block/cover readings were **30.0/29.9/105.0 °C**. Normal completion retained the final 30 °C
target; no cooling command was sent and the cover remained hot.

Offline processing produced **960 well/filter/cycle values**, with extraction statistics, exposure
candidates, selections and explicitly associated calibration hashes. **578 selected values are
flagged saturated**; all 192 physical-filter-1 values selected unsaturated exposures. The saturated
values are not quantitative assay results. The reference-PC backup masks, background coefficients
and uniformity factors were applied without changing calibration. Their current validity remains
unverified. The earlier exact comparison against vendor outputs is separate evidence; this new
PLR acquisition has no same-acquisition vendor output comparison.

`ubuntu_sync_completed_vectors.json` retains small original frame-boundary and completion
observations. Offline tests replay the final status/history/status observations, verify the saved
frame associations, and ensure repeated calls cannot restart or cancel the completed run. The
experimental warning remains for broader programs, communication-fault recovery, physical well
labeling, unsaturated measurement comparisons and current calibration validity.

Full evidence is outside Git in `abi7500-research/captures/abi7500-plr-sync-003/`: executed source,
before/after journals, USB transcript, audit, original frames, calibration inputs, per-well results
and temperature/image plots. USB transcript SHA-256:
`279a48cd157e78f57027ec847ef2e0ceef7f42e8bf96b7703ddfd2ba3e1912c5`.

## Shorter exposures remove selected saturation flags

Session `abi7500-plr-lowexp-001` ran on **2026-10-10, 11:50:28.358–11:54:04.364 UTC**, under fresh
approval for one shorter-exposure validation. Ubuntu PLR used the same sealed plate, persistent
journal, two-cycle 35 °C/15 s then 30 °C/60 s program, lamp power 1 and 900-second observation
budget. Filter 1 retained 540/180/60 ms; filters 2–5 used **30/10/5 ms**. Both cycles completed
normally, with matching tagged history reporting a 196-second thermal duration.

| Collection | Frames | Hold remaining before/after collection | First-frame to last-frame interval | Reported sample at every frame boundary |
|---|---|---|---|---|
| Cycle 1, group 0/step 1 | 15 | 45 → 18 s | 24.288 s | 30.0 °C |
| Cycle 2, group 0/step 1 | 15 | 44 → 16 s | 24.584 s | 30.0 °C |

Every original image contains 157,312 bytes; all run/hold/filter associations and exposure echoes
validated. Repeated retrieval after completion returned identical saved collections with no USB
I/O. The complete transcript audit confirms one prepare/upload/Start, **30 CJ / 30 CC / 30 CD**,
zero Stop/idle-target commands and no USB or firmware errors. There was no exposure retry, second
Start, homing, plate-loading motion, reconnect, firmware write or calibration change.

All **960 selected well/filter/cycle values were unflagged**, compared with 578 flagged values in
`plr-sync-003`. This uses the recovered vendor rule: an exposure is saturated when more than six
selected well pixels exceed 4000 counts. It is not a claim that every pixel in every frame is
unclipped. The selection algorithm retained the longer exposures where usable and chose shorter
ones where necessary:

| Physical filter | Saturated selections before / 192 | Saturated selections after / 192 | Selected exposure counts across both cycles |
|---|---|---|---|
| 1 | 0 | 0 | 192 at 540 ms |
| 2 | 170 | 0 | 138 at 30 ms; 54 at 10 ms |
| 3 | 117 | 0 | 192 at 30 ms |
| 4 | 111 | 0 | 192 at 30 ms |
| 5 | 180 | 0 | 16 at 30 ms; 168 at 10 ms; 8 at 5 ms |

At filter 2, 54 well/cycle extractions were flagged at 30 ms. At filter 5, 176 were flagged at
30 ms and eight at 10 ms. All 5 ms extractions were unflagged. Original frames and every exposure
candidate remain preserved, including the saturated candidates that were not selected.

**Quantitative stability remains unresolved.** Comparing the same fixed, unsaturated 5 ms
exposures between cycles gives median cycle-2/cycle-1 ratios of 0.992, 0.987, **0.898** and
**0.923** for physical filters 2, 3, 4 and 5 respectively (96 wells each). The roughly 10% and 8%
declines at filters 4 and 5 also appear at other unsaturated exposures. They therefore are not
explained solely by switching the selected exposure. This session does not identify their cause.
The unchanged filter-1 settings gave a median current/previous-run ratio of 0.982 over 192
well/cycle comparisons.

Among unsaturated, positive candidates, exposure-normalized 5 ms signals were typically about
2.0–2.3% below corresponding 10 ms signals at filters 2–5. These comparisons use pre-uniformity
vendor-compatible integers, exclude saturated or nonpositive candidates and preserve the sample
counts in `exposure-comparison.json`. They are descriptive checks, with no prespecified precision
acceptance threshold. They do not establish linearity, current calibration validity or assay accuracy.

The cover began at 104.6 °C; the first active observation followed Start acknowledgement by
1.530 seconds. Cleanup verified inactive state 2, lamp `0000 0002`, stationary filter `0000` and
sample/block/cover readings of **30.0/30.0/104.9 °C**. No cooling command was sent. Cold-cover
startup with this budget, broader programs and communication-fault recovery remain unverified.

The exact reference-PC masks, background coefficients and uniformity factors were reused and
identified by hashes. There is no same-acquisition vendor output for this PLR run. Independent
physical well orientation and quantitative consistency still need validation; no Ct, dye
unmixing, assay interpretation or recalibration was performed.

`ubuntu_short_exposure_vectors.json` preserves six original command/reply pairs and counts for
offline regression testing. Full evidence remains outside Git in
`abi7500-research/captures/abi7500-plr-lowexp-001/`, including executed source, before/after
journals, original images, calibration inputs, per-well results, the exposure comparison and
plots. USB transcript SHA-256:
`215febf6a20292f7bf11427fd1aee8a4947820a20b6dab7c2ababdfec65db844`.

(abi7500-offline-cycle-drift-audit)=
## Offline cycle-drift audit

Filter numbers in this audit are **physical wheel positions**, not vendor channel numbers.
Physical position 5 maps to vendor channel 1 (SYBR/FAM), and position 4 to vendor channel 2
(VIC/JOE). See the [channel mapping](optics-and-measurements.md). The plate was subsequently
reported by the user to contain SYBR Green; the decline at physical position 5 therefore
includes its principal detection channel, not the vendor's Cy5 channel.

`abi7500-drift-001` compared **120 original frames** from the vendor acquisition,
`plr-sync-002`, `plr-sync-003` and `plr-lowexp-001`, without hardware I/O. An independent NumPy
implementation copied the original wire words, applied the recovered crop/rotation, and summed
the saved masks and surrounding rings. All 90 PLR crops matched their saved image bytes;
**8,640 well/exposure extractions and background/exposure-corrected candidates matched** the
saved PLR records. Reconstructed exposure selection also matched all **960 original vendor EDS
integers**. The audit reused the hashed calibration inputs without changing measurements or
adding a drift correction.

The stricter comparison below uses the same exposure and well in both cycles, requires positive
signals, and excludes any well with even one selected pixel above 4000 in either frame. This is
stricter than the native saturation flag, which permits up to six such pixels. Values are median
cycle-2/cycle-1 ratios; the different stages use the same 96 wells in the short-exposure session.

| Physical filter / exposure | Uncorrected pixel sum | Ring-subtracted signal | Background/exposure-corrected candidate |
|---|---|---|---|
| 1 / 60 ms | 0.9987 | 0.9928 | 0.9924 |
| 2 / 5 ms | 0.9937 | 0.9917 | 0.9917 |
| 3 / 5 ms | 0.9934 | 0.9869 | 0.9869 |
| 4 / 5 ms | 0.9503 | 0.8977 | 0.8978 |
| 5 / 5 ms | 0.9352 | 0.9226 | 0.9228 |

The decrease is already present in uncorrected pixel sums. Subtracting surrounding pixels
changes the relative decrease, but subsequent calibration/exposure normalization changes the
filter-4/5 ratios only slightly. It persists at other unsaturated exposures, so neither exposure
selection nor calibration processing alone explains it. At filter 1, the longer 540 ms exposure
has a ring-subtracted median ratio of 0.9883 and less well-to-well scatter than 60 ms.

The **vendor-controlled acquisition also shows declines**: at its shortest exposures, the
ring-subtracted ratios are 0.9056 at filter 4/85 ms (**10 matched wells**) and 0.9361 at filter
5/30 ms (**3 matched wells**). Longer vendor exposures clipped most wells, limiting this
comparison. Its timing and acquisition settings differ; this is evidence that a similar decline
is not exclusive to PLR, not proof of an identical cause or a controlled software comparison.

Across the short-exposure session's 15 matched frame pairs, optical-black reference means changed
by less than 0.6 camera counts. Whole-image translation estimates were at most 0.02 pixel in
either axis; this descriptive estimator is not a physical-motion bound and does not exclude
other optical changes. Frame-boundary sample readings were 30.0 °C and recorded lamp/filter
states matched the requested settings. These observations do not independently measure sample
temperature, lamp output, filter transmission or fluorescence stability.

This is a two-collection comparison on one existing plate. The wells are not independent run
replicates, and no precision acceptance threshold was prespecified. Plate changes, illumination
changes and thermal history remain possible explanations. Current calibration validity and
physical well labeling remain unverified. No automatic normalization or runtime driver change
is justified by this audit.

The diagnostic comparison used two repetitions of 30 °C/40 s then 30 °C/60 s, collecting
15 frames in each second hold, about 100 seconds apart. It preserved the short-exposure settings,
lamp power and filter order while removing the intervening 35 °C excursions. The runner passed
nine offline mock scenarios before the separately approved hardware session described below.

Analysis code, input hashes, per-exposure statistics and plots remain outside Git in
`abi7500-research/analyses/abi7500-drift-001/`. The prepared session is
`abi7500-research/plans/abi7500-plr-iso30-001/`.

(abi7500-constant30-validation)=
## Constant-30 °C optical comparison

Session `abi7500-plr-iso30-001` ran on **2026-10-10, 12:14:36.154–12:18:27.760 UTC**, with fresh
approval for one constant-30 °C session. Ubuntu PLR used the existing sealed plate, reported by
the user to contain SYBR Green, and the persistent run journal. Every uploaded sample target
was 30 °C: two repetitions of 40 s then 60 s, with collection during each second hold. The
200 seconds of programmed holds completed normally; matching tagged history reported a
212-second thermal duration. The observation budget was 900 seconds, including startup.

Physical position 1 used 540/180/60 ms; positions 2–5 used 30/10/5 ms. Lamp power was 1, with
five seconds of settling and the same filter order as `plr-lowexp-001`.

| Collection | Original frames | Hold remaining before/after collection | First-frame to last-frame interval | Reported sample at frame boundaries |
|---|---|---|---|---|
| 1, group 0/step 1 | 15 | 44 → 18 s | 24.178 s | 30.0 °C throughout |
| 2, group 0/step 1 | 15 | 45 → 16 s | 25.114 s | 30.0 °C throughout |

First exposures were **103.098 seconds apart**, compared with 98.672 seconds in the earlier
short-exposure 35 °C/30 °C run. All 30 original frames contain 157,312 bytes; saved crops,
exposure echoes, run identities, hold associations and stationary optical readbacks agree.
The complete USB audit validated framing/checksums, sequence IDs and replies: one
prepare/upload/Start, exactly **30 CJ / 30 CC / 30 CD**, and no Stop or idle-target write.
There was no USB error, camera retry, homing, plate-loading movement, reconnect, firmware or
calibration write. Repeated collection retrieval returned the saved results with zero USB I/O.

All **960 selected well/filter/collection measurements were unflagged** under the native
saturation rule. Some unused longer exposures still clipped wells. The table below instead
requires **zero selected pixels above 4000** in all four matched observations: both earlier
cycles and both constant-temperature collections. Every row has 96 positive matched wells.
Ratios are medians of collection-2/collection-1 background/exposure-corrected candidates before
uniformity, at one fixed exposure. Dye names identify channel assignments, not unmixed dye signals.

| Vendor channel | Physical position | Exposure | Earlier 35 °C/30 °C ratio | Constant-30 °C ratio |
|---|---|---|---|---|
| 1 — SYBR/FAM | 5 | 5 ms | 0.9228 | **0.9394** |
| 2 — VIC/JOE | 4 | 5 ms | 0.8978 | **0.9255** |
| 3 — NED/TAMRA/Cy3 | 3 | 5 ms | 0.9869 | 0.9908 |
| 4 — ROX/Texas Red | 2 | 5 ms | 0.9917 | 0.9935 |
| 5 — Cy5 | 1 | 540 ms | 0.9877 | 0.9894 |

The decline persisted without intervening 35 °C excursions: about **6.1% in the SYBR/FAM
channel** and **7.5% in vendor channel 2**. It was already present in uncorrected pixel sums;
ring-subtracted ratios were 0.9392 and 0.9254, respectively. An independent NumPy extraction
matched all 5,760 saved well/exposure extractions and candidates across this and the comparison
session. Calibration inputs matched by hash. No drift normalization or runtime driver change
was made.

This establishes that the 35 °C excursions are not required for the observed decline. It does
not identify its cause or establish that removing those excursions caused the smaller decline:
each condition has only one run on the same aged plate, at different times and with different
prior illumination and thermal history. Wells are not independent run replicates. Dye/plate
changes, illumination changes and other time-dependent effects remain possible. Current
calibration validity, physical well labeling and quantitative assay accuracy remain unverified;
no dye unmixing, ROX normalization or Ct analysis was performed.

Initial sample/block/cover readings were 25.0/25.0/105.4 °C. The first active observation followed
Start acknowledgement by 1.018 seconds. All programmed targets were 30 °C; the initial approach
to that target is retained in the temperature trace. Cleanup confirmed inactive state 2,
lamp `0000 0002`, stationary filter `0000`, and final readings of **30.0/29.9/104.6 °C**.
No cooling command was sent; the cover remained hot.

Full evidence remains outside Git in `abi7500-research/captures/abi7500-plr-iso30-001/`: executed
source, before/after journals, original frames, calibration inputs, complete USB transcript,
audits, per-well results, matched comparisons and plots labeled with both numbering systems.
USB transcript SHA-256:
`de842bf2dffbc305912147538443d38ac128dd6bf71d7a95a7367dcd6d9f66fb`.

(abi7500-parked-validation)=
## Parking between collections with the lamp enabled

Session `abi7500-plr-park-001` ran on **2026-10-10, 13:05:55.402–13:10:17.308 UTC**, under
explicit approval to verify and close the illumination path between collections while keeping
the lamp warm. It used the existing sealed SYBR Green plate and the same constant-30 °C thermal
program and 30-image exposure plan as `plr-iso30-001`. The added setting was
`CollectionPlan(..., park_between_collections=True)`. It commands absolute position 0 after
both collections, waits for stationary readback, verifies unchanged lamp power 1, and persists
that evidence before recording a complete collection. The same hold/run identity must still
match after parking. No home command or calibration write is sent by this option.

### Optical-path check

Before the thermal Start, eight idle diagnostic frames compared lamp-off/filter 5,
lamp-on/position 0, lamp-on/filter 5, then lamp-on/position 0 again, at 5 and 30 ms each.
The reported sample temperature was 25.0 °C for these checks. Lamp power 1 settled for five
seconds before the first parked read; subsequent diagnostics added warmup before the thermal
run. At **unsaturated 5 ms**, full-image mean counts were:

| State | Mean camera count |
|---|---|
| Lamp off, physical filter 5 | 129.296 |
| Lamp on, position 0 before open read | 134.069 |
| Lamp on, physical filter 5 | 395.737 |
| Lamp on, position 0 after open read | 134.050 |

After subtracting the lamp-off baseline, position 0 retained approximately **1.8% of the
open-path signal**, or **98.2% suppression**. A small exposure-dependent residual remained.
Both parked observations passed the prepared gate (open-minus-dark mean above 10 counts;
parked-minus-dark no more than the larger of 3 counts or 5% of open-minus-dark). The 30 ms open
image clipped 15,073 pixels above 4000, so it is not used to quantify linear attenuation.
Camera suppression supports the optical-path interpretation; it does **not** directly measure
excitation irradiance at the sample or prove complete darkness at the wells.

### Synchronized acquisition and comparison

The thermal program completed normally with matching tagged history reporting **212 seconds**.
There was one prepare/upload/Start, **38 CJ/CC/CD** operations (eight diagnostics plus 30
scheduled images), no retry, Stop, idle-target write, homing, reconnect or plate movement.
Lamp enable/power stayed at 1 throughout the two collections and their intervening interval.
The USB audit validated exact framing, checksums, sequence IDs, counted reads and allowed commands.

| Collection | Scheduled frames | Hold remaining before / after parking | First to last frame | Sample at frame boundaries |
|---|---|---|---|---|
| 1 | 15 | 44 / 13 s | 25.322 s | 30.0 °C |
| 2 | 15 | 45 / 13 s | 25.361 s | 30.0 °C |

First exposures were **101.669 seconds apart**. Both collection records contain stationary
position 0 and lamp `0001 0001` readback. All **960 selected well measurements were unflagged**;
unused longer exposures can still clip. Cached retrieval returned identical results with no USB I/O.

The comparison below uses a fixed exposure and the same 96 positive wells in all four images
(two from each session), with **zero well pixels above 4000**. Values are median
collection-2/collection-1 ratios after background/exposure correction, before uniformity.
Channel names identify optical assignments, not spectrally unmixed dye quantities.

| Vendor channel | Physical position | Exposure | Earlier unparked 30 °C | Parked 30 °C |
|---|---|---|---|---|
| 1 — SYBR/FAM | 5 | 5 ms | 0.9394 | **0.9921** |
| 2 — VIC/JOE | 4 | 5 ms | 0.9255 | **0.9905** |
| 3 — NED/TAMRA/Cy3 | 3 | 5 ms | 0.9908 | 0.9963 |
| 4 — ROX/Texas Red | 2 | 5 ms | 0.9935 | 0.9961 |
| 5 — Cy5 | 1 | 540 ms | 0.9894 | 0.9938 |

The SYBR-channel decline was approximately **0.8% instead of 6.1%**, and vendor channel 2
approximately **0.9% instead of 7.5%**. Ring-subtracted ratios before calibration were 0.99208
and 0.99050, respectively. Independent original-word/NumPy processing matched all **5,760**
well/exposure extractions and candidates across both sessions. Calibration hashes matched;
no drift correction was applied.

This supports a contribution from inter-collection optical state, but does not prove
photobleaching. Each condition has only one run on the same aged plate; prior illumination,
time and extra diagnostic warmup differ. Wells are not independent run replicates. The
vendor sends **`0x67` home after each burst**, verified in both the native collection-end idle
path and original capture frames 11213/15211. Earlier timing comparisons that counted only
absolute `0x69` moves overlooked those home commands. The parked session tested an absolute
move to 0, not homing; a possible alignment benefit from resetting the wheel remains untested.
Current calibration validity, physical well labeling and assay accuracy remain unverified.

Cleanup confirmed inactive state 2, lamp `0000 0002`, stationary filter 0, and sample/block/cover
**30.0/29.9/104.6 °C**, then closed USB. No cooling command was sent; the cover remained hot.
Full evidence stays outside Git in `abi7500-research/captures/abi7500-plr-park-001/`, including
38 original frames, executed source, journals, calibration inputs, diagnostics, per-well
comparisons, command audit and plots. USB transcript SHA-256:
`4c416d40c41caa29e56f458ff6421fc1a6595cbc0bf51fad01b28d3eb9cfc09c`.

(abi7500-homing-validation)=
## Native homing versus absolute parking

Session `abi7500-plr-home-001` ran on **2026-10-10, 13:28:20.961–13:35:58.309 UTC**, with explicit
approval for one four-collection, 60-image comparison. It used the existing sealed plate,
persistent journal, lamp power 1 and a common **30-second warmup at position 0** before Start.
The thermal program repeated 30 °C/40 s then 30 °C/60 s four times. Collection began in each
second hold with the same short exposures: physical position 1 at 540/180/60 ms; positions 2–5
at 30/10/5 ms. `park_between_collections=True, home_after_collections=(2, 4)` requested end
operations **park → home → park → home**, without extra diagnostic images.

### Control and timing

The 400 seconds of programmed holds completed normally, with matching tagged history reporting
**416 seconds**. The first active observation followed Start acknowledgement by 1.002 seconds.
All 60 frames had a reported sample temperature of **30.0 °C** immediately before exposure and
after transfer. Both native homes returned stationary position 0 and unchanged lamp `0001 0001`.
The homing operation is private to an explicitly reserved collection; it is not an untracked
public reset command. Plan ordinals and `homing_requested` are persisted before movement.

| Collection | End operation | Hold remaining before collection / after closure | First to last frame | Command dispatch to first stationary observation |
|---|---|---|---|---|
| 1 | Absolute park (`0x69`) | 45 / 12 s | 26.121 s | 3.471 s |
| 2 | Native home (`0x67`) | 45 / 9 s | 26.288 s | 5.955 s |
| 3 | Absolute park (`0x69`) | 44 / 11 s | 26.826 s | 3.591 s |
| 4 | Native home (`0x67`) | 45 / 9 s | 26.757 s | 6.000 s |

The home waits observed 33 and 34 busy motor replies before stationary readback. Dispatch-to-readback
intervals include USB/polling latency; they are not exact mechanical motion durations. Each
operation fit within its hold. Lamp commands remained unchanged between collections. The full
USB audit verified framing/checksums, sequence IDs, transfer counts and the allowed command list:
one prepare/upload/Start, **60 CJ/CC/CD**, exactly two home commands, and no Stop, idle-target,
plate movement, firmware/calibration/counter write, reconnect or command retry. Cached collection
retrieval returned identical saved results without USB I/O.

### Signal and image comparison

All **1,920 selected well/channel/collection measurements were unflagged** by the native saturation
rule. Unused longer exposures can still clip. The table compares the same **96 positive wells
across all four collections**, requiring zero well pixels above 4000 at the fixed exposure.
Values are median next-collection/preceding-collection ratios after background/exposure correction,
before uniformity. Channel labels do not identify unmixed dye quantities.

| Vendor channel | Physical position | Exposure | 1 → 2, after park | 2 → 3, after home | 3 → 4, after park |
|---|---|---|---|---|---|
| 1 — SYBR/FAM | 5 | 5 ms | **0.99182** | **0.99529** | **0.99574** |
| 2 — VIC/JOE | 4 | 5 ms | 0.99005 | 0.99327 | 0.99478 |
| 3 — NED/TAMRA/Cy3 | 3 | 5 ms | 0.99689 | 0.99937 | 1.00093 |
| 4 — ROX/Texas Red | 2 | 5 ms | 0.99660 | 0.99897 | 0.99973 |
| 5 — Cy5 | 1 | 540 ms | 0.99363 | 0.99596 | 0.99779 |

SYBR-channel declines were **0.82%, 0.47% and 0.43%**, respectively; vendor channel 2 declines
were 0.99%, 0.67% and 0.52%. The first-exposure intervals were 102.044, 102.729 and 101.327 seconds.
The operations are assigned to the interval *after* their collection, not to the images taken
before the operation. The final home validates control/readback only: it has no subsequent image.

Independent original-word cropping and NumPy ROI/ring extraction reproduced all **5,760 unique
well/exposure extractions and candidates**. Ring-subtracted SYBR ratios before calibration were
0.99181, 0.99528 and 0.99573. Fourier whole-image registration estimated at most 0.02 pixel per
axis across the first parked interval, 0.02 after home and 0.01 after the final parked interval,
considering all 15 matched exposure pairs per interval. These are descriptive registration
estimates, not physical alignment bounds, and do not exclude optical changes other than translation.

This validates native collection-end homing on the reference C9254-03/G2.10 instrument within
this tested timing budget. It does **not** demonstrate an additional alignment or stability
benefit over absolute parking: the last parked interval was similar to the home interval and
might retain a home-induced alignment change. There is only one fixed-order run on an aged
plate, with one imaged home interval and two parked intervals. Lamp warmup, plate history and
time remain potential contributors; wells are not independent run replicates. No drift correction
or automatic switch to homing was added. Homing remains explicit and older plans keep their
original behavior. Current calibration validity, physical well labeling, photobleaching mechanism,
broader thermal programs and quantitative assay accuracy remain unverified.

Cleanup confirmed inactive state 2, stationary filter 0, lamp `0000 0002`, and sample/block/cover
**30.0/30.0/105.2 °C**, then closed USB. The cover remained hot; no cooling command was sent.
Full evidence is outside Git in `abi7500-research/captures/abi7500-plr-home-001/`: original frames,
source, journals, calibration inputs, complete USB trace, movement timings, well measurements,
independent comparisons and plots. USB transcript SHA-256:
`9e4485c61085740326765abc59d074db2f6e3d2de45d10768fb084c56749a690`.


(abi7500-cycling-home-validation)=
## Two thermal cycles with collection-end homing

Session `abi7500-plr-cychome-001` ran on **2026-10-10, 14:00:31.851–14:04:28.959 UTC**,
with fresh approval for one next-step session. It combined the previously checked thermal
program and short exposures with native homing after **both** collections and the scheduler's
checkpoint-only progress reads. The existing sealed plate was retained.

The program repeated **35 °C/15 s then 30 °C/60 s twice**, with collection in each 30 °C hold.
Physical filter 1 used 540/180/60 ms; filters 2–5 used 30/10/5 ms. The thermal upload and exposure
choices match `plr-lowexp-001`. This session used lamp power 1 and a **30-second warmup at
position 0**, compared with five seconds in that earlier session. The plan explicitly set
`park_between_collections=True, home_after_collections=(1, 2)`, with a 45-second read window
and 900-second overall observation budget. There were **30 scheduled images and no diagnostic
exposures**. Thermal timing remained on the instrument.

### Completion, timing and saved progress

The 150 seconds of programmed holds completed normally; matching tagged run history reported
**197 seconds** including the thermal transitions. The first active observation followed Start
acknowledgement by **1.024 seconds**. Every frame's before/after status identified
the intended run and hold, and reported sample **30.0 °C**. Both homes returned stationary
position 0 with lamp `0001 0001` before the collection was accepted.

| Collection | Hold remaining before collection / after closure | First to last frame | Home dispatch to first stationary observation |
|---|---|---|---|
| 1 | 44 / 8 s | 27.110 s | 6.154 s |
| 2 | 44 / 7 s | 27.763 s | 5.947 s |

The observed home intervals include USB and polling latency. Motor polling observed
34 and 33 busy replies, respectively. Both operations fit inside their holds; the second home
has no following acquisition and validates motion/readback only. First exposures were
**96.202 seconds** apart.

The full transcript audit verified packet framing/checksums, sequence IDs and the permitted
command list: one prepare/upload/Start, **30 CJ/CC/CD**, ten absolute filter selections and two
native homes. There was no Stop, idle-target write, plate-loading movement, reconnect, retry,
firmware change or calibration/counter write. Lamp commands remained unchanged between reads.
Saved progress returned the same checkpoints as the collection results, and both the explicit
progress read and repeated completed-result retrieval caused **zero USB I/O**. Progress polling
uses checkpoints without reloading accumulated image payloads; this run does not establish a
worst-case timing bound for longer programs.

### Fluorescence comparison

All **960 selected well/channel/cycle measurements were unflagged** by the native saturation
rule. Some unused longer exposures still clip. Independent cropping and NumPy ROI/ring
processing reproduced all **2,880 current well/exposure extractions and candidates**; with the
two earlier comparison sessions, 8,640 such comparisons matched.

The table uses the same **96 positive, unclipped wells in all six observations** and fixed
exposures. Ratios are median cycle-2/cycle-1 background/exposure-corrected candidates before
uniformity. Channel labels do not identify unmixed dye quantities.

| Vendor channel | Physical filter | Fixed exposure | Earlier 35/30 °C, last filter selected | Earlier constant 30 °C, absolute park | This 35/30 °C run, native home |
|---|---|---|---|---|---|
| 1 — SYBR/FAM | 5 | 5 ms | 0.92276 | 0.99210 | 0.99054 |
| 2 — VIC/JOE | 4 | 5 ms | 0.89784 | 0.99055 | 0.98877 |
| 3 — NED/TAMRA/Cy3 | 3 | 5 ms | 0.98692 | 0.99629 | 0.99693 |
| 4 — ROX/Texas Red | 2 | 5 ms | 0.99172 | 0.99615 | 0.99580 |
| 5 — Cy5 | 1 | 540 ms | 0.98769 | 0.99376 | 0.99368 |

SYBR-channel decline was **0.95%**, versus **7.72%** in the earlier cycling session and 0.79%
in the constant-30 °C parking session. Channel 2 declined 1.12%, versus 10.22% and 0.95%.
The current SYBR ratio before calibration (ring-subtracted pixels) was 0.99053, so calibration
processing does not create this difference. Whole-image Fourier registration estimated shifts
of at most 0.01 pixel per axis across all 15 matched exposure pairs; these are descriptive
estimates, not physical alignment bounds.

This validates the combined two-cycle acquisition, native homing and updated scheduler on the
reference instrument. The smaller signal decline is consistent with the earlier closed-path
results, but does not isolate a mechanism or show homing superior to absolute parking. These
are chronological sessions on one aged plate, with different prior illumination and lamp
warmup; the constant-temperature comparison also differs in thermal timing. There is only one
between-collection interval in the current run. Wells are not independent run replicates, and
plotted well percentiles are not confidence intervals. Photobleaching, current calibration
validity, physical well labeling, longer/hotter programs and quantitative assay accuracy remain
unverified. No drift normalization or automatic change to collection defaults was introduced.

Cleanup confirmed inactive state 2, stationary filter 0, lamp `0000 0002`, and sample/block/cover
**30.0/30.0/105.3 °C**, then closed USB. The cover remained hot; no cooling command was sent.
The 159-test ABI package, scoped typing and lint/format checks covered the unchanged driver
source before this run; six offline runner scenarios checked success and failure cleanup.
Full evidence remains outside Git in `abi7500-research/captures/abi7500-plr-cychome-001/`,
including source snapshots, journals, calibration inputs, original frames, USB trace,
measurements and comparisons. Transcript SHA-256:
`5c4be1060a0e9f619137e09dc4441d60b1033eeb4b064538cbe7c17af3f5baf3`.


(abi7500-sybr10-validation)=
## Ten-cycle SYBR-channel acquisition and portable export

The approved `abi7500-plr-sybr10-001` session ran **2026-10-10, 14:25:23.872–14:42:10.185 UTC**
on the reference C9254-03/G2.10 instrument from Ubuntu, with the existing sealed plate.
It repeated **35 °C/15 s then 30 °C/60 s ten times**, for 750 seconds of programmed holds.
Each 30 °C hold collected **30/10/5 ms** at physical filter **5** (vendor channel 1, SYBR/FAM),
giving **30 images and 960 selected well/cycle measurements**. Other channels were not collected.
The plan used a 45-second read window, native home after all ten collections, lamp power 1,
30-second lamp warmup at position 0 and an **1800-second overall observation budget**.
This was an engineering time series, not an amplification experiment.

### Control, timing and original data

The program completed normally with matching tagged history of **964 seconds**.
The first active observation followed Start acknowledgement by 1.030 seconds.
Every frame was associated with the intended run and hold; all before/after sample estimates
were **30.0 °C**. Every home verified stationary position 0 with the lamp
still enabled. Home dispatch to first stationary observation ranged from **5.970 to
6.158 seconds**, including polling latency.

| Cycle | Hold remaining before / after collection and home | First frame to last frame | Median 5 ms signal / cycle 1 |
|---|---|---|---|
| 1 | 45 / 30 s | 3.869 s | 1.00000 |
| 2 | 45 / 30 s | 3.696 s | 0.98952 |
| 3 | 44 / 30 s | 3.864 s | 0.98473 |
| 4 | 45 / 30 s | 3.578 s | 0.98188 |
| 5 | 45 / 30 s | 3.760 s | 0.97912 |
| 6 | 45 / 30 s | 3.813 s | 0.97639 |
| 7 | 44 / 29 s | 3.807 s | 0.97430 |
| 8 | 44 / 29 s | 3.858 s | 0.97142 |
| 9 | 44 / 29 s | 3.930 s | 0.96921 |
| 10 | 44 / 29 s | 3.759 s | 0.96691 |

The byte audit verified one prepare/upload/Start, 30 CJ/CC/CD sequences, ten physical-filter-5
selections and ten native homes. No Stop, idle-target write, reconnect or acquisition replay
occurred. Progress reads and cached completed-result retrieval added no USB traffic.
This validates ten repeats on this host within this tested budget; it is not a worst-case timing
bound or a validation of longer, hotter or multi-channel programs.

### Measurements and export

**0 of 960 selected results** were flagged by the native saturation
rule. Unused exposure candidates, including any clipped pixels, remain available. Independent
NumPy cropping and ROI/ring calculations reproduced all **2,880 well/exposure extractions and
normalized candidates**; independent exposure selection and binary32 uniformity multiplication
matched all **960 selected results**.

The fixed-5-ms trend used the same **96 positive wells with zero well pixels above 4000
in every cycle**. The median cycle-10/cycle-1 ratio was **0.96691** after background
and exposure correction (-3.31% change), with well 5th–95th percentiles
0.95993–0.97878. The corresponding ring-subtracted ratio was
**0.96690**. These percentiles describe wells, not confidence
intervals or independent run replicates. Whole-image registration estimated at most
0.02 pixel per axis relative to cycle 1; this is not
a physical alignment bound.

The three-image bursts change illumination duration and timing compared with earlier
fifteen-image collections. One chronological series on an aged plate cannot isolate
photobleaching, lamp drift or other mechanisms. No correction was applied to remove temporal
drift. Calibration validity, physical well labeling and quantitative assay accuracy remain
unverified.

After cleanup, `export_run()` produced a portable ZIP. Its 30 original images matched byte for
byte, and all 960 selected values plus 2,880 exposure candidates matched the saved evidence.
Export issued **zero USB calls** and left the journal unchanged. Original images, calibration
bytes, saturated candidates, metadata and content hashes are retained.

Cleanup confirmed inactive state 2, stationary filter 0 and lamp `0000 0002`;
sample/block/cover were **30.0/29.9/105.2 °C**.
The cover remained hot; closing USB did not cool it. The executed source matched the version
with 166 passing package tests; six offline runner scenarios covered setup rejection and failure
cleanup. Full evidence remains outside Git in `abi7500-research/captures/abi7500-plr-sybr10-001/`.
USB transcript SHA-256: `6a2691b932dfd162a039d579887705a87b205a02e6fb1aa9411b3dcf6243b11f`.

(abi7500-wellmap-validation)=
## Physical well labels with a known-pattern fluorescein plate

The approved `abi7500-plr-wellmap-001` session ran **2026-10-10,
15:36:49.610–15:38:51.489 UTC** on the reference instrument from Ubuntu.
The operator loaded a VWR **89218-296** 96-well plate with a clear cover and confirmed
**A1 at the back-left**, viewed from the front of the instrument. Wells **A1, A4, C9, F3,
H7 and H12** each contained **20 µL of 3.2 µM fluorescein**; the other 90 wells were empty.
The physical labels and orientation are operator-provided ground truth. The diluent and exact
seal product were not reported; empty wells are not solvent-matched blanks.

PLR executed one **30 °C/60-second hold**, collecting **5, 10 and 30 ms**, in that order,
at physical filter **5** (vendor channel 1). Lamp power was 1, with 30 seconds of warmup
at position 0, a 45-second read window and one native home after the burst.
Thermal completion was normal, with matching tagged history of **68 seconds**.
The collection and home began with 44 seconds remaining and finished with 29 seconds remaining.
The three-frame interval was **3.735 seconds**; every before/after sample estimate was **30.0 °C**.

### Label and image processing checks

**All six expected wells were the six brightest regions in every image**, both by raw ROI mean
and by ring-subtracted sum. No rotation, mask, labeling or calibration adjustment was made to
obtain the match. Independent NumPy reconstruction of the original camera payloads reproduced
the PLR crop; independent ROI and ring calculations matched all **288 well/exposure extractions**.

| Exposure | Positive-well raw mean range | Empty-well raw mean range | Native saturated wells |
|---|---|---|---|
| 5 ms | 350.43–545.79 | 129.28–134.64 | None |
| 10 ms | 573.81–969.61 | 130.29–139.82 | None |
| 30 ms | 1448.19–2403.40 | 133.34–157.86 | A4, C9, F3, H7 |

The 5 ms and 10 ms images had no clipped well pixels. At 30 ms the four flagged wells also
contained clipped pixels. All candidates and flags were retained; exposure selection produced
**96 results with no selected saturation flags**, using shorter images where required.
These raw pixel statistics establish signal separation, not a concentration calibration.

The six-marker pattern distinguishes the expected mapping from row reversal, column reversal,
180° reversal and column-major labeling. The expected mapping matched all six markers;
those alternatives matched 0, 0, 2 and 2 respectively. This validates the physical orientation
and **these six labels in filter 5 on this plate**. It does not exclude every arbitrary
permutation of all 96 wells or establish other channels, current calibration validity,
spectral unmixing or quantitative assay accuracy.

### Cleanup and preserved evidence

The byte audit verified one load, one prepare/upload/Start, three CJ/CC/CD sequences,
one filter selection and one native home. There was no Stop, idle-temperature write, reconnect
or exposure replay. After normal completion and matching run-history reconciliation,
cleanup disabled the lamp and verified stationary filter 0. A single journaled `0x72`
plate-release command was acknowledged; `0x73` polling confirmed state 2 → 1 → 0.
USB was then closed. The last sample/block/cover readings were **30.0/29.8/105.0 °C**;
the cover remained hot. Plate release in this session used the audited research runner;
`stop()` still only closes communication and does not release or cool the instrument.

The preceding separately approved `abi7500-plr-release-001` session used the same release command
after reconciling the completed ten-cycle run. The operator confirmed that the drawer opened
before loading this known-pattern plate. No firmware or calibration changes were made.

Offline `export_run()` retained all three original camera transfers byte for byte, all 96 selected
values and all 288 candidates, with zero USB operations and an unchanged journal. The portable
archive is `plr-wellmap-001-results.zip`, SHA-256
`f517de3dc9dfdd163e0833c03bd54caaea5c26d14ec2832eee5b45d73816f7cc`.
The unchanged driver passed **166 package tests** before this session; eight offline runner
scenarios checked setup rejection, success and failure cleanup, including ambiguous release.
Full frames, calibration inputs, USB trace, source snapshots, operator confirmation, independent
analysis and the plate-map figure remain outside Git in
`abi7500-research/captures/abi7500-plr-wellmap-001/`.
USB transcript SHA-256: `27bc8cf4c97b4d3702c3c132789b182c2958d579eed6e8971c659cf19a6c2a9c`.
