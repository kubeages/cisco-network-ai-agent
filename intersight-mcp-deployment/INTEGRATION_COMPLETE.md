# Intersight MCP Integration - Complete ✅

## Summary

Successfully integrated Cisco Intersight into the Network AI Agent. The system can now query compute/server information alongside network data.

**Deployment Date**: 2026-05-29
**Components Updated**: Backend API, Intersight MCP Server
**Status**: ✅ Deployed and Ready for Testing

---

## What Was Implemented

### 1. Intersight MCP Server ✅
- **Deployed**: `intersight-mcp` pod running in `gbaia` namespace
- **Endpoint**: `http://intersight-mcp:3000`
- **Tools Available**: 66 core read-only tools
- **Health**: Healthy and responding

### 2. Backend Integration ✅
- **Query Classification**: Detects Intersight keywords
- **Query Function**: `query_intersight_with_llm()`
- **HTTP Client**: Communicates with Intersight MCP via REST API
- **Routing**: Automatic routing for compute-related queries

### 3. Environment Configuration ✅
```bash
INTERSIGHT_MCP_ENABLED=true
INTERSIGHT_MCP_URL=http://intersight-mcp:3000
INTERSIGHT_BASE_URL=https://intersight.com/api/v1
```

---

## Architecture

```
┌──────────────────────────────────────────────────────┐
│                    User Query                         │
│         "List all UCS servers"                        │
└─────────────────────┬────────────────────────────────┘
                      │
                      ▼
        ┌─────────────────────────┐
        │   Backend API           │
        │   classify_query_intent │
        │   → "intersight"        │
        └────────────┬────────────┘
                     │
                     ▼
        ┌─────────────────────────────────┐
        │ query_intersight_with_llm()     │
        │ 1. GET /health (check status)   │
        │ 2. GET /api/tools (list tools)  │
        │ 3. POST /api/execute (call tool)│
        └────────────┬────────────────────┘
                     │
                     ▼
        ┌─────────────────────────────────┐
        │   Intersight MCP Server         │
        │   Port: 3000                    │
        │   - list_compute_servers        │
        │   - list_vnics                  │
        │   - get_server_health           │
        │   - 63 more tools...            │
        └────────────┬────────────────────┘
                     │
                     ▼
        ┌─────────────────────────────────┐
        │   Cisco Intersight API          │
        │   Account: CAI-NL               │
        │   Region: us-east-1             │
        └─────────────────────────────────┘
```

---

## Query Classification

The backend now classifies queries into 4 types:

| Type | Description | Example Queries |
|------|-------------|-----------------|
| **neo4j** | Static topology, relationships | "List all tenants", "Show EPG structure" |
| **mcp** | Live network metrics | "Current bandwidth usage", "Network health status" |
| **intersight** | Compute/server data | "List all UCS servers", "Show server health" |
| **hybrid** | Combined queries | "Network and compute health across all fabrics" |

### Intersight Keywords (Auto-Detection):
- `server`, `ucs`, `compute`
- `blade`, `rack unit`, `chassis`
- `fabric interconnect`, `hyperflex`
- `vnic`, `adapter`
- `server health`, `server alarm`
- `server cpu`, `server memory`

---

## Available Queries

### Server Inventory:
```
User: "List all UCS servers"
AI: Lists servers with names, models, status from Intersight

User: "Show me blade servers"
AI: Lists blade servers in chassis

User: "What rack servers do we have?"
AI: Lists rack-mounted servers
```

### Server Health:
```
User: "Show server health"
AI: Returns health status, alarms, telemetry

User: "Are there any server alarms?"
AI: Lists active server alarms by severity

User: "What's the CPU usage of servers?"
AI: Returns server telemetry data
```

### Network Adapters (for future MAC correlation):
```
User: "Show me server network adapters"
AI: Lists vNICs with MAC addresses

User: "What MAC addresses are assigned to servers?"
AI: Lists MAC pool allocations
```

### Policies:
```
User: "Show server boot policies"
AI: Lists boot order policies

User: "What LAN connectivity policies exist?"
AI: Lists LAN policies for server connectivity
```

---

## Testing

### Test 1: Basic Server Query
```bash
# Via UI or API:
Question: "List all UCS servers"

Expected:
- 🖥️  Query classified as: intersight
- Backend calls Intersight MCP
- Returns list of servers with names, models, status
```

