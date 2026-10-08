r"""
Build a central MAC -> Device reference database
=================================================

Reads all per-switch CSVs produced by process_portnox_export.py (including
OMZIL_Corporate.csv and OM-CISCO-WLC2.csv), splits multi-MAC cells so each
individual MAC becomes one row, and writes a single reference CSV:

    mac_device_reference.csv
    columns: mac, device, os, form_factor, manufacturer, ip, source

This reference is then used by the rack diagram to LABEL a switchport with
the real device identity when it sees a MAC (instead of a raw MAC/vendor).

USAGE (from the project root):
    .\.venv\Scripts\python.exe PCIDSS\build_mac_database.py ^
        --source "portnox_output\per_switch_2026-10-02_123416"

    (defaults to that folder if --source is omitted)
"""

import argparse
import csv
import re
from collections import OrderedDict
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent

DEFAULT_SOURCE = PROJECT_ROOT / "portnox_output" / "per_switch_2026-10-02_123416"
OUTPUT_CSV = HERE / "mac_device_reference.csv"


def norm_mac(mac: str) -> str:
    """Canonical MAC key: lowercase, colon-separated, no stray chars.
    Returns '' if the string isn't a plausible MAC."""
    hexonly = re.sub(r"[^0-9a-fA-F]", "", mac or "")
    if len(hexonly) != 12:
        return ""
    hexonly = hexonly.lower()
    return ":".join(hexonly[i:i + 2] for i in range(0, 12, 2))


def split_multi(value: str):
    """Split a space/comma/semicolon-separated cell into parts."""
    if not value:
        return []
    return [p for p in re.split(r"[,;\s]+", value.strip()) if p]


def build(source_dir: Path) -> dict:
    """Walk every CSV in source_dir and build {mac_key: record}."""
    db = OrderedDict()
    files = sorted(source_dir.glob("*.csv"))
    if not files:
        print(f"No CSV files found in {source_dir}")
        return db

    rows_read = 0
    for f in files:
        with open(f, encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                rows_read += 1
                macs = split_multi(row.get("mac", ""))
                ips = split_multi(row.get("ip", ""))
                ip0 = ips[0] if ips else ""
                device = (row.get("device") or "").strip()
                osv = (row.get("os") or "").strip()
                form = (row.get("form_factor") or "").strip()
                manuf = (row.get("manufacturer") or "").strip()
                source = (row.get("switch_nas") or f.stem).strip()
                for raw_mac in macs:
                    key = norm_mac(raw_mac)
                    if not key:
                        continue
                    # Keep the first good record for a MAC; prefer one that
                    # actually has a device name if we see the MAC again.
                    existing = db.get(key)
                    if existing and existing["device"]:
                        continue
                    db[key] = {
                        "mac": key,
                        "device": device,
                        "os": osv,
                        "form_factor": form,
                        "manufacturer": manuf,
                        "ip": ip0,
                        "source": source,
                    }
    print(f"Read {rows_read} rows from {len(files)} files "
          f"-> {len(db)} unique MACs")
    return db


def write_db(db: dict, out_csv: Path):
    fields = ["mac", "device", "os", "form_factor", "manufacturer", "ip", "source"]
    with open(out_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for rec in db.values():
            w.writerow(rec)


def load_mac_database(path: Path = OUTPUT_CSV) -> dict:
    """Helper for other scripts: load the reference CSV into {mac_key: record}.
    Returns {} if the file doesn't exist yet."""
    db = {}
    if not Path(path).exists():
        return db
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            key = norm_mac(row.get("mac", ""))
            if key:
                db[key] = row
    return db


def main() -> None:
    ap = argparse.ArgumentParser(description="Build central MAC->Device reference.")
    ap.add_argument("--source", default=str(DEFAULT_SOURCE),
                    help="Folder of per-switch CSVs (from process_portnox_export.py)")
    ap.add_argument("--out", default=str(OUTPUT_CSV), help="Output reference CSV")
    args = ap.parse_args()

    source = Path(args.source)
    if not source.exists():
        print(f"ERROR: source folder not found: {source}")
        return

    db = build(source)
    if not db:
        return
    write_db(db, Path(args.out))

    # quick stats
    with_device = sum(1 for r in db.values() if r["device"])
    print(f"Reference written -> {args.out}")
    print(f"  {with_device}/{len(db)} MACs have a device name")


if __name__ == "__main__":
    main()
