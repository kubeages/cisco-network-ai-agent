# gbaia — Network AI Agent for Cisco Data Centers

Conversational AI agent that answers natural-language questions about a
Cisco ACI / Nexus Dashboard / Intersight environment by combining a
live knowledge graph, real-time API queries, and a self-hosted LLM.

Ask questions like:

- *"How many fabrics do we have and what are their names?"*
- *"Tell me about the ACI node `aci-site-01-apic-02` — role, model, faults."*
- *"Show me the UCS server `ucs-prod-01-6454-5` — health, CPU, memory."*
- *"Are there any critical anomalies on the `cai-emea-vxlan-site-01` fabric?"*

---

## What it does

Three Cisco product families, one chat window.

| Product | What we pull | How | Where it lands |
|---|---|---|---|
| **Cisco APIC (ACI)** | Tenants, EPGs, BDs, VRFs, Subnets, App Profiles, Fabric Nodes, Endpoints, Faults | Direct REST (`aaaLogin` + `class` queries) | Neo4j graph |
| **Cisco Nexus Dashboard** | Fabrics, Anomalies, Advisories, node inventory | Direct REST (X-Nd-Apikey auth) + MCP for live queries | Neo4j graph + on-demand via MCP |
| **Cisco Intersight** | Servers, Chassis, Fabric Interconnects, vNICs, adapters, MAC correlations | Python SDK (bulk load) + TS MCP (on-demand) | Neo4j graph + on-demand via MCP |

The ingestor runs every 5 minutes and refreshes the Neo4j graph.
The backend routes each user question to the best data source at runtime.

---

## Why it's useful

- **Unified view across three siloed product families.** Same question,
  same UI, whether the answer lives in APIC's policy tree, Nexus Dashboard's
  anomaly stream, or Intersight's compute inventory.
- **Live *and* historical data.** Neo4j gives you fast graph queries over
  a fresh snapshot; MCP gives you live per-tool call for questions the
  graph can't answer.
- **Runs on your cluster.** LLM (Qwen 3.6 27B) served by vLLM inside the
  same OpenShift cluster — no prompts or credentials leave your network.
- **Prompt & response inspection.** Every turn is filtered through Cisco
  AI Defense (SaaS or on-prem) with a policy configured in the AI Defense
  console. Prompt-injection and PII-leak attempts are blocked at the edge.
- **One-shot deploy.** `openshift/deploy.sh` brings up the entire stack
  (7 workloads + 3 secrets + 5 image builds) from an empty namespace.

---

## What's under the hood

Seven workloads, one namespace:

| Workload | Purpose |
|---|---|
| **gbaia-frontend** (Gradio) | Chat UI, capability badges, session state |
| **gbaia-backend** (FastAPI) | Query routing, LLM orchestration, MCP client |
| **gbaia-apic-ingestor** | 5-min bulk load: APIC + ND + Intersight → Neo4j |
| **gbaia-neo4j** | Knowledge graph — Fabrics, Tenants, EPGs, Nodes, Servers, …  |
| **gbaia-nd-mcp-server** | MCP server exposing ~150 ND REST endpoints as LLM tools |
| **gbaia-nd-mcp-postgres** | ND-MCP's persistent state (cluster creds, sessions) |
| **gbaia-intersight-mcp** | MCP server exposing Intersight tools (HTTP mode, 3000) |

See `PlatformArchitecture.svg` for the full picture, and `DemoFlow.svg`
for how a single question travels through the stack.

---

## Getting started

Clone, fill in credentials, deploy:

```bash
git clone https://github.com/kubeages/cisco-network-ai-agent.git
cd cisco-network-ai-agent/openshift
cp .env.example .env
# edit .env — set APIC/ND/Intersight/LLM URLs + creds
./deploy.sh
```

Everything under `openshift/` is portable across OpenShift 4 clusters —
Deployments use `image.openshift.io/triggers` annotations so image
references are namespace-independent, and the deploy script creates
all three Secrets from `.env` values (nothing sensitive touches git).

Full deployment reference: `openshift/README.md`.

---

## What's intentionally out of scope

- Multi-tenant / multi-user auth — this is single-tenant for demo use.
- Write actions to APIC / ND / Intersight — MCP is used in **read-only**
  mode; the backend never issues configuration changes.
- Automatic remediation — the LLM proposes fixes; humans execute them.
- Long-term telemetry storage — Neo4j holds the current fabric snapshot,
  not historical time-series (that's what Nexus Dashboard is for).
