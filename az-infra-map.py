#!/usr/bin/env python3
"""
az-infra-map.py — Generate a Mermaid diagram of Azure infrastructure.

Queries Azure Resource Graph for structural data and produces a .mermaid file
showing the containment hierarchy (Subscription → RG → VNet → Subnet) and
connectivity (peerings, VM→NIC→Subnet placement).

Prerequisites:
  - az cli logged in with read access
  - az extension: resource-graph (az extension add --name resource-graph)

Usage:
  # Full run: fetch from Azure + generate diagram
  python3 az-infra-map.py

  # Fetch only: save raw data to cache directory (no diagram)
  python3 az-infra-map.py --fetch

  # Render only: generate diagram from cached data (no Azure calls)
  python3 az-infra-map.py --from-cache

  # Custom paths
  python3 az-infra-map.py --cache-dir ./my-cache --output my-infra.mermaid
"""

import argparse
import json
import re
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# Throttle settings — Resource Graph allows ~15 req / 5s per tenant
QUERY_DELAY_SECONDS = 2  # pause between queries to stay under the limit
MAX_RETRIES = 4  # retry on 429 / RateLimiting
INITIAL_BACKOFF_SECONDS = 5  # first retry waits this long, then doubles


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class Subnet:
    name: str
    prefix: str
    resource_ids: list = field(default_factory=list)  # resources placed here


@dataclass
class VNet:
    name: str
    rg: str
    subscription_id: str
    location: str
    address_space: list = field(default_factory=list)
    subnets: dict = field(default_factory=dict)  # name -> Subnet


@dataclass
class Peering:
    source_vnet: str
    source_rg: str
    source_sub: str
    remote_vnet_id: str
    state: str


@dataclass
class Resource:
    name: str
    resource_type: str
    rg: str
    subscription_id: str
    subnet_id: str | None = None  # resolved placement


