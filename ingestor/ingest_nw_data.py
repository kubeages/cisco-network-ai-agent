"""
================================================================================
Network Knowledge Graph Ingestor
================================================================================

This module provides a standalone, recurring data ingestion service for building
and maintaining a network knowledge graph in a Neo4j database.

Purpose:
--------
The primary goal of this service is to create a unified and correlated view of
a network's configuration and operational state. It acts as the "Collector"
for the AI Agent, providing the foundational "Memory" of the system.

Data Sources:
-------------
1.  Cisco APIC: For logical configuration data, including Tenants, Application
    Profiles (APs), and End-Point Groups (EPGs). This forms the structural
    "map" of the network.
2.  Cisco Nexus Dashboard: For operational state data, including the list of
    managed fabrics and detailed anomaly information affecting specific tenants.

Key Functionality:
------------------
- Recurring Execution: Uses the 'schedule' library to run the full
  ingestion job at a defined interval (e.g., every 5 minutes).
- Authentication: Handles token-based authentication for both APIC and
  Nexus Dashboard APIs.
- Data Correlation: Enriches the graph by linking APIC configuration objects
  (like Tenants) with operational data from Nexus Dashboard (like Anomalies).
- Synchronization: Implements a "sync and sweep" mechanism. It timestamps
  every object seen during a run and deletes any objects from the graph that
  are no longer present in the source APIs, ensuring the graph does not
  contain stale data.

Architecture:
-------------
- Designed to run as a long-running, self-contained container.
- Reads all necessary credentials and endpoints from environment variables for
  flexibility and security.
- Connects to a Neo4j database to store the graph data.

"""

import requests
import json
import os
import time
import schedule
import threading
from neo4j import GraphDatabase, exceptions
from datetime import datetime, timezone

# --- Optional Splunk Observability / OpenTelemetry Instrumentation ---
SPLUNK_ENABLED = os.getenv("SPLUNK_OBSERVABILITY_ENABLED", "false").lower() == "true"

if SPLUNK_ENABLED:
    print("🔭 Splunk Observability enabled - initializing OpenTelemetry...")
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_VERSION, DEPLOYMENT_ENVIRONMENT
    from opentelemetry.instrumentation.requests import RequestsInstrumentor
    from opentelemetry.trace import SpanKind, Status, StatusCode

    OTEL_EXPORTER_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://splunk-otel-collector-agent.splunk-otel.svc.cluster.local:4317")
    OTEL_SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "gbaia-ingestor")
    OTEL_ENVIRONMENT = os.getenv("OTEL_ENVIRONMENT", "production")

    resource = Resource.create({
        SERVICE_NAME: OTEL_SERVICE_NAME,
        SERVICE_VERSION: "1.0.0",
        DEPLOYMENT_ENVIRONMENT: OTEL_ENVIRONMENT,
        "service.namespace": "gbaia",
    })

    tracer_provider = TracerProvider(resource=resource)
    otlp_exporter = OTLPSpanExporter(endpoint=OTEL_EXPORTER_ENDPOINT, insecure=True)
    tracer_provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
    trace.set_tracer_provider(tracer_provider)

    # Instrument requests library for APIC/ND API calls
    RequestsInstrumentor().instrument()

    # Create tracer for custom spans
    ingestor_tracer = trace.get_tracer("ingestor", "1.0.0")
    print(f"✅ OpenTelemetry initialized - sending traces to {OTEL_EXPORTER_ENDPOINT}")
else:
    ingestor_tracer = None
    print("🔭 Splunk Observability disabled (set SPLUNK_OBSERVABILITY_ENABLED=true to enable)")

# --- Prometheus Metrics ---
METRICS_ENABLED = os.getenv("PROMETHEUS_METRICS_ENABLED", "true").lower() == "true"

if METRICS_ENABLED:
    from prometheus_client import Counter, Gauge, Histogram, start_http_server

    # Metrics definitions
    SYNC_DURATION = Histogram(
        'ingestor_sync_duration_seconds',
        'Duration of sync cycles',
        ['source'],  # apic, nexus_dashboard, total
        buckets=[1, 5, 10, 30, 60, 120, 300]
    )
    RECORDS_SYNCED = Counter(
        'ingestor_records_synced_total',
        'Total records synced',
        ['source', 'type']  # source: apic/nd, type: tenant/anomaly/etc
    )
    RECORDS_DELETED = Counter(
        'ingestor_records_deleted_total',
        'Total stale records deleted',
        ['source']
    )
    SYNC_ERRORS = Counter(
        'ingestor_sync_errors_total',
        'Total sync errors',
        ['source', 'error_type']
    )
    LAST_SYNC_SUCCESS = Gauge(
        'ingestor_last_sync_success_timestamp',
        'Timestamp of last successful sync',
        ['source']
    )
    SYNC_STATUS = Gauge(
        'ingestor_sync_status',
        'Current sync status (1=success, 0=failure)',
        ['source']
    )

    # Start Prometheus HTTP server on port 9090
    def start_metrics_server():
        print("📊 Starting Prometheus metrics server on port 9090...")
        start_http_server(9090)
        print("✅ Prometheus metrics available at http://0.0.0.0:9090/metrics")

    metrics_thread = threading.Thread(target=start_metrics_server, daemon=True)
    metrics_thread.start()
else:
    print("📊 Prometheus metrics disabled (set PROMETHEUS_METRICS_ENABLED=true to enable)")


# Helper function for traced Neo4j operations
def traced_neo4j_run(session, query, operation_name="neo4j.query", **params):
    """Execute a Neo4j query with optional OpenTelemetry tracing."""
    if ingestor_tracer:
        with ingestor_tracer.start_as_current_span(
            operation_name,
            kind=SpanKind.CLIENT,
            attributes={
                "db.system": "neo4j",
                "db.operation": "write",
                "db.statement": query[:500],  # Truncate long queries
            }
        ) as span:
            try:
                result = session.run(query, **params)
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                raise
    else:
        return session.run(query, **params)


