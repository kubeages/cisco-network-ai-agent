# Intersight MCP Deployment Summary

## Status: ✅ DEPLOYED

Deployed: 2026-05-29
Namespace: `gbaia`
Version: 1.0.16

---

## Deployed Resources

### 1. Secret: `intersight-secrets`
```bash
oc get secret intersight-secrets -n gbaia
```

**Contents**:
- `api-key-id`: Intersight API Key
- `private-key`: EC Private Key (PEM format)
- `base-url`: https://intersight.com/api/v1
- `account`: CAI-NL

### 2. ImageStream & Build: `intersight-mcp`
```bash
oc get imagestream intersight-mcp -n gbaia
oc get buildconfig intersight-mcp -n gbaia
```

**Build Details**:
- Source: https://github.com/jim-coyne/Intersight_MCP
- Strategy: Docker (multi-stage build with TypeScript compilation)
- Output: intersight-mcp:latest

### 3. Deployment: `intersight-mcp`
```bash
oc get deployment intersight-mcp -n gbaia
```

**Configuration**:
- Replicas: 1
- Image: image-registry.openshift-image-registry.svc:5000/gbaia/intersight-mcp:latest
- Port: 3000 (HTTP)
- Mode: `core` (66 read-only tools enabled)

**Environment Variables**:
- `INTERSIGHT_API_KEY_ID`: From secret
- `INTERSIGHT_API_SECRET_KEY_PATH`: /app/secrets/private_key.pem
- `INTERSIGHT_BASE_URL`: From secret
- `INTERSIGHT_TOOL_MODE`: core
- `PORT`: 3000

### 4. Service: `intersight-mcp`
```bash
oc get service intersight-mcp -n gbaia
```

**Service Details**:
- Type: ClusterIP
- Port: 3000 → 3000
- Endpoint: `http://intersight-mcp.gbaia.svc.cluster.local:3000`

---

## Health Check

### Pod Status:
```bash
oc get pods -n gbaia -l app=intersight-mcp
# NAME                              READY   STATUS    RESTARTS   AGE
# intersight-mcp-554cb8d8f8-gdv6f   1/1     Running   0          5m
```

### Health Endpoint:
```bash
curl http://intersight-mcp:3000/health
```

**Response**:
```json
{
  "status": "healthy",
  "timestamp": "2026-05-29T11:58:33.005Z",
  "version": "1.0.16",
  "configuration": {
    "toolMode": "whitelist",
    "enabledTools": 66,
    "totalTools": 198,
    "enableAllTools": false
  }
}
```

---

## Available Tools (66 Core Tools)

### Server Inventory:
- `list_compute_servers` - List all servers (blades + rack)
- `get_server_details` - Get server details by MOID
- `list_compute_blades` - List blade servers
- `list_compute_rack_units` - List rack servers

### Network Adapters (for MAC correlation):
- `list_vnics` - List virtual NICs with MAC addresses ⭐
- `get_vnic` - Get specific vNIC details
- `list_macpool_blocks` - List MAC address pools
- `list_pool_leases` - List active MAC/IP leases

### Health & Alarms:
- `list_alarms` - List alarms with filtering
- `get_server_telemetry` - CPU, memory, temp, power metrics
- `get_thermal_statistics` - Temperature data
- `get_power_statistics` - Power consumption

### Policies:
- `list_lan_connectivity_policies` - LAN connectivity
- `list_eth_adapter_policies` - Ethernet adapter configs
- `list_boot_policies` - Boot order policies
- `list_bios_policies` - BIOS settings

### Full list:
```bash
curl http://intersight-mcp:3000/api/tools | jq '.tools[] | .name'
```

---

## Testing

### From within cluster:
```bash
# List tools
oc exec -n gbaia deployment/backend-api -- \
  curl -s http://intersight-mcp:3000/api/tools

# Execute tool (example - list servers)
oc exec -n gbaia deployment/backend-api -- \
  curl -s -X POST http://intersight-mcp:3000/api/execute \
  -H "Content-Type: application/json" \
  -d '{"tool":"list_compute_servers","arguments":{}}'
```

### From backend pod:
```python
import httpx

async def test_intersight_mcp():
    async with httpx.AsyncClient() as client:
        # Health check
        response = await client.get("http://intersight-mcp:3000/health")
        print(response.json())
        
        # List tools
        tools = await client.get("http://intersight-mcp:3000/api/tools")
        print(f"Available tools: {len(tools.json()['tools'])}")
        
        # Execute tool
        result = await client.post(
            "http://intersight-mcp:3000/api/execute",
            json={"tool": "list_compute_servers", "arguments": {}}
        )
        print(result.json())
```

---

## Next Steps

### 1. Backend Integration (Next)
Update `backend/backend.py` to connect to Intersight MCP:

