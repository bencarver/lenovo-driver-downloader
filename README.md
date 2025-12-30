# Lenovo Driver Downloader

A command-line tool to download all driver files for a Lenovo device using its serial number.

## Installation

```bash
pip install -r requirements.txt
```

For SCCM package extraction, install 7-Zip:
- **macOS**: `brew install sevenzip`
- **Windows**: Download from [7-zip.org](https://7-zip.org)

## Usage

### Download all drivers for a device

```bash
python lenovo_driver_downloader.py YOUR_SERIAL_NUMBER
```

### Specify output directory

```bash
python lenovo_driver_downloader.py YOUR_SERIAL_NUMBER -o ./my_drivers
```

### Download only specific categories

```bash
python lenovo_driver_downloader.py YOUR_SERIAL_NUMBER -c BIOS Audio Chipset
```

### Download SCCM Driver Packs (for deployment/OOBE)

Download and auto-extract SCCM packages containing `.inf` drivers:

```bash
python lenovo_driver_downloader.py YOUR_SERIAL_NUMBER --sccm
```

Download without extracting:

```bash
python lenovo_driver_downloader.py YOUR_SERIAL_NUMBER --sccm --no-extract
```

### List available driver categories

```bash
python lenovo_driver_downloader.py YOUR_SERIAL_NUMBER --list
```

### Show product information only

```bash
python lenovo_driver_downloader.py YOUR_SERIAL_NUMBER --info
```

### Control parallel downloads

```bash
python lenovo_driver_downloader.py YOUR_SERIAL_NUMBER -w 8  # Use 8 parallel downloads
```

## Finding Your Serial Number

The serial number can typically be found:
- On a sticker on the bottom or back of the laptop
- In BIOS/UEFI settings
- Using PowerShell: `Get-CimInstance win32_bios | select SerialNumber`
- Using Lenovo Vantage software

## Output

Downloaded drivers are saved to a directory named `drivers_<SERIAL_NUMBER>` by default, organized by category:

```
drivers_ABC12345/
├── driver_manifest.json   # Metadata about all drivers
├── BIOS/
│   └── bios_update.exe
├── Audio/
│   └── audio_driver.exe
├── Chipset/
│   └── chipset_driver.exe
└── ...
```

### SCCM Package Output

When using `--sccm`, packages are downloaded and extracted to an SCCM folder:

```
drivers_ABC12345/
└── SCCM/
    ├── tc_model_w11_24_202501.exe      # Original package
    └── tc_model_w11_24_202501/         # Extracted drivers
        ├── Audio/
        │   └── *.inf, *.sys, *.cat
        ├── Chipset/
        ├── Network/
        └── ...
```

## Using SCCM Drivers for Deployment

### During Windows OOBE (USB method)

1. Copy extracted driver folder to USB
2. At "Let's connect you to a network" screen, press `Shift + F10`
3. Run: `pnputil /add-driver E:\SCCM\tc_model\*.inf /subdirs`

### Inject into Windows Image (DISM)

```cmd
DISM /Image:C:\Mount /Add-Driver /Driver:C:\Drivers\SCCM\tc_model /Recurse
```

### MDT/SCCM Deployment

Import the extracted driver folder into your deployment share's Out-of-Box Drivers.

## Notes

- The tool uses Lenovo's public support APIs
- Large driver files are streamed to avoid memory issues
- Already downloaded files are skipped on re-run
- A manifest file is saved with metadata about all available drivers
- SCCM packages are available for ThinkPad/ThinkCentre business models

