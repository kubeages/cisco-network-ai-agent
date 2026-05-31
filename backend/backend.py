"""
================================================================================
Network AI Agent - Backend API
================================================================================

This module provides the core logic for the Network AI Agent, served via a
FastAPI application. It acts as the "Brain" of the system.

Purpose:
--------
The API exposes a single endpoint (`/ask`) that accepts natural language
questions about a network. It translates these questions into database queries,
retrieves the information from a Neo4j knowledge graph, and formulates a
helpful, context-aware answer.

Key Functionality:
------------------
- Conditional LLM Loading: The application can be configured to use either
  the OpenAI API or a self-hosted, vLLM-compatible model (like Phi or Mistral). This is
  controlled by the `LOCAL_LLM_URL` environment variable. If the variable is
  set, it connects to the local model; otherwise, it defaults to OpenAI.

- GraphCypherQAChain: It uses LangChain's `GraphCypherQAChain` to implement
  a two-step reasoning process:
    1.  **Cypher Generation:** An LLM (`cypher_llm`) generates a Cypher query
        based on the user's question, chat history, and the database schema.
    2.  **Answer Synthesis:** The query results are passed to a second LLM
        (`qa_llm`) which formulates a final, human-readable answer and
        suggests recommended actions for troubleshooting.

- Dynamic Suggestions: After generating an answer, a third LLM call is
  made to create a list of relevant follow-up questions, which are sent back
  to the frontend to guide the user.

- Conversational Context: The system is designed to handle multi-turn
  conversations by passing the chat history to the Cypher generation step,
  allowing the agent to understand follow-up questions.

API Endpoint:
-------------
- `POST /ask`: Accepts a JSON payload with a "question" and "chat_history"
  and returns a JSON object with the "answer" and a list of "suggestions".

"""

import os, re, httpx, json, asyncio, tiktoken
from fastapi import FastAPI, HTTPException, Security, Depends
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field
from typing import List, AsyncGenerator, Optional, Dict, Any

from langchain_openai import ChatOpenAI
from langchain_classic.chains import GraphCypherQAChain
from langchain_community.graphs import Neo4jGraph
from langchain_core.prompts import PromptTemplate

# MCP Client for Nexus Dashboard integration
from mcp_client import init_mcp_client, shutdown_mcp_client, get_mcp_client

# --- Optional Splunk Observability / OpenTelemetry Instrumentation ---
SPLUNK_ENABLED = os.getenv("SPLUNK_OBSERVABILITY_ENABLED", "false").lower() == "true"

if SPLUNK_ENABLED:
    print("🔭 Splunk Observability enabled - initializing OpenTelemetry...")
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_VERSION, DEPLOYMENT_ENVIRONMENT
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.logging import LoggingInstrumentor

    # Get configuration from environment
    OTEL_EXPORTER_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://splunk-otel-collector-agent.splunk-otel.svc.cluster.local:4317")
    SPLUNK_ACCESS_TOKEN = os.getenv("SPLUNK_ACCESS_TOKEN", "")
    OTEL_SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "gbaia-backend")
    OTEL_ENVIRONMENT = os.getenv("OTEL_ENVIRONMENT", "production")

    # Create resource with service metadata
    resource = Resource.create({
        SERVICE_NAME: OTEL_SERVICE_NAME,
        SERVICE_VERSION: "1.0.0",
        DEPLOYMENT_ENVIRONMENT: OTEL_ENVIRONMENT,
        "service.namespace": "gbaia",
    })

    # Configure tracer provider
    tracer_provider = TracerProvider(resource=resource)

    # Configure OTLP exporter (sends to local OTel collector)
    otlp_exporter = OTLPSpanExporter(
        endpoint=OTEL_EXPORTER_ENDPOINT,
        insecure=True  # Using cluster-internal communication
    )
    tracer_provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
    trace.set_tracer_provider(tracer_provider)

    # Instrument httpx for outgoing HTTP calls (LLM requests)
    HTTPXClientInstrumentor().instrument()

    # Instrument logging
    LoggingInstrumentor().instrument(set_logging_format=True)

    # Create tracer for custom Neo4j spans
    neo4j_tracer = trace.get_tracer("neo4j.driver", "1.0.0")

    print(f"✅ OpenTelemetry initialized - sending traces to {OTEL_EXPORTER_ENDPOINT}")
else:
    neo4j_tracer = None
    print("🔭 Splunk Observability disabled (set SPLUNK_OBSERVABILITY_ENABLED=true to enable)")


# --- Optional Cisco AI Defense Integration ---
AI_DEFENSE_ENABLED = os.getenv("AI_DEFENSE_ENABLED", "false").lower() == "true"
AI_DEFENSE_API_KEY = os.getenv("AI_DEFENSE_API_KEY", "")
AI_DEFENSE_ENDPOINT = os.getenv("AI_DEFENSE_ENDPOINT", "https://us.api.inspect.aidefense.security.cisco.com")
AI_DEFENSE_INSPECT_PROMPTS = os.getenv("AI_DEFENSE_INSPECT_PROMPTS", "true").lower() == "true"
AI_DEFENSE_INSPECT_RESPONSES = os.getenv("AI_DEFENSE_INSPECT_RESPONSES", "true").lower() == "true"
# When USE_POLICY is true, the dashboard policy is used instead of inline rules
AI_DEFENSE_USE_POLICY = os.getenv("AI_DEFENSE_USE_POLICY", "false").lower() == "true"

# Default rules to check - only used when AI_DEFENSE_USE_POLICY is false
# Available rules per API spec (ai_defense_ap_is_1_0_0_2025_02_28.json):
# - Code Detection, Harassment, Hate Speech, PCI, PHI, PII
# - Prompt Injection, Profanity, Sexual Content & Exploitation
# - Social Division & Polarization, Violence & Public Safety Threats
AI_DEFENSE_RULES = [
    {"rule_name": "PII", "entity_types": ["Email Address", "IP Address", "Phone Number"]},
    {"rule_name": "PCI"},  # Payment Card Industry (credit cards)
    {"rule_name": "Prompt Injection"},
    {"rule_name": "Code Detection"},
    {"rule_name": "Harassment"},
    {"rule_name": "Hate Speech"},
    {"rule_name": "Profanity"},
    {"rule_name": "Violence & Public Safety Threats"},
]

class AIDefenseResult(BaseModel):
    """Result from Cisco AI Defense inspection"""
    is_safe: bool = True
    severity: str = "NONE_SEVERITY"
    classifications: List[str] = Field(default_factory=list)
    violated_rules: List[str] = Field(default_factory=list)
    explanation: Optional[str] = None
    attack_technique: Optional[str] = None
    action: str = "allow"  # "allow" or "block"

def inspect_with_ai_defense(content: str, role: str = "user") -> AIDefenseResult:
    """
    Inspect content using Cisco AI Defense API.

    Args:
        content: The text content to inspect (user prompt or AI response)
        role: Either "user" for prompts or "assistant" for AI responses

    Returns:
        AIDefenseResult with inspection results
    """
    if not AI_DEFENSE_ENABLED or not AI_DEFENSE_API_KEY:
        return AIDefenseResult(is_safe=True, action="allow")

    try:
        url = f"{AI_DEFENSE_ENDPOINT}/api/v1/inspect/chat"
        headers = {
            "X-Cisco-AI-Defense-API-Key": AI_DEFENSE_API_KEY,
            "Content-Type": "application/json",
            "accept": "application/json"
        }

        payload = {
            "messages": [
                {
                    "role": role,
                    "content": content
                }
            ],
            "metadata": {
                "src_app": "gbaia-backend",
                "user_agent": "GBAIA/1.0"
            },
            "config": {}
        }

        # Use dashboard policy when AI_DEFENSE_USE_POLICY is true, otherwise use inline rules
        if not AI_DEFENSE_USE_POLICY:
            payload["config"]["enabled_rules"] = AI_DEFENSE_RULES

        # Use httpx for consistency with the rest of the codebase
        with httpx.Client(timeout=10.0) as client:
            response = client.post(url, headers=headers, json=payload)

            if response.status_code == 401:
                print("⚠️ AI Defense API authentication failed - check API key")
                return AIDefenseResult(is_safe=True, action="allow")

            response.raise_for_status()
            data = response.json()

        # Parse the response
        is_safe = data.get("is_safe", True)
        severity = data.get("severity", "NONE_SEVERITY")
        classifications = data.get("classifications", [])
        violated_rules = [r.get("rule_name", "") for r in data.get("rules", [])]
        explanation = data.get("explanation")
        attack_technique = data.get("attack_technique")

        # Use action directly from API response (Block/Allow)
        # API returns capitalized values: "Block", "Allow"
        api_action = data.get("action", "Allow")
        if api_action == "Block":
            action = "block"
        elif not is_safe:
            action = "warn"  # Not blocked but flagged as unsafe
        else:
            action = "allow"

        result = AIDefenseResult(
            is_safe=is_safe,
            severity=severity,
            classifications=classifications,
            violated_rules=violated_rules,
            explanation=explanation,
            attack_technique=attack_technique,
            action=action
        )

        if not is_safe:
            print(f"🛡️ AI Defense detected issue: {classifications} - Severity: {severity} - Action: {action}")

        return result

    except httpx.TimeoutException:
        print("⚠️ AI Defense API timeout - allowing request")
        return AIDefenseResult(is_safe=True, action="allow")
    except Exception as e:
        print(f"⚠️ AI Defense API error: {e} - allowing request")
        return AIDefenseResult(is_safe=True, action="allow")

if AI_DEFENSE_ENABLED:
    if AI_DEFENSE_API_KEY:
        print(f"🛡️ Cisco AI Defense enabled - endpoint: {AI_DEFENSE_ENDPOINT}")
        print(f"   Inspect prompts: {AI_DEFENSE_INSPECT_PROMPTS}, Inspect responses: {AI_DEFENSE_INSPECT_RESPONSES}")
        if AI_DEFENSE_USE_POLICY:
            print("   Mode: Dashboard policy (rules configured in AI Defense console)")
        else:
            print("   Mode: Inline rules (rules defined in code)")
    else:
        print("⚠️ AI Defense enabled but AI_DEFENSE_API_KEY not set - feature disabled")
        AI_DEFENSE_ENABLED = False
else:
    print("🛡️ Cisco AI Defense disabled (set AI_DEFENSE_ENABLED=true to enable)")


def traced_neo4j_query(graph_instance, cypher_query: str, operation_name: str = "cypher.query"):
    """Execute a Neo4j query with OpenTelemetry tracing."""
    if neo4j_tracer:
        from opentelemetry.trace import SpanKind, Status, StatusCode

        with neo4j_tracer.start_as_current_span(
            operation_name,
            kind=SpanKind.CLIENT,
            attributes={
                "db.system": "neo4j",
                "db.name": "neo4j",
                "db.operation": "query",
                "db.statement": cypher_query[:1000],  # Truncate long queries
                "db.neo4j.query_type": "read",
            }
        ) as span:
            try:
                result = graph_instance.query(cypher_query)
                span.set_attribute("db.neo4j.result_count", len(result))
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.set_attribute("db.neo4j.error", str(e))
                raise
    else:
        # No tracing, execute directly
        return graph_instance.query(cypher_query)


# --- Data Models ---
class Query(BaseModel):
    question: str
    chat_history: List[List[str]] = Field(default_factory=list)
    source: str = Field(default="user")  # "user", "suggestion", or "graph_click"

class SecurityInfo(BaseModel):
    """Security inspection results from Cisco AI Defense"""
    prompt_safe: bool = True
    response_safe: bool = True
    prompt_severity: str = "NONE_SEVERITY"
    response_severity: str = "NONE_SEVERITY"
    prompt_violations: List[str] = Field(default_factory=list)
    response_violations: List[str] = Field(default_factory=list)
    blocked: bool = False
    warning: Optional[str] = None

class DataSource(BaseModel):
    """Tracks the source of data in the response"""
    type: str  # "neo4j" or "mcp"
    description: str  # Human-readable description of what was queried
    details: Optional[Dict[str, Any]] = None  # Additional metadata (query, tool name, etc.)

class Response(BaseModel):
    answer: str
    suggestions: List[str] = Field(default_factory=list)
    security: Optional[SecurityInfo] = None
    sources: List[DataSource] = Field(default_factory=list)  # Track data sources

class GraphNode(BaseModel):
    id: str
    label: str
    type: str
    properties: dict = Field(default_factory=dict)

class GraphEdge(BaseModel):
    source: str
    target: str
    type: str

class GraphResponse(BaseModel):
    nodes: List[GraphNode] = Field(default_factory=list)
    edges: List[GraphEdge] = Field(default_factory=list)

# --- Configuration ---
NEO4J_URI = os.getenv("NEO4J_URI")
NEO4J_USER = os.getenv("NEO4J_USER")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")
LOCAL_LLM_URL = os.getenv("LOCAL_LLM_URL")
LOCAL_LLM_TOKEN = os.getenv("LOCAL_LLM_TOKEN")  # Bearer token for authenticated LLM endpoints

# --- Model Proxy Configuration (for AI Defense Validation) ---
MODEL_PROXY_ENABLED = os.getenv("MODEL_PROXY_ENABLED", "false").lower() == "true"
MODEL_PROXY_API_KEY = os.getenv("MODEL_PROXY_API_KEY", "")  # API key for proxy authentication

# --- MCP (Model Context Protocol) Configuration ---
MCP_ENABLED = os.getenv("MCP_ENABLED", "false").lower() == "true"
MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "")
MCP_TOKEN = os.getenv("MCP_TOKEN", "")

# --- Intersight MCP Configuration ---
INTERSIGHT_MCP_ENABLED = os.getenv("INTERSIGHT_MCP_ENABLED", "true").lower() == "true"
INTERSIGHT_MCP_URL = os.getenv("INTERSIGHT_MCP_URL", "http://intersight-mcp:3000")

# --- Runtime Configuration ---
from runtime_config import init_runtime_config, get_runtime_config

# Initialize runtime config with PVC mount point or fallback to local
CONFIG_PATH = os.getenv("CONFIG_PATH", "/data/config.json")
init_runtime_config(CONFIG_PATH)
print(f"✅ Runtime configuration initialized at {CONFIG_PATH}")

# --- App Initialization ---
app = FastAPI(
    title="Network AI Agent API",
    description="API to interact with the network knowledge graph."
)

# Instrument FastAPI if Splunk Observability is enabled
if SPLUNK_ENABLED:
    FastAPIInstrumentor.instrument_app(app)
    print("✅ FastAPI instrumented for tracing")

print("▶️  Connecting to Neo4j knowledge graph...")
graph = Neo4jGraph(url=NEO4J_URI, username=NEO4J_USER, password=NEO4J_PASSWORD)
graph.refresh_schema()
print("✅ Connection with Neo4j established.")

# --- Conditional LLM Initialization ---
if LOCAL_LLM_URL:
    print(f"✅ Using local vLLM-compatible model at: {LOCAL_LLM_URL}")

    # Configure HTTP client with SSL verification disabled for internal endpoints
    # If token is provided, it will be used via api_key parameter
    custom_client = httpx.Client(verify=False)

    # The model name MUST match the deployed model on the vLLM server
    # Using Cisco-approved GREEN model: Mistral Nemo 12B (Apache 2.0 license)
    local_model_name = "mistral-nemo-12b"

    # Use token if provided, otherwise use placeholder
    llm_api_key = LOCAL_LLM_TOKEN if LOCAL_LLM_TOKEN else "EMPTY"

    cypher_llm = ChatOpenAI(
        http_client=custom_client,
        base_url=LOCAL_LLM_URL,
        api_key=llm_api_key,
        model_name=local_model_name,
        temperature=0
    )
    qa_llm = ChatOpenAI(
        http_client=custom_client,
        base_url=LOCAL_LLM_URL,
        api_key=llm_api_key,
        model_name=local_model_name,
        temperature=0
    )
    suggestion_llm = ChatOpenAI(
        http_client=custom_client,
        base_url=LOCAL_LLM_URL,
        api_key=llm_api_key,
        model_name=local_model_name,
        temperature=0.3
    )
else:
    print("✅ Using OpenAI models (LOCAL_LLM_URL not set).")
    openai_client = httpx.Client(verify=False)
    openai_async_client = httpx.AsyncClient(verify=False)
    cypher_llm = ChatOpenAI(model="gpt-4o-mini", temperature=0, http_client=openai_client, http_async_client=openai_async_client)
    qa_llm = ChatOpenAI(model="gpt-4o-mini", temperature=0, http_client=openai_client, http_async_client=openai_async_client)
    suggestion_llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.3, http_client=openai_client, http_async_client=openai_async_client)