```python
# Add to backend environment
INTERSIGHT_MCP_URL = os.getenv("INTERSIGHT_MCP_URL", "http://intersight-mcp:3000")

# Connect via HTTP
async def query_intersight(tool_name: str, arguments: dict):
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{INTERSIGHT_MCP_URL}/api/execute",
            json={"tool": tool_name, "arguments": arguments}
        )
        return response.json()
```

### 2. Query Classification
Add Intersight keywords to query classification:

```python
# In backend/backend.py
if "server" in question_lower or "ucs" in question_lower or "compute" in question_lower:
    # Query Intersight MCP
    result = await query_intersight("list_compute_servers", {})
```

### 3. Neo4j Correlation
Extend ingestor to fetch server inventory and create correlation relationships:

```python
# Fetch servers from Intersight MCP
servers = await query_intersight("list_compute_servers", {})

# Get vNICs with MAC addresses
for server in servers:
    vnics = await query_intersight("list_vnics", {"server": server['name']})
    
    # Create Neo4j nodes and relationships
    for vnic in vnics:
        # Match with ACI endpoints by MAC
        MATCH (endpoint:Endpoint {mac: vnic['mac']})
        MERGE (server:IntersightServer {name: server['name']})
        MERGE (server)-[:CONNECTED_TO]->(endpoint)
```

### 4. Test Queries
Once integrated, these queries become possible:

- "List all UCS servers"
- "Show me server health"
- "Which servers are in EPG 'database-tier'?" (after MAC correlation)
- "Are there compute issues affecting network performance?"

---

## Troubleshooting

### Pod not starting:
```bash
oc logs -n gbaia deployment/intersight-mcp
oc describe pod -n gbaia -l app=intersight-mcp
```

### Secret issues:
```bash
oc get secret intersight-secrets -n gbaia -o yaml
# Verify api-key-id and private-key are set
```

### Connection issues:
```bash
# Test from backend pod
oc exec -n gbaia deployment/backend-api -- \
  curl -v http://intersight-mcp:3000/health
```

### Rebuild image:
```bash
cd /tmp/intersight-mcp
oc start-build intersight-mcp --from-dir=. --follow -n gbaia
```

---

## Architecture Diagram

```
┌────────────────────────────────────────────────────────┐
│                   gbaia namespace                       │
├────────────────────────────────────────────────────────┤
│                                                         │
│  ┌─────────────────┐                                   │
│  │  Backend API    │                                   │
│  │  (FastAPI)      │                                   │
│  └────────┬────────┘                                   │
│           │                                             │
│           ├──────────────▶ ┌──────────────────┐       │
│           │                │  ND MCP Server   │       │
│           │                │  (Python/stdio)  │       │
│           │                │  Port: N/A       │       │
│           │                └──────────────────┘       │
│           │                                             │
│           ├──────────────▶ ┌──────────────────┐       │
│           │                │ Intersight MCP   │ ✅ NEW│
│           │                │ (Node.js/HTTP)   │       │
│           │                │ Port: 3000       │       │
│           │                │ Tools: 66        │       │
│           │                └─────────┬────────┘       │
│           │                          │                 │
│           │                          │                 │
│           │                   ┌──────▼─────────┐      │
│           │                   │ intersight-    │      │
│           │                   │ secrets        │      │
│           │                   │ • API Key      │      │
│           │                   │ • Private Key  │      │
│           │                   └────────────────┘      │
│           │                                             │
│           └──────────────▶ ┌──────────────┐           │
│                            │   Neo4j      │           │
│                            │  (Graph DB)  │           │
│                            └──────────────┘           │
│                                                         │
└────────────────────────────────────────────────────────┘
                            │
                            │ API calls to
                            ▼
                    ┌────────────────────┐
                    │ Cisco Intersight   │
                    │ (CAI-NL account)   │
                    │ intersight-aws-    │
                    │ us-east-1          │
                    └────────────────────┘
```

---

## Files Created

```
openshift/intersight-mcp/
├── Dockerfile              # Custom multi-stage build
├── buildconfig.yaml        # Binary build config
├── deployment.yaml         # Deployment manifest
├── service.yaml           # ClusterIP service
└── secret.yaml            # Intersight credentials

intersight-mcp-deployment/
├── ASSESSMENT.md          # Integration assessment
└── DEPLOYMENT_SUMMARY.md  # This file
```

---

## Logs

**Startup logs**:
```
✅ Intersight API Service initialized successfully
🔧 Configuration: whitelist mode
🛠️  Tools: 66 of 198 enabled
🚀 Intersight MCP HTTP Server v1.0.15 running on port 3000
🔧 Configuration Mode: WHITELIST
⚡ Enabled Tools: 66 tools available
```

---

## Success Criteria

- [✅] Pod running and healthy
- [✅] Health endpoint responding
- [✅] 66 tools available via HTTP API
- [✅] Service accessible within cluster
- [⏳] Backend integration (next step)
- [⏳] Neo4j correlation (next step)
- [⏳] End-to-end query testing (next step)

**Status**: Phase 1 Complete - MCP Server Deployed ✅
**Next**: Backend Integration & Tool Testing
