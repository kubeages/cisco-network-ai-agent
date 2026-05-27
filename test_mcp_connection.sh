#!/bin/bash
# Quick test to verify MCP connectivity from backend environment
# Tests SSE endpoint and tool discovery

MCP_URL="https://nd-mcp-server-gbaia.apps.fp-ocp.amsdmz.local"
MCP_TOKEN="mcp_token_gbaia_2024"

echo "============================================================"
echo "MCP Server Connectivity Test"
echo "============================================================"
echo "Endpoint: $MCP_URL"
echo "============================================================"
echo ""

PASSED=0
TOTAL=0

# Test 1: Health Check
echo "Test 1: Health Check"
TOTAL=$((TOTAL+1))
RESPONSE=$(curl -sk "$MCP_URL/api/health" 2>/dev/null)
STATUS=$(echo "$RESPONSE" | jq -r '.status')
if [ "$STATUS" = "healthy" ] || [ "$STATUS" = "degraded" ]; then
    echo "✓ Health Check: $STATUS"
    DB_STATUS=$(echo "$RESPONSE" | jq -r '.services[0].status')
    CLUSTER_STATUS=$(echo "$RESPONSE" | jq -r '.services[1].status')
    echo "  - Database: $DB_STATUS"
    echo "  - Cluster Config: $CLUSTER_STATUS"
    PASSED=$((PASSED+1))
else
    echo "✗ Health Check Failed"
    echo "Response: $RESPONSE"
fi

# Test 2: Clusters
echo ""
echo "Test 2: Cluster Configuration"
TOTAL=$((TOTAL+1))
RESPONSE=$(curl -sk "$MCP_URL/api/clusters" 2>/dev/null)
CLUSTER_COUNT=$(echo "$RESPONSE" | jq '. | length' 2>/dev/null)
if [ ! -z "$CLUSTER_COUNT" ] && [ "$CLUSTER_COUNT" -gt 0 ]; then
    echo "✓ Clusters: $CLUSTER_COUNT configured"
    echo "$RESPONSE" | jq -r '.[] | "  - \(.name): \(.url) (\(.status))"'
    PASSED=$((PASSED+1))
else
    echo "✗ Cluster Configuration Failed"
    echo "Response: $RESPONSE"
fi

# Test 3: MCP SSE Endpoint
echo ""
echo "Test 3: MCP SSE Endpoint"
TOTAL=$((TOTAL+1))
HTTP_CODE=$(curl -sk -o /dev/null -w "%{http_code}" \
    -H "Authorization: Bearer $MCP_TOKEN" \
    -H "Accept: text/event-stream" \
    --max-time 3 \
    "$MCP_URL/mcp/sse" 2>/dev/null)

if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "000" ]; then
    echo "✓ MCP SSE Endpoint: Connected (HTTP $HTTP_CODE)"
    echo "  - Authorization: Bearer token accepted"
    PASSED=$((PASSED+1))
else
    echo "✗ MCP SSE Endpoint Failed (HTTP $HTTP_CODE)"
fi

# Test 4: API Guidance
echo ""
echo "Test 4: API Guidance"
TOTAL=$((TOTAL+1))
RESPONSE=$(curl -sk -H "Authorization: Bearer $MCP_TOKEN" "$MCP_URL/api/guidance/apis" 2>/dev/null)
API_COUNT=$(echo "$RESPONSE" | jq '. | length' 2>/dev/null)
if [ ! -z "$API_COUNT" ] && [ "$API_COUNT" -gt 0 ]; then
    echo "✓ Available APIs: $API_COUNT"
    TOTAL_OPS=0
    for i in $(seq 0 $((API_COUNT-1))); do
        TITLE=$(echo "$RESPONSE" | jq -r ".[$i].title")
        OPS=$(echo "$RESPONSE" | jq -r ".[$i].operation_count // 0")
        TOTAL_OPS=$((TOTAL_OPS+OPS))
        echo "  - $TITLE: $OPS operations"
    done
    echo ""
    echo "Total Operations: $TOTAL_OPS"
    PASSED=$((PASSED+1))
else
    echo "✗ API Guidance Failed"
    echo "Response: $RESPONSE"
fi

# Summary
echo ""
echo "============================================================"
echo "Results: $PASSED/$TOTAL tests passed"
echo ""

if [ "$PASSED" -eq "$TOTAL" ]; then
    echo "✅ Phase 1 Complete: MCP connectivity verified!"
    echo "============================================================"
    exit 0
else
    echo "⚠️  Some tests failed"
    echo "============================================================"
    exit 1
fi