# --- MCP Client Initialization ---
@app.on_event("startup")
async def startup_event():
    """Initialize MCP client on application startup"""
    if MCP_ENABLED:
        if not MCP_SERVER_URL or not MCP_TOKEN:
            print("⚠️ MCP enabled but MCP_SERVER_URL or MCP_TOKEN not set - MCP integration disabled")
            return

        try:
            print(f"▶️  Connecting to MCP server at {MCP_SERVER_URL}...")
            await init_mcp_client(url=MCP_SERVER_URL, token=MCP_TOKEN)

            mcp = get_mcp_client()
            if mcp and mcp.connected:
                tool_count = len(mcp.tools)
                read_only_count = len(mcp.get_read_only_tools())
                categories = mcp.get_tool_categories()

                print(f"✅ MCP client connected - {tool_count} tools discovered ({read_only_count} read-only)")
                print(f"   Categories: Insights={len(categories['insights'])}, "
                      f"Manage={len(categories['manage'])}, "
                      f"Infrastructure={len(categories['infrastructure'])}, "
                      f"OneManage={len(categories['onemanage'])}")
        except Exception as e:
            print(f"⚠️ Failed to initialize MCP client: {e}")
            print("   Backend will continue without MCP integration")
    else:
        print("📦 MCP integration disabled (set MCP_ENABLED=true to enable)")


@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup MCP client on application shutdown"""
    if MCP_ENABLED:
        try:
            await shutdown_mcp_client()
            print("✅ MCP client disconnected")
        except Exception as e:
            print(f"⚠️ Error during MCP shutdown: {e}")


# --- Define the Prompt Templates ---
CYPHER_GENERATION_TEMPLATE = """Generate a Cypher query. Output ONLY the query, no markdown, no explanations.

=== CRITICAL SYNTAX RULES ===
1. ALWAYS start with MATCH or OPTIONAL MATCH (never start with RETURN)
2. Use OPTIONAL MATCH for relationships that might not exist
3. Define ALL variables in MATCH/OPTIONAL MATCH before using them in RETURN
4. Use only ONE RETURN statement at the end of the query
5. NEVER use relationship patterns like (n)-[:REL]->(m) inside RETURN clause
6. Property filters go in WHERE clause, not inline in MATCH (except for simple node property matches)

=== COMMON ERRORS TO AVOID ===
❌ WRONG: RETURN n.name, (n)-[:HAS_FAULT]->(f)
✅ RIGHT: MATCH (n)-[:HAS_FAULT]->(f) ... RETURN n.name, f.code

❌ WRONG: MATCH (n) RETURN n.name RETURN n.role
✅ RIGHT: MATCH (n) RETURN n.name, n.role

❌ WRONG: RETURN n.name WHERE n.role = 'leaf'
✅ RIGHT: MATCH (n) WHERE n.role = 'leaf' RETURN n.name

=== KEY RELATIONSHIPS ===
- Node -[:BELONGS_TO]-> Fabric (nodes belong to fabrics)
- Node -[:HAS_FAULT]-> Fault (nodes have faults with severity: warning, minor, major, critical)
- Fabric -[:HAS_ANOMALY]-> Anomaly (fabrics have anomalies with severity: warning, major, critical)
- Anomaly -[:AFFECTS]-> Tenant (anomalies affect specific tenants)
- Fabric -[:HAS_ADVISORY]-> Advisory (fabrics have advisories)
- Tenant -[:BELONGS_TO]-> Fabric (tenants belong to fabrics)
- Tenant -[:HAS_AP]-> AppProfile (tenants have application profiles)
- AppProfile -[:HAS_EPG]-> EPG (application profiles have EPGs)
- Tenant -[:HAS_VRF]-> VRF (tenants have VRFs)
- Tenant -[:HAS_BD]-> BridgeDomain (tenants have bridge domains)
- BridgeDomain -[:HAS_SUBNET]-> Subnet (bridge domains have subnets)
- IntersightServer -[:CONNECTED_TO]-> Endpoint (servers connect to network endpoints via MAC correlation)
- Endpoint -[:MEMBER_OF]-> EPG (endpoints are members of EPGs)

IMPORTANT: Severity values are ALWAYS lowercase: 'critical', 'major', 'warning', 'minor'

=== NODE PROPERTIES ===
Common Node properties: name, role (spine/leaf), model, address, serial, fabricSt (status)
Common Fabric properties: name, health, status
Common Fault properties: code, severity, descr, cause, created
Common Anomaly properties: name (e.g. VPC_DOWN), severity, category, details, fabric, lastSeen
  IMPORTANT: When querying an Anomaly, you MUST always return BOTH `a.details` and `a.fabric`
  alongside name/severity. The `details` field contains the human-readable root cause
  (which leaf/spine, which interface, which vPC name) and the `fabric` field names the
  ACI fabric it came from. Without these, the answer cannot say WHERE the issue is.
  An Anomaly has NO `affects` or `fix` property - do not invent them. There is no
  `Resource` label in the graph.
Common Subnet properties: ip, scope, bd, tenant

=== EXAMPLES ===

EXAMPLE 1 - Simple node property query:
Question: "What is the model of node 'leaf-101'?" or "Show me node 'leaf-101'"
MATCH (n:Node {{name: 'leaf-101'}})
RETURN n.name, n.model, n.role, n.address

EXAMPLE 2 - Node details with fabric and faults:
Question: "Show me details about node 'leaf-102' including its role, model, and any faults"
MATCH (n:Node {{name: 'leaf-102'}})-[:BELONGS_TO]->(f:Fabric)
OPTIONAL MATCH (n)-[:HAS_FAULT]->(fault:Fault)
RETURN n.name, n.role, n.model, n.address, n.serial, f.name AS fabric_name,
       fault.code AS fault_code, fault.severity AS fault_severity, fault.descr AS fault_description

EXAMPLE 3 - Nodes with warning faults in a fabric:
Question: "List nodes in fabric 'ams-aci' with warning faults"
MATCH (n:Node)-[:BELONGS_TO]->(f:Fabric)
WHERE f.name = 'ams-aci'
MATCH (n)-[:HAS_FAULT]->(fault:Fault)
WHERE fault.severity = 'warning'
RETURN n.name AS node_name, fault.code AS fault_code, fault.descr AS description

EXAMPLE 4 - List ALL anomalies in a fabric with full details:
Question: "List all anomalies in fabric 'ams-aci'"
MATCH (f:Fabric)-[:HAS_ANOMALY]->(a:Anomaly)
WHERE f.name = 'ams-aci'
RETURN a.name AS anomaly_name, a.severity, a.lastSeen
ORDER BY a.severity

EXAMPLE 5 - All faults for a specific node:
Question: "Show faults for node 'leaf-104'"
MATCH (n:Node)-[:HAS_FAULT]->(f:Fault)
WHERE n.name = 'leaf-104'
RETURN n.name AS node, f.code, f.severity, f.descr AS description, f.cause

EXAMPLE 6 - Anomalies with specific severity:
Question: "List critical anomalies in fabric 'ams-aci'"
MATCH (f:Fabric)-[:HAS_ANOMALY]->(a:Anomaly)
WHERE f.name = 'ams-aci' AND a.severity = 'critical'
RETURN a.name AS anomaly_name, a.severity, a.lastSeen

EXAMPLE 7 - Fabric status overview with counts:
Question: "What is the status of fabric 'ams-aci'?"
MATCH (f:Fabric)
WHERE f.name = 'ams-aci'
OPTIONAL MATCH (f)-[:HAS_ANOMALY]->(a:Anomaly)
RETURN f.name AS fabric_name, count(a) AS anomaly_count,
       collect(a.name) AS anomaly_names, collect(a.severity) AS severities

EXAMPLE 8 - Tenant with all related entities:
Question: "Tell me about tenant 'example-tenant'"
MATCH (t:Tenant)
WHERE t.name = 'example-tenant'
OPTIONAL MATCH (t)-[:HAS_AP]->(ap:AppProfile)
OPTIONAL MATCH (ap)-[:HAS_EPG]->(epg:EPG)
OPTIONAL MATCH (t)-[:HAS_VRF]->(vrf:VRF)
OPTIONAL MATCH (t)-[:HAS_BD]->(bd:BridgeDomain)
RETURN t.name AS tenant,
       collect(DISTINCT ap.name) AS app_profiles,
       collect(DISTINCT epg.name) AS epgs,
       collect(DISTINCT vrf.name) AS vrfs,
       collect(DISTINCT bd.name) AS bridge_domains

EXAMPLE 9 - Tenants in a fabric:
Question: "What tenants exist in fabric 'ams-aci'?"
MATCH (t:Tenant)-[:BELONGS_TO]->(f:Fabric)
WHERE f.name = 'ams-aci'
RETURN t.name AS tenant_name

EXAMPLE 10 - Tenants affected by anomalies:
Question: "Show me tenants with anomalies"
MATCH (a:Anomaly)-[:AFFECTS]->(t:Tenant)
RETURN t.name AS tenant_name,
       collect(a.name) AS anomalies,
       collect(a.severity) AS severities

EXAMPLE 11 - Details about a specific anomaly (USE THIS PATTERN for ANY "tell me about
this anomaly" / "what is anomaly X" / "details about anomaly X" question - all of these
need `a.details` and `a.fabric` or the answer cannot describe what actually happened):
Question: "Tell me about the VPC_DOWN anomaly"
MATCH (a:Anomaly {{name: 'VPC_DOWN'}})
OPTIONAL MATCH (a)-[:AFFECTS]->(t:Tenant)
OPTIONAL MATCH (f:Fabric)-[:HAS_ANOMALY]->(a)
RETURN a.name, a.severity, a.category, a.details, a.fabric, a.lastSeen,
       collect(DISTINCT t.name) AS affected_tenants,
       collect(DISTINCT f.name) AS fabrics

EXAMPLE 11b - Details about ONE SPECIFIC anomaly instance (graph-click case where the
user gives a uuid). Multiple anomalies often share the same name (e.g. VPC_DOWN exists
once per affected vPC), so filtering by uuid returns ONLY that one row - the right
behaviour when the user clicked one specific node on the topology map:
Question: "Tell me about the specific anomaly with uuid 'VPC394994828872'"
MATCH (a:Anomaly {{uuid: 'VPC394994828872'}})
OPTIONAL MATCH (a)-[:AFFECTS]->(t:Tenant)
OPTIONAL MATCH (f:Fabric)-[:HAS_ANOMALY]->(a)
RETURN a.name, a.severity, a.category, a.details, a.fabric, a.lastSeen,
       collect(DISTINCT t.name) AS affected_tenants,
       collect(DISTINCT f.name) AS fabrics

EXAMPLE 11c - Details about ONE SPECIFIC fault instance by uuid (graph-click case for
Fault nodes - same rationale as 11b, faults can share codes across many objects):
Question: "Tell me about the specific fault with uuid 'F123456'"
MATCH (fault:Fault {{uuid: 'F123456'}})
OPTIONAL MATCH (fault)<-[:HAS_FAULT]-(n)
RETURN fault.code, fault.severity, fault.descr, fault.cause, fault.created,
       labels(n)[0] AS affected_type, n.name AS affected_object

EXAMPLE 12 - Anomalies affecting a specific tenant:
Question: "What anomalies affect the flexpod tenant?"
MATCH (a:Anomaly)-[:AFFECTS]->(t:Tenant)
WHERE t.name = 'flexpod'
RETURN t.name AS tenant_name, a.name AS anomaly_name, a.severity, a.details

EXAMPLE 13 - Nodes with multiple properties and counts:
Question: "Show all leaf nodes in fabric 'ams-aci' with their fault counts"
MATCH (n:Node)-[:BELONGS_TO]->(f:Fabric)
WHERE f.name = 'ams-aci' AND n.role = 'leaf'
OPTIONAL MATCH (n)-[:HAS_FAULT]->(fault:Fault)
RETURN n.name, n.model, n.address, count(fault) AS fault_count
ORDER BY fault_count DESC

EXAMPLE 14 - Subnets under a BridgeDomain:
Question: "Show all subnets under BridgeDomain 'inb'"
MATCH (bd:BridgeDomain {{name: 'inb'}})-[:HAS_SUBNET]->(s:Subnet)
RETURN s.ip AS subnet_ip, s.scope AS scope

EXAMPLE 15 - BridgeDomain with all details including subnets:
Question: "Describe BridgeDomain 'inb' with all its subnets"
MATCH (bd:BridgeDomain {{name: 'inb'}})
OPTIONAL MATCH (bd)-[:HAS_SUBNET]->(s:Subnet)
OPTIONAL MATCH (t:Tenant)-[:HAS_BD]->(bd)
RETURN bd.name AS bridge_domain, t.name AS tenant,
       collect(s.ip) AS subnet_ips, collect(s.scope) AS subnet_scopes

EXAMPLE 16 - Query an entity when type is unknown:
Question: "Tell me about 'TS-FI-1-8'"
MATCH (n {{name: 'TS-FI-1-8'}})
OPTIONAL MATCH (n)-[r]->(m)
RETURN labels(n) AS entity_type, properties(n) AS entity_properties,
       collect({{rel_type: type(r), target_label: labels(m)[0], target_name: m.name}}) AS relationships

EXAMPLE 17 - IntersightServer with connected endpoints:
Question: "Show me server 'TS-FI-1-4' and its network connections"
MATCH (s:IntersightServer {{name: 'TS-FI-1-4'}})
OPTIONAL MATCH (s)-[:CONNECTED_TO]->(e:Endpoint)
OPTIONAL MATCH (e)-[:MEMBER_OF]->(epg:EPG)
RETURN s.name AS server, s.model AS model, s.serial AS serial,
       collect(DISTINCT e.mac) AS endpoint_macs,
       collect(DISTINCT e.ip) AS endpoint_ips,
       collect(DISTINCT epg.name) AS connected_epgs

EXAMPLE 18 - All IntersightServers with correlation status:
Question: "Show all servers and their network correlation status"
MATCH (s:IntersightServer)
OPTIONAL MATCH (s)-[:CONNECTED_TO]->(e:Endpoint)
RETURN s.name AS server, s.model AS model,
       count(e) AS connected_endpoints,
       collect(e.mac) AS endpoint_macs
ORDER BY connected_endpoints DESC

EXAMPLE 19 - Endpoint with EPG membership:
Question: "Tell me about endpoint with MAC '00:25:B5:56:65:46'"
MATCH (e:Endpoint {{mac: '00:25:B5:56:65:46'}})
OPTIONAL MATCH (e)-[:MEMBER_OF]->(epg:EPG)
OPTIONAL MATCH (s:IntersightServer)-[:CONNECTED_TO]->(e)
OPTIONAL MATCH (epg)<-[:HAS_EPG]-(ap:AppProfile)<-[:HAS_AP]-(t:Tenant)
RETURN e.mac AS mac, e.ip AS ip, e.name AS endpoint_name,
       epg.name AS epg, ap.name AS app_profile, t.name AS tenant,
       s.name AS connected_server

EXAMPLE 20 - Endpoints in a specific EPG:
Question: "Show all endpoints in EPG 'nodes'"
MATCH (e:Endpoint)-[:MEMBER_OF]->(epg:EPG {{name: 'nodes'}})
OPTIONAL MATCH (s:IntersightServer)-[:CONNECTED_TO]->(e)
RETURN e.mac AS mac, e.ip AS ip, s.name AS server
ORDER BY e.ip

Schema:
{schema}

Chat History (use this to understand follow-up questions):
{chat_history}

Current Question: {question}

If the question refers to something from the chat history (like "the tenant mentioned above" or "that application profile"), extract the specific name from the history and use it in your query.

THINK: What entities and relationships do I need? Write MATCH/OPTIONAL MATCH first, then RETURN.

Cypher:"""

CYPHER_PROMPT = PromptTemplate.from_template(CYPHER_GENERATION_TEMPLATE)

# Concise template for direct user queries and suggestions - focus on actual data
QA_TEMPLATE_CONCISE = """You are an expert Cisco ACI network operations assistant. Answer based on the query results provided.

DOMAIN CONTEXT - ALWAYS REMEMBER:
- This is Cisco ACI / Nexus Dashboard / Intersight, NOT AWS, Azure, or GCP.
- "VPC" = Cisco Virtual Port Channel (pair of leaves presenting one logical link),
  NEVER Virtual Private Cloud.
- "Tenant" = an ACI tenant (policy/forwarding domain), NEVER a cloud-provider tenant.
- "Fabric" = a Cisco ACI fabric (e.g. ams-aci), NEVER a cloud fabric.
- Remediation must reference APIC / NX-OS / Nexus Dashboard, not cloud consoles.

CRITICAL ACCURACY RULES:
1. Base ALL facts, names, counts, and severities ONLY on the Query Results below
2. If the results show an empty list [] or null/None, explicitly state "none found" or "0" - NEVER invent items
3. Count items exactly as they appear - if you see ["item1"], that's exactly 1 item
4. You MAY provide brief helpful context for items that DO exist in the results
5. If `details` and `fabric` fields are present on an anomaly, USE them in the answer
   - they describe exactly which leaf/vPC/interface is affected and in which fabric.

FORMATTING STYLE:
- Use natural English without excessive quotes
- DO NOT wrap your entire answer in quotes
- For lists: 2 application profiles found: access and ave-ctrl (no quotes around names)
- For single items: Found tenant baelen (no quotes)
- Only use quotes when referring to user's input: No results for 'invalid-name'

FORBIDDEN:
- Do NOT invent names, counts, or details not present in the results
- Do NOT fabricate issues or items when results show empty []
- Do NOT make up example data or placeholders
- Do NOT mix quote styles like "found: 'item1', 'item2'"

