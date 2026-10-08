"""
PCI Discovery -> Switchport Mapping -> Diagram  (end-to-end pipeline)
=====================================================================

For ONE switch, this script:
  1. DISCOVER : connects (Netmiko) and pulls MAC address-table + ARP + status
  2. MAP      : builds a per-switchport map  (Port -> MAC -> IP -> Vendor)
  3. DIAGRAM  : renders a switch port-grid PNG, colour-coded, with a CSV

Credentials come from .env (same folder):
    DEVICE_HOST=<switch IP>
    NET_USERNAME=...
    NET_PASSWORD=...
    NET_SECRET=...
    DEVICE_TYPE=cisco_ios        # optional

Outputs land in:  switchport_output\
    <host>_portmap_<ts>.csv
    <host>_portmap_<ts>.png

READ-ONLY on the device (only 'show' commands).

USAGE (Windows PowerShell):
    cd C:\\Users\\om477259\\network-automation
    .\\.venv\\Scripts\\python.exe pci_discovery_to_diagram.py
"""

import csv
import os
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

from dotenv import load_dotenv
from netmiko import ConnectHandler
from netmiko.exceptions import (
    NetmikoAuthenticationException,
    NetmikoTimeoutException,
)

# Optional offline OUI -> vendor lookup.
try:
    from mac_vendor_lookup import MacLookup
    _ML = MacLookup()
    _VENDOR_OK = True
except Exception:  # noqa: BLE001
    _ML = None
    _VENDOR_OK = False

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
OUTPUT_DIR = Path(__file__).resolve().parent / "switchport_output"

DEVICE = {
    "device_type": os.getenv("DEVICE_TYPE", "cisco_ios"),
    "host": os.getenv("DEVICE_HOST"),
    "username": os.getenv("NET_USERNAME"),
    "password": os.getenv("NET_PASSWORD"),
    "secret": os.getenv("NET_SECRET"),
    "fast_cli": False,
}

# Match physical switch ports (Gi/Fa/Te/Eth 1/0/5 etc). Skip CPU/internal.
PHYS_PORT_RE = re.compile(r"^(Gi|Fa|Te|Tw|Eth|Et|Po)\S*\d", re.IGNORECASE)


# --------------------------------------------------------------------------
# 1. DISCOVER
# --------------------------------------------------------------------------
def discover(conn) -> dict:
    """Pull the raw tables we need, parsed where possible."""
    def grab(cmd):
        return conn.send_command(cmd, use_textfsm=True, read_timeout=60)

    return {
        "mac": grab("show mac address-table"),
        "arp": grab("show ip arp"),
        "status": grab("show interfaces status"),
        "prompt": conn.find_prompt(),
    }


# --------------------------------------------------------------------------
# 2. MAP
# --------------------------------------------------------------------------
def _norm_mac(mac: str) -> str:
    return (mac or "").lower().replace(".", "").replace(":", "").replace("-", "")


def _vendor(mac: str) -> str:
    if not _VENDOR_OK:
        return ""
    try:
        return _ML.lookup(mac)
    except Exception:  # noqa: BLE001
        return "Unknown"


def build_portmap(data: dict) -> list:
    """Return a list of per-port dicts:
        port, mac_count, macs, ips, vendors, vlan, note
    One row per physical port that has at least one MAC learned."""
    mac_rows = data["mac"] if isinstance(data["mac"], list) else []
    arp_rows = data["arp"] if isinstance(data["arp"], list) else []
    status_rows = data["status"] if isinstance(data["status"], list) else []

    # MAC -> IP from ARP
    mac_to_ip = {}
    for a in arp_rows:
        m = _norm_mac(a.get("mac") or a.get("mac_address") or a.get("hardware_addr", ""))
        ip = a.get("address") or a.get("ip_address", "")
        if m:
            mac_to_ip[m] = ip

    # port -> status/vlan/description from 'show int status'
    port_meta = {}
    for s in status_rows:
        p = s.get("port") or s.get("interface")
        if p:
            port_meta[p] = {
                "vlan": str(s.get("vlan", s.get("vlan_id", ""))),
                "status": s.get("status", ""),
                "desc": s.get("name", s.get("description", "")),
            }

    # group MACs by port
    by_port = defaultdict(list)
    for m in mac_rows:
        port = m.get("destination_port") or m.get("ports") or m.get("port")
        if isinstance(port, list):
            port = port[0] if port else ""
        mac = m.get("destination_address") or m.get("mac")
        vlan = str(m.get("vlan", ""))
        if port and mac and PHYS_PORT_RE.match(str(port)):
            by_port[port].append((mac, vlan))

    out = []
    for port, entries in by_port.items():
        macs = [e[0] for e in entries]
        vlans = sorted({e[1] for e in entries if e[1]})
        ips = [mac_to_ip.get(_norm_mac(mc), "") for mc in macs]
        vendors = []
        for mc in macs:
            v = _vendor(mc)
            if v and v not in vendors:
                vendors.append(v)
        meta = port_meta.get(port, {})
        note = ""
        if len(macs) > 1:
            note = "multi-MAC (trunk/uplink or phone+PC)"
        out.append({
            "port": port,
            "mac_count": len(macs),
            "macs": " ".join(macs),
            "ips": " ".join(ip for ip in ips if ip),
            "vlan": meta.get("vlan") or (vlans[0] if vlans else ""),
            "vendor": "; ".join(vendors),
            "status": meta.get("status", ""),
            "description": meta.get("desc", ""),
            "note": note,
        })
    out.sort(key=lambda r: _port_sort_key(r["port"]))
    return out