# --- Configuration ---
APIC_URL = os.getenv("APIC_URL")
APIC_USER = os.getenv("APIC_USER")
APIC_PASSWORD = os.getenv("APIC_PASSWORD")

ND_URL = os.getenv("ND_URL")
ND_USER = os.getenv("ND_USER")
ND_PASSWORD = os.getenv("ND_PASSWORD")

NEO4J_URI = os.getenv("NEO4J_URI")
NEO4J_USER = os.getenv("NEO4J_USER")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")

# Intersight configuration
# Option 1: Direct SDK access (preferred for MAC addresses)
INTERSIGHT_API_KEY_ID = os.getenv("INTERSIGHT_API_KEY_ID", "")
INTERSIGHT_API_SECRET_KEY = os.getenv("INTERSIGHT_API_SECRET_KEY", "")  # PEM content or path
INTERSIGHT_BASE_URL = os.getenv("INTERSIGHT_BASE_URL", "https://intersight.com")

# Option 2: MCP server (fallback for queries)
INTERSIGHT_MCP_ENABLED = os.getenv("INTERSIGHT_MCP_ENABLED", "true").lower() == "true"
INTERSIGHT_MCP_URL = os.getenv("INTERSIGHT_MCP_URL", "http://intersight-mcp:3000")

# Enable Intersight ingestion only if SDK credentials are available
INTERSIGHT_ENABLED = bool(INTERSIGHT_API_KEY_ID and INTERSIGHT_API_SECRET_KEY)

# Optional: Specify which ND fabric this APIC belongs to (for linking Tenants/Nodes)
# If not set, will try to auto-detect or use first fabric
APIC_FABRIC_NAME = os.getenv("APIC_FABRIC_NAME", "")
# ---------------------

requests.urllib3.disable_warnings()

def mark_data_source_availability(driver, apic_available, nd_available, intersight_available):
    """
    Create/update DataSource nodes in Neo4j to track availability.
    This allows the backend to adapt queries based on available data.
    """
    with driver.session() as session:
        timestamp = datetime.now(timezone.utc)

        # Mark APIC availability
        session.run("""
            MERGE (ds:DataSource {name: 'apic'})
            SET ds.available = $available,
                ds.last_check = $timestamp,
                ds.description = $description,
                ds.provides = $provides
        """,
        available=apic_available,
        timestamp=timestamp,
        description="Cisco APIC - ACI Policy Model",
        provides="Tenants, EPGs, Contracts, Bridge Domains, VRFs"
        )

        # Mark ND availability
        session.run("""
            MERGE (ds:DataSource {name: 'nexus_dashboard'})
            SET ds.available = $available,
                ds.last_check = $timestamp,
                ds.description = $description,
                ds.provides = $provides
        """,
        available=nd_available,
        timestamp=timestamp,
        description="Cisco Nexus Dashboard - Operations & Monitoring",
        provides="Fabrics, Anomalies, Health Metrics, Compliance"
        )

        # Mark Intersight availability
        session.run("""
            MERGE (ds:DataSource {name: 'intersight'})
            SET ds.available = $available,
                ds.last_check = $timestamp,
                ds.description = $description,
                ds.provides = $provides
        """,
        available=intersight_available,
        timestamp=timestamp,
        description="Cisco Intersight - Compute Infrastructure",
        provides="UCS Servers, Health, Alarms, vNICs, MAC Addresses"
        )

        print(f"  - APIC: {'✅ Available' if apic_available else '❌ Unavailable'}")
        print(f"  - Nexus Dashboard: {'✅ Available' if nd_available else '❌ Unavailable'}")
        print(f"  - Intersight: {'✅ Available' if intersight_available else '❌ Unavailable'}")


def run_ingestion_job():
    """
    Main function that runs a full data ingestion cycle
    from APIC and Nexus Dashboard to Neo4j.
    """
    print(f"\n🚀 === STARTING SCHEDULED INGESTION TASK: {datetime.now()} === 🚀")

    total_start_time = time.time()

    # Create parent span for entire sync job
    if ingestor_tracer:
        with ingestor_tracer.start_as_current_span(
            "ingestion_job",
            kind=SpanKind.INTERNAL,
            attributes={"job.type": "scheduled_sync"}
        ) as job_span:
            _execute_ingestion(job_span)
    else:
        _execute_ingestion(None)

    # Record total sync duration
    if METRICS_ENABLED:
        SYNC_DURATION.labels(source="total").observe(time.time() - total_start_time)