EXAMPLES OF CORRECT RESPONSES:
- Results show app_profiles: ["talos"], epgs: [] → "1 application profile: talos. No EPGs configured"
- Results show 2 items → "2 application profiles found: access and ave-ctrl"
- Results show anomalies with names/severities → List the actual anomalies found
- Results show empty [] for something asked about → "No items found for this entity"

Query Results:
{context}

Question:
{question}

Answer:
"""

# Detailed template for graph node clicks - full report with context
QA_TEMPLATE_DETAILED = """You are an expert Cisco ACI network operations assistant. Provide a comprehensive report.

DOMAIN CONTEXT - ALWAYS REMEMBER:
- This system is about Cisco ACI / Nexus Dashboard / Intersight, NOT AWS, Azure, or GCP.
- "VPC" here is Cisco Virtual Port Channel (a pair of leaf switches presenting one logical
  link to a downstream device), NEVER Virtual Private Cloud.
- "Tenant" is an ACI tenant (an isolated policy and forwarding domain in the fabric),
  NEVER a cloud-provider tenant.
- "Fabric" is a Cisco ACI fabric named like 'ams-aci' / 'fp-fabric', NEVER a generic
  cloud fabric.
- "EPG" = Endpoint Group, "BD" = Bridge Domain, "VRF" = Virtual Routing & Forwarding,
  "AVE" = Application Virtual Edge - all Cisco ACI constructs.
- Remediation advice must reference ACI/NX-OS commands and APIC/Nexus Dashboard
  consoles, not AWS/cloud consoles.

CRITICAL ACCURACY RULES:
1. Base ALL facts, names, counts, and severities ONLY on the Query Results below
2. If the results show an empty list [] or null/None for something, explicitly state "none" or "0" - NEVER invent items
3. Count items exactly as they appear in arrays - if you see ["item1", "item2"], that's exactly 2 items
4. ONLY report anomaly/fault severities if they are EXPLICITLY shown in the results (e.g., "severity": "critical")
5. You MAY provide helpful explanations for items that DO exist in the results
6. You MAY suggest remediation steps for actual issues found in the results
7. If `details` and/or `fabric` fields are present on an anomaly row, USE them in the
   answer - they describe exactly which leaf/vPC/interface is affected and in which
   fabric. Do NOT say "affected resources: none" when these fields contain that info.

FORMATTING STYLE:
- Use natural English without excessive quotes
- DO NOT wrap your entire answer in quotes
- For lists: 2 application profiles: access and ave-ctrl (no quotes around names)
- For single items: Tenant: baelen (no quotes)
- Only use quotes when referring to user's input or when quoting error messages

FORBIDDEN - NEVER DO THESE:
- Do NOT invent entity names, counts, or severities not explicitly in the results
- Do NOT guess or infer severity from anomaly names - only use severity if it's in the data
- Do NOT fabricate issues or anomalies that aren't in the results
- Do NOT add anomalies that are not listed in the Query Results
- Do NOT skip or omit anomalies that ARE in the Query Results - list ALL of them
- Do NOT mix quote styles like "found: 'item1', 'item2'"

Format your response:

## Entity Summary
Describe the queried entity based on the Query Results:
- List all properties shown (name, status, type, etc.)
- State exact counts from the data (count the rows/items in the results)
- Explain the entity's role in the network architecture
- If a field shows [] or null, state "0" or "none" for that item

## Issues Detected
IMPORTANT: You MUST list EVERY anomaly/fault from the Query Results. Count the rows in the results and ensure your list has the same count. Do not summarize or skip any.

SPECIAL CASE - If the queried entity IS an Anomaly or Fault (the 'name' field contains an anomaly name like 'ENDPOINT_TRAFFIC_SCORE_UNHEALTHY'):
- The entity itself is the issue - describe it with its severity from the results
- List any affected resources (tenants, nodes, fabrics) from the results

For EACH row in the Query Results that contains an anomaly or fault:
- **[Severity] NAME** - Brief explanation of what this issue means and its impact
- If severity is not shown, write **[Unknown] NAME**

Verify: If Query Results show 10 anomaly rows, you must list all 10. If results show 7, list all 7.

If NO anomalies/faults appear AND the entity is NOT itself an anomaly/fault, state: "No issues found in the database for this entity"

## Recommended Actions
For issues ACTUALLY FOUND in the results above, provide:

**Critical (fix immediately):**
- Specific troubleshooting steps for the critical issues listed

**Major (fix soon):**
- Remediation guidance for major issues listed

**Warning (monitor/review):**
- What to investigate for warning issues listed

If no issues were found in the sections above, simply write "No actions required" and skip the severity categories.

Query Results:
{context}

Question:
{question}

Answer:
"""

# Default template (used for backwards compatibility)
QA_TEMPLATE = QA_TEMPLATE_CONCISE

QA_PROMPT = PromptTemplate.from_template(QA_TEMPLATE)

def clean_cypher_output(cypher_text: str) -> str:
    """Remove explanatory text, markdown formatting, and fix common syntax errors that LLMs sometimes add."""
    if not cypher_text:
        return cypher_text

    text = cypher_text.strip()

    # Remove markdown code blocks (```cypher, ```sql, ``` etc.)
    import re
    # Match code blocks with optional language specifier
    code_block_pattern = r'```(?:cypher|sql|neo4j)?\s*\n?(.*?)\n?```'
    match = re.search(code_block_pattern, text, re.DOTALL | re.IGNORECASE)
    if match:
        text = match.group(1).strip()

    lines = text.split('\n')
    clean_lines = []

    # Common patterns that indicate explanatory text (not Cypher)
    explanation_starters = [
        'this query', 'this will', 'the query', 'the above', 'note:', 'explanation:',
        'here is', 'here\'s', 'i have', 'i\'ve', 'this returns', 'this retrieves',
        'this cypher', 'the result', 'this should', 'this assumes'
    ]

    for line in lines:
        line_lower = line.strip().lower()
        # Skip empty lines between query and explanation
        if not line.strip():
            # If we already have query content, empty line might signal end
            if clean_lines and clean_lines[-1].strip():
                # Check if next non-empty line looks like explanation
                continue
            continue

        # Check if this line starts explanatory text
        is_explanation = any(line_lower.startswith(starter) for starter in explanation_starters)
        if is_explanation:
            break  # Stop collecting lines once we hit explanation

        clean_lines.append(line)

    result = '\n'.join(clean_lines).strip()

    # Fix common LLM syntax errors in Cypher
    # Fix duplicate closing braces/parentheses like }}) or }}} or )))
    result = re.sub(r'\}{2,}', '}', result)  # }} or }}} -> }
    result = re.sub(r'\){2,}', ')', result)  # )) or ))) -> )
    result = re.sub(r'\]{2,}', ']', result)  # ]] or ]]] -> ]

    # Fix }}) pattern (common LLM mistake)
    result = re.sub(r'\}\)', '})', result)  # Already correct, but ensure no doubles
    result = re.sub(r'\}\}\)', '})', result)  # }}) -> })
    result = re.sub(r'\}\)\)', '})', result)  # })) -> })

    # Balance brackets - count and fix mismatched pairs
    def balance_brackets(s, open_char, close_char):
        open_count = s.count(open_char)
        close_count = s.count(close_char)
        if close_count > open_count:
            # Remove excess closing brackets from the end
            excess = close_count - open_count
            for _ in range(excess):
                last_idx = s.rfind(close_char)
                if last_idx != -1:
                    s = s[:last_idx] + s[last_idx+1:]
        return s

    result = balance_brackets(result, '(', ')')
    result = balance_brackets(result, '{', '}')
    result = balance_brackets(result, '[', ']')

    # Fix multiple RETURN statements (common LLM error)
    # Keep only the first RETURN statement
    return_count = len(re.findall(r'\bRETURN\b', result, re.IGNORECASE))
    if return_count > 1:
        print(f"⚠️ Found {return_count} RETURN statements, keeping only the first")
        # Find position of second RETURN and truncate
        parts = re.split(r'\bRETURN\b', result, maxsplit=2, flags=re.IGNORECASE)
        if len(parts) >= 2:
            # Reconstruct with only first RETURN
            result = parts[0] + 'RETURN' + parts[1]

    return result


def validate_cypher_query(cypher: str) -> tuple[bool, str]:
    """
    Validate a Cypher query for common issues.
    Returns (is_valid, error_message)
    """
    import re

    if not cypher or not cypher.strip():
        return False, "Empty query"

    cypher_upper = cypher.upper()

    # Check if starts with valid keyword
    valid_starts = ('MATCH', 'OPTIONAL', 'WITH', 'CALL', 'RETURN', 'UNWIND', 'CREATE', 'MERGE')
    if not any(cypher_upper.strip().startswith(kw) for kw in valid_starts):
        return False, "Query must start with MATCH, OPTIONAL MATCH, WITH, CALL, RETURN, or similar keyword"

    # Check for multiple RETURN statements (shouldn't happen after cleaning)
    return_count = len(re.findall(r'\bRETURN\b', cypher_upper))
    if return_count > 1:
        return False, f"Query contains {return_count} RETURN statements (should have only one)"

    # Check for pattern expressions in RETURN clause (Neo4j doesn't allow this)
    # Pattern like: RETURN ... (n)-[:REL]->(m) ...
    return_match = re.search(r'RETURN\s+(.+)', cypher, re.IGNORECASE | re.DOTALL)
    if return_match:
        return_clause = return_match.group(1)
        # Check for relationship patterns in RETURN
        if re.search(r'\([^)]*\)-\[', return_clause):
            return False, "Cannot use relationship patterns in RETURN clause (use OPTIONAL MATCH instead)"

    # Basic bracket balance check
    for open_char, close_char in [('(', ')'), ('{', '}'), ('[', ']')]:
        if cypher.count(open_char) != cypher.count(close_char):
            return False, f"Unbalanced {open_char}{close_char} brackets"

    return True, ""

# --- Create the GraphCypherQAChain ---
graph_qa_chain = GraphCypherQAChain.from_llm(
    cypher_llm=cypher_llm,
    qa_llm=qa_llm,
    graph=graph,
    verbose=True,
    cypher_prompt=CYPHER_PROMPT,
    qa_prompt=QA_PROMPT,
    validate_cypher=False,  # Disable built-in validation, we'll clean first
    return_intermediate_steps=True,
    allow_dangerous_requests=True
)
print("✅ GraphCypherQAChain initialized.")

# --- Helper function to detect anomaly-related queries ---
def is_anomaly_query(question: str) -> bool:
    """Check if the question is about anomalies, faults, or issues."""
    question_lower = question.lower()
    anomaly_keywords = [
        'anomaly', 'anomalies',
        'fault', 'faults',
        'issue', 'issues',
        'problem', 'problems',
        'error', 'errors',
        'critical', 'major', 'warning', 'minor',
        'down', 'unhealthy', 'violation'
    ]
    return any(keyword in question_lower for keyword in anomaly_keywords)

# --- Prompt for generating suggestions ---
suggestion_prompt = PromptTemplate.from_template(
    """You are a network operations assistant. Based on the conversation, generate three direct follow-up queries that a user might want to ask next.

CRITICAL: PRESERVE CONTEXT! If the question mentions a specific tenant, fabric, node, or other entity, INCLUDE that context in your suggestions.

CRITICAL: RESPECT ENTITY TYPES! Do NOT confuse different entity types:
- Tenants (e.g., 'edge', 'vxlan_stretch', 'infra') → belong to Tenants and contain AppProfiles, EPGs, VRFs, BridgeDomains
- Fabrics (e.g., 'ams-aci', 'fabric1') → contain Nodes (spine/leaf switches)
- Nodes (e.g., 'leaf-101', 'spine-201') → are part of Fabrics, not Tenants
- AppProfiles, EPGs, VRFs, BridgeDomains → belong to Tenants, not Fabrics

Entity type validation:
1. If answer mentions "tenant 'X'" or "Tenant: X", ONLY use X in tenant-related suggestions (AppProfiles, EPGs, VRFs, BridgeDomains)
2. If answer mentions "fabric 'Y'", ONLY use Y in fabric-related suggestions (nodes, topology)
3. NEVER suggest "nodes in tenant X" or "EPGs in fabric Y" - these are invalid combinations

IMPORTANT: Write each suggestion as a direct command or question that will be sent to the AI, NOT as a question asking the user what they want.

Good examples (notice correct entity usage):
- Question about tenant 'edge' → "Show EPGs in AppProfile 'network-segments' under tenant 'edge'"
- Question about fabric 'ams-aci' → "Show all nodes in fabric 'ams-aci'"
- Question about node 'leaf-102' → "What faults affect node 'leaf-102'?"
- Answer shows tenant 'vxlan_stretch' → "Show AppProfiles in tenant 'vxlan_stretch'" (NOT "nodes in fabric 'vxlan_stretch'")
- Question about tenant 'infraservices' showing anomaly 'BD_WITH_SUBNET...' → "What is the status of anomaly 'BD_WITH_SUBNET...' in tenant 'infraservices'" (NOT just "What is the status of anomaly 'BD_WITH_SUBNET...'")
- General question → "List all tenants" (no specific context to preserve)

Bad examples (do NOT use these formats):
- "Do you want me to list the EPGs?" (asking user, not direct query)
- "Should I check the anomalies?" (asking user, not direct query)
- "Show details of AppProfile 'X'" (missing tenant context if tenant was mentioned)
- "List all nodes in fabric 'vxlan_stretch'" when vxlan_stretch is a tenant (wrong entity type!)

Context preservation rules:
1. If question mentions "tenant 'X'", include "in tenant 'X'" or "under tenant 'X'" in suggestions
2. If question mentions "fabric 'Y'", include "in fabric 'Y'" in suggestions
3. If question mentions "node 'Z'", include references to that specific node
4. If answer mentions specific names, identify their entity type from the answer first, then use appropriately
5. CRITICAL: If answer lists anomalies/faults in response to a question about a specific tenant/fabric/node, the anomaly suggestions MUST preserve that scope:
   - Question about tenant 'X' showing anomaly 'A' → suggest "What is the status of anomaly 'A' in tenant 'X'"
   - Question about fabric 'Y' showing fault 'F' → suggest "Tell me about fault 'F' in fabric 'Y'"
   - Question about node 'Z' showing fault 'F' → suggest "What is fault 'F' affecting node 'Z'"

The network graph has the following schema: {schema}

Question: {question}
Answer: {answer}

