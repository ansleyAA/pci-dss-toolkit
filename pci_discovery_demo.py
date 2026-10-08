"""
PCI Branch Discovery - Single-Device Demo
==========================================

Connects to ONE switch/router, collects the data that underpins PCI
segmentation documentation, and prints a readable report:

  - Device facts (hostname / version)
  - Interface status + the VLAN on each port   (the "port map")
  - VLAN table                                   (segmentation)
  - CDP/LLDP neighbors                           (how it connects to others)

This is a DEMO of the data-collection step for the fleet-wide approach.
It is READ-ONLY (only runs 'show' commands - makes no changes).

--------------------------------------------------------------------------
CREDENTIALS (.env)
--------------------------------------------------------------------------
Reads a .env file in the SAME folder as this script. Expected keys:

    DEVICE_HOST= os.getenv("DEVICE_HOST")
    NET_USERNAME=os.getenv("NET_USERNAME")
    NET_PASSWORD=os.getenv("NET_PASSWORD")
    NET_SECRET=os.getenv("NET_SECRET")
    DEVICE_TYPE=cisco_ios        # optional, defaults to cisco_ios

(No spaces around '=', no leading spaces before the key.)

--------------------------------------------------------------------------
RUN (Windows PowerShell)
--------------------------------------------------------------------------
    cd C:\\Users\\om477259\\network-automation
    .\\.venv\\Scripts\\python.exe pci_discovery_demo.py

(Deps: netmiko, python-dotenv - already in your venv.)
"""

import os
import csv
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from netmiko import ConnectHandler
from netmiko.exceptions import (
    NetmikoAuthenticationException,
    NetmikoTimeoutException,
)

# Optional OUI -> vendor lookup (offline). Degrades gracefully if unavailable.
try:
    from mac_vendor_lookup import MacLookup
    _MAC_LOOKUP = MacLookup()
    _VENDOR_OK = True
except Exception:  # noqa: BLE001 - library missing or DB not built
    _MAC_LOOKUP = None
    _VENDOR_OK = False

# Load .env sitting next to this script.
# Load .env from the project root (parent of this PCIDSS folder).
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# Where to write exports (next to this script).
OUTPUT_DIR = Path(__file__).resolve().parent / "discovery_output"

DEVICE = {
    "device_type": os.getenv("DEVICE_TYPE", "cisco_ios"),
    "host": os.getenv("DEVICE_HOST"),
    "username": os.getenv("NET_USERNAME"),
    "password": os.getenv("NET_PASSWORD"),
    "secret": os.getenv("NET_SECRET"),
    "fast_cli": False,
}


def run_structured(conn, command: str):
    """Run a show command and return parsed data if a TextFSM template exists,
    otherwise return the raw string. Returns (parsed_or_text, is_structured)."""
    result = conn.send_command(command, use_textfsm=True, read_timeout=60)
    return result, isinstance(result, list)


def section(title: str):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def report_interfaces(conn):
    """Port map: each interface, its status, and VLAN (if available)."""
    section("INTERFACE / PORT MAP  (show interfaces status)")
    data, structured = run_structured(conn, "show interfaces status")
    if structured and data:
        # keys vary by template: port, name/description, status, vlan
        print(f"{'Port':<16}{'Status':<14}{'VLAN':<10}{'Description'}")
        print("-" * 60)
        for row in data:
            port = row.get("port", row.get("interface", "?"))
            status = row.get("status", "?")
            vlan = str(row.get("vlan", row.get("vlan_id", "")))
            desc = row.get("name", row.get("description", ""))
            print(f"{port:<16}{status:<14}{vlan:<10}{desc}")
    else:
        # Routers (no 'show int status') fall back to ip interface brief.
        print("('show interfaces status' not available - trying ip int brief)")
        data2, s2 = run_structured(conn, "show ip interface brief")
        if s2 and data2:
            for row in data2:
                print(f"  {row.get('interface','?'):<24}"
                      f"{row.get('ip_address','?'):<16}"
                      f"{row.get('status','?')}/{row.get('proto', row.get('protocol',''))}")
        else:
            print(data2)


def report_vlans(conn):
    """Segmentation: the VLAN table."""
    section("VLANs  (show vlan brief)  -- segmentation evidence")
    data, structured = run_structured(conn, "show vlan brief")
    if structured and data:
        for row in data:
            vid = row.get("vlan_id", row.get("vlan", "?"))
            name = row.get("vlan_name", row.get("name", "?"))
            print(f"  VLAN {vid:<6} {name}")
    else:
        print("('show vlan brief' not available on this device - likely a router)")
        if isinstance(data, str):
            print(data)


