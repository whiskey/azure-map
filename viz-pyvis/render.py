#!/usr/bin/env python3
"""
pyvis renderer — interactive force-directed network graph.

Reads cached Azure data from ../.az-cache/ and generates an interactive HTML
file using pyvis (vis.js under the hood).

Requires: pip install pyvis

Usage:
  python3 render.py
  python3 render.py --cache-dir /path
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

try:
    from pyvis.network import Network
except ImportError:
    print("ERROR: pyvis not installed. Run: pip install pyvis", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Data loading (same pattern as other renderers)
# ---------------------------------------------------------------------------
CACHE_DIR = Path(__file__).resolve().parent.parent / ".az-cache"


def load(name: str, cache_dir: Path = CACHE_DIR):
    path = cache_dir / f"{name}.json"
    if not path.exists():
        print(f"ERROR: {path} not found. Run az-infra-map.py --fetch first.", file=sys.stderr)
        sys.exit(1)
    with open(path) as f:
        return json.load(f)


def parse_subnet_from_id(sid: str):
    m = re.search(
        r"/subscriptions/([^/]+)/resourceGroups/([^/]+)/providers/"
        r"Microsoft\.Network/virtualNetworks/([^/]+)/subnets/([^/]+)",
        sid, re.IGNORECASE,
    )
    return m.groups() if m else None


def parse_vnet_from_id(rid: str):
    m = re.search(
        r"/subscriptions/([^/]+)/resourceGroups/([^/]+)/providers/"
        r"Microsoft\.Network/virtualNetworks/([^/]+)",
        rid, re.IGNORECASE,
    )
    return m.groups() if m else None


def build_render_model(cache_dir: Path = CACHE_DIR) -> dict:
    subscriptions = load("subscriptions", cache_dir)
    resource_groups = load("resource_groups", cache_dir)
    vnets_raw = load("vnets", cache_dir)
    peerings_raw = load("peerings", cache_dir)
    nics_raw = load("nics", cache_dir)
    vms_raw = load("vms", cache_dir)
    pes_raw = load("private_endpoints", cache_dir)
    nsgs_raw = load("nsgs", cache_dir)

    nic_map = {}
    for r in nics_raw:
        nic_id = (r.get("nicId") or "").lower()
        subnet_id = r.get("subnetId", "")
        if nic_id and subnet_id:
            nic_map[nic_id] = subnet_id

    vnets = {}
    for r in vnets_raw:
        key = f"{r['subscriptionId']}/{r['resourceGroup']}/{r['vnetName']}".lower()
        if key not in vnets:
            addr = r.get("addressSpace", [])
            if isinstance(addr, str):
                addr = [addr]
            vnets[key] = {
                "name": r["vnetName"],
                "rg": r["resourceGroup"],
                "subscription_id": r["subscriptionId"],
                "location": r.get("location", ""),
                "cidrs": addr,
                "subnets": {},
            }
        sn = r.get("subnetName", "")
        if sn:
            vnets[key]["subnets"][sn] = {
                "name": sn,
                "prefix": r.get("subnetPrefix", ""),
                "nsg": None,
                "resources": [],
            }

    for r in vms_raw:
        nic_id = (r.get("nicId") or "").lower()
        subnet_id = nic_map.get(nic_id)
        if not subnet_id:
            continue
        parsed = parse_subnet_from_id(subnet_id)
        if not parsed:
            continue
        sub_id, rg, vnet_name, sn_name = parsed
        vkey = f"{sub_id}/{rg}/{vnet_name}".lower()
        if vkey in vnets and sn_name in vnets[vkey]["subnets"]:
            vnets[vkey]["subnets"][sn_name]["resources"].append(
                {"name": r["vmName"], "type": "VM"}
            )

    for r in pes_raw:
        subnet_id = r.get("subnetId", "")
        if not subnet_id:
            continue
        parsed = parse_subnet_from_id(subnet_id)
        if not parsed:
            continue
        sub_id, rg, vnet_name, sn_name = parsed
        vkey = f"{sub_id}/{rg}/{vnet_name}".lower()
        if vkey in vnets and sn_name in vnets[vkey]["subnets"]:
            vnets[vkey]["subnets"][sn_name]["resources"].append(
                {"name": r.get("name", "pe"), "type": "PrivateEndpoint"}
            )

    for r in nsgs_raw:
        sid = (r.get("subnetId") or "").lower()
        if not sid:
            continue
        parsed = parse_subnet_from_id(sid)
        if not parsed:
            continue
        sub_id, rg, vnet_name, sn_name = parsed
        vkey = f"{sub_id}/{rg}/{vnet_name}".lower()
        if vkey in vnets and sn_name in vnets[vkey]["subnets"]:
            vnets[vkey]["subnets"][sn_name]["nsg"] = r["nsgName"]

    rgs_by_sub = defaultdict(set)
    for rg in resource_groups:
        rgs_by_sub[rg["subscriptionId"]].add(rg["name"])

    rgs_with_vnets = set()
    for v in vnets.values():
        rgs_with_vnets.add(f"{v['subscription_id']}/{v['rg']}".lower())

    model_subs = []
    for sub_id, sub_name in sorted(subscriptions.items(), key=lambda x: x[1]):
        sub_vnets = [v for v in vnets.values() if v["subscription_id"] == sub_id]
        all_rgs = sorted(rgs_by_sub.get(sub_id, set()))
        standalone = [rg for rg in all_rgs if f"{sub_id}/{rg}".lower() not in rgs_with_vnets]
        total_vms = sum(
            len([r for r in sn["resources"] if r["type"] == "VM"])
            for v in sub_vnets for sn in v["subnets"].values()
        )
        model_subs.append({
            "id": sub_id,
            "name": sub_name,
            "vnets": sub_vnets,
            "standalone_rgs": standalone,
            "counts": {
                "vnets": len(sub_vnets),
                "subnets": sum(len(v["subnets"]) for v in sub_vnets),
                "vms": total_vms,
                "rgs": len(all_rgs),
            },
        })

    seen = set()
    model_peerings = []
    for r in peerings_raw:
        remote = parse_vnet_from_id(r.get("remoteVnet", ""))
        if not remote:
            continue
        r_sub, r_rg, r_vnet = remote
        src_key = f"{r['subscriptionId']}/{r['resourceGroup']}/{r['vnetName']}".lower()
        dst_key = f"{r_sub}/{r_rg}/{r_vnet}".lower()
        pair = tuple(sorted([src_key, dst_key]))
        if pair in seen:
            continue
        seen.add(pair)
        model_peerings.append({
            "source": src_key,
            "target": dst_key,
            "source_vnet": r["vnetName"],
            "target_vnet": r_vnet,
            "state": r.get("peeringState", "Unknown"),
        })

    return {"subscriptions": model_subs, "peerings": model_peerings, "vnets_index": vnets}


# ---------------------------------------------------------------------------
# Color palette
# ---------------------------------------------------------------------------
SUB_COLORS = [
    "#4285f4", "#ea4335", "#34a853", "#fbbc05",
    "#9c27b0", "#00bcd4", "#ff5722",
]


# ---------------------------------------------------------------------------
# pyvis graph generation
# ---------------------------------------------------------------------------
def generate_pyvis(model: dict) -> Network:
    net = Network(
        height="100vh",
        width="100%",
        bgcolor="#1a1a2e",
        font_color="white",
        directed=False,
        notebook=False,
        select_menu=False,
        filter_menu=True,
    )

    # Physics options for a clean layout
    net.set_options("""
    {
      "physics": {
        "forceAtlas2Based": {
          "gravitationalConstant": -100,
          "centralGravity": 0.01,
          "springLength": 200,
          "springConstant": 0.02,
          "damping": 0.4
        },
        "solver": "forceAtlas2Based",
        "stabilization": {
          "iterations": 200
        }
      },
      "interaction": {
        "hover": true,
        "tooltipDelay": 100,
        "navigationButtons": true
      },
      "edges": {
        "smooth": {
          "type": "continuous"
        }
      }
    }
    """)

    vnet_key_to_node = {}

    for i, sub in enumerate(model["subscriptions"]):
        color = SUB_COLORS[i % len(SUB_COLORS)]
        sub_node = f"sub:{sub['id'][:8]}"
        counts = sub["counts"]

        # Subscription node
        net.add_node(
            sub_node,
            label=sub["name"],
            title=(
                f"<b>{sub['name']}</b><br>"
                f"ID: {sub['id']}<br>"
                f"VNets: {counts['vnets']}<br>"
                f"Subnets: {counts['subnets']}<br>"
                f"VMs: {counts['vms']}<br>"
                f"Resource Groups: {counts['rgs']}"
            ),
            color=color,
            size=40,
            shape="box",
            font={"size": 16, "face": "monospace", "color": "white"},
            borderWidth=3,
            group=sub["name"],
        )

        # VNet nodes
        for vnet in sub["vnets"]:
            vkey = f"{sub['id']}/{vnet['rg']}/{vnet['name']}".lower()
            vnode = f"vnet:{vnet['name']}"
            vnet_key_to_node[vkey] = vnode

            sn_count = len(vnet["subnets"])
            res_count = sum(len(sn["resources"]) for sn in vnet["subnets"].values())
            cidr_str = ", ".join(vnet["cidrs"])

            net.add_node(
                vnode,
                label=f"{vnet['name']}\n{cidr_str}",
                title=(
                    f"<b>{vnet['name']}</b><br>"
                    f"RG: {vnet['rg']}<br>"
                    f"CIDR: {cidr_str}<br>"
                    f"Location: {vnet['location']}<br>"
                    f"Subnets: {sn_count}<br>"
                    f"Resources: {res_count}"
                ),
                color={"background": "#2d5a27", "border": "#34a853"},
                size=30,
                shape="box",
                font={"size": 12, "face": "monospace", "color": "white"},
                group=sub["name"],
            )

            # Sub -> VNet containment edge
            net.add_edge(sub_node, vnode, color="#555555", width=1, dashes=True)

            # Subnet nodes
            for sn_name, sn in sorted(vnet["subnets"].items()):
                sn_node = f"sn:{vnet['name']}/{sn_name}"
                vm_count = len([r for r in sn["resources"] if r["type"] == "VM"])
                pe_count = len([r for r in sn["resources"] if r["type"] == "PrivateEndpoint"])

                parts = [f"<b>{sn_name}</b><br>Prefix: {sn['prefix']}"]
                if sn.get("nsg"):
                    parts.append(f"NSG: {sn['nsg']}")
                if vm_count:
                    parts.append(f"VMs: {vm_count}")
                if pe_count:
                    parts.append(f"Private Endpoints: {pe_count}")
                if sn["resources"]:
                    # List first 20 resources in tooltip
                    parts.append("<br>Resources:")
                    for r in sn["resources"][:20]:
                        icon = "VM" if r["type"] == "VM" else "PE"
                        parts.append(f"  [{icon}] {r['name']}")
                    if len(sn["resources"]) > 20:
                        parts.append(f"  ...and {len(sn['resources']) - 20} more")

                node_label = sn_name
                if sn["resources"]:
                    node_label += f"\n({len(sn['resources'])})"

                net.add_node(
                    sn_node,
                    label=node_label,
                    title="<br>".join(parts),
                    color={"background": "#1a3a5c", "border": "#4285f4"},
                    size=15 + min(len(sn["resources"]), 30),  # scale by resource count
                    shape="dot",
                    font={"size": 10, "face": "monospace", "color": "#cccccc"},
                    group=sub["name"],
                )

                net.add_edge(vnode, sn_node, color="#333333", width=1, dashes=True)

    # Peering edges
    for p in model["peerings"]:
        src = vnet_key_to_node.get(p["source"])
        dst = vnet_key_to_node.get(p["target"])
        if src and dst:
            label = p["state"] if p["state"] != "Connected" else "peered"
            net.add_edge(
                src, dst,
                color="#ff9800",
                width=3,
                title=f"Peering: {p['source_vnet']} <-> {p['target_vnet']} ({label})",
                label=label,
                font={"size": 10, "color": "#ff9800"},
            )

    return net


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="pyvis interactive network renderer")
    parser.add_argument("--cache-dir", default=None, help="Override cache directory")
    args = parser.parse_args()

    cache_dir = Path(args.cache_dir) if args.cache_dir else CACHE_DIR
    output_dir = Path(__file__).resolve().parent / "output"
    output_dir.mkdir(exist_ok=True)

    print(f"Loading data from {cache_dir}/")
    model = build_render_model(cache_dir)

    print("Building pyvis graph...")
    net = generate_pyvis(model)

    out = output_dir / "azure-infra.html"
    net.save_graph(str(out))
    print(f"  {out}")
    print(f"\nTo view: open {out} in a browser")


if __name__ == "__main__":
    main()