Return only a list of three direct queries, one per line, without numbers or bullets.
Follow-up queries:"""
)

# --- Query Classification and MCP Integration ---
def get_capability_aware_suggestions(capabilities: dict) -> List[str]:
    """Generate query suggestions based on available data sources"""
    suggestions = []

    if capabilities['nd_available']:
        suggestions.extend([
            "What is the current health status of the network?",
            "Show me current anomalies",
            "List all managed fabrics"
        ])

    if capabilities['apic_available']:
        suggestions.extend([
            "Show me all tenants",
            "List application profiles"
        ])

    if capabilities['full_topology']:
        suggestions.append("Show me the complete network topology")

    # Fallback if no sources available
    if not suggestions:
        suggestions = ["Check system status", "List data sources"]

    return suggestions[:3]  # Limit to 3


def validate_query_feasibility(question: str, capabilities: dict) -> tuple[bool, str]:
    """
    Check if a query can be answered with available data sources.
    Returns (is_feasible, error_message)
    """
    question_lower = question.lower()

    # Check for APIC-specific queries
    apic_keywords = ['epg', 'endpoint group', 'tenant', 'contract', 'app profile',
                     'application profile', 'bridge domain', 'vrf']

    needs_apic = any(kw in question_lower for kw in apic_keywords)

    if needs_apic and not capabilities['apic_available']:
        return False, (
            "⚠️ This query requires APIC policy model data (EPG/Tenant/Contracts) which is currently unavailable. "
            "\n\n**Available queries:**\n"
            "- Network health and status\n"
            "- Fabric and device information\n"
            "- Live performance metrics\n"
            "- Anomalies and compliance\n"
            "\n💡 Try asking about current health, fabrics, or device status instead."
        )

    # Check for ND/MCP-specific queries
    if 'live' in question_lower or 'current' in question_lower or 'health' in question_lower:
        if not capabilities['nd_available']:
            return False, (
                "⚠️ This query requires Nexus Dashboard operational data which is currently unavailable. "
                "\n\n**Available queries:**\n"
                "- Network topology (if APIC is available)\n"
                "- Policy configuration\n"
                "\n💡 Nexus Dashboard connection is required for live metrics and health data."
            )

    return True, ""


def check_mcp_health(mcp_url: str, mcp_name: str, health_endpoint: str = "/health") -> dict:
    """
    Check if an MCP server is healthy and responsive.

    Args:
        mcp_url: Base URL of the MCP server
        mcp_name: Name of the MCP service (for logging)
        health_endpoint: Health check endpoint path (default: /health)

    Returns:
        dict with 'available' (bool) and 'error' (str or None)
    """
    try:
        import httpx
        with httpx.Client(timeout=2.0, verify=False) as client:
            response = client.get(f"{mcp_url}{health_endpoint}")
            if response.status_code == 200:
                return {'available': True, 'error': None}
            else:
                return {'available': False, 'error': f"HTTP {response.status_code}"}
    except httpx.TimeoutException:
        return {'available': False, 'error': 'Timeout'}
    except httpx.ConnectError:
        return {'available': False, 'error': 'Connection refused'}
    except Exception as e:
        return {'available': False, 'error': str(e)[:50]}


def check_nd_mcp_real_health(mcp_url: str) -> dict:
    """
    Deeper health check for the ND MCP: verifies the MCP can actually authenticate
    with the configured Nexus Dashboard cluster, not just that the MCP service is up.

    POSTs to /api/clusters/default/test which makes a live login attempt against ND.
    Returns available=True only when status is "success"; if the MCP is reachable but
    cannot reach/authenticate to ND, returns available=False with the ND error.
    """
    try:
        import httpx
        with httpx.Client(timeout=8.0, verify=False) as client:
            # First make sure the MCP web API itself is responsive
            health = client.get(f"{mcp_url}/api/health")
            if health.status_code != 200:
                return {'available': False, 'error': f"MCP /api/health HTTP {health.status_code}"}

            # Now verify the underlying ND connection
            r = client.post(f"{mcp_url}/api/clusters/default/test")
            if r.status_code != 200:
                return {'available': False, 'error': f"cluster test HTTP {r.status_code}"}

            try:
                body = r.json()
            except Exception:
                return {'available': False, 'error': 'cluster test returned non-JSON'}

            status = (body.get('status') or '').lower()
            if status == 'success':
                return {'available': True, 'error': None}

            message = body.get('message') or 'ND test failed'
            return {'available': False, 'error': message[:80]}
    except httpx.TimeoutException:
        return {'available': False, 'error': 'Timeout'}
    except httpx.ConnectError:
        return {'available': False, 'error': 'Connection refused'}
    except Exception as e:
        return {'available': False, 'error': str(e)[:80]}


def get_data_source_capabilities():
    """
    Check which data sources are available in Neo4j and MCP servers.
    Returns dict with capability flags and MCP status.
    """
    try:
        query = """
        MATCH (ds:DataSource)
        WHERE ds.available = true
        RETURN ds.name as source, ds.provides as provides
        """
        result = graph.query(query)

        sources = {row['source']: row.get('provides', '') for row in result}

        # Check MCP server health.
        # For ND MCP, the bare /api/health endpoint returns 200 even when the underlying
        # Nexus Dashboard auth is broken, so we use a deeper probe that actually attempts
        # login against the configured cluster. See check_nd_mcp_real_health.
        nd_mcp_status = check_nd_mcp_real_health(MCP_SERVER_URL) if MCP_ENABLED and MCP_SERVER_URL else {'available': False, 'error': 'Disabled'}
        intersight_mcp_status = check_mcp_health(INTERSIGHT_MCP_URL, "Intersight MCP", "/health") if INTERSIGHT_MCP_ENABLED else {'available': False, 'error': 'Disabled'}

        capabilities = {
            'apic_available': 'apic' in sources,
            'nd_available': 'nexus_dashboard' in sources,
            'intersight_available': 'intersight' in sources,
            'policy_model': 'apic' in sources,  # EPG, Tenant, Contracts
            'live_metrics': 'nexus_dashboard' in sources,  # MCP data
            'fabric_topology': 'nexus_dashboard' in sources,  # Basic fabric info
            'full_topology': 'apic' in sources and 'nexus_dashboard' in sources,
            'sources': sources,
            # MCP health status
            'mcp_health': {
                'nd_mcp': nd_mcp_status,
                'intersight_mcp': intersight_mcp_status
            }
        }

        return capabilities
    except Exception as e:
        print(f"⚠️ Failed to check data source capabilities: {e}")
        # Default to assuming both available (backward compatibility)
        return {
            'apic_available': True,
            'nd_available': True,
            'intersight_available': False,
            'policy_model': True,
            'live_metrics': True,
            'fabric_topology': True,
            'full_topology': True,
            'sources': {},
            'mcp_health': {
                'nd_mcp': {'available': False, 'error': 'Unknown'},
                'intersight_mcp': {'available': False, 'error': 'Unknown'}
            }
        }


def classify_query_intent(question: str) -> str:
    """
    Classify query intent to determine data source using keywords + LLM.

    Returns:
        "neo4j" - Static topology, relationships, historical data
        "mcp" - Live metrics, current status, real-time data (network)
        "intersight" - Compute/server data from Cisco Intersight
        "hybrid" - Requires both sources
    """
    question_lower = question.lower()

    # Extract quoted entity names (e.g., 'TS-FI-1-1', "server-01")
    import re
    quoted_entities = re.findall(r"['\"]([^'\"]+)['\"]", question)

    # Check if any quoted entity is an IntersightServer in Neo4j
    if INTERSIGHT_MCP_ENABLED and quoted_entities:
        try:
            for entity_name in quoted_entities:
                result = graph.query(
                    "MATCH (n:IntersightServer {name: $name}) RETURN labels(n) AS labels LIMIT 1",
                    params={"name": entity_name}
                )
                if result:
                    print(f"🖥️  Intersight query detected (entity '{entity_name}' is IntersightServer)")
                    return "intersight"
        except Exception as e:
            print(f"⚠️ Error checking entity type: {e}")

    # Check for Fabric Interconnect pattern in quoted entities (e.g., "TS-FI-1-1")
    if INTERSIGHT_MCP_ENABLED and quoted_entities:
        if any(re.search(r'\bfi\b|-fi-', e.lower()) for e in quoted_entities):
            print(f"🖥️  Intersight query detected (Fabric Interconnect pattern in entity)")
            return "intersight"

    # Keywords indicating Intersight/compute queries
    intersight_keywords = [
        "server", "ucs", "compute", "blade", "rack unit", "chassis",
        "fabric interconnect", "hyperflex", "vnic", "adapter",
        "server health", "server alarm", "server cpu", "server memory"
    ]

    # Check for Intersight keywords first (most specific)
    if INTERSIGHT_MCP_ENABLED and any(keyword in question_lower for keyword in intersight_keywords):
        print(f"🖥️  Intersight query detected (keyword match)")
        return "intersight"

    # Keywords indicating live/real-time data (MCP)
    live_keywords = [
        "current", "now", "live", "real-time", "latest",
        "health", "status", "cpu", "memory", "bandwidth",
        "utilization", "performance", "metric", "stat"
    ]

    # Keywords indicating topology/relationships (Neo4j)
    topology_keywords = [
        "connected", "relationship", "belongs", "topology",
        "graph", "path", "neighbor", "linked", "structure"
    ]

    # Keywords indicating historical data (Neo4j)
    historical_keywords = [
        "history", "past", "previous", "before", "changed",
        "was", "were", "used to", "last month", "yesterday"
    ]

    # Keywords indicating network-wide scope (suggests hybrid)
    scope_keywords = [
        "across", "all", "entire", "whole", "network-wide",
        "every", "each", "multiple", "fabrics"
    ]

    # Check for indicators
    has_live = any(keyword in question_lower for keyword in live_keywords)
    has_topology = any(keyword in question_lower for keyword in topology_keywords)
    has_historical = any(keyword in question_lower for keyword in historical_keywords)
    has_scope = any(keyword in question_lower for keyword in scope_keywords)

    # Improved classification rules
    if has_historical:
        return "neo4j"  # Historical data only in Neo4j
    elif has_live and (has_topology or has_scope):
        return "hybrid"  # Needs topology discovery + live data
    elif has_live:
        # Use LLM to determine if this is truly MCP-only or needs hybrid
        # For queries that might need multi-fabric data
        if has_scope or any(word in question_lower for word in ["network", "fabric", "site"]):
            classification_prompt = f"""Classify this network monitoring query into ONE category:

- "neo4j": Query about topology, relationships, connections, or historical data
- "mcp": Query about live metrics for a SINGLE specific device/fabric
- "hybrid": Query about live metrics ACROSS multiple fabrics/devices, or combining topology with live data

Question: {question}

