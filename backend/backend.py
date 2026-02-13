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

import os, re, httpx, json, asyncio
from fastapi import FastAPI, HTTPException, Security, Depends
from fastapi.responses import StreamingResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field
from typing import List, AsyncGenerator, Optional

from langchain_openai import ChatOpenAI
from langchain_classic.chains import GraphCypherQAChain
from langchain_community.graphs import Neo4jGraph
from langchain_core.prompts import PromptTemplate

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

class Response(BaseModel):
    answer: str
    suggestions: List[str] = Field(default_factory=list)
    security: Optional[SecurityInfo] = None

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
    # GPT-5 models don't support the temperature parameter (only default value of 1 is allowed)
    # See: https://community.openai.com/t/temperature-in-gpt-5-models/1337133
    print("✅ Using OpenAI models (LOCAL_LLM_URL not set).")
    cypher_llm = ChatOpenAI(model="gpt-5-mini")
    qa_llm = ChatOpenAI(model="gpt-5-mini")
    suggestion_llm = ChatOpenAI(model="gpt-5-mini")


# --- Define the Prompt Templates ---
CYPHER_GENERATION_TEMPLATE = """Generate a Cypher query. Output ONLY the query, no markdown.

RULE: Every variable in RETURN must be defined in MATCH or OPTIONAL MATCH first!

KEY RELATIONSHIPS:
- Node -[:BELONGS_TO]-> Fabric (nodes belong to fabrics)
- Node -[:HAS_FAULT]-> Fault (nodes have faults with severity: warning, minor, major, critical)
- Fabric -[:HAS_ANOMALY]-> Anomaly (fabrics have anomalies with severity: warning, major, critical)
- Fabric -[:HAS_ADVISORY]-> Advisory (fabrics have advisories)
- Tenant -[:BELONGS_TO]-> Fabric (tenants belong to fabrics)
- Tenant -[:HAS_AP]-> AppProfile (tenants have application profiles)
- AppProfile -[:HAS_EPG]-> EPG (application profiles have EPGs)
- Tenant -[:HAS_VRF]-> VRF (tenants have VRFs)
- Tenant -[:HAS_BD]-> BridgeDomain (tenants have bridge domains)

IMPORTANT: Severity values are ALWAYS lowercase: 'critical', 'major', 'warning', 'minor'

EXAMPLE 1 - Nodes with warning faults in a fabric:
Question: "List nodes in fabric 'ams-aci' with warning faults"
MATCH (n:Node)-[:BELONGS_TO]->(f:Fabric) WHERE f.name = 'ams-aci'
MATCH (n)-[:HAS_FAULT]->(fault:Fault) WHERE fault.severity = 'warning'
RETURN n.name AS node_name, fault.code AS fault_code, fault.descr AS description

EXAMPLE 2 - List ALL anomalies in a fabric with full details:
Question: "List all anomalies in fabric 'ams-aci'" or "What anomalies are in fabric 'ams-aci'?"
MATCH (f:Fabric)-[:HAS_ANOMALY]->(a:Anomaly) WHERE f.name = 'ams-aci'
RETURN a.name AS anomaly_name, a.severity, a.lastSeen
ORDER BY a.severity

EXAMPLE 3 - All faults for a specific node:
Question: "Show faults for node 'leaf-104'"
MATCH (n:Node)-[:HAS_FAULT]->(f:Fault) WHERE n.name = 'leaf-104'
RETURN n.name AS node, f.code, f.severity, f.descr AS description, f.cause

EXAMPLE 4 - Anomalies with specific severity in a fabric:
Question: "List critical anomalies in fabric 'ams-aci'"
MATCH (f:Fabric)-[:HAS_ANOMALY]->(a:Anomaly)
WHERE f.name = 'ams-aci' AND a.severity = 'critical'
RETURN a.name AS anomaly_name, a.severity, a.lastSeen

EXAMPLE 5 - Fabric status overview:
Question: "What is the status of fabric 'ams-aci'?"
MATCH (f:Fabric) WHERE f.name = 'ams-aci'
OPTIONAL MATCH (f)-[:HAS_ANOMALY]->(a:Anomaly)
RETURN f.name AS fabric_name, count(a) AS anomaly_count, collect(a.name) AS anomaly_names, collect(a.severity) AS severities

EXAMPLE 6 - Tenant with all related entities:
Question: "Tell me about tenant 'example-tenant'"
MATCH (t:Tenant) WHERE t.name = 'example-tenant'
OPTIONAL MATCH (t)-[:HAS_AP]->(ap:AppProfile)
OPTIONAL MATCH (ap)-[:HAS_EPG]->(epg:EPG)
OPTIONAL MATCH (t)-[:HAS_VRF]->(vrf:VRF)
OPTIONAL MATCH (t)-[:HAS_BD]->(bd:BridgeDomain)
RETURN t.name AS tenant, collect(DISTINCT ap.name) AS app_profiles, collect(DISTINCT epg.name) AS epgs, collect(DISTINCT vrf.name) AS vrfs, collect(DISTINCT bd.name) AS bridge_domains

EXAMPLE 7 - Tenants in a fabric:
Question: "What tenants exist in fabric 'ams-aci'?"
MATCH (t:Tenant)-[:BELONGS_TO]->(f:Fabric) WHERE f.name = 'ams-aci'
RETURN t.name AS tenant_name

Schema:
{schema}

Chat History (use this to understand follow-up questions):
{chat_history}

Current Question: {question}

If the question refers to something from the chat history (like "the tenant mentioned above" or "that application profile"), extract the specific name from the history and use it in your query.

Cypher:"""

