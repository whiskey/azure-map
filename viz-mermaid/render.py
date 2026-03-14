#!/usr/bin/env python3
"""
Mermaid renderer with detail levels (l1 / l2 / l3).

Reads cached Azure data from ../.az-cache/ and generates Mermaid flowcharts.
  l1 — Subscription overview with aggregate counts and peering edges
  l2 — + VNet subgraphs with CIDR ranges and subnet summaries
  l3 — + Individual VMs, PEs, NSG badges (full detail, like the original)

Usage:
  python3 render.py                      # generates all three levels
  python3 render.py --detail l2          # generate only l2
  python3 render.py --cache-dir /path    # custom cache location
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Data loading
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

    # NIC -> subnet mapping
    nic_map = {}
    for r in nics_raw:
        nic_id = (r.get("nicId") or "").lower()
        subnet_id = r.get("subnetId", "")
        if nic_id and subnet_id:
            nic_map[nic_id] = subnet_id

    # Build VNet/subnet hierarchy
    vnets = {}  # key -> {name, rg, sub, location, cidrs, subnets: {name -> {prefix, resources}}}
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

    # Place VMs into subnets
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

    # Place PEs into subnets
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

    # NSG map
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

    # RGs by subscription
    rgs_by_sub = defaultdict(set)
    for rg in resource_groups:
        rgs_by_sub[rg["subscriptionId"]].add(rg["name"])

    rgs_with_vnets = set()
    for v in vnets.values():
        rgs_with_vnets.add(f"{v['subscription_id']}/{v['rg']}".lower())

    # Build final model
    model_subs = []
    for sub_id, sub_name in sorted(subscriptions.items(), key=lambda x: x[1]):
        sub_vnets = [v for v in vnets.values() if v["subscription_id"] == sub_id]
        all_rgs = sorted(rgs_by_sub.get(sub_id, set()))
        standalone = [rg for rg in all_rgs if f"{sub_id}/{rg}".lower() not in rgs_with_vnets]

        total_vms = sum(
            len([r for r in sn["resources"] if r["type"] == "VM"])
            for v in sub_vnets
            for sn in v["subnets"].values()
        )
        total_subnets = sum(len(v["subnets"]) for v in sub_vnets)

        model_subs.append({
            "id": sub_id,
            "name": sub_name,
            "vnets": sub_vnets,
            "standalone_rgs": standalone,
            "counts": {
                "vnets": len(sub_vnets),
                "subnets": total_subnets,
                "vms": total_vms,
                "rgs": len(all_rgs),
            },
        })

    # Deduplicated peerings
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
        state = r.get("peeringState", "Unknown")
        model_peerings.append({
            "source": src_key,
            "target": dst_key,
            "source_vnet": r["vnetName"],
            "target_vnet": r_vnet,
            "state": state,
        })

    return {"subscriptions": model_subs, "peerings": model_peerings, "vnets_index": vnets}


# ---------------------------------------------------------------------------
# Mermaid helpers
# ---------------------------------------------------------------------------
def sanitize(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", text)


def esc(text: str) -> str:
    return text.replace('"', "'").replace("[", "(").replace("]", ")")


# ---------------------------------------------------------------------------
# Mermaid generation
# ---------------------------------------------------------------------------
def generate_mermaid(model: dict, detail: str = "l3") -> str:
    lines = ["graph TB", ""]
    vnet_node_ids = {}  # vnet_key -> mermaid node id

    for sub in model["subscriptions"]:
        sub_safe = sanitize(sub["id"][:12])
        short_id = sub["id"][:8]

        if detail == "l1":
            # L1: subscription box with counts
            counts = sub["counts"]
            label = (
                f'{esc(sub["name"])}'
                f"<br/><small>{short_id}…</small>"
                f'<br/><small>{counts["vnets"]} VNets · {counts["vms"]} VMs · {counts["rgs"]} RGs</small>'
            )
            lines.append(f'  sub_{sub_safe}["{label}"]')

            # Register a single node per subscription for peering edges
            for vnet in sub["vnets"]:
                vkey = f"{sub['id']}/{vnet['rg']}/{vnet['name']}".lower()
                vnet_node_ids[vkey] = f"sub_{sub_safe}"
        else:
            # L2/L3: subscription as subgraph
            lines.append(
                f'  subgraph sub_{sub_safe}["{esc(sub["name"])}<br/><small>{short_id}…</small>"]'
            )
            lines.append("    direction TB")

            # Group VNets by RG
            vnets_by_rg = defaultdict(list)
            for vnet in sub["vnets"]:
                vnets_by_rg[vnet["rg"]].append(vnet)

            for rg_name, rg_vnets in sorted(vnets_by_rg.items()):
                rg_safe = sanitize(f"{sub_safe}_{rg_name}")
                lines.append(f'    subgraph rg_{rg_safe}["{esc(rg_name)}"]')
                lines.append("      direction TB")

                for vnet in rg_vnets:
                    vkey = f"{sub['id']}/{vnet['rg']}/{vnet['name']}".lower()
                    vnet_safe = sanitize(f"{rg_safe}_{vnet['name']}")
                    vnet_node_ids[vkey] = f"vnet_{vnet_safe}"

                    addr_str = ", ".join(vnet["cidrs"]) if vnet["cidrs"] else ""
                    vnet_label = esc(vnet["name"])
                    if addr_str:
                        vnet_label += f"<br/><small>{esc(addr_str)}</small>"

                    if detail == "l2":
                        # L2: VNet box with subnet count
                        sn_count = len(vnet["subnets"])
                        res_count = sum(len(sn["resources"]) for sn in vnet["subnets"].values())
                        vnet_label += f"<br/><small>{sn_count} subnets · {res_count} resources</small>"
                        lines.append(f'      vnet_{vnet_safe}["{vnet_label}"]')
                    else:
                        # L3: full subgraph with subnets and resources
                        lines.append(f'      subgraph vnet_{vnet_safe}["{vnet_label}"]')
                        lines.append("        direction TB")

                        for sn_name, sn in sorted(vnet["subnets"].items()):
                            sn_safe = sanitize(f"{vnet_safe}_{sn_name}")
                            sn_label = f"{esc(sn_name)}<br/><small>{sn['prefix']}</small>"
                            if sn.get("nsg"):
                                sn_label += f"<br/><small>🛡 {esc(sn['nsg'])}</small>"

                            if sn["resources"]:
                                lines.append(f'        subgraph sn_{sn_safe}["{sn_label}"]')
                                lines.append("          direction LR")
                                for res in sn["resources"]:
                                    res_safe = sanitize(f"{sn_safe}_{res['name']}")
                                    icon = "🖥" if res["type"] == "VM" else "🔒"
                                    lines.append(
                                        f'          {res_safe}["{icon} {esc(res["name"])}"]'
                                    )
                                lines.append("        end")
                            else:
                                lines.append(f'        sn_{sn_safe}["{sn_label}"]')

                        lines.append("      end")

                lines.append("    end")

            # Standalone RGs
            standalone = sub.get("standalone_rgs", [])
            if standalone:
                bucket_safe = sanitize(f"{sub_safe}_other_rgs")
                rg_list = "<br/>".join(esc(r) for r in standalone[:15])
                remaining = len(standalone) - 15
                if remaining > 0:
                    rg_list += f"<br/><small>…and {remaining} more</small>"
                lines.append(f'    {bucket_safe}["{rg_list}"]')

            lines.append("  end")
            lines.append("")

    # Peering edges
    for p in model["peerings"]:
        src_node = vnet_node_ids.get(p["source"])
        dst_node = vnet_node_ids.get(p["target"])
        if src_node and dst_node and src_node != dst_node:
            label = p["state"] if p["state"] != "Connected" else "peered"
            lines.append(f'  {src_node} <--->|"{label}"| {dst_node}')

    # Styling
    lines.append("")
    lines.append("  %% Styling")
    lines.append("  classDef subStyle fill:#e8f0fe,stroke:#4285f4,stroke-width:2px")
    lines.append("  classDef rgStyle fill:#fef7e0,stroke:#f9ab00,stroke-width:1px")
    lines.append("  classDef vnetStyle fill:#e6f4ea,stroke:#34a853,stroke-width:1px")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Mermaid renderer with detail levels")
    parser.add_argument(
        "--detail",
        choices=["l1", "l2", "l3", "all"],
        default="all",
        help="Detail level (default: all — generates l1, l2, l3)",
    )
    parser.add_argument("--cache-dir", default=None, help="Override cache directory")
    args = parser.parse_args()

    cache_dir = Path(args.cache_dir) if args.cache_dir else CACHE_DIR
    output_dir = Path(__file__).resolve().parent / "output"
    output_dir.mkdir(exist_ok=True)

    print(f"Loading data from {cache_dir}/")
    model = build_render_model(cache_dir)

    levels = ["l1", "l2", "l3"] if args.detail == "all" else [args.detail]

    for level in levels:
        mermaid = generate_mermaid(model, detail=level)
        out = output_dir / f"{level}.mermaid"
        with open(out, "w") as f:
            f.write(mermaid)
        print(f"  {out} ({len(mermaid.splitlines())} lines)")

    print("\nTo view: open in VS Code with Mermaid extension, or paste into https://mermaid.live")


if __name__ == "__main__":
    main()
