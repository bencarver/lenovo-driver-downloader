#!/usr/bin/env python3
"""
ASUS Driver Downloader
Downloads all drivers for an ASUS device by model name and (optionally) assembles
a Lenovo-SCCM-style injectable driver pack for clean Windows installs.

Unlike Lenovo ThinkPads, ASUS does not publish a single consolidated "SCCM
driver pack" for consumer laptops. Instead the ASUS support feed lists the
individual driver installers for the model. This tool pulls that feed, downloads
the packages, extracts the raw .inf/.sys/.cat driver trees, and lays them out
under SCCM/<model>/<Category>/ so they can be injected during OOBE via
`pnputil /add-driver *.inf /subdirs` or into an image via DISM /Add-Driver.
"""

import argparse
import json
import mmap
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import unquote

import requests
from tqdm import tqdm


# ASUS operating-system ids used by the support webapi (osid).
# 52 (Windows 11 64-bit) is verified against the UX5406SA feed. Others are the
# commonly observed values; pass --osid explicitly if a model uses a different one.
OS_IDS = {
    "win11": "52",
    "windows11": "52",
    "11": "52",
    "win10": "48",
    "windows10": "48",
    "10": "48",
}

# Category names / title keywords that are applications or tools rather than
# injectable hardware drivers. Excluded from the SCCM/driver-injection pack.
APP_CATEGORY_KEYWORDS = {
    "os assistant", "utilities", "utility", "software", "app", "application",
}
APP_TITLE_KEYWORDS = [
    "myasus", "armoury", "armory", "screenxpert", "glidex", "storycube",
    "screenpad", "aura", "cleaner", "winre", "cloud recovery", "office",
    "mcafee", "norton", "smart gesture app", "link to myasus", "gamevisual",
    "virtualpet", "aacap", "aac ap",
]

# Some feed entries are delivered via the Microsoft Store rather than as a
# downloadable installer. These cannot be downloaded or injected — they must be
# installed from the Store after Windows setup. We detect and skip them.
STORE_URL_MARKERS = ("apps.microsoft.com", "microsoft.com/store", "/store/apps/")


def is_store_item(url: str) -> bool:
    """True if the download points at the Microsoft Store instead of a file."""
    if not url:
        return False
    u = url.lower()
    if any(m in u for m in STORE_URL_MARKERS):
        return True
    # A bare Store product id (e.g. 9NSG1HWGCKVM) has no file extension.
    filename = url.split("/")[-1].split("?")[0]
    return "." not in filename


