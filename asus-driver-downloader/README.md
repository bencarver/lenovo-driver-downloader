# ASUS Driver Downloader

A command-line tool to download all driver files for an ASUS device by **model
name**, and to assemble a Lenovo-SCCM-style injectable driver pack for clean
Windows installs. Built to mirror the `lenovo-driver-downloader` workflow.

## How this differs from the Lenovo tool

Lenovo publishes prebuilt **SCCM driver packs** (single `.exe`) for ThinkPad /
ThinkCentre business models. **ASUS does not** ship a consolidated pack for
consumer laptops (Zenbook / Vivobook / most models). Instead ASUS lists the
individual driver installers per model.

So this tool:

1. Pulls the model's driver feed from ASUS's support API.
2. Downloads each driver package (from `dlcdnets.asus.com`).
3. With `--sccm`, extracts the raw `.inf` / `.sys` / `.cat` driver trees and
   lays them out under `SCCM/<model>/<Category>/` — i.e. it **builds the pack
   for you**, giving the same `pnputil` / `DISM` end-workflow as the Lenovo tool.

Two other differences: ASUS drivers are keyed by **model name** (e.g.
`UX5406SA`), not a serial number; and the target OS is selected with an ASUS
**`osid`** (Windows 11 64-bit = `52`).

## Installation

```bash
pip install -r requirements.txt
```

For SCCM `.inf` extraction on **macOS/Linux**, `innoextract` is **required** —
every ASUS driver package for this model is an Inno Setup 6.x self-extractor, and
`innoextract` is the only tool that reliably unpacks them (7-Zip alone can read
the big packages but not the small ones, which wrap a nested Inno payload):

```bash
brew install innoextract           # REQUIRED on macOS
brew install sevenzip cabextract unshield   # recommended extras/fallbacks
```

Without `innoextract`, smaller driver packs fail to extract with
"Could not unpack to .inf files." If you saw that, install it and re-run.

Some ASUS packages ("Business Intelligence" installers — e.g. Ethernet, Serial
IO, TXT, chipset, camera, Dolby) carry the actual driver as a 7-Zip payload
appended to the installer. The tool carves and unpacks these automatically using
system 7-Zip if present, otherwise the bundled `py7zr` fallback (installed via
`requirements.txt`). Uppercase `.INF` files are handled too.

On **Windows** no extra tools are needed — packages run their own silent
self-extractor (the tool passes ASUS's `/VERYSILENT /SUPPRESSMSGBOXES
/NORESTART` and `/DIR=`). Installing `innoextract` on Windows too is still the
cleanest option.

### Microsoft Store items are skipped

A few feed entries (e.g. Intel Connectivity Performance Suite, Intel Graphics
Software, Realtek/Cirrus audio consoles) are delivered via the **Microsoft
Store**, not as downloadable installers. The tool detects and skips these — they
can't be injected — and lists them so you can install them from the Store after
Windows setup.

## Usage

Your machine (from the Best Buy SKU `UX5406SA-S14.U732G1T`) is the **Zenbook S14,
Intel Core Ultra 7 258V (Lunar Lake), Windows 11 24H2+**. The support model is
`UX5406SA`.

### List what's available

```bash
python asus_driver_downloader.py UX5406SA --list
```

### Build the injectable driver pack (recommended for clean installs)

```bash
python asus_driver_downloader.py UX5406SA --sccm
```

Downloads the hardware drivers and extracts them to
`drivers_UX5406SA/SCCM/UX5406SA/<Category>/`. Apps/utilities (MyASUS, StoryCube,
etc.) are excluded by default — add `--include-apps` to keep them.

Download the packs without extracting:

```bash
python asus_driver_downloader.py UX5406SA --sccm --no-extract
```

### BIOS / firmware

```bash
python asus_driver_downloader.py UX5406SA --list-bios   # show available versions
python asus_driver_downloader.py UX5406SA --bios        # download latest BIOS + firmware
```

`--bios` downloads, into `drivers_UX5406SA/BIOS/`, the latest of each: the **EZ
Flash `.zip`** (auto-extracted to `BIOS/EZ_Flash/` — the raw capsule to copy to
a FAT32 USB), the **Windows `.exe`** updater, and any **SSD/firmware** update. It
then prints flashing instructions.

Flashing before you image the machine is the clean approach: copy the capsule
from `BIOS/EZ_Flash/` to a FAT32 USB, boot into UEFI (tap **F2**), open **MyASUS
in UEFI → EZ Flash 3**, pick the file, and keep the charger connected. ASUS
consumer laptops generally do **not** receive BIOS updates via Windows Update, so
don't rely on that. Never interrupt a BIOS flash.

### Download everything by category (Lenovo-tool default behavior)

```bash
python asus_driver_downloader.py UX5406SA
python asus_driver_downloader.py UX5406SA --drivers-only     # skip apps
python asus_driver_downloader.py UX5406SA -c Audio Bluetooth # specific categories
```

### Other options

```bash
python asus_driver_downloader.py UX5406SA --info          # resolved model + feed summary
python asus_driver_downloader.py UX5406SA --osid win10    # target Windows 10 64-bit (osid 48)
python asus_driver_downloader.py UX5406SA --all-versions  # keep every version, not just latest
python asus_driver_downloader.py UX5406SA -w 8            # 8 parallel downloads
```

The full SKU works too — `UX5406SA-S14.U732G1T` is normalized to `UX5406SA`.

## Output

### `--sccm` (injectable pack)

```
drivers_UX5406SA/
├── driver_manifest.json
└── SCCM/
    └── UX5406SA/
        ├── _packages/                 # original downloaded .exe installers
        ├── Chipset/
        │   └── <driver>/ *.inf *.sys *.cat
        ├── Audio/
        ├── Bluetooth/
        ├── Networking/
        └── ...
```

### Default (all drivers by category)

```
drivers_UX5406SA/
├── driver_manifest.json
├── Audio/
├── Bluetooth/
├── Chipset/
└── ...
```

## Using the pack for a clean Windows install

### During Windows OOBE (USB method)

1. Copy `SCCM/UX5406SA/` to a USB drive.
2. At the OOBE network screen press `Shift + F10`.
3. Run:

   ```cmd
   pnputil /add-driver X:\SCCM\UX5406SA\*.inf /subdirs /install
   ```

### Inject into a Windows image (DISM)

```cmd
DISM /Image:C:\Mount /Add-Driver /Driver:C:\SCCM\UX5406SA /Recurse
```

### MDT / SCCM

Import `SCCM/UX5406SA/` into your deployment share's Out-of-Box Drivers.

## Notes

- Get the WLAN/Ethernet driver on first, so networking works during setup — the
  Intel WLAN and Realtek Ethernet packages are under `Networking/`.
- The tool keeps only the newest version of each driver by default (`--all-versions`
  to override) and records SHA-256 + hardware IDs in `driver_manifest.json`.
- Uses ASUS's public support API (`GetPDDrivers`, the same endpoint the ASUS
  "Driver & Utility" pages call). If ASUS changes the feed, verify with `--info`.
- OS ids: `win11` = `52` (verified for this model), `win10` = `48`. You can pass
  a raw numeric `--osid` if a model uses a different value.
```
