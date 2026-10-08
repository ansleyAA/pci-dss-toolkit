# PCI DSS Network Discovery & Diagramming Toolkit

A set of Python tools that automate the **network documentation** work required
for PCI DSS — discovering live switch/router state, mapping every switchport to
its connected device, and generating editable network diagrams. Built on
Netmiko, with offline MAC-vendor lookup and draw.io / Visio-exportable output.

> **Why:** PCI DSS Requirement 1 requires accurate, current network and
> data-flow documentation. Maintaining that by hand across many sites is
> error-prone and goes stale. These tools generate it from the live network.

---

## ⚠️ Security first (read before using)

This toolkit connects to production network devices and produces files that
contain **real, sensitive data** (hostnames, IP addresses, MAC addresses,
device identities). Treat it accordingly:

- **Credentials live only in `.env`** (git-ignored) — never hardcoded, never
  committed. Per-device enable secrets are referenced by variable name in the
  inventory, not stored in it.
- **Generated output is git-ignored** (`rack_output/`, `mac_device_reference.csv`,
  `session_*.log`, etc.) — it contains real network data and must not be pushed.
- **The toolkit runs offline** — SSH to devices is on your own network; the only
  internet touch is a one-time MAC OUI database download (see Prerequisites),
  after which it is fully offline. No device data is sent anywhere.
- Review `.gitignore` before every commit. **When in doubt, don't commit it.**

---

## Prerequisites

- **Python 3.10+** (developed/tested on 3.14)
- **Network reachability** from where you run the tools to the target devices
  over SSH (TCP 22). Devices must have **SSHv2** enabled.
- **Device credentials** with privilege to run `show` commands (and enable mode).
- A **virtual environment** is strongly recommended.

### Install

```bash
# 1. clone and enter the repo
git clone <your-repo-url>
cd pci-dss-toolkit

# 2. create + activate a virtual environment
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

# 3. install dependencies
pip install -r requirements.txt

# 4. (one-time, needs internet) build the offline MAC OUI database
python -m mac_vendor_lookup
```

### Configure

```bash
# copy and fill in the templates (both are git-ignored once copied)
cp .env.example .env
cp rack_inventory.example.yaml rack_inventory.yaml
# then edit .env (credentials) and rack_inventory.yaml (your devices)
```

---

## What's in the toolkit

| Script | Purpose |
|--------|---------|
| **`rack_consolidation.py`** | Main tool. Discovers all switches/routers in a rack, maps every port, auto-detects interconnects (CDP/LLDP), and renders a consolidated diagram (PNG + CSV + editable `.drawio`). |
| **`build_mac_database.py`** | Builds a central MAC→device reference CSV from Portnox CLEAR per-switch exports (used to label ports with real device identity). |
| **`process_portnox_export.py`** | Turns a raw Portnox CLEAR `devices.csv` export into clean per-switch port-map CSVs. |
| **`pci_discovery_to_diagram.py`** | Single-switch discover → map → diagram pipeline. |
| **`pci_discovery_demo.py`** | Single-device discovery report (facts, ports, VLANs, MAC/ARP, combined Port→MAC→IP→Vendor). |
| **`mock_rack_run.py`** | Runs the rack pipeline against built-in mock data (no live devices) — for testing/demos. |

---

## Usage

### 1. Rack / floor consolidation diagram (primary tool)

Define your rack groups in `rack_inventory.yaml`, then:

```bash
# list available rack groups
python rack_consolidation.py --list

# generate the diagram for one rack
python rack_consolidation.py --rack ExampleSite-RackA
```

Outputs land in `rack_output/<RackName>/`:
- `<rack>_<timestamp>.png`     — quick preview
- `<rack>_<timestamp>.csv`     — per-port data (switch, port, kind, VLAN, MAC, IP, vendor, neighbor…)
- `<rack>_<timestamp>.drawio`  — **editable** diagram (open in draw.io; export to Visio `.vsdx`)

**What the diagram shows** (every port is mapped):

| Port kind | Meaning | Colour |
|-----------|---------|--------|
| Trunk / uplink | CDP/LLDP neighbor — labeled with the connected device | blue |
| Access / routed | Endpoint (MAC/IP/vendor), or a router L3 interface (description + IP) | green |
| Connected | Link up, no MAC learned | grey |
| Not connected | Nothing plugged in | white |
| Shutdown | Administratively down | red |

Routers (detected via `show version`) are drawn with a **router icon** and
sorted to the **top** of the rack; switches use a switch icon. A legend
explaining the colours is included at the bottom of the diagram.

### 2. Build the MAC → device reference (optional enrichment)

If you have Portnox CLEAR data, build the reference so ports are labeled with
real device names instead of raw MAC/vendor:

```bash
# first, turn a raw CLEAR export into per-switch CSVs
python process_portnox_export.py --input path/to/devices.csv --per-switch

# then build the central reference from those per-switch CSVs
python build_mac_database.py --source portnox_output/per_switch_<timestamp>
```

`rack_consolidation.py` automatically uses `mac_device_reference.csv` if present.

### 3. Try it with no devices (mock)

```bash
python mock_rack_run.py
```

---

## Per-device credentials

Login (username/password) is shared from `.env`. Each device can have its **own
enable secret**:

- In `rack_inventory.yaml`, a device references its secret by variable name:
  ```yaml
  - name: SW-SITE-01
    host: 10.0.0.11
    device_type: cisco_ios
    secret_env: SECRET_SW_SITE_01
  ```
- In `.env`, that variable holds the real secret:
  ```
  SECRET_SW_SITE_01=theRealEnableSecret
  ```

If a device has no `secret_env`, it falls back to `NET_SECRET`. The tool warns
if a referenced secret variable is missing.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| `Incompatible version (1.5 instead of 2.0)` | Device offers SSHv1. Enable SSHv2: `ip ssh version 2` (regen RSA key). |
| `Connection refused` on port 22 | SSH not running — generate the RSA key (`crypto key generate rsa`). |
| `No route to host` / ARP FAILED | Device/interface down or topology link issue; check the interface is up/up. |
| `stuck in user mode (>)` / enable fails | Wrong enable secret — check `secret_env` and the matching `.env` variable. |
| `Pattern not detected (#|>)` | Session-setup/prompt issue; a login banner or paging. The tool sets timeouts + `global_cmd_verify=False` to mitigate. |
| Vendor column blank | OUI DB not built — run `python -m mac_vendor_lookup` once (online). |

Set `RACK_SESSION_LOG=1` to write a per-device SSH session log into the output
folder for deeper diagnosis.

---

## Important caveats

- These tools **generate documentation and evidence** to support PCI DSS
  Requirement 1 — they do **not** make a compliance determination. Validate
  scope and sufficiency with your **QSA**.
- Diagrams are a point-in-time snapshot. Re-run on change / at least annually.
- The MAC→device reference is only as accurate as the source (e.g., Portnox
  profiling). The tool falls back to OUI vendor when a device name isn't known.

---

## License

Internal tool — add your organization's license/usage terms here.