class AsusDriverDownloader:
    """Downloads ASUS drivers using the public support webapi."""

    # The support webapi is served from the rog.asus.com host but works for the
    # whole ASUS catalogue when systemCode=asus is passed. This is the same
    # endpoint the asus.com/rog.asus.com "Driver & Utility" pages call.
    DRIVERS_API_URL = "https://rog.asus.com/support/webapi/product/GetPDDrivers"
    BIOS_API_URL = "https://rog.asus.com/support/webapi/product/GetPDBIOS"
    DOWNLOAD_HOST = "dlcdnets.asus.com"

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.asus.com/",
        "Origin": "https://www.asus.com",
    }

    def __init__(self, model: str, osid: str = "52", website: str = "global",
                 output_dir: str = None, max_workers: int = 4,
                 latest_only: bool = True):
        """
        Args:
            model:      ASUS model name (e.g. "UX5406SA"). Full marketing SKUs
                        like "UX5406SA-S14.U732G1T" are normalized automatically.
            osid:       ASUS operating-system id (52 = Windows 11 64-bit).
            website:    ASUS region code (global/us/...).
            output_dir: Directory to save downloads.
            max_workers: Max concurrent downloads.
            latest_only: Keep only the newest version of each driver title.
        """
        self.raw_model = model.strip()
        self.model = self.normalize_model(model)
        self.osid = str(osid)
        self.website = website
        self.output_dir = Path(output_dir) if output_dir else Path(f"drivers_{self.model}")
        self.max_workers = max_workers
        self.latest_only = latest_only
        self.session = requests.Session()
        self.session.headers.update(self.HEADERS)
        self._raw_feed = None
        self._bios_feed = None

    # ------------------------------------------------------------------ #
    # Model / feed helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def normalize_model(model: str) -> str:
        """Reduce a full SKU to the base support model.

        ASUS keys its driver feed on the short model name (the "model2Name"),
        e.g. UX5406SA. Marketing SKUs append a config suffix after a hyphen or
        dot: "UX5406SA-S14.U732G1T" -> "UX5406SA".
        """
        m = model.strip().upper().replace(" ", "")
        # Split on the first '-' or '.' that follows the alphanumeric model root.
        m = re.split(r"[-.]", m, maxsplit=1)[0]
        return m

    def fetch_feed(self) -> dict:
        """Fetch the raw driver feed JSON from the ASUS support webapi."""
        if self._raw_feed is not None:
            return self._raw_feed

        params = {
            "website": self.website,
            "model": self.model,
            "pdid": "",
            "mode": "",
            "cpu": "",
            "osid": self.osid,
            "active": "",
            "systemCode": "asus",
        }
        print(f"\n🔍 Querying ASUS driver feed for model: {self.model} "
              f"(osid={self.osid}, region={self.website})")
        resp = self.session.get(self.DRIVERS_API_URL, params=params, timeout=30)
        resp.raise_for_status()
        try:
            data = resp.json()
        except ValueError:
            raise ValueError("ASUS returned a non-JSON response. Check the model "
                             "name and osid.")
        self._raw_feed = data
        return data

    def get_drivers_list(self) -> list:
        """Return a normalized list of driver categories -> files."""
        data = self.fetch_feed()
        result = (data or {}).get("Result") or {}
        categories = result.get("Obj") or []

        drivers = []
        for cat in categories:
            cat_name = cat.get("Name", "Other")
            files = []
            for f in cat.get("Files", []):
                url = (f.get("DownloadUrl") or {}).get("Global") or ""
                if not url:
                    continue
                files.append({
                    "title": f.get("Title", "Unknown"),
                    "version": f.get("Version", ""),
                    "release_date": f.get("ReleaseDate", ""),
                    "size": f.get("FileSize", ""),
                    "sha256": f.get("sha256", ""),
                    "url": url,
                    "silent_switches": self._parse_silent_switches(f.get("ExeModule", "")),
                    "severity": f.get("severityContentWording", ""),
                    "hardware_ids": [h.get("hardwareid") for h in
                                     (f.get("HardwareInfoList") or []) if h.get("hardwareid")],
                    "store": is_store_item(url),
                })
            if files:
                if self.latest_only:
                    files = self._dedupe_latest(files)
                drivers.append({"category": cat_name, "files": files})

        total = sum(len(d["files"]) for d in drivers)
        print(f"✅ Found {total} driver package(s) across {len(drivers)} categor"
              f"{'y' if len(drivers) == 1 else 'ies'}")
        return drivers

    @staticmethod
    def _parse_silent_switches(exe_module: str) -> str:
        """Extract the silent-install switches ASUS ships in ExeModule.

        Format is "<filename>%%<switches>", e.g.
        "LAN_..._1.exe%%/SUPPRESSMSGBOXES /VERYSILENT /NORESTART".
        """
        if not exe_module or "%%" not in exe_module:
            return ""
        return exe_module.split("%%", 1)[1].strip()

    @staticmethod
    def _dedupe_latest(files: list) -> list:
        """Keep only the newest ReleaseDate per driver Title."""
        best = {}
        for f in files:
            key = f["title"]
            cur = best.get(key)
            if cur is None or (f["release_date"] or "") > (cur["release_date"] or ""):
                best[key] = f
        return list(best.values())

    # ------------------------------------------------------------------ #
    # Filtering
    # ------------------------------------------------------------------ #
    @classmethod
    def is_hardware_driver(cls, category: str, title: str) -> bool:
        """Heuristic: True for injectable hardware drivers, False for apps/tools."""
        c = (category or "").lower()
        t = (title or "").lower()
        if c in APP_CATEGORY_KEYWORDS:
            return False
        for kw in APP_TITLE_KEYWORDS:
            if kw in t:
                return False
        return True

    def driver_only_list(self) -> list:
        """Driver list filtered to injectable hardware drivers (no apps, no Store)."""
        out = []
        for d in self.get_drivers_list():
            files = [f for f in d["files"]
                     if not f["store"]
                     and self.is_hardware_driver(d["category"], f["title"])]
            if files:
                out.append({"category": d["category"], "files": files})
        return out

    @staticmethod
    def _strip_store(drivers: list) -> tuple:
        """Split a driver list into (downloadable, store_items)."""
        downloadable, store = [], []
        for d in drivers:
            keep = [f for f in d["files"] if not f["store"]]
            drop = [f for f in d["files"] if f["store"]]
            if keep:
                downloadable.append({"category": d["category"], "files": keep})
            for f in drop:
                store.append((d["category"], f))
        return downloadable, store

    @staticmethod
    def _report_store(store_items: list):
        if not store_items:
            return
        print(f"\n🏪 {len(store_items)} item(s) are delivered via the Microsoft "
              f"Store — skipped (install these from the Store after setup):")
        for cat, f in store_items:
            print(f"   • [{cat}] {f['title']}")

    # ------------------------------------------------------------------ #
    # Listing / info
    # ------------------------------------------------------------------ #
    def list_categories(self):
        drivers = self.get_drivers_list()
        print(f"\n📁 Available categories for {self.model}:")
        for d in sorted(drivers, key=lambda x: x["category"]):
            hw = sum(1 for f in d["files"]
                     if self.is_hardware_driver(d["category"], f["title"]))
            tag = "" if hw == len(d["files"]) else f" ({hw} driver / {len(d['files'])-hw} app)"
            print(f"   • {d['category']}: {len(d['files'])} file(s){tag}")
            for f in d["files"]:
                if f["store"]:
                    kind = "store"
                elif self.is_hardware_driver(d["category"], f["title"]):
                    kind = "drv"
                else:
                    kind = "app"
                print(f"       [{kind}] {f['title']} — {f['version']} "
                      f"({f['size']}, {f['release_date']})")
        print("\n   Legend: [drv] injectable driver  [app] application  "
              "[store] Microsoft Store (install after setup)")

    # ------------------------------------------------------------------ #
    # Download
    # ------------------------------------------------------------------ #
    def _download_one(self, file_info: dict, dest_dir: Path) -> tuple:
        url = file_info["url"]
        filename = unquote(url.split("/")[-1].split("?")[0])
        filepath = dest_dir / filename
        if filepath.exists() and filepath.stat().st_size > 0:
            return (True, filename, "Already exists")
        try:
            resp = self.session.get(url, stream=True, timeout=120)
            resp.raise_for_status()
            with open(filepath, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        fh.write(chunk)
            return (True, filename, None)
        except Exception as e:
            if filepath.exists():
                filepath.unlink()
            return (False, filename, str(e))

    def download_all_drivers(self, categories: list = None, drivers_only: bool = False):
        """Download every package, organized by category (mirrors Lenovo default)."""
        drivers = self.driver_only_list() if drivers_only else self.get_drivers_list()
        if not drivers:
            print("❌ No drivers found for this model/OS.")
            return

        if categories:
            wanted = {c.lower() for c in categories}
            drivers = [d for d in drivers if d["category"].lower() in wanted]
            print(f"📁 Filtered to categories: {categories}")

        drivers, store_items = self._strip_store(drivers)
        self._report_store(store_items)

        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._write_manifest(drivers)

        tasks = []
        for d in drivers:
            cat_dir = self.output_dir / self._safe(d["category"])
            cat_dir.mkdir(parents=True, exist_ok=True)
            for f in d["files"]:
                tasks.append((f, cat_dir))

        print(f"\n⬇️  Downloading {len(tasks)} file(s) to {self.output_dir.absolute()}")
        self._run_downloads(tasks)

    def _run_downloads(self, tasks: list):
        ok = skip = fail = 0
        with tqdm(total=len(tasks), desc="Downloading", unit="file") as pbar:
            with ThreadPoolExecutor(max_workers=self.max_workers) as ex:
                futs = {ex.submit(self._download_one, t[0], t[1]): t for t in tasks}
                for fut in as_completed(futs):
                    success, name, err = fut.result()
                    if success:
                        skip += 1 if err == "Already exists" else 0
                        ok += 0 if err == "Already exists" else 1
                    else:
                        fail += 1
                        tqdm.write(f"❌ {name}: {err}")
                    pbar.update(1)
        print(f"\n✨ Done — downloaded {ok}, skipped {skip}, failed {fail}")

    def _write_manifest(self, drivers: list):
        path = self.output_dir / "driver_manifest.json"
        with open(path, "w") as fh:
            json.dump({
                "model": self.model,
                "raw_model": self.raw_model,
                "osid": self.osid,
                "website": self.website,
                "download_date": time.strftime("%Y-%m-%d %H:%M:%S"),
                "drivers": drivers,
            }, fh, indent=2)
        print(f"📄 Manifest: {path.name}")

    # ------------------------------------------------------------------ #
    # SCCM-style pack (download + extract .inf trees)
    # ------------------------------------------------------------------ #
    def build_sccm_pack(self, include_apps: bool = False, extract: bool = True,
                        categories: list = None):
        """Download hardware drivers and extract them into an injectable tree.

        This is the ASUS equivalent of Lenovo's SCCM pack: since ASUS ships no
        prebuilt pack, we assemble one under SCCM/<model>/<Category>/.
        """
        drivers = self.get_drivers_list() if include_apps else self.driver_only_list()
        if categories:
            wanted = {c.lower() for c in categories}
            drivers = [d for d in drivers if d["category"].lower() in wanted]
        drivers, store_items = self._strip_store(drivers)
        self._report_store(store_items)
        if not drivers:
            print("❌ No downloadable driver packages found for this model/OS.")
            return

        pack_root = self.output_dir / "SCCM" / self.model
        raw_dir = pack_root / "_packages"
        raw_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._write_manifest(drivers)

        # 1) download every package into _packages/
        print(f"\n📦 Assembling SCCM-style driver pack at {pack_root.absolute()}")
        tasks = [(f, raw_dir) for d in drivers for f in d["files"]]
        self._run_downloads(tasks)

        if not extract:
            print("\n⏭️  Skipping extraction (--no-extract).")
            print(f"   Packages saved to: {raw_dir.absolute()}")
            return

        # 2) extract each package into SCCM/<model>/<Category>/<title>/
        print(f"\n🔧 Extracting driver packages into injectable .inf trees...")
        for d in drivers:
            cat = self._safe(d["category"])
            for f in d["files"]:
                filename = unquote(f["url"].split("/")[-1].split("?")[0])
                exe_path = raw_dir / filename
                if not (exe_path.exists() and exe_path.stat().st_size > 0):
                    continue
                target = pack_root / cat / self._safe(f["title"])
                target.mkdir(parents=True, exist_ok=True)
                print(f"\n   → {d['category']} / {f['title']} ({filename})")
                self.extract_package(exe_path, target)

        # 3) summary
        inf_total = len(self._infs(pack_root))
        print(f"\n✨ SCCM pack complete — {inf_total} .inf files under {pack_root.absolute()}")
        print("\n💡 Deploy for a clean Windows install:")
        print(f"   • OOBE (USB): copy the pack to USB, press Shift+F10, then run")
        print(f"       pnputil /add-driver X:\\SCCM\\{self.model}\\*.inf /subdirs /install")
        print(f"   • Offline image (DISM):")
        print(f"       DISM /Image:C:\\Mount /Add-Driver /Driver:C:\\SCCM\\{self.model} /Recurse")
        print(f"   • MDT/SCCM: import {pack_root.name} into Out-of-Box Drivers.")
        if not include_apps:
            print("\n   (Apps/utilities like MyASUS were excluded. Use --include-apps to keep them.)")

    # ------------------------------------------------------------------ #
    # BIOS / firmware
    # ------------------------------------------------------------------ #
    def fetch_bios_feed(self) -> dict:
        """Fetch the raw BIOS/firmware feed JSON from the ASUS support webapi."""
        if self._bios_feed is not None:
            return self._bios_feed
        params = {
            "website": self.website,
            "model": self.model,
            "pdid": "",
            "cpu": "",
            "systemCode": "asus",
        }
        print(f"\n🔍 Querying ASUS BIOS/firmware feed for model: {self.model}")
        resp = self.session.get(self.BIOS_API_URL, params=params, timeout=30)
        resp.raise_for_status()
        try:
            data = resp.json()
        except ValueError:
            raise ValueError("ASUS returned a non-JSON BIOS response. Check the model name.")
        self._bios_feed = data
        return data

    def get_bios_list(self) -> list:
        """Return normalized BIOS/firmware categories -> files (latest per title)."""
        data = self.fetch_bios_feed()
        categories = ((data or {}).get("Result") or {}).get("Obj") or []
        out = []
        for cat in categories:
            files = []
            for f in cat.get("Files", []):
                url = (f.get("DownloadUrl") or {}).get("Global") or ""
                if not url:
                    continue
                files.append({
                    "title": f.get("Title", "Unknown"),
                    "version": f.get("Version", ""),
                    "release_date": f.get("ReleaseDate", ""),
                    "size": f.get("FileSize", ""),
                    "sha256": f.get("sha256", ""),
                    "url": url,
                    "severity": f.get("severityContentWording", ""),
                })
            if files:
                if self.latest_only:
                    files = self._dedupe_latest(files)
                out.append({"category": cat.get("Name", "BIOS"), "files": files})
        return out

    def list_bios(self):
        bios = self.get_bios_list()
        if not bios:
            print("❌ No BIOS/firmware found for this model.")
            return
        print(f"\n🧩 BIOS / firmware for {self.model}:")
        for c in bios:
            print(f"   • {c['category']}:")
            for f in c["files"]:
                sev = f" [{f['severity']}]" if f["severity"] else ""
                print(f"       {f['title']} — v{f['version']} "
                      f"({f['size']}, {f['release_date']}){sev}")

    def download_bios(self, extract: bool = True):
        """Download the latest BIOS (EZ Flash zip + Windows updater) and firmware."""
        bios = self.get_bios_list()
        if not bios:
            print("❌ No BIOS/firmware found for this model.")
            return

        dest = self.output_dir / "BIOS"
        dest.mkdir(parents=True, exist_ok=True)

        # Summary of what we're about to fetch
        print(f"\n🧩 Fetching BIOS/firmware for {self.model}:")
        for c in bios:
            for f in c["files"]:
                sev = f" [{f['severity']}]" if f["severity"] else ""
                print(f"   • {c['category']}: {f['title']} v{f['version']} "
                      f"({f['size']}, {f['release_date']}){sev}")

        # Manifest
        with open(dest / "bios_manifest.json", "w") as fh:
            json.dump({"model": self.model, "website": self.website,
                       "download_date": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "bios": bios}, fh, indent=2)

        tasks = [(f, dest) for c in bios for f in c["files"]]
        print(f"\n⬇️  Downloading {len(tasks)} BIOS/firmware file(s) to {dest.absolute()}")
        self._run_downloads(tasks)

        # Extract the EZ Flash .zip so the raw BIOS capsule is ready for USB.
        ez_files = []
        if extract:
            for c in bios:
                for f in c["files"]:
                    fn = unquote(f["url"].split("/")[-1].split("?")[0])
                    if fn.lower().endswith(".zip"):
                        zpath = dest / fn
                        if zpath.exists():
                            outdir = dest / "EZ_Flash"
                            outdir.mkdir(exist_ok=True)
                            names = self._unzip(zpath, outdir)
                            ez_files.extend(names)

        # Guidance
        latest_ez = self._pick(bios, "BIOS")
        latest_win = self._pick(bios, "BIOS Update(Windows)")
        latest_fw = self._pick(bios, "Firmware")
        print(f"\n✨ BIOS/firmware download complete → {dest.absolute()}")
        if latest_ez:
            print(f"\n🔧 EZ Flash (recommended before imaging), BIOS v{latest_ez['version']}:")
            if ez_files:
                print(f"   • Capsule file(s) in {dest / 'EZ_Flash'}: {', '.join(ez_files[:4])}")
            print(f"   • Copy the capsule to a FAT32 USB stick.")
            print(f"   • Boot into UEFI (tap F2), open MyASUS in UEFI → EZ Flash 3,")
            print(f"     select the file, and let it flash. Keep the charger plugged in.")
        if latest_win:
            print(f"\n🪟 Windows updater (alternative), BIOS v{latest_win['version']}:")
            print(f"   • Run the *_BIOS_Update_*.exe on the running Windows PC (AC power).")
        if latest_fw:
            print(f"\n💾 Firmware: {latest_fw['title']} v{latest_fw['version']} — "
                  f"run its .exe in Windows.")
        print("\n⚠️  BIOS flashing carries risk — never interrupt it or cut power. "
              "ASUS consumer models generally do NOT get BIOS via Windows Update.")

    @staticmethod
    def _pick(bios: list, category: str):
        for c in bios:
            if c["category"].lower() == category.lower() and c["files"]:
                return c["files"][0]
        return None

    @staticmethod
    def _unzip(zpath: Path, outdir: Path) -> list:
        """Extract a .zip (BIOS EZ Flash package) and return member file names."""
        try:
            with zipfile.ZipFile(zpath) as zf:
                zf.extractall(outdir)
                return [n for n in zf.namelist() if not n.endswith("/")]
        except Exception as e:
            print(f"   ⚠️  Could not unzip {zpath.name}: {e}")
            return []

    # ------------------------------------------------------------------ #
    # Extraction (recursive: Inno Setup / InstallShield / 7-Zip / zip / cab)
    # ------------------------------------------------------------------ #
    # ASUS driver packages are Inno Setup 6.x self-extractors. Larger ones store
    # the .inf tree directly; smaller ones wrap a *nested* archive/installer that
    # holds the real driver. So extraction must recurse into what it extracts.
    ARCHIVE_EXTS = {".exe", ".cab", ".7z", ".zip", ".msi", ".bin", ".gz", ".xz"}
    MAX_EXTRACT_DEPTH = 4

    def extract_package(self, exe_path: Path, extract_dir: Path) -> bool:
        extract_dir = extract_dir.resolve()
        extract_dir.mkdir(parents=True, exist_ok=True)
        tools = self._extract_tools()

        if any(tools.values()):
            self._extract_recursive(exe_path, extract_dir, tools, depth=0)

        n = len(self._infs(extract_dir))
        if n:
            self._cleanup(extract_dir)
            self._flatten(extract_dir)
            print(f"      ✅ extracted ({len(self._infs(extract_dir))} .inf)")
            return True

        # ASUS "Business Intelligence" packages append the real driver as a 7-Zip
        # (or zip/cab) overlay that innoextract ignores. Carve it out and extract.
        if any(tools.values()):
            self._extract_embedded(exe_path, extract_dir, tools)
            n = len(self._infs(extract_dir))
            if n:
                self._cleanup(extract_dir)
                self._flatten(extract_dir)
                print(f"      ✅ extracted embedded payload ({n} .inf)")
                return True

        # Windows fallback: run the installer's own silent self-extractor.
        if sys.platform == "win32" and self._run_silent_windows(exe_path, extract_dir):
            n = len(self._infs(extract_dir))
            if n:
                print(f"      ✅ extracted via silent install ({n} .inf)")
                return True

        self._cleanup(extract_dir)  # don't leave 7-Zip PE-section litter behind
        print("      ⚠️  Could not unpack to .inf files.")
        self._print_extract_hints(tools)
        return False

    @staticmethod
    def _extract_tools() -> dict:
        return {
            "innoextract": shutil.which("innoextract"),
            "sevenz": next((c for c in ("7z", "7zz", "7za") if shutil.which(c)), None),
            "cabextract": shutil.which("cabextract"),
            "unshield": shutil.which("unshield"),
            "unzip": shutil.which("unzip"),
        }

    def _extract_recursive(self, src: Path, out: Path, tools: dict, depth: int):
        """Extract src into out; if no .inf appears, recurse into nested payloads."""
        if depth > self.MAX_EXTRACT_DEPTH or not src.exists():
            return
        before = {p for p in out.rglob("*") if p.is_file()}
        self._extract_once(src, out, tools)
        if self._infs(out):
            return

        after = {p for p in out.rglob("*") if p.is_file()}
        new_files = after - before
        candidates = []
        for p in new_files:
            if p.resolve() == src.resolve():
                continue
            name = p.name
            ext = p.suffix.lower()
            headerless = name in ("[0]", "[1]", "[2]", "[3]")
            no_ext_blob = ext == "" and p.stat().st_size > 500_000
            if ext in self.ARCHIVE_EXTS or headerless or no_ext_blob:
                candidates.append(p)
        # Largest first — the driver payload is usually the biggest blob.
        candidates.sort(key=lambda p: -p.stat().st_size)

        for payload in candidates:
            self._extract_recursive(payload, out, tools, depth + 1)
            if self._infs(out):
                return

    def _extract_once(self, src: Path, out: Path, tools: dict):
        """Single extraction pass, choosing a tool by format/extension."""
        ext = src.suffix.lower()

        # Inno Setup self-extractors (the common ASUS case) — check by content.
        if tools["innoextract"] and self._is_inno(src, tools["innoextract"]):
            self._run([tools["innoextract"], "-e", "-s", "-d", str(out), str(src)], 1800)
            return

        if ext == ".zip" and tools["unzip"]:
            self._run([tools["unzip"], "-o", "-qq", str(src), "-d", str(out)], 600)
            if self._infs(out):
                return

        if ext == ".cab":
            if tools["cabextract"]:
                self._run([tools["cabextract"], "-q", "-d", str(out), str(src)], 600)
                if self._infs(out):
                    return
            if tools["unshield"]:
                self._run([tools["unshield"], "-d", str(out), "x", str(src)], 600)
                if self._infs(out):
                    return

        # Generic fallback: 7-Zip handles PE/SFX/msi/cab/zip/gzip/xz and more.
        if tools["sevenz"]:
            self._run([tools["sevenz"], "x", "-y", f"-o{out}", "-t*", str(src)], 900)
            if self._infs(out):
                return

        # Last-ditch: cabextract on anything cab-like without 7-Zip.
        if tools["cabextract"] and ext in ("", ".cab", ".bin"):
            self._run([tools["cabextract"], "-q", "-d", str(out), str(src)], 600)

    # Signatures of archive formats ASUS embeds as an overlay in BI packages.
    _EMBED_SIGS = (
        (b"\x37\x7A\xBC\xAF\x27\x1C", ".7z"),
        (b"PK\x03\x04", ".zip"),
        (b"MSCF", ".cab"),
    )
    _MAX_CARVE_BYTES = 300 * 1024 * 1024  # don't carve pathologically large blobs

    def _extract_embedded(self, src: Path, out: Path, tools: dict):
        """Carve embedded archive overlays out of the original .exe and extract.

        Handles ASUS BI installers, which append the driver as a 7-Zip archive
        (containing setup.bat + the .inf/.sys/.cat) after the Inno Setup data.
        """
        try:
            size = src.stat().st_size
            with open(src, "rb") as fh:
                mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
        except (OSError, ValueError):
            return
        try:
            found = []
            for sig, ext in self._EMBED_SIGS:
                start = 0
                while len(found) < 40:
                    i = mm.find(sig, start)
                    if i < 0:
                        break
                    found.append((i, ext))
                    start = i + 1
            found.sort()
            for off, ext in found:
                if size - off > self._MAX_CARVE_BYTES:
                    continue
                carved = out / f"_embedded_{off}{ext}"
                try:
                    with open(carved, "wb") as w:
                        w.write(mm[off:])
                except OSError:
                    continue
                # Try system tools first; fall back to py7zr for .7z.
                self._extract_once(carved, out, tools)
                if not self._infs(out) and ext == ".7z":
                    self._extract_7z_py(carved, out)
                try:
                    carved.unlink()
                except OSError:
                    pass
                if self._infs(out):
                    return
        finally:
            mm.close()

    @staticmethod
    def _extract_7z_py(archive: Path, out: Path) -> bool:
        """Pure-Python 7-Zip extraction fallback (used if system 7-Zip is absent)."""
        try:
            import py7zr
        except ImportError:
            return False
        try:
            with py7zr.SevenZipFile(str(archive), "r") as z:
                z.extractall(path=str(out))
            return True
        except Exception:
            return False

    @staticmethod
    def _is_inno(src: Path, innoextract: str) -> bool:
        # Note: do NOT pass -s here — silent mode suppresses the version banner
        # ("setup data version") that we detect Inno Setup by.
        try:
            info = subprocess.run([innoextract, "-i", str(src)],
                                  capture_output=True, text=True, timeout=60)
        except (subprocess.TimeoutExpired, OSError):
            return False
        return "setup data version" in f"{info.stdout}\n{info.stderr}".lower()

    @staticmethod
    def _run(cmd: list, timeout: int):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            return None

    def _run_silent_windows(self, exe_path: Path, extract_dir: Path) -> bool:
        """Windows-only: run the installer's own silent extractor as a fallback."""
        try:
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = subprocess.SW_HIDE
            cmd = [str(exe_path.absolute()),
                   "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                   f"/DIR={extract_dir.absolute()}"]
            subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                           startupinfo=startup, creationflags=subprocess.CREATE_NO_WINDOW)
            time.sleep(1)
            return bool(self._infs(extract_dir))
        except Exception:
            return False

    @staticmethod
    def _print_extract_hints(tools: dict):
        if not tools["innoextract"]:
            print("         Install innoextract (most ASUS packs are Inno Setup):")
            print("           macOS: brew install innoextract")
        if not tools["sevenz"]:
            print("         Also recommended: brew install sevenzip cabextract unshield")
        if tools["innoextract"] and tools["sevenz"]:
            print("         This package may be an install-only stub (no extractable "
                  ".inf). Run it on the target Windows PC to install directly.")

    @staticmethod
    def _flatten(extract_dir: Path):
        """Lift files out of Inno Setup wrapper dirs to a predictable location."""
        for wrapper in ("code$GetExtractPath$", "app", "{app}", "tmp", "{tmp}"):
            wdir = extract_dir / wrapper
            if not wdir.is_dir():
                continue
            for child in list(wdir.iterdir()):
                dest = extract_dir / child.name
                if not dest.exists():
                    try:
                        shutil.move(str(child), str(dest))
                    except Exception:
                        pass
            try:
                wdir.rmdir()
            except OSError:
                pass

    # Artifacts left behind when 7-Zip splits a PE executable instead of
    # unpacking its payload — noise we don't want in the driver pack.
    _PE_ARTIFACTS = (
        "[0]", "[1]", "[2]", "[3]", "CERTIFICATE", ".text", ".data", ".rdata",
        ".idata", ".edata", ".didata", ".itext", ".bss", ".tls", ".rsrc",
        ".reloc", ".pdata", ".00cfg",
    )

    @classmethod
    def _cleanup(cls, extract_dir: Path):
        for a in cls._PE_ARTIFACTS:
            p = extract_dir / a
            try:
                if p.is_file():
                    p.unlink()
                elif p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
            except Exception:
                pass

    @staticmethod
    def _infs(root: Path) -> list:
        """Case-insensitive recursive search for driver .inf files.

        ASUS ships some .inf as uppercase (.INF); rglob('*.inf') is
        case-sensitive on Linux (and on case-sensitive macOS/APFS volumes).
        """
        return [p for p in root.rglob("*")
                if p.is_file() and p.suffix.lower() == ".inf"]

    @staticmethod
    def _safe(name: str) -> str:
        return re.sub(r'[<>:"/\\|?*]+', "_", (name or "Other")).strip() or "Other"


def resolve_osid(value: str) -> str:
    if value is None:
        return "52"
    v = str(value).strip().lower()
    return OS_IDS.get(v, v)  # accept a raw numeric osid too


def main():
    parser = argparse.ArgumentParser(
        description="Download ASUS drivers by model name and build an "
                    "SCCM-style injectable driver pack for clean Windows installs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s UX5406SA                         # Download all drivers (by category)
  %(prog)s UX5406SA-S14.U732G1T             # Full SKU is normalized to UX5406SA
  %(prog)s UX5406SA --list                  # List available drivers/apps
  %(prog)s UX5406SA --sccm                  # Build injectable driver pack (drivers only)
  %(prog)s UX5406SA --sccm --include-apps   # Include MyASUS/utilities too
  %(prog)s UX5406SA --sccm --no-extract     # Download packs without extracting
  %(prog)s UX5406SA --bios                  # Download latest BIOS (EZ Flash + Windows) + firmware
  %(prog)s UX5406SA --list-bios             # List available BIOS/firmware versions
  %(prog)s UX5406SA --osid win10            # Target Windows 10 64-bit (osid 48)
  %(prog)s UX5406SA -c Audio Bluetooth      # Only specific categories

OS ids (osid): win11=52 (default, verified), win10=48. You can also pass a raw
numeric osid if you know it.
        """,
    )
    parser.add_argument("model", help="ASUS model name or full SKU (e.g. UX5406SA)")
    parser.add_argument("-o", "--output", default=None, help="Output directory")
    parser.add_argument("--osid", default="52",
                        help="OS id or alias (win11/win10). Default 52 = Windows 11 64-bit")
    parser.add_argument("--website", default="global", help="ASUS region code (default: global)")
    parser.add_argument("-c", "--categories", nargs="+", default=None,
                        help="Only these categories (e.g. Audio Bluetooth Chipset)")
    parser.add_argument("-w", "--workers", type=int, default=4,
                        help="Parallel downloads (default: 4)")
    parser.add_argument("--all-versions", action="store_true",
                        help="Keep every version (default keeps latest per driver)")
    parser.add_argument("--list", action="store_true",
                        help="List available drivers without downloading")
    parser.add_argument("--info", action="store_true",
                        help="Show the raw resolved model / feed summary")
    parser.add_argument("--drivers-only", action="store_true",
                        help="With default download: skip apps/utilities")
    parser.add_argument("--sccm", action="store_true",
                        help="Build an injectable driver pack (download + extract .inf trees)")
    parser.add_argument("--bios", action="store_true",
                        help="Download latest BIOS (EZ Flash zip + Windows updater) and firmware")
    parser.add_argument("--list-bios", action="store_true",
                        help="List available BIOS/firmware versions without downloading")
    parser.add_argument("--include-apps", action="store_true",
                        help="With --sccm: also include apps/utilities")
    parser.add_argument("--no-extract", action="store_true",
                        help="With --sccm: download packs but don't extract")

    args = parser.parse_args()

    print("=" * 60)
    print("  ASUS Driver Downloader")
    print("=" * 60)

    try:
        dl = AsusDriverDownloader(
            model=args.model,
            osid=resolve_osid(args.osid),
            website=args.website,
            output_dir=args.output,
            max_workers=args.workers,
            latest_only=not args.all_versions,
        )

        if args.info:
            data = dl.fetch_feed()
            result = (data or {}).get("Result") or {}
            print(f"\n📱 Model: {dl.raw_model} → resolved '{dl.model}' (osid {dl.osid})")
            print(f"   Categories: {len(result.get('Obj') or [])}, "
                  f"total files: {result.get('Count', '?')}")
        elif args.list:
            dl.list_categories()
        elif args.list_bios:
            dl.list_bios()
        elif args.bios:
            dl.download_bios(extract=not args.no_extract)
        elif args.sccm:
            dl.build_sccm_pack(include_apps=args.include_apps,
                               extract=not args.no_extract,
                               categories=args.categories)
        else:
            dl.download_all_drivers(categories=args.categories,
                                    drivers_only=args.drivers_only)

    except requests.HTTPError as e:
        print(f"\n❌ HTTP error from ASUS: {e}")
        sys.exit(1)
    except ValueError as e:
        print(f"\n❌ Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n\n⚠️  Cancelled by user")
        sys.exit(1)


if __name__ == "__main__":
    main()
