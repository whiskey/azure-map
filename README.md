# Azure Infrastructure Map

Visualize Azure network infrastructure (Subscriptions, VNets, Subnets, VMs, Peerings) from Azure Resource Graph data.

## Quick start

```bash
# 1. Fetch data from Azure (requires az cli login)
python3 az-infra-map.py --fetch

# 2. Pick a renderer and generate output
cd viz-d3 && python3 render.py        # Interactive HTML (recommended)
cd viz-mermaid && python3 render.py    # Mermaid with detail levels
cd viz-graphviz && python3 render.py   # SVG via Graphviz
cd viz-pyvis && python3 render.py      # pyvis interactive network
```

## Data pipeline

`az-infra-map.py` queries Azure Resource Graph and caches raw JSON in `.az-cache/`. All renderers read from this shared cache — no Azure access needed after the initial fetch.

```
az-infra-map.py --fetch  →  .az-cache/*.json  →  viz-*/render.py  →  viz-*/output/
```

## Renderers

| Directory | Output | Dependencies | Detail levels |
|-----------|--------|-------------|---------------|
| `viz-mermaid/` | `.mermaid` files | None | l1, l2, l3 |
| `viz-d3/` | Single HTML | None (D3 via CDN) | Interactive drill-down |
| `viz-graphviz/` | `.svg` + `.dot` | `pip install graphviz` + system `dot` | l1, l2, l3 |
| `viz-pyvis/` | Single HTML | `pip install pyvis` | Flat interactive graph |

### Detail levels (Mermaid / Graphviz)

- **l1** — Subscription boxes with aggregate counts, peering edges
- **l2** — + VNet clusters with CIDR ranges and subnet summaries
- **l3** — + Individual VMs, private endpoints, NSG badges

## Prerequisites

- `az` CLI logged in with read access
- `az extension add --name resource-graph`
- Python 3.10+