### Test 2: Health Query
```bash
Question: "Show me server health status"

Expected:
- Classified as intersight
- Returns health metrics, alarms, telemetry
```

### Test 3: Network Adapter Query
```bash
Question: "Show me server network adapters with MAC addresses"

Expected:
- Classified as intersight
- Returns vNIC list with MAC addresses
```

### Test via Backend Logs:
```bash
# Watch logs for Intersight queries
oc logs -n gbaia deployment/backend-api -f | grep -E "intersight|Intersight|🖥️"

# Expected output when query is processed:
# 🖥️  Intersight query detected
# 🔧 Intersight MCP: 66 tools available
# 🔧 Selected Intersight tool: list_compute_servers
```

---

## Code Changes

### backend/backend.py:

**1. Configuration** (lines 346-348):
```python
# --- Intersight MCP Configuration ---
INTERSIGHT_MCP_ENABLED = os.getenv("INTERSIGHT_MCP_ENABLED", "true").lower() == "true"
INTERSIGHT_MCP_URL = os.getenv("INTERSIGHT_MCP_URL", "http://intersight-mcp:3000")
```

**2. Query Classification** (lines 1059-1077):
```python
def classify_query_intent(question: str) -> str:
    """
    Returns:
        "neo4j" | "mcp" | "intersight" | "hybrid"
    """
    question_lower = question.lower()
    
    # Intersight keywords (checked first - most specific)
    intersight_keywords = [
        "server", "ucs", "compute", "blade", "rack unit",
        "chassis", "fabric interconnect", "hyperflex",
        "vnic", "adapter", "server health", "server alarm"
    ]
    
    if INTERSIGHT_MCP_ENABLED and any(keyword in question_lower for keyword in intersight_keywords):
        print(f"🖥️  Intersight query detected")
        return "intersight"
```

**3. Query Function** (lines 1415-1519):
```python
async def query_intersight_with_llm(question: str, chat_history: str = "") -> tuple[str, List[DataSource]]:
    """
    Query Cisco Intersight via MCP HTTP server.
    """
    # 1. Check health
    # 2. Get available tools
    # 3. Select best tool via keyword matching + LLM
    # 4. Execute tool via POST /api/execute
    # 5. Synthesize answer with LLM
    # 6. Return answer + data sources
```

**4. Query Routing** (lines 1665-1670):
```python
if query_intent == "intersight":
    print("🖥️  Executing Intersight query...")
    answer, data_sources = await query_intersight_with_llm(query.question, history_str)
```

---

## Data Flow Example

**User Query**: "List all UCS servers"

1. **Classification**:
   ```
   classify_query_intent("List all UCS servers")
   → "intersight" (detected keyword: "ucs", "servers")
   ```

2. **Tool Selection**:
   ```
   Available tools: [list_compute_servers, list_compute_blades, ...]
   LLM selects: "list_compute_servers"
   ```

3. **Execution**:
   ```
   POST http://intersight-mcp:3000/api/execute
   Body: {"tool": "list_compute_servers", "arguments": {}}
   ```

4. **Response**:
   ```json
   {
     "servers": [
       {"name": "UCS-01", "model": "UCSB-B200-M5", "status": "ok"},
       {"name": "UCS-02", "model": "UCSC-C220-M5", "status": "ok"}
     ]
   }
   ```

5. **Synthesis**:
   ```
   LLM: "Found 2 UCS servers: UCS-01 (blade UCSB-B200-M5) and UCS-02 (rack UCSC-C220-M5), both operational."
   ```

---

## Troubleshooting

### Issue: Query not classified as Intersight
**Symptoms**: Network query returns "No data" even though asking about servers

**Check**:
1. Verify keywords: Does query contain "server", "ucs", "compute", etc.?
2. Check logs: `oc logs -n gbaia deployment/backend-api | grep "classified as"`

**Fix**: Add more specific keywords or use explicit phrasing like "UCS servers"

### Issue: "Intersight MCP server is not available"
**Symptoms**: Error message about Intersight not available

**Check**:
```bash
# Check Intersight MCP pod
oc get pods -n gbaia -l app=intersight-mcp

# Check health endpoint
oc exec -n gbaia deployment/backend-api -- \
  curl -s http://intersight-mcp:3000/health
```

