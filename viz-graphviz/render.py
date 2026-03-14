#!/usr/bin/env python3
"""
Graphviz renderer — DOT → SVG with detail levels (l1 / l2 / l3).

Reads cached Azure data from ../.az-cache/ and generates DOT files rendered
to SVG via the graphviz system binary.
  l1 — Subscription overview (fdp layout) with aggregate counts and peering edges
  l2 — + VNet clusters with CIDR ranges and subnet summaries
  l3 — + Individual VMs, PEs, NSG badges (full detail)

Requires:
  pip install graphviz
  brew install graphviz   (or apt-get install graphviz)

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

try:
    import graphviz
except ImportError:
    print(
        "ERROR: graphviz Python package not installed. Run: pip install graphviz",
        file=sys.stderr,
    )
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


def sanitize(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", text)


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

SUB_BG_COLORS = [
    "#e8f0fe", "#fce8e6", "#e6f4ea", "#fef7e0",
    "#f3e8fd", "#e0f7fa", "#fbe9e7",
]


# ---------------------------------------------------------------------------
# Graphviz generation
# ---------------------------------------------------------------------------
def generate_l1(model: dict) -> graphviz.Graph:
    """L1: Subscription overview with aggregate counts, peering edges."""
    g = graphviz.Graph("azure_infra_l1", engine="fdp", format="svg")
    g.attr(
        bgcolor="#ffffff",
        fontname="Helvetica",
        pad="0.5",
        overlap="false",
        splines="true",
    )
    g.attr("node", fontname="Helvetica", fontsize="11")
    g.attr("edge", fontname="Helvetica", fontsize="9")

    # Map subscription IDs to node names and vnet keys
    sub_node_map = {}  # sub_id -> node_name
    vnet_to_sub_node = {}  # vnet_key -> sub node_name

    for i, sub in enumerate(model["subscriptions"]):
        color = SUB_COLORS[i % len(SUB_COLORS)]
        bg = SUB_BG_COLORS[i % len(SUB_BG_COLORS)]
        node_name = f"sub_{sanitize(sub['id'][:12])}"
        sub_node_map[sub["id"]] = node_name

        counts = sub["counts"]
        label = (
            f"<<b>{_escape_html(sub['name'])}</b>"
            f"<br/><font point-size='9' color='#666666'>{sub['id'][:8]}…</font>"
            f"<br/><font point-size='9'>"
            f"{counts['vnets']} VNets · {counts['subnets']} Subnets · {counts['vms']} VMs"
            f"</font>"
            f"<br/><font point-size='9' color='#888888'>{counts['rgs']} Resource Groups</font>>"
        )

        g.node(
            node_name,
            label=label,
            shape="box",
            style="filled,rounded",
            fillcolor=bg,
            color=color,
            penwidth="2",
            width="3",
            height="1.2",
        )

        for vnet in sub["vnets"]:
            vkey = f"{sub['id']}/{vnet['rg']}/{vnet['name']}".lower()
            vnet_to_sub_node[vkey] = node_name

    # Peering edges (aggregate to subscription level)
    seen_sub_peers = set()
    for p in model["peerings"]:
        src_node = vnet_to_sub_node.get(p["source"])
        dst_node = vnet_to_sub_node.get(p["target"])
        if src_node and dst_node and src_node != dst_node:
            pair = tuple(sorted([src_node, dst_node]))
            if pair in seen_sub_peers:
                continue
            seen_sub_peers.add(pair)
            g.edge(
                src_node, dst_node,
                label="peered",
                style="dashed",
                color="#ff9800",
                penwidth="2",
                fontcolor="#ff9800",
            )

    return g


def generate_l2(model: dict) -> graphviz.Graph:
    """L2: Subscriptions with VNet clusters showing CIDR and subnet summaries."""
    g = graphviz.Graph("azure_infra_l2", engine="fdp", format="svg")
    g.attr(
        bgcolor="#ffffff",
        fontname="Helvetica",
        pad="0.5",
        overlap="false",
        splines="true",
        K="1.5",
    )
    g.attr("node", fontname="Helvetica", fontsize="10", style="filled")
    g.attr("edge", fontname="Helvetica", fontsize="9")

    vnet_node_ids = {}  # vnet_key -> node id

    for i, sub in enumerate(model["subscriptions"]):
        color = SUB_COLORS[i % len(SUB_COLORS)]
        bg = SUB_BG_COLORS[i % len(SUB_BG_COLORS)]
        sub_safe = sanitize(sub["id"][:12])

        with g.subgraph(name=f"cluster_sub_{sub_safe}") as sg:
            sg.attr(
                label=f"<<b>{_escape_html(sub['name'])}</b><br/><font point-size='9'>{sub['id'][:8]}…</font>>",
                style="filled,rounded",
                fillcolor=bg,
                color=color,
                penwidth="2",
                fontname="Helvetica",
            )

            # Group VNets by RG
            vnets_by_rg = defaultdict(list)
            for vnet in sub["vnets"]:
                vnets_by_rg[vnet["rg"]].append(vnet)

            for rg_name, rg_vnets in sorted(vnets_by_rg.items()):
                rg_safe = sanitize(f"{sub_safe}_{rg_name}")

                with sg.subgraph(name=f"cluster_rg_{rg_safe}") as rg_sg:
                    rg_sg.attr(
                        label=f"<<font point-size='10'>{_escape_html(rg_name)}</font>>",
                        style="filled,rounded",
                        fillcolor="#fef7e0",
                        color="#f9ab00",
                        penwidth="1",
                    )

                    for vnet in rg_vnets:
                        vkey = f"{sub['id']}/{vnet['rg']}/{vnet['name']}".lower()
                        vnet_safe = sanitize(f"{rg_safe}_{vnet['name']}")
                        vnet_node = f"vnet_{vnet_safe}"
                        vnet_node_ids[vkey] = vnet_node

                        cidr_str = ", ".join(vnet["cidrs"]) if vnet["cidrs"] else ""
                        sn_count = len(vnet["subnets"])
                        res_count = sum(
                            len(sn["resources"]) for sn in vnet["subnets"].values()
                        )

                        label = (
                            f"<<b>{_escape_html(vnet['name'])}</b>"
                            f"<br/><font point-size='9'>{_escape_html(cidr_str)}</font>"
                            f"<br/><font point-size='9'>{sn_count} subnets · {res_count} resources</font>>"
                        )

                        rg_sg.node(
                            vnet_node,
                            label=label,
                            shape="box",
                            fillcolor="#e6f4ea",
                            color="#34a853",
                            penwidth="1.5",
                        )

            # Standalone RGs
            standalone = sub.get("standalone_rgs", [])
            if standalone:
                bucket_safe = sanitize(f"{sub_safe}_other_rgs")
                rg_list = "\\n".join(standalone[:10])
                remaining = len(standalone) - 10
                if remaining > 0:
                    rg_list += f"\\n…and {remaining} more"
                sg.node(
                    bucket_safe,
                    label=rg_list,
                    shape="note",
                    fillcolor="#f5f5f5",
                    color="#cccccc",
                    fontsize="8",
                )

    # Peering edges
    for p in model["peerings"]:
        src_node = vnet_node_ids.get(p["source"])
        dst_node = vnet_node_ids.get(p["target"])
        if src_node and dst_node:
            label = p["state"] if p["state"] != "Connected" else "peered"
            g.edge(
                src_node, dst_node,
                label=label,
                style="dashed",
                color="#ff9800",
                penwidth="2",
                fontcolor="#ff9800",
                len="2",
            )

    return g


def generate_l3(model: dict) -> graphviz.Graph:
    """L3: Full detail with individual VMs, PEs, NSG badges."""
    g = graphviz.Graph("azure_infra_l3", engine="fdp", format="svg")
    g.attr(
        bgcolor="#ffffff",
        fontname="Helvetica",
        pad="0.5",
        overlap="false",
        splines="true",
        K="1.2",
    )
    g.attr("node", fontname="Helvetica", fontsize="9", style="filled")
    g.attr("edge", fontname="Helvetica", fontsize="8")

    vnet_node_ids = {}

    for i, sub in enumerate(model["subscriptions"]):
        color = SUB_COLORS[i % len(SUB_COLORS)]
        bg = SUB_BG_COLORS[i % len(SUB_BG_COLORS)]
        sub_safe = sanitize(sub["id"][:12])

        with g.subgraph(name=f"cluster_sub_{sub_safe}") as sg:
            sg.attr(
                label=f"<<b>{_escape_html(sub['name'])}</b><br/><font point-size='9'>{sub['id'][:8]}…</font>>",
                style="filled,rounded",
                fillcolor=bg,
                color=color,
                penwidth="2",
                fontname="Helvetica",
            )

            vnets_by_rg = defaultdict(list)
            for vnet in sub["vnets"]:
                vnets_by_rg[vnet["rg"]].append(vnet)

            for rg_name, rg_vnets in sorted(vnets_by_rg.items()):
                rg_safe = sanitize(f"{sub_safe}_{rg_name}")

                with sg.subgraph(name=f"cluster_rg_{rg_safe}") as rg_sg:
                    rg_sg.attr(
                        label=f"<<font point-size='9'>{_escape_html(rg_name)}</font>>",
                        style="filled,rounded",
                        fillcolor="#fef7e0",
                        color="#f9ab00",
                        penwidth="1",
                    )

                    for vnet in rg_vnets:
                        vkey = f"{sub['id']}/{vnet['rg']}/{vnet['name']}".lower()
                        vnet_safe = sanitize(f"{rg_safe}_{vnet['name']}")
                        vnet_node = f"vnet_{vnet_safe}"
                        vnet_node_ids[vkey] = vnet_node

                        cidr_str = ", ".join(vnet["cidrs"]) if vnet["cidrs"] else ""

                        with rg_sg.subgraph(name=f"cluster_vnet_{vnet_safe}") as vsg:
                            vsg.attr(
                                label=f"<<b>{_escape_html(vnet['name'])}</b><br/><font point-size='8'>{_escape_html(cidr_str)}</font>>",
                                style="filled,rounded",
                                fillcolor="#e6f4ea",
                                color="#34a853",
                                penwidth="1.5",
                            )

                            # Invisible anchor node for peering edges
                            vsg.node(
                                vnet_node,
                                label="",
                                shape="point",
                                width="0",
                                height="0",
                                style="invis",
                            )

                            for sn_name, sn in sorted(vnet["subnets"].items()):
                                sn_safe = sanitize(f"{vnet_safe}_{sn_name}")

                                nsg_line = ""
                                if sn.get("nsg"):
                                    nsg_line = f'<tr><td align="left"><font point-size="8" color="#9c27b0">NSG: {_escape_html(sn["nsg"])}</font></td></tr>'

                                if sn["resources"]:
                                    # Subnet with resources — use HTML table
                                    rows = []
                                    rows.append(
                                        f'<tr><td align="left"><b>{_escape_html(sn_name)}</b></td></tr>'
                                    )
                                    rows.append(
                                        f'<tr><td align="left"><font point-size="8" color="#666666">{sn["prefix"]}</font></td></tr>'
                                    )
                                    if nsg_line:
                                        rows.append(nsg_line)
                                    rows.append('<hr/>')

                                    # Cap at 25 resources to keep SVG manageable
                                    for res in sn["resources"][:25]:
                                        icon = "VM" if res["type"] == "VM" else "PE"
                                        rows.append(
                                            f'<tr><td align="left"><font point-size="8">[{icon}] {_escape_html(res["name"])}</font></td></tr>'
                                        )
                                    if len(sn["resources"]) > 25:
                                        remaining = len(sn["resources"]) - 25
                                        rows.append(
                                            f'<tr><td align="left"><font point-size="8" color="#888888">…and {remaining} more</font></td></tr>'
                                        )

                                    table_label = (
                                        '<<table border="0" cellborder="0" cellspacing="1">'
                                        + "".join(rows)
                                        + "</table>>"
                                    )

                                    vsg.node(
                                        f"sn_{sn_safe}",
                                        label=table_label,
                                        shape="box",
                                        fillcolor="#fff8e1",
                                        color="#e0af68",
                                        penwidth="1",
                                    )
                                else:
                                    # Empty subnet — simple box
                                    sn_label = (
                                        f"<<b>{_escape_html(sn_name)}</b>"
                                        f"<br/><font point-size='8'>{sn['prefix']}</font>>"
                                    )
                                    if sn.get("nsg"):
                                        sn_label = (
                                            f"<<b>{_escape_html(sn_name)}</b>"
                                            f"<br/><font point-size='8'>{sn['prefix']}</font>"
                                            f"<br/><font point-size='8' color='#9c27b0'>NSG: {_escape_html(sn['nsg'])}</font>>"
                                        )
                                    vsg.node(
                                        f"sn_{sn_safe}",
                                        label=sn_label,
                                        shape="box",
                                        fillcolor="#f5f5f5",
                                        color="#cccccc",
                                        penwidth="1",
                                    )

            # Standalone RGs
            standalone = sub.get("standalone_rgs", [])
            if standalone:
                bucket_safe = sanitize(f"{sub_safe}_other_rgs")
                rg_list = "\\n".join(standalone[:10])
                remaining = len(standalone) - 10
                if remaining > 0:
                    rg_list += f"\\n…and {remaining} more"
                sg.node(
                    bucket_safe,
                    label=rg_list,
                    shape="note",
                    fillcolor="#f5f5f5",
                    color="#cccccc",
                    fontsize="7",
                )

    # Peering edges
    for p in model["peerings"]:
        src_node = vnet_node_ids.get(p["source"])
        dst_node = vnet_node_ids.get(p["target"])
        if src_node and dst_node:
            label = p["state"] if p["state"] != "Connected" else "peered"
            g.edge(
                src_node, dst_node,
                label=label,
                style="dashed",
                color="#ff9800",
                penwidth="2",
                fontcolor="#ff9800",
                len="2",
            )

    return g


def _escape_html(text: str) -> str:
    """Escape characters that break Graphviz HTML labels."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
GENERATORS = {
    "l1": generate_l1,
    "l2": generate_l2,
    "l3": generate_l3,
}


def main():
    parser = argparse.ArgumentParser(description="Graphviz DOT → SVG renderer with detail levels")
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
        gen_fn = GENERATORS[level]
        dot_graph = gen_fn(model)

        # Save DOT source
        dot_path = output_dir / f"{level}.dot"
        with open(dot_path, "w") as f:
            f.write(dot_graph.source)

        # Render to SVG
        svg_path = output_dir / level
        dot_graph.render(filename=str(svg_path), cleanup=True)
        print(f"  {output_dir}/{level}.svg ({len(dot_graph.source.splitlines())} DOT lines)")

    print(f"\nTo view: open output/*.svg in a browser")
    print("  SVGs are fully zoomable and searchable (Ctrl+F)")


if __name__ == "__main__":
    main()
