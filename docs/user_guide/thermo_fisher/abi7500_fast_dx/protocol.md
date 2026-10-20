# Recovered packet formats

These formats describe the reference software and captured traffic. Connection, identity, status
and thermal-only start/cancel and 35 °C/60-second completion have been exercised from Ubuntu; see the
[hardware validation report](hardware-validation.md). Thermal-only run control is experimental and
limited to the reported sessions on G2.10; wider programs and fault recovery remain unverified.
Direct Ubuntu snapshots verified illuminated well patterns at filters 1 and 5, with lamp-off and
filter-0 restoration. Offline camera processing matches all 960 saved values from one recorded
vendor acquisition. A scheduled Ubuntu session completed two mild cycles normally with 30 frames
and matching run history. Broader thermal programs, current calibration validity and optical
recovery remain unverified.
The codecs in `pylabrobot.thermo_fisher.abi7500_fast_dx.protocol` operate entirely offline.

## Evidence and limits

Sources include the downloaded **7500 Software v2.3** installer, the reference PC's installation
and logs, and approved USB captures of startup/shutdown, a completed acquisition, and cancellation.
The installer's
[release notes](https://downloads.thermofisher.com/7500/100023991%20-%207500%20and%207500F%20F%20Software%20v2.3_vSubmitted.pdf)
list Windows 10 x64 and 7500/7500 Fast; they do not establish Fast Dx compatibility.
Prefer the original control PC's working software until its version and instrument compatibility
are known.

| Reference | SHA-256 | Relevant entry points (RVA) |
|---|---|---|
| `BudgieManagerEXT.dll` from the main installer CAB | `9dbff7366d38a6c43b3ba363dd007b40f65d0dead3c1d3b9eb09428f91a8102f` | `_buildCommand` `0x370b0`, `_prepareWriteBuffer` `0x36e48`, `checksum` `0x3764b` |
| 77,824-byte `ArpLib.dll` from the main installer CAB | `4d4c5e7db38a9a8a6cefeab214eb1efd721d5a12dd201bbc7bdeabff3b05f93b` | `ARPAPI_SendString` `0x2c70`, `ARPAPI_GetString` `0x2d30`, send helper `0x5930` |

The installer also contains larger Win32/Win64 ArpLib DLLs in its CCD driver directories. Their
behavior is not established by the smaller DLL's analysis. The reference PC's v2.3 installation
contains byte-identical copies of both DLLs listed above. Compare USB captures before relying on
these formats for control; matching installed files does not establish the Windows driver's USB
transfer behavior.

The small `test_data/encoder_vectors.json` fixture contains outputs of the thermal encoder executed
in an isolated x86 emulator. Only memory-copy and memory-fill external calls were emulated; there
was no OS or instrument access. The vectors cover empty, binary, multi-packet, and maximum-size
payloads. They are **binary-derived fixtures, not hardware captures**. Arbitrary payload vectors
test framing and do not represent valid experiment instructions. Installer files, decompilation
outputs, calibration backups, and full captures must remain outside Git.

The reference PC's `config/logs/shell.log` contains 499 complete thermal packets: 249 matching
request/reply pairs and an initial reply whose request precedes the retained log. All 499 satisfy
the length and checksum rules below. Every complete pair echoes its command and sequence, and
every recorded packet uses address `ff ff ff ff` and error field zero. These observations cover
the saved log, not all firmware or error conditions. `test_data/thermal_log_vectors.json` retains
eight unmodified packets from four query/reply pairs, with the full log's hash and source lines.
These are vendor debug-log bytes, not USB captures; the outer USB messages are absent.

The separate `abi7500-connect-001.pcapng` capture has SHA-256
`f78202bd40ec06915d06936a8528644f92bde7d4cd4bf23befedeab6c62e6a35`.
It contains 590 USBPcap records over 119.385 seconds, all for bus 1, device 7. All captured lengths
equal original lengths. The 295 submission/completion pairs are complete, and every completion
reports USB status zero. Drop statistics are absent, so zero packet loss cannot be independently
established. USBPcap submission status fields are not completion results.

The first six records are injected descriptor/configuration metadata at a common timestamp, not
evidence of physical re-enumeration during the session. The remaining records contain 221 bulk
writes and 71 bulk reads. The 55 thermal requests and 55 reconstructed replies match the
corresponding `shell.log` bytes exactly; the log also contains older sessions that must be excluded
from this comparison. `test_data/usb_serial_vectors.json` retains a small selection of unmodified
bulk payloads, including padded empty reads and replies split across multiple serial reads.

## USB and the serial tunnel

The bundled `ArpDrv.inf` associates the Hamamatsu camera with USB VID `0x0c2b`, PID `0x0300`.
An Ubuntu descriptor inventory for the available instrument reported a C9254-01 device, interface
0, bulk OUT endpoint `0x06`, and bulk IN endpoint `0x85`, with 64-byte endpoint packets.
The startup capture confirms these endpoints and VID/PID. Its camera identification response names
`C9254-03`, which differs from the earlier USB product string; retain both identifiers rather than
assuming they are interchangeable model names.

The PC uses the external **USB Type-B port**. The reference library tunnels thermal-controller
serial packets inside Hamamatsu USB messages. This does not make the external connection RS-232
and does not expose a Linux serial port. The
[EZ-Tap Pro](https://stratusengineering.com/product/ez-tap-pro-rs-232-passive-tap-module-bus-analyzer-protocol-analyzer-2/)
monitors RS-232 wiring; its USB connector transfers those captures to a host. Use USB capture for
the 7500's external cable.

| Message | Format | Implemented codec |
|---|---|---|
| Serial write | ASCII `RS`, payload length as four space-padded decimal characters, then binary payload | `encode_serial_write` |
| Serial read request | ASCII `RR`, four-character decimal length including the two-byte reply count | `encode_serial_read` |
| Serial read reply | Little-endian unsigned 16-bit payload count, then serial bytes | `decode_serial_read` |

The serial payload limit is 1024 bytes. For example, sending 21 serial bytes produces `RS  21`
followed by those bytes; requesting 21 serial bytes produces `RR  23`. The write helper divides the
wrapped message into writes of at most 64 bytes, without a terminator or an extra zero-length write.
The wrapper can make a full 1024-byte payload span 1030 bytes.

In the startup capture, all 64 RR replies occupy exactly the requested wire length. The first two
bytes report how many following bytes are valid serial data; the remaining tail can contain
arbitrary nonzero bytes. Five reads have count zero despite nonzero tails. Four version replies
arrive over two RR transactions each: a 91-byte thermal reply splits into 68+23, 70+21, 70+21, or
35+56 valid bytes. The application requests the remaining serial length on the second read.
Each RR reply in this capture is one USB read completion, including those longer than the
endpoint's 64-byte maximum packet size; host transfer records are not individual USB bus packets.

Pass the original serial capacity as `decode_serial_read(data, requested_size=capacity)` to
validate the complete wire length and extract only the counted bytes. Without that argument, the
decoder accepts only an unpadded reply. Feed the extracted bytes into `ThermalPacketDecoder` to
assemble complete thermal packets. Never feed the unused tail into that decoder or interpret a
zero count as a complete thermal response. The observation driver polls empty reads within one
command deadline, matches the command/address/sequence, and never automatically resends a query.

## Observed startup and shutdown

The capture begins with the vendor login dialog and includes a warning that one or more
calibrations are expired or invalid. The operator reports choosing **Ignore & Continue Startup**.
The log reports a successful connection and later disconnection. This establishes communication
with the reference installation, not valid calibration or validated fluorescence measurements.

Before the first thermal query, the captured bulk exchanges include:

| OUT message | IN response or observed purpose |
|---|---|
| `CH   5` | Five ASCII bytes, `ready` |
| `Cf  15,,.,1.05,2.19,,` | Reference initialization message; no corresponding IN transfer |
| `CF  64` | 64 bytes containing a NUL-terminated camera identification string and an opaque tail |
| `CI   4` | `41 01 f5 00`, two little-endian values: 321 and 245 |
| `RP  109600,8,0,0` | Internal serial configuration, not an external RS-232 connection |
| `RC` | Serial flush, also observed during setup and at application shutdown |

`ARPAPI_GetImageSize` at RVA `0x2320` subtracts one from the first CI value and, in its default
mode, five from the second, yielding 320 by 240. Preserve the original CI values; the complete
transfer and reference crop are described below.

Startup also sends thermal filter-wheel home/move and lamp configuration commands; merely opening
the application is not a passive status query. While idle, the capture contains 88 `C:` writes
at roughly half-second intervals without corresponding bulk IN transfers. The reference library's
background routine at RVA `0x1070` contains that command. Its timing and recovery requirements need
further validation. These observations are protocol evidence, not a script to replay blindly.

## Thermal packets

Multi-byte thermal fields use **big endian**, unlike the outer read count.

| Offset | Size | Meaning |
|---|---|---|
| 0 | 2 | ASCII `XX` |
| 2 | 4 | Address; outbound reference requests use `ff ff ff ff` |
| 6 | 2 | Length excluding the first six bytes: `15 + payload length` |
| 8 | 4 | ASCII `9700` |
| 12 | 2 | Command identifier |
| 14 | 2 | Error field; zero in requests |
| 16 | 4 | Message sequence number |
| 20 | variable | Binary command payload |
| last | 1 | Sum of bytes from offset 6 through the payload, modulo 256 |

Total size is `21 + payload length`; the tunnel limit allows a payload of at most 1003 bytes.
Although the native buffer writer places a `#` after the checksum in memory, that byte is excluded
from the transmitted length. It must not be appended to the encoded packet.
The native function named `crcCheck8` calls an additive checksum, not a polynomial CRC.

A reference temperature query with sequence 1 is:

```text
58 58 ff ff ff ff 00 0f 39 37 30 30 00 06 00 00 00 00 00 01 e6
```

Static inspection also identifies the following command IDs and expected response payload sizes.
These are evidence for capture annotation, not exposed hardware methods:

| ID | Native operation | Expected response payload |
|---|---|---|
| `0x06` | GetTemperature | 16 bytes |
| `0x0a` | GetVersion | 70 bytes |
| `0x17` | GetInstrumentState | 46 bytes |
| `0x36` | GetCoverTemp | 6 bytes |

The framing decoder preserves opaque payloads and errors; it does not treat a valid checksum as a
successful operation. The separate extended-status decoder below interprets fields verified against
native code and vendor logs. Complete state/error enumerations and run reconciliation after failures
or reconnects still require validation.

## Acquisition and cancellation captures

Two engineering sessions on the original Windows 10 PC used 7500 Software v2.3 and the existing
sealed plate. The operator confirmed that the plate and seal appeared intact. Expired calibration
warnings were acknowledged; these sessions do not validate an assay or calibration. The original
reference experiment was preserved, and separate templates were used for the test experiments.

| Capture | SHA-256 | USB records / complete pairs | Thermal pairs | Complete camera frames |
|---|---|---|---|---|
| `abi7500-acquire-001.pcap` | `4a76d6b4b604cfffaad61081dd83eb8fb9a64f18a453c5a48f2a3504738671bd` | 15,782 / 7,891 | 643 | 46 |
| `abi7500-cancel-001.pcap` | `ca31a95997b6c8a52b17ea17f701da0eb49de87d79bca8b9d22fc198f77cf118` | 6,962 / 3,481 | 332 | 16 |

Both captures have complete submission/completion pairs, equal captured/original record lengths,
and zero USB completion statuses. Every reconstructed thermal request and reply matches its vendor
log bytes, including command, sequence, payload, and checksum. All thermal error fields are zero.
Drop statistics are unavailable; completeness checks do not independently establish zero packet
loss. These captures started after the application's initial connection and include run-specific
initialization. The separate connection capture supplies the application startup evidence.

The completed program uses Standard mode, 20 µL, a 105 °C cover, an initial 95 °C hold for 15 seconds,
then two cycles of 95 °C for 15 seconds and 60 °C for 60 seconds with collection. The vendor reports
completion at 2026-10-09 10:28:57.231, Windows local time (UTC−04:00).

The cancellation session uses the same program. The vendor logs the stop action at 10:38:28.731
during the initial heating ramp and finishes cancellation at 10:38:40.574, reporting
`onservercommandcomplete(33, -38, "User stopped pending command.")`. This is an application result,
not a thermal packet error code. Capture includes normal application disconnection afterward.

Both sessions contain an identical 98-byte thermal program payload under command `0x12`, followed
by a `0x13` start request. Cancellation adds a `0x07` stop and a `0x22` request with payload `00 fa`.
The native `StopMethod` routine also emits `0x07` and a setpoint call with argument 250. These
observations support the experimental thermal-only API; they do not validate its hardware behavior.
`test_data/usb_run_vectors.json` retains the exact program/start/stop exchange bytes, including
the two OUT transfer boundaries of the program request. Tests check framing without replaying them.

## Experimental connection and observation

`pylabrobot.thermo_fisher.ABI7500FastDx` exposes `setup()`, `get_identity()`, `get_status()` and
`stop()`. These operations were checked on one G2.10 instrument with Windows disconnected.
`get_history()`, `start_run()`, `get_run()` and `cancel_run()` also succeeded in thermal-only
Ubuntu sessions: one start/cancel and one completed 35 °C/60-second hold. See
[thermal runs](thermal-runs.md) for remaining limits. Importing or
constructing the class does not open the instrument.
Setup opens USB, checks `CH`,
sends the captured `Cf` initialization metadata, reads `CF`/`CI`, configures the internal serial
tunnel with `RP`, flushes it with `RC`, and checks the thermal application mode (`0x2f`) and version
(`0x0a`). A bootloader response is rejected without attempting a firmware upload. This is a subset
of vendor startup: no filter homing, lamp configuration or thermal-program commands are sent.
This reduced initialization sequence succeeded in `abi7500-plr-observe-001`. The opaque `Cf`
metadata is still specific to the reference handshake; other firmware and startup states are untested.

The connection sends `C:` keepalives every half second, serialized with queries. A keepalive failure,
malformed reply, cancellation during an exchange, or timeout invalidates the connection. Subsequent
queries require `stop()` and a new `setup()`. There are no automatic command retries. Each thermal
response must match the request's command, sequence and address and have a zero error field; error
packets retain their original numeric code. The timeout covers all fragments and empty serial polls,
with up to one extra second permitted by the USB transport's integer read-timeout granularity.
In-flight USB executor calls finish before cancellation releases the transaction lock or closes USB.

The USB transport's `send_zero_length_packet=False` option prevents a second, empty write after
64-byte chunks. Its default remains true for existing drivers. Camera responses are assembled to
their exact requested length, and counted serial replies exclude their arbitrary padding. `stop()`
closes communication; it is not a command to cancel or cool an active run.

The Ubuntu session contains eight matched thermal queries and 167 counted serial reads. Boot and
status responses arrived in fragments as small as one byte, interspersed with zero-count reads and
nonzero padded tails. `test_data/ubuntu_observation_vectors.json` retains two complete exchanges
for transport replay tests. This is a PLR I/O transcript, not a kernel USB capture.

## Extended thermal status

Command `0x29` returns 50 payload bytes. `GetRavenState` (RVA `0x3a8ac`) establishes byte swapping;
`Ask9700ForStatus` (RVA `0x3c4c5`) supplies sequence progress interpretation. The decoded temperatures
and elapsed time agree with 228 corresponding human-readable `Poll9700` log records across the
acquisition and cancellation captures. A vendor log entry with all-zero application status at the
completion boundary is excluded: it is not a faithful representation of the preceding wire reply.
`test_data/status_vectors.json` retains representative captured replies and independent log values.

| Payload offset | Format | Interpretation |
|---|---|---|
| 1 | byte | Native controller state; full enumeration unverified |
| 4 | BE32 | Measured block temperature, tenths of a degree Celsius |
| 8 | BE32 | Measured cover temperature, tenths of a degree Celsius |
| 12 | BE32 | Measured ambient temperature, tenths of a degree Celsius |
| 16 | BE32 | Elapsed counter in seconds; can advance while inactive |
| 20 | BE32 | Remaining hold seconds; `ffffffff` is unavailable; can be stale while inactive |
| 26 | BE16 | Estimated sample temperature, tenths of a degree Celsius |
| 28 | byte | Native zero-based cycle-group index |
| 29 | byte | Native zero-based step index |
| 30 | byte | Sequence phase: 0 inactive, 1 ramping, 2 holding, 4 paused, 8 modified |
| 31 | byte | Repetition counter, one based during the captured run |

The decoder retains all 50 original bytes, including unclassified flags and native date records.
An inactive phase can follow completion, cancellation, or other transitions and does not establish
which run produced it. Native cycle/step indices can differ from a vendor experiment's stage/step
indices. Never infer successful completion or ownership of an active run from these fields alone.
The Ubuntu observation session reported an inactive sequence while elapsed time advanced from
11557 to 11562 seconds, hold time remained 14 seconds, and cover temperature fell from 105.3 to
104.8 °C. Neither advancing elapsed time nor a nonzero hold value proves an active sequence;
an inactive sequence does not imply that the cover is cool.

## Internal plate state and release

Native `TC9700Control::EjectPlate` dispatches empty command `0x72`; `GetBlockState` uses `0x73`
with a two-byte big-endian reply. Captured states are unloaded 0, moving 1 and loaded 2. A release
acknowledgement precedes motion completion. The external drawer position and plate presence are
not established by this response. `plate_release_vectors.json` retains original successful and
cancelled vendor-run release exchanges plus the Ubuntu status/release transfers.

The public {ref}`plate API <abi7500-plate-release>` requires matching finished-run evidence and
parked, dark optics, commits release intent before dispatch, then observes motion without replay.
This is distinct from `0x71` loading during run preparation and from thermal cancellation.

## Thermal program records

`ThermalProgram` encodes finite cycle/hold records under `0x12`. The native
`AddSegment` (RVA `0x3b3ec`) and `AddSetpoint` (`0x3b4ae`) builders, followed by
`_getFullMethodBytes` (`0x3b7f1`) and `_covertMethod2WriteBuffer` (`0x39785`), reproduce the captured
98-byte program exactly in isolated x86 emulation. Five fixture vectors also vary cycle count,
temperature, hold time, increments and ramp percentage. Only memory copying and a successful
ramp-validation callback are substituted; these vectors validate encoding, not firmware acceptance.

The payload begins with a cycle-group count and 47 metadata bytes. The codec preserves the metadata
instead of inventing dates or block/mode defaults. This includes method name (17 bytes), user name
(7), two native date records (9 each), volume (1), block type (2), run mode (1) and tube type (1).
Each cycle begins with a ten-byte record:

| Offset | Cycle record | Setpoint record |
|---|---|---|
| 0 | `02` | `03` |
| 1 | `00` | `00`; nonzero 9600-emulation flags unsupported |
| 2–3 | Repetitions byte, step-count byte | BE16 hold seconds |
| 4–5 | Zero | BE16 temperature in tenths of a degree |
| 6–7 | Zero | Signed BE16 hold increment per repetition |
| 8 | Zero | Signed byte temperature increment in tenths of a degree |
| 9 | Zero | Ramp percentage |

The implemented subset allows one to 99 repetitions, one to seven steps per group, and at most 49
total group/step records, following the native validator. Holds are finite (0–5939 seconds), and
temperatures are 4.0–99.9 °C. Pause records and infinite holds are rejected. Exact tenths are required;
temperatures are not silently rounded. Rate acceptance still depends on the block/firmware.
Optical collection flags and cover temperature are not in this payload, so uploading these bytes
alone would not implement a complete qPCR experiment. The public run API restricts configuration
to captured Standard/block-2/tube-0 headers and stamps the method/user labels for ownership.
The default accepts 20 µL. Explicit calibration startup or normal `standby_startup=True`
additionally accepts the captured 30 µL controller setting; this is not a measurement of the
actual liquid volume.
It exposes no raw upload or command replay method.

## Thermal history and run identity

`GetHistoryHead` (RVA `0x437ac`) queries `0x19` for 94 bytes. `FormatHistoryRecordHeader`
(`0x44185`) labels the following fields; the completed and cancelled captures provide one history
reply each. Both preserve the uploaded method/user labels at offsets 46 and 63. No current-run
method readback has been established: history can lag the active sequence.

| Offset | Format | Meaning |
|---|---|---|
| 6 | BE16 | Outcome: 0 completed normally, 1 aborted early, 253 no final result (`no_result`) |
| 10 | 9 bytes | Native run start date record |
| 19 | 9 bytes | Native run end date record |
| 28 | BE32 | Run duration, seconds |
| 32 | BE16 | Reaction volume, µL |
| 46 | 17 bytes | NUL-terminated method label, matching the captured uploaded name |
| 63 | 7 bytes | NUL-terminated user label, matching the captured uploaded user |

The history start record matches bytes 32–40 of extended status for the corresponding run. These
bytes remain opaque; neither clock synchronization nor timestamp uniqueness is assumed. Initial
status replies after an acknowledged `0x13` still contain the preceding run's date, indices and
elapsed counter. The controller can report state 3 with inactive sequence phase while starting.
The PLR driver requires fresh evidence before reporting active progress, and never infers completion
from sequence inactivity alone.

The native formatter describes code 253 as no run since power-on, but the direct Ubuntu session
also returned it during active ramping, with the fresh PLR method/user labels and start record.
PLR therefore reports `no_result`, without inferring inactivity. During cover warmup, state 3 and
inactive sequence phase persisted for about 273 seconds with the previous run's history. Active
progress, rather than the acknowledgement alone, established that the thermal step had begun.

The separate Ubuntu hold session reached holding phase with 60 seconds remaining at elapsed 13,
then 1 second remaining at elapsed 72, and recorded normal completion at elapsed 73. A transient
inactive/state-6 reply preceded inactive/state-2 status. PLR kept the transition `uncertain` until
the recognized idle state and matching completed history agreed. No broad meaning is assigned to
state 6. The last target remained 35 °C after normal completion; no automatic idle-target command
is part of `get_run()` or connection teardown.

The experimental lifecycle uses `0x1b`, a one-second native preparation delay, `0x12`, a half-second
native upload delay, plate-state query `0x73`, optional load `0x71`, then `0x13`. The captures show
plate state 1 during motion, 2 after loading, and 0 after unloading. The driver polls loading to
state 2 and rechecks thermal inactivity before Start. Optical initialization and automatic plate
ejection are not part of this thermal-only sequence. Cancellation uses `0x07` only for an identified
active sequence, confirms inactivity, then sends `0x22` with `00fa` (25 °C idle target).

`test_data/run_control_vectors.json` retains the unmodified vendor reference exchanges.
`test_data/ubuntu_run_vectors.json` retains original Ubuntu upload/load/start/stop/idle exchanges and
checkpoints, including preserved `plr-cancel-001` / `PLR` labels and a fresh matching history record.
`test_data/ubuntu_hold_vectors.json` retains the completed hold's exchanges and status/history
observations, including both boundary transitions and the `plr-hold-001` / `PLR` completed record.
Other fault tests use explicitly synthetic PLR labels; they do not validate real fault recovery.
The native `CreateMethod`
routine (`0x3b156`) supports a 16-character method name and six-character user name. PLR writes the
explicit run ID into these existing fields and uses `PLR` for the user; no firmware field or
device-side idempotency token is invented.

## Camera transfer and reference crop

The offline `camera` module reconstructs the complete CD payload and the image produced by
`ARPAPI_GetImageData` at RVA `0x2500`. The original USB bytes are retained in `CameraFrame.wire_data`.

| Operation | Captured format |
|---|---|
| Set camera exposure | `CJ   2,  540`: two-byte reply, requested exposure 540 ms; reply `1c 02` |
| Start capture | `CC` |
| Poll readiness | `CH   5`, replying `not r` while busy and `ready` when ready |
| Read image | `CD  64,2458 `, including the final space |

`ARPAPI_SetExposuretime` (ArpLib RVA `0x1900`) accepts a double in seconds. For camera version
2.10 and later, the setter encodes `CJ%4d,%5d`, with response length 2 and
`trunc((seconds + 0.00049) * 1000)`. The x87 arithmetic is at `0x1a01`–`0x1a11`; the constants
at `0xcce8` and `0xcce0` are 0.00049 and 1000. Earlier firmware uses different CE formats and
is not supported by the snapshot API. The native accepted range is 0.005–50 seconds; PLR limits
experimental snapshots to 1.048 seconds, the longest setting in this capture.

All 46 CJ replies echo the requested millisecond value as a little-endian uint16. This confirms
the command setting, not independently measured sensor integration time. The capture contains
1,053 `not r` and 47 `ready` replies. ArpLib's capture thread at RVA `0x1c00` sends CC once,
waits 10 ms between readiness polls, then reads CD. Its abort path contains `Cc`; PLR does not
expose that unvalidated abort operation. All 46 CC-to-CD sequences contain only CH polling,
including the exposures lasting over a second; no idle `C:` occurs inside them. The native
idle routine at RVA `0x1070` checks an activity flag set by camera commands. PLR holds the USB
transaction lock throughout the exposure/read, excluding its idle heartbeat until completion.
`camera_commands.json` retains all exposure echoes and
the complete camera command sequence associated with the retained image fixture (CC frame
12081, CD frame 12235), plus native optical query packets.

The direct Ubuntu `plr-lowexp-001` session also acknowledged 5, 10 and 30 ms requests, eight of
each, alongside two requests each at 60, 180 and 540 ms. All 30 exposures produced complete
images. `ubuntu_short_exposure_vectors.json` retains the six exact CJ command/reply pairs and
their counts; 5 ms returns `05 00`. This confirms command acceptance and retrieval, not an
independent measurement of sensor integration time or exposure linearity.

Snapshot metadata queries use command `0x6b` for the two-byte big-endian filter index, `0x6c`
for the two-byte motor state (`00 00` stationary), and `0x66` for the four-byte lamp reply.
Native wrappers are at BudgieManagerEXT RVAs `0x4072d`, `0x40d7d`, and `0x410b1`. Lamp enable
and power are two big-endian words; raw bytes remain available. The implementation compares
original optical replies before and after capture
without moving the filter or changing the lamp. See [snapshot limits](raw-snapshots.md).

For the 321 × 245 wire dimensions, the reference library requests
`2 × (floor(321 × 245 / 64) + 1)` blocks of 64 bytes: 157,312 bytes. This contains 157,290 bytes of
little-endian uint16 samples and a 22-byte uninterpreted tail. Retain that tail without assuming
zero padding. A captured frame consists of 38 reads of 4,096 bytes and one read of 1,664 bytes;
these are host transfers, not individual USB packets. The assembler accepts arbitrary fragments
but rejects a missing or excess byte.

The default reference copy (OPB=0) produces 320 × 240 pixels. For output row `r` starting at zero,
copy 320 samples starting at wire sample index `321 × (r + 5) − 1`. The one-pixel offset matters:
a simple rectangular crop at wire row 5, column 0 is incorrect. OPB=1 produces 320 × 245 pixels
starting each row at wire sample index `321 × r`. Neither copy mode establishes physical well
orientation or applies calibration.

These copy rules were checked by executing the native function in an isolated x86 emulator with
synthetic ramp buffers, then with every captured camera payload. PLR's default image bytes agree
exactly for all 62 frames (4,761,600 pixels). No external functions or hardware were used by the
emulator. `test_data/camera_crop_vectors.json` retains the synthetic native outputs;
`camera_capture_vector.json` retains one compressed, unmodified CD payload, its actual fragment
lengths, and the native output hash. Full captures remain outside Git.

The completed run contains one initial camera frame followed by three groups of 15 frames. In each
group, filter-position requests 1 through 5 precede three exposures apiece. The latter two groups
precede the cycle 1 and cycle 2 result events. The cancelled run contains the initial frame and
the first 15-frame group, with no completed cycle result. Exposure selection/combination and the
meaning of the initialization images still require tracing; do not average the three images or
assign dye names based only on their order.

## Saved calibration and image evidence

The reference installation stores instrument-specific calibration in
`eclipse/plugins/com.apldbio.sds.instrument.sds7500fast_1.0.0/config/<instrument-serial>/`.
That directory includes `background.ini`, `uniformity.ini`, `puredye.ini`, `peaks.ini`,
`Calibration.sda`, `SystemConfiguration.sdc`, and native `.sds` calibration experiments.
The INI-like files include multiline numeric tables delimited by `<<eof` and `eof`; they are not
ordinary single-line INI settings. Embedded copies in `.eds` files are snapshots and may differ
from the installation's currently selected calibration.

The native module's `bin/Mask/` directory contains five `ROIMask_W96_F*_I809_Commit.aoi` files,
alongside intermediate, silent, and failed masks. Preserve those distinctions rather than choosing
whichever file is newest. The masks use an MFC archive format; their geometry is not yet decoded
by PLR.

Five `AutoROI_F0.tif` through `AutoROI_F4.tif` files in that directory are genuine uncompressed
320 by 240 images, stored as unsigned 16-bit samples. Those saved images establish a reference
file layout, not the USB camera frame layout or its complete processing history. Their filter
indices must not be equated to dye names without validating the calibration mapping.

In contrast, the 1,035 `.tiff` members examined in the supplied experiment archive are each 768
bytes: 96 big-endian signed 64-bit well values. The Java `RawFilterData.convertBuffer` implementation
confirms that encoding. These entries are not TIFF camera images despite their suffix. Their names
encode stage, cycle, collection point, and filter. Preserve the original values; correction status
and physical well orientation require validation against the acquisition and calibration pipeline.

The completed engineering EDS contains ten arrays (two cycles × five filters). All 960 integers
match the corresponding `onupdatespectradataxml` event values exactly, using vendor well indices
1 through 96. The cancelled experiment contains no filter arrays. `read_eds_filter_data` reads
these members without extracting the ZIP, preserving their original bytes, integer precision,
acquisition indices, and vendor ordering. It rejects malformed arrays and duplicate acquisition
indices. This agreement validates the saved-data decoder, not the conversion from pixels to wells.

## Lamp/filter control and uniformity correction

The captured envelopes and native `BudgieManagerEXT.dll` wrappers establish:

| Command | Request payload | Reply | Native RVA |
| --- | --- | --- | --- |
| `0x64` SetLamp | BE16 enable, 0 or 1 | empty acknowledgement | `0x40fbf` |
| `0x65` SetLampPower | BE16 native power 0 or 1 | empty acknowledgement | `0x40eb9` |
| `0x66` GetLampState | empty | BE16 enable, then BE16 power | `0x410b1` |
| `0x67` AsyncHomeFilterWheel | empty | empty acknowledgement; motion is asynchronous | `0x41614` |
| `0x69` MoveToFWPos | BE16 absolute position | empty acknowledgement | `0x40c4a` |
| `0x6b` GetFilterPosition | empty | BE16 position | `0x4072d` |
| `0x6c` RequestFilterMotorState | empty | `0000` stationary, `0001` moving in capture | `0x40d7d` |

`WaitForFilterMove` (`0x421e3`) polls at 100 ms with a 15-second limit. The driver does not
expose `0x76` (calibration position setting), `0x7a` (lamp monitoring/reset), or implicit homing.
Native errors are raised, not converted into synthetic lamp state. TCWrapper `SetLampState`
(RVA `0x1e836`) enables the lamp, waits 250 ms, then selects power 0 or 1. HAMCamera (`0x2064a`)
waits another 250 ms before readback. PLR preserves these delays when a change is required.
The direct optical helper `OpenShutter(true)` (`0x1d35b`) is a no-op; false requests absolute
filter position 0. This is distinct from the collection-end **idle** path: `ReadAsync` (`0x4a5c`)
calls `BudgieManager::OpenShutter(false, true)` (`0xea98`), whose Gemini non-isothermal branch
dispatches through the HAMCamera idle hook (`0x204e3`), optics helper (`0x1d5da`), and TCWrapper
(`0x1ea2b`) to `AsyncHomeFilterWheel` (`0x41614`). Requests at frames 11213 and 15211 of the
vendor acquisition confirm `0x67` after each burst. A timestamp comparison that counts only
`0x69` moves misses this home operation and cannot establish how long that vendor optical path
stayed open. Absolute position 0 and homing must not be treated as interchangeable alignment
operations. PLR's default optional collection parking uses `0x69`, with stationary/position/lamp
readback. Explicit `CollectionPlan.home_after_collections` ordinals instead send `0x67` once,
wait up to 15 seconds for motor completion, then require stationary position 0 and unchanged
lamp words. Original frames 15251–15293 show the motor still busy several seconds after a home
acknowledgement and later stationary. They do not establish exact motion duration or final
position. `filter_home_vectors.json` retains the original acknowledgement and motor replies.
The collection intent records homing before actuation. The native command's retry loop is not
copied: an ambiguous home makes the collection uncertain and cannot be replayed.

Captures include enabled power words 0 and 1; the Ubuntu snapshot observed
disabled state `0000 0002`. Power words are not light measurements. PLR uses query-before-change
setters with final readback and never retries an ambiguous mutation.
`optical_control_vectors.json` retains representative captured command/reply pairs.

`GeminiUniformityPreprocessing.repackageRawDataForPlatform` converts saved signed 64-bit well
values to Java float. Its `applyUniformity` multiplies by
`coefficients[filter][well // 12][well % 12]` using float multiplication. It does not subtract the
background file in this branch. `GeminiUniformityCalibration` reads `[uniformity]` matrices;
`[uniformity_spectra]` is a separate field and must not replace those factors.
The original vendor method and PLR agree bit-for-bit on all 960 values from `acquire-complete.eds`,
using its embedded calibration (SHA-256
`ae3a560ea0aadf9b08060ceac4584f865b603df046bbe2faebf1974eac7b86d0`).
The EDS SHA-256 is `4e5804a8bcf8902f3bf2bafce504e7a4e5ee827cc7634d08793da61ae5652faf`.
`uniformity_vectors.json` contains synthetic coefficients and another 480 original-Java outputs,
including int64 rounding boundaries; full calibration backups remain outside Git.

Offline mask recovery identifies `PlateMask` schema 10, `CMNImageAOI` schema 2, and
`CMNImageMatrix` schema 1. Each of five committed `.aoi` files contains 96 serialized pixel masks, and all five occur
byte-for-byte in the backed-up `Calibration.sda`. This is not a live configuration query.
`ExtractObjects` (`0xbb28`) delegates to `0x2bc3a`. The recovered reference path is described below;
other optional reduction, dark-reference and hot-pixel configurations are not inferred from it.
The saved-array uniformity API and the camera reconstruction API preserve different source objects.
See [measurement limits](optics-and-measurements.md).

The offline `image_processing` module supports diagnostic extraction from those saved masks.
The MFC stream uses 16-bit class tags, schemas 10/2/1 and a 96-object list. AOI outlines contain
signed row/column pairs; the nonzero uint16 matrix entries select pixels relative to the outline's
minimum row/column. Native first/next iterators at RVAs `0x95a20`/`0x95a50` select these pixels in
row-major order. Original files and uninterpreted statistics/footer bytes are retained.

HAMCamera's correction at BudgieManagerEXT RVA `0x200c7` consumes the OPB=1 320×245 image. It
truncates the mean of the first 640 pixels, removes five rows, and stores
`max(0, pixel - offset + 128)` in uint16 (including native wrap on overflow). OPB=1 has a different
one-pixel horizontal origin from the default OPB=0 view. These must not be treated as the same crop.
Isolated original x86 execution matched 48 saved frames and six arithmetic boundary cases.
Diagnostic logging was stubbed and the MSVCRT integer conversion modeled using x87 truncation.
All 480 decoded mask regions also matched native iterator execution using reconstructed AOI
memory. These checks establish individual stages, not MFC-deserializer or end-to-end fluorescence
parity. Diagnostic sums include zero pixels and do not implement vendor reduction settings.

## Reference camera-to-vendor reconstruction

The matching native `HS7500.dll` has SHA-256
`97fedadf13db8668c805c91df7a20d1197d569a01d24be8d687cd46c1751c983`.
BudgieManagerEXT has SHA-256
`9dbff7366d38a6c43b3ba363dd007b40f65d0dead3c1d3b9eb09428f91a8102f`.
Recovery of their called routines and comparison with the original acquisition establish:

| Stage | Native evidence | Implemented reference behavior |
|---|---|---|
| Mask orientation | Budgie wrapper `0x17eca`, HAMCamera `0x2035b` | Rotate the OPB=0 320×240 crop 180° before masks |
| Well extraction | Budgie `0x74627`, ROI statistics `0x7e2e8` | Sum nonzero image pixels selected by each native mask |
| Surrounding pixels | Budgie `0x7467b`, dilation `0x8e8f9`/`0x8ea83` | Adjacent eight-neighbor ring, excluding the ROI; subtract nonzero ring mean × nonzero ROI count |
| Saturation | Native threshold/count path | Flag more than six ROI pixels above 4000 |
| Exposure/background | HS7500 `0x17552`, `0x3d190`, `0x1d390` | Offset subtraction; scale to 1000 ms using `ceil((ms+0.2746)/0.1702)*0.1702`; subtract slope × 1000; truncate/clip to uint32 |
| Exposure selection | HS7500 `0x4de24` | Prefer uniquely longest unflagged exposure; fallback to uniquely shortest original exposure, then first on tie |
| Saved filter order | HS7500 `0x1bb85` | Reverse physical filters: saved index = 6 − physical position |

The exposure selector also preserves the sole-positive-exposure case, even when flagged.
Eighty isolated native cases exercise unequal/equal times, disabled exposures, every flag
combination and all five filter-bit positions. Logging and allocator dependencies were isolated;
this oracle does not communicate with hardware.

`Background.csv` version 5 stores five offset rows then five slope rows in saved-filter order.
The exact backup SHA-256 is
`0de05b98e4d19a870ca947c3369140de040897c30afaa3355da2350006db4e48`.
Its coefficients are parsed directly to binary32. The lower-precision EDS `background.ini` misses
ten saved integers and is not substituted for this file. Calibration files remain outside Git.
The reference configuration uses OPB=0 and no additional dark lookup. A separately verified OPB=1
correction must not be inserted into this path.

The 30 frames associated with stage 2, cycles 1–2 reconstruct **960/960 saved EDS integers exactly**.
Applying the vendor-verified uniformity stage also matches all 960 corrected binary32 values.
Native F0–F4 mask files map to physical filters 1–5 and hence saved filters 5–1. These tests verify
the orientation and indices relative to vendor outputs; they are not an independent labeled-plate
orientation experiment. Small recorded patches cover all filters and both cycles in
`fluorescence_vectors.json`; original frames and full calibration files remain outside Git.
Many reference wells select a saturated fallback. Matching those numbers does not make them valid
quantitative measurements; the API retains their saturation flags and all three candidates.

A separate {ref}`known-pattern plate session <abi7500-wellmap-validation>` verified the physical
orientation and labels A1, A4, C9, F3, H7 and H12 in filter 5. All six fluorescein wells matched
the six brightest regions in three exposures, without changing the processing or calibration.
This supplements the vendor-output comparison; it does not verify every well or channel.

The [hardware report](hardware-validation.md) records the illumination session with the native
lamp delays and five-second settling intervals. It produced visible well patterns at filters 1
and 5, unlike the earlier immediate snapshot. The session does not isolate which delay resolved
the earlier dark image or establish quantitative calibration.

## Stored exposure-table readback

`TC9700Control::ReadExposureTimes` (BudgieManagerEXT RVA `0x447f4`) sends command `0x80`
with no payload and expects 254 response bytes. The table starts at offset 2 (`0x46220`);
element addressing is `2 + physical_filter_index * 12 + exposure_index * 4` (`0x46196`),
with both native indices starting at zero.
The native accessor reverses each four-byte value into a 32-bit Windows `long` (`0x45390`,
`0x45b00`), establishing signed big-endian milliseconds on the wire. The leading two bytes and trailing
192 bytes remain opaque. PLR preserves them and returns durations in seconds without sorting.

Two complete USB exchanges in `stored_exposure_vectors.json` retain the vendor's original
fragmentation, byte-count prefixes and padding. Their table matches the following CJ commands
for both completed collection points in `abi7500-acquire-001`:

| Physical filter | Exposure slots in recorded order (ms) |
|---|---|
| 1 | 540, 180, 60 |
| 2 | 540, 180, 60 |
| 3 | 900, 300, 100 |
| 4 | 540, 255, 85 |
| 5 | 1048, 90, 30 |

`get_stored_exposure_times()` exposes only this readback. The native table-writing path
(`WriteExposureTimes`, RVA `0x449a2`, command `0x7f`) is not implemented. Querying a stored
table does not establish calibration validity or authorize using its durations for a new run.
Two {ref}`direct Ubuntu readbacks on G2.10 <abi7500-exptable-validation>` matched the complete
vendor table. `ubuntu_stored_exposure_vectors.json` retains one full query with all 142 counted
serial reads, including zero-count replies, one-byte fragments and stale padding. The USB
operations are losslessly compressed; offline replay checks their exact order and bytes.

## Status-driven optical scheduling

The native Windows implementation schedules optical reads on the host while thermal timing stays
on the controller. `BudgieManager::EstimateReadTime_PCR` (Budgie RVA `0x120bb`) benchmarks a complete
configured read. HS7500 preparation (`0x118ae`) checks collection holds against that budget plus a
margin. `Poll9700` (`0x143bd`) starts a read when the hold countdown in milliseconds is at most the
estimated duration plus margin, suppressing duplicates by group/step/repetition. Budgie
`ReadAsync` (`0x4a5c`) compares the thermal indices before and after the burst.

The matching reference log gives a measured read budget of **22,593 ms** plus **2,000 ms** margin.
Both captured collections begin with 24 seconds remaining and end with 1 second remaining, at
zero-based group 1/step 1, repetitions 1 and 2. Each contains physical filters 1–5 with three
exposures each. `collection_schedule_vectors.json` retains the original boundary exchanges and
frame associations. There is no recovered hardware-trigger or hold-extension handshake in this
path. Command `0x67` is filter-wheel homing, not an acquisition trigger. PLR invokes it only for
explicitly selected collection-end ordinals and verifies completion before accepting that point.

These entry points are in the installer/reference-PC binaries already used for protocol recovery.
HS7500 SHA-256 is `97fedadf13db8668c805c91df7a20d1197d569a01d24be8d687cd46c1751c983`;
the Budgie hash is listed above. Full decompilation and binaries remain outside Git.

PLR's [collection API](synchronized-acquisition.md) uses a configurable 45-second default window
because per-frame status and optical checks add overhead. It journals the full collection intent
and each CJ/CC intent, checks the same run and hold around every image, and retains uncertain data
without replay. This is a proposed scheduling budget, not a proven worst-case acquisition time.
An incoherent observation spanning startup is reconciled by further queries without actuation.
The first Ubuntu attempt ended before exposure. The second acquired 15 frames in each of two
holds, starting at 44 seconds remaining and ending at 14/13 seconds. A later host observation
timeout cancelled the final hold. Those captured phase associations and the startup regression
are documented in the [hardware report](hardware-validation.md). A subsequent approved session
with a 900-second observation budget completed both cycles normally. Its collections began at
44/45 seconds remaining and finished at 14/13 seconds; all 30 frame boundaries remained in the
intended 30 °C holds. Recorded metadata and final observations are retained in
`ubuntu_sync_completed_vectors.json`.

## Remaining control and measurement validation

- Validate broader thermal programs and cold-cover startup with the longer observation budget.
  The completed two-cycle optical run began with a warm cover; it does not establish a worst-case
  acquisition time or a universal session timeout.
- Validate additional startup states, teardown and recovery after ambiguous communication failures
  without repeating a potentially completed run or exposure.
- Compare additional acquisitions, including unsaturated measurements, other configurations and
  calibration backups. The exact reference comparison does not cover every native processing mode.
- Extend physical-label validation beyond the six known-pattern wells tested in filter 5;
  validate dye/channel interpretation separately. Saved filter indices alone do not identify
  dyes or establish current calibration validity.

Mocked/captured tests establish framing, image reconstruction, processing and run-state behavior.
The separate hardware report records direct Ubuntu connection, one start/cancel, clean reconnect
during startup, one completed 35 °C/60-second hold, illuminated snapshots and 30 images collected
within two scheduled holds followed by normal thermal completion. Broader thermal programs and
fault recovery remain unverified.