def report_neighbors(conn):
    """Topology: how this device connects to others."""
    section("NEIGHBORS  (CDP)  -- device interconnects")
    data, structured = run_structured(conn, "show cdp neighbors detail")
    if structured and data:
        for row in data:
            nbr = row.get("destination_host", row.get("neighbor", "?"))
            local = row.get("local_port", row.get("local_interface", "?"))
            remote = row.get("remote_port", row.get("neighbor_interface", "?"))
            print(f"  {local:<22} -> {nbr}  ({remote})")
    else:
        print("(No CDP data parsed - trying LLDP)")
        data2, s2 = run_structured(conn, "show lldp neighbors")
        print(data2 if not s2 else
              "\n".join(f"  {r}" for r in data2) if data2 else "  none")


def report_mac_table(conn):
    """Layer 2: which MAC addresses are learned on which switchport.
    (Switches only - routers don't have a MAC address table.)
    Returns the parsed rows (or []) so they can be joined with ARP."""
    section("MAC ADDRESS TABLE  (show mac address-table)  -- MAC -> port")
    data, structured = run_structured(conn, "show mac address-table")
    if structured and data:
        print(f"{'VLAN':<8}{'MAC Address':<18}{'Type':<10}{'Port'}")
        print("-" * 50)
        for row in data:
            vlan = str(row.get("vlan", "?"))
            mac = row.get("destination_address", row.get("mac", "?"))
            typ = row.get("type", "")
            port = row.get("destination_port", row.get("ports", row.get("port", "?")))
            if isinstance(port, list):
                port = ",".join(port)
            print(f"{vlan:<8}{mac:<18}{typ:<10}{port}")
        return data
    print("('show mac address-table' not available - likely a router (L3).")
    print(" Use the ARP table below for IP<->MAC on routers.)")
    return []


def report_arp(conn):
    """Layer 3: IP <-> MAC mapping from the ARP table.
    Returns the parsed rows (or []) so they can be joined with the MAC table."""
    section("ARP TABLE  (show ip arp)  -- IP <-> MAC <-> interface")
    data, structured = run_structured(conn, "show ip arp")
    if structured and data:
        print(f"{'IP Address':<18}{'MAC Address':<18}{'Age':<8}{'Interface'}")
        print("-" * 55)
        for row in data:
            ip = row.get("address", row.get("ip_address", "?"))
            mac = row.get("mac", row.get("mac_address", row.get("hardware_addr", "?")))
            age = str(row.get("age", ""))
            iface = row.get("interface", "")
            print(f"{ip:<18}{mac:<18}{age:<8}{iface}")
        return data
    print(data if isinstance(data, str) else "  (no ARP entries parsed)")
    return []


def _norm_mac(mac: str) -> str:
    """Normalize a MAC to a comparable form (lowercase, no separators).
    Handles both 'aabb.cc00.0100' and 'aa:bb:cc:00:01:00' styles."""
    if not mac:
        return ""
    return mac.lower().replace(".", "").replace(":", "").replace("-", "")


def _vendor(mac: str) -> str:
    """Infer the manufacturer from the MAC's OUI (offline lookup).
    Returns a vendor name, 'Unknown', or a note if lookup is unavailable.

    NOTE: this identifies the MANUFACTURER, which only *implies* a device
    family (e.g. a VoIP-vendor OUI -> likely a phone). It is NOT a definitive
    device type - treat it as an inference."""
    if not _VENDOR_OK:
        return "(vendor lookup unavailable)"
    try:
        return _MAC_LOOKUP.lookup(mac)
    except Exception:  # noqa: BLE001 - OUI not found / malformed
        return "Unknown"


