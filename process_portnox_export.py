"""
Process Portnox CLEAR 'devices.csv' export into clean, PCI-ready port maps.

Reads the CLEAR endpoint export and produces, per switch (NAS):
    Switch (NAS) | Port | MAC(s) | IP(s) | Device | OS | Form Factor | Manufacturer

Outputs:
  - one combined CSV (all switches, sorted by switch then port)
  - optionally one CSV per switch (good for per-branch documentation)
  - a short summary (switch count, endpoint count, unmapped rows)

READ-ONLY on the source file. Handles multi-value MAC(s)/IP(s) cells.

--------------------------------------------------------------------------
USAGE (Windows PowerShell)
--------------------------------------------------------------------------
    cd C:\\Users\\om477259\\network-automation
    .\\.venv\\Scripts\\python.exe process_portnox_export.py
    .\\.venv\\Scripts\\python.exe process_portnox_export.py --per-switch
    .\\.venv\\Scripts\\python.exe process_portnox_export.py --input "C:\\path\\to\\devices.csv"
"""

import argparse
import csv
import re
from datetime import datetime
from pathlib import Path

DEFAULT_INPUT = r"C:\Users\om477259\Downloads\devices.csv"
OUTPUT_DIR = Path(__file__).resolve().parent / "portnox_output"

# Source columns we care about (exact CLEAR headers).
COL_NAS = "NAS name"
COL_NAS_IP = "NAS IP"
COL_PORT = "Device Port"
COL_MAC = "MAC(s)"
COL_IP = "IP(s)"
COL_DEVICE = "Device"
COL_OS = "OS"
COL_FORM = "Form Factor"
COL_MANUF = "Manufacturer"
COL_MACVENDOR = "MAC Vendor"

OUTPUT_FIELDS = [
    "switch_nas", "nas_ip", "port", "mac", "ip",
    "device", "os", "form_factor", "manufacturer",
]


def clean_multi(value: str) -> str:
    """Collapse a multi-value cell (comma/semicolon/newline separated) into a
    compact, de-duplicated, space-joined string."""
    if not value:
        return ""
    parts = re.split(r"[,;\n]+", value)
    seen, out = set(), []
    for p in (x.strip() for x in parts):
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return " ".join(out)


def load_rows(input_path: Path) -> list:
    """Read the CLEAR CSV into normalized port-map rows."""
    with open(input_path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = []
        for r in reader:
            manuf = (r.get(COL_MANUF) or "").strip() or (r.get(COL_MACVENDOR) or "").strip()
            rows.append({
                "switch_nas": (r.get(COL_NAS) or "").strip(),
                "nas_ip": (r.get(COL_NAS_IP) or "").strip(),
                "port": (r.get(COL_PORT) or "").strip(),
                "mac": clean_multi(r.get(COL_MAC, "")),
                "ip": clean_multi(r.get(COL_IP, "")),
                "device": (r.get(COL_DEVICE) or "").strip(),
                "os": (r.get(COL_OS) or "").strip(),
                "form_factor": (r.get(COL_FORM) or "").strip(),
                "manufacturer": manuf or "Unknown",
            })
    return rows


def _port_sort_key(port: str):
    """Sort ports naturally: group by the numbers in e.g. Gi1/0/5."""
    nums = tuple(int(n) for n in re.findall(r"\d+", port)) if port else ()
    return (port.split("/")[0] if port else "", nums, port)


def write_combined(rows: list, out_path: Path):
    rows_sorted = sorted(rows, key=lambda r: (r["switch_nas"],
                                              _port_sort_key(r["port"])))
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=OUTPUT_FIELDS)
        w.writeheader()
        w.writerows(rows_sorted)


def write_per_switch(rows: list, folder: Path):
    by_switch = {}
    for r in rows:
        by_switch.setdefault(r["switch_nas"] or "UNKNOWN_NAS", []).append(r)
    folder.mkdir(parents=True, exist_ok=True)
    for switch, srows in by_switch.items():
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", switch)[:60]
        srows.sort(key=lambda r: _port_sort_key(r["port"]))
        with open(folder / f"{safe}.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=OUTPUT_FIELDS)
            w.writeheader()
            w.writerows(srows)
    return len(by_switch)


def main() -> None:
    ap = argparse.ArgumentParser(description="Process Portnox CLEAR export.")
    ap.add_argument("--input", default=DEFAULT_INPUT, help="Path to devices.csv")
    ap.add_argument("--per-switch", action="store_true",
                    help="Also write one CSV per switch (per-branch docs).")
    args = ap.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ERROR: input file not found: {input_path}")
        return

    rows = load_rows(input_path)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")

    combined_path = OUTPUT_DIR / f"portmap_combined_{ts}.csv"
    write_combined(rows, combined_path)

    # Summary (no sensitive values printed).
    switches = {r["switch_nas"] for r in rows if r["switch_nas"]}
    unmapped = sum(1 for r in rows if not r["port"] or not r["switch_nas"])
    print("=== Portnox CLEAR port-map processing ===")
    print(f"Input            : {input_path}")
    print(f"Endpoints read   : {len(rows)}")
    print(f"Distinct switches: {len(switches)}")
    print(f"Rows w/o port/NAS: {unmapped} (e.g. wireless or unmanaged)")
    print(f"Combined CSV     : {combined_path}")

    if args.per_switch:
        folder = OUTPUT_DIR / f"per_switch_{ts}"
        count = write_per_switch(rows, folder)
        print(f"Per-switch CSVs  : {count} files -> {folder}")

    print("\nDone. Columns: switch_nas, nas_ip, port, mac, ip, device, os, "
          "form_factor, manufacturer.")


if __name__ == "__main__":
    main()
