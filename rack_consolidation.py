r"""
Rack / Floor Consolidation  -  multi-switch port map + interconnect diagram
===========================================================================

For a group of switches in one rack/floor, this script:
  1. DISCOVER   : connects (Netmiko, concurrently) to each switch and pulls
                  MAC table, ARP, interface status, and CDP/LLDP neighbors
  2. MAP        : builds each switch's per-port map (Port -> MAC -> IP -> Vendor)
  3. INTERCONNECT: auto-detects switch-to-switch links from CDP/LLDP
                  (works for stacked / trunk-linked / core-uplinked - whatever
                   the neighbors report)
  4. DIAGRAM    : renders ONE consolidated, rack-style diagram (switches
                  stacked top-to-bottom) with interconnect lines + a CSV

Credentials come from the project-root .env:
    NET_USERNAME, NET_PASSWORD, NET_SECRET
Which switches form a rack is defined in rack_inventory.yaml.

READ-ONLY on devices (only 'show' commands).

USAGE (Windows PowerShell, from the project root):
    .\.venv\Scripts\python.exe PCIDSS\rack_consolidation.py --rack Floor1-RackA
    .\.venv\Scripts\python.exe PCIDSS\rack_consolidation.py --list
"""

import argparse
import csv
import os
import re
import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

# Quiet Paramiko's background-thread tracebacks (e.g. SSHv1 'IncompatiblePeer')
# so a single unreachable/legacy device doesn't spam the console. The error is
# still caught and reported cleanly per-switch by discover_switch().
logging.getLogger("paramiko").setLevel(logging.CRITICAL)
logging.getLogger("paramiko.transport").setLevel(logging.CRITICAL)

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Rectangle

import yaml
from dotenv import load_dotenv
from netmiko import ConnectHandler
from netmiko.exceptions import (
    NetmikoAuthenticationException,
    NetmikoTimeoutException,
)

try:
    from mac_vendor_lookup import MacLookup
    _ML = MacLookup()
    _VENDOR_OK = True
except Exception:  # noqa: BLE001
    _ML = None
    _VENDOR_OK = False

HERE = Path(__file__).resolve().parent
load_dotenv(HERE.parent / ".env")                 # .env in project root
INVENTORY = HERE / "rack_inventory.yaml"
OUTPUT_DIR = HERE / "rack_output"

CREDS = {
    "username": os.getenv("NET_USERNAME"),
    "password": os.getenv("NET_PASSWORD"),
    "secret": os.getenv("NET_SECRET"),
}

PHYS_PORT_RE = re.compile(r"^(Gi|Fa|Te|Tw|Eth|Et)\S*\d", re.IGNORECASE)

# Central MAC -> device reference (built by build_mac_database.py).
# Loaded once; used to label access ports with the real device identity.
_MAC_DB_PATH = HERE / "mac_device_reference.csv"