Think step-by-step:
1. Does it ask for live/current data?
2. Does it need to know about multiple fabrics or network-wide scope?
3. Does it combine topology (what's connected) with metrics?

Classification (respond with ONLY one word - neo4j, mcp, or hybrid):"""

            try:
                llm_response = qa_llm.invoke(classification_prompt)
                llm_classification = llm_response.content.strip().lower() if hasattr(llm_response, 'content') else str(llm_response).strip().lower()

                # Extract the classification (handle cases where LLM adds explanation)
                for valid_type in ["hybrid", "neo4j", "mcp"]:
                    if valid_type in llm_classification:
                        print(f"🤖 LLM refined classification: {valid_type}")
                        return valid_type

                # Fallback if LLM response is unclear
                print(f"⚠️ LLM classification unclear: '{llm_classification}', defaulting to hybrid for network-scoped query")
                return "hybrid"
            except Exception as e:
                print(f"⚠️ LLM classification failed: {e}, defaulting to hybrid")
                return "hybrid"
        else:
            return "mcp"  # Live data, single scope
    else:
        return "neo4j"  # Default to Neo4j for topology/anomalies


async def query_mcp_with_llm(question: str, chat_history: str = "") -> tuple[str, List[DataSource]]:
    """
    Use LLM to select and execute appropriate MCP tools for the question.

    Returns:
        Tuple of (answer text, list of data sources)
    """
    mcp = get_mcp_client()
    if not mcp or not mcp.connected:
        return ("MCP integration is not available at this time.", [])

    try:
        # Get available read-only tools
        tools = mcp.get_read_only_tools()
        if not tools:
            return ("No MCP tools available.", [])

        # Improved tool selection with LLM assistance
        question_lower = question.lower()

        # Keyword-based filtering to narrow down candidates
        candidate_tools = []

        # Check for specific queries BEFORE generic ones to avoid wrong tool selection
        if "anomal" in question_lower:
            # Anomaly queries - prioritize anomaly tools
            candidate_tools = [t for t in tools if "anomal" in t.lower()]
        elif "fault" in question_lower:
            # Fault queries
            candidate_tools = [t for t in tools if "fault" in t.lower()]
        elif "compliance" in question_lower:
            candidate_tools = [t for t in tools if "compliance" in t.lower()]
        elif "bandwidth" in question_lower or "traffic" in question_lower:
            candidate_tools = [t for t in tools if any(kw in t.lower() for kw in ["bandwidth", "traffic", "flow", "interface"])]
        elif "cpu" in question_lower or "memory" in question_lower:
            candidate_tools = [t for t in tools if any(kw in t.lower() for kw in ["cpu", "memory", "resource", "utilization"])]
        elif "health" in question_lower or "status" in question_lower:
            # Generic health/status queries (fallback after specific checks)
            health_keywords = ["health", "status", "fabric", "site", "device"]
            for keyword in health_keywords:
                matches = [t for t in tools if keyword in t.lower() and "compliance" not in t.lower()]
                candidate_tools.extend(matches)
                if len(candidate_tools) >= 10:
                    break

        # Remove duplicates while preserving order
        seen = set()
        candidate_tools = [t for t in candidate_tools if not (t in seen or seen.add(t))]

        if not candidate_tools:
            # Fallback: search for any get/list/describe tools
            candidate_tools = [t for t in tools[:50] if any(prefix in t.lower() for prefix in ["get", "list", "describe"])]

        # Use LLM to select the most relevant tools from candidates
        if len(candidate_tools) > 5:
            tool_selection_prompt = f"""You are helping select the most relevant API tools to answer a user's question.

Question: {question}

Available tools (first 20):
{chr(10).join(f"{i+1}. {t}" for i, t in enumerate(candidate_tools[:20]))}

Select the top 1-3 most relevant tool names (comma-separated) that would best answer this question. Only return the tool names, nothing else.

Selected tools:"""

            selection_response = qa_llm.invoke(tool_selection_prompt)
            selected_names = selection_response.content if hasattr(selection_response, 'content') else str(selection_response)

            # Parse LLM response to extract tool names
            selected_tools = []
            for line in selected_names.split('\n'):
                for candidate in candidate_tools:
                    if candidate in line:
                        selected_tools.append(candidate)
                        break

            # Fallback if LLM didn't return valid tools
            if not selected_tools:
                selected_tools = candidate_tools[:3]
        else:
            selected_tools = candidate_tools[:3]

        if not selected_tools:
            # Last resort fallback
            selected_tools = tools[:1]

        print(f"🔧 Selected {len(selected_tools)} MCP tools from {len(candidate_tools)} candidates: {selected_tools[:3]}")

        # Execute selected tools
        sources = []
        results = []

        # Discover available fabrics from Neo4j if tools need fabricName
        fabric_names = []

        # Check if tools need fabric parameter (broader pattern matching)
        fabric_keywords = ["fabric", "resource", "device", "analyze", "status", "overview",
                          "telemetry", "health", "interface", "anomal", "compliance"]
        tools_need_fabric = any(any(kw in t.lower() for kw in fabric_keywords)
                                for t in selected_tools[:3])

        # Also check if question explicitly mentions a fabric or tenant
        import re
        fabric_match = re.search(r"fabric\s+['\"]?([a-zA-Z0-9_-]+)['\"]?", question.lower())
        specific_fabric = fabric_match.group(1) if fabric_match else None

        # Check if question mentions a tenant - resolve to fabric
        tenant_match = re.search(r"tenant\s+['\"]?([a-zA-Z0-9_-]+)['\"]?", question.lower())
        if tenant_match and not specific_fabric:
            tenant_name = tenant_match.group(1)
            try:
                # Find which fabric owns this tenant
                tenant_fabric_query = f"""
                MATCH (f:Fabric)-[:MANAGES]->(t:Tenant {{name: '{tenant_name}'}})
                RETURN f.name AS fabric
                LIMIT 1
                """
                tenant_fabric_result = graph.query(tenant_fabric_query)
                if tenant_fabric_result and len(tenant_fabric_result) > 0:
                    specific_fabric = tenant_fabric_result[0].get('fabric')
                    print(f"📊 Resolved tenant '{tenant_name}' to fabric '{specific_fabric}'")
            except Exception as e:
                print(f"⚠️ Failed to resolve tenant '{tenant_name}' to fabric: {e}")

        if tools_need_fabric or specific_fabric:
            try:
                if specific_fabric:
                    # Question mentions specific fabric - use only that one
                    fabric_names = [specific_fabric]
                    print(f"📊 Using fabric from question: {specific_fabric}")
                else:
                    # Query all fabrics from Neo4j
                    fabric_query = "MATCH (f:Fabric) RETURN f.name AS fabric"
                    fabric_result = graph.query(fabric_query)
                    fabric_names = [row['fabric'] for row in fabric_result if row.get('fabric')]
                    print(f"📊 Discovered {len(fabric_names)} fabrics from Neo4j: {fabric_names}")
            except Exception as e:
                print(f"⚠️ Failed to query fabrics from Neo4j: {e}")
                # Fallback to environment variable
                default_fabric = os.getenv("APIC_FABRIC_NAME", "")
                if default_fabric:
                    fabric_names = [default_fabric]

        for tool_name in selected_tools[:3]:  # Limit to 3 tools max
            tool_lower = tool_name.lower()

            # Determine if this tool needs fabric parameter (broader matching)
            needs_fabric = any(kw in tool_lower for kw in fabric_keywords)

            if needs_fabric and fabric_names:
                # Call tool for each fabric and aggregate results
                for fabric_name in fabric_names:
                    try:
                        tool_args = {"fabricName": fabric_name}
                        print(f"🔧 Calling {tool_name} with args: {tool_args}")

                        result = await mcp.call_tool(tool_name, tool_args)

                        # Extract text from MCP response
                        content = result.get("content", [])
                        if content and len(content) > 0:
                            text = content[0].get("text", "")
                            results.append(f"Fabric: {fabric_name}\nTool: {tool_name}\nResult: {text}")

                            sources.append(DataSource(
                                type="mcp",
                                description=f"Real-time data from {tool_name} (fabric: {fabric_name})",
                                details={"tool": tool_name, "arguments": tool_args, "fabric": fabric_name}
                            ))
                    except Exception as e:
                        print(f"⚠️ Failed to execute {tool_name} for fabric {fabric_name}: {e}")
                        continue
            else:
                # Tool doesn't need fabric parameter, call once
                try:
                    tool_args = {}
                    print(f"🔧 Calling {tool_name} with args: {tool_args}")

                    result = await mcp.call_tool(tool_name, tool_args)

                    # Extract text from MCP response
                    content = result.get("content", [])
                    if content and len(content) > 0:
                        text = content[0].get("text", "")
                        results.append(f"Tool: {tool_name}\nResult: {text}")

                        sources.append(DataSource(
                            type="mcp",
                            description=f"Real-time data from {tool_name}",
                            details={"tool": tool_name, "arguments": tool_args}
                        ))
                except Exception as e:
                    print(f"⚠️ Failed to execute tool {tool_name}: {e}")
                    continue

        if results:
            # Truncate results to fit within token budget
            # Target: ~4000 tokens for results (leave 4000 for prompt + answer + overhead)
            MAX_RESULT_TOKENS = 4000

            # Use tiktoken for accurate token counting
            try:
                encoding = tiktoken.get_encoding("cl100k_base")  # GPT-4 encoding, close enough for estimation
            except:
                # Fallback to rough estimation if tiktoken fails
                encoding = None

            def count_tokens(text: str) -> int:
                if encoding:
                    return len(encoding.encode(text))
                else:
                    return len(text) // 4  # Rough fallback

            truncated_results = []
            current_tokens = 0

            for result in results:
                result_tokens = count_tokens(result)
                if current_tokens + result_tokens <= MAX_RESULT_TOKENS:
                    truncated_results.append(result)
                    current_tokens += result_tokens
                else:
                    # Add truncated version if space allows
                    remaining_tokens = MAX_RESULT_TOKENS - current_tokens
                    if remaining_tokens > 100:  # At least 100 tokens worth
                        # Binary search to find how much text fits
                        left, right = 0, len(result)
                        while left < right:
                            mid = (left + right + 1) // 2
                            if count_tokens(result[:mid]) <= remaining_tokens - 20:  # Reserve 20 for truncation marker
                                left = mid
                            else:
                                right = mid - 1
                        if left > 0:
                            truncated_results.append(result[:left] + "\n[... truncated ...]")
                            current_tokens += count_tokens(result[:left] + "\n[... truncated ...]")
                    break

            combined_results = "\n\n".join(truncated_results)

            if len(truncated_results) < len(results):
                print(f"⚠️ Truncated MCP results: kept {len(truncated_results)}/{len(results)} results ({current_tokens} tokens) to fit budget")
            else:
                print(f"✅ All {len(results)} MCP results fit within token budget ({current_tokens} tokens)")

            # Use LLM to format the results into a natural answer
            synthesis_prompt = f"""Based on the following real-time data from Nexus Dashboard, provide a clear and concise answer to the question.

Question: {question}

Real-time Data:
{combined_results}

Answer:"""

            qa_response = qa_llm.invoke(synthesis_prompt)
            answer = qa_response.content if hasattr(qa_response, 'content') else str(qa_response)

            return (answer, sources)
        else:
            return ("No real-time data available for this query.", [])

    except Exception as e:
        print(f"❌ MCP query error: {e}")
        return (f"Error querying real-time data: {str(e)}", [])


async def query_intersight_with_llm(question: str, chat_history: str = "") -> tuple[str, List[DataSource]]:
    """
    Query Cisco Intersight for compute/server information via MCP HTTP server.

    Returns:
        Tuple of (answer text, list of data sources)
    """
    if not INTERSIGHT_MCP_ENABLED:
        return ("Intersight integration is not enabled.", [])

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            # Check health
            health_response = await client.get(f"{INTERSIGHT_MCP_URL}/health")
            if health_response.status_code != 200:
                return ("Intersight MCP server is not available.", [])

            # Get available tools
            tools_response = await client.get(f"{INTERSIGHT_MCP_URL}/api/tools")
            tools_data = tools_response.json()
            tools = [t["name"] for t in tools_data.get("tools", [])]

            if not tools:
                return ("No Intersight tools available.", [])

            print(f"🔧 Intersight MCP: {len(tools)} tools available")

            # Keyword-based tool selection
            question_lower = question.lower()
            candidate_tools = []

            # Step A: Extract quoted entities and look up MOIDs in Neo4j FIRST.
            # We use this to pick a more precise tool (get_server_details, alarm filtering by MOID).
            import re
            quoted_entities = re.findall(r"['\"]([^'\"]+)['\"]", question)
            _has_fi_entity = any(re.search(r'\bfi\b|-fi-', e.lower()) for e in quoted_entities)

            server_moid = None
            server_name = None
            if quoted_entities:
                for entity_name in quoted_entities:
                    try:
                        result = graph.query(
                            "MATCH (s:IntersightServer {name: $name}) RETURN s.moid AS moid, s.name AS name LIMIT 1",
                            params={"name": entity_name}
                        )
                        if result and len(result) > 0:
                            server_moid = result[0].get('moid')
                            server_name = result[0].get('name')
                            print(f"🔍 Found server '{server_name}' with MOID: {server_moid}")
                            break
                    except Exception as e:
                        print(f"⚠️ Error looking up server MOID: {e}")

            # If the current question has no quoted entity (follow-ups like "what about this
            # server's adapters?") look back through chat_history for a recently-mentioned
            # IntersightServer name. This keeps multi-turn conversations grounded.
            if not server_moid and chat_history:
                try:
                    history_entities = re.findall(r"['\"]([^'\"]+)['\"]", chat_history)
                    # Walk in reverse so the most recent mention wins
                    for entity_name in reversed(history_entities):
                        result = graph.query(
                            "MATCH (s:IntersightServer {name: $name}) RETURN s.moid AS moid, s.name AS name LIMIT 1",
                            params={"name": entity_name}
                        )
                        if result and len(result) > 0:
                            server_moid = result[0].get('moid')
                            server_name = result[0].get('name')
                            print(f"🔍 Resolved 'this server' from chat history -> '{server_name}' ({server_moid})")
                            break
                except Exception as e:
                    print(f"⚠️ Error looking up server MOID from chat history: {e}")

            # Step B: Tool selection
            # Alarm queries (prioritize before generic health)
            if "alarm" in question_lower:
                candidate_tools = [t for t in tools if "alarm" in t.lower()]
            # Fabric Interconnect queries (check before generic server check)
            elif _has_fi_entity or "fabric interconnect" in question_lower or "fabric-interconnect" in question_lower:
                candidate_tools = [t for t in tools if any(kw in t.lower() for kw in ["fabric_interconnect", "network_element"])]
                if not candidate_tools:
                    candidate_tools = [t for t in tools if "fabric" in t.lower() and "interconnect" in t.lower()]
            # NOTE: get_server_details, get_server_profile, and get_server_telemetry are
            # broken in the Intersight MCP server (1.0.16) - they receive `undefined` for the
            # MOID regardless of how it's passed and return HTTP 404. Until upstream is fixed,
            # we route everything through list_compute_servers and post-filter the response
            # in Python (see "Post-filter" block below). The MCP also ignores the OData filter
            # parameter so server-side filtering doesn't help either.
            #
            # If the user asked about CPU/memory/health/etc. for a SPECIFIC server we know in
            # Neo4j, prefer list_compute_servers + post-filter: the PhysicalSummary record
            # already includes CpuCapacity, AvailableMemory, AlarmSummary, OperPowerState etc.
            # This avoids the broken telemetry tool while still answering the question.
            elif server_moid and ("cpu" in question_lower or "memory" in question_lower or "health" in question_lower or "hardware" in question_lower or "temperature" in question_lower):
                candidate_tools = [t for t in tools if "list" in t.lower() and "server" in t.lower() and "profile" not in t.lower()]
            # Health/telemetry queries WITHOUT a specific server (whole-fleet view)
            elif "health" in question_lower or "telemetry" in question_lower or "cpu" in question_lower or "memory" in question_lower or "temperature" in question_lower:
                candidate_tools = [t for t in tools if any(kw in t.lower() for kw in ["health", "telemetry", "statistics"])]
            # Server/compute keywords
            elif "server" in question_lower or "ucs" in question_lower or "compute" in question_lower or "blade" in question_lower or "rack" in question_lower:
                candidate_tools = [t for t in tools if any(kw in t.lower() for kw in ["server", "compute", "blade", "rack"]) and "profile" not in t.lower()]
            # Network adapter / vNIC / connectivity questions for a SPECIFIC known server:
            # The vnic/adapter MCP tools are all GET-by-MOID and broken (return 404
            # 'undefined'), and list_vnics needs a lanConnectivityPolicyMoid we don't have.
            # Route through list_compute_servers - the PhysicalSummary record includes
            # MgmtIpAddress, KvmIpAddress, etc. We additionally enrich the answer below
            # with Endpoint nodes already correlated to this server in Neo4j (real MACs +
            # ACI-learned IPs).
            elif server_moid and ("mac" in question_lower or "vnic" in question_lower or "adapter" in question_lower or "connectivity" in question_lower or "network" in question_lower):
                candidate_tools = [t for t in tools if "list" in t.lower() and "server" in t.lower() and "profile" not in t.lower()]
            # Network adapter/vNIC keywords (no specific server - fleet-wide)
            elif "mac" in question_lower or "vnic" in question_lower or "adapter" in question_lower:
                candidate_tools = [t for t in tools if any(kw in t.lower() for kw in ["vnic", "adapter", "mac", "ethernet"])]
            # Policy keywords
            elif "policy" in question_lower or "bios" in question_lower or "boot" in question_lower:
                candidate_tools = [t for t in tools if "policy" in t.lower()]
            else:
                # Default: list servers
                candidate_tools = [t for t in tools if "list" in t.lower() and "server" in t.lower() and "profile" not in t.lower()]

            if not candidate_tools:
                candidate_tools = tools[:5]  # Fallback to first 5 tools

            # Use LLM to select best tool
            if len(candidate_tools) > 1:
                tool_selection_prompt = f"""Select the most relevant Cisco Intersight tool to answer the question.

Question: {question}

Available tools:
{chr(10).join(f"{i+1}. {t}" for i, t in enumerate(candidate_tools[:10]))}

Return only the tool name, nothing else.
Selected tool:"""

                selection_response = qa_llm.invoke(tool_selection_prompt)
                selected_tool_text = selection_response.content if hasattr(selection_response, 'content') else str(selection_response)

                # Parse tool name
                selected_tool = None
                for tool in candidate_tools:
                    if tool in selected_tool_text:
                        selected_tool = tool
                        break

                if not selected_tool:
                    selected_tool = candidate_tools[0]
            else:
                selected_tool = candidate_tools[0]

            print(f"🔧 Selected Intersight tool: {selected_tool}")

            # Step C: Generate tool arguments
            tool_arguments = {}

            # Add server MOID to tool arguments if found
            if server_moid:
                # Pass MOID to get/query tools (e.g. get_server_details, get_server_telemetry)
                if any(kw in selected_tool.lower() for kw in ["get_server", "get_compute", "health", "telemetry", "statistics", "detail"]):
                    # get_server_telemetry uses serverMoid, others use moid
                    if "telemetry" in selected_tool.lower():
                        tool_arguments["serverMoid"] = server_moid
                    else:
                        tool_arguments["moid"] = server_moid
                    print(f"📋 Passing server MOID to tool: {server_moid}")
                # For list_alarms with a server MOID, filter by AffectedMoid
                elif "alarm" in selected_tool.lower():
                    tool_arguments["filter"] = f"AffectedMoid eq '{server_moid}'"
                    print(f"📋 Filtering alarms by AffectedMoid: {server_moid}")
                # For list tools, use filter parameter with server name (MCP often ignores this
                # but harmless to try as a hint)
                elif "list" in selected_tool.lower() and server_name:
                    tool_arguments["filter"] = f"Name eq '{server_name}'"
                    print(f"📋 Filtering by server name: {server_name}")

            # For fabric_interconnect/network_element tools, filter by name from quoted entity
            if "fabric_interconnect" in selected_tool.lower() or "network_element" in selected_tool.lower():
                for entity_name in quoted_entities:
                    if re.search(r'\bfi\b|-fi-', entity_name.lower()):
                        tool_arguments["filter"] = f"Name eq '{entity_name}'"
                        print(f"📋 Filtering fabric interconnect by name: {entity_name}")
                        break

            # For list_alarms, add severity filter if mentioned
            if selected_tool == "list_alarms":
                if "critical" in question_lower:
                    tool_arguments["filter"] = "Severity eq 'Critical'"
                elif "warning" in question_lower:
                    tool_arguments["filter"] = "Severity eq 'Warning'"
                elif "info" in question_lower:
                    tool_arguments["filter"] = "Severity eq 'Info'"

            # For server queries with power state
            elif "list_compute_servers" in selected_tool or "list_compute_blades" in selected_tool or "list_compute_rack_units" in selected_tool:
                if "powered on" in question_lower or "running" in question_lower:
                    tool_arguments["filter"] = "OperPowerState eq 'on'"
                elif "powered off" in question_lower or "shutdown" in question_lower:
                    tool_arguments["filter"] = "OperPowerState eq 'off'"

            # Execute tool
            execute_response = await client.post(
                f"{INTERSIGHT_MCP_URL}/api/execute",
                json={"tool": selected_tool, "arguments": tool_arguments},
                timeout=60.0
            )

            if execute_response.status_code != 200:
                error_text = execute_response.text
                print(f"❌ Intersight tool execution failed: {error_text}")
                return (f"Failed to execute Intersight tool: {error_text}", [])

            result = execute_response.json()

            # Post-filter: the Intersight MCP often ignores the OData `filter` parameter and
            # returns ALL records. When we know which server/MOID the user asked about, trim the
            # results list to matching entries so the LLM doesn't synthesize from the wrong row.
            if "list" in selected_tool.lower() and (server_moid or server_name):
                try:
                    # The MCP response shape is {success, tool, parameters, result: {Results: [...]}}
                    inner = result.get("result") if isinstance(result, dict) else None
                    if isinstance(inner, dict) and isinstance(inner.get("Results"), list):
                        all_rows = inner["Results"]
                        target_name_lower = (server_name or "").lower()
                        filtered = []
                        for row in all_rows:
                            row_moid = row.get("Moid")
                            row_name = (row.get("Name") or "").lower()
                            row_affected = (row.get("AffectedMoid") or "") if isinstance(row.get("AffectedMoid"), str) else ""
                            if server_moid and (row_moid == server_moid or row_affected == server_moid):
                                filtered.append(row)
                            elif target_name_lower and row_name == target_name_lower:
                                filtered.append(row)
                        if filtered and len(filtered) != len(all_rows):
                            print(f"🔎 Post-filtered {len(all_rows)} → {len(filtered)} result(s) matching server '{server_name or server_moid}'")
                            inner["Results"] = filtered
                except Exception as e:
                    print(f"⚠️ Post-filter failed (continuing with raw result): {e}")

            # Enrich with Neo4j-correlated endpoints for adapter/connectivity questions about
            # a known server. The MCP doesn't expose a per-server host-eth-interface tool, but
            # the ingestor already correlates server vNICs to ACI Endpoints by MAC. Pull that
            # data so the LLM can answer "what adapters does this server have" properly.
            if server_moid and ("mac" in question_lower or "vnic" in question_lower or "adapter" in question_lower or "connectivity" in question_lower or "network" in question_lower):
                try:
                    endpoint_rows = graph.query(
                        """
                        MATCH (s:IntersightServer {moid: $moid})-[r:CONNECTED_TO]->(e:Endpoint)
                        OPTIONAL MATCH (e)-[:MEMBER_OF]->(epg:EPG)
                        OPTIONAL MATCH (epg)<-[:HAS_EPG]-(ap:AppProfile)<-[:HAS_AP]-(t:Tenant)
                        RETURN e.mac AS mac, e.ip AS ip,
                               r.interface_name AS interface,
                               epg.name AS epg,
                               ap.name AS app_profile,
                               t.name AS tenant
                        ORDER BY r.interface_name
                        """,
                        params={"moid": server_moid}
                    )
                    if endpoint_rows:
                        print(f"🔗 Enriching with {len(endpoint_rows)} correlated endpoint(s) from Neo4j")
                        if isinstance(result, dict):
                            result["correlatedEndpointsFromAci"] = endpoint_rows
                        _aci_endpoint_count = len(endpoint_rows)
                    else:
                        _aci_endpoint_count = 0
                except Exception as e:
                    print(f"⚠️ Endpoint enrichment failed: {e}")
                    _aci_endpoint_count = 0
            else:
                _aci_endpoint_count = 0

            # Track data source(s)
            sources = [DataSource(
                type="intersight",
                description=f"Cisco Intersight compute data from {selected_tool}",
                details={"tool": selected_tool, "account": "CAI-NL"}
            )]
            if _aci_endpoint_count > 0:
                sources.append(DataSource(
                    type="neo4j",
                    description=f"Correlated ACI endpoints ({_aci_endpoint_count}) from knowledge graph",
                    details={"server": server_name or server_moid}
                ))

            # Truncate result to fit token budget
            MAX_INTERSIGHT_TOKENS = 4000
            result_json = json.dumps(result, indent=2)

            try:
                encoding = tiktoken.get_encoding("cl100k_base")
            except:
                encoding = None

            def count_tokens(text: str) -> int:
                if encoding:
                    return len(encoding.encode(text))
                else:
                    return len(text) // 4

            result_tokens = count_tokens(result_json)
            truncation_note = ""

            if result_tokens > MAX_INTERSIGHT_TOKENS:
                print(f"⚠️  Intersight result too large ({result_tokens} tokens), truncating to {MAX_INTERSIGHT_TOKENS}")

                # Binary search to find truncation point
                left, right = 0, len(result_json)
                while left < right:
                    mid = (left + right + 1) // 2
                    if count_tokens(result_json[:mid]) <= MAX_INTERSIGHT_TOKENS:
                        left = mid
                    else:
                        right = mid - 1

                result_json = result_json[:left] + "\n... (truncated due to size)"
                truncation_note = f"\n\nNote: Result truncated from {result_tokens} to ~{MAX_INTERSIGHT_TOKENS} tokens."

            # Synthesize answer from tool result
            synthesis_prompt = f"""Based on the following Cisco Intersight compute/server data, provide a clear answer to the question.

Question: {question}

Intersight Data:
{result_json}{truncation_note}

Answer (be concise and focus on the key information):"""

            qa_response = qa_llm.invoke(synthesis_prompt)
            answer = qa_response.content if hasattr(qa_response, 'content') else str(qa_response)

            return (answer, sources)

    except Exception as e:
        print(f"❌ Intersight query error: {e}")
        import traceback
        traceback.print_exc()
        return (f"Error querying Intersight: {str(e)}", [])


async def query_hybrid(question: str, chat_history: str = "") -> tuple[str, List[DataSource]]:
    """
    Execute hybrid query using both Neo4j and MCP.

    Returns:
        Tuple of (answer text, list of data sources)
    """
    all_sources = []

    # Step 1: Get topology/relationships from Neo4j
    try:
        cypher_chain = CYPHER_PROMPT | cypher_llm
        cypher_response = cypher_chain.invoke({
            "schema": graph.get_schema,
            "question": question,
            "chat_history": chat_history
        })
        raw_cypher = cypher_response.content if hasattr(cypher_response, 'content') else str(cypher_response)
        clean_cypher = clean_cypher_output(raw_cypher)

        # Validate Cypher query
        is_valid, validation_error = validate_cypher_query(clean_cypher)
        if is_valid:
            neo4j_result = traced_neo4j_query(graph, clean_cypher, "neo4j.cypher.hybrid_query")

            all_sources.append(DataSource(
                type="neo4j",
                description="Network topology and relationships from knowledge graph",
                details={"query": clean_cypher, "result_count": len(neo4j_result)}
            ))

            neo4j_context = f"Topology Data:\n{str(neo4j_result)}"
        else:
            print(f"⚠️ Invalid Cypher in hybrid query: {validation_error}")
            neo4j_context = "No topology data available."
    except Exception as e:
        print(f"⚠️ Neo4j query failed in hybrid mode: {e}")
        neo4j_context = "Topology data unavailable."

    # Step 2: Get live data from MCP
    mcp_answer, mcp_sources = await query_mcp_with_llm(question, chat_history)
    all_sources.extend(mcp_sources)

    # Step 3: Merge results
    merge_prompt = f"""You are a network operations assistant. Combine the following information to answer the user's question comprehensively.

Question: {question}

{neo4j_context}

Live Data:
{mcp_answer}

Provide a comprehensive answer that integrates both the topology/relationship data and the live operational data:"""

    qa_response = qa_llm.invoke(merge_prompt)
    answer = qa_response.content if hasattr(qa_response, 'content') else str(qa_response)

    return (answer, all_sources)


# --- API Endpoint ---
@app.post("/ask", response_model=Response)
async def ask_agent(query: Query):
    # Convert Gradio's history format to a simple string for the prompt
    history_str = "\n".join([f"Human: {q}\nAssistant: {a}" for q, a in query.chat_history])

    print(f"🤖 Received question: {query.question} with history: {history_str}")

    # Initialize security info and sources
    security_info = SecurityInfo()
    data_sources = []

    # --- Cisco AI Defense: Inspect user prompt ---
    if AI_DEFENSE_ENABLED and AI_DEFENSE_INSPECT_PROMPTS:
        print("🛡️ Inspecting user prompt with AI Defense...")
        prompt_result = inspect_with_ai_defense(query.question, role="user")
        security_info.prompt_safe = prompt_result.is_safe
        security_info.prompt_severity = prompt_result.severity
        security_info.prompt_violations = prompt_result.violated_rules

        if prompt_result.action == "block":
            security_info.blocked = True
            security_info.warning = f"⚠️ Security Alert: Your question was blocked due to policy violations: {', '.join(prompt_result.classifications)}"
            if prompt_result.explanation:
                security_info.warning += f"\n\nDetails: {prompt_result.explanation}"
            print(f"🚫 Prompt blocked by AI Defense: {prompt_result.classifications}")
            return Response(
                answer="I cannot process this request due to security policy restrictions. Please rephrase your question.",
                suggestions=[],
                security=security_info,
                sources=[]
            )
        elif prompt_result.action == "warn":
            security_info.warning = f"⚠️ Security Notice: Potential concerns detected in your question ({prompt_result.severity}): {', '.join(prompt_result.violated_rules)}"
            print(f"⚠️ Prompt warning from AI Defense: {prompt_result.violated_rules}")

    # --- Check Data Source Capabilities ---
    capabilities = get_data_source_capabilities()
    is_feasible, error_msg = validate_query_feasibility(query.question, capabilities)

    if not is_feasible:
        print(f"⚠️ Query not feasible with current data sources: {error_msg}")
        return Response(
            answer=error_msg,
            suggestions=get_capability_aware_suggestions(capabilities),
            security=security_info,
            sources=[]
        )

    # --- Query Classification and Routing ---
    query_intent = classify_query_intent(query.question)
    print(f"🔍 Query classified as: {query_intent}")

    try:
        if query_intent == "intersight":
            # Intersight-only query (compute/server data)
            print("🖥️  Executing Intersight query...")
            answer, data_sources = await query_intersight_with_llm(query.question, history_str)

        elif query_intent == "mcp":
            # MCP-only query (live data)
            print("🔌 Executing MCP-only query...")
            answer, data_sources = await query_mcp_with_llm(query.question, history_str)

        elif query_intent == "hybrid":
            # Hybrid query (Neo4j + MCP)
            print("🔄 Executing hybrid query (Neo4j + MCP)...")
            answer, data_sources = await query_hybrid(query.question, history_str)

        else:
            # Neo4j-only query (topology/relationships)
            print("📊 Executing Neo4j-only query...")
            # Step 1: Generate Cypher using the LLM
            cypher_chain = CYPHER_PROMPT | cypher_llm
            cypher_response = cypher_chain.invoke({
                "schema": graph.get_schema,
                "question": query.question,
                "chat_history": history_str
            })
            raw_cypher = cypher_response.content if hasattr(cypher_response, 'content') else str(cypher_response)
            print(f"📝 Raw Cypher generated:\n{raw_cypher}")

            # Step 2: Clean the Cypher (remove explanatory text)
            clean_cypher = clean_cypher_output(raw_cypher)
            print(f"🧹 Cleaned Cypher:\n{clean_cypher}")

            # Step 3: Validate the Cypher query
            is_valid, validation_error = validate_cypher_query(clean_cypher)
            if not is_valid:
                print(f"❌ Cypher validation failed: {validation_error}")
                raise ValueError(f"Invalid Cypher query: {validation_error}")

            # Step 4: Execute the Cypher query
            query_result = traced_neo4j_query(graph, clean_cypher, "neo4j.cypher.user_query")
            print(f"📊 Query returned {len(query_result)} results")

            # Track Neo4j source
            data_sources.append(DataSource(
                type="neo4j",
                description="Network topology and knowledge graph",
                details={"query": clean_cypher, "result_count": len(query_result)}
            ))

            # Step 4: Generate answer using qa_llm
            # Use detailed template for anomaly queries to include recommended actions
            if is_anomaly_query(query.question):
                qa_template = QA_TEMPLATE_DETAILED
                print("📋 Using detailed template for anomaly query")
            else:
                qa_template = QA_TEMPLATE_CONCISE

            qa_prompt_dynamic = PromptTemplate.from_template(qa_template)
            qa_chain = qa_prompt_dynamic | qa_llm
            qa_response = qa_chain.invoke({
                "context": str(query_result),
                "question": query.question
            })
            answer = qa_response.content if hasattr(qa_response, 'content') else str(qa_response)

        # --- Cisco AI Defense: Inspect LLM response ---
        if AI_DEFENSE_ENABLED and AI_DEFENSE_INSPECT_RESPONSES:
            print("🛡️ Inspecting LLM response with AI Defense...")
            response_result = inspect_with_ai_defense(answer, role="assistant")
            security_info.response_safe = response_result.is_safe
            security_info.response_severity = response_result.severity
            security_info.response_violations = response_result.violated_rules

            if response_result.action == "block":
                security_info.blocked = True
                security_info.warning = f"⚠️ Security Alert: The AI response was blocked due to policy violations: {', '.join(response_result.classifications)}"
                print(f"🚫 Response blocked by AI Defense: {response_result.classifications}")
                answer = "I generated a response, but it was blocked by security policies. Please try a different question."
            elif response_result.action == "warn":
                if security_info.warning:
                    security_info.warning += f"\n\n⚠️ Response Notice: {', '.join(response_result.violated_rules)}"
                else:
                    security_info.warning = f"⚠️ Response Notice: Potential concerns in AI response ({response_result.severity}): {', '.join(response_result.violated_rules)}"
                print(f"⚠️ Response warning from AI Defense: {response_result.violated_rules}")

    except Exception as e:
        error_msg = str(e)
        print(f"❌ Error during query execution: {error_msg}")
        if "CypherSyntaxError" in error_msg or "SyntaxError" in error_msg:
            answer = "I apologize, but I had trouble generating a valid database query for your question. Could you please rephrase your question or be more specific about what information you're looking for?"
        elif "does not appear to be a valid Cypher" in error_msg:
            answer = "I couldn't understand how to query the database for that question. Could you please rephrase it more specifically?"
        else:
            answer = f"I encountered an error while processing your question. Please try rephrasing your question or asking something simpler."

    print(f"💡 Generated answer: {answer}")
    print(f"📍 Data sources used: {[s.type for s in data_sources]}")

    # Return answer with security info and data sources
    return Response(
        answer=answer,
        suggestions=[],
        security=security_info if AI_DEFENSE_ENABLED else None,
        sources=data_sources
    )


# --- Streaming API Endpoint ---
# Section markers for progress tracking during QA response generation
QA_SECTIONS = [
    ("## Entity Summary", "📋 Generating Entity Summary..."),
    ("## Issues Detected", "⚠️ Analyzing Issues Detected..."),
    ("## Recommended Actions", "🔧 Preparing Recommended Actions..."),
    ("## Additional Context", "📚 Adding Additional Context..."),
]

@app.post("/ask/stream")
async def ask_agent_stream(query: Query):
    """Stream progress updates while processing the query, including QA section progress"""

    async def generate_stream() -> AsyncGenerator[str, None]:
        history_str = "\n".join([f"Human: {q}\nAssistant: {a}" for q, a in query.chat_history])

        # Initialize security tracking
        security_info = {
            "prompt_safe": True,
            "response_safe": True,
            "blocked": False,
            "warning": None
        }

        # Send initial status
        yield f"data: {json.dumps({'status': 'thinking', 'message': '🔍 Analyzing your question...'})}\n\n"
        await asyncio.sleep(0.1)

        # --- Cisco AI Defense: Inspect user prompt ---
        if AI_DEFENSE_ENABLED and AI_DEFENSE_INSPECT_PROMPTS:
            yield f"data: {json.dumps({'status': 'security', 'message': '🛡️ Checking security policies...'})}\n\n"
            await asyncio.sleep(0.1)

            prompt_result = inspect_with_ai_defense(query.question, role="user")
            security_info["prompt_safe"] = prompt_result.is_safe

            if prompt_result.action == "block":
                security_info["blocked"] = True
                security_info["warning"] = f"Security policy violation: {', '.join(prompt_result.classifications)}"
                yield f"data: {json.dumps({'status': 'blocked', 'message': '🚫 Request blocked by security policy', 'answer': 'I cannot process this request due to security policy restrictions.', 'security': security_info})}\n\n"
                return
            elif prompt_result.action == "warn":
                warning_msg = f"Security notice: {', '.join(prompt_result.violated_rules)}"
                security_info["warning"] = warning_msg
                yield f"data: {json.dumps({'status': 'warning', 'message': f'⚠️ {warning_msg}'})}\n\n"
                await asyncio.sleep(0.1)

        # --- Check Data Source Capabilities ---
        capabilities = get_data_source_capabilities()
        is_feasible, error_msg = validate_query_feasibility(query.question, capabilities)

        if not is_feasible:
            print(f"⚠️ Query not feasible with current data sources")
            yield f"data: {json.dumps({'status': 'complete', 'message': '⚠️ Data source limitation', 'answer': error_msg, 'sources': [], 'security': security_info if AI_DEFENSE_ENABLED else None})}\n\n"
            return

        # --- Query Classification and Routing ---
        query_intent = classify_query_intent(query.question)
        print(f"🔍 Query classified as: {query_intent} (streaming mode)")

        data_sources = []
        answer = ""

        try:
            # Route based on query intent
            if query_intent == "intersight":
                # Intersight-only query (compute/server data)
                yield f"data: {json.dumps({'status': 'routing', 'message': '🖥️  Routing to Intersight for compute data...'})}\n\n"
                await asyncio.sleep(0.1)

                yield f"data: {json.dumps({'status': 'fetching', 'message': '📡 Fetching server data from Cisco Intersight...'})}\n\n"
                await asyncio.sleep(0.1)

                answer, data_sources = await query_intersight_with_llm(query.question, history_str)

                yield f"data: {json.dumps({'status': 'synthesizing', 'message': '🤖 Generating response...'})}\n\n"
                await asyncio.sleep(0.1)

            elif query_intent == "mcp":
                # MCP-only query (live data)
                yield f"data: {json.dumps({'status': 'routing', 'message': '🔌 Routing to MCP for live data...'})}\n\n"
                await asyncio.sleep(0.1)

                yield f"data: {json.dumps({'status': 'fetching', 'message': '📡 Fetching real-time data from Nexus Dashboard...'})}\n\n"
                await asyncio.sleep(0.1)

                answer, data_sources = await query_mcp_with_llm(query.question, history_str)

                yield f"data: {json.dumps({'status': 'synthesizing', 'message': '🤖 Generating response...'})}\n\n"
                await asyncio.sleep(0.1)

            elif query_intent == "hybrid":
                # Hybrid query (Neo4j + MCP)
                yield f"data: {json.dumps({'status': 'routing', 'message': '🔄 Fetching topology and live data...'})}\n\n"
                await asyncio.sleep(0.1)

                yield f"data: {json.dumps({'status': 'discovering', 'message': '📊 Discovering fabrics and resources...'})}\n\n"
                await asyncio.sleep(0.1)

                answer, data_sources = await query_hybrid(query.question, history_str)

                yield f"data: {json.dumps({'status': 'synthesizing', 'message': '🤖 Aggregating data from multiple sources...'})}\n\n"
                await asyncio.sleep(0.1)

            else:
                # Neo4j-only query (topology/relationships)
                yield f"data: {json.dumps({'status': 'routing', 'message': '📊 Querying network topology...'})}\n\n"
                await asyncio.sleep(0.1)

                # Step 1: Generate Cypher
                yield f"data: {json.dumps({'status': 'generating', 'message': '📝 Generating database query...'})}\n\n"
                await asyncio.sleep(0.1)

                cypher_chain = CYPHER_PROMPT | cypher_llm
                cypher_response = cypher_chain.invoke({
                    "schema": graph.get_schema,
                    "question": query.question,
                    "chat_history": history_str
                })
                raw_cypher = cypher_response.content if hasattr(cypher_response, 'content') else str(cypher_response)
                clean_cypher = clean_cypher_output(raw_cypher)

                # Validate the Cypher query
                is_valid, validation_error = validate_cypher_query(clean_cypher)
                if not is_valid:
                    print(f"❌ Cypher validation failed: {validation_error}")
                    raise ValueError(f"Invalid Cypher query: {validation_error}")

                # Step 2: Execute query
                yield f"data: {json.dumps({'status': 'querying', 'message': '🔎 Querying the knowledge graph...'})}\n\n"
                await asyncio.sleep(0.1)

                query_result = traced_neo4j_query(graph, clean_cypher, "neo4j.cypher.user_query")
                result_count = len(query_result)

                yield f"data: {json.dumps({'status': 'processing', 'message': f'📊 Found {result_count} results, generating response...'})}\n\n"
                await asyncio.sleep(0.1)

                # Step 3: Generate answer with streaming to track section progress
                # Choose template based on query source or if it's an anomaly query
                is_graph_click = query.source == "graph_click"
                is_anomaly = is_anomaly_query(query.question)

                if is_graph_click or is_anomaly:
                    qa_template = QA_TEMPLATE_DETAILED
                    response_msg = '💡 Generating detailed report...' if is_graph_click else '💡 Generating anomaly report with recommended actions...'
                    # Sections for detailed template
                    sections_to_track = [
                        ("## Entity Summary", "📋 Generating Entity Summary..."),
                        ("## Issues Detected", "⚠️ Analyzing Issues Detected..."),
                        ("## Recommended Actions", "🔧 Preparing Recommended Actions..."),
                        ("## Additional Context", "📚 Adding Additional Context..."),
                    ]
                else:
                    qa_template = QA_TEMPLATE_CONCISE
                    response_msg = '💡 Generating response...'
                    # Sections for concise template
                    sections_to_track = [
                        ("## Results", "📊 Presenting Results..."),
                        ("## Recommended Actions", "🔧 Adding Recommendations..."),
                    ]

                yield f"data: {json.dumps({'status': 'responding', 'message': response_msg})}\n\n"
                await asyncio.sleep(0.1)

                # Create streaming QA LLM for section progress
                streaming_kwargs = {
                    "http_client": httpx.Client(verify=False),
                    "http_async_client": httpx.AsyncClient(verify=False),
                    "model_name": local_model_name if LOCAL_LLM_URL else "gpt-4o-mini",
                    "temperature": 0,
                    "streaming": True,
                }
                if LOCAL_LLM_URL:
                    streaming_kwargs["base_url"] = LOCAL_LLM_URL
                    streaming_kwargs["api_key"] = LOCAL_LLM_TOKEN if LOCAL_LLM_TOKEN else "EMPTY"
                streaming_qa_llm = ChatOpenAI(**streaming_kwargs)

                qa_prompt = PromptTemplate.from_template(qa_template)
                qa_chain = qa_prompt | streaming_qa_llm

                # Track which sections we've seen
                detected_sections = set()
                answer_chunks = []
                current_buffer = ""

                # Stream the QA response
                async for chunk in qa_chain.astream({
                    "context": str(query_result),
                    "question": query.question
                }):
                    chunk_text = chunk.content if hasattr(chunk, 'content') else str(chunk)
                    answer_chunks.append(chunk_text)
                    current_buffer += chunk_text

                    # Check for section headers in the accumulated buffer
                    for section_marker, section_message in sections_to_track:
                        if section_marker in current_buffer and section_marker not in detected_sections:
                            detected_sections.add(section_marker)
                            yield f"data: {json.dumps({'status': 'section', 'message': section_message})}\n\n"
                            await asyncio.sleep(0.05)

                answer = "".join(answer_chunks)

                # Track Neo4j source
                data_sources.append(DataSource(
                    type="neo4j",
                    description="Network topology and knowledge graph",
                    details={"query": clean_cypher, "result_count": result_count}
                ))

            # --- Cisco AI Defense: Inspect LLM response ---
            if AI_DEFENSE_ENABLED and AI_DEFENSE_INSPECT_RESPONSES:
                yield f"data: {json.dumps({'status': 'security', 'message': '🛡️ Validating response...'})}\n\n"
                await asyncio.sleep(0.1)

                response_result = inspect_with_ai_defense(answer, role="assistant")
                security_info["response_safe"] = response_result.is_safe

                if response_result.action == "block":
                    security_info["blocked"] = True
                    security_info["warning"] = f"Response blocked: {', '.join(response_result.classifications)}"
                    answer = "I generated a response, but it was blocked by security policies. Please try a different question."
                    yield f"data: {json.dumps({'status': 'blocked', 'message': '🚫 Response blocked by security policy'})}\n\n"
                elif response_result.action == "warn":
                    warn_msg = f"Response notice: {', '.join(response_result.violated_rules)}"
                    if security_info["warning"]:
                        security_info["warning"] += f" | {warn_msg}"
                    else:
                        security_info["warning"] = warn_msg

        except Exception as e:
            error_msg = str(e)
            print(f"❌ Error during query execution: {error_msg}")
            if "CypherSyntaxError" in error_msg or "SyntaxError" in error_msg:
                answer = "I apologize, but I had trouble generating a valid database query for your question. Could you please rephrase your question or be more specific about what information you're looking for?"
            elif "does not appear to be a valid Cypher" in error_msg:
                answer = "I couldn't understand how to query the database for that question. Could you please rephrase it more specifically?"
            else:
                answer = f"I encountered an error while processing your question. Please try rephrasing your question or asking something simpler."

        # Send final response with security info if AI Defense is enabled
        final_response = {'status': 'complete', 'message': '✅ Response complete!', 'answer': answer}
        if AI_DEFENSE_ENABLED:
            final_response['security'] = security_info

        # Add data sources based on query routing (neo4j/mcp/hybrid)
        final_response['sources'] = [source.dict() for source in data_sources]

        yield f"data: {json.dumps(final_response)}\n\n"

    return StreamingResponse(
        generate_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"}
    )


# --- Suggestions Endpoint (called asynchronously by frontend) ---
class SuggestionRequest(BaseModel):
    question: str
    answer: str

class SuggestionResponse(BaseModel):
    suggestions: List[str] = Field(default_factory=list)

@app.post("/suggestions", response_model=SuggestionResponse)
def get_suggestions(request: SuggestionRequest):
    """Generate follow-up suggestions based on question and answer"""
    print(f"🤔 Generating suggestions for: {request.question[:50]}...")

    try:
        # Detect if this is an Intersight/compute query
        question_lower = request.question.lower()
        answer_lower = request.answer.lower()

        is_intersight_query = any(kw in question_lower or kw in answer_lower
                                   for kw in ["server", "ucs", "compute", "blade", "rack",
                                             "chassis", "intersight", "alarm", "cpu", "memory"])

        # Detect if this is a specific anomaly/fault INSTANCE (came from a graph node click
        # and contains a uuid in the question). Generic name-only anomaly questions go
        # through the standard schema prompt; instance questions get the resource-focused
        # prompt below so suggestions probe the affected device/vPC/tenant, not a re-query
        # of the same anomaly name across the fabric.
        anomaly_uuid_match = re.search(r"uuid '([^']+)'", request.question)
        is_anomaly_instance = bool(anomaly_uuid_match) and (
            'anomaly' in question_lower or 'fault' in question_lower
        )

        if is_anomaly_instance:
            # Generate follow-ups that drill into the AFFECTED RESOURCE.
            # Hard constraints: the knowledge graph only models these node types -
            #   Fabric, Tenant, AppProfile, EPG, VRF, BridgeDomain, Subnet, Node
            #   (spine/leaf switches), Anomaly, Fault, Advisory, HealthSummary,
            #   IntersightServer, Endpoint.
            # It does NOT model vPCs, port-channels, interfaces, or VLANs as nodes -
            # those names appear only as strings inside Anomaly.details. So suggestions
            # MUST be queries that the graph can answer (about nodes/fabrics/tenants/etc.),
            # not "tenants of vPC X" (no such relationship).
            anomaly_instance_prompt = PromptTemplate.from_template(
                """The user clicked one specific anomaly/fault on a Cisco ACI topology graph.
Generate three concrete follow-up queries that DRILL INTO THE AFFECTED RESOURCE.

THE KNOWLEDGE GRAPH ONLY HAS THESE NODE TYPES (no others exist):
  Fabric, Tenant, AppProfile, EPG, VRF, BridgeDomain, Subnet, Node (spine/leaf),
  Anomaly, Fault, Advisory, HealthSummary, IntersightServer, Endpoint.
vPCs, port-channels, interfaces, and VLANs are NOT nodes - they only appear as
strings inside Anomaly.details. Do NOT generate queries about "tenants of vPC X" or
"what EPGs use vPC X" - those relationships don't exist in the graph.

HARD RULES:
1. Use ONLY names that literally appear in the Answer text below. Do NOT invent fault
   codes, fabric names, or node names. If the answer says "leaf-101", you may use
   "leaf-101"; do not write "leaf-103".
2. Only generate queries the graph schema can answer. Good shapes:
   - "Show all anomalies on Node '<leaf name>' in fabric '<fabric name>'"
   - "List all faults on Node '<leaf name>'"
   - "What other anomalies exist in fabric '<fabric name>'?"
   - "Show the health summary for fabric '<fabric name>'"
   - "What tenants are affected by anomalies in fabric '<fabric name>'?"
   - "What is the role and status of Node '<leaf name>'?"
3. NEVER suggest "List all fabrics", "Show all anomalies", or any unscoped fleet-wide
   query - those are the initial suggestions, not useful drill-downs.
4. NEVER re-query the same anomaly name (e.g. "status of VPC_DOWN") - the user just
   read that answer.
5. Each suggestion must reference at least one specific name from the Answer.

Question: {question}
Answer: {answer}

Return exactly three queries, one per line, no numbers, no bullets, no quotes around
the entire line.
Follow-up queries:"""
            )
            suggestion_chain = anomaly_instance_prompt | suggestion_llm
            suggestion_response = suggestion_chain.invoke({
                "question": request.question,
                "answer": request.answer
            })
        elif is_intersight_query:
            # Use Intersight-specific prompt
            intersight_prompt = PromptTemplate.from_template(
                """You are a compute infrastructure assistant. Based on the conversation about servers and compute resources, generate three direct follow-up queries that a user might want to ask next.

Focus on compute/server topics like:
- Server health and alarms
- Hardware details (CPU, memory, storage)
- Server configurations and policies
- Network adapters and connectivity
- Chassis and fabric interconnects

Question: {question}
Answer: {answer}

Return only a list of three direct queries, one per line, without numbers or bullets.
Follow-up queries:"""
            )
            suggestion_chain = intersight_prompt | suggestion_llm
            suggestion_response = suggestion_chain.invoke({
                "question": request.question,
                "answer": request.answer
            })
        else:
            # Use network-focused prompt
            suggestion_chain = suggestion_prompt | suggestion_llm
            suggestion_response = suggestion_chain.invoke({
                "question": request.question,
                "answer": request.answer,
                "schema": graph.get_schema
            })
        suggestions = []
        for line in suggestion_response.content.split("\n"):
            processed_line = line.strip()
            if processed_line:
                if processed_line.startswith("- "):
                    processed_line = processed_line[2:]
                elif processed_line.startswith("* "):
                    processed_line = processed_line[2:]
                match = re.match(r"^\d+\.\s*", processed_line)
                if match:
                    processed_line = processed_line[match.end():]
                if processed_line:
                    # Strip enclosing quotes (LLM often wraps suggestions in quotes)
                    if (processed_line.startswith('"') and processed_line.endswith('"')) or \
                       (processed_line.startswith("'") and processed_line.endswith("'")):
                        processed_line = processed_line[1:-1]
                    suggestions.append(processed_line)
        print(f"✅ Generated suggestions: {suggestions}")
        return {"suggestions": suggestions}
    except Exception as e:
        print(f"❌ Error generating suggestions: {e}")
        return {"suggestions": []}

@app.get("/api/health")
def health_check():
    """Health check endpoint for readiness/liveness probes"""
    return {"status": "Network AI Agent API is running"}

# --- Data Source Capabilities Endpoint ---
@app.get("/api/capabilities")
def get_capabilities():
    """
    Return available data sources and query capabilities.
    Used by frontend to adapt UI and suggestions.
    """
    capabilities = get_data_source_capabilities()

    return {
        "data_sources": {
            "apic": {
                "available": capabilities['apic_available'],
                "provides": "ACI Policy Model (Tenants, EPGs, Contracts, VRFs)",
                "query_types": ["topology", "policy", "configuration"]
            },
            "nexus_dashboard": {
                "available": capabilities['nd_available'],
                "provides": "Operational Data (Health, Metrics, Anomalies, Compliance)",
                "query_types": ["health", "metrics", "anomalies", "compliance"]
            },
            "intersight": {
                "available": capabilities['intersight_available'],
                "provides": "Compute/Server Data (UCS, Health, Telemetry)",
                "query_types": ["server", "compute", "health", "telemetry"]
            }
        },
        "capabilities": {
            "policy_queries": capabilities['policy_model'],
            "live_metrics": capabilities['live_metrics'],
            "fabric_topology": capabilities['fabric_topology'],
            "full_topology": capabilities['full_topology']
        },
        "mcp_health": {
            "nd_mcp": capabilities['mcp_health']['nd_mcp'],
            "intersight_mcp": capabilities['mcp_health']['intersight_mcp']
        },
        "suggested_queries": {
            "always_available": [
                "List all fabrics",
                "Show network overview"
            ],
            "with_apic": [
                "Show all tenants",
                "List EPGs in tenant X",
                "Show application profiles"
            ] if capabilities['apic_available'] else [],
            "with_nd": [
                "What is the current health status?",
                "Show me anomalies",
                "Check compliance status"
            ] if capabilities['nd_available'] else [],
            "with_intersight": [
                "Show all servers",
                "What is the health of server X?",
                "List compute resources"
            ] if capabilities['intersight_available'] else []
        },
        "mode": "full" if capabilities['full_topology']
                else ("apic_only" if capabilities['apic_available']
                else ("nd_only" if capabilities['nd_available']
                else "degraded"))
    }

# --- AI Defense Status Endpoint ---
@app.get("/ai-defense/status")
def ai_defense_status():
    """Check AI Defense integration status"""
    return {
        "enabled": AI_DEFENSE_ENABLED,
        "endpoint": AI_DEFENSE_ENDPOINT if AI_DEFENSE_ENABLED else None,
        "inspect_prompts": AI_DEFENSE_INSPECT_PROMPTS if AI_DEFENSE_ENABLED else False,
        "inspect_responses": AI_DEFENSE_INSPECT_RESPONSES if AI_DEFENSE_ENABLED else False,
        "use_policy": AI_DEFENSE_USE_POLICY if AI_DEFENSE_ENABLED else False,
        "mode": "dashboard_policy" if AI_DEFENSE_USE_POLICY else "inline_rules",
        "rules_count": len(AI_DEFENSE_RULES) if (AI_DEFENSE_ENABLED and not AI_DEFENSE_USE_POLICY) else 0
    }

class AIDefenseTestRequest(BaseModel):
    content: str
    role: str = "user"  # "user" or "assistant"

@app.post("/ai-defense/test")
def test_ai_defense(request: AIDefenseTestRequest):
    """Test content against AI Defense policies"""
    if not AI_DEFENSE_ENABLED:
        return {"error": "AI Defense is not enabled", "enabled": False}

    result = inspect_with_ai_defense(request.content, request.role)
    return {
        "enabled": True,
        "is_safe": result.is_safe,
        "severity": result.severity,
        "classifications": result.classifications,
        "violated_rules": result.violated_rules,
        "explanation": result.explanation,
        "attack_technique": result.attack_technique,
        "action": result.action
    }

# --- MCP Status and Tools Endpoints ---
@app.get("/mcp/status")
def mcp_status():
    """Check MCP integration status"""
    mcp = get_mcp_client()

    if not MCP_ENABLED:
        return {
            "enabled": False,
            "connected": False,
            "message": "MCP integration is disabled"
        }

    if not mcp:
        return {
            "enabled": True,
            "connected": False,
            "message": "MCP client not initialized"
        }

    categories = mcp.get_tool_categories()
    read_only_tools = mcp.get_read_only_tools()

    return {
        "enabled": True,
        "connected": mcp.connected,
        "server_url": MCP_SERVER_URL,
        "tools_total": len(mcp.tools),
        "tools_read_only": len(read_only_tools),
        "tools_write": len(mcp.tools) - len(read_only_tools),
        "categories": {
            "insights": len(categories["insights"]),
            "manage": len(categories["manage"]),
            "infrastructure": len(categories["infrastructure"]),
            "onemanage": len(categories["onemanage"])
        },
        "last_updated": mcp.tools_last_updated.isoformat() if mcp.tools_last_updated else None
    }


@app.get("/mcp/tools")
def mcp_list_tools(read_only: bool = True, category: Optional[str] = None):
    """
    List available MCP tools

    Args:
        read_only: Only return read-only (GET) operations (default: True)
        category: Filter by category (insights, manage, infrastructure, onemanage)
    """
    mcp = get_mcp_client()

    if not mcp or not mcp.connected:
        raise HTTPException(status_code=503, detail="MCP client not available")

    # Get all tools
    if category:
        categories = mcp.get_tool_categories()
        tool_names = categories.get(category.lower(), [])
        tools = [
            {
                "name": name,
                "description": mcp.tools[name].get("description", ""),
                "read_only": mcp.tools[name].get("read_only", False),
                "method": mcp.tools[name].get("method", "UNKNOWN")
            }
            for name in tool_names
            if name in mcp.tools and (not read_only or mcp.tools[name].get("read_only", False))
        ]
    else:
        tools = [
            {
                "name": name,
                "description": info.get("description", ""),
                "read_only": info.get("read_only", False),
                "method": info.get("method", "UNKNOWN")
            }
            for name, info in mcp.tools.items()
            if not read_only or info.get("read_only", False)
        ]

    return {
        "count": len(tools),
        "tools": tools,
        "filters": {
            "read_only": read_only,
            "category": category
        }
    }


@app.get("/mcp/tools/{tool_name}")
def mcp_get_tool(tool_name: str):
    """Get detailed information about a specific MCP tool"""
    mcp = get_mcp_client()

    if not mcp or not mcp.connected:
        raise HTTPException(status_code=503, detail="MCP client not available")

    if tool_name not in mcp.tools:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")

    tool_info = mcp.tools[tool_name]
    return {
        "name": tool_name,
        "description": tool_info.get("description", ""),
        "method": tool_info.get("method", "UNKNOWN"),
        "read_only": tool_info.get("read_only", False),
        "parameters": tool_info.get("parameters", {}),
        "path": tool_info.get("path", "")
    }


class MCPToolCallRequest(BaseModel):
    tool: str
    arguments: Dict[str, Any] = Field(default_factory=dict)
    timeout: Optional[int] = None


@app.post("/mcp/execute")
async def mcp_execute_tool(request: MCPToolCallRequest):
    """
    Execute an MCP tool (admin only - for testing)

    This endpoint allows direct tool execution for testing purposes.
    In production, tools should be called via LLM agent.
    """
    mcp = get_mcp_client()

    if not mcp or not mcp.connected:
        raise HTTPException(status_code=503, detail="MCP client not available")

    try:
        result = await mcp.call_tool(
            tool_name=request.tool,
            arguments=request.arguments,
            timeout=request.timeout
        )
        return {
            "success": True,
            "tool": request.tool,
            "result": result
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Tool execution failed: {str(e)}")


@app.post("/mcp/refresh")
async def mcp_refresh_tools():
    """Force refresh MCP tool discovery"""
    mcp = get_mcp_client()

    if not mcp:
        raise HTTPException(status_code=503, detail="MCP client not available")

    try:
        await mcp.discover_tools(force_refresh=True)
        return {
            "success": True,
            "tools_count": len(mcp.tools),
            "updated_at": mcp.tools_last_updated.isoformat() if mcp.tools_last_updated else None
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Tool refresh failed: {str(e)}")


def sanitize_properties(props: dict) -> dict:
    """Convert non-serializable Neo4j types to strings"""
    sanitized = {}
    for key, value in props.items():
        if hasattr(value, 'isoformat'):  # Handle DateTime, Date, Time objects
            sanitized[key] = value.isoformat()
        elif isinstance(value, (list, tuple)):
            sanitized[key] = [str(v) if hasattr(v, 'isoformat') else v for v in value]
        else:
            sanitized[key] = value
    return sanitized

@app.get("/api/graph", response_model=GraphResponse)
def get_graph_data():
    """Return all nodes and relationships for visualization"""

    # Query to get all nodes and relationships, including parent tenant for context
    query = """
    MATCH (n)
    OPTIONAL MATCH (n)-[r]->(m)
    OPTIONAL MATCH (tenant:Tenant)-[:HAS_AP|HAS_EPG|HAS_VRF|HAS_BD*1..3]->(n)
    RETURN
        id(n) AS source_id,
        labels(n) AS source_labels,
        properties(n) AS source_props,
        tenant.name AS parent_tenant,
        type(r) AS rel_type,
        id(m) AS target_id,
        labels(m) AS target_labels,
        properties(m) AS target_props
    """

    result = traced_neo4j_query(graph, query, "neo4j.cypher.graph_visualization")

    nodes_dict = {}  # Use dict to avoid duplicates
    edges = []

    for record in result:
        # Process source node
        source_id = str(record["source_id"])
        if source_id not in nodes_dict:
            source_labels = record["source_labels"] or []
            source_props = sanitize_properties(record["source_props"] or {})
            node_type = source_labels[0] if source_labels else "Unknown"

            # Add parent tenant to properties if available
            if record.get("parent_tenant"):
                source_props["_parent_tenant"] = record["parent_tenant"]

            # Special handling for nodes without 'name' property
            if node_type == "Subnet":
                # Subnets use 'ip' property for display
                node_label = source_props.get("ip", f"Subnet-{source_id}")
            else:
                node_label = source_props.get("name", source_props.get("dn", f"Node-{source_id}"))

            nodes_dict[source_id] = GraphNode(
                id=source_id,
                label=node_label,
                type=node_type,
                properties=source_props
            )

        # Process target node and relationship if exists
        if record["target_id"] is not None:
            target_id = str(record["target_id"])
            if target_id not in nodes_dict:
                target_labels = record["target_labels"] or []
                target_props = sanitize_properties(record["target_props"] or {})
                node_type = target_labels[0] if target_labels else "Unknown"

                # Special handling for nodes without 'name' property
                if node_type == "Subnet":
                    # Subnets use 'ip' property for display
                    node_label = target_props.get("ip", f"Subnet-{target_id}")
                else:
                    node_label = target_props.get("name", target_props.get("dn", f"Node-{target_id}"))

                nodes_dict[target_id] = GraphNode(
                    id=target_id,
                    label=node_label,
                    type=node_type,
                    properties=target_props
                )

            # Add edge
            if record["rel_type"]:
                edges.append(GraphEdge(
                    source=source_id,
                    target=target_id,
                    type=record["rel_type"]
                ))

    print(f"📊 Returning graph with {len(nodes_dict)} nodes and {len(edges)} edges")
    return GraphResponse(nodes=list(nodes_dict.values()), edges=edges)


# --- Model Proxy Endpoint for AI Defense Validation ---
# This endpoint provides direct access to the underlying LLM for security validation testing

model_proxy_api_key_header = APIKeyHeader(name="X-Model-Proxy-API-Key", auto_error=False)

async def verify_model_proxy_api_key(api_key: str = Security(model_proxy_api_key_header)):
    """Verify the API key for model proxy access"""
    if not MODEL_PROXY_ENABLED:
        raise HTTPException(status_code=404, detail="Model proxy is not enabled")
    if not MODEL_PROXY_API_KEY:
        raise HTTPException(status_code=500, detail="Model proxy API key not configured")
    if api_key != MODEL_PROXY_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return api_key

class ModelProxyChatMessage(BaseModel):
    role: str  # "user", "assistant", or "system"
    content: str

class ModelProxyChatRequest(BaseModel):
    model: Optional[str] = None  # Optional, will use configured model if not provided
    messages: List[ModelProxyChatMessage]
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = 1024
    stream: Optional[bool] = False

@app.get("/model/status")
def model_proxy_status():
    """Check model proxy status"""
    return {
        "enabled": MODEL_PROXY_ENABLED,
        "model_url": LOCAL_LLM_URL if MODEL_PROXY_ENABLED else None,
        "model_name": "mistral-nemo-12b" if LOCAL_LLM_URL else None,
        "requires_auth": bool(MODEL_PROXY_API_KEY)
    }

@app.post("/model/chat/completions")
async def model_proxy_chat(
    request: ModelProxyChatRequest,
    api_key: str = Depends(verify_model_proxy_api_key)
):
    """
    Proxy endpoint for AI Defense Validation.
    Routes chat completion requests directly to the underlying vLLM model.

    This endpoint bypasses all GBAIA logic (no RAG, no Neo4j) and provides
    direct access to the model for security validation testing.
    """
    if not LOCAL_LLM_URL:
        raise HTTPException(status_code=500, detail="Local LLM URL not configured")

    # Build the request for vLLM
    vllm_url = f"{LOCAL_LLM_URL}/chat/completions"
    model_name = request.model or "mistral-nemo-12b"

    payload = {
        "model": model_name,
        "messages": [{"role": m.role, "content": m.content} for m in request.messages],
        "temperature": request.temperature,
        "max_tokens": request.max_tokens,
        "stream": request.stream
    }

    headers = {
        "Content-Type": "application/json"
    }
    if LOCAL_LLM_TOKEN:
        headers["Authorization"] = f"Bearer {LOCAL_LLM_TOKEN}"

    try:
        async with httpx.AsyncClient(verify=False, timeout=60.0) as client:
            response = await client.post(vllm_url, json=payload, headers=headers)
            response.raise_for_status()
            return response.json()
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Model request timed out")
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=f"Model error: {e.response.text}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Proxy error: {str(e)}")

# Log model proxy status on startup
if MODEL_PROXY_ENABLED:
    if MODEL_PROXY_API_KEY:
        print(f"🔌 Model Proxy enabled - direct LLM access at /model/chat/completions (API key required)")
    else:
        print("⚠️ Model Proxy enabled but MODEL_PROXY_API_KEY not set - endpoint disabled")
        MODEL_PROXY_ENABLED = False
else:
    print("🔌 Model Proxy disabled (set MODEL_PROXY_ENABLED=true to enable)")


# =============================================================================
# Configuration Management Endpoints
# =============================================================================

@app.get("/api/config")
async def get_config() -> Dict[str, Any]:
    """Get all runtime configuration values."""
    return get_runtime_config().get_all()


@app.post("/api/config")
async def update_config(changes: Dict[str, Any]) -> Dict[str, Any]:
    """Update runtime configuration values.

    Returns:
        {
            "config": {...},  # Updated configuration
            "results": {"field": "updated" or "error message"}
        }
    """
    results = get_runtime_config().update(changes)
    return {
        "config": get_runtime_config().get_all(),
        "results": results
    }


@app.post("/api/config/models")
async def discover_models_endpoint(request: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Discover available models from a provider.

    Request body:
        {
            "provider": "openai" | "anthropic" | "vllm" | "ollama",
            "base_url": "https://api.openai.com/v1" (optional),
            "api_key": "..." (optional)
        }

    Returns:
        List of model dicts with id, name, context, and optional pricing.
    """
    from models_catalog import discover_models

    provider = request.get("provider")
    base_url = request.get("base_url")
    api_key = request.get("api_key")

    if not provider:
        return []

    return await discover_models(provider, base_url, api_key)


@app.post("/api/verify/{component}")
async def verify_component(component: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """Verify connectivity and configuration for a component.

    Components: llm, llm-alt, apic, nd, neo4j, splunk, ai-defense
    """

    if component == "llm":
        return await verify_llm(config)
    elif component == "llm-alt":
        return await verify_llm_alt(config)
    elif component == "apic":
        return await verify_apic(config)
    elif component == "nd":
        return await verify_nd(config)
    elif component == "neo4j":
        return await verify_neo4j_connection(config)
    elif component == "splunk":
        return await verify_splunk(config)
    elif component == "ai-defense":
        return await verify_ai_defense(config)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown component: {component}")


async def verify_llm(config: Dict[str, Any]) -> Dict[str, Any]:
    """Verify LLM connection and list available models."""
    provider = config.get("llm_provider", "openai")
    base_url = config.get("llm_base_url", "")
    api_key = config.get("llm_api_key", "")
    model = config.get("llm_model", "")

    if not base_url or not api_key:
        return {
            "status": "fail",
            "message": "Missing base URL or API key",
            "tests": []
        }

    try:
        async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
            # Test models endpoint
            headers = {"Authorization": f"Bearer {api_key}"}
            models_url = f"{base_url}/models" if not base_url.endswith('/models') else base_url

            response = await client.get(models_url, headers=headers)
            response.raise_for_status()

            data = response.json()
            models = data.get("data", [])
            model_ids = [m.get("id") for m in models]

            # Check if specified model exists
            model_exists = model in model_ids if model else True

            return {
                "status": "success",
                "message": f"Connected to {provider} - {len(models)} models available",
                "tests": [
                    {
                        "name": "API Connection",
                        "status": "pass",
                        "message": f"Successfully connected to {base_url}"
                    },
                    {
                        "name": "Model Availability",
                        "status": "pass" if model_exists else "warning",
                        "message": f"Model '{model}' {'found' if model_exists else 'not found'}"
                    }
                ]
            }
    except httpx.HTTPStatusError as e:
        return {
            "status": "fail",
            "message": f"HTTP Error: {e.response.status_code}",
            "tests": [
                {
                    "name": "API Connection",
                    "status": "fail",
                    "message": str(e)
                }
            ]
        }
    except Exception as e:
        return {
            "status": "fail",
            "message": f"Connection failed: {str(e)}",
            "tests": []
        }


async def verify_llm_alt(config: Dict[str, Any]) -> Dict[str, Any]:
    """Verify alternative LLM connection."""
    if not config.get("llm_alt_enabled", False):
        return {
            "status": "warning",
            "message": "Alternative LLM is disabled",
            "tests": []
        }

    # Create a temporary config dict with alt fields mapped to primary fields
    alt_config = {
        "llm_provider": config.get("llm_alt_provider", ""),
        "llm_base_url": config.get("llm_alt_base_url", ""),
        "llm_api_key": config.get("llm_alt_api_key", ""),
        "llm_model": config.get("llm_alt_model", ""),
    }

    return await verify_llm(alt_config)


async def verify_apic(config: Dict[str, Any]) -> Dict[str, Any]:
    """Verify Cisco APIC connection."""
    apic_url = config.get("apic_url", "")
    apic_user = config.get("apic_user", "")
    apic_password = config.get("apic_password", "")

    if not all([apic_url, apic_user, apic_password]):
        return {
            "status": "fail",
            "message": "Missing APIC credentials",
            "tests": []
        }

    try:
        async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
            login_url = f"{apic_url}/api/aaaLogin.json"
            payload = {
                "aaaUser": {
                    "attributes": {
                        "name": apic_user,
                        "pwd": apic_password
                    }
                }
            }

            response = await client.post(login_url, json=payload)
            response.raise_for_status()

            data = response.json()
            token = data.get("imdata", [{}])[0].get("aaaLogin", {}).get("attributes", {}).get("token")

            return {
                "status": "success",
                "message": "Successfully authenticated to APIC",
                "tests": [
                    {
                        "name": "Authentication",
                        "status": "pass",
                        "message": "Login successful, token received"
                    }
                ]
            }
    except Exception as e:
        return {
            "status": "fail",
            "message": f"APIC connection failed: {str(e)}",
            "tests": []
        }


async def verify_nd(config: Dict[str, Any]) -> Dict[str, Any]:
    """Verify Nexus Dashboard connection."""
    nd_url = config.get("nd_url", "")
    nd_user = config.get("nd_user", "")
    nd_password = config.get("nd_password", "")

    if not all([nd_url, nd_user, nd_password]):
        return {
            "status": "fail",
            "message": "Missing Nexus Dashboard credentials",
            "tests": []
        }

    try:
        async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
            login_url = f"{nd_url}/login"
            payload = {"userName": nd_user, "userPasswd": nd_password, "domain": "local"}

            response = await client.post(login_url, json=payload)
            response.raise_for_status()

            return {
                "status": "success",
                "message": "Successfully authenticated to Nexus Dashboard",
                "tests": [
                    {
                        "name": "Authentication",
                        "status": "pass",
                        "message": "Login successful"
                    }
                ]
            }
    except Exception as e:
        return {
            "status": "fail",
            "message": f"Nexus Dashboard connection failed: {str(e)}",
            "tests": []
        }


async def verify_neo4j_connection(config: Dict[str, Any]) -> Dict[str, Any]:
    """Verify Neo4j connection."""
    from neo4j import GraphDatabase

    neo4j_uri = config.get("neo4j_uri", "")
    neo4j_user = config.get("neo4j_user", "")
    neo4j_password = config.get("neo4j_password", "")

    if not all([neo4j_uri, neo4j_user, neo4j_password]):
        return {
            "status": "fail",
            "message": "Missing Neo4j credentials",
            "tests": []
        }

    try:
        driver = GraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_password))

        # Test connection with a simple query
        with driver.session() as session:
            result = session.run("MATCH (n) RETURN count(n) as count")
            record = result.single()
            node_count = record["count"] if record else 0

        driver.close()

        return {
            "status": "success",
            "message": f"Connected to Neo4j - {node_count} nodes in database",
            "tests": [
                {
                    "name": "Connection",
                    "status": "pass",
                    "message": "Successfully connected to Neo4j"
                },
                {
                    "name": "Query Test",
                    "status": "pass",
                    "message": f"Database contains {node_count} nodes"
                }
            ]
        }
    except Exception as e:
        return {
            "status": "fail",
            "message": f"Neo4j connection failed: {str(e)}",
            "tests": []
        }