CYPHER_PROMPT = PromptTemplate.from_template(CYPHER_GENERATION_TEMPLATE)

# Concise template for direct user queries and suggestions - focus on actual data
QA_TEMPLATE_CONCISE = """You are an expert Cisco ACI network operations assistant. Answer based on the query results provided.

CRITICAL ACCURACY RULES:
1. Base ALL facts, names, counts, and severities ONLY on the Query Results below
2. If the results show an empty list [] or null/None, explicitly state "none found" or "0" - NEVER invent items
3. Count items exactly as they appear - if you see ["item1"], that's exactly 1 item
4. You MAY provide brief helpful context for items that DO exist in the results

FORBIDDEN:
- Do NOT invent names, counts, or details not present in the results
- Do NOT fabricate issues or items when results show empty []
- Do NOT make up example data or placeholders

EXAMPLES OF CORRECT RESPONSES:
- Results show app_profiles: ["talos"], epgs: [] → "1 application profile (talos), no EPGs configured"
- Results show anomalies with names/severities → List the actual anomalies found
- Results show empty [] for something asked about → "No [items] found for this entity"

Query Results:
{context}

Question:
{question}

Answer:
"""

# Detailed template for graph node clicks - full report with context
QA_TEMPLATE_DETAILED = """You are an expert Cisco ACI network operations assistant. Provide a comprehensive report.

CRITICAL ACCURACY RULES:
1. Base ALL facts, names, counts, and severities ONLY on the Query Results below
2. If the results show an empty list [] or null/None for something, explicitly state "none" or "0" - NEVER invent items
3. Count items exactly as they appear in arrays - if you see ["item1", "item2"], that's exactly 2 items
4. ONLY report anomaly/fault severities if they are EXPLICITLY shown in the results (e.g., "severity": "critical")
5. You MAY provide helpful explanations for items that DO exist in the results
6. You MAY suggest remediation steps for actual issues found in the results

FORBIDDEN - NEVER DO THESE:
- Do NOT invent entity names, counts, or severities not explicitly in the results
- Do NOT guess or infer severity from anomaly names - only use severity if it's in the data
- Do NOT fabricate issues or anomalies that aren't in the results
- Do NOT add anomalies that are not listed in the Query Results
- Do NOT skip or omit anomalies that ARE in the Query Results - list ALL of them

Format your response:

## Entity Summary
Describe the queried entity based on the Query Results:
- List all properties shown (name, status, type, etc.)
- State exact counts from the data (count the rows/items in the results)
- Explain the entity's role in the network architecture
- If a field shows [] or null, state "0" or "none" for that item

## Issues Detected
IMPORTANT: You MUST list EVERY anomaly/fault from the Query Results. Count the rows in the results and ensure your list has the same count. Do not summarize or skip any.

For EACH row in the Query Results that contains an anomaly or fault:
- **[Severity] NAME** - Brief explanation of what this issue means and its impact
- If severity is not shown, write **[Unknown] NAME**

Verify: If Query Results show 10 anomaly rows, you must list all 10. If results show 7, list all 7.

If NO anomalies/faults appear in the results, state: "No issues found in the database for this entity"

## Recommended Actions
For issues ACTUALLY FOUND in the results above, provide:

**Critical (fix immediately):**
- Specific troubleshooting steps for the critical issues listed

**Major (fix soon):**
- Remediation guidance for major issues listed

**Warning (monitor/review):**
- What to investigate for warning issues listed

If no issues were found, state: "No actions required - no issues detected"

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

    return result

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

IMPORTANT: Write each suggestion as a direct command or question that will be sent to the AI, NOT as a question asking the user what they want.

Good examples:
- "List all EPGs under tenant 'example'"
- "Show the anomalies affecting fabric 'ams-aci'"
- "What is the severity of the INTERFACE_DOWN anomaly?"

Bad examples (do NOT use these formats):
- "Do you want me to list the EPGs?"
- "Should I check the anomalies?"
- "Would you like to see more details?"

The network graph has the following schema: {schema}

Question: {question}
Answer: {answer}

Return only a list of three direct queries, one per line, without numbers or bullets.
Follow-up queries:"""
)

