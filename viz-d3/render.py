#!/usr/bin/env python3
"""
D3.js interactive renderer — self-contained HTML with drill-down.

Reads cached Azure data from ../.az-cache/ and generates a single HTML file
with embedded D3.js visualization supporting three detail levels:
  L1 — Subscription overview (default view, click to drill in)
  L2 — VNet + subnet view within a subscription
  L3 — Resource detail within a subnet

No pip dependencies — D3 is loaded from CDN.

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
        sub_vnets = sorted(
            [v for v in vnets.values() if v["subscription_id"] == sub_id],
            key=lambda v: v["name"],
        )
        all_rgs = sorted(rgs_by_sub.get(sub_id, set()))
        standalone = [rg for rg in all_rgs if f"{sub_id}/{rg}".lower() not in rgs_with_vnets]
        total_vms = sum(
            len([r for r in sn["resources"] if r["type"] == "VM"])
            for v in sub_vnets for sn in v["subnets"].values()
        )
        total_pes = sum(
            len([r for r in sn["resources"] if r["type"] == "PrivateEndpoint"])
            for v in sub_vnets for sn in v["subnets"].values()
        )

        # Convert subnets dict to sorted list for JSON
        for v in sub_vnets:
            v["subnets"] = sorted(v["subnets"].values(), key=lambda s: s["name"])

        model_subs.append({
            "id": sub_id,
            "name": sub_name,
            "vnets": sub_vnets,
            "standalone_rgs": standalone,
            "counts": {
                "vnets": len(sub_vnets),
                "subnets": sum(len(v["subnets"]) for v in sub_vnets),
                "vms": total_vms,
                "pes": total_pes,
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
            "source_sub": r["subscriptionId"],
            "target_sub": r_sub,
            "source_vnet": r["vnetName"],
            "target_vnet": r_vnet,
            "source_key": src_key,
            "target_key": dst_key,
            "state": r.get("peeringState", "Unknown"),
        })

    return {"subscriptions": model_subs, "peerings": model_peerings}


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Azure Infrastructure Map</title>
<script src="https://d3js.org/d3.v7.min.js"></script>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace;
    background: #0f1117;
    color: #e0e0e0;
    overflow: hidden;
    height: 100vh;
  }
  #header {
    position: fixed; top: 0; left: 0; right: 0; z-index: 100;
    background: #1a1b26;
    border-bottom: 1px solid #2a2b36;
    padding: 10px 20px;
    display: flex; align-items: center; gap: 12px;
  }
  #header h1 { font-size: 14px; color: #7aa2f7; white-space: nowrap; }
  #breadcrumb {
    display: flex; align-items: center; gap: 4px;
    font-size: 13px; color: #888;
  }
  #breadcrumb span { cursor: pointer; color: #7aa2f7; }
  #breadcrumb span:hover { text-decoration: underline; }
  #breadcrumb .sep { color: #555; cursor: default; }
  #breadcrumb .current { color: #e0e0e0; cursor: default; }
  #legend {
    margin-left: auto; font-size: 11px; display: flex; gap: 14px; color: #888;
  }
  #legend .item { display: flex; align-items: center; gap: 4px; }
  #legend .dot {
    width: 10px; height: 10px; border-radius: 2px; display: inline-block;
  }
  #canvas { width: 100%; height: 100vh; padding-top: 44px; }
  svg { width: 100%; height: 100%; }

  /* Tooltip */
  #tooltip {
    position: fixed; pointer-events: none; z-index: 200;
    background: #1a1b26; border: 1px solid #3a3b46;
    border-radius: 6px; padding: 10px 14px;
    font-size: 12px; line-height: 1.5;
    max-width: 350px; display: none;
    box-shadow: 0 4px 12px rgba(0,0,0,0.5);
  }
  #tooltip .tt-title { font-weight: 600; color: #7aa2f7; margin-bottom: 4px; }
  #tooltip .tt-row { color: #aaa; }
  #tooltip .tt-row b { color: #e0e0e0; }

  /* Detail panel (L3) */
  #detail-panel {
    position: fixed; top: 44px; right: 0; bottom: 0; width: 380px;
    background: #1a1b26; border-left: 1px solid #2a2b36;
    overflow-y: auto; display: none; z-index: 50; padding: 16px;
  }
  #detail-panel h2 { font-size: 14px; color: #7aa2f7; margin-bottom: 4px; }
  #detail-panel .meta { font-size: 12px; color: #888; margin-bottom: 12px; }
  #detail-panel .res-list { list-style: none; }
  #detail-panel .res-list li {
    padding: 4px 8px; font-size: 12px; border-bottom: 1px solid #2a2b36;
    display: flex; align-items: center; gap: 6px;
  }
  #detail-panel .res-list li:hover { background: #24253a; }
  #detail-panel .res-icon { font-size: 14px; }
  #detail-panel .close-btn {
    position: absolute; top: 12px; right: 12px; cursor: pointer;
    color: #888; font-size: 18px; background: none; border: none;
  }
  #detail-panel .close-btn:hover { color: #fff; }
</style>
</head>
<body>
<div id="header">
  <h1>Azure Infrastructure Map</h1>
  <div id="breadcrumb"></div>
  <div id="legend">
    <div class="item"><span class="dot" style="background:#7aa2f7"></span> Subscription</div>
    <div class="item"><span class="dot" style="background:#9ece6a"></span> VNet</div>
    <div class="item"><span class="dot" style="background:#e0af68"></span> Subnet</div>
    <div class="item"><span class="dot" style="background:#ff9e64"></span> Peering</div>
  </div>
</div>
<div id="canvas"><svg></svg></div>
<div id="tooltip"></div>
<div id="detail-panel">
  <button class="close-btn" onclick="closeDetail()">&times;</button>
  <div id="detail-content"></div>
</div>

<script>
const DATA = __DATA_JSON__;

const SUB_COLORS = ["#7aa2f7","#f7768e","#9ece6a","#e0af68","#bb9af7","#7dcfff","#ff9e64"];
const width = window.innerWidth;
const height = window.innerHeight - 44;

const svg = d3.select("svg");
const g = svg.append("g");

// Zoom
const zoom = d3.zoom()
  .scaleExtent([0.2, 4])
  .on("zoom", e => g.attr("transform", e.transform));
svg.call(zoom);

const tooltip = d3.select("#tooltip");
let currentView = { level: "l1", sub: null, vnet: null };

// ---------------------------------------------------------------------------
// Breadcrumb
// ---------------------------------------------------------------------------
function updateBreadcrumb() {
  const bc = d3.select("#breadcrumb");
  bc.html("");
  bc.append("span").text("Overview").on("click", () => renderL1());
  if (currentView.sub) {
    bc.append("span").attr("class","sep").text(" / ");
    if (currentView.vnet) {
      bc.append("span").text(currentView.sub.name).on("click", () => renderL2(currentView.sub));
      bc.append("span").attr("class","sep").text(" / ");
      bc.append("span").attr("class","current").text(currentView.vnet.name);
    } else {
      bc.append("span").attr("class","current").text(currentView.sub.name);
    }
  }
}

// ---------------------------------------------------------------------------
// Tooltip helpers
// ---------------------------------------------------------------------------
function showTooltip(evt, html) {
  tooltip.html(html).style("display","block");
  const tt = tooltip.node().getBoundingClientRect();
  let x = evt.clientX + 12, y = evt.clientY + 12;
  if (x + tt.width > window.innerWidth) x = evt.clientX - tt.width - 12;
  if (y + tt.height > window.innerHeight) y = evt.clientY - tt.height - 12;
  tooltip.style("left", x+"px").style("top", y+"px");
}
function hideTooltip() { tooltip.style("display","none"); }

// ---------------------------------------------------------------------------
// L1 — Subscription overview
// ---------------------------------------------------------------------------
function renderL1() {
  currentView = { level: "l1", sub: null, vnet: null };
  updateBreadcrumb();
  closeDetail();
  g.selectAll("*").remove();

  const subs = DATA.subscriptions;
  const nodes = subs.map((s,i) => ({
    ...s, index: i, color: SUB_COLORS[i % SUB_COLORS.length],
    x: width/2 + Math.cos(i * 2*Math.PI/subs.length) * 250,
    y: height/2 + Math.sin(i * 2*Math.PI/subs.length) * 250,
  }));

  // Build peering edges at subscription level (aggregate)
  const subPeerSet = new Set();
  const subPeers = [];
  DATA.peerings.forEach(p => {
    const key = [p.source_sub, p.target_sub].sort().join("|");
    if (!subPeerSet.has(key) && p.source_sub !== p.target_sub) {
      subPeerSet.add(key);
      subPeers.push({
        source: nodes.find(n => n.id === p.source_sub),
        target: nodes.find(n => n.id === p.target_sub),
      });
    }
  });

  const sim = d3.forceSimulation(nodes)
    .force("charge", d3.forceManyBody().strength(-800))
    .force("center", d3.forceCenter(width/2, height/2))
    .force("collision", d3.forceCollide().radius(120))
    .force("link", d3.forceLink(subPeers).distance(300).strength(0.3));

  // Edges
  const links = g.selectAll(".peer-link")
    .data(subPeers).enter()
    .append("line")
    .attr("stroke", "#ff9e64").attr("stroke-width", 2)
    .attr("stroke-dasharray", "6,4").attr("opacity", 0.6);

  // Node groups
  const nodeG = g.selectAll(".sub-node")
    .data(nodes).enter()
    .append("g").attr("class","sub-node").attr("cursor","pointer")
    .on("click", (e,d) => renderL2(d))
    .on("mouseover", (e,d) => {
      showTooltip(e,
        `<div class="tt-title">${d.name}</div>` +
        `<div class="tt-row"><b>${d.counts.vnets}</b> VNets &middot; <b>${d.counts.subnets}</b> Subnets</div>` +
        `<div class="tt-row"><b>${d.counts.vms}</b> VMs &middot; <b>${d.counts.rgs}</b> RGs</div>` +
        `<div class="tt-row" style="margin-top:4px;color:#7aa2f7">Click to expand</div>`
      );
    })
    .on("mousemove", (e) => showTooltip(e, tooltip.html()))
    .on("mouseout", hideTooltip)
    .call(d3.drag()
      .on("start", (e,d) => { if (!e.active) sim.alphaTarget(0.3).restart(); d.fx=d.x; d.fy=d.y; })
      .on("drag", (e,d) => { d.fx=e.x; d.fy=e.y; })
      .on("end", (e,d) => { if (!e.active) sim.alphaTarget(0); d.fx=null; d.fy=null; })
    );

  nodeG.append("rect")
    .attr("width", 200).attr("height", 80).attr("rx", 8)
    .attr("x", -100).attr("y", -40)
    .attr("fill", d => d.color + "22")
    .attr("stroke", d => d.color).attr("stroke-width", 2);

  nodeG.append("text")
    .attr("text-anchor","middle").attr("y", -12)
    .attr("fill","white").attr("font-size","13px").attr("font-weight","600")
    .text(d => d.name.length > 24 ? d.name.slice(0,22)+"…" : d.name);

  nodeG.append("text")
    .attr("text-anchor","middle").attr("y", 8)
    .attr("fill","#aaa").attr("font-size","11px")
    .text(d => `${d.counts.vnets} VNets · ${d.counts.vms} VMs`);

  nodeG.append("text")
    .attr("text-anchor","middle").attr("y", 24)
    .attr("fill","#666").attr("font-size","10px")
    .text(d => d.id.slice(0,8) + "…");

  sim.on("tick", () => {
    nodeG.attr("transform", d => `translate(${d.x},${d.y})`);
    links.attr("x1",d=>d.source.x).attr("y1",d=>d.source.y)
         .attr("x2",d=>d.target.x).attr("y2",d=>d.target.y);
  });

  svg.transition().duration(500).call(zoom.transform, d3.zoomIdentity);
}

// ---------------------------------------------------------------------------
// L2 — VNets & subnets within a subscription
// ---------------------------------------------------------------------------
function renderL2(sub) {
  currentView = { level: "l2", sub, vnet: null };
  updateBreadcrumb();
  closeDetail();
  g.selectAll("*").remove();

  const vnets = sub.vnets;
  if (!vnets.length) {
    g.append("text").attr("x",width/2).attr("y",height/2)
     .attr("text-anchor","middle").attr("fill","#888").attr("font-size","14px")
     .text("No VNets in this subscription");
    return;
  }

  // Layout: VNets as large boxes with subnets as smaller boxes inside
  const padding = 30;
  const snBoxH = 36;
  const snGap = 4;
  const vnetPadTop = 50;
  const vnetW = 320;

  // Calculate heights
  vnets.forEach(v => {
    v._h = vnetPadTop + v.subnets.length * (snBoxH + snGap) + padding;
    v._w = vnetW;
  });

  // Position VNets in a grid
  const cols = Math.min(vnets.length, Math.max(2, Math.floor(width / (vnetW + 40))));
  vnets.forEach((v,i) => {
    const col = i % cols;
    const row = Math.floor(i / cols);
    v._x = 40 + col * (vnetW + 40);
    v._y = 40 + row * 500;
  });

  // Find peerings relevant to this subscription
  const relevantPeerings = DATA.peerings.filter(
    p => p.source_sub === sub.id || p.target_sub === sub.id
  );

  // VNet groups
  const vnetG = g.selectAll(".vnet-box")
    .data(vnets).enter()
    .append("g").attr("class","vnet-box")
    .attr("transform", d => `translate(${d._x},${d._y})`);

  // VNet background
  vnetG.append("rect")
    .attr("width", d => d._w).attr("height", d => d._h).attr("rx", 6)
    .attr("fill", "#9ece6a11").attr("stroke", "#9ece6a").attr("stroke-width", 1.5);

  // VNet title
  vnetG.append("text")
    .attr("x", 12).attr("y", 22)
    .attr("fill", "#9ece6a").attr("font-size", "13px").attr("font-weight", "600")
    .text(d => d.name);

  vnetG.append("text")
    .attr("x", 12).attr("y", 38)
    .attr("fill", "#888").attr("font-size", "11px")
    .text(d => (d.cidrs || []).join(", "));

  // Subnets inside each VNet
  vnetG.each(function(vnet) {
    const vg = d3.select(this);
    vnet.subnets.forEach((sn, si) => {
      const sy = vnetPadTop + si * (snBoxH + snGap);
      const sg = vg.append("g")
        .attr("transform", `translate(10, ${sy})`)
        .attr("cursor", sn.resources.length ? "pointer" : "default")
        .on("click", () => { if (sn.resources.length) showDetail(sub, vnet, sn); })
        .on("mouseover", (e) => {
          const parts = [`<div class="tt-title">${sn.name}</div>`];
          parts.push(`<div class="tt-row">Prefix: <b>${sn.prefix}</b></div>`);
          if (sn.nsg) parts.push(`<div class="tt-row">NSG: <b>${sn.nsg}</b></div>`);
          const vms = sn.resources.filter(r=>r.type==="VM").length;
          const pes = sn.resources.filter(r=>r.type==="PrivateEndpoint").length;
          if (vms) parts.push(`<div class="tt-row"><b>${vms}</b> VMs</div>`);
          if (pes) parts.push(`<div class="tt-row"><b>${pes}</b> Private Endpoints</div>`);
          if (sn.resources.length) parts.push(`<div class="tt-row" style="color:#7aa2f7;margin-top:4px">Click to see resources</div>`);
          showTooltip(e, parts.join(""));
        })
        .on("mousemove", e => showTooltip(e, tooltip.html()))
        .on("mouseout", hideTooltip);

      sg.append("rect")
        .attr("width", vnet._w - 20).attr("height", snBoxH).attr("rx", 4)
        .attr("fill", sn.resources.length ? "#e0af6815" : "#ffffff08")
        .attr("stroke", sn.resources.length ? "#e0af68" : "#444")
        .attr("stroke-width", 1);

      sg.append("text")
        .attr("x", 10).attr("y", 15)
        .attr("fill", "#e0e0e0").attr("font-size", "11px")
        .text(sn.name.length > 30 ? sn.name.slice(0,28)+"…" : sn.name);

      // Second line: prefix + resource count
      let meta = sn.prefix;
      if (sn.resources.length) meta += ` · ${sn.resources.length} resources`;
      if (sn.nsg) meta += " · NSG";
      sg.append("text")
        .attr("x", 10).attr("y", 28)
        .attr("fill", "#777").attr("font-size", "10px")
        .text(meta);
    });
  });

  // Draw peering lines between VNets
  const vnetKeyMap = {};
  vnets.forEach(v => {
    const key = `${sub.id}/${v.rg}/${v.name}`.toLowerCase();
    vnetKeyMap[key] = v;
  });

  relevantPeerings.forEach(p => {
    const srcV = vnetKeyMap[p.source_key];
    const dstV = vnetKeyMap[p.target_key];
    if (srcV && dstV) {
      g.append("line")
        .attr("x1", srcV._x + srcV._w).attr("y1", srcV._y + 30)
        .attr("x2", dstV._x).attr("y2", dstV._y + 30)
        .attr("stroke", "#ff9e64").attr("stroke-width", 2)
        .attr("stroke-dasharray", "6,4").attr("opacity", 0.6);
    }
  });

  // Reset zoom to fit content
  const totalW = cols * (vnetW + 40) + 40;
  const maxH = d3.max(vnets, v => v._y + v._h) + 40;
  const scale = Math.min(width / totalW, height / maxH, 1) * 0.9;
  svg.transition().duration(500).call(
    zoom.transform,
    d3.zoomIdentity.translate(20, 20).scale(scale)
  );
}

// ---------------------------------------------------------------------------
// L3 — Resource detail panel
// ---------------------------------------------------------------------------
function showDetail(sub, vnet, sn) {
  currentView.vnet = vnet;
  updateBreadcrumb();

  const panel = d3.select("#detail-panel").style("display","block");
  const content = d3.select("#detail-content");
  content.html("");

  content.append("h2").text(sn.name);
  const meta = [`Prefix: ${sn.prefix}`];
  if (sn.nsg) meta.push(`NSG: ${sn.nsg}`);
  meta.push(`VNet: ${vnet.name}`);
  meta.push(`RG: ${vnet.rg}`);
  content.append("div").attr("class","meta").html(meta.join("<br>"));

  const vms = sn.resources.filter(r => r.type === "VM");
  const pes = sn.resources.filter(r => r.type === "PrivateEndpoint");

  if (vms.length) {
    content.append("h3").style("font-size","12px").style("color","#9ece6a")
      .style("margin","12px 0 6px").text(`Virtual Machines (${vms.length})`);
    const ul = content.append("ul").attr("class","res-list");
    vms.forEach(r => {
      const li = ul.append("li");
      li.append("span").attr("class","res-icon").text("🖥");
      li.append("span").text(r.name);
    });
  }

  if (pes.length) {
    content.append("h3").style("font-size","12px").style("color","#bb9af7")
      .style("margin","12px 0 6px").text(`Private Endpoints (${pes.length})`);
    const ul = content.append("ul").attr("class","res-list");
    pes.forEach(r => {
      const li = ul.append("li");
      li.append("span").attr("class","res-icon").text("🔒");
      li.append("span").text(r.name);
    });
  }

  if (!sn.resources.length) {
    content.append("div").style("color","#666").style("margin-top","12px")
      .text("No resources in this subnet");
  }
}

function closeDetail() {
  d3.select("#detail-panel").style("display","none");
  if (currentView.vnet) {
    currentView.vnet = null;
    updateBreadcrumb();
  }
}

// Keyboard: Escape to go back
document.addEventListener("keydown", e => {
  if (e.key === "Escape") {
    if (currentView.vnet) closeDetail();
    else if (currentView.sub) renderL1();
  }
});

// Start
renderL1();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="D3.js interactive renderer")
    parser.add_argument("--cache-dir", default=None, help="Override cache directory")
    args = parser.parse_args()

    cache_dir = Path(args.cache_dir) if args.cache_dir else CACHE_DIR
    output_dir = Path(__file__).resolve().parent / "output"
    output_dir.mkdir(exist_ok=True)

    print(f"Loading data from {cache_dir}/")
    model = build_render_model(cache_dir)

    data_json = json.dumps(model, indent=None)
    html = HTML_TEMPLATE.replace("__DATA_JSON__", data_json)

    out = output_dir / "azure-infra.html"
    with open(out, "w") as f:
        f.write(html)

    print(f"  {out} ({len(html.splitlines())} lines)")
    print(f"\nTo view: open {out} in a browser")
    print("  Controls: click to drill down, Escape to go back, scroll to zoom")


if __name__ == "__main__":
    main()