async def verify_splunk(config: Dict[str, Any]) -> Dict[str, Any]:
    """Verify Splunk OTel collector connection."""
    if not config.get("splunk_enabled", False):
        return {
            "status": "warning",
            "message": "Splunk tracing is disabled",
            "tests": []
        }

    otel_endpoint = config.get("otel_endpoint", "")

    if not otel_endpoint:
        return {
            "status": "fail",
            "message": "Missing OTel endpoint",
            "tests": []
        }

    # For OTel gRPC endpoint, we just check if it's reachable
    # Full verification would require sending a test span
    return {
        "status": "success",
        "message": f"OTel endpoint configured: {otel_endpoint}",
        "tests": [
            {
                "name": "Configuration",
                "status": "pass",
                "message": "OTel endpoint is set"
            }
        ]
    }


async def verify_ai_defense(config: Dict[str, Any]) -> Dict[str, Any]:
    """Verify Cisco AI Defense connection."""
    if not config.get("ai_defense_enabled", False):
        return {
            "status": "warning",
            "message": "AI Defense is disabled",
            "tests": []
        }

    api_key = config.get("ai_defense_api_key", "")
    endpoint = config.get("ai_defense_endpoint", "")

    if not api_key or not endpoint:
        return {
            "status": "fail",
            "message": "Missing AI Defense credentials",
            "tests": []
        }

    try:
        async with httpx.AsyncClient(verify=True, timeout=10.0) as client:
            # Test with a simple prompt
            url = f"https://{endpoint}/api/v1/inspect"
            headers = {
                "X-API-Key": api_key,
                "Content-Type": "application/json"
            }
            payload = {
                "content": "Hello, this is a test.",
                "rules": [{"rule_name": "PII"}]
            }

            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()

            return {
                "status": "success",
                "message": "Successfully connected to AI Defense",
                "tests": [
                    {
                        "name": "API Connection",
                        "status": "pass",
                        "message": "Inspection API is reachable"
                    }
                ]
            }
    except Exception as e:
        return {
            "status": "fail",
            "message": f"AI Defense connection failed: {str(e)}",
            "tests": []
        }