**Fix**: Restart Intersight MCP if unhealthy:
```bash
oc rollout restart deployment/intersight-mcp -n gbaia
```

### Issue: Tool execution fails
**Symptoms**: 500 error when executing Intersight tool

**Check Intersight MCP logs**:
```bash
oc logs -n gbaia deployment/intersight-mcp --tail=50
```

**Common causes**:
- API credentials expired/invalid
- Network connectivity to Intersight API
- Tool arguments incorrect

---

## Next Steps

### Phase 3: Neo4j Correlation (Planned)

**Goal**: Link servers to network endpoints via MAC addresses

**Implementation**:
1. Extend ingestor to fetch Intersight inventory
2. Query vNICs to get MAC addresses
3. Match with ACI endpoint MACs
4. Create `Server→CONNECTED_TO→EPG` relationships

**Queries Enabled**:
- "Which servers are in EPG 'database-tier'?"
- "Show me all infrastructure for AppProfile 'web-tier'"
- "Are compute issues causing network problems in tenant X?"

### Phase 4: Hybrid Queries (Planned)

**Goal**: Combined network + compute analysis

**Example**:
```
User: "Why is AppProfile 'web-tier' slow?"
AI: "Network: EPG shows high packet loss (15%)
     Compute: Server UCS-WEB-02 (10.61.131.47) has 98% CPU
     Root cause: Server overload causing network retransmissions"
```

---

## Success Criteria

- [✅] Intersight MCP server deployed and healthy
- [✅] Backend can query Intersight MCP via HTTP
- [✅] Queries classified correctly (intersight vs neo4j/mcp)
- [✅] Tool selection working (keyword + LLM)
- [✅] Answer synthesis from Intersight data
- [⏳] End-to-end user testing (next)
- [⏳] MAC correlation (Phase 3)
- [⏳] Hybrid queries (Phase 4)

---

## Files Modified

```
backend/backend.py
  - Added Intersight configuration (lines 346-348)
  - Added classify_query_intent detection (lines 1059-1077)
  - Added query_intersight_with_llm() (lines 1415-1519)
  - Added intersight routing (lines 1665-1670, 1837-1850)

openshift/intersight-mcp/
  - Dockerfile (custom multi-stage build)
  - buildconfig.yaml (binary build)
  - deployment.yaml (1 replica, port 3000)
  - service.yaml (ClusterIP)
  - secret.yaml (API credentials)

Deployment configuration:
  - backend-api: Added INTERSIGHT_MCP_ENABLED, INTERSIGHT_MCP_URL env vars
```

---

## Testing Commands

```bash
# 1. Check all components are running
oc get pods -n gbaia | grep -E "intersight|backend"

# 2. Test Intersight MCP directly
oc exec -n gbaia deployment/backend-api -- \
  curl -s http://intersight-mcp:3000/health | jq

# 3. Test backend can reach Intersight MCP
oc exec -n gbaia deployment/backend-api -- \
  curl -s http://intersight-mcp:3000/api/tools | jq '.tools[] | .name' | head -10

# 4. Watch logs during query
oc logs -n gbaia deployment/backend-api -f | grep -E "🖥️|Intersight"

# 5. Test via UI
# Navigate to application and ask: "List all UCS servers"
```

---

## Support

**Documentation**:
- Intersight MCP Server: https://github.com/jim-coyne/Intersight_MCP
- ASSESSMENT.md: Integration analysis
- DEPLOYMENT_SUMMARY.md: Deployment details

**Logs**:
```bash
# Backend
oc logs -n gbaia deployment/backend-api --tail=100

# Intersight MCP
oc logs -n gbaia deployment/intersight-mcp --tail=100
```

**Health Checks**:
```bash
# Backend health
curl http://backend-api:8000/

# Intersight MCP health
curl http://intersight-mcp:3000/health
```

---

## Summary

✅ **Integration Complete**
- Intersight MCP Server: Deployed & Healthy
- Backend Integration: Code deployed
- Query Routing: Functional
- Tool Execution: Ready

**Ready for User Testing!**

Try queries like:
- "List all UCS servers"
- "Show me server health status"
- "What network adapters do servers have?"

Next: MAC-based correlation to link servers with network endpoints.
