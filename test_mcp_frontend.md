# Testing MCP Integration from Frontend

## Test Queries

### 1. MCP-Only (Live Data) - Should show 🔌 icon
```
What is the current health status of the network?
Show me live CPU usage
What is the real-time bandwidth utilization?
Get latest memory statistics
Show current status of all devices
```

### 2. Neo4j-Only (Topology) - Should show 📊 icon
```
Show me the network topology
Which devices are connected to each other?
What is the relationship between switches and routers?
Show me all neighbors of device X
```

### 3. Hybrid (Both) - Should show 📊 and 🔌 icons
```
Show me switches with high CPU usage
Which devices in the topology have health issues?
Show connected devices with current status
```

## How to Monitor

### Watch backend logs for MCP calls:
```bash
oc logs -n gbaia deployment/backend-api -f | grep -E "(query_intent|mcp_tool_call|data_source)"
```

### Check frontend response:
Look for the **Data Sources** section at the bottom of each answer with icons:
- 📊 Neo4j Graph Database
- 🔌 MCP Live Data

## Expected Behavior

1. Type an MCP query (e.g., "current health status")
2. Backend classifies intent as "mcp"
3. Backend calls MCP tools via JSON-RPC
4. Response includes source tracking
5. Frontend displays answer with 🔌 icon

## Troubleshooting

If MCP queries fail:
- Check backend pod logs: `oc logs -n gbaia deployment/backend-api --tail=50`
- Verify MCP server is running: `oc get pods -n gbaia | grep mcp-server`
- Check MCP client connection: Look for "sse_connected" in backend logs
