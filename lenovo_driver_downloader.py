#!/usr/bin/env python3
"""
Lenovo Driver Downloader
Downloads all drivers for a Lenovo device using its serial number.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import unquote, urljoin

import requests
from tqdm import tqdm


class LenovoDriverDownloader:
    """Downloads Lenovo drivers using the support API."""
    
    # Lenovo Support API endpoints
    BASE_API_URL = "https://pcsupport.lenovo.com/us/en/api/v4"
    PRODUCT_API_URL = f"{BASE_API_URL}/mse/getproducts"
    DRIVERS_API_URL = f"{BASE_API_URL}/downloads/DS_drivers"
    
    # Alternative direct API
    SUPPORT_API_URL = "https://supportapi.lenovo.com"
    
    # Headers to mimic browser requests
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://pcsupport.lenovo.com/",
        "Origin": "https://pcsupport.lenovo.com",
    }

    def __init__(self, serial_number: str, output_dir: str = None, max_workers: int = 4):
        """
        Initialize the downloader.
        
        Args:
            serial_number: Lenovo device serial number
            output_dir: Directory to save downloaded drivers
            max_workers: Maximum concurrent downloads
        """
        self.serial_number = serial_number.strip().upper()
        self.output_dir = Path(output_dir) if output_dir else Path(f"drivers_{self.serial_number}")
        self.max_workers = max_workers
        self.session = requests.Session()
        self.session.headers.update(self.HEADERS)
        self.product_info = None
        
    def get_product_info(self) -> dict:
        """Get product information from serial number."""
        print(f"\n🔍 Looking up product info for serial: {self.serial_number}")
        
        # Try the main PC support API first
        try:
            url = f"https://pcsupport.lenovo.com/us/en/api/v4/mse/getproducts?productId={self.serial_number}"
            response = self.session.get(url, timeout=30)
            
            if response.status_code == 200:
                data = response.json()
                if data and len(data) > 0:
                    self.product_info = data[0]
                    print(f"✅ Found product: {self.product_info.get('Name', 'Unknown')}")
                    print(f"   Machine Type: {self.product_info.get('Id', 'Unknown')}")
                    return self.product_info
        except Exception as e:
            print(f"⚠️  Primary API lookup failed: {e}")
        
        # Try alternate endpoint
        try:
            url = f"https://pcsupport.lenovo.com/us/en/products/{self.serial_number.lower()}"
            response = self.session.get(url, timeout=30)
            
            if response.status_code == 200:
                # Extract product ID from page content
                import re
                match = re.search(r'"productId":\s*"([^"]+)"', response.text)
                if match:
                    product_id = match.group(1)
                    # Now get proper product info
                    url2 = f"https://pcsupport.lenovo.com/us/en/api/v4/mse/getproducts?productId={product_id}"
                    response2 = self.session.get(url2, timeout=30)
                    if response2.status_code == 200:
                        data = response2.json()
                        if data and len(data) > 0:
                            self.product_info = data[0]
                            print(f"✅ Found product: {self.product_info.get('Name', 'Unknown')}")
                            return self.product_info
        except Exception as e:
            print(f"⚠️  Alternate lookup failed: {e}")
        
        raise ValueError(f"Could not find product info for serial number: {self.serial_number}")
    
    def get_drivers_list(self) -> list:
        """Get list of available drivers for the product."""
        if not self.product_info:
            self.get_product_info()
        
        product_id = self.product_info.get('Id', self.serial_number)
        print(f"\n📋 Fetching driver list for {product_id}...")
        
        drivers = []
        
        # Try the downloads API
        try:
            url = f"https://pcsupport.lenovo.com/us/en/api/v4/downloads/drivers?productId={product_id}"
            response = self.session.get(url, timeout=30)
            
            if response.status_code == 200:
                data = response.json()
                
                # Parse the driver data
                downloads = data.get('body', {}).get('DownloadItems', [])
                
                if not downloads:
                    # Try alternate structure
                    downloads = data.get('DownloadItems', [])
                
                for item in downloads:
                    driver_info = {
                        'title': item.get('Title', 'Unknown'),
                        'category': item.get('Category', {}).get('Name', 'Other'),
                        'version': item.get('Version', 'Unknown'),
                        'release_date': item.get('Date', {}).get('Unix', ''),
                        'files': []
                    }
                    
                    # Get download files
                    files = item.get('Files', [])
                    for f in files:
                        file_info = {
                            'name': f.get('Name', ''),
                            'url': f.get('URL', ''),
                            'size': f.get('Size', 0),
                            'sha256': f.get('SHA256', ''),
                        }
                        if file_info['url']:
                            driver_info['files'].append(file_info)
                    
                    if driver_info['files']:
                        drivers.append(driver_info)
                
                print(f"✅ Found {len(drivers)} drivers with downloadable files")
                return drivers
                
        except Exception as e:
            print(f"⚠️  Error fetching drivers: {e}")
        
        # Try alternate V2 API
        try:
            url = f"https://pcsupport.lenovo.com/us/en/api/v2/products/{product_id}/downloads"
            response = self.session.get(url, timeout=30)
            
            if response.status_code == 200:
                data = response.json()
                # Parse V2 response format...
                downloads = data.get('Downloads', [])
                for item in downloads:
                    driver_info = {
                        'title': item.get('Name', 'Unknown'),
                        'category': item.get('Category', 'Other'),
                        'version': item.get('Version', ''),
                        'files': []
                    }
                    
                    url = item.get('DownloadUrl', '')
                    if url:
                        driver_info['files'].append({
                            'name': item.get('FileName', url.split('/')[-1]),
                            'url': url,
                            'size': item.get('Size', 0),
                        })
                        drivers.append(driver_info)
                
                print(f"✅ Found {len(drivers)} drivers (V2 API)")
                return drivers
                
        except Exception as e:
            print(f"⚠️  V2 API also failed: {e}")
        
        return drivers
    
    def download_file(self, file_info: dict, category_dir: Path) -> tuple:
        """
        Download a single driver file.
        
        Returns:
            Tuple of (success: bool, filename: str, error: str or None)
        """
        url = file_info['url']
        # Always extract the actual filename from URL (not the description in 'name')
        filename = url.split('/')[-1].split('?')[0]
        # URL decode the filename in case it has encoded characters
        filename = unquote(filename)
        filepath = category_dir / filename
        
        # Skip if already exists
        if filepath.exists():
            return (True, filename, "Already exists")
        
        try:
            # Stream download for large files
            response = self.session.get(url, stream=True, timeout=60)
            response.raise_for_status()
            
            total_size = int(response.headers.get('content-length', 0))
            
            with open(filepath, 'wb') as f:
                if total_size:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                else:
                    f.write(response.content)
            
            return (True, filename, None)
            
        except Exception as e:
            # Clean up partial download
            if filepath.exists():
                filepath.unlink()
            return (False, filename, str(e))
    
    def download_all_drivers(self, categories: list = None):
        """
        Download all drivers.
        
        Args:
            categories: Optional list of categories to filter (e.g., ['BIOS', 'Audio'])
        """
        drivers = self.get_drivers_list()
        
        if not drivers:
            print("❌ No drivers found to download")
            return
        
        # Filter by category if specified
        if categories:
            categories_lower = [c.lower() for c in categories]
            drivers = [d for d in drivers if d['category'].lower() in categories_lower]
            print(f"📁 Filtered to {len(drivers)} drivers in categories: {categories}")
        
        # Create output directory
        self.output_dir.mkdir(parents=True, exist_ok=True)
        print(f"\n📂 Saving drivers to: {self.output_dir.absolute()}")
        
        # Save driver metadata
        metadata_path = self.output_dir / "driver_manifest.json"
        with open(metadata_path, 'w') as f:
            json.dump({
                'serial_number': self.serial_number,
                'product': self.product_info,
                'drivers': drivers,
                'download_date': time.strftime('%Y-%m-%d %H:%M:%S')
            }, f, indent=2)
        print(f"📄 Saved manifest to {metadata_path.name}")
        
        # Prepare download tasks
        download_tasks = []
        for driver in drivers:
            category = driver['category'].replace('/', '-').replace('\\', '-')
            category_dir = self.output_dir / category
            category_dir.mkdir(exist_ok=True)
            
            for file_info in driver['files']:
                download_tasks.append((file_info, category_dir, driver['title']))
        
        print(f"\n⬇️  Starting download of {len(download_tasks)} files...")
        
        # Download with progress bar
        success_count = 0
        fail_count = 0
        skip_count = 0
        
        with tqdm(total=len(download_tasks), desc="Downloading", unit="file") as pbar:
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                futures = {
                    executor.submit(self.download_file, task[0], task[1]): task
                    for task in download_tasks
                }
                
                for future in as_completed(futures):
                    task = futures[future]
                    try:
                        success, filename, error = future.result()
                        if success:
                            if error == "Already exists":
                                skip_count += 1
                            else:
                                success_count += 1
                        else:
                            fail_count += 1
                            tqdm.write(f"❌ Failed: {filename} - {error}")
                    except Exception as e:
                        fail_count += 1
                        tqdm.write(f"❌ Error: {e}")
                    
                    pbar.update(1)
        
        # Summary
        print(f"\n✨ Download complete!")
        print(f"   ✅ Downloaded: {success_count}")
        print(f"   ⏭️  Skipped (existing): {skip_count}")
        print(f"   ❌ Failed: {fail_count}")
        print(f"   📂 Location: {self.output_dir.absolute()}")
    
    def list_categories(self) -> list:
        """List available driver categories."""
        drivers = self.get_drivers_list()
        
        categories = {}
        for driver in drivers:
            cat = driver['category']
            if cat not in categories:
                categories[cat] = 0
            categories[cat] += len(driver['files'])
        
        print(f"\n📁 Available categories:")
        for cat, count in sorted(categories.items()):
            print(f"   • {cat}: {count} files")
        
        return list(categories.keys())
    
    def get_sccm_packages(self) -> list:
        """Get only SCCM driver packages from the drivers list."""
        drivers = self.get_drivers_list()
        
        sccm_packages = []
        for driver in drivers:
            title = driver['title'].lower()
            # Look for SCCM packages specifically
            if 'sccm' in title:
                # Filter to only the .exe files (the actual driver packs)
                exe_files = [f for f in driver['files'] if f['url'].lower().endswith('.exe')]
                if exe_files:
                    sccm_packages.append({
                        'title': driver['title'],
                        'category': driver['category'],
                        'files': exe_files
                    })
        
        return sccm_packages
    
    def extract_sccm_package(self, exe_path: Path, extract_dir: Path) -> bool:
        """
        Extract an SCCM package .exe file.
        
        Lenovo SCCM packages are InstallShield self-extractors. On Windows,
        they can be run with /extract. On macOS/Linux, we need special tools.
        
        Returns:
            True if extraction succeeded, False otherwise
        """
        extract_dir.mkdir(parents=True, exist_ok=True)
        
        # On Windows, use the exe's built-in extraction (most reliable)
        if sys.platform == 'win32':
            return self._extract_sccm_windows(exe_path, extract_dir)
        else:
            return self._extract_sccm_unix(exe_path, extract_dir)
    
    def _extract_sccm_windows(self, exe_path: Path, extract_dir: Path) -> bool:
        """Extract SCCM package on Windows using native extraction."""
        try:
            print(f"   📦 Running self-extractor...")
            # Lenovo packages support /extract or /VERYSILENT /DIR=
            result = subprocess.run(
                [str(exe_path), '/VERYSILENT', f'/DIR={extract_dir}'],
                capture_output=True,
                text=True,
                timeout=600
            )
            
            if result.returncode == 0 or any(extract_dir.iterdir()):
                print(f"   ✅ Extracted to {extract_dir.name}/")
                return True
            
            # Try alternate extraction flag
            result = subprocess.run(
                [str(exe_path), f'/extract:{extract_dir}'],
                capture_output=True,
                text=True,
                timeout=600
            )
            
            if result.returncode == 0 or any(extract_dir.iterdir()):
                print(f"   ✅ Extracted to {extract_dir.name}/")
                return True
                
            print(f"   ⚠️  Self-extraction failed")
            return False
            
        except Exception as e:
            print(f"   ⚠️  Extraction error: {e}")
            return False
    
    def _extract_sccm_unix(self, exe_path: Path, extract_dir: Path) -> bool:
        """Extract SCCM package on macOS/Linux using available tools."""
        
        # Find available extraction tools
        sevenz_cmd = None
        for cmd in ['7z', '7zz', '7za']:
            if shutil.which(cmd):
                sevenz_cmd = cmd
                break
        
        innoextract_cmd = shutil.which('innoextract')
        cabextract_cmd = shutil.which('cabextract')
        
        if not sevenz_cmd:
            print(f"   ❌ 7-Zip not found. Install it:")
            print(f"      brew install sevenzip")
            return False
        
        try:
            # Step 1: Extract outer layer with 7z
            print(f"   📦 Extracting outer layer...")
            result = subprocess.run(
                [sevenz_cmd, 'x', '-y', f'-o{extract_dir}', str(exe_path)],
                capture_output=True,
                text=True,
                timeout=300
            )
            
            if result.returncode != 0:
                print(f"   ⚠️  Outer extraction failed: {result.stderr[:100]}")
                return False
            
            # Check what we got
            payload_file = extract_dir / '[0]'
            
            if not payload_file.exists():
                # Check if we already have driver files
                inf_files = list(extract_dir.rglob('*.inf'))
                if inf_files:
                    print(f"   ✅ Extracted {len(inf_files)} driver files")
                    return True
                print(f"   ✅ Extracted to {extract_dir.name}/")
                return True
            
            # Step 2: Try to extract the [0] payload
            print(f"   📦 Extracting inner payload...")
            
            # Try cabextract first (Lenovo often uses CAB format inside)
            if cabextract_cmd:
                result2 = subprocess.run(
                    [cabextract_cmd, '-d', str(extract_dir), str(payload_file)],
                    capture_output=True,
                    text=True,
                    timeout=600
                )
                if result2.returncode == 0:
                    self._cleanup_extract_artifacts(extract_dir)
                    print(f"   ✅ Extracted drivers to {extract_dir.name}/")
                    return True
            
            # Try innoextract (for Inno Setup packages)
            if innoextract_cmd:
                result2 = subprocess.run(
                    [innoextract_cmd, '-d', str(extract_dir), str(payload_file)],
                    capture_output=True,
                    text=True,
                    timeout=600
                )
                if result2.returncode == 0:
                    self._cleanup_extract_artifacts(extract_dir)
                    print(f"   ✅ Extracted drivers to {extract_dir.name}/")
                    return True
            
            # Try 7z with different options
            result2 = subprocess.run(
                [sevenz_cmd, 'x', '-y', f'-o{extract_dir}', '-t*', str(payload_file)],
                capture_output=True,
                text=True,
                timeout=600
            )
            if result2.returncode == 0:
                self._cleanup_extract_artifacts(extract_dir)
                print(f"   ✅ Extracted drivers to {extract_dir.name}/")
                return True
            
            # If nothing worked, check if the [0] file is actually the drivers data
            # Some packages have a different structure
            print(f"   ⚠️  Could not extract inner payload automatically")
            print(f"   💡 The package may need Windows to extract properly.")
            print(f"      Option 1: Run on Windows: {exe_path.name} /VERYSILENT /DIR=C:\\Drivers")
            print(f"      Option 2: Install more tools: brew install cabextract innoextract")
            
            return False
            
        except subprocess.TimeoutExpired:
            print(f"   ⚠️  Extraction timed out")
            return False
        except Exception as e:
            print(f"   ⚠️  Extraction error: {e}")
            return False
    
    def _cleanup_extract_artifacts(self, extract_dir: Path):
        """Remove extraction artifacts like [0] and CERTIFICATE files."""
        for artifact in ['[0]', 'CERTIFICATE', '[1]', '[2]']:
            artifact_path = extract_dir / artifact
            if artifact_path.exists():
                try:
                    artifact_path.unlink()
                except Exception:
                    pass
    
    def download_sccm_packages(self, extract: bool = True, selected_indices: list = None):
        """
        Download SCCM driver packages only.
        
        Args:
            extract: If True, automatically extract the packages after download
            selected_indices: Optional list of package indices to download (0-based).
                            If None, user will be prompted to select packages.
        """
        sccm_packages = self.get_sccm_packages()
        
        if not sccm_packages:
            print("❌ No SCCM packages found for this device")
            print("   SCCM packages are typically available for ThinkPad/ThinkCentre business models")
            return
        
        print(f"\n📦 Found {len(sccm_packages)} SCCM package(s):")
        for idx, pkg in enumerate(sccm_packages):
            print(f"   [{idx + 1}] {pkg['title']}")
            for f in pkg['files']:
                filename = f['url'].split('/')[-1].split('?')[0]
                size_str = f['size'] if f['size'] else "Unknown size"
                print(f"       - {filename} ({size_str})")
        
        # Let user select packages if not specified
        if selected_indices is None:
            print(f"\n🔢 Select packages to download:")
            print(f"   • Enter package numbers separated by commas (e.g., 1,3,5)")
            print(f"   • Enter 'all' to download all packages")
            print(f"   • Enter 'none' to cancel")
            
            while True:
                try:
                    selection = input("\n   Your selection: ").strip().lower()
                    
                    if selection == 'none':
                        print("❌ Download cancelled")
                        return
                    elif selection == 'all' or selection == '':
                        selected_indices = list(range(len(sccm_packages)))
                        break
                    else:
                        # Parse comma-separated numbers
                        indices = []
                        for num_str in selection.split(','):
                            num_str = num_str.strip()
                            if num_str:
                                num = int(num_str)
                                if 1 <= num <= len(sccm_packages):
                                    indices.append(num - 1)  # Convert to 0-based
                                else:
                                    print(f"   ⚠️  Invalid number: {num}. Must be between 1 and {len(sccm_packages)}")
                                    raise ValueError()
                        
                        if indices:
                            selected_indices = sorted(set(indices))  # Remove duplicates and sort
                            break
                        else:
                            print("   ⚠️  No valid packages selected. Try again.")
                            
                except ValueError:
                    print("   ⚠️  Invalid input. Please enter numbers separated by commas, 'all', or 'none'.")
                except KeyboardInterrupt:
                    print("\n❌ Download cancelled")
                    return
        
        # Filter to selected packages
        selected_packages = [sccm_packages[i] for i in selected_indices]
        
        if not selected_packages:
            print("❌ No packages selected")
            return
        
        print(f"\n✅ Selected {len(selected_packages)} package(s) to download:")
        for idx in selected_indices:
            print(f"   • {sccm_packages[idx]['title']}")
        
        # Create output directory
        self.output_dir.mkdir(parents=True, exist_ok=True)
        sccm_dir = self.output_dir / "SCCM"
        sccm_dir.mkdir(exist_ok=True)
        print(f"\n📂 Saving to: {sccm_dir.absolute()}")
        
        # Download each selected package
        downloaded_files = []
        for pkg in selected_packages:
            for file_info in pkg['files']:
                url = file_info['url']
                filename = unquote(url.split('/')[-1].split('?')[0])
                filepath = sccm_dir / filename
                
                if filepath.exists():
                    print(f"\n⏭️  {filename} already exists, skipping download")
                    downloaded_files.append(filepath)
                    continue
                
                print(f"\n⬇️  Downloading {filename} ({file_info['size']})...")
                
                try:
                    response = self.session.get(url, stream=True, timeout=60)
                    response.raise_for_status()
                    
                    total_size = int(response.headers.get('content-length', 0))
                    
                    with open(filepath, 'wb') as f:
                        with tqdm(total=total_size, unit='B', unit_scale=True, desc=filename[:40]) as pbar:
                            for chunk in response.iter_content(chunk_size=8192):
                                if chunk:
                                    f.write(chunk)
                                    pbar.update(len(chunk))
                    
                    print(f"   ✅ Downloaded {filename}")
                    downloaded_files.append(filepath)
                    
                except Exception as e:
                    print(f"   ❌ Failed to download {filename}: {e}")
                    if filepath.exists():
                        filepath.unlink()
        
        # Extract packages if requested
        if extract and downloaded_files:
            print(f"\n📦 Extracting SCCM packages...")
            for filepath in downloaded_files:
                if filepath.suffix.lower() == '.exe':
                    extract_dir = sccm_dir / filepath.stem
                    if extract_dir.exists() and any(extract_dir.iterdir()):
                        print(f"\n⏭️  {filepath.stem}/ already extracted, skipping")
                        continue
                    print(f"\n🔧 Extracting {filepath.name}...")
                    self.extract_sccm_package(filepath, extract_dir)
        
        # Summary
        print(f"\n✨ SCCM package download complete!")
        print(f"   📂 Location: {sccm_dir.absolute()}")
        
        if extract:
            # List extracted folders with .inf counts
            print(f"\n📁 Extracted driver folders:")
            for filepath in downloaded_files:
                if filepath.suffix.lower() == '.exe':
                    extract_dir = sccm_dir / filepath.stem
                    if extract_dir.exists():
                        inf_count = len(list(extract_dir.rglob('*.inf')))
                        print(f"   • {extract_dir.name}/  ({inf_count} .inf driver files)")
        
        print(f"\n💡 Usage for OOBE/Deployment:")
        print(f"   • USB Install: pnputil /add-driver <path>\\*.inf /subdirs")
        print(f"   • DISM Inject: DISM /Image:C:\\Mount /Add-Driver /Driver:<path> /Recurse")


def main():
    parser = argparse.ArgumentParser(
        description="Download Lenovo drivers by serial number",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s PF1234AB                      # Download all drivers
  %(prog)s PF1234AB -o ./my_drivers      # Specify output directory
  %(prog)s PF1234AB -c BIOS Audio        # Download only BIOS and Audio drivers
  %(prog)s PF1234AB --list               # List available categories
  %(prog)s PF1234AB -w 8                 # Use 8 parallel downloads
  %(prog)s PF1234AB --sccm               # Download & extract SCCM driver packs (interactive selection)
  %(prog)s PF1234AB --sccm --sccm-packages 1 3 5  # Download specific SCCM packages by number
  %(prog)s PF1234AB --sccm --no-extract  # Download SCCM packs without extracting
        """
    )
    
    parser.add_argument(
        "serial_number",
        help="Lenovo device serial number"
    )
    parser.add_argument(
        "-o", "--output",
        help="Output directory for downloaded drivers",
        default=None
    )
    parser.add_argument(
        "-c", "--categories",
        nargs="+",
        help="Only download specific categories (e.g., BIOS Audio Chipset)",
        default=None
    )
    parser.add_argument(
        "-w", "--workers",
        type=int,
        default=4,
        help="Number of parallel downloads (default: 4)"
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available driver categories without downloading"
    )
    parser.add_argument(
        "--info",
        action="store_true",
        help="Show product info only"
    )
    parser.add_argument(
        "--sccm",
        action="store_true",
        help="Download only SCCM driver packs (contains .inf files for deployment/OOBE)"
    )
    parser.add_argument(
        "--sccm-packages",
        nargs="+",
        type=int,
        help="Select specific SCCM packages by number (e.g., --sccm-packages 1 3 5). Use with --sccm. If not specified, interactive selection will be shown."
    )
    parser.add_argument(
        "--no-extract",
        action="store_true",
        help="Don't auto-extract SCCM packages (use with --sccm)"
    )
    
    args = parser.parse_args()
    
    print("=" * 60)
    print("  Lenovo Driver Downloader")
    print("=" * 60)
    
    try:
        downloader = LenovoDriverDownloader(
            serial_number=args.serial_number,
            output_dir=args.output,
            max_workers=args.workers
        )
        
        if args.info:
            info = downloader.get_product_info()
            print(f"\n📱 Product Information:")
            print(json.dumps(info, indent=2))
        elif args.list:
            downloader.list_categories()
        elif args.sccm:
            # Convert 1-based indices from command line to 0-based
            selected_indices = None
            if args.sccm_packages:
                selected_indices = [idx - 1 for idx in args.sccm_packages]
            downloader.download_sccm_packages(extract=not args.no_extract, selected_indices=selected_indices)
        else:
            downloader.download_all_drivers(categories=args.categories)
            
    except ValueError as e:
        print(f"\n❌ Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n\n⚠️  Download cancelled by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