def _port_sort_key(port: str):
    nums = tuple(int(n) for n in re.findall(r"\d+", port))
    return (re.sub(r"\d+", "", port), nums)


# --------------------------------------------------------------------------
# 3. DIAGRAM
# --------------------------------------------------------------------------
def render_diagram(portmap: list, host: str, out_png: Path):
    """Render a simple port-grid: one cell per mapped port."""
    n = len(portmap)
    if n == 0:
        print("  (no physical ports with learned MACs - nothing to diagram)")
        return
    cols = 8
    rows = (n + cols - 1) // cols
    fig_w = max(14, cols * 2.2)
    fig_h = max(5, rows * 1.9 + 3)
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    ax.set_xlim(0, cols)
    ax.set_ylim(0, rows + 2)
    ax.axis("off")
    ax.text(cols / 2, rows + 1.4, f"Switchport MAC Map - {host}",
            ha="center", fontsize=15, weight="bold")
    ax.text(cols / 2, rows + 0.9,
            f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} "
            f"- {n} mapped ports (physical, with learned MAC)",
            ha="center", fontsize=8.5, style="italic", color="#555")

    for i, r in enumerate(portmap):
        col = i % cols
        row = rows - 1 - (i // cols)
        x, y = col, row
        multi = r["mac_count"] > 1
        color = "#ffe0b2" if multi else "#d6e8ff"   # amber=multi, blue=single
        ax.add_patch(Rectangle((x + 0.05, y + 0.05), 0.9, 0.9,
                     fc=color, ec="#333", lw=1))
        port_short = r["port"].replace("GigabitEthernet", "Gi").replace(
            "FastEthernet", "Fa").replace("TenGigabitEthernet", "Te")
        ax.text(x + 0.5, y + 0.78, port_short, ha="center", fontsize=6.5, weight="bold")
        # first MAC (short)
        mac0 = r["macs"].split(" ")[0] if r["macs"] else ""
        ax.text(x + 0.5, y + 0.55, mac0, ha="center", fontsize=5.0)
        ip0 = r["ips"].split(" ")[0] if r["ips"] else ""
        ax.text(x + 0.5, y + 0.38, ip0, ha="center", fontsize=5.0, color="#1565c0")
        vend = (r["vendor"].split(";")[0] if r["vendor"] else "")[:16]
        ax.text(x + 0.5, y + 0.20, vend, ha="center", fontsize=4.6, color="#444")
        if multi:
            ax.text(x + 0.5, y + 0.08, f"x{r['mac_count']} MACs",
                    ha="center", fontsize=4.6, color="#b00020")

    # legend
    ax.add_patch(Rectangle((0.1, 0.1), 0.4, 0.4, fc="#d6e8ff", ec="#333"))
    ax.text(0.6, 0.3, "single MAC (access port)", fontsize=8, va="center")
    ax.add_patch(Rectangle((3.1, 0.1), 0.4, 0.4, fc="#ffe0b2", ec="#333"))
    ax.text(3.6, 0.3, "multi-MAC (trunk/uplink or phone+PC)", fontsize=8, va="center")

    plt.tight_layout()
    plt.savefig(out_png, dpi=150, bbox_inches="tight")
    plt.close(fig)


def write_csv(portmap: list, out_csv: Path):
    fields = ["port", "mac_count", "macs", "ips", "vlan", "vendor",
              "status", "description", "note"]
    with open(out_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(portmap)


# --------------------------------------------------------------------------
# PIPELINE
# --------------------------------------------------------------------------
def main() -> None:
    if not DEVICE["host"] or not DEVICE["username"]:
        print("ERROR: set DEVICE_HOST and NET_USERNAME in .env.")
        return

    print(f"[1/3] DISCOVER - connecting to {DEVICE['host']} ...")
    try:
        with ConnectHandler(**DEVICE) as conn:
            conn.enable()
            data = discover(conn)
    except NetmikoAuthenticationException:
        print("ERROR: authentication failed (check .env credentials).")
        return
    except NetmikoTimeoutException:
        print(f"ERROR: cannot reach {DEVICE['host']}:22 (IP/port/firewall?).")
        return
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}")
        return

    if not isinstance(data["mac"], list):
        print("WARNING: 'show mac address-table' returned no structured data.")
        print("This is likely a ROUTER (no MAC table) - point at a SWITCH.")
        return

    print("[2/3] MAP - building per-switchport map ...")
    portmap = build_portmap(data)
    print(f"      {len(portmap)} physical ports with learned MACs.")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    host = str(DEVICE["host"]).replace(":", "-").replace("/", "-")
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    out_csv = OUTPUT_DIR / f"{host}_portmap_{ts}.csv"
    out_png = OUTPUT_DIR / f"{host}_portmap_{ts}.png"

    write_csv(portmap, out_csv)
    print(f"      CSV  -> {out_csv}")

    print("[3/3] DIAGRAM - rendering port grid ...")
    render_diagram(portmap, DEVICE["host"], out_png)
    print(f"      PNG  -> {out_png}")
    print("\nDone. Pipeline complete: discover -> map -> diagram.")


if __name__ == "__main__":
    main()