def _execute_ingestion(parent_span):
    """
    Adaptive ingestion - handles missing data sources gracefully.
    Marks data source availability in Neo4j for backend awareness.
    """
    # Track which sources are available
    apic_available = False
    nd_available = False
    intersight_available = False

    try:
        neo4j_driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
        neo4j_driver.verify_connectivity()

        # === APIC Data (Policy Model) ===
        apic_start = time.time()
        print("\n📋 Attempting APIC connection...")
        try:
            apic_session = get_apic_session(APIC_URL, APIC_USER, APIC_PASSWORD)
            if apic_session:
                if ingestor_tracer:
                    with ingestor_tracer.start_as_current_span(
                        "process_apic_data",
                        kind=SpanKind.INTERNAL,
                        attributes={"data.source": "apic", "apic.url": APIC_URL}
                    ):
                        process_apic_data(neo4j_driver, apic_session, APIC_URL)
                else:
                    process_apic_data(neo4j_driver, apic_session, APIC_URL)

                if METRICS_ENABLED:
                    SYNC_DURATION.labels(source="apic").observe(time.time() - apic_start)
                    LAST_SYNC_SUCCESS.labels(source="apic").set(time.time())
                    SYNC_STATUS.labels(source="apic").set(1)

                apic_available = True
                print("✅ APIC data successfully ingested")
            else:
                print("⚠️  APIC connection failed - continuing without policy model data")
        except Exception as e:
            print(f"⚠️  APIC processing failed: {e}")
            print("   Continuing with Nexus Dashboard only...")
            if METRICS_ENABLED:
                SYNC_ERRORS.labels(source="apic", error_type=type(e).__name__).inc()
                SYNC_STATUS.labels(source="apic").set(0)

        # === Nexus Dashboard Data (Operational/Fabric Data) ===
        nd_start = time.time()
        print("\n📡 Attempting Nexus Dashboard connection...")
        try:
            nd_token = get_nexus_token(ND_URL, ND_USER, ND_PASSWORD)
            if nd_token:
                if ingestor_tracer:
                    with ingestor_tracer.start_as_current_span(
                        "process_nexus_dashboard_data",
                        kind=SpanKind.INTERNAL,
                        attributes={"data.source": "nexus_dashboard", "nd.url": ND_URL}
                    ):
                        process_nexus_dashboard_data(neo4j_driver, nd_token, ND_URL)
                else:
                    process_nexus_dashboard_data(neo4j_driver, nd_token, ND_URL)

                if METRICS_ENABLED:
                    SYNC_DURATION.labels(source="nexus_dashboard").observe(time.time() - nd_start)
                    LAST_SYNC_SUCCESS.labels(source="nexus_dashboard").set(time.time())
                    SYNC_STATUS.labels(source="nexus_dashboard").set(1)

                nd_available = True
                print("✅ Nexus Dashboard data successfully ingested")
            else:
                print("⚠️  Nexus Dashboard connection failed - no operational data available")
        except Exception as e:
            print(f"⚠️  Nexus Dashboard processing failed: {e}")
            if METRICS_ENABLED:
                SYNC_ERRORS.labels(source="nexus_dashboard", error_type=type(e).__name__).inc()
                SYNC_STATUS.labels(source="nexus_dashboard").set(0)

        # === Intersight Data (Compute Infrastructure) ===
        if INTERSIGHT_ENABLED:
            intersight_start = time.time()
            print("\n🖥️  Attempting Intersight SDK connection...")
            try:
                if ingestor_tracer:
                    with ingestor_tracer.start_as_current_span(
                        "process_intersight_data",
                        kind=SpanKind.INTERNAL,
                        attributes={"data.source": "intersight", "intersight.api_key": INTERSIGHT_API_KEY_ID[:20] + "..."}
                    ):
                        process_intersight_data(neo4j_driver)
                else:
                    process_intersight_data(neo4j_driver)

                if METRICS_ENABLED:
                    SYNC_DURATION.labels(source="intersight").observe(time.time() - intersight_start)
                    LAST_SYNC_SUCCESS.labels(source="intersight").set(time.time())
                    SYNC_STATUS.labels(source="intersight").set(1)

                intersight_available = True
                print("✅ Intersight data successfully ingested and correlated")
            except Exception as e:
                print(f"⚠️  Intersight processing failed: {e}")
                if METRICS_ENABLED:
                    SYNC_ERRORS.labels(source="intersight", error_type=type(e).__name__).inc()
                    SYNC_STATUS.labels(source="intersight").set(0)
        else:
            print("\n🖥️  Intersight disabled (set INTERSIGHT_API_KEY_ID and INTERSIGHT_API_SECRET_KEY to enable)")

        # === Mark Data Source Availability in Neo4j ===
        print("\n📊 Updating data source availability...")
        mark_data_source_availability(neo4j_driver, apic_available, nd_available, intersight_available)

        neo4j_driver.close()

        # Determine completion status
        sources_available = sum([apic_available, nd_available, intersight_available])

        if sources_available == 3:
            print(f"\n✅ === FULL INGESTION COMPLETED (APIC + ND + Intersight): {datetime.now()} === ✅")
        elif sources_available >= 2:
            active = []
            if apic_available: active.append("APIC")
            if nd_available: active.append("ND")
            if intersight_available: active.append("Intersight")
            print(f"\n✅ === PARTIAL INGESTION COMPLETED ({' + '.join(active)}): {datetime.now()} === ✅")
            if not apic_available: print("   Note: APIC unavailable - no policy model data")
            if not nd_available: print("   Note: ND unavailable - no operational/fabric data")
            if not intersight_available: print("   Note: Intersight unavailable - no compute/server data")
        elif sources_available == 1:
            if apic_available:
                print(f"\n✅ === PARTIAL INGESTION COMPLETED (APIC ONLY): {datetime.now()} === ✅")
            elif nd_available:
                print(f"\n✅ === PARTIAL INGESTION COMPLETED (ND ONLY): {datetime.now()} === ✅")
            else:
                print(f"\n✅ === PARTIAL INGESTION COMPLETED (Intersight ONLY): {datetime.now()} === ✅")
        else:
            print(f"\n❌ === INGESTION FAILED: No data sources available === ❌")

        if parent_span:
            parent_span.set_status(Status(StatusCode.OK))

    except Exception as e:
        print(f"❌ CRITICAL ERROR DURING INGESTION TASK: {e}")
        if METRICS_ENABLED:
            SYNC_ERRORS.labels(source="total", error_type=type(e).__name__).inc()
            SYNC_STATUS.labels(source="apic").set(0)
            SYNC_STATUS.labels(source="nexus_dashboard").set(0)
        if parent_span:
            parent_span.set_status(Status(StatusCode.ERROR, str(e)))
            parent_span.record_exception(e)

