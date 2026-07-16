# Add ASUS driver downloader (SCCM-style pack + BIOS mode)

## Summary

Adds `asus-driver-downloader/`, an ASUS counterpart to the Lenovo tool. Because
ASUS does not publish consolidated SCCM driver packs for consumer laptops, this
tool pulls the model's driver feed, downloads each package, extracts the raw
`.inf`/`.sys`/`.cat` trees, and assembles an injectable pack under
`SCCM/<model>/` — giving the same `pnputil` / DISM deployment workflow.

Validated end-to-end against the ASUS Zenbook S14 **UX5406SA** (Intel Lunar Lake,
Windows 11 24H2): all 23 downloadable driver packages extract (117 `.inf`).

## What's included

- **Model-based lookup** via ASUS's `GetPDDrivers` support API (osid 52 = Win11 64-bit;
  full SKUs like `UX5406SA-S14.U732G1T` are normalized to `UX5406SA`).
- **`--sccm`** builds the injectable driver pack (download + extract to `.inf` trees).
- **Recursive extraction** — Inno Setup via innoextract, plus a fallback that carves
  the 7-Zip payload embedded in ASUS "Business Intelligence" installers (Ethernet,
  Serial IO, TXT, chipset, camera, Dolby, etc.).
- **Microsoft Store items skipped** (Intel Graphics Software, ICPS, Realtek/Cirrus
  consoles) — not injectable; listed so they can be installed from the Store post-setup.
- **Case-insensitive `.inf`** handling (ASUS ships some as `.INF`).
- **`--bios`** downloads the latest BIOS via `GetPDBIOS`: EZ Flash `.zip` (auto-extracted
  capsule for USB), the Windows `.exe` updater, and SSD firmware, with flashing guidance.
- Other modes: `--list`, `--list-bios`, `--info`, `--drivers-only`, `-c` category filter,
  `--all-versions`, `--osid`, `-w` workers.

## Testing

- Offline unit tests: feed parsing, latest-version dedupe, driver/app/Store filtering,
  model normalization, osid aliases, embedded-payload (nested-archive) extraction,
  BIOS parsing/dedupe/zip-capsule extraction.
- Live extraction verified against all downloaded UX5406SA packages (117 `.inf`).

## Notes

- Requires `innoextract` on macOS/Linux (`brew install innoextract`); `py7zr` added to
  `requirements.txt` as a portable 7-Zip fallback.
- ASUS consumer laptops generally do not receive BIOS via Windows Update; EZ Flash before
  imaging is recommended.
