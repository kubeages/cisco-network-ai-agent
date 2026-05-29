# Intersight MCP Server Assessment

Repository: https://github.com/jim-coyne/Intersight_MCP

## Verdict: ✅ **HIGHLY SUITABLE**

This MCP server is perfect for integration. It has everything we need for compute-network correlation.

---

## Key Features Needed for Our Use Case

### ✅ Server Inventory
- `list_compute_blades` - Blade servers
- `list_compute_rack_units` - Rack servers
- `get_top_system` - System details

### ✅ Network Adapter Information (for MAC correlation)
- `list_vnics` - Virtual NICs with MAC addresses ⭐
- `list_macpool_blocks` - MAC pools
- `list_pool_leases` - Active MAC allocations

### ✅ Health & Alarms (for fault correlation)
- `list_alarms` - Server alarms by severity
- `get_server_telemetry` - CPU, memory, temp, power
- `createSecurityHealthCheckReport()` - Comprehensive health analysis

### ✅ Policy Information
- `list_lan_connectivity_policies` - LAN policies
- `list_eth_adapter_policies` - Ethernet adapter configs

---

## Architecture Comparison

### Nexus Dashboard MCP (Current):
- **Transport**: stdio (process pipes)
- **Language**: Python
- **Deployment**: Sidecar container with stdio communication

### Intersight MCP (This One):
- **Transport**: HTTP/SSE (REST API) ⭐ **Better for containers!**
- **Language**: Node.js/TypeScript
- **Deployment**: Standalone HTTP service on port 3000

**Advantage**: HTTP transport is cleaner for Kubernetes/OpenShift - no stdio pipe complexity!

---

## Deployment Approach

### Same Namespace: `gbaia`

```
┌─────────────────────────────────────────────────┐
│              gbaia namespace                     │
├─────────────────────────────────────────────────┤
│                                                  │
│  ┌────────────────┐    ┌──────────────────┐    │
│  │  Backend API   │───▶│  ND MCP Server   │    │
│  │  (FastAPI)     │    │  (Python/stdio)  │    │
│  └────────┬───────┘    └──────────────────┘    │
│           │                                      │
│           ├───────────▶┌──────────────────┐    │
│           │            │ Intersight MCP   │    │
│           │            │ (Node.js/HTTP)   │←─┐ │
│           │            │ Port: 3000       │  │ │
│           │            └──────────────────┘  │ │
│           │                                  │ │
│           └──────────▶┌──────────────┐      │ │
│                       │   Neo4j      │      │ │
│                       └──────────────┘      │ │
│                                             │ │
│  Secrets:                                   │ │
│  • gbaia-secrets (existing)                 │ │
│  • intersight-secrets (new) ────────────────┘ │
│                                                │
└────────────────────────────────────────────────┘
```

---

## Required Modifications

### 1. Dockerfile Enhancement
**Current Dockerfile issue**: Expects pre-built `build/` directory

**Solution**: Build TypeScript inside container

```dockerfile
# Add build step
RUN npm ci
RUN npm run build

# Then copy and run
CMD ["node", "build/http-server.js"]
```

### 2. OpenShift Resources Needed

**BuildConfig** (`intersight-mcp-buildconfig.yaml`):
- Source: GitHub repo
- Build strategy: Docker
- Output: Image to internal registry

**Deployment** (`intersight-mcp-deployment.yaml`):
- 1 replica
- Port 3000 exposed
- Environment variables from secret
- Volume mount for private key

**Service** (`intersight-mcp-service.yaml`):
- ClusterIP service
- Port 3000 → 3000
- Selector: app=intersight-mcp

**Secret** (`intersight-secrets`):
- API Key ID
- Private Key (PEM file)
- Base URL

### 3. Backend Integration

**Update `backend/backend.py`** to connect to Intersight MCP via HTTP:

```python
# mcp_client.py
INTERSIGHT_MCP_URL = os.getenv("INTERSIGHT_MCP_URL", "http://intersight-mcp:3000")

async def connect_intersight_mcp():
    """Connect to Intersight MCP server via HTTP"""
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{INTERSIGHT_MCP_URL}/health")
        if response.status_code == 200:
            print("✅ Connected to Intersight MCP")
        
        # List available tools
        tools_response = await client.get(f"{INTERSIGHT_MCP_URL}/tools")
        tools = tools_response.json()
        print(f"📋 Intersight MCP tools: {len(tools)}")
```

---

## Credentials Configuration

### From Your `.env`:
```bash
INTERSIGHT_ACCOUNT=CAI-NL
INTERSIGHT_REGION=intersight-aws-us-east-1
INTERSIGHT_APIKEY=5fcdec297564612d331ea551/68ee0c397564613001cdc614/6a17e9e775646130017c2c8d
INTERSIGHT_SECRETKEY=<EC PRIVATE KEY>
```

### Map to Intersight MCP format:
```bash
INTERSIGHT_API_KEY_ID=5fcdec297564612d331ea551/68ee0c397564613001cdc614/6a17e9e775646130017c2c8d
INTERSIGHT_PRIVATE_KEY_PATH=/app/secrets/private_key.pem
INTERSIGHT_BASE_URL=https://intersight-aws-us-east-1.intersight.com
```

---

## Testing Plan

### Phase 1: Deploy MCP Server (30 minutes)
1. Create OpenShift resources
2. Build and deploy container
3. Verify health endpoint: `curl http://intersight-mcp:3000/health`
4. Test tool listing: `curl http://intersight-mcp:3000/tools`

### Phase 2: Backend Integration (1 hour)
1. Add HTTP MCP client to backend
2. Implement tool calling
3. Test basic query: "List all UCS servers"

### Phase 3: Correlation (2 hours)
1. Query server vNICs to get MAC addresses
2. Match with ACI endpoint MACs
3. Test query: "Which servers are in EPG 'database-tier'?"

---

## Expected Benefits

### Immediate (Week 1):
- ✅ Query Intersight data: "List all UCS servers"
- ✅ Get server health: "What's the health of server UCS-01?"
- ✅ View alarms: "Show me critical server alarms"

### Short Term (Week 2-3):
- ✅ MAC correlation: "Which servers are in AppProfile X?"
- ✅ Health correlation: "Are compute issues causing network problems?"

### Long Term (Month 2+):
- ✅ Full stack visibility: "Show me all infrastructure for application Y"
- ✅ Root cause analysis: Network + Compute correlation
- ✅ Capacity planning: Combined resource utilization

---

## Next Steps

**Option A**: Deploy now (I'll create all the manifests)
**Option B**: Test locally first with docker-compose
**Option C**: Review and modify the approach

**Recommendation**: Option A - deploy directly to OpenShift since we have the credentials and it's low risk (read-only tools by default).

Ready to proceed?