# --- FUNCTIONS FOR INTERSIGHT ---
def process_intersight_data(driver, mcp_url=None):
    """
    Fetch server inventory from Intersight using direct SDK access and create
    relationships with network endpoints based on MAC address correlation.

    Uses Intersight Python SDK for reliable MAC address retrieval.
    """
    sync_start_time = datetime.now(timezone.utc)
    counters = {
        "servers": 0,
        "vnics": 0,
        "mac_correlations": 0,
        "new_servers": 0,
        "updated_servers": 0
    }

    print("  🖥️  Fetching server inventory from Intersight SDK...")

    try:
        # Initialize Intersight API client with SDK
        from intersight.api_client import ApiClient
        from intersight.configuration import Configuration
        from intersight.signing import HttpSigningConfiguration
        from intersight.api import compute_api, adapter_api

        # Configure API client
        config = Configuration()
        config.host = INTERSIGHT_BASE_URL

        # Configure HTTP signature authentication
        # API secret can be either PEM content or file path
        import tempfile
        import os as os_module

        # Always use temp file for consistency
        if INTERSIGHT_API_SECRET_KEY.startswith("-----BEGIN"):
            # Direct PEM content - write to temp file
            # Environment variables may have literal \n instead of newlines
            pem_content = INTERSIGHT_API_SECRET_KEY.replace('\\n', '\n')

            # IMPORTANT: Some keys are PKCS#8 format but have SEC1 headers (BEGIN EC PRIVATE KEY)
            # Fix by normalizing to PKCS#8 headers if needed
            if 'BEGIN EC PRIVATE KEY' in pem_content:
                print(f"  🔧 Detected SEC1 header, converting to PKCS#8 header...")
                pem_content = pem_content.replace('BEGIN EC PRIVATE KEY', 'BEGIN PRIVATE KEY')
                pem_content = pem_content.replace('END EC PRIVATE KEY', 'END PRIVATE KEY')

            with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.pem') as f:
                f.write(pem_content)
                key_file_path = f.name
            print(f"  📝 Wrote PEM key to temp file: {key_file_path}")
        else:
            # File path provided
            key_file_path = INTERSIGHT_API_SECRET_KEY
            print(f"  📁 Using key file: {key_file_path}")

        # Debug: Validate key format
        print(f"  🔍 Validating private key...")
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives.asymmetric import ec

        with open(key_file_path, 'rb') as key_file:
            key_data = key_file.read()

        print(f"  📊 Key data length: {len(key_data)} bytes")

        # Load the private key
        try:
            private_key = serialization.load_pem_private_key(
                key_data,
                password=None,
                backend=default_backend()
            )
            print(f"  ✅ Key loaded successfully")
        except Exception as e:
            print(f"  ❌ Failed to load key: {e}")
            raise

        # Check key type and determine signing algorithm
        if isinstance(private_key, ec.EllipticCurvePrivateKey):
            print(f"  ✅ Valid EC private key detected")
            print(f"     Curve: {private_key.curve.name}")
            # Intersight SDK uses PyCryptodome and expects specific algorithm names:
            # 'fips-186-3' or 'deterministic-rfc6979' for EC keys
            signing_algorithm = "fips-186-3"

            # Intersight SDK expects EC keys in SEC1 format (BEGIN EC PRIVATE KEY)
            # Convert from PKCS#8 back to SEC1 for SDK compatibility
            print(f"  🔄 Converting key to SEC1 format for SDK...")
            sec1_key = private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption()
            )

            # Write SEC1 formatted key to new temp file
            with tempfile.NamedTemporaryFile(mode='wb', delete=False, suffix='.pem') as f:
                f.write(sec1_key)
                key_file_path = f.name
            print(f"  ✅ Wrote SEC1 key to: {key_file_path}")

        else:
            print(f"  ✅ Valid RSA private key detected")
            print(f"     Key size: {private_key.key_size} bits")
            # Intersight SDK expects 'RSASSA-PSS' or 'RSASSA-PKCS1-v1_5' for RSA keys
            signing_algorithm = "RSASSA-PSS"

        print(f"  📝 Using signing algorithm: {signing_algorithm}")
        print(f"  📝 Using key file: {key_file_path}")

        # Create signing configuration
        # Intersight API requires these specific headers to be signed
        signed_headers = ["(request-target)", "host", "date", "digest"]

        signing_config = HttpSigningConfiguration(
            key_id=INTERSIGHT_API_KEY_ID,
            private_key_path=key_file_path,
            signing_scheme="hs2019",
            signing_algorithm=signing_algorithm,
            hash_algorithm="sha256",
            signed_headers=signed_headers
        )
        print(f"  ✅ Signing configuration created successfully")

        config.signing_info = signing_config

        api_client = ApiClient(config)
        compute_instance = compute_api.ComputeApi(api_client)
        adapter_instance = adapter_api.AdapterApi(api_client)

        # Step 1: Get list of all compute servers
        print("  📡 Querying compute.PhysicalSummary...")
        servers_response = compute_instance.get_compute_physical_summary_list()

        if not servers_response or not hasattr(servers_response, 'results'):
            print("  ❌ No server data returned from Intersight")
            return

        servers = servers_response.results
        print(f"  📊 Found {len(servers)} servers in Intersight")

        # Test: Query all adapters without filter to see if any exist
        print(f"  🔍 Testing adapter query (first 10)...")
        try:
            test_adapters = adapter_instance.get_adapter_host_eth_interface_list(top=10)
            if test_adapters and hasattr(test_adapters, 'results'):
                print(f"    Total adapters in system: {len(test_adapters.results)} (showing first 10)")
                for tadapter in test_adapters.results[:3]:
                    print(f"      - {getattr(tadapter, 'name', 'N/A')}, MAC: {getattr(tadapter, 'mac_address', 'N/A')}, Parent: {getattr(getattr(tadapter, 'parent', None), 'moid', 'N/A')}")
        except Exception as e:
            print(f"    ⚠️  Test query failed: {e}")

        with driver.session() as db_session:
            for server in servers:
                try:
                    # Extract server information from SDK object
                    server_name = getattr(server, 'name', 'Unknown')
                    server_moid = getattr(server, 'moid', None)
                    server_model = getattr(server, 'model', 'Unknown')
                    server_serial = getattr(server, 'serial', '')
                    server_cpu_cores = getattr(server, 'num_cpus', 0)
                    server_memory_gb = getattr(server, 'total_memory', 0) // 1024 if hasattr(server, 'total_memory') else 0
                    server_power_state = getattr(server, 'oper_power_state', 'unknown')

                    # Health status from alarm summary
                    alarm_summary = getattr(server, 'alarm_summary', None)
                    if alarm_summary:
                        critical_alarms = getattr(alarm_summary, 'critical', 0)
                        warning_alarms = getattr(alarm_summary, 'warning', 0)
                    else:
                        critical_alarms = 0
                        warning_alarms = 0

                    if critical_alarms > 0:
                        health_status = "Critical"
                    elif warning_alarms > 0:
                        health_status = "Warning"
                    else:
                        health_status = "Healthy"

                    if not server_moid:
                        print(f"  ⚠️  Skipping server {server_name} - no MOID")
                        continue

                    # Create or update IntersightServer node
                    result = traced_neo4j_run(
                        db_session,
                        """
                        MERGE (s:IntersightServer {moid: $moid})
                        ON CREATE SET
                            s.name = $name,
                            s.model = $model,
                            s.serial = $serial,
                            s.cpu_cores = $cpu_cores,
                            s.memory_gb = $memory_gb,
                            s.power_state = $power_state,
                            s.health = $health,
                            s.critical_alarms = $critical_alarms,
                            s.warning_alarms = $warning_alarms,
                            s.first_seen = $timestamp,
                            s.last_seen = $timestamp
                        ON MATCH SET
                            s.name = $name,
                            s.model = $model,
                            s.serial = $serial,
                            s.cpu_cores = $cpu_cores,
                            s.memory_gb = $memory_gb,
                            s.power_state = $power_state,
                            s.health = $health,
                            s.critical_alarms = $critical_alarms,
                            s.warning_alarms = $warning_alarms,
                            s.last_seen = $timestamp
                        RETURN s.name AS name,
                               CASE WHEN s.first_seen = $timestamp THEN true ELSE false END AS is_new
                        """,
                        "neo4j.create_server",
                        moid=server_moid,
                        name=server_name,
                        model=server_model,
                        serial=server_serial,
                        cpu_cores=server_cpu_cores,
                        memory_gb=server_memory_gb,
                        power_state=server_power_state,
                        health=health_status,
                        critical_alarms=critical_alarms,
                        warning_alarms=warning_alarms,
                        timestamp=sync_start_time
                    )

                    record = result.single()
                    if record:
                        counters["servers"] += 1
                        if record["is_new"]:
                            counters["new_servers"] += 1
                        else:
                            counters["updated_servers"] += 1

                    # Step 3: Get adapter host ethernet interfaces for this server using SDK
                    # Query adapter.HostEthInterface filtered by parent compute blade/rack unit
                    try:
                        # Filter: Parent/Moid eq 'server_moid'
                        filter_str = f"Parent/Moid eq '{server_moid}'"
                        adapters_response = adapter_instance.get_adapter_host_eth_interface_list(filter=filter_str)

                        if adapters_response and hasattr(adapters_response, 'results'):
                            adapters = adapters_response.results
                            if len(adapters) > 0:
                                print(f"    📡 Found {len(adapters)} adapters for server '{server_name}'")

                            # Process each ethernet interface
                            for adapter in adapters:
                                mac_address = getattr(adapter, 'mac_address', '')
                                if mac_address:
                                    mac_address = mac_address.upper()

                                if mac_address and mac_address != "" and mac_address != "00:00:00:00:00:00":
                                    counters["vnics"] += 1
                                    adapter_name = getattr(adapter, 'name', 'Unknown')

                                    # Step 4: Correlate with ACI endpoints by MAC address
                                    correlation_result = traced_neo4j_run(
                                        db_session,
                                        """
                                        MATCH (s:IntersightServer {moid: $server_moid})
                                        MATCH (e:Endpoint)
                                        WHERE toUpper(e.mac) = $mac
                                        MERGE (s)-[r:CONNECTED_TO]->(e)
                                        ON CREATE SET r.created = $timestamp
                                        SET r.last_seen = $timestamp,
                                            r.interface_name = $interface_name
                                        RETURN e.mac AS matched_mac, e.ip AS endpoint_ip
                                        """,
                                        "neo4j.correlate_mac",
                                        server_moid=server_moid,
                                        mac=mac_address,
                                        interface_name=adapter_name,
                                        timestamp=sync_start_time
                                    )

                                    matched_records = list(correlation_result)
                                    if matched_records:
                                        counters["mac_correlations"] += len(matched_records)
                                        for match in matched_records:
                                            print(f"    ✅ Correlated: Server '{server_name}' ↔ MAC {mac_address} ↔ IP {match['endpoint_ip']}")
                    except Exception as adapter_error:
                        print(f"  ⚠️  Could not fetch adapters for server {server_name}: {adapter_error}")
                        # Continue with server even if adapters fail

                except Exception as server_error:
                    print(f"  ⚠️  Error processing server {server.get('Name', 'Unknown')}: {server_error}")
                    continue

            # Clean up stale servers (not seen in this sync)
            cleanup_result = traced_neo4j_run(
                db_session,
                """
                MATCH (s:IntersightServer)
                WHERE s.last_seen < $sync_start_time
                DETACH DELETE s
                RETURN count(s) AS deleted_count
                """,
                "neo4j.cleanup_stale_servers",
                sync_start_time=sync_start_time
            )

            deleted = cleanup_result.single()["deleted_count"]
            if deleted > 0:
                print(f"  🗑️  Removed {deleted} stale servers")

        # Print summary
        print(f"\n  📊 Intersight Ingestion Summary:")
        print(f"     - Servers processed: {counters['servers']} ({counters['new_servers']} new, {counters['updated_servers']} updated)")
        print(f"     - vNICs found: {counters['vnics']}")
        print(f"     - MAC correlations created: {counters['mac_correlations']}")

        if METRICS_ENABLED:
            RECORDS_SYNCED.labels(source="intersight", type="server").inc(counters["servers"])
            RECORDS_SYNCED.labels(source="intersight", type="vnic").inc(counters["vnics"])
            RECORDS_SYNCED.labels(source="intersight", type="correlation").inc(counters["mac_correlations"])

    except ImportError as e:
        print(f"  ❌ Intersight SDK not available: {e}")
        print("     Install with: pip install intersight")
        raise
    except Exception as e:
        print(f"  ❌ Intersight SDK error: {e}")
        import traceback
        traceback.print_exc()
        raise

