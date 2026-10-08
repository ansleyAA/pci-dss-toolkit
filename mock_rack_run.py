"""Mock run with the NEW complete port mapping (all ports + CDP + shutdown).
Produces PNG + CSV + .drawio in PCIDSS/rack_output/."""
import importlib.util, pathlib
from datetime import datetime
spec = importlib.util.spec_from_file_location("rc", pathlib.Path("rack_consolidation.py"))
rc = importlib.util.module_from_spec(spec); spec.loader.exec_module(rc)

sw1 = {
    "name": "SW_ICT_01", "host": "10.0.0.1", "error": None,
    "version": [{"hostname": "SW_ICT_01", "hardware": "WS-C3850-48P", "version": "16.9.4"}],
    "status": [
        {"port": "Gi1/0/1", "vlan": "10", "status": "connected", "name": "Teller-PC1"},
        {"port": "Gi1/0/2", "vlan": "20", "status": "connected", "name": "Phone"},
        {"port": "Gi1/0/3", "vlan": "10", "status": "connected", "name": "POS-1"},
        {"port": "Gi1/0/4", "vlan": "10", "status": "notconnect", "name": "spare"},
    ],
    "ipbrief": [
        {"interface": "GigabitEthernet1/0/1", "status": "up", "proto": "up"},
        {"interface": "GigabitEthernet1/0/2", "status": "up", "proto": "up"},
        {"interface": "GigabitEthernet1/0/3", "status": "up", "proto": "up"},
        {"interface": "GigabitEthernet1/0/4", "status": "down", "proto": "down"},
        {"interface": "GigabitEthernet1/0/5", "status": "administratively down", "proto": "down"},
        {"interface": "GigabitEthernet1/0/6", "status": "administratively down", "proto": "down"},
        {"interface": "GigabitEthernet1/0/48", "status": "up", "proto": "up"},
    ],
    "mac": [
        {"vlan": "10", "destination_address": "00:1b:44:11:3a:b7", "destination_port": "Gi1/0/1"},
        {"vlan": "10", "destination_address": "00:50:56:12:34:56", "destination_port": "Gi1/0/3"},
        {"vlan": "10", "destination_address": "aa:aa:aa:00:00:01", "destination_port": "Gi1/0/48"},
    ],
    "arp": [
        {"address": "10.1.1.101", "mac": "001b.4411.3ab7"},
        {"address": "10.1.1.103", "mac": "0050.5612.3456"},
    ],
    "cdp": [
        {"local_port": "Gi1/0/48", "destination_host": "SW_ICT_02", "remote_port": "Gi1/0/48", "platform": "WS-C3850"},
        {"local_port": "Gi1/0/2", "destination_host": "SEP-Phone-01", "remote_port": "Port 1", "platform": "Cisco IP Phone"},
    ],
    "lldp": [],
    "ipbrief": [{"interface": "GigabitEthernet1/0/5", "status": "administratively down"}],
}
sw2 = {
    "name": "SW_ICT_02", "host": "10.0.0.2", "error": None,
    "version": [{"hostname": "SW_ICT_02", "hardware": "WS-C2960X-48", "version": "15.2(7)"}],
    "status": [
        {"port": "Gi1/0/1", "vlan": "10", "status": "connected", "name": "Server-1"},
        {"port": "Gi1/0/2", "vlan": "30", "status": "notconnect", "name": ""},
        {"port": "Gi1/0/48", "vlan": "trunk", "status": "connected", "name": "UPLINK-to-ICT01"},
    ],
    "ipbrief": [
        {"interface": "GigabitEthernet1/0/1", "status": "up", "proto": "up"},
        {"interface": "GigabitEthernet1/0/2", "status": "down", "proto": "down"},
        {"interface": "GigabitEthernet1/0/3", "status": "administratively down", "proto": "down"},
        {"interface": "GigabitEthernet1/0/48", "status": "up", "proto": "up"},
    ],
    "mac": [
        {"vlan": "10", "destination_address": "00:50:56:aa:bb:01", "destination_port": "Gi1/0/1"},
        {"vlan": "10", "destination_address": "bb:bb:bb:00:00:01", "destination_port": "Gi1/0/48"},
    ],
    "arp": [{"address": "10.1.1.150", "mac": "0050.56aa.bb01"}],
    "cdp": [
        {"local_port": "Gi1/0/48", "destination_host": "SW_ICT_01", "remote_port": "Gi1/0/48", "platform": "WS-C3850"},
    ],
    "lldp": [],
}

discovered = [sw1, sw2]
rack = "Floor1-RackA-MOCK"
switches_map = []
for r in discovered:
    pm = rc.build_portmap(r)
    f = rc._device_facts(r)
    facts_str = " ".join(x for x in [f.get("model", ""), f.get("version", "")] if x)
    switches_map.append((r["name"], r["host"], pm, facts_str))
for name, host, pm, facts in switches_map:
    kinds = {}
    for p in pm:
        kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
    print(f"{name}: {len(pm)} ports -> {kinds}  [{facts}]")

links = rc.detect_interconnects(discovered)
print("interconnects:", [(l["a_name"], l["a_port"], l["b_name"], l["b_port"]) for l in links])

import re as _re
safe = _re.sub(r"[^A-Za-z0-9._-]", "_", rack)
out = pathlib.Path("rack_output") / safe       # subfolder named after the rack
out.mkdir(parents=True, exist_ok=True)
ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
base = out / f"{safe}_{ts}"
rc.render_rack(rack, switches_map, links, base.with_suffix(".png"))
rc.write_combined_csv(rack, switches_map, base.with_suffix(".csv"))
rc.write_drawio(rack, switches_map, links, base.with_suffix(".drawio"))
print("PNG   ->", base.with_suffix(".png"))
print("CSV   ->", base.with_suffix(".csv"))
print("drawio->", base.with_suffix(".drawio"))