# --- API Endpoint ---
@app.post("/ask", response_model=Response)
def ask_agent(query: Query):
    # Convert Gradio's history format to a simple string for the prompt
    history_str = "\n".join([f"Human: {q}\nAssistant: {a}" for q, a in query.chat_history])

    print(f"🤖 Received question: {query.question} with history: {history_str}")

    # Initialize security info
    security_info = SecurityInfo()

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
                security=security_info
            )
        elif prompt_result.action == "warn":
            security_info.warning = f"⚠️ Security Notice: Potential concerns detected in your question ({prompt_result.severity}): {', '.join(prompt_result.violated_rules)}"
            print(f"⚠️ Prompt warning from AI Defense: {prompt_result.violated_rules}")

    try:
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

        # Step 3: Execute the Cypher query
        if not clean_cypher or not clean_cypher.strip().upper().startswith(('MATCH', 'OPTIONAL', 'WITH', 'CALL', 'RETURN')):
            raise ValueError("Generated text does not appear to be a valid Cypher query")

        query_result = traced_neo4j_query(graph, clean_cypher, "neo4j.cypher.user_query")
        print(f"📊 Query returned {len(query_result)} results")

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

    # Return answer with security info if AI Defense is enabled
    return Response(
        answer=answer,
        suggestions=[],
        security=security_info if AI_DEFENSE_ENABLED else None
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

        try:
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

            if not clean_cypher or not clean_cypher.strip().upper().startswith(('MATCH', 'OPTIONAL', 'WITH', 'CALL', 'RETURN')):
                raise ValueError("Generated text does not appear to be a valid Cypher query")

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
            streaming_qa_llm = ChatOpenAI(
                http_client=httpx.Client(verify=False) if LOCAL_LLM_URL else None,
                base_url=LOCAL_LLM_URL if LOCAL_LLM_URL else None,
                api_key=(LOCAL_LLM_TOKEN if LOCAL_LLM_TOKEN else "EMPTY") if LOCAL_LLM_URL else None,
                model_name=local_model_name if LOCAL_LLM_URL else "gpt-5-mini",
                temperature=0,
                streaming=True
            )

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
                    suggestions.append(processed_line)
        print(f"✅ Generated suggestions: {suggestions}")
        return {"suggestions": suggestions}
    except Exception as e:
        print(f"❌ Error generating suggestions: {e}")
        return {"suggestions": []}

@app.get("/")
def read_root():
    return {"status": "Network AI Agent API is running"}

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

@app.get("/graph", response_model=GraphResponse)
def get_graph_data():
    """Return all nodes and relationships for visualization"""

    # Query to get all nodes and relationships
    query = """
    MATCH (n)
    OPTIONAL MATCH (n)-[r]->(m)
    RETURN
        id(n) AS source_id,
        labels(n) AS source_labels,
        properties(n) AS source_props,
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