def report_port_mac_ip(mac_rows: list, arp_rows: list):
    """Combined view: PORT -> MAC -> IP -> VENDOR, by joining the MAC address
    table (MAC->port) with the ARP table (MAC->IP) and adding an OUI-based
    vendor inference. Also exports the result to a timestamped CSV.

    This is the money view for a port map: not just 'a device is on this port'
    but which MAC, what IP, and (inferred) what kind of device."""
    section("COMBINED  PORT -> MAC -> IP -> VENDOR  (joined + OUI lookup)")

    if not mac_rows:
        print("  (No MAC address table data - likely a router, or the command/")
        print("   template wasn't available. The combined view needs a switch's")
        print("   MAC address table.)")
        return

    # Build MAC -> IP lookup from the ARP table.
    mac_to_ip = {}
    for a in arp_rows:
        amac = _norm_mac(a.get("mac") or a.get("mac_address") or a.get("hardware_addr", ""))
        aip = a.get("address") or a.get("ip_address", "")
        if amac:
            mac_to_ip[amac] = aip

    # Build the combined rows.
    combined = []
    for m in mac_rows:
        mac = m.get("destination_address", m.get("mac", "?"))
        port = m.get("destination_port", m.get("ports", m.get("port", "?")))
        if isinstance(port, list):
            port = ",".join(port)
        vlan = str(m.get("vlan", ""))
        typ = m.get("type", "")
        ip = mac_to_ip.get(_norm_mac(mac), "-")
        vendor = _vendor(mac)
        combined.append({
            "port": port, "vlan": vlan, "mac": mac,
            "ip": ip, "vendor_inferred": vendor, "type": typ,
        })

    # Print.
    print(f"{'Port':<14}{'VLAN':<6}{'MAC Address':<17}{'IP Address':<16}"
          f"{'Vendor (inferred)':<28}{'Type'}")
    print("-" * 92)
    for r in combined:
        print(f"{r['port']:<14}{r['vlan']:<6}{r['mac']:<17}{r['ip']:<16}"
              f"{r['vendor_inferred'][:27]:<28}{r['type']}")

    matched = sum(1 for r in combined if r["ip"] != "-")
    print(f"\n  {matched}/{len(combined)} MACs resolved to an IP via ARP.")
    if not _VENDOR_OK:
        print("  Vendor lookup unavailable - run: python -m mac_vendor_lookup "
              "(to build the offline OUI DB), or 'pip install mac-vendor-lookup'.")
    print("  Vendor = MANUFACTURER inferred from the OUI; implies but does not")
    print("  prove the exact device type.")

    # Export to CSV.
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    host = str(DEVICE["host"]).replace(":", "-").replace("/", "-")
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    csv_path = OUTPUT_DIR / f"portmap_{host}_{ts}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=["port", "vlan", "mac", "ip", "vendor_inferred", "type"])
        writer.writeheader()
        writer.writerows(combined)
    print(f"\n  Exported {len(combined)} rows -> {csv_path}")


def report_facts(conn):
    section("DEVICE FACTS")
    print(f"  Host        : {DEVICE['host']}")
    print(f"  Prompt      : {conn.find_prompt()}")
    ver, structured = run_structured(conn, "show version")
    if structured and ver:
        v = ver[0]
        print(f"  Hostname    : {v.get('hostname', '?')}")
        print(f"  Version     : {v.get('version', '?')}")
        print(f"  Model/HW    : {v.get('hardware', v.get('platform', '?'))}")
    print(f"  Collected   : {datetime.now().isoformat(timespec='seconds')}")


def main() -> None:
    # Fail early with a clear message if .env is missing/incomplete.
    if not DEVICE["host"] or not DEVICE["username"]:
        print("ERROR: DEVICE_HOST and/or NET_USERNAME not set.")
        print("Create a .env next to this script with DEVICE_HOST, NET_USERNAME,")
        print("NET_PASSWORD, NET_SECRET (no spaces around '=').")
        return

    print(f"Connecting to {DEVICE['host']} ...")
    try:
        with ConnectHandler(**DEVICE) as conn:
            conn.enable()
            report_facts(conn)
            report_interfaces(conn)
            report_vlans(conn)
            mac_rows = report_mac_table(conn)
            arp_rows = report_arp(conn)
            report_port_mac_ip(mac_rows, arp_rows)
            report_neighbors(conn)
    except NetmikoAuthenticationException:
        print("ERROR: Authentication failed - check NET_USERNAME/PASSWORD/SECRET.")
        return
    except NetmikoTimeoutException:
        print(f"ERROR: Could not reach {DEVICE['host']}:22 - check IP/port/firewall.")
        return
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}")
        return

    print("\nDone. This is the per-device data that, collected across all")
    print("branches, feeds the port maps, VLAN/segmentation evidence, and")
    print("the branch register for PCI documentation.")


if __name__ == "__main__":
    main()