def _load_mac_db():
    """Load mac_device_reference.csv into {normalized_mac: record}. Empty if absent."""
    db = {}
    if not _MAC_DB_PATH.exists():
        return db
    try:
        with open(_MAC_DB_PATH, encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                key = (row.get("mac") or "").lower().replace(":", "").replace(".", "").replace("-", "")
                if key:
                    db[key] = row
    except Exception:  # noqa: BLE001
        pass
    return db


MAC_DB = _load_mac_db()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _norm_mac(mac: str) -> str:
    return (mac or "").lower().replace(".", "").replace(":", "").replace("-", "")


def _device_for_mac(mac: str) -> str:
    """Return the friendly device name for a MAC from the reference DB,
    or '' if unknown / the 'device' is just the MAC itself."""
    rec = MAC_DB.get(_norm_mac(mac))
    if not rec:
        return ""
    dev = (rec.get("device") or "").strip()
    # Portnox sometimes stores the MAC itself as the 'device' when unprofiled.
    if not dev or _norm_mac(dev) == _norm_mac(mac):
        return ""
    return dev


def _vendor(mac: str) -> str:
    if not _VENDOR_OK:
        return ""
    try:
        return _ML.lookup(mac)
    except Exception:  # noqa: BLE001
        return "Unknown"


def _port_sort_key(port: str):
    nums = tuple(int(n) for n in re.findall(r"\d+", port))
    return (re.sub(r"\d+", "", port), nums)


# --------------------------------------------------------------------------
# 1. DISCOVER (one switch)
# --------------------------------------------------------------------------
def discover_switch(sw: dict) -> dict:
    """Connect to one switch and return raw parsed tables.
    Returns a dict with name/host and the parsed command outputs (or an error)."""
    name = sw.get("name", sw["host"])
    # Per-device enable secret: if the inventory gives 'secret_env', read that
    # .env variable; otherwise fall back to the shared NET_SECRET.
    # (Optionally allow per-device username/password the same way.)
    secret = CREDS["secret"]
    if sw.get("secret_env"):
        val = os.getenv(sw["secret_env"])
        if val is None:
            print(f"    WARNING: {name}: secret_env '{sw['secret_env']}' not "
                  f"found in .env - falling back to NET_SECRET.")
        secret = val or CREDS["secret"]
    username = CREDS["username"]
    if sw.get("username_env"):
        username = os.getenv(sw["username_env"]) or CREDS["username"]
    password = CREDS["password"]
    if sw.get("password_env"):
        password = os.getenv(sw["password_env"]) or CREDS["password"]

    result = {"name": name, "host": sw["host"], "error": None}

    # Guard: Netmiko calls .rstrip() on creds, so None -> cryptic crash.
    # Fail clearly instead.
    missing = [n for n, v in (("username", username), ("password", password),
                              ("secret", secret)) if not v]
    if missing:
        result["error"] = (f"missing credential(s): {', '.join(missing)} "
                           f"- check .env (NET_USERNAME/NET_PASSWORD/NET_SECRET "
                           f"and any secret_env), and that it loaded.")
        return result

    device = {
        "device_type": sw.get("device_type", "cisco_ios"),
        "host": sw["host"],
        "username": username,
        "password": password,
        "secret": secret,
        "fast_cli": False,
        # Connection-level timing: the 'Pattern not detected' during session
        # setup (not send_command) is governed by these, NOT read_timeout.
        # A login banner + slow prompt can need longer here.
        "conn_timeout": 20,
        "banner_timeout": 30,
        "auth_timeout": 30,
        "global_delay_factor": 2,
        # Disable command-echo verification. A login banner or unusual prompt
        # echo can make Netmiko's verification fail during session setup
        # (symptom: 'Pattern not detected' / stuck at '>'). This is a common,
        # safe fix for devices that reach '#' manually but stall under Netmiko.
        "global_cmd_verify": False,
    }
    # Optional per-device session log for diagnosing prompt/paging issues.
    if os.getenv("RACK_SESSION_LOG"):
        safe_h = str(sw["host"]).replace(":", "-").replace("/", "-")
        device["session_log"] = str(OUTPUT_DIR / f"session_{safe_h}.log")

    try:
        with ConnectHandler(**device) as conn:
            # Enter enable mode. If this fails (e.g. wrong enable secret), the
            # device stays at '>' and later commands time out looking for '#'.
            # Catch it and report a clear, actionable error.
            try:
                conn.enable()
            except Exception as e:  # noqa: BLE001
                result["error"] = (f"enable failed ({e}) - check this device's "
                                   f"enable secret (secret_env in rack_inventory.yaml "
                                   f"-> matching var in .env).")
                return result
            if ">" in conn.find_prompt():
                result["error"] = ("stuck in user mode (>) - enable secret likely "
                                   "wrong for this device. Fix secret_env / .env.")
                return result
            # Belt-and-suspenders: make sure paging is off (a '--More--' pager
            # is the usual cause of "Pattern not detected" prompt timeouts).
            try:
                conn.send_command_timing("terminal length 0")
            except Exception:  # noqa: BLE001
                pass

            def grab(cmd, timeout=180):
                """Run a show command; on timeout/err, log and return [] so one
                slow command doesn't abort the whole switch's discovery."""
                try:
                    return conn.send_command(cmd, use_textfsm=True,
                                             read_timeout=timeout)
                except Exception as e:  # noqa: BLE001
                    result.setdefault("cmd_errors", []).append(f"{cmd}: {e}")
                    return []

            result["mac"] = grab("show mac address-table")
            result["arp"] = grab("show ip arp")
            result["status"] = grab("show interfaces status")
            result["ipbrief"] = grab("show ip interface brief")
            result["version"] = grab("show version")
            result["intdesc"] = grab("show interfaces description")
            # CDP first, LLDP fallback
            cdp = grab("show cdp neighbors detail")
            result["cdp"] = cdp if isinstance(cdp, list) else []
            lldp = grab("show lldp neighbors detail")
            result["lldp"] = lldp if isinstance(lldp, list) else []
    except NetmikoAuthenticationException:
        result["error"] = "authentication failed"
    except NetmikoTimeoutException:
        result["error"] = "unreachable (timeout)"
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "incompatible version" in msg.lower() or "1.5" in msg:
            result["error"] = ("device only offers SSHv1 - enable SSHv2 on the "
                               "switch: 'ip ssh version 2' (regen RSA key if needed)")
        else:
            result["error"] = msg
    return result


# --------------------------------------------------------------------------
# 2. MAP (per switch port map)
# --------------------------------------------------------------------------
def _norm_ifname(name: str) -> str:
    """Normalize interface names so Gi1/0/1, GigabitEthernet1/0/1, Gi 1/0/1
    all compare equal. Returns a canonical short form like 'Gi1/0/1'."""
    if not name:
        return ""
    s = str(name).strip().replace(" ", "")
    # expand/compress common prefixes to a short canonical form
    repl = [
        ("TenGigabitEthernet", "Te"), ("GigabitEthernet", "Gi"),
        ("FastEthernet", "Fa"), ("TwoGigabitEthernet", "Tw"),
        ("Ethernet", "Eth"), ("Te", "Te"), ("Gi", "Gi"),
        ("Fa", "Fa"), ("Eth", "Eth"),
    ]
    low = s
    for long, short in repl:
        if low.lower().startswith(long.lower()):
            return short + s[len(long):]
    return s


def build_router_portmap(raw: dict) -> list:
    """Router port map: label each interface with its DESCRIPTION (from
    'show interfaces description') appended with its IP (from
    'show ip interface brief'). Maps ALL interfaces, including shutdown.

    kinds used:
      - routed     : up interface (shows description + IP)
      - notconnect : operationally down
      - shutdown   : administratively down
    """
    desc_rows = raw.get("intdesc") if isinstance(raw.get("intdesc"), list) else []
    ipbrief_rows = raw.get("ipbrief") if isinstance(raw.get("ipbrief"), list) else []

    # interface -> IP (and admin/oper status) from 'show ip interface brief'
    ip_by_if = {}
    status_by_if = {}
    for b in ipbrief_rows:
        iface = _norm_ifname(b.get("interface") or b.get("intf") or "")
        if not iface:
            continue
        ip = (b.get("ip_address") or b.get("ipaddr") or "").strip()
        ip_by_if[iface] = "" if ip.lower() in ("unassigned", "none", "") else ip
        status_by_if[iface] = (b.get("status") or "").lower()

    # interface -> description/status from 'show interfaces description'
    desc_by_if = {}
    dstatus_by_if = {}
    for d in desc_rows:
        iface = _norm_ifname(d.get("port") or d.get("interface") or d.get("intf") or "")
        if not iface:
            continue
        desc_by_if[iface] = (d.get("description") or d.get("descrip") or "").strip()
        dstatus_by_if[iface] = (d.get("status") or "").lower()

    all_ifs = set(ip_by_if) | set(desc_by_if)
    out = []
    for iface in sorted(all_ifs, key=_port_sort_key):
        if not PHYS_PORT_RE.match(iface):
            continue
        ip = ip_by_if.get(iface, "")
        desc = desc_by_if.get(iface, "")
        st = status_by_if.get(iface) or dstatus_by_if.get(iface) or ""

        if "admin" in st or "administratively" in st:
            kind = "shutdown"
        elif st in ("down", "notconnect") or dstatus_by_if.get(iface) == "down":
            kind = "notconnect"
        else:
            kind = "routed"

        out.append({
            "port": iface,
            "kind": kind,
            "status": st,
            "vlan": "",
            "description": desc,
            "ip": ip,          # router-specific field used for labeling
            "neighbor": "",
            "neighbor_port": "",
            "mac_count": 0,
            "macs": "",
            "ips": ip,         # keep 'ips' populated too for the CSV
            "vendor": "",
        })
    return out


def build_portmap(raw: dict) -> list:
    """Build a COMPLETE per-port map for one switch.

    Master list of ports comes from 'show interfaces status'. Each port is
    then enriched and classified into a 'kind':
      - neighbor   : CDP/LLDP shows a device -> label with neighbor (MAC ignored)
      - access     : has learned MAC(s), no CDP neighbor -> MAC/IP/vendor
      - connected  : up/connected but no MAC and no neighbor
      - notconnect : operationally down (nothing plugged in)
      - shutdown   : administratively down (from 'show ip interface brief')
    EVERY physical port is included.

    If the device is a ROUTER (per show version), uses a different labeling:
    port description (from 'show interfaces description') + IP (from
    'show ip interface brief'). See build_router_portmap().
    """
    if _is_router(raw):
        return build_router_portmap(raw)

    mac_rows = raw.get("mac") if isinstance(raw.get("mac"), list) else []
    arp_rows = raw.get("arp") if isinstance(raw.get("arp"), list) else []
    status_rows = raw.get("status") if isinstance(raw.get("status"), list) else []
    ipbrief_rows = raw.get("ipbrief") if isinstance(raw.get("ipbrief"), list) else []
    cdp_rows = (raw.get("cdp") or []) + (raw.get("lldp") or [])

    # --- MAC -> IP from ARP ---
    mac_to_ip = {}
    for a in arp_rows:
        m = _norm_mac(a.get("mac") or a.get("mac_address") or a.get("hardware_addr", ""))
        ip = a.get("address") or a.get("ip_address", "")
        if m:
            mac_to_ip[m] = ip

    # --- MACs grouped by (normalized) port ---
    macs_by_port = defaultdict(list)
    for m in mac_rows:
        port = m.get("destination_port") or m.get("ports") or m.get("port")
        if isinstance(port, list):
            port = port[0] if port else ""
        mac = m.get("destination_address") or m.get("mac")
        vlan = str(m.get("vlan", ""))
        if port and mac:
            macs_by_port[_norm_ifname(port)].append((mac, vlan))

    # --- CDP/LLDP neighbor by (normalized) local port ---
    neighbor_by_port = {}
    for n in cdp_rows:
        local = (n.get("local_port") or n.get("local_interface")
                 or n.get("local_intf") or "")
        nbr = (n.get("destination_host") or n.get("neighbor")
               or n.get("neighbor_name") or n.get("device_id") or "")
        rport = (n.get("remote_port") or n.get("neighbor_interface")
                 or n.get("port_id") or "")
        platform = (n.get("platform") or n.get("capabilities") or "")
        if local:
            neighbor_by_port[_norm_ifname(local)] = {
                "name": nbr, "rport": rport, "platform": platform,
            }

    # --- admin-down (shutdown) ports from 'show ip interface brief' ---
    # Robust: template field is 'status' with value 'administratively down',
    # but be defensive about key/value variations across IOS versions.
    shutdown_ports = set()
    for b in ipbrief_rows:
        iface = (b.get("interface") or b.get("intf") or b.get("port") or "")
        status = (b.get("status") or b.get("link_status") or "").lower()
        proto = (b.get("proto") or b.get("protocol") or "").lower()
        # 'administratively down' (any abbreviation), or status 'down' flagged admin
        if iface and ("admin" in status or "administratively" in status
                      or status == "disabled"):
            shutdown_ports.add(_norm_ifname(iface))

    # --- status/VLAN/description per port (the MASTER port list) ---
    def classify_status(st: str) -> str:
        st = (st or "").lower()
        if "connect" in st and "notconnect" not in st and "not " not in st:
            return "connected"
        if "notconnect" in st or st in ("down", "notconnected"):
            return "notconnect"
        if "disable" in st or "err" in st:
            return "disabled"
        return st or ""

    out = []
    seen = set()

    def add_port(port_norm, raw_status="", vlan="", desc=""):
        if not port_norm or not PHYS_PORT_RE.match(port_norm):
            return
        if port_norm in seen:
            return
        seen.add(port_norm)

        nbr = neighbor_by_port.get(port_norm)
        macs = macs_by_port.get(port_norm, [])
        is_shut = port_norm in shutdown_ports
        st = classify_status(raw_status)

        # classify kind (precedence: shutdown > neighbor > access > connected > notconnect)
        if is_shut or st == "disabled":
            kind = "shutdown"
        elif nbr:
            kind = "neighbor"
        elif macs:
            kind = "access"
        elif st == "connected":
            kind = "connected"
        else:
            kind = "notconnect"

        row = {
            "port": port_norm,
            "kind": kind,
            "status": raw_status,
            "vlan": vlan or (macs[0][1] if macs else ""),
            "description": desc,
            "neighbor": "",
            "neighbor_port": "",
            "mac_count": 0,
            "macs": "",
            "ips": "",
            "vendor": "",
        }

        if kind == "neighbor":
            row["neighbor"] = nbr["name"]
            row["neighbor_port"] = nbr["rport"]
            # CDP wins -> MAC intentionally ignored for this port
        elif kind == "access":
            mlist = [m[0] for m in macs]
            ips = [mac_to_ip.get(_norm_mac(mc), "") for mc in mlist]
            vendors = []
            for mc in mlist:
                v = _vendor(mc)
                if v and v not in vendors:
                    vendors.append(v)
            row["mac_count"] = len(mlist)
            row["macs"] = " ".join(mlist)
            row["ips"] = " ".join(ip for ip in ips if ip)
            row["vendor"] = "; ".join(vendors)
        out.append(row)

    # Build a per-port metadata map from 'show interfaces status' first
    # (richer: vlan, description, connected/notconnect), keyed by norm name.
    status_meta = {}
    for s in status_rows:
        port = _norm_ifname(s.get("port") or s.get("interface") or "")
        if port:
            status_meta[port] = {
                "vlan": str(s.get("vlan", s.get("vlan_id", ""))),
                "desc": s.get("name", s.get("description", "")),
                "status": s.get("status", ""),
            }

    # 1) MASTER LIST from 'show ip interface brief' - lists EVERY interface,
    #    including administratively-down (shutdown) ones. Enrich from status.
    for b in ipbrief_rows:
        iface = _norm_ifname(b.get("interface") or b.get("intf") or "")
        if not iface:
            continue
        meta = status_meta.get(iface, {})
        # prefer the status-table status (connected/notconnect/disabled) when
        # present; else derive from ip-brief (admin-down -> shutdown).
        ib_status = (b.get("status") or "").lower()
        raw_status = meta.get("status") or (
            "administratively down" if "admin" in ib_status else ib_status)
        add_port(iface, raw_status=raw_status,
                 vlan=meta.get("vlan", ""), desc=meta.get("desc", ""))

    # 2) also add any ports from 'show interfaces status' not already added
    #    (covers cases where ip-brief is sparse).
    for s in status_rows:
        port = _norm_ifname(s.get("port") or s.get("interface") or "")
        add_port(port, raw_status=s.get("status", ""),
                 vlan=str(s.get("vlan", s.get("vlan_id", ""))),
                 desc=s.get("name", s.get("description", "")))

    # 3) make sure every detected shutdown port is present
    for p in shutdown_ports:
        add_port(p, raw_status="administratively down")

    # 4) safety net: any port that had a MAC or neighbor but wasn't listed
    for p in list(macs_by_port.keys()) + list(neighbor_by_port.keys()):
        add_port(p)

    out.sort(key=lambda r: _port_sort_key(r["port"]))
    return out


# --------------------------------------------------------------------------
# 3. INTERCONNECT detection (CDP/LLDP)
# --------------------------------------------------------------------------
def detect_interconnects(switches: list) -> list:
    """Given discovered switches, find links BETWEEN switches in this rack.
    Returns list of dicts: {a_name, a_port, b_name, b_port, via}.
    Matches a neighbor's device-id/host against the rack's switch names/hosts."""
    # Build lookup of identifiers that belong to switches in THIS rack.
    ident = {}
    for s in switches:
        for key in (s["name"], s["host"]):
            if key:
                ident[key.lower()] = s["name"]

    links = []
    seen = set()
    for s in switches:
        neighbors = (s.get("cdp") or []) + (s.get("lldp") or [])
        for n in neighbors:
            nbr_id = (n.get("destination_host") or n.get("neighbor")
                      or n.get("neighbor_name") or n.get("device_id") or "")
            nbr_ip = (n.get("management_ip") or n.get("mgmt_address")
                      or n.get("interface_ip") or "")
            local_port = (n.get("local_port") or n.get("local_interface") or "")
            remote_port = (n.get("remote_port") or n.get("neighbor_interface")
                           or n.get("port_id") or "")
            # does this neighbor match another switch in the rack?
            match = None
            for cand in (nbr_id.lower(), nbr_ip.lower()):
                # match on exact or hostname-prefix
                for ident_key, ident_name in ident.items():
                    if cand and (cand == ident_key or cand.startswith(ident_key)
                                 or ident_key in cand):
                        if ident_name != s["name"]:
                            match = ident_name
                            break
                if match:
                    break
            if match:
                # de-dup (A-B same as B-A)
                pair = tuple(sorted([f"{s['name']}:{local_port}",
                                     f"{match}:{remote_port}"]))
                if pair not in seen:
                    seen.add(pair)
                    links.append({
                        "a_name": s["name"], "a_port": local_port,
                        "b_name": match, "b_port": remote_port,
                        "via": "CDP/LLDP",
                    })
    return links


# --------------------------------------------------------------------------
# 4. DIAGRAM (rack-style, stacked vertically)
# --------------------------------------------------------------------------
def render_rack(rack_name, switches_map, links, out_png):
    """switches_map: list of (name, host, portmap-list). Stacked top->bottom."""
    n_sw = len(switches_map)
    # layout
    sw_h = 2.0
    gap = 2.4
    width = 16
    height = n_sw * (sw_h + gap) + 3
    fig, ax = plt.subplots(figsize=(width, max(6, height * 0.7)))
    ax.set_xlim(0, 20)
    ax.set_ylim(0, height)
    ax.axis("off")
    ax.text(10, height - 0.6, f"Rack / Floor Consolidation - {rack_name}",
            ha="center", fontsize=15, weight="bold")
    ax.text(10, height - 1.3,
            f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} - "
            f"{n_sw} switch(es), {len(links)} interconnect(s) detected",
            ha="center", fontsize=8.5, style="italic", color="#555")

    # position each switch band
    centers = {}
    for i, entry in enumerate(switches_map):
        name, host, portmap = entry[0], entry[1], entry[2]
        facts_str = entry[3] if len(entry) > 3 else ""
        top = height - 2.4 - i * (sw_h + gap)
        y = top - sw_h
        # switch band
        ax.add_patch(FancyBboxPatch((1, y), 18, sw_h,
                     boxstyle="round,pad=0.1,rounding_size=0.2",
                     fc="#eef4ff", ec="#333", lw=1.4))
        ax.text(1.4, y + sw_h - 0.35,
                f"{name}  ({host})" + (f"  -  {facts_str}" if facts_str else ""),
                fontsize=10, weight="bold", va="top")
        centers[name] = (y + sw_h / 2)
        # port cells along the band
        ports = portmap[:48]  # cap for readability
        if ports:
            cell_w = 17.4 / max(len(ports), 1)
            for j, p in enumerate(ports):
                px = 1.3 + j * cell_w
                fill, _stroke, lines = _port_style_and_lines(p)
                ax.add_patch(Rectangle((px, y + 0.15), cell_w * 0.9, sw_h - 0.6,
                             fc=fill, ec="#999", lw=0.5))
                short = re.sub(r"[A-Za-z]+", lambda m: m.group()[:2], p["port"])
                ax.text(px + cell_w * 0.45, y + sw_h - 0.5, short,
                        ha="center", fontsize=4.0, weight="bold")
                # second line: neighbor name / MAC tail / SHUTDOWN / status
                if p["kind"] == "neighbor":
                    sub = (p.get("neighbor") or "")[:10]
                elif p["kind"] == "access":
                    sub = (p["macs"].split(" ")[0][-8:] if p["macs"] else "")
                elif p["kind"] == "shutdown":
                    sub = "SHUT"
                elif p["kind"] == "connected":
                    sub = "conn"
                else:
                    sub = "notconn"
                ax.text(px + cell_w * 0.45, y + sw_h - 0.85, sub,
                        ha="center", fontsize=3.4)
        else:
            ax.text(10, y + sw_h / 2, "(no ports returned / switch unreachable)",
                    ha="center", fontsize=8, color="#999", style="italic")

    # interconnect lines between switch bands
    for lk in links:
        ya = centers.get(lk["a_name"])
        yb = centers.get(lk["b_name"])
        if ya is None or yb is None:
            continue
        ax.add_patch(FancyArrowPatch((19.3, ya), (19.3, yb), arrowstyle="-",
                     color="#2e7d32", lw=2.5,
                     connectionstyle="arc3,rad=0.3"))
        ax.text(19.8, (ya + yb) / 2,
                f"{lk['a_port']}\n<->\n{lk['b_port']}",
                fontsize=6, color="#2e7d32", va="center", ha="left",
                bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="#2e7d32"))

    # legend (bottom) - matches the kind-based port colours
    legend_items = [
        ("#dae8fc", "Trunk / uplink (CDP neighbor)"),
        ("#d5e8d4", "Access / routed (endpoint or L3)"),
        ("#f5f5f5", "Connected (up, no MAC)"),
        ("#ffffff", "Not connected"),
        ("#f8cecc", "Shutdown (admin down)"),
    ]
    lx = 1.0
    for fc, text in legend_items:
        ax.add_patch(Rectangle((lx, 0.3), 0.6, 0.5, fc=fc, ec="#333"))
        ax.text(lx + 0.75, 0.55, text, fontsize=6.5, va="center")
        lx += 0.75 + len(text) * 0.11 + 0.6
    ax.plot([lx, lx + 0.8], [0.55, 0.55], color="#2e7d32", lw=2.5)
    ax.text(lx + 0.95, 0.55, "Interconnect (CDP/LLDP)", fontsize=6.5, va="center")

    plt.tight_layout()
    plt.savefig(out_png, dpi=150, bbox_inches="tight")
    plt.close(fig)


