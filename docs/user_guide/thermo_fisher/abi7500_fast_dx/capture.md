# Reference PC and USB captures

Use the original working control PC first. Inventorying and copying files can happen without
opening the instrument. Every real-instrument session requires explicit approval for that session
under the repository's `AGENTS.md`, including a connection-only session.

## Preserve the working reference

Record the Windows version, architecture and service pack, installed 7500/SDS software version,
USB driver version, and instrument model/firmware from existing records. Preserve screenshots or
existing reports showing which software works with the Fast Dx model. Do not upgrade the working
installation to v2.3 merely to match the downloaded installer.

Close the vendor application after any existing run has finished. Copy its installation directory,
configuration, calibration/ROI files, logs, and example experiment/data directories to a fresh
backup outside Git. Include data stored outside the installation directory; installation files
alone are not a calibration backup. Record original paths, timestamps, file sizes, and SHA-256
hashes, and verify copied hashes. Record relevant application settings from the working PC without
changing them. Preserve the original files and inspect copies.

The inspected v2.3 installer uses `C:\Applied Biosystems\7500`; its native logging configuration
references `config\logs\geminiadapter.log`. These are search starting points, not verified paths
for a Fast Dx installation. Files being modified or locked during copying need to be recollected
after the application is closed. Do not claim the backup is complete while errors remain.

Also preserve `config\logs\shell.log` and `response.log`. A reference v2.3 installation logs thermal
request/reply hex bytes in `shell.log`, while `response.log` records application operations and
status events. The [protocol notes](protocol.md) identify instrument-specific calibration and
camera-image locations inside the plugin directories; these are missed by copying `Experiments`
alone.

## Capture separate, approved sessions

Follow [Wireshark's USB capture guidance](https://wiki.wireshark.org/CaptureSetup/USB) and the
[USBPcap tour](https://desowin.org/usbpcap/tour.html). Check capture-tool support for the original
PC's OS before installing anything. If it requires a reboot, finish and preserve any current run
first. Keep the original working vendor software and USB driver.

With Remmina, leave the instrument connected to the Windows PC and capture on Windows. Remote
desktop traffic on Ubuntu does not contain the instrument's local USB transfers. USBPcap must be
installed separately if the Wireshark installer does not offer it. If USBPcap interfaces are absent,
follow its extcap setup instructions rather than selecting an Ethernet or Wi-Fi interface.

Check the saved `auto-connect` preference before planning startup: the reference installation has
it enabled. Start USB capture before opening the vendor application during the approved session.
Treat startup as instrument activity, not passive file viewing. Functional tests are a separate
operation and can move the filter wheel and operate the lamp and shutter; do not add them to a
connection-only session without explicit approval.

The observed normal startup itself homes/moves the filter wheel and configures the lamp. Include
those initialization actions in the connection session's scope. Record login and expired/invalid
calibration dialogs and the operator's choices. Continuing startup does not make a calibration
valid; preserve the warning when interpreting later measurements.

In Wireshark, identify the USBPcap interface for the instrument's root hub using its device list.
Select the Hamamatsu device and disable capture from all already-connected devices, especially if
a network adapter shares that hub. Keep capture from newly connected devices enabled to retain
any re-enumeration during the approved session.
Open that interface's options and enable descriptor injection for already connected devices;
this avoids unplugging the instrument just to obtain descriptors. Injected descriptors are
capture metadata, not evidence of a new USB enumeration. Capture full payloads, not just packet
summaries. USBPcap's snapshot length defaults to 65,535 bytes and rejects zero; start with a
1,048,576-byte snapshot and a 16,777,216-byte capture buffer, then verify that captured packet
lengths equal original lengths. Increase the snapshot if any transfer is truncated. These options
are documented in the [USBPcap capture source](https://github.com/desowin/usbpcap/blob/master/USBPcapCMD/cmd.c).
Start capturing before the approved application startup to record initialization.
Preserve the complete capture even if display filters are used for inspection. USB software
captures contain host requests and completions; a submitted OUT payload must not be counted again
from its completion record.

Use separate capture files for:

1. **Connection and identification:** approved connection, discovery, and idle-status observation;
   no experiment start. Record every operator action and any dialog exactly.
2. **Acquisition:** a separately approved, operator-specified protocol with saved vendor experiment,
   logs, raw images where available, and exported fluorescence from that same acquisition.
3. **Cancellation:** a separately approved run and deliberate cancellation, with state before and
   after cancellation and the matching experiment/logs.

For every session retain a UTC action log, OS/software/driver versions, instrument identity and
firmware, capture interface, capture drop/truncation statistics, experiment settings, and hashes of
associated files. Annotate uncertain timestamps instead of inventing precision. A session offering
a firmware update or recalibration must stop before accepting that action. Power-loss and cable-
disconnect experiments require their own approval; do not simulate them during a valuable run.

Only extract small, reviewed binary fixtures into the repository. Keep full captures and all
calibration and experiment data outside Git. Thermal packets, captured camera transfers, and saved
EDS well arrays can be inspected with the [offline notebook](hello-world.ipynb). Camera decoding
preserves the original bytes and reference crop; it does not yet derive calibrated well values.

## Windows VM fallback

Use the VM if the original PC cannot provide suitable captures. Allocate 2 vCPUs, 4 GiB RAM, and a
64 GiB virtual disk under QEMU/KVM on Ubuntu. Image the existing Windows installation USB to a
regular file after positively identifying its device path; use the stick only as a read-only
source. A bootable Windows installation stick is not necessarily an ISO. Attach its image as a
read-only virtual USB disk and keep the separate VM disk writable. Preserve the original stick.

Install Windows 10 x64 and the appropriate vendor software without instrument passthrough. Keep
the VM isolated from the network when network access is unnecessary. Verify Fast Dx compatibility
before a separately approved instrument connection. Pass through only the identified Hamamatsu
device, never unrelated USB or serial devices. Preserve a VM snapshot before adding instrument
drivers or changing software versions.

For an approved passthrough session, use Ubuntu `usbmon` with Wireshark/dumpcap on the instrument's
USB bus and full snapshot length. This can capture the host-side transfers even while Windows owns
the device through QEMU. Re-enumeration may change the device address, so retain bus-level captures
and descriptor events. Establish whether startup uses USB control transfers as well as bulk I/O.

The eventual acceptance run must use Ubuntu PLR directly with Windows disconnected, with explicit
per-run approval, and retrieve the corresponding frames and measurements. A successful Windows
run or passing offline tests does not satisfy that hardware validation.