# --- FUNCTIONS FOR APIC ---
def get_apic_session(url, user, password):
    session = requests.Session()
    try:
        response = session.post(f"{url}/api/aaaLogin.json", json={"aaaUser": {"attributes": {"name": user, "pwd": password}}}, verify=False)
        response.raise_for_status()
        print("✅ APIC Login successful!")
        return session
    except requests.exceptions.RequestException as e:
        print(f"❌ APIC Login failed: {e}")
        return None

def process_apic_data(driver, session, url):
    sync_start_time = datetime.now(timezone.utc)
    counters = {"tenant": 0, "vrf": 0, "bd": 0, "subnet": 0, "ap": 0, "epg": 0, "node": 0, "fault": 0}

    with driver.session() as db_session:
        # --- 1. Fetch Tenant hierarchy: Tenants, APs, EPGs, VRFs, BDs, Subnets ---
        tenant_query_url = f"{url}/api/node/class/fvTenant.json?query-target=subtree&target-subtree-class=fvAp,fvAEPg,fvCtx,fvBD,fvSubnet"
        print(f"▶️  Fetching APIC tenant data (Tenants, APs, EPGs, VRFs, BDs, Subnets)...")
        tenant_data = session.get(tenant_query_url, verify=False).json().get("imdata", [])

        if tenant_data:
            print(f"  - Received {len(tenant_data)} tenant-related objects")
            for item in tenant_data:
                if "fvTenant" in item:
                    attrs = item["fvTenant"]["attributes"]
                    traced_neo4j_run(db_session, "MERGE (t:Tenant {name: $name}) SET t.lastSeen = $timestamp",
                                   "neo4j.merge.tenant", name=attrs["name"], timestamp=sync_start_time)
                    counters["tenant"] += 1

                elif "fvCtx" in item:
                    # VRF/Context - L3 routing domain
                    attrs = item["fvCtx"]["attributes"]
                    tenant_name = attrs["dn"].split('/')[1][3:]
                    db_session.run("""
                        MERGE (t:Tenant {name: $t_name}) SET t.lastSeen = $timestamp
                        MERGE (v:VRF {name: $vrf_name, tenant: $t_name})
                        SET v.lastSeen = $timestamp, v.dn = $dn
                        MERGE (t)-[:HAS_VRF]->(v)
                    """, t_name=tenant_name, vrf_name=attrs["name"], dn=attrs["dn"], timestamp=sync_start_time)

                elif "fvBD" in item:
                    # Bridge Domain - L2 forwarding domain
                    attrs = item["fvBD"]["attributes"]
                    tenant_name = attrs["dn"].split('/')[1][3:]
                    db_session.run("""
                        MERGE (t:Tenant {name: $t_name}) SET t.lastSeen = $timestamp
                        MERGE (bd:BridgeDomain {name: $bd_name, tenant: $t_name})
                        SET bd.lastSeen = $timestamp, bd.dn = $dn
                        MERGE (t)-[:HAS_BD]->(bd)
                    """, t_name=tenant_name, bd_name=attrs["name"], dn=attrs["dn"], timestamp=sync_start_time)

                elif "fvSubnet" in item:
                    # Subnet within a Bridge Domain
                    attrs = item["fvSubnet"]["attributes"]
                    dn_parts = attrs["dn"].split('/')
                    tenant_name = dn_parts[1][3:]
                    bd_name = dn_parts[2][3:]
                    subnet_ip = attrs.get("ip", "unknown")
                    db_session.run("""
                        MERGE (bd:BridgeDomain {name: $bd_name, tenant: $t_name}) SET bd.lastSeen = $timestamp
                        MERGE (s:Subnet {ip: $subnet_ip, bd: $bd_name, tenant: $t_name})
                        SET s.lastSeen = $timestamp, s.scope = $scope
                        MERGE (bd)-[:HAS_SUBNET]->(s)
                    """, t_name=tenant_name, bd_name=bd_name, subnet_ip=subnet_ip,
                         scope=attrs.get("scope", ""), timestamp=sync_start_time)

                elif "fvAp" in item:
                    attrs = item["fvAp"]["attributes"]
                    tenant_name = attrs["dn"].split('/')[1][3:]
                    db_session.run("""
                        MERGE (t:Tenant {name: $t_name}) SET t.lastSeen = $timestamp
                        MERGE (ap:AppProfile {name: $ap_name, tenant: $t_name}) SET ap.lastSeen = $timestamp
                        MERGE (t)-[:HAS_AP]->(ap)
                    """, t_name=tenant_name, ap_name=attrs["name"], timestamp=sync_start_time)

                elif "fvAEPg" in item:
                    attrs = item["fvAEPg"]["attributes"]
                    tenant_name = attrs["dn"].split('/')[1][3:]
                    ap_name = attrs["dn"].split('/')[2][3:]
                    db_session.run("""
                        MERGE (t:Tenant {name: $t_name}) SET t.lastSeen = $timestamp
                        MERGE (ap:AppProfile {name: $ap_name, tenant: $t_name}) SET ap.lastSeen = $timestamp
                        MERGE (e:EPG {name: $epg_name, ap: $ap_name, tenant: $t_name}) SET e.lastSeen = $timestamp
                        MERGE (ap)-[:HAS_EPG]->(e)
                    """, t_name=tenant_name, ap_name=ap_name, epg_name=attrs["name"], timestamp=sync_start_time)

        # --- 2. Fetch Fabric Nodes (Spines, Leaves, Controllers) ---
        nodes_url = f"{url}/api/node/class/fabricNode.json"
        print(f"▶️  Fetching APIC fabric nodes (Spines, Leaves)...")
        nodes_data = session.get(nodes_url, verify=False).json().get("imdata", [])

        if nodes_data:
            print(f"  - Received {len(nodes_data)} fabric nodes")
            for item in nodes_data:
                if "fabricNode" in item:
                    attrs = item["fabricNode"]["attributes"]
                    db_session.run("""
                        MERGE (n:Node {id: $node_id})
                        SET n.name = $name,
                            n.role = $role,
                            n.fabricSt = $fabric_st,
                            n.model = $model,
                            n.serial = $serial,
                            n.address = $address,
                            n.lastSeen = $timestamp
                    """, node_id=attrs.get("id"), name=attrs.get("name"), role=attrs.get("role"),
                         fabric_st=attrs.get("fabricSt"), model=attrs.get("model"),
                         serial=attrs.get("serial"), address=attrs.get("address"),
                         timestamp=sync_start_time)

        # --- 3. Fetch Faults ---
        faults_url = f"{url}/api/node/class/faultInst.json?query-target-filter=and(ne(faultInst.severity,\"cleared\"))"
        print(f"▶️  Fetching APIC faults...")
        faults_data = session.get(faults_url, verify=False).json().get("imdata", [])

        if faults_data:
            print(f"  - Received {len(faults_data)} active faults")
            for item in faults_data:
                if "faultInst" in item:
                    attrs = item["faultInst"]["attributes"]
                    # Extract affected object from DN
                    affected_dn = attrs.get("dn", "")
                    db_session.run("""
                        MERGE (f:Fault {code: $code, dn: $dn})
                        SET f.severity = $severity,
                            f.cause = $cause,
                            f.descr = $descr,
                            f.type = $type,
                            f.created = $created,
                            f.lastSeen = $timestamp
                    """, code=attrs.get("code"), dn=affected_dn, severity=attrs.get("severity"),
                         cause=attrs.get("cause"), descr=attrs.get("descr"), type=attrs.get("type"),
                         created=attrs.get("created"), timestamp=sync_start_time)

                    # Try to link fault to affected Node if it's a node fault
                    if "/node-" in affected_dn:
                        try:
                            node_id = affected_dn.split("/node-")[1].split("/")[0]
                            db_session.run("""
                                MATCH (n:Node {id: $node_id})
                                MATCH (f:Fault {code: $code, dn: $dn})
                                MERGE (n)-[:HAS_FAULT]->(f)
                            """, node_id=node_id, code=attrs.get("code"), dn=affected_dn)
                        except (IndexError, KeyError):
                            pass

        # --- Sweep old APIC objects ---
        print("\n▶️  Sweep (APIC): Deleting old objects...")
        result = db_session.run("""
            MATCH (n)
            WHERE (n:Tenant OR n:AppProfile OR n:EPG OR n:VRF OR n:BridgeDomain OR n:Subnet OR n:Node OR n:Fault)
            AND (n.lastSeen IS NULL OR n.lastSeen < $timestamp)
            DETACH DELETE n
            RETURN count(n) as deleted_count
        """, timestamp=sync_start_time)
        deleted_count = result.single()["deleted_count"]
        print(f"✅ APIC data synchronized. {deleted_count} old object(s) deleted.")

        # Record metrics
        if METRICS_ENABLED:
            RECORDS_DELETED.labels(source="apic").inc(deleted_count)
            for obj_type, count in counters.items():
                if count > 0:
                    RECORDS_SYNCED.labels(source="apic", type=obj_type).inc(count)