def write_combined_csv(rack_name, switches_map, out_csv):
    fields = ["switch", "host", "port", "kind", "status", "vlan", "description",
              "neighbor", "neighbor_port", "mac_count", "macs", "ips", "vendor"]
    with open(out_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for entry in switches_map:
            name, host, portmap = entry[0], entry[1], entry[2]
            for p in portmap:
                w.writerow({"switch": name, "host": host, **p})


# --------------------------------------------------------------------------
# 4b. DRAW.IO (editable, Visio-exportable) output
# --------------------------------------------------------------------------
def _xml_escape(s: str) -> str:
    s = str(s)
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _label(s: str) -> str:
    """Escape for XML AND convert newlines to draw.io line breaks (&#10;).
    Use this for any cell VALUE that should show multiple lines."""
    return _xml_escape(s).replace("\n", "&#10;")


# Colour scheme by port 'kind' (fill, stroke).
_KIND_COLORS = {
    "neighbor":   ("#dae8fc", "#6c8ebf"),   # blue  - uplink/trunk to a device
    "access":     ("#d5e8d4", "#82b366"),   # green - endpoint with MAC
    "connected":  ("#f5f5f5", "#999999"),   # grey  - up, no MAC/neighbor
    "notconnect": ("#ffffff", "#cccccc"),   # white - nothing plugged in
    "shutdown":   ("#f8cecc", "#b85450"),   # red   - administratively down
    "routed":     ("#d5e8d4", "#82b366"),   # green - router L3 interface (desc+IP)
}


def _device_facts(raw: dict) -> dict:
    """Extract hostname / model / version from parsed 'show version'."""
    ver = raw.get("version")
    facts = {"hostname": "", "model": "", "version": ""}
    if isinstance(ver, list) and ver:
        v = ver[0]
        facts["hostname"] = v.get("hostname", "")
        facts["model"] = (v.get("hardware") or v.get("platform")
                          or v.get("model") or "")
        if isinstance(facts["model"], list):
            facts["model"] = facts["model"][0] if facts["model"] else ""
        facts["version"] = v.get("version", "")
    return facts


# Model-string tokens that indicate a ROUTER platform.
_ROUTER_HINTS = ("ISR", "ASR", "CISCO19", "CISCO29", "CISCO39", "C8200",
                 "C8300", "C8500", "C1100", "C1000-", "4321", "4331", "4351",
                 "4431", "4451", "1941", "2901", "2911", "2921", "2951",
                 "3925", "3945", "Router", "ROUTER")
_SWITCH_HINTS = ("Catalyst", "WS-C", "C9200", "C9300", "C9400", "C9500",
                 "C3850", "C3650", "C2960", "Nexus", "N9K", "N5K", "N7K")


def _is_router(raw: dict) -> bool:
    """Decide if a device is a ROUTER (vs switch).
    1) model-string hints from 'show version'
    2) heuristic fallback: no switch MAC table AND ip-brief has IP'd interfaces.
    """
    model = (_device_facts(raw).get("model") or "").upper()
    for h in _SWITCH_HINTS:
        if h.upper() in model:
            return False
    for h in _ROUTER_HINTS:
        if h.upper() in model:
            return True
    # Heuristic: switches return a MAC address-table / interfaces status;
    # routers typically don't, but have IP-addressed interfaces.
    mac = raw.get("mac")
    status = raw.get("status")
    ipbrief = raw.get("ipbrief") if isinstance(raw.get("ipbrief"), list) else []
    has_switch_tables = (isinstance(mac, list) and mac) or (isinstance(status, list) and status)
    has_ips = any((b.get("ip_address") or "").lower() not in ("", "unassigned", "none")
                  for b in ipbrief)
    return (not has_switch_tables) and has_ips


def _port_style_and_lines(p: dict):
    """Return (fill, stroke, [label lines]) for a port based on its 'kind'."""
    kind = p.get("kind", "notconnect")
    fill, stroke = _KIND_COLORS.get(kind, _KIND_COLORS["notconnect"])
    port = p["port"]
    vlan = p.get("vlan", "")
    desc = p.get("description", "")

    if kind == "routed":
        # Router L3 interface: label = description, appended with the IP.
        ip = p.get("ip", "") or (p.get("ips", "").split(" ")[0] if p.get("ips") else "")
        label = desc if desc else "(no description)"
        if ip:
            label = f"{label} ({ip})"
        lines = [port, label]
    elif kind == "neighbor":
        lines = [port, "-> " + (p.get("neighbor") or "neighbor"),
                 p.get("neighbor_port", ""), desc]
    elif kind == "access":
        mac0 = (p["macs"].split(" ")[0] if p["macs"] else "-")
        ip0 = (p["ips"].split(" ")[0] if p["ips"] else "-")
        # Prefer the friendly device name from the central MAC database;
        # fall back to the OUI vendor if the MAC isn't in the reference.
        device = _device_for_mac(mac0)
        label = device if device else (p["vendor"].split(";")[0] if p["vendor"] else "-")
        lines = [port, f"VLAN {vlan}" if vlan else "", label[:18], mac0, ip0]
        if p.get("mac_count", 0) > 1:
            lines.append(f"x{p['mac_count']} MACs")
    elif kind == "shutdown":
        lines = [port, "SHUTDOWN", desc]
    elif kind == "connected":
        lines = [port, f"VLAN {vlan}" if vlan else "", "connected", desc]
    else:  # notconnect
        lines = [port, "notconnect", desc]
    return fill, stroke, lines


def write_drawio(rack_name, switches_map, links, out_path):
    """Emit a draw.io (.drawio) file with Cisco stencil switch icons.
    Opens in diagrams.net / the draw.io VS Code extension, and exports to
    Visio (.vsdx) via File > Export.

    Layout: switches stacked vertically (rack style). Each switch row has a
    Cisco switch ICON (left) plus a port-grid container (right) whose cells
    are colour-coded. Interconnects are orthogonal edges between port cells."""
    cells = []
    nid = [1]  # mutable id counter

    def new_id():
        nid[0] += 1
        return f"n{nid[0]}"

    # Cisco switch stencil (draw.io built-in Cisco shape library).
    CISCO_SWITCH = ("sketch=0;html=1;dashed=0;whiteSpace=wrap;fillColor=#036897;"
                    "strokeColor=#ffffff;shape=mxgraph.cisco19.rect;"
                    "prIcon=l3_switch;")
    CISCO_ROUTER = ("sketch=0;html=1;dashed=0;whiteSpace=wrap;fillColor=#036897;"
                    "strokeColor=#ffffff;shape=mxgraph.cisco19.rect;"
                    "prIcon=router;")

    # geometry
    icon_x = 40
    icon_w = 120
    icon_h = 80
    grid_x = 210
    grid_w = 980
    grid_pad_top = 26        # space for the grid's header label
    row_pad = 50
    port_w = 112             # wider so MAC/IP fit
    port_h = 70              # taller for 4-5 lines
    port_gap = 10

    switch_grid_ids = {}       # name -> port-grid container id
    switch_icon_ids = {}       # name -> icon id
    port_cell_ids = {}         # (switch, port) -> cell id (for edges)

    y = 60
    for entry in switches_map:
        name, host, portmap = entry[0], entry[1], entry[2]
        facts_str = entry[3] if len(entry) > 3 else ""
        is_router = entry[4] if len(entry) > 4 else False
        ports = portmap[:48]
        per_row = max(1, (grid_w - 20) // (port_w + port_gap))
        n_rows = max(1, (len(ports) + per_row - 1) // per_row) if ports else 1
        grid_h = grid_pad_top + n_rows * (port_h + port_gap) + 10
        row_h = max(grid_h, icon_h + 30)

        # --- Cisco device ICON (left): router icon if it's a router, else switch ---
        icon_style = CISCO_ROUTER if is_router else CISCO_SWITCH
        icon_id = new_id()
        switch_icon_ids[name] = icon_id
        dtype = "ROUTER" if is_router else "SWITCH"
        icon_text = f"{name}\n{host}\n[{dtype}]" + (f"\n{facts_str}" if facts_str else "")
        icon_label = _label(icon_text)              # real newline -> &#10;
        iy = y + (row_h - icon_h) / 2
        cells.append(
            f'<mxCell id="{icon_id}" value="{icon_label}" '
            f'style="{icon_style}verticalLabelPosition=bottom;'
            f'verticalAlign=top;fontSize=11;fontStyle=1;" '
            f'vertex="1" parent="1">'
            f'<mxGeometry x="{icon_x}" y="{iy}" width="{icon_w}" height="{icon_h}" as="geometry"/>'
            f'</mxCell>'
        )

        # --- Port-grid container (right) ---
        gid = new_id()
        switch_grid_ids[name] = gid
        cells.append(
            f'<mxCell id="{gid}" value="{_xml_escape(name + " - ports")}" '
            f'style="rounded=1;whiteSpace=wrap;html=1;fillColor=#f8f8f8;'
            f'strokeColor=#b0b0b0;verticalAlign=top;align=left;'
            f'spacingLeft=8;spacingTop=4;fontSize=10;fontColor=#555555;fontStyle=2;" '
            f'vertex="1" parent="1">'
            f'<mxGeometry x="{grid_x}" y="{y}" width="{grid_w}" height="{row_h}" as="geometry"/>'
            f'</mxCell>'
        )
        # link icon -> grid
        lk_id = new_id()
        cells.append(
            f'<mxCell id="{lk_id}" style="endArrow=none;html=1;strokeColor=#036897;'
            f'strokeWidth=1.5;" edge="1" parent="1" source="{icon_id}" target="{gid}">'
            f'<mxGeometry relative="1" as="geometry"/></mxCell>'
        )

        # Port cells inside the grid
        if ports:
            col = 0
            row = 0
            for p in ports:
                pid = new_id()
                port_cell_ids[(name, p["port"])] = pid
                fill, stroke, lines = _port_style_and_lines(p)
                ptext = _label("\n".join(x for x in lines if x))
                px = 10 + col * (port_w + port_gap)
                py = grid_pad_top + row * (port_h + port_gap)
                cells.append(
                    f'<mxCell id="{pid}" value="{ptext}" '
                    f'style="rounded=0;whiteSpace=wrap;html=1;fillColor={fill};'
                    f'strokeColor={stroke};fontSize=8;verticalAlign=top;'
                    f'spacingTop=2;" '
                    f'vertex="1" parent="{gid}">'
                    f'<mxGeometry x="{px}" y="{py}" width="{port_w}" height="{port_h}" as="geometry"/>'
                    f'</mxCell>'
                )
                col += 1
                if col >= per_row:
                    col = 0
                    row += 1
        else:
            empty_id = new_id()
            cells.append(
                f'<mxCell id="{empty_id}" '
                f'value="(no physical ports with learned MACs / unreachable)" '
                f'style="text;html=1;fontSize=10;fontColor=#999999;align=center;" '
                f'vertex="1" parent="{gid}">'
                f'<mxGeometry x="10" y="{grid_pad_top}" width="{grid_w-20}" height="30" as="geometry"/>'
                f'</mxCell>'
            )

        y += row_h + row_pad

    # Interconnect edges between switches (connect the actual port cells)
    for lk in links:
        a_cell = port_cell_ids.get((lk["a_name"], lk["a_port"])) or switch_icon_ids.get(lk["a_name"])
        b_cell = port_cell_ids.get((lk["b_name"], lk["b_port"])) or switch_icon_ids.get(lk["b_name"])
        if not a_cell or not b_cell:
            continue
        eid = new_id()
        elabel = _xml_escape(f"{lk['a_port']} <-> {lk['b_port']}")
        cells.append(
            f'<mxCell id="{eid}" value="{elabel}" '
            f'style="endArrow=none;html=1;strokeColor=#2e7d32;strokeWidth=2;'
            f'fontSize=9;fontColor=#2e7d32;edgeStyle=orthogonalEdgeStyle;'
            f'exitX=1;exitY=0.5;entryX=1;entryY=0.5;" '
            f'edge="1" parent="1" source="{a_cell}" target="{b_cell}">'
            f'<mxGeometry relative="1" as="geometry"/>'
            f'</mxCell>'
        )

    # --- Legend (bottom): explains the port colour coding ---
    legend_y = y + 20
    ax_x = 40
    # header
    hdr_id = new_id()
    cells.append(
        f'<mxCell id="{hdr_id}" value="Legend - port colour coding" '
        f'style="text;html=1;fontSize=12;fontStyle=1;align=left;" vertex="1" parent="1">'
        f'<mxGeometry x="{ax_x}" y="{legend_y}" width="300" height="24" as="geometry"/>'
        f'</mxCell>'
    )
    legend_items = [
        ("#dae8fc", "#6c8ebf", "Trunk / uplink  (CDP neighbor - shows connected device)"),
        ("#d5e8d4", "#82b366", "Access / routed  (endpoint MAC/IP, or router L3 interface)"),
        ("#f5f5f5", "#999999", "Connected  (link up, no MAC learned)"),
        ("#ffffff", "#cccccc", "Not connected  (nothing plugged in)"),
        ("#f8cecc", "#b85450", "Shutdown  (administratively down)"),
    ]
    ly = legend_y + 30
    for fc, sc, text in legend_items:
        sw_id = new_id()
        cells.append(
            f'<mxCell id="{sw_id}" value="" '
            f'style="rounded=0;whiteSpace=wrap;html=1;fillColor={fc};strokeColor={sc};" '
            f'vertex="1" parent="1">'
            f'<mxGeometry x="{ax_x}" y="{ly}" width="30" height="18" as="geometry"/>'
            f'</mxCell>'
        )
        txt_id = new_id()
        cells.append(
            f'<mxCell id="{txt_id}" value="{_xml_escape(text)}" '
            f'style="text;html=1;fontSize=10;align=left;verticalAlign=middle;" '
            f'vertex="1" parent="1">'
            f'<mxGeometry x="{ax_x + 38}" y="{ly}" width="520" height="18" as="geometry"/>'
            f'</mxCell>'
        )
        ly += 24
    # interconnect line sample
    line_id = new_id()
    cells.append(
        f'<mxCell id="{line_id}" value="Interconnect (CDP/LLDP link between devices)" '
        f'style="html=1;endArrow=none;strokeColor=#2e7d32;strokeWidth=2;'
        f'fontSize=10;align=left;verticalLabelPosition=right;labelPosition=right;" '
        f'edge="1" parent="1">'
        f'<mxGeometry relative="1" as="geometry">'
        f'<mxPoint x="{ax_x}" y="{ly + 9}" as="sourcePoint"/>'
        f'<mxPoint x="{ax_x + 30}" y="{ly + 9}" as="targetPoint"/>'
        f'</mxGeometry></mxCell>'
    )

    # Title
    title_id = new_id()
    title = _xml_escape(
        f"Rack / Floor Consolidation - {rack_name}   "
        f"({len(switches_map)} switches, {len(links)} interconnects) - "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )
    cells.insert(0,
        f'<mxCell id="{title_id}" value="{title}" '
        f'style="text;html=1;fontSize=16;fontStyle=1;align=left;" '
        f'vertex="1" parent="1">'
        f'<mxGeometry x="40" y="10" width="960" height="30" as="geometry"/>'
        f'</mxCell>'
    )

    body = "\n        ".join(cells)
    doc = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<mxfile host="app.diagrams.net" type="device">\n'
        f'  <diagram name="{_xml_escape(rack_name)}">\n'
        f'    <mxGraphModel dx="1200" dy="800" grid="1" gridSize="10" '
        f'guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" '
        f'pageScale="1" pageWidth="1400" pageHeight="1000" math="0" shadow="0">\n'
        f'      <root>\n'
        f'        <mxCell id="0"/>\n'
        f'        <mxCell id="1" parent="0"/>\n'
        f'        {body}\n'
        f'      </root>\n'
        f'    </mxGraphModel>\n'
        f'  </diagram>\n'
        f'</mxfile>\n'
    )
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(doc)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def load_inventory():
    with open(INVENTORY, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def main() -> None:
    ap = argparse.ArgumentParser(description="Rack/floor consolidation diagram.")
    ap.add_argument("--rack", help="Rack/floor group name from rack_inventory.yaml")
    ap.add_argument("--list", action="store_true", help="List rack groups and exit.")
    args = ap.parse_args()

    inv = load_inventory()
    racks = inv.get("racks", {})

    if args.list or not args.rack:
        print("Available rack groups:")
        for name, members in racks.items():
            print(f"  {name}  ({len(members)} switches)")
        if not args.rack:
            print("\nRun with --rack <name>")
        return

    if args.rack not in racks:
        print(f"ERROR: rack '{args.rack}' not in inventory. Use --list.")
        return
    if not CREDS["username"]:
        print("ERROR: NET_USERNAME not set in project-root .env.")
        return

    members = racks[args.rack]
    print(f"[1] DISCOVER - {len(members)} switch(es) in {args.rack} (concurrent) ...")
    discovered = []
    with ThreadPoolExecutor(max_workers=min(8, len(members))) as pool:
        futs = {pool.submit(discover_switch, sw): sw for sw in members}
        for f in as_completed(futs):
            r = f.result()
            status = r["error"] or "ok"
            print(f"    {r['name']} ({r['host']}): {status}")
            for ce in r.get("cmd_errors", []):
                print(f"        ! command issue: {ce}")
            discovered.append(r)

    print("[2] MAP - building per-switch port maps ...")
    switches_map = []
    for r in discovered:
        pm = [] if r["error"] else build_portmap(r)
        facts = _device_facts(r) if not r["error"] else {}
        # facts_str: model + version for the diagram label (from 'show version')
        facts_str = ""
        if facts.get("model") or facts.get("version"):
            parts = [facts.get("model", ""), facts.get("version", "")]
            facts_str = " ".join(p for p in parts if p)
        is_router = _is_router(r) if not r["error"] else False
        switches_map.append((r["name"], r["host"], pm, facts_str, is_router))
        if not r["error"]:
            kinds = {}
            for p in pm:
                kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
            dtype = "router" if is_router else "switch"
            print(f"    {r['name']}: {len(pm)} ports {kinds}  ({dtype})"
                  + (f"  [{facts_str}]" if facts_str else ""))

    # Sort routers to the TOP of the rack (routers first), then switches,
    # preserving original order within each group.
    switches_map.sort(key=lambda e: 0 if (len(e) > 4 and e[4]) else 1)

    print("[3] INTERCONNECT - detecting switch-to-switch links (CDP/LLDP) ...")
    reachable = [r for r in discovered if not r["error"]]
    links = detect_interconnects(reachable)
    for lk in links:
        print(f"    {lk['a_name']}:{lk['a_port']} <-> {lk['b_name']}:{lk['b_port']}")
    if not links:
        print("    (none detected - stacked switches or no CDP/LLDP visibility)")

    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", args.rack)
    # Group each rack's outputs into its own subfolder (named after the rack).
    rack_dir = OUTPUT_DIR / safe
    rack_dir.mkdir(parents=True, exist_ok=True)
    out_png = rack_dir / f"{safe}_{ts}.png"
    out_csv = rack_dir / f"{safe}_{ts}.csv"
    out_drawio = rack_dir / f"{safe}_{ts}.drawio"

    print("[4] DIAGRAM - rendering consolidated rack view ...")
    render_rack(args.rack, switches_map, links, out_png)
    write_combined_csv(args.rack, switches_map, out_csv)
    write_drawio(args.rack, switches_map, links, out_drawio)
    print(f"    PNG    -> {out_png}  (quick preview)")
    print(f"    CSV    -> {out_csv}")
    print(f"    drawio -> {out_drawio}  (editable; open in draw.io, export to Visio .vsdx)")
    print("\nDone.")


if __name__ == "__main__":
    main()