# ---------------------------------------------------------------------------
# Azure Resource Graph query helper
# ---------------------------------------------------------------------------
def run_graph_query(query: str, first: int = 1000) -> list:
    """Execute an az graph query with pagination and retry-on-throttle."""
    all_results = []
    skip_token = None

    while True:
        cmd = [
            "az",
            "graph",
            "query",
            "-q",
            query,
            "--first",
            str(first),
            "-o",
            "json",
        ]
        if skip_token:
            cmd.extend(["--skip-token", skip_token])

        # Retry loop for rate limiting
        backoff = INITIAL_BACKOFF_SECONDS
        for attempt in range(MAX_RETRIES + 1):
            result = subprocess.run(cmd, capture_output=True, text=True)

            if result.returncode == 0:
                break

            if "RateLimiting" in result.stderr or "throttled" in result.stderr.lower():
                if attempt < MAX_RETRIES:
                    print(
                        f"   ⏳ Rate limited, retrying in {backoff}s "
                        f"(attempt {attempt + 1}/{MAX_RETRIES})…",
                        file=sys.stderr,
                    )
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                else:
                    print(
                        f"ERROR: Still rate limited after {MAX_RETRIES} retries.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
            else:
                print(
                    f"ERROR: az graph query failed:\n{result.stderr}", file=sys.stderr
                )
                sys.exit(1)

        data = json.loads(result.stdout)
        all_results.extend(data.get("data", []))

        skip_token = data.get("skip_token")
        total = data.get("total_records", 0)

        if not skip_token or len(all_results) >= total:
            break

        # Small pause between pagination requests too
        time.sleep(QUERY_DELAY_SECONDS)

    # Delay before the next query to stay under the rate limit
    time.sleep(QUERY_DELAY_SECONDS)

    return all_results


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------
def get_subscriptions() -> dict:
    """Return {subscription_id: display_name}."""
    result = subprocess.run(
        ["az", "account", "list", "-o", "json"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"ERROR: az account list failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)
    subs = json.loads(result.stdout)
    return {s["id"]: s["name"] for s in subs if s.get("state") == "Enabled"}


def get_resource_groups() -> list[dict]:
    """Get all resource groups across subscriptions."""
    rows = run_graph_query(
        "ResourceContainers "
        "| where type == 'microsoft.resources/subscriptions/resourcegroups' "
        "| project name, subscriptionId, location"
    )
    return rows


# ---------------------------------------------------------------------------
# Mermaid generation
# ---------------------------------------------------------------------------
def sanitize_id(text: str) -> str:
    """Create a mermaid-safe node ID from arbitrary text."""
    return re.sub(r"[^a-zA-Z0-9_]", "_", text)


def escape_label(text: str) -> str:
    """Escape characters that break Mermaid labels."""
    return text.replace('"', "'").replace("[", "(").replace("]", ")")


def parse_vnet_from_resource_id(resource_id: str) -> tuple[str, str, str] | None:
    """Extract (subscription_id, rg, vnet_name) from an ARM resource ID."""
    # /subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.Network/virtualNetworks/{name}
    m = re.search(
        r"/subscriptions/([^/]+)/resourceGroups/([^/]+)/providers/"
        r"Microsoft\.Network/virtualNetworks/([^/]+)",
        resource_id,
        re.IGNORECASE,
    )
    if m:
        return m.group(1), m.group(2), m.group(3)
    return None


def parse_subnet_from_id(subnet_id: str) -> tuple[str, str, str, str] | None:
    """Extract (sub, rg, vnet, subnet) from a subnet ARM ID."""
    m = re.search(
        r"/subscriptions/([^/]+)/resourceGroups/([^/]+)/providers/"
        r"Microsoft\.Network/virtualNetworks/([^/]+)/subnets/([^/]+)",
        subnet_id,
        re.IGNORECASE,
    )
    if m:
        return m.group(1), m.group(2), m.group(3), m.group(4)
    return None


def generate_mermaid(
    subscriptions: dict,
    resource_groups: list[dict],
    vnets: list[VNet],
    peerings: list[Peering],
    vms: list[Resource],
    private_endpoints: list[Resource],
    nsg_map: dict,
) -> str:
    lines = ["graph TB"]
    lines.append("")

    # -- Build placement index: subnet_key -> [resources]
    subnet_resources: dict[str, list[Resource]] = defaultdict(list)
    for res in vms + private_endpoints:
        if res.subnet_id:
            parsed = parse_subnet_from_id(res.subnet_id)
            if parsed:
                sub_id, rg, vnet, sn = parsed
                key = f"{sub_id}/{rg}/{vnet}/{sn}".lower()
                subnet_resources[key].append(res)

    # -- Organize RGs by subscription
    rgs_by_sub: dict[str, set] = defaultdict(set)
    for rg in resource_groups:
        rgs_by_sub[rg["subscriptionId"]].add(rg["name"])
    # Also add RGs from VNets (in case Resource Graph returned them separately)
    for v in vnets:
        rgs_by_sub[v.subscription_id].add(v.rg)

    # -- Index VNets by (sub, rg)
    vnets_by_rg: dict[str, list[VNet]] = defaultdict(list)
    for v in vnets:
        key = f"{v.subscription_id}/{v.rg}".lower()
        vnets_by_rg[key].append(v)

    # -- Track which RGs have VNets (we'll only render RGs with network content
    #    to keep the diagram manageable; standalone RGs are listed separately)
    rgs_with_vnets = set()
    for v in vnets:
        rgs_with_vnets.add(f"{v.subscription_id}/{v.rg}".lower())

    # -- Render hierarchy
    vnet_node_ids: dict[str, str] = {}  # (sub/rg/vnet) -> mermaid node id

    for sub_id, sub_name in sorted(subscriptions.items(), key=lambda x: x[1]):
        sub_safe = sanitize_id(sub_id[:12])
        short_sub = sub_id[:8]
        lines.append(
            f'  subgraph sub_{sub_safe}["{escape_label(sub_name)}<br/><small>{short_sub}…</small>"]'
        )
        lines.append(f"    direction TB")

        rgs_in_sub = sorted(rgs_by_sub.get(sub_id, set()))

        # RGs with VNets — render fully
        for rg_name in rgs_in_sub:
            rg_key = f"{sub_id}/{rg_name}".lower()
            if rg_key not in rgs_with_vnets:
                continue

            rg_safe = sanitize_id(f"{sub_safe}_{rg_name}")
            lines.append(f'    subgraph rg_{rg_safe}["{escape_label(rg_name)}"]')
            lines.append(f"      direction TB")

            for vnet in vnets_by_rg.get(rg_key, []):
                vnet_key = f"{sub_id}/{rg_name}/{vnet.name}".lower()
                vnet_safe = sanitize_id(f"{rg_safe}_{vnet.name}")
                vnet_node_ids[vnet_key] = f"vnet_{vnet_safe}"

                addr_str = ", ".join(vnet.address_space) if vnet.address_space else ""
                vnet_label = f"{escape_label(vnet.name)}"
                if addr_str:
                    vnet_label += f"<br/><small>{escape_label(addr_str)}</small>"

                lines.append(f'      subgraph vnet_{vnet_safe}["{vnet_label}"]')
                lines.append(f"        direction TB")

                for sn_name, sn in sorted(vnet.subnets.items()):
                    sn_key = f"{sub_id}/{rg_name}/{vnet.name}/{sn_name}".lower()
                    sn_safe = sanitize_id(f"{vnet_safe}_{sn_name}")

                    nsg_name = nsg_map.get(
                        # Build the full ARM-style path to match
                        f"/subscriptions/{sub_id}/resourcegroups/{rg_name}"
                        f"/providers/microsoft.network/virtualnetworks/{vnet.name}"
                        f"/subnets/{sn_name}".lower(),
                        "",
                    )
                    sn_label = f"{escape_label(sn_name)}<br/><small>{sn.prefix}</small>"
                    if nsg_name:
                        sn_label += f"<br/><small>🛡 {escape_label(nsg_name)}</small>"

                    placed = subnet_resources.get(sn_key, [])
                    if placed:
                        lines.append(f'        subgraph sn_{sn_safe}["{sn_label}"]')
                        lines.append(f"          direction LR")
                        for res in placed:
                            res_safe = sanitize_id(f"{sn_safe}_{res.name}")
                            icon = "🖥" if res.resource_type == "VM" else "🔒"
                            lines.append(
                                f'          {res_safe}["{icon} {escape_label(res.name)}"]'
                            )
                        lines.append(f"        end")
                    else:
                        # Leaf node — no subgraph needed
                        lines.append(f'        sn_{sn_safe}["{sn_label}"]')

                lines.append(f"      end")

            lines.append(f"    end")

        # Standalone RGs (no VNets) — compact summary
        standalone = [
            rg for rg in rgs_in_sub if f"{sub_id}/{rg}".lower() not in rgs_with_vnets
        ]
        if standalone:
            bucket_safe = sanitize_id(f"{sub_safe}_other_rgs")
            rg_list = "<br/>".join(escape_label(r) for r in standalone[:15])
            remaining = len(standalone) - 15
            if remaining > 0:
                rg_list += f"<br/><small>…and {remaining} more</small>"
            lines.append(f'    {bucket_safe}["{rg_list}"]')

        lines.append(f"  end")
        lines.append("")

    # -- Peering edges (deduplicated: only render A→B, not also B→A)
    seen_peerings = set()
    for p in peerings:
        remote = parse_vnet_from_resource_id(p.remote_vnet_id)
        if not remote:
            continue
        r_sub, r_rg, r_vnet = remote
        src_key = f"{p.source_sub}/{p.source_rg}/{p.source_vnet}".lower()
        dst_key = f"{r_sub}/{r_rg}/{r_vnet}".lower()

        pair = tuple(sorted([src_key, dst_key]))
        if pair in seen_peerings:
            continue
        seen_peerings.add(pair)

        src_node = vnet_node_ids.get(src_key)
        dst_node = vnet_node_ids.get(dst_key)
        if src_node and dst_node:
            state_label = p.state if p.state != "Connected" else "peered"
            lines.append(f'  {src_node} <--->|"{state_label}"| {dst_node}')

    # -- Styling
    lines.append("")
    lines.append("  %% Styling")
    lines.append("  classDef subStyle fill:#e8f0fe,stroke:#4285f4,stroke-width:2px")
    lines.append("  classDef rgStyle fill:#fef7e0,stroke:#f9ab00,stroke-width:1px")
    lines.append("  classDef vnetStyle fill:#e6f4ea,stroke:#34a853,stroke-width:1px")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------
CACHE_FILES = [
    "subscriptions",
    "resource_groups",
    "vnets",
    "peerings",
    "nics",
    "vms",
    "private_endpoints",
    "nsgs",
]


def save_cache(cache_dir: Path, name: str, data) -> None:
    """Write query results to a JSON file in the cache directory."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{name}.json"
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    print(f"   💾 Cached → {path}")


def load_cache(cache_dir: Path, name: str):
    """Load query results from a cached JSON file."""
    path = cache_dir / f"{name}.json"
    if not path.exists():
        print(f"ERROR: Cache file not found: {path}", file=sys.stderr)
        print(f"       Run with --fetch first to populate the cache.", file=sys.stderr)
        sys.exit(1)
    with open(path) as f:
        data = json.load(f)
    print(
        f"   📂 Loaded ← {path} ({len(data) if isinstance(data, (list, dict)) else '?'} entries)"
    )
    return data


# ---------------------------------------------------------------------------
# Fetch phase — query Azure and store raw results
# ---------------------------------------------------------------------------
def fetch_all(cache_dir: Path) -> None:
    """Run all Azure queries and save raw results to cache_dir."""
    print("🔍 Fetching subscriptions…")
    subscriptions = get_subscriptions()
    save_cache(cache_dir, "subscriptions", subscriptions)
    print(f"   Found {len(subscriptions)} enabled subscription(s)")

    print("🔍 Fetching resource groups…")
    resource_groups = get_resource_groups()
    save_cache(cache_dir, "resource_groups", resource_groups)
    print(f"   Found {len(resource_groups)} resource group(s)")

    print("🔍 Fetching VNets & subnets…")
    vnets_raw = run_graph_query(
        "Resources "
        "| where type =~ 'microsoft.network/virtualnetworks' "
        "| mv-expand subnet = properties.subnets "
        "| project vnetName=name, resourceGroup, subscriptionId, location, "
        "  addressSpace=properties.addressSpace.addressPrefixes, "
        "  subnetName=subnet.name, "
        "  subnetPrefix=subnet.properties.addressPrefix"
    )
    save_cache(cache_dir, "vnets", vnets_raw)
    print(f"   Found {len(vnets_raw)} VNet/subnet row(s)")

    print("🔍 Fetching peerings…")
    peerings_raw = run_graph_query(
        "Resources "
        "| where type =~ 'microsoft.network/virtualnetworks' "
        "| mv-expand peering = properties.virtualNetworkPeerings "
        "| where isnotnull(peering) "
        "| project vnetName=name, resourceGroup, subscriptionId, "
        "  peerName=peering.name, "
        "  remoteVnet=peering.properties.remoteVirtualNetwork.id, "
        "  peeringState=peering.properties.peeringState"
    )
    save_cache(cache_dir, "peerings", peerings_raw)
    print(f"   Found {len(peerings_raw)} peering(s)")

    print("🔍 Fetching NICs…")
    nics_raw = run_graph_query(
        "Resources "
        "| where type =~ 'microsoft.network/networkinterfaces' "
        "| mv-expand ipconfig = properties.ipConfigurations "
        "| project nicId=id, nicName=name, resourceGroup, subscriptionId, "
        "  privateIP=ipconfig.properties.privateIPAddress, "
        "  subnetId=ipconfig.properties.subnet.id"
    )
    save_cache(cache_dir, "nics", nics_raw)
    print(f"   Found {len(nics_raw)} NIC(s)")

    print("🔍 Fetching VMs…")
    vms_raw = run_graph_query(
        "Resources "
        "| where type =~ 'microsoft.compute/virtualMachines' "
        "| mv-expand nic = properties.networkProfile.networkInterfaces "
        "| project vmName=name, resourceGroup, subscriptionId, "
        "  vmSize=properties.hardwareProfile.vmSize, "
        "  nicId=nic.id"
    )
    save_cache(cache_dir, "vms", vms_raw)
    print(f"   Found {len(vms_raw)} VM(s)")

    print("🔍 Fetching private endpoints…")
    pes_raw = run_graph_query(
        "Resources "
        "| where type =~ 'microsoft.network/privateendpoints' "
        "| mv-expand subnet = properties.subnet "
        "| project name, resourceGroup, subscriptionId, "
        "  subnetId=properties.subnet.id, "
        "  targetId=properties.privateLinkServiceConnections[0]"
        "    .properties.privateLinkServiceId"
    )
    save_cache(cache_dir, "private_endpoints", pes_raw)
    print(f"   Found {len(pes_raw)} private endpoint(s)")

    print("🔍 Fetching NSG associations…")
    nsgs_raw = run_graph_query(
        "Resources "
        "| where type =~ 'microsoft.network/networksecuritygroups' "
        "| mv-expand subnet = properties.subnets "
        "| project nsgName=name, resourceGroup, subscriptionId, "
        "  subnetId=subnet.id"
    )
    save_cache(cache_dir, "nsgs", nsgs_raw)
    print(f"   Found {len(nsgs_raw)} NSG→subnet association(s)")

    print(f"\n✅ All data cached in {cache_dir}/")


# ---------------------------------------------------------------------------
# Render phase — load cache and build Mermaid
# ---------------------------------------------------------------------------
def parse_cached_vnets(rows: list) -> list[VNet]:
    """Transform raw VNet query rows into VNet objects."""
    vnets: dict[str, VNet] = {}
    for r in rows:
        key = f"{r['subscriptionId']}/{r['resourceGroup']}/{r['vnetName']}"
        if key not in vnets:
            addr = r.get("addressSpace", [])
            if isinstance(addr, str):
                addr = [addr]
            vnets[key] = VNet(
                name=r["vnetName"],
                rg=r["resourceGroup"],
                subscription_id=r["subscriptionId"],
                location=r.get("location", ""),
                address_space=addr,
            )
        sn_name = r.get("subnetName", "")
        if sn_name:
            vnets[key].subnets[sn_name] = Subnet(
                name=sn_name,
                prefix=r.get("subnetPrefix", ""),
            )
    return list(vnets.values())


def parse_cached_peerings(rows: list) -> list[Peering]:
    return [
        Peering(
            source_vnet=r["vnetName"],
            source_rg=r["resourceGroup"],
            source_sub=r["subscriptionId"],
            remote_vnet_id=r.get("remoteVnet", ""),
            state=r.get("peeringState", "Unknown"),
        )
        for r in rows
    ]


def parse_cached_nics(rows: list) -> dict:
    nic_map = {}
    for r in rows:
        nic_id = (r.get("nicId") or "").lower()
        subnet_id = r.get("subnetId", "")
        if nic_id and subnet_id:
            nic_map[nic_id] = subnet_id
    return nic_map


def parse_cached_vms(rows: list, nic_map: dict) -> list[Resource]:
    vms = []
    for r in rows:
        nic_id = (r.get("nicId") or "").lower()
        subnet_id = nic_map.get(nic_id)
        vms.append(
            Resource(
                name=r["vmName"],
                resource_type="VM",
                rg=r["resourceGroup"],
                subscription_id=r["subscriptionId"],
                subnet_id=subnet_id,
            )
        )
    return vms


def parse_cached_pes(rows: list) -> list[Resource]:
    return [
        Resource(
            name=r.get("name", "pe"),
            resource_type="PrivateEndpoint",
            rg=r["resourceGroup"],
            subscription_id=r["subscriptionId"],
            subnet_id=r.get("subnetId"),
        )
        for r in rows
    ]


def parse_cached_nsgs(rows: list) -> dict:
    nsg_map = {}
    for r in rows:
        sid = (r.get("subnetId") or "").lower()
        if sid:
            nsg_map[sid] = r["nsgName"]
    return nsg_map


def render_from_cache(cache_dir: Path, output: str) -> None:
    """Load cached data and generate Mermaid diagram."""
    print(f"📂 Loading cached data from {cache_dir}/")

    subscriptions = load_cache(cache_dir, "subscriptions")
    resource_groups = load_cache(cache_dir, "resource_groups")
    vnets_raw = load_cache(cache_dir, "vnets")
    peerings_raw = load_cache(cache_dir, "peerings")
    nics_raw = load_cache(cache_dir, "nics")
    vms_raw = load_cache(cache_dir, "vms")
    pes_raw = load_cache(cache_dir, "private_endpoints")
    nsgs_raw = load_cache(cache_dir, "nsgs")

    print("\n📐 Generating Mermaid diagram…")
    vnets = parse_cached_vnets(vnets_raw)
    peerings = parse_cached_peerings(peerings_raw)
    nic_map = parse_cached_nics(nics_raw)
    vms = parse_cached_vms(vms_raw, nic_map)
    pes = parse_cached_pes(pes_raw)
    nsg_map = parse_cached_nsgs(nsgs_raw)

    mermaid = generate_mermaid(
        subscriptions,
        resource_groups,
        vnets,
        peerings,
        vms,
        pes,
        nsg_map,
    )

    with open(output, "w") as f:
        f.write(mermaid)

    print(f"✅ Written to {output}")
    print(f"   ({len(mermaid.splitlines())} lines)")
    print()
    print("To render:")
    print(f"  - VS Code: open {output} with Mermaid preview extension")
    print(f"  - Browser: open mermaid-viewer.html and drop the file in")
    print(f"  - Web:     paste into https://mermaid.live")
    print(f"  - DevOps:  embed in wiki as ```mermaid code block")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Generate Mermaid diagram of Azure infra"
    )
    parser.add_argument(
        "-o",
        "--output",
        default="azure-infra.mermaid",
        help="Output file path (default: azure-infra.mermaid)",
    )
    parser.add_argument(
        "--cache-dir",
        default=".az-cache",
        help="Directory for cached query results (default: .az-cache)",
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--fetch",
        action="store_true",
        help="Fetch data from Azure and save to cache (no diagram)",
    )
    mode.add_argument(
        "--from-cache",
        action="store_true",
        help="Generate diagram from cached data (no Azure calls)",
    )

    args = parser.parse_args()
    cache_dir = Path(args.cache_dir)

    if args.fetch:
        # Fetch only
        fetch_all(cache_dir)

    elif args.from_cache:
        # Render only
        render_from_cache(cache_dir, args.output)

    else:
        # Default: fetch + render
        fetch_all(cache_dir)
        print()
        render_from_cache(cache_dir, args.output)


if __name__ == "__main__":
    main()
