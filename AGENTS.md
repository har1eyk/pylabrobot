# Agent guide

## Scope and safety

Make the requested change and nothing else. Avoid unrelated rewrites,
documentation, or configuration work.

**Never drive real hardware without explicit, per-run approval.** Approval
from a previous run does not carry over. This applies to PLR and vendor control.

## Required reading and validation

Before changing files, read the [contribution guide][contributing]. Also read:

- [Device driver guide][drivers] for new drivers or hello-world notebooks.
- [Device registry guide][registry] for registry entries, tables, or cards.

These guides are the source of truth for implementation and validation. Update
those sources instead of duplicating their rules here. Add tests when they cover
behavior that could regress; do not add tests for every request by default.

Keep this file actionable, wrap lines at 100 columns, and use reference links for
long paths. Keep detailed recipes, study histories, and inventories in their
linked sources.

## ABI 7500 Fast Dx objective

This chat and its research folders/scripts support a native ABI 7500 Fast Dx
library in PLR. The end objective is to run a qPCR plate from Ubuntu and interpret
its data comparably to native ABI software: thermal programs, synchronized
acquisition, calibration, per-well fluorescence, and validated quantification.
Windows runs provide protocol evidence and comparison data toward that objective.

The integration remains experimental. Ct and assay interpretation are not
implemented. Read the [user guide][guide] and [hardware evidence][hardware] before
claiming validation. Use `pylabrobot.io.USB` for native communication; vendor DLLs
must not become driver dependencies.

### Windows control through Remmina

The existing console route uses `/usr/bin/python3`, GTK/AT-SPI and X11/XTest to
control Remmina. A separate temporary PowerShell bridge runs Windows jobs and
transfers files; it does not provide screenshots or mouse control.

- [UI controller][ui]: screenshots, clicks, keys, typing and clipboard paste.
- [Windows bridge][bridge] and [bridge template][bridge-template]: bounded job
  execution and file transfer from the Windows console.
- [Log observer][observer]: vendor status snapshots without direct USB access.
- [Reference jobs][jobs]: inventory, backups, capture and result preservation.
- [HEX comparison preparation][hex-plan]: prepared comparison procedure.
- [Verified HEX connection][hex-connection]: X11 recovery, current session details
  and screenshot evidence. Rediscover process/window IDs after a restart.
- [Earlier UI helpers][ui-tools]: historical controllers and bridge variants.

Check these tools before concluding that Windows access is unavailable. Use a
mechanism permitted by current tool instructions and user authorization.
Historical helpers contain stale process/window IDs, dimensions, LAN addresses
and output paths. Copy a helper to a fresh session workspace; discover current
values and verify a screenshot before issuing actions. Preserve the originals.

Before a vendor capture, verify USB ownership, back up configuration/masks, and
start USBPcap before v2.3. Preserve actions, full payloads, logs, experiments,
native outputs and calibration changes. The observed executable path is
`C:\Applied Biosystems\7500\bin\7500.exe`. EZ-Tap Pro cannot tap this USB connection.
Reconcile interrupted runs before another start; do not automatically repeat.

### Evidence and outputs

- [Research workspace][research]: `captures/<session>/` holds working evidence,
  `analyses/<study>/` holds studies, and `plans/<session>/` holds preparation.
  Not all local research has been packaged in Seafile.
- [Seafile artifact catalog][catalog]: categorized files and study archives.
- [Images and figures][figures]: exact archive members and available local views.
- [Complete inventory][inventory]: loose files, ZIP members and metadata.

Keep installers, vendor binaries, full captures, calibration backups and VM disks
outside Git. Retain protocol documentation and small regression fixtures in Git.
Preserve Seafile duplicates/conflict copies until compared. Catalogs describe
local snapshots, not verified remote synchronization; update them for new bundles.

Before interpreting results, read the session summary, audit and provenance.
Record instrument/camera identity, plate contents/orientation, exposure,
temperature, masks/background and processing version. Preserve raw frames and
intermediate values. Distinguish same-frame processing agreement from agreement
between acquisitions. Keep native diagnostics separate from PLR acceptance gates;
do not discard failed wells, substitute dyes or change thresholds to obtain a pass.

### Calibration notebook

Read `Exp1015--Calibrating 7500 Fast Dx Real-Time PCR.md` in the
[Harley calibration notebook folder][notebook] before proposing a plate. Strip
embedded image data URLs when extracting text. Prefer this copy over duplicates.

Use its final **CALIBRATION INSTRUCTIONS** for the recipe reference: ROI →
background → optical → separate dye plates. The overview places RNase P
verification afterward. The mixed ROI plate is reused for optical calibration.
Earlier entries contain alternative orders, failed trials and revised recipes.

Confirm actual stock concentrations and final total volumes. The final
fluorescein recipe is 80 nM; earlier entries and some project runs use 160 nM.
Preserve the actual preparation for each run. New explicit user preparation
information takes precedence over historical recipes. Keep physical 6-HEX identity
separate from the notebook's VIC label; using fluorescein under FAM/SYBR labels
does not establish chemical equivalence or assay validity. Historical accepted
warnings are not evidence of current calibration validity.

[contributing]: docs/contributor_guide/contributing.md
[drivers]: docs/contributor_guide/device-driver-guide.md
[registry]: docs/contributor_guide/device-registry.md
[guide]: docs/user_guide/thermo_fisher/abi7500_fast_dx/index.md
[hardware]: docs/user_guide/thermo_fisher/abi7500_fast_dx/hardware-validation.md
[research]: ../abi7500-research/
[ui]: ../abi7500-research/captures/abi7500-unit2-dye-hex-vendor-001/ui.py
[bridge]: ../abi7500-research/captures/abi7500-unit2-dye-hex-vendor-001/bridge.py
[bridge-template]: ../abi7500-research/tools/ui/windows_unit2_rox_compare_bridge.py
[observer]: ../abi7500-research/captures/abi7500-unit2-dye-hex-vendor-001/observe.py
[jobs]: ../abi7500-research/captures/abi7500-unit2-dye-hex-vendor-001/jobs/
[hex-plan]: ../abi7500-research/plans/abi7500-unit2-dye-hex-vendor-001/READ-ME-FIRST.txt
[hex-connection]: ../abi7500-research/captures/abi7500-unit2-dye-hex-vendor-001/CONNECTION-README.md
[ui-tools]: ../abi7500-research/tools/ui/
[catalog]: ../../../Seafile/Temp/ABI-7500/project-index/index.md
[figures]: ../../../Seafile/Temp/ABI-7500/project-index/figures.md
[inventory]: ../../../Seafile/Temp/ABI-7500/project-index/artifact-inventory.json
[notebook]: ../../../Seafile/Temp/ABI-7500/Harley-previous-7500-calibration/