# --- FUNCTIONS FOR NEXUS DASHBOARD ---
def get_nexus_token(url, user, password):
    try:
        response = requests.post(f"{url}/login", json={"userName": user, "password": password}, verify=False)
        response.raise_for_status()
        print("✅ Nexus Dashboard Login successful!")
        return response.json().get("token")
    except requests.exceptions.RequestException as e:
        print(f"❌ Nexus Dashboard Login failed: {e}")
        return None

def process_nexus_dashboard_data(driver, token, url):
    if not token: return
    
    headers = {"Authorization": f"Bearer {token}"}
    fabrics_url = f"{url}/api/v1/oneManage/manage/fabricsSummaryBrief"
    print(f"▶️  Fetching fabric list from URL: {fabrics_url}")
    
    try:
        response = requests.get(fabrics_url, headers=headers, verify=False)
        response.raise_for_status()
        fabrics_data = response.json().get("fabrics", [])
        print(f"✅ Received {len(fabrics_data)} fabrics from Nexus Dashboard.")

        with driver.session() as db_session:
            sync_start_time = datetime.now(timezone.utc)

            for fabric in fabrics_data:
                fabric_name = fabric.get("fabricName")
                if not fabric_name: continue

                print(f"\nProcessing fabric: {fabric_name}...")
                db_session.run("MERGE (f:Fabric {name: $fabric_name}) SET f.lastSeen = $timestamp", 
                               fabric_name=fabric_name, timestamp=sync_start_time)
                
                details_url = f"{url}/api/v1/analyze/anomalies/details?fabricName={fabric_name}&offset=0&fabricStatus=online&featureSet=telemetry&includeSystemAnomalies=false&includeSuspendedAlerts=false&includeAnomalies=all"
                print(f"  - Fetching detailed anomalies from: {details_url}")
                details_response = requests.get(details_url, headers=headers, verify=False)
                details_data = details_response.json().get("anomalies", [])
                
                print(f"  - Found {len(details_data)} anomaly details. Correlating in graph...")
                for anomaly in details_data:
                    # Create the Anomaly node first and mark it as seen
                    db_session.run("""
                        MERGE (a:Anomaly {uuid: $uuid})
                        SET a.name = $name,
                            a.severity = $severity,
                            a.category = $category,
                            a.details = $details,
                            a.fabric = $fabric_name,
                            a.lastSeen = $timestamp
                    """, 
                    uuid=anomaly.get("anomalyId"),
                    name=anomaly.get("mnemonicTitle"),
                    severity=anomaly.get("severity"),
                    category=anomaly.get("category"),
                    details=anomaly.get("anomalyString"),
                    fabric_name=fabric_name,
                    timestamp=sync_start_time
                    )
                    
                    # Now, create relationships to the Fabric AND the affected Tenant
                    
                    # 1. Create relationship to the Fabric
                    db_session.run("""
                        MATCH (f:Fabric {name: $fabric_name})
                        MATCH (a:Anomaly {uuid: $uuid})
                        MERGE (f)-[:HAS_ANOMALY]->(a)
                    """, fabric_name=fabric_name, uuid=anomaly.get("anomalyId"))

                    # 2. Find affected tenant and create relationship to it
                    affected_tenant_name = None
                    anomaly_objects = anomaly.get("anomalyObjects") or []
                    for obj in anomaly_objects:
                        if obj.get("objectType") == "tenant":
                            affected_tenant_name = obj.get("name")
                            break
                    
                    if affected_tenant_name:
                        db_session.run("""
                            MATCH (t:Tenant {name: $tenant_name})
                            MATCH (a:Anomaly {uuid: $uuid})
                            MERGE (a)-[:AFFECTS]->(t)
                        """, tenant_name=affected_tenant_name, uuid=anomaly.get("anomalyId"))

            # --- Fetch Advisories (PSIRTs, Field Notices, EOL) ---
            for fabric in fabrics_data:
                fabric_name = fabric.get("fabricName")
                if not fabric_name: continue

                advisories_url = f"{url}/api/v1/advisories?fabricName={fabric_name}"
                print(f"  - Fetching advisories for fabric: {fabric_name}")
                try:
                    advisories_response = requests.get(advisories_url, headers=headers, verify=False)
                    if advisories_response.status_code == 200:
                        advisories_data = advisories_response.json().get("advisories", [])
                        print(f"    Found {len(advisories_data)} advisories")

                        for advisory in advisories_data:
                            db_session.run("""
                                MERGE (a:Advisory {id: $adv_id})
                                SET a.name = $name,
                                    a.type = $type,
                                    a.severity = $severity,
                                    a.description = $description,
                                    a.affectedDevices = $affected_devices,
                                    a.fabric = $fabric_name,
                                    a.lastSeen = $timestamp
                            """,
                            adv_id=advisory.get("advisoryId", advisory.get("id", "")),
                            name=advisory.get("advisoryName", advisory.get("name", "")),
                            type=advisory.get("advisoryType", advisory.get("type", "")),
                            severity=advisory.get("severity", ""),
                            description=advisory.get("description", ""),
                            affected_devices=advisory.get("affectedDeviceCount", 0),
                            fabric_name=fabric_name,
                            timestamp=sync_start_time)

                            # Link advisory to fabric
                            db_session.run("""
                                MATCH (f:Fabric {name: $fabric_name})
                                MATCH (a:Advisory {id: $adv_id})
                                MERGE (f)-[:HAS_ADVISORY]->(a)
                            """, fabric_name=fabric_name, adv_id=advisory.get("advisoryId", advisory.get("id", "")))
                    else:
                        print(f"    Advisories endpoint returned status {advisories_response.status_code}")
                except Exception as e:
                    print(f"    Could not fetch advisories: {e}")

            # --- Link all Tenants and Nodes to their Fabric ---
            # Determine which fabric to use for linking APIC objects
            target_fabric = APIC_FABRIC_NAME
            if not target_fabric and fabrics_data:
                # Auto-detect: find the ACI fabric (APIC only manages ACI fabrics)
                for f in fabrics_data:
                    if f.get("type") == "aci":
                        target_fabric = f.get("fabricName", "")
                        break
                # Fallback to first fabric if no ACI type found
                if not target_fabric:
                    target_fabric = fabrics_data[0].get("fabricName", "")

            if target_fabric:
                print(f"\n▶️  Linking APIC objects to Fabric: {target_fabric}")

                # Remove stale Tenant-Fabric links to other fabrics, then link to target
                db_session.run("""
                    MATCH (t:Tenant)-[r:BELONGS_TO]->(f:Fabric)
                    WHERE f.name <> $fabric_name
                    DELETE r
                """, fabric_name=target_fabric)
                result = db_session.run("""
                    MATCH (f:Fabric {name: $fabric_name})
                    MATCH (t:Tenant)
                    WHERE NOT (t)-[:BELONGS_TO]->(f)
                    MERGE (t)-[:BELONGS_TO]->(f)
                    RETURN count(t) as linked_count
                """, fabric_name=target_fabric)
                tenant_count = result.single()["linked_count"]
                print(f"  - Linked {tenant_count} Tenants to Fabric '{target_fabric}'")

                # Remove stale Node-Fabric links to other fabrics, then link to target
                db_session.run("""
                    MATCH (n:Node)-[r:BELONGS_TO]->(f:Fabric)
                    WHERE f.name <> $fabric_name
                    DELETE r
                """, fabric_name=target_fabric)
                result = db_session.run("""
                    MATCH (f:Fabric {name: $fabric_name})
                    MATCH (n:Node)
                    WHERE NOT (n)-[:BELONGS_TO]->(f)
                    MERGE (n)-[:BELONGS_TO]->(f)
                    RETURN count(n) as linked_count
                """, fabric_name=target_fabric)
                node_count = result.single()["linked_count"]
                print(f"  - Linked {node_count} Nodes to Fabric '{target_fabric}'")
            else:
                print("\n⚠️  No fabric found to link APIC objects. Set APIC_FABRIC_NAME env var.")

            # --- Sweep old ND-related objects ---
            print("\n▶️  Sweep (ND): Deleting old Fabric, Anomaly, and Advisory objects...")
            result = db_session.run("""
                MATCH (n)
                WHERE (n:Fabric OR n:Anomaly OR n:Advisory) AND (n.lastSeen IS NULL OR n.lastSeen < $timestamp)
                DETACH DELETE n
                RETURN count(n) as deleted_count
            """, timestamp=sync_start_time)
            deleted_count = result.single()["deleted_count"]
            print(f"✅ Nexus Dashboard data synchronized. {deleted_count} old object(s) deleted.")

    except requests.exceptions.RequestException as e:
        print(f"❌ Failed to get data from Nexus Dashboard: {e}")
    except json.JSONDecodeError:
        print("❌ Failed to parse JSON from Nexus Dashboard response.")

# --- Main Scheduler Loop ---
if __name__ == "__main__":
    print("🤖 Data ingestor started. Scheduling task...")
    
    schedule.every(60).minutes.do(run_ingestion_job)
    
    print("👍 Task scheduled. Running the first ingestion right now...")
    run_ingestion_job()
    
    while True:
        schedule.run_pending()
        time.sleep(1)