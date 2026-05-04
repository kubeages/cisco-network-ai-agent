"""
================================================================================
Network AI Agent - Frontend User Interface
================================================================================

This module provides a web-based user interface for the Network AI Agent,
built using the Gradio library. It serves as the "Face" of the application,
allowing users to interact with the AI agent in a conversational manner.

"""

import os
import gradio as gr
import requests
import json
import html

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
    from opentelemetry.instrumentation.logging import LoggingInstrumentor

    # Configuration from environment
    OTEL_EXPORTER_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://splunk-otel-collector-agent.splunk-otel.svc.cluster.local:4317")
    OTEL_SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "gbaia-frontend")
    OTEL_ENVIRONMENT = os.getenv("OTEL_ENVIRONMENT", "production")

    # Create resource with service information
    resource = Resource(attributes={
        SERVICE_NAME: OTEL_SERVICE_NAME,
        SERVICE_VERSION: "1.0.0",
        DEPLOYMENT_ENVIRONMENT: OTEL_ENVIRONMENT,
    })

    # Configure tracer provider
    provider = TracerProvider(resource=resource)

    # Configure OTLP exporter (sends to local OTel collector)
    otlp_exporter = OTLPSpanExporter(
        endpoint=OTEL_EXPORTER_ENDPOINT,
        insecure=True
    )
    provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
    trace.set_tracer_provider(provider)

    # Instrument requests library for outgoing HTTP calls
    RequestsInstrumentor().instrument()

    # Instrument logging
    LoggingInstrumentor().instrument(set_logging_format=True)

    print(f"✅ OpenTelemetry initialized - sending traces to {OTEL_EXPORTER_ENDPOINT}")
else:
    print("🔭 Splunk Observability disabled (set SPLUNK_OBSERVABILITY_ENABLED=true to enable)")

BACKEND_URL = "http://backend-api:8000/ask"
BACKEND_STREAM_URL = "http://backend-api:8000/ask/stream"
SUGGESTIONS_URL = "http://backend-api:8000/suggestions"
GRAPH_URL = "http://backend-api:8000/api/graph"

# Node styling configuration - J.A.R.V.I.S. cyan/teal palette
NODE_COLORS = {
    "Tenant": "#00d4ff",      # Cyan (primary accent)
    "AppProfile": "#00e5a0",  # Teal green
    "EPG": "#00c9b7",         # Aqua
    "Fabric": "#f0883e",      # Warm orange (central hub)
    "Anomaly": "#ff5757",     # Alert red
    "HealthSummary": "#a78bfa", # Soft purple
    "VRF": "#818cf8",         # Indigo
    "BridgeDomain": "#34d399", # Emerald
    "Subnet": "#67e8f9",      # Light cyan
    "Node": "#c4b5fd",        # Lavender
    "Fault": "#fb923c",       # Orange
    "Advisory": "#fbbf24",    # Amber
    "Unknown": "#8b949e",     # Gray
}

# Shape mapping for each node type
NODE_SHAPES = {
    "Tenant": "box",
    "AppProfile": "ellipse",
    "EPG": "dot",
    "Fabric": "diamond",
    "Anomaly": "triangle",
    "HealthSummary": "star",
    "VRF": "hexagon",
    "BridgeDomain": "square",
    "Subnet": "dot",
    "Node": "database",
    "Fault": "triangleDown",
    "Advisory": "star",
    "Unknown": "dot",
}

# --- Initial suggestions ---
initial_suggestions = [
    "How many tenants are there and what VRFs do they have?",
    "Show me all fabric nodes and their roles (spine/leaf).",
    "What faults and anomalies are currently active?",
    "List all Bridge Domains and their subnets."
]

# Node type to query mapping
NODE_QUERY_TEMPLATES = {
    "Tenant": "Tell me about the tenant '{name}'. What application profiles, EPGs, VRFs, and Bridge Domains does it have? Are there any anomalies affecting it?",
    "AppProfile": "What EPGs belong to the application profile '{name}'? Show me its structure and any issues.",
    "EPG": "Describe the EPG '{name}'. What tenant and application profile does it belong to?",
    "Fabric": "What is the status of the fabric '{name}'? List any anomalies and advisories detected in this fabric.",
    "Anomaly": "Tell me more about this anomaly: '{name}'. What is its severity and which resources are affected? How can I fix it?",
    "HealthSummary": "Show me the health summary details for '{name}'.",
    "VRF": "Tell me about the VRF '{name}'. Which tenant does it belong to and what is its purpose in the network?",
    "BridgeDomain": "Describe the Bridge Domain '{name}'. What subnets are configured in it and which tenant owns it?",
    "Subnet": "What is the subnet '{name}'? Which Bridge Domain contains it and what is its scope?",
    "Node": "Show me details about the fabric node '{name}'. What is its role (spine/leaf), model, and current status? Are there any faults affecting it?",
    "Fault": "Tell me about this fault: '{name}'. What is its severity, cause, and which object is affected? How can I resolve it?",
    "Advisory": "Tell me about this advisory: '{name}'. What type is it (PSIRT, EOL, Field Notice)? How many devices are affected and what action should I take?",
    "Unknown": "Tell me about '{name}'."
}

def fetch_graph_data():
    """Fetch graph data from the backend API"""
    try:
        response = requests.get(GRAPH_URL, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching graph data: {e}")
        return {"nodes": [], "edges": []}

def generate_graph_html():
    """Generate 3D force graph visualization HTML with particle effects"""
    graph_data = fetch_graph_data()
    print(f"🎨 generate_graph_html called: {len(graph_data.get('nodes', []))} nodes, {len(graph_data.get('edges', []))} edges")

    # Debug: Show sample node types and their colors
    if graph_data.get('nodes'):
        sample_nodes = graph_data.get('nodes', [])[:5]
        print("📊 Sample nodes and their assigned colors:")
        for node in sample_nodes:
            ntype = node.get('type', 'Unknown')
            color = NODE_COLORS.get(ntype, NODE_COLORS['Unknown'])
            print(f"   {ntype:15s} -> {color}")

    if not graph_data.get("nodes"):
        return """
        <div style="display: flex; justify-content: center; align-items: center;
                    height: 500px; color: #666; font-size: 16px; background: #0d1117; border-radius: 8px;">
            <p style="color: #8b949e;">No graph data available. Run the ingestor to populate the database.</p>
        </div>
        """

    # Severity color mapping for anomalies
    ANOMALY_SEVERITY_COLORS = {
        "critical": "#f85149",
        "major": "#ff7b72",
        "minor": "#ffa657",
        "warning": "#d29922",
        "info": "#79c0ff",
    }

    # Prepare nodes for 3D graph
    graph_nodes = []
    for node in graph_data.get("nodes", []):
        node_type = node.get("type", "Unknown")
        props = node.get("properties", {})

        if node_type == "Anomaly":
            severity = props.get("severity", "").lower()
            color = ANOMALY_SEVERITY_COLORS.get(severity, "#ff7b72")
        else:
            color = NODE_COLORS.get(node_type, NODE_COLORS["Unknown"])

        # Size based on node type
        size = 12 if node_type == "Fabric" else 10 if node_type in ["Tenant", "Node"] else 8 if node_type == "Anomaly" else 6

        original_label = node.get("label", f"Node-{node['id']}")
        display_label = "Anomaly" if node_type == "Anomaly" else original_label

        graph_nodes.append({
            "id": node["id"],
            "name": display_label,
            "originalName": original_label,
            "nodeType": node_type,
            "color": color,
            "val": size,
            "severity": props.get("severity", "").lower() if node_type == "Anomaly" else None,
            "properties": props
        })

    # Prepare links for 3D graph
    graph_links = []
    for edge in graph_data.get("edges", []):
        graph_links.append({
            "source": edge["source"],
            "target": edge["target"],
            "type": edge.get("type", "")
        })

    # Generate JSON for 3D graph
    nodes_json = json.dumps(graph_nodes)
    links_json = json.dumps(graph_links)

    # Create the inner HTML with 3D force graph
    inner_html = f"""
<!DOCTYPE html>
<html>
<head>
    <script src="https://unpkg.com/three@0.149.0/build/three.min.js"></script>
    <script src="https://unpkg.com/3d-force-graph@1.73.3"></script>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
    <style>
        body {{
            margin: 0;
            padding: 0;
            overflow: hidden;
            background: #0d1117;
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
        }}
        #3d-graph {{
            width: 100vw;
            height: 100vh;
        }}
        #legend {{
            position: absolute;
            top: 56px;
            right: 12px;
            display: flex;
            flex-direction: column;
            gap: 8px;
            font-size: 11px;
            z-index: 100;
        }}
        .legend-row {{
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
        }}
        .legend-item {{
            display: flex;
            align-items: center;
            padding: 6px 10px;
            border-radius: 6px;
            cursor: pointer;
            background: rgba(33, 38, 45, 0.9);
            border: 1px solid rgba(240,246,252,0.1);
            color: #e6edf3;
            transition: all 0.2s ease;
            user-select: none;
            font-weight: 500;
        }}
        .legend-item:hover {{
            background: rgba(0, 212, 255, 0.2);
            border-color: #00d4ff;
        }}
        .legend-item.inactive {{
            opacity: 0.35;
        }}
        .legend-shape {{
            width: 16px;
            height: 16px;
            margin-right: 6px;
            filter: drop-shadow(0 0 3px currentColor);
        }}
        #click-hint {{
            position: absolute;
            bottom: 50px;
            left: 50%;
            transform: translateX(-50%);
            background: rgba(33, 38, 45, 0.9);
            color: #8b949e;
            padding: 8px 14px;
            border-radius: 6px;
            font-size: 11px;
            border: 1px solid rgba(240,246,252,0.1);
            white-space: nowrap;
        }}
        #reset-btn {{
            position: absolute;
            bottom: 50px;
            right: 12px;
            background: linear-gradient(135deg, #00d4ff 0%, #0088cc 100%);
            color: #fff;
            padding: 8px 16px;
            border-radius: 6px;
            font-size: 12px;
            font-weight: 600;
            border: none;
            cursor: pointer;
            display: none;
            z-index: 200;
            box-shadow: 0 4px 12px rgba(0, 212, 255, 0.3);
        }}
        #reset-btn:hover {{
            transform: translateY(-2px);
            box-shadow: 0 6px 20px rgba(0, 212, 255, 0.4);
        }}
        .anomaly-group {{
            display: flex;
            align-items: center;
            padding: 8px 10px;
            border: 1px solid rgba(248, 81, 73, 0.3);
            border-radius: 8px;
            background: rgba(248, 81, 73, 0.08);
            gap: 8px;
        }}
        .anomaly-group-title {{
            font-size: 9px;
            color: #f85149;
            text-transform: uppercase;
            writing-mode: vertical-rl;
            transform: rotate(180deg);
            font-weight: 600;
        }}
        .anomaly-items {{
            display: flex;
            flex-wrap: wrap;
            gap: 4px;
        }}
        /* Force tooltip wrapper to be fully transparent */
        .float-tooltip-kap,
        .scene-tooltip, .graph-tooltip,
        div[class*="tooltip"],
        #3d-graph > div:not(canvas) {{
            background: transparent !important;
            border: none !important;
            box-shadow: none !important;
            padding: 0 !important;
            margin: 0 !important;
            max-width: none !important;
            border-radius: 0 !important;
            font: inherit !important;
            color: inherit !important;
        }}
        /* Light mode overrides */
        body.light-mode {{
            background: #d5dce8;
        }}
        body.light-mode .legend-item {{
            background: rgba(255, 255, 255, 0.9);
            border: 1px solid rgba(9, 105, 218, 0.12);
            color: #1f2328;
            box-shadow: 0 2px 8px rgba(9, 105, 218, 0.1);
        }}
        body.light-mode .legend-item:hover {{
            background: rgba(9, 105, 218, 0.12);
            border-color: #0969da;
        }}
        body.light-mode .legend-item.inactive {{
            opacity: 0.35;
        }}
        body.light-mode .legend-shape {{
            filter: drop-shadow(0 0 3px rgba(9, 105, 218, 0.4));
        }}
        body.light-mode #click-hint {{
            background: rgba(255, 255, 255, 0.9);
            color: #656d76;
            border: 1px solid rgba(9, 105, 218, 0.12);
            box-shadow: 0 2px 8px rgba(9, 105, 218, 0.1);
        }}
        body.light-mode #reset-btn {{
            background: linear-gradient(135deg, #0969da 0%, #0550ae 100%);
            box-shadow: 0 4px 12px rgba(9, 105, 218, 0.3);
        }}
        body.light-mode .anomaly-group {{
            border: 1px solid rgba(248, 81, 73, 0.2);
            background: rgba(248, 81, 73, 0.05);
        }}
    </style>
</head>
<body>
    <div id="3d-graph"></div>
    <div id="legend">
        <div class="legend-row">
            <div class="legend-item" data-type="Fabric">
                <svg width="16" height="16" viewBox="0 0 16 16" class="legend-shape">
                    <polygon points="8,1 15,8 8,15 1,8" fill="#f0883e"/>
                </svg>Fabric
            </div>
            <div class="legend-item" data-type="Tenant">
                <svg width="16" height="16" viewBox="0 0 16 16" class="legend-shape">
                    <rect x="2" y="2" width="12" height="12" fill="#00d4ff"/>
                </svg>Tenant
            </div>
            <div class="legend-item" data-type="Node">
                <svg width="16" height="16" viewBox="0 0 16 16" class="legend-shape">
                    <rect x="3" y="2" width="10" height="12" rx="3" fill="#c4b5fd"/>
                </svg>Node
            </div>
            <div class="legend-item" data-type="VRF">
                <svg width="16" height="16" viewBox="0 0 16 16" class="legend-shape">
                    <polygon points="8,1 14,4 14,12 8,15 2,12 2,4" fill="#818cf8"/>
                </svg>VRF
            </div>
            <div class="legend-item" data-type="BridgeDomain">
                <svg width="16" height="16" viewBox="0 0 16 16" class="legend-shape">
                    <rect x="1" y="5" width="14" height="6" fill="#34d399"/>
                </svg>BD
            </div>
            <div class="legend-item" data-type="Subnet">
                <svg width="16" height="16" viewBox="0 0 16 16" class="legend-shape">
                    <circle cx="8" cy="8" r="5" fill="#67e8f9"/>
                </svg>Subnet
            </div>
            <div class="legend-item" data-type="AppProfile">
                <svg width="16" height="16" viewBox="0 0 16 16" class="legend-shape">
                    <polygon points="8,1 13,4 13,11 8,15 3,11 3,4" fill="#00e5a0"/>
                </svg>App
            </div>
            <div class="legend-item" data-type="EPG">
                <svg width="16" height="16" viewBox="0 0 16 16" class="legend-shape">
                    <circle cx="8" cy="8" r="6" fill="#00c9b7"/>
                </svg>EPG
            </div>
            <div class="legend-item inactive" data-type="Orphans" id="orphans-toggle">
                <svg width="16" height="16" viewBox="0 0 16 16" class="legend-shape">
                    <circle cx="8" cy="8" r="5" fill="none" stroke="#8b949e" stroke-width="2" stroke-dasharray="2,2"/>
                </svg>Orphans
            </div>
        </div>
        <div class="legend-row">
            <div class="anomaly-group">
                <span class="anomaly-group-title">Issues</span>
                <div class="anomaly-items">
                    <div class="legend-item" data-type="Anomaly" data-severity="critical">
                        <svg width="16" height="16" viewBox="0 0 16 16" class="legend-shape">
                            <polygon points="8,1 15,14 1,14" fill="#f85149"/>
                        </svg>Critical
                    </div>
                    <div class="legend-item" data-type="Anomaly" data-severity="major">
                        <svg width="16" height="16" viewBox="0 0 16 16" class="legend-shape">
                            <polygon points="8,1 15,14 1,14" fill="#ff7b72"/>
                        </svg>Major
                    </div>
                    <div class="legend-item" data-type="Anomaly" data-severity="minor">
                        <svg width="16" height="16" viewBox="0 0 16 16" class="legend-shape">
                            <polygon points="8,1 15,14 1,14" fill="#ffa657"/>
                        </svg>Minor
                    </div>
                    <div class="legend-item" data-type="Anomaly" data-severity="warning">
                        <svg width="16" height="16" viewBox="0 0 16 16" class="legend-shape">
                            <polygon points="8,1 15,14 1,14" fill="#d29922"/>
                        </svg>Warning
                    </div>
                    <div class="legend-item" data-type="Fault">
                        <svg width="16" height="16" viewBox="0 0 16 16" class="legend-shape">
                            <polygon points="8,14 1,2 15,2" fill="#fb923c"/>
                        </svg>Fault
                    </div>
                    <div class="legend-item" data-type="Advisory">
                        <svg width="16" height="16" viewBox="0 0 16 16" class="legend-shape">
                            <circle cx="8" cy="8" r="5" fill="none" stroke="#fbbf24" stroke-width="3"/>
                        </svg>Advisory
                    </div>
                </div>
            </div>
        </div>
    </div>
    <button id="reset-btn">Reset View</button>
    <div id="click-hint">Click node to explore | Left-drag to rotate | Right-drag to pan | Scroll to zoom</div>
    <script>
        // Theme support
        var currentTheme = 'dark';
        var themeColors = {{
            dark: {{
                bg: '#0d1117',
                tooltipBg: 'linear-gradient(180deg, rgba(34,40,52,0.97) 0%, rgba(16,20,28,0.98) 100%)',
                tooltipBgFallback: 'rgba(22,27,34,0.97)',
                tooltipBorderTop: 'rgba(100,200,255,0.30)',
                tooltipBorderSide: 'rgba(240,246,252,0.10)',
                tooltipBorderBottom: 'rgba(0,0,0,0.5)',
                tooltipDropShadow: 'drop-shadow(0 4px 8px rgba(0,0,0,0.5)) drop-shadow(0 12px 32px rgba(0,0,0,0.4))',
                tooltipBadgeBg: 'rgba(0, 212, 255, 0.15)',
                tooltipBadgeText: '#00d4ff',
                tooltipAccent: '#00d4ff',
                tooltipTitle: '#fff',
                tooltipSep: 'rgba(240,246,252,0.1)',
                tooltipLabel: '#8b949e',
                tooltipValue: '#e6edf3',
                labelBg: 'rgba(13, 17, 23, 0.8)',
                labelColor: '#e6edf3',
                linkColor: 'rgba(0, 212, 255, 0.35)',
                particleColor: '#00d4ff'
            }},
            light: {{
                bg: '#d5dce8',
                tooltipBg: 'linear-gradient(180deg, rgba(255,255,255,0.98) 0%, rgba(230,236,248,0.97) 100%)',
                tooltipBgFallback: 'rgba(248,250,255,0.97)',
                tooltipBorderTop: 'rgba(255,255,255,0.95)',
                tooltipBorderSide: 'rgba(0,40,100,0.10)',
                tooltipBorderBottom: 'rgba(0,30,80,0.20)',
                tooltipDropShadow: 'drop-shadow(0 4px 8px rgba(0,40,100,0.12)) drop-shadow(0 12px 32px rgba(0,30,80,0.10))',
                tooltipBadgeBg: 'rgba(5, 80, 174, 0.12)',
                tooltipBadgeText: '#0550ae',
                tooltipAccent: '#0550ae',
                tooltipTitle: '#1a1a2e',
                tooltipSep: 'rgba(0,0,0,0.1)',
                tooltipLabel: '#4a5568',
                tooltipValue: '#1a1a2e',
                labelBg: 'rgba(255, 255, 255, 0.92)',
                labelColor: '#1a1a2e',
                linkColor: 'rgba(0, 90, 180, 0.35)',
                particleColor: '#0969da'
            }}
        }};

        // Graph data
        var graphData = {{
            nodes: {nodes_json},
            links: {links_json}
        }};
        var allNodes = graphData.nodes;
        var allLinks = graphData.links;

        // THREE.js is loaded globally

        // Function to create text sprite using canvas
        function createTextSprite(text, color) {{
            var canvas = document.createElement('canvas');
            var context = canvas.getContext('2d');
            canvas.width = 256;
            canvas.height = 64;

            // Background
            var tc = themeColors[currentTheme];
            context.fillStyle = tc.labelBg;
            context.roundRect(0, 0, canvas.width, canvas.height, 8);
            context.fill();

            // Text
            context.font = 'bold 24px Inter, Arial, sans-serif';
            context.fillStyle = color || tc.labelColor;
            context.textAlign = 'center';
            context.textBaseline = 'middle';

            // Truncate text if too long
            var displayText = text.length > 20 ? text.substring(0, 18) + '...' : text;
            context.fillText(displayText, canvas.width / 2, canvas.height / 2);

            var texture = new THREE.CanvasTexture(canvas);
            var material = new THREE.SpriteMaterial({{ map: texture, transparent: true }});
            var sprite = new THREE.Sprite(material);
            sprite.scale.set(20, 5, 1);
            return sprite;
        }}

        // Function to create custom 3D shape based on node type
        function createNodeObject(node) {{
            var geometry, material, mesh;
            var size = node.val || 6;
            var color = new THREE.Color(node.color);

            // Create material with bright colors (no lighting required)
            material = new THREE.MeshBasicMaterial({{
                color: color,
                transparent: true,
                opacity: 0.9
            }});

            // Select geometry based on node type
            switch(node.nodeType) {{
                case 'Fabric':
                    // Octahedron (diamond shape) - prominent center
                    geometry = new THREE.OctahedronGeometry(size * 1.2);
                    break;
                case 'Tenant':
                    // Box (cube) - structural element
                    geometry = new THREE.BoxGeometry(size * 1.5, size * 1.5, size * 1.5);
                    break;
                case 'Node':
                    // Cylinder - physical device
                    geometry = new THREE.CylinderGeometry(size * 0.8, size * 0.8, size * 1.5, 8);
                    break;
                case 'VRF':
                    // Icosahedron - complex routing
                    geometry = new THREE.IcosahedronGeometry(size);
                    break;
                case 'BridgeDomain':
                    // Box - container
                    geometry = new THREE.BoxGeometry(size * 1.2, size * 0.8, size * 1.2);
                    break;
                case 'AppProfile':
                    // Dodecahedron - application
                    geometry = new THREE.DodecahedronGeometry(size);
                    break;
                case 'EPG':
                    // Sphere - endpoint group
                    geometry = new THREE.SphereGeometry(size * 0.8, 16, 16);
                    break;
                case 'Subnet':
                    // Small sphere - network segment
                    geometry = new THREE.SphereGeometry(size * 0.6, 12, 12);
                    break;
                case 'Anomaly':
                    // Tetrahedron (triangle/warning) - alert
                    geometry = new THREE.TetrahedronGeometry(size * 1.2);
                    break;
                case 'Fault':
                    // Cone - warning indicator
                    geometry = new THREE.ConeGeometry(size * 0.8, size * 1.5, 6);
                    break;
                case 'Advisory':
                    // Torus (ring) - information
                    geometry = new THREE.TorusGeometry(size * 0.7, size * 0.3, 8, 16);
                    break;
                case 'HealthSummary':
                    // Star-like (octahedron stretched)
                    geometry = new THREE.OctahedronGeometry(size);
                    break;
                default:
                    // Default sphere
                    geometry = new THREE.SphereGeometry(size * 0.7, 16, 16);
            }}

            mesh = new THREE.Mesh(geometry, material);

            // Create group to hold shape and label
            var group = new THREE.Group();
            group.add(mesh);

            // Add text label below the shape
            var label = createTextSprite(node.name, themeColors[currentTheme].labelColor);
            label.position.set(0, -size * 2, 0);
            group.add(label);

            return group;
        }}

        // Create 3D Force Graph
        var Graph = ForceGraph3D()
            (document.getElementById('3d-graph'))
            .graphData(graphData)
            .backgroundColor('#0d1117')
            .nodeLabel(node => {{
                // HTML tooltip on hover - 3D styled card
                var tc = themeColors[currentTheme];
                // Outer wrapper with drop-shadow
                var html = '<div style="filter: ' + tc.tooltipDropShadow + ';">';
                // Inner card with gradient, borders, highlight
                html += '<div style="'
                    + 'background: ' + tc.tooltipBgFallback + ';'
                    + 'background: ' + tc.tooltipBg + ';'
                    + 'padding: 14px 18px;'
                    + 'border-radius: 12px;'
                    + 'border-top: 1.5px solid ' + tc.tooltipBorderTop + ';'
                    + 'border-left: 1px solid ' + tc.tooltipBorderSide + ';'
                    + 'border-right: 1px solid ' + tc.tooltipBorderSide + ';'
                    + 'border-bottom: 2.5px solid ' + tc.tooltipBorderBottom + ';'
                    + 'max-width: 320px;'
                    + '">';
                // Node type badge with accent background
                html += '<div style="display:inline-block; color: ' + tc.tooltipBadgeText + '; font-size: 9px; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px; padding: 3px 10px; border-radius: 6px; background: ' + tc.tooltipBadgeBg + '; font-weight: 700;">' + node.nodeType + '</div>';
                // Node name
                html += '<div style="color: ' + tc.tooltipTitle + '; font-weight: 700; font-size: 15px; margin-bottom: 10px; padding-bottom: 9px; border-bottom: 1px solid ' + tc.tooltipSep + ';">' + (node.originalName || node.name) + '</div>';
                if (node.properties) {{
                    for (var key in node.properties) {{
                        if (node.properties[key] !== null && node.properties[key] !== '') {{
                            var val = node.properties[key];
                            if (typeof val === 'string' && val.length > 40) val = val.substring(0, 40) + '...';
                            html += '<div style="font-size: 11px; margin: 3px 0; line-height: 1.5;"><span style="color: ' + tc.tooltipLabel + '; font-weight: 500;">' + key + ':</span> <span style="color: ' + tc.tooltipValue + '; font-weight: 400;">' + val + '</span></div>';
                        }}
                    }}
                }}
                html += '</div></div>';
                return html;
            }})
            .nodeThreeObject(node => createNodeObject(node))
            .linkColor(() => 'rgba(0, 212, 255, 0.35)')
            .linkOpacity(0.6)
            .linkWidth(1.5)
            .linkDirectionalParticles(2)
            .linkDirectionalParticleSpeed(0.006)
            .linkDirectionalParticleWidth(2)
            .linkDirectionalParticleColor(() => '#00d4ff')
            .onNodeHover(node => {{
                document.body.style.cursor = node ? 'pointer' : 'default';
            }})
            .onNodeClick(node => {{
                if (node) {{
                    // Focus on node with animation
                    var distance = 120;
                    var distRatio = 1 + distance / Math.hypot(node.x, node.y, node.z);
                    Graph.cameraPosition(
                        {{ x: node.x * distRatio, y: node.y * distRatio, z: node.z * distRatio }},
                        node,
                        2000
                    );

                    // Send message to parent
                    window.parent.postMessage({{
                        type: 'nodeClick',
                        nodeId: node.id,
                        nodeLabel: node.originalName || node.name,
                        nodeType: node.nodeType,
                        properties: node.properties || {{}}
                    }}, '*');
                }}
            }});

        // Configure forces to spread nodes apart
        Graph.d3Force('charge').strength(-150);  // Stronger repulsion
        Graph.d3Force('link').distance(80);      // Longer links

        // Add custom force to spread Fabric nodes apart from each other
        Graph.d3Force('fabricSpread', function(alpha) {{
            var fabricNodes = graphData.nodes.filter(function(n) {{ return n.nodeType === 'Fabric'; }});
            if (fabricNodes.length < 2) return;

            // Push fabrics away from each other
            var minDistance = 200;  // Minimum distance between fabrics
            for (var i = 0; i < fabricNodes.length; i++) {{
                for (var j = i + 1; j < fabricNodes.length; j++) {{
                    var n1 = fabricNodes[i];
                    var n2 = fabricNodes[j];
                    var dx = (n2.x || 0) - (n1.x || 0);
                    var dy = (n2.y || 0) - (n1.y || 0);
                    var dz = (n2.z || 0) - (n1.z || 0);
                    var dist = Math.sqrt(dx * dx + dy * dy + dz * dz) || 1;

                    if (dist < minDistance) {{
                        var k = alpha * (minDistance - dist) / dist * 0.5;
                        n1.vx = (n1.vx || 0) - dx * k;
                        n1.vy = (n1.vy || 0) - dy * k;
                        n1.vz = (n1.vz || 0) - dz * k;
                        n2.vx = (n2.vx || 0) + dx * k;
                        n2.vy = (n2.vy || 0) + dy * k;
                        n2.vz = (n2.vz || 0) + dz * k;
                    }}
                }}
            }}
        }});

        // Compute fabric-connected nodes using BFS
        function computeFabricConnectedNodes() {{
            var connectedIds = new Set();
            var queue = [];

            // Build adjacency list
            var adjacency = {{}};
            allNodes.forEach(function(node) {{
                adjacency[node.id] = [];
            }});
            allLinks.forEach(function(link) {{
                var sourceId = typeof link.source === 'object' ? link.source.id : link.source;
                var targetId = typeof link.target === 'object' ? link.target.id : link.target;
                if (adjacency[sourceId]) adjacency[sourceId].push(targetId);
                if (adjacency[targetId]) adjacency[targetId].push(sourceId);
            }});

            // Start BFS from all Fabric nodes
            allNodes.forEach(function(node) {{
                if (node.nodeType === 'Fabric') {{
                    queue.push(node.id);
                    connectedIds.add(node.id);
                }}
            }});

            // BFS traversal
            while (queue.length > 0) {{
                var currentId = queue.shift();
                var neighbors = adjacency[currentId] || [];
                neighbors.forEach(function(neighborId) {{
                    if (!connectedIds.has(neighborId)) {{
                        connectedIds.add(neighborId);
                        queue.push(neighborId);
                    }}
                }});
            }}

            return connectedIds;
        }}

        // Mark orphan nodes
        var fabricConnectedIds = computeFabricConnectedNodes();
        allNodes.forEach(function(node) {{
            node.isOrphan = !fabricConnectedIds.has(node.id);
        }});

        // Legend filtering
        var activeFilters = new Set();
        // Orphans hidden by default
        activeFilters.add('Orphans');

        // Apply initial filter (hide orphans)
        function applyFilters() {{
            var showOrphans = !activeFilters.has('Orphans');

            var filteredNodes = allNodes.filter(function(node) {{
                // Check orphan filter
                if (!showOrphans && node.isOrphan) {{
                    return false;
                }}
                // Check type filters
                if (node.nodeType === 'Anomaly' && node.severity) {{
                    return !activeFilters.has('Anomaly-' + node.severity);
                }}
                return !activeFilters.has(node.nodeType);
            }});

            var filteredNodeIds = new Set(filteredNodes.map(n => n.id));
            var filteredLinks = allLinks.filter(function(link) {{
                var sourceId = typeof link.source === 'object' ? link.source.id : link.source;
                var targetId = typeof link.target === 'object' ? link.target.id : link.target;
                return filteredNodeIds.has(sourceId) && filteredNodeIds.has(targetId);
            }});

            Graph.graphData({{ nodes: filteredNodes, links: filteredLinks }});
            document.getElementById('reset-btn').style.display = activeFilters.size > 0 ? 'block' : 'none';
        }}

        // Apply initial filter
        applyFilters();

        document.querySelectorAll('.legend-item').forEach(function(item) {{
            item.addEventListener('click', function() {{
                var type = this.getAttribute('data-type');
                var severity = this.getAttribute('data-severity');
                var filterKey = severity ? 'Anomaly-' + severity : type;

                if (activeFilters.has(filterKey)) {{
                    activeFilters.delete(filterKey);
                    this.classList.remove('inactive');
                }} else {{
                    activeFilters.add(filterKey);
                    this.classList.add('inactive');
                }}

                applyFilters();
            }});
        }});

        // Reset button - resets to default state (orphans hidden) and resets camera
        document.getElementById('reset-btn').addEventListener('click', function() {{
            // Reset filters to default (only orphans hidden)
            activeFilters.clear();
            activeFilters.add('Orphans');  // Keep orphans hidden by default
            document.querySelectorAll('.legend-item').forEach(function(item) {{
                if (item.getAttribute('data-type') === 'Orphans') {{
                    item.classList.add('inactive');
                }} else {{
                    item.classList.remove('inactive');
                }}
            }});
            applyFilters();

            // Reset camera view - zoom to fit all visible nodes
            setTimeout(function() {{
                Graph.zoomToFit(400, 50);  // 400ms animation, 50px padding
            }}, 100);
        }});

        // Issues group toggle
        document.querySelector('.anomaly-group-title').addEventListener('click', function() {{
            var items = document.querySelectorAll('.anomaly-group .legend-item');
            var allHidden = Array.from(items).every(item => item.classList.contains('inactive'));

            items.forEach(function(item) {{
                var severity = item.getAttribute('data-severity');
                var type = item.getAttribute('data-type');
                var filterKey = severity ? 'Anomaly-' + severity : type;

                if (allHidden) {{
                    activeFilters.delete(filterKey);
                    item.classList.remove('inactive');
                }} else {{
                    activeFilters.add(filterKey);
                    item.classList.add('inactive');
                }}
            }});

            applyFilters();
        }});

        // Theme change listener - receives messages from parent page
        window.addEventListener('message', function(event) {{
            if (event.data && event.data.type === 'themeChange') {{
                currentTheme = event.data.theme;
                var tc = themeColors[currentTheme];
                var isLight = currentTheme === 'light';

                // Update body class for CSS overrides
                if (isLight) {{
                    document.body.classList.add('light-mode');
                }} else {{
                    document.body.classList.remove('light-mode');
                }}

                // Update graph background
                document.body.style.background = tc.bg;
                Graph.backgroundColor(tc.bg);

                // Update link colors
                Graph.linkColor(function() {{ return tc.linkColor; }});
                Graph.linkDirectionalParticleColor(function() {{ return tc.particleColor; }});

                // Regenerate node objects for updated label colors
                Graph.nodeThreeObject(function(node) {{ return createNodeObject(node); }});
            }}
        }});

        // Check parent theme on initial load
        setTimeout(function() {{
            try {{
                if (window.parent && window.parent.document.documentElement.classList.contains('light-mode')) {{
                    currentTheme = 'light';
                    var tc = themeColors[currentTheme];
                    document.body.classList.add('light-mode');
                    document.body.style.background = tc.bg;
                    Graph.backgroundColor(tc.bg);
                    Graph.linkColor(function() {{ return tc.linkColor; }});
                    Graph.linkDirectionalParticleColor(function() {{ return tc.particleColor; }});
                    Graph.nodeThreeObject(function(node) {{ return createNodeObject(node); }});
                }}
            }} catch(e) {{}}
        }}, 500);

        // Strip float-tooltip-kap wrapper styling via MutationObserver
        // The 3d-force-graph lib uses float-tooltip which injects its own
        // stylesheet with background/padding/border on .float-tooltip-kap.
        // CSS !important may lose to the injected sheet, so we strip inline.
        (function() {{
            var graphEl = document.getElementById('3d-graph');
            if (!graphEl) return;
            function stripTooltipWrapper(el) {{
                el.style.setProperty('background', 'transparent', 'important');
                el.style.setProperty('border', 'none', 'important');
                el.style.setProperty('box-shadow', 'none', 'important');
                el.style.setProperty('padding', '0', 'important');
                el.style.setProperty('margin', '0', 'important');
                el.style.setProperty('max-width', 'none', 'important');
                el.style.setProperty('border-radius', '0', 'important');
                el.style.setProperty('font', 'inherit', 'important');
                el.style.setProperty('color', 'inherit', 'important');
            }}
            // Watch for tooltip div being added or changed
            var obs = new MutationObserver(function(mutations) {{
                var tt = graphEl.querySelector('.float-tooltip-kap');
                if (tt) stripTooltipWrapper(tt);
                // Also catch any div that isn't the canvas
                graphEl.querySelectorAll(':scope > div').forEach(function(d) {{
                    if (!d.querySelector('canvas') && !d.id) {{
                        stripTooltipWrapper(d);
                    }}
                }});
            }});
            obs.observe(graphEl, {{ childList: true, subtree: true, attributes: true, attributeFilter: ['style', 'class'] }});
            // Also strip on first paint
            setTimeout(function() {{
                var tt = graphEl.querySelector('.float-tooltip-kap');
                if (tt) stripTooltipWrapper(tt);
                graphEl.querySelectorAll(':scope > div').forEach(function(d) {{
                    if (!d.querySelector('canvas') && !d.id) {{
                        stripTooltipWrapper(d);
                    }}
                }});
            }}, 1000);
        }})();
    </script>
</body>
</html>
"""
    # Escape HTML for srcdoc attribute
    escaped_html = html.escape(inner_html, quote=True)

    # Wrap in iframe using srcdoc - allow-same-origin needed for postMessage to work
    return f'<iframe srcdoc="{escaped_html}" style="width: 100vw; height: 100vh; border: none;" sandbox="allow-scripts allow-same-origin" id="graph-iframe"></iframe>'

def get_agent_response_streaming(question, history, source="user"):
    """Get answer from backend with streaming progress updates

    Args:
        question: The question to ask
        history: Chat history
        source: Source of the query - "user", "suggestion", or "graph_click"
    """
    history = history or []
    # Convert messages format to list of [question, answer] pairs for backend
    history_pairs = []
    i = 0
    while i < len(history):
        if history[i].get("role") == "user":
            user_msg = history[i].get("content", "")
            assistant_msg = ""
            if i + 1 < len(history) and history[i + 1].get("role") == "assistant":
                assistant_msg = history[i + 1].get("content", "")
                i += 1
            history_pairs.append([user_msg, assistant_msg])
        i += 1

    # Add user message immediately
    history.append({"role": "user", "content": question})

    try:
        # Use streaming endpoint
        response = requests.post(
            BACKEND_STREAM_URL,
            json={"question": question, "chat_history": history_pairs, "source": source},
            stream=True,
            timeout=180  # Increased timeout for detailed responses
        )
        response.raise_for_status()

        answer = ""
        progress_messages = []  # Track all progress messages
        current_phase = ""  # Track current processing phase

        # Process SSE stream
        for line in response.iter_lines():
            if line:
                line_text = line.decode('utf-8')
                if line_text.startswith('data: '):
                    try:
                        data = json.loads(line_text[6:])
                        status = data.get('status', '')
                        message = data.get('message', '')

                        if status == 'complete':
                            answer = data.get('answer', 'No answer received.')
                            history.append({"role": "assistant", "content": answer})
                            yield history, "", question, answer
                        elif status == 'section':
                            # Section progress - add as indented sub-item
                            progress_messages.append(f"   {message}")
                            progress_text = "\n".join(progress_messages)
                            temp_history = history + [{"role": "assistant", "content": f"*{progress_text}*"}]
                            yield temp_history, "", question, ""
                        else:
                            # Main phase progress (thinking, generating, querying, etc.)
                            if message != current_phase:
                                current_phase = message
                                progress_messages.append(message)
                            progress_text = "\n".join(progress_messages)
                            temp_history = history + [{"role": "assistant", "content": f"*{progress_text}*"}]
                            yield temp_history, "", question, ""
                    except json.JSONDecodeError:
                        pass

    except requests.exceptions.RequestException as e:
        answer = f"Error contacting the backend: {e}. Is the backend running at {BACKEND_STREAM_URL}?"
        history.append({"role": "assistant", "content": answer})
        yield history, "", question, answer
    except Exception as e:
        answer = f"An unexpected error occurred in the frontend: {e}"
        history.append({"role": "assistant", "content": answer})
        yield history, "", question, answer


def get_agent_response(question, history):
    """Get answer from backend (non-streaming fallback)"""
    history = history or []
    history_pairs = []
    i = 0
    while i < len(history):
        if history[i].get("role") == "user":
            user_msg = history[i].get("content", "")
            assistant_msg = ""
            if i + 1 < len(history) and history[i + 1].get("role") == "assistant":
                assistant_msg = history[i + 1].get("content", "")
                i += 1
            history_pairs.append([user_msg, assistant_msg])
        i += 1

    try:
        response = requests.post(
            BACKEND_URL,
            json={"question": question, "chat_history": history_pairs}
        )
        response.raise_for_status()
        data = response.json()
        answer = data.get("answer", "No valid answer received.")
    except Exception as e:
        answer = f"Error: {e}"

    history.append({"role": "user", "content": question})
    history.append({"role": "assistant", "content": answer})
    return history, "", question, answer


def fetch_suggestions(question, answer):
    """Fetch suggestions asynchronously after answer is displayed"""
    if not question or not answer:
        return initial_suggestions

    try:
        response = requests.post(
            SUGGESTIONS_URL,
            json={"question": question, "answer": answer},
            timeout=30
        )
        response.raise_for_status()
        data = response.json()
        suggestions = data.get("suggestions", [])
        if suggestions:
            return suggestions
    except Exception as e:
        print(f"Error fetching suggestions: {e}")

    return initial_suggestions

def generate_node_query(node_data_json):
    """Generate a contextual query based on clicked node"""
    if not node_data_json:
        return None

    try:
        node_data = json.loads(node_data_json)
        node_type = node_data.get("nodeType", "Unknown")
        node_label = node_data.get("nodeLabel", "Unknown")

        template = NODE_QUERY_TEMPLATES.get(node_type, NODE_QUERY_TEMPLATES["Unknown"])
        return template.format(name=node_label)
    except (json.JSONDecodeError, KeyError):
        return None

def refresh_graph():
    """Refresh the graph visualization"""
    return generate_graph_html()

# --- Custom CSS ---
custom_css = """
/* === FULLSCREEN IMMERSIVE LAYOUT === */

/* Kill Gradio defaults - no scrollbars, no padding, no borders */
html, body {
    margin: 0 !important;
    padding: 0 !important;
    overflow: hidden !important;
    background: #0d1117 !important;
    height: 100vh !important;
}
.gradio-container {
    max-width: 100% !important;
    padding: 0 !important;
    margin: 0 !important;
    background: transparent !important;
    overflow: hidden !important;
    height: 100vh !important;
}
.main, .wrap, .contain {
    padding: 0 !important;
    gap: 0 !important;
    overflow: hidden !important;
}
footer {
    display: none !important;
}

/* === FULLSCREEN GRAPH BACKGROUND === */
#graph-bg {
    position: fixed !important;
    top: 0 !important;
    left: 0 !important;
    width: 100vw !important;
    height: 100vh !important;
    z-index: 0 !important;
    border: none !important;
    padding: 0 !important;
    margin: 0 !important;
    overflow: hidden !important;
}
#graph-bg iframe {
    width: 100vw !important;
    height: 100vh !important;
    border: none !important;
}
#graph-bg > div {
    padding: 0 !important;
    margin: 0 !important;
}

/* === TRANSPARENT OVERLAY CONTAINER === */
#overlay-container {
    position: fixed !important;
    top: 0 !important;
    left: 0 !important;
    width: 100vw !important;
    height: 100vh !important;
    z-index: 10 !important;
    pointer-events: none !important;
    background: transparent !important;
    border: none !important;
    padding: 0 !important;
    display: flex !important;
    flex-direction: column !important;
    overflow: hidden !important;
}

/* === HEADER BAR === */
#header-bar {
    pointer-events: auto !important;
    flex-shrink: 0 !important;
    padding: 0 !important;
}
#header-bar > div {
    padding: 0 !important;
}
.header-inner {
    background: rgba(13, 17, 23, 0.8);
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    border-bottom: 1px solid rgba(0, 212, 255, 0.2);
    padding: 10px 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
}
.header-title {
    font-size: 18px;
    font-weight: 700;
    color: #e6edf3;
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    display: flex;
    align-items: center;
    gap: 10px;
}
.header-accent {
    color: #00d4ff;
}
.header-subtitle {
    font-size: 11px;
    color: #8b949e;
    font-weight: 400;
}

/* === GLASSMORPHIC CHAT PANEL (draggable + resizable) === */
#chat-panel {
    pointer-events: auto !important;
    position: absolute !important;
    top: 52px !important;
    left: 16px !important;
    width: 420px !important;
    height: calc(100vh - 94px) !important;
    background: rgba(13, 17, 23, 0.25) !important;
    backdrop-filter: blur(12px) !important;
    -webkit-backdrop-filter: blur(12px) !important;
    border: 1px solid rgba(0, 212, 255, 0.15) !important;
    border-radius: 12px !important;
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4), inset 0 1px 0 rgba(0, 212, 255, 0.05) !important;
    padding: 0 14px 12px 14px !important;
    display: flex !important;
    flex-direction: column !important;
    gap: 8px !important;
    overflow: hidden !important;
    min-width: 300px !important;
    min-height: 250px !important;
    max-width: 90vw !important;
    max-height: calc(100vh - 60px) !important;
}
/* Drag handle at top of chat panel */
.chat-drag-handle {
    cursor: grab;
    padding: 8px 0 4px 0;
    text-align: center;
    flex-shrink: 0;
    user-select: none;
    -webkit-user-select: none;
}
.chat-drag-handle:active { cursor: grabbing; }
.chat-drag-handle .drag-dots {
    display: inline-block;
    width: 40px;
    height: 4px;
    border-radius: 2px;
    background: rgba(0, 212, 255, 0.3);
    transition: background 0.2s;
}
.chat-drag-handle:hover .drag-dots {
    background: rgba(0, 212, 255, 0.6);
}

/* Resize grip (bottom-right corner, injected via JS) */
.resize-grip {
    position: absolute !important;
    bottom: 2px !important;
    right: 2px !important;
    width: 22px !important;
    height: 22px !important;
    cursor: nwse-resize !important;
    z-index: 100 !important;
    pointer-events: auto !important;
    background: transparent !important;
    border-radius: 0 0 10px 0 !important;
}
.resize-grip::after {
    content: '';
    position: absolute;
    bottom: 4px;
    right: 4px;
    width: 10px;
    height: 10px;
    border-right: 2px solid rgba(0, 212, 255, 0.3);
    border-bottom: 2px solid rgba(0, 212, 255, 0.3);
    pointer-events: none;
}
.resize-grip:hover::after {
    border-color: rgba(0, 212, 255, 0.7);
}

/* === CHATBOT === */
#chatbot,
#chatbot *:not([data-testid="bot"]):not([data-testid="user"]):not(.message):not(.bot):not(.user) {
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
}
#chatbot {
    flex: 1 !important;
    min-height: 0 !important;
    max-height: none !important;
    overflow-y: auto !important;
}
/* Bot bubbles - 3D cyan glow */
#chatbot [data-testid="bot"],
#chatbot .bot,
#chatbot .message.bot {
    background: rgba(0, 212, 255, 0.40) !important;
    border: 1px solid rgba(0, 212, 255, 0.50) !important;
    border-radius: 14px 14px 14px 4px !important;
    box-shadow: 0 2px 8px rgba(0, 212, 255, 0.15), inset 0 1px 0 rgba(255,255,255,0.05) !important;
    color: #e6edf3 !important;
    font-size: 13px !important;
    line-height: 1.5 !important;
    padding: 10px 14px !important;
}
/* User bubbles - 3D subtle white glow */
#chatbot [data-testid="user"],
#chatbot .user,
#chatbot .message.user {
    background: rgba(255, 255, 255, 0.35) !important;
    border: 1px solid rgba(255, 255, 255, 0.40) !important;
    border-radius: 14px 14px 4px 14px !important;
    box-shadow: 0 2px 8px rgba(255, 255, 255, 0.08), inset 0 1px 0 rgba(255,255,255,0.05) !important;
    color: #e6edf3 !important;
    font-size: 13px !important;
    line-height: 1.5 !important;
    padding: 10px 14px !important;
}

/* === INPUT FIELD === */
#chat-panel .input-row {
    flex-shrink: 0 !important;
}
#chat-panel textarea,
#chat-panel input[type="text"] {
    background: rgba(22, 27, 34, 0.3) !important;
    border: 1px solid rgba(0, 212, 255, 0.2) !important;
    border-radius: 8px !important;
    color: #e6edf3 !important;
    font-size: 13px !important;
    padding: 10px 12px !important;
}
#chat-panel textarea:focus,
#chat-panel input[type="text"]:focus {
    border-color: #00d4ff !important;
    box-shadow: 0 0 0 2px rgba(0, 212, 255, 0.15) !important;
    outline: none !important;
}
/* Send button */
#send-btn {
    background: linear-gradient(135deg, #00d4ff 0%, #0088cc 100%) !important;
    color: #fff !important;
    border: none !important;
    border-radius: 8px !important;
    font-weight: 600 !important;
    font-size: 13px !important;
    cursor: pointer !important;
    transition: all 0.2s ease !important;
    min-width: 70px !important;
}
#send-btn:hover {
    box-shadow: 0 4px 16px rgba(0, 212, 255, 0.35) !important;
    transform: translateY(-1px) !important;
}

/* === SUGGESTION BUTTONS === */
.suggestion-btn {
    background: rgba(0, 212, 255, 0.06) !important;
    border: 1px solid rgba(0, 212, 255, 0.15) !important;
    color: #b0d4e8 !important;
    border-radius: 6px !important;
    font-size: 11px !important;
    padding: 6px 10px !important;
    cursor: pointer !important;
    transition: all 0.2s ease !important;
    text-align: left !important;
}
.suggestion-btn:hover {
    background: rgba(0, 212, 255, 0.12) !important;
    border-color: rgba(0, 212, 255, 0.35) !important;
    color: #e6edf3 !important;
    box-shadow: 0 2px 8px rgba(0, 212, 255, 0.15) !important;
}

/* === SELECTED NODE DISPLAY === */
#selected-node-display {
    font-size: 12px;
    padding: 6px 10px;
    background: rgba(0, 212, 255, 0.08);
    border-radius: 6px;
    border: 1px solid rgba(0, 212, 255, 0.15);
    color: #e6edf3;
    flex-shrink: 0;
}
#selected-node-display * {
    color: #e6edf3 !important;
}

/* === REFRESH GRAPH BUTTON === */
#refresh-btn {
    background: rgba(0, 212, 255, 0.1) !important;
    border: 1px solid rgba(0, 212, 255, 0.2) !important;
    color: #00d4ff !important;
    border-radius: 6px !important;
    font-size: 11px !important;
    cursor: pointer !important;
    flex-shrink: 0 !important;
}
#refresh-btn:hover {
    background: rgba(0, 212, 255, 0.2) !important;
}

/* === FOOTER BAR === */
#footer-bar {
    pointer-events: none !important;
    flex-shrink: 0 !important;
    padding: 0 !important;
    margin-top: auto !important;
}
#footer-bar > div {
    padding: 0 !important;
}
.footer-inner {
    background: rgba(13, 17, 23, 0.7);
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
    border-top: 1px solid rgba(0, 212, 255, 0.1);
    padding: 6px 24px;
    text-align: center;
    font-size: 10px;
    color: #484f58;
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
}

/* === HIDDEN NODE CLICK DATA === */
#node-click-data {
    position: absolute !important;
    width: 0 !important;
    height: 0 !important;
    overflow: hidden !important;
    opacity: 0 !important;
    pointer-events: none !important;
    margin: 0 !important;
    padding: 0 !important;
    border: none !important;
}

/* === HIDE GRADIO PROGRESS INDICATORS === */
.progress-text,
.eta-bar,
.progress-bar,
.meta-text,
.meta-text-center,
.generating,
[data-testid="bot"] .progress-text,
#chatbot .progress-text,
.chatbot .progress-text {
    display: none !important;
    visibility: hidden !important;
}

/* === REMOVE GRADIO BORDERS/BACKGROUNDS === */
.block, .form, .gap {
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
}

/* Labels in chat panel */
#chat-panel label, #chat-panel .label-wrap {
    color: #8b949e !important;
    font-size: 11px !important;
}

/* Suggestions label */
#suggestions-label {
    color: #8b949e !important;
    font-size: 11px !important;
    flex-shrink: 0 !important;
    margin: 0 !important;
    padding: 0 !important;
}
#suggestions-label p {
    margin: 0 !important;
    color: #8b949e !important;
    font-size: 11px !important;
}

/* === RESPONSIVE === */
@media (max-width: 768px) {
    #chat-panel {
        width: calc(100vw - 32px) !important;
        left: 16px !important;
    }
}

/* === THEME TOGGLE BUTTON === */
#theme-toggle {
    background: rgba(13, 17, 23, 0.7);
    border: 1px solid rgba(0, 212, 255, 0.4);
    color: #00d4ff;
    border-radius: 8px;
    padding: 6px 10px;
    cursor: pointer;
    display: flex;
    align-items: center;
    font-size: 14px;
    transition: all 0.2s ease;
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
}
#theme-toggle:hover {
    background: rgba(13, 17, 23, 0.85);
    border-color: rgba(0, 212, 255, 0.6);
    color: #ffffff;
}

/* === LIGHT MODE OVERRIDES === */
.light-mode,
.light-mode body {
    background: #d5dce8 !important;
}
.light-mode .gradio-container {
    background: transparent !important;
}
.light-mode .header-inner {
    background: rgba(255, 255, 255, 0.92);
    border-bottom: 1px solid rgba(0, 60, 130, 0.12);
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.06);
}
.light-mode .header-title {
    color: #1f2328;
}
.light-mode .header-accent {
    color: #0969da;
}
.light-mode .header-subtitle {
    color: #656d76;
}
.light-mode #theme-toggle {
    background: rgba(255, 255, 255, 0.75);
    border: 1px solid rgba(9, 105, 218, 0.35);
    color: #0550ae;
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
}
.light-mode #theme-toggle:hover {
    background: rgba(255, 255, 255, 0.9);
    border-color: rgba(9, 105, 218, 0.5);
    color: #0339a6;
}
.light-mode #chat-panel {
    background: rgba(255, 255, 255, 0.97) !important;
    border: 1px solid rgba(0, 60, 130, 0.18) !important;
    box-shadow: 0 4px 24px rgba(0, 40, 100, 0.12), 0 1px 3px rgba(0, 0, 0, 0.08) !important;
    backdrop-filter: blur(20px) !important;
    -webkit-backdrop-filter: blur(20px) !important;
}
.light-mode .chat-drag-handle .drag-dots {
    background: rgba(26, 58, 110, 0.4);
}
.light-mode .chat-drag-handle:hover .drag-dots {
    background: rgba(26, 58, 110, 0.7);
}
.light-mode #chatbot [data-testid="bot"],
.light-mode #chatbot .bot,
.light-mode #chatbot .message.bot {
    background: #e8f1fc !important;
    border: 1px solid #c4d9f2 !important;
    box-shadow: 0 1px 3px rgba(0, 80, 170, 0.08) !important;
    color: #1a1a2e !important;
}
.light-mode #chatbot [data-testid="user"],
.light-mode #chatbot .user,
.light-mode #chatbot .message.user {
    background: #f0f2f5 !important;
    border: 1px solid #d8dce2 !important;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.04) !important;
    color: #1a1a2e !important;
}
.light-mode #chat-panel textarea,
.light-mode #chat-panel input[type="text"] {
    background: #ffffff !important;
    border: 1px solid rgba(0, 105, 218, 0.3) !important;
    color: #1a1a2e !important;
}
.light-mode #chat-panel textarea:focus,
.light-mode #chat-panel input[type="text"]:focus {
    border-color: #0969da !important;
    box-shadow: 0 0 0 2px rgba(9, 105, 218, 0.15) !important;
}
.light-mode #send-btn {
    background: linear-gradient(135deg, #0969da 0%, #0550ae 100%) !important;
}
.light-mode #send-btn:hover {
    box-shadow: 0 4px 16px rgba(9, 105, 218, 0.3) !important;
}
.light-mode .suggestion-btn {
    background: #ffffff !important;
    border: 1px solid rgba(9, 105, 218, 0.25) !important;
    color: #0550ae !important;
}
.light-mode .suggestion-btn:hover {
    background: rgba(9, 105, 218, 0.08) !important;
    border-color: rgba(9, 105, 218, 0.45) !important;
    color: #0550ae !important;
    box-shadow: 0 2px 8px rgba(9, 105, 218, 0.15) !important;
}
.light-mode #selected-node-display {
    background: rgba(9, 105, 218, 0.06);
    border: 1px solid rgba(9, 105, 218, 0.18);
    color: #0550ae;
}
.light-mode #selected-node-display * {
    color: #0550ae !important;
}
.light-mode #refresh-btn {
    background: rgba(9, 105, 218, 0.08) !important;
    border: 1px solid rgba(9, 105, 218, 0.15) !important;
    color: #0969da !important;
}
.light-mode #refresh-btn:hover {
    background: rgba(9, 105, 218, 0.15) !important;
}
.light-mode .footer-inner {
    background: rgba(255, 255, 255, 0.9);
    border-top: 1px solid rgba(0, 60, 130, 0.1);
    box-shadow: 0 -1px 3px rgba(0, 0, 0, 0.04);
    color: #4a5568;
}
.light-mode #chat-panel label,
.light-mode #chat-panel .label-wrap {
    color: #1a3a6e !important;
}
.light-mode #suggestions-label,
.light-mode #suggestions-label p {
    color: #1a3a6e !important;
}
"""

# JavaScript to handle messages from iframe
js_code = """
<script>
window.addEventListener('message', function(event) {
    if (event.data && event.data.type === 'nodeClick') {
        console.log('Node click received:', event.data);

        // Find the hidden textbox - try multiple selectors
        let hiddenInput = document.querySelector('#node-click-data textarea');
        if (!hiddenInput) {
            hiddenInput = document.querySelector('#node-click-data input');
        }
        if (!hiddenInput) {
            // Try finding by traversing the component wrapper
            const wrapper = document.getElementById('node-click-data');
            if (wrapper) {
                hiddenInput = wrapper.querySelector('textarea') || wrapper.querySelector('input');
            }
        }

        console.log('Hidden input found:', hiddenInput);

        if (hiddenInput) {
            const nodeData = JSON.stringify({
                nodeId: event.data.nodeId,
                nodeLabel: event.data.nodeLabel,
                nodeType: event.data.nodeType,
                properties: event.data.properties
            });

            // Use native setter to properly set the value
            const nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')?.set
                || Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;

            if (nativeInputValueSetter) {
                nativeInputValueSetter.call(hiddenInput, nodeData);
            } else {
                hiddenInput.value = nodeData;
            }

            // Dispatch multiple events to ensure Gradio detects the change
            hiddenInput.dispatchEvent(new Event('input', { bubbles: true, cancelable: true }));
            hiddenInput.dispatchEvent(new Event('change', { bubbles: true, cancelable: true }));
            hiddenInput.dispatchEvent(new InputEvent('input', { bubbles: true, cancelable: true, inputType: 'insertText' }));

            console.log('Events dispatched for value:', nodeData);
        } else {
            console.error('Could not find hidden input element');
        }
    }
});
</script>
"""

# JavaScript for draggable + resizable panel
# Grips are appended to document.body (outside Gradio DOM) to avoid all interference
drag_resize_js = """
function() {
    var MIN_W = 300, MIN_H = 250;
    var mode = null;
    var startX, startY, startL, startT, startW, startH;
    var overlay = null;

    function attach() {
        var panel = document.querySelector('#chat-panel');
        if (!panel) { setTimeout(attach, 500); return; }
        var handle = panel.querySelector('.chat-drag-handle');

        function S(prop, val) { panel.style.setProperty(prop, val, 'important'); }

        /* Transparent fullscreen overlay — blocks iframe from stealing events */
        function showOverlay(cursor) {
            overlay = document.createElement('div');
            overlay.style.cssText = 'position:fixed;top:0;left:0;width:100vw;height:100vh;z-index:99999;cursor:'+cursor+';';
            document.body.appendChild(overlay);
            overlay.addEventListener('mousemove', onMove);
            overlay.addEventListener('mouseup', onUp);
        }
        function hideOverlay() {
            if (overlay) { overlay.remove(); overlay = null; }
        }

        function onMove(e) {
            var dx = e.clientX - startX, dy = e.clientY - startY;
            if (mode === 'drag') {
                S('left', Math.max(0, Math.min(startL + dx, window.innerWidth - 80)) + 'px');
                S('top', Math.max(0, Math.min(startT + dy, window.innerHeight - 80)) + 'px');
                S('bottom', 'auto');
            } else if (mode === 'resize') {
                S('width', Math.max(MIN_W, startW + dx) + 'px');
                S('height', Math.max(MIN_H, startH + dy) + 'px');
            }
        }
        function onUp() {
            mode = null;
            hideOverlay();
            document.body.style.removeProperty('user-select');
        }

        /* Create visible resize grip in bottom-right corner */
        var grip = document.createElement('div');
        grip.style.cssText = 'position:absolute;bottom:0;right:0;width:28px;height:28px;cursor:nwse-resize;z-index:100;pointer-events:auto;';
        grip.innerHTML = '<svg width="14" height="14" style="position:absolute;bottom:5px;right:5px;pointer-events:none;"><line x1="12" y1="4" x2="4" y2="12" stroke="rgba(0,212,255,0.4)" stroke-width="1.5"/><line x1="12" y1="8" x2="8" y2="12" stroke="rgba(0,212,255,0.4)" stroke-width="1.5"/></svg>';
        function gripColor(hover) {
            var lt = document.documentElement.classList.contains('light-mode');
            var c = hover ? (lt ? 'rgba(26,58,110,0.8)' : 'rgba(0,212,255,0.8)') : (lt ? 'rgba(26,58,110,0.4)' : 'rgba(0,212,255,0.4)');
            grip.querySelectorAll('line').forEach(function(l){ l.setAttribute('stroke', c); });
        }
        grip.addEventListener('mouseenter', function() { gripColor(true); });
        grip.addEventListener('mouseleave', function() { if (!mode) gripColor(false); });
        panel.appendChild(grip);

        /* --- DRAG (via handle bar) --- */
        if (handle) {
            handle.addEventListener('mousedown', function(e) {
                mode = 'drag';
                startX = e.clientX; startY = e.clientY;
                var r = panel.getBoundingClientRect();
                startL = r.left; startT = r.top;
                S('transition', 'none');
                document.body.style.setProperty('user-select', 'none', 'important');
                showOverlay('grabbing');
                e.preventDefault(); e.stopPropagation();
            });
        }

        /* --- RESIZE (via corner grip) --- */
        grip.addEventListener('mousedown', function(e) {
            mode = 'resize';
            startX = e.clientX; startY = e.clientY;
            var r = panel.getBoundingClientRect();
            startW = r.width; startH = r.height;
            S('transition', 'none');
            document.body.style.setProperty('user-select', 'none', 'important');
            showOverlay('nwse-resize');
            e.preventDefault(); e.stopPropagation();
        });

        console.log('[GBAIA] Drag + corner resize ready');

        /* === Theme Toggle === */
        var themeBtn = document.getElementById('theme-toggle');
        if (themeBtn && !themeBtn._themeAttached) {
            themeBtn._themeAttached = true;

            function applyLightMode(isLight) {
                var S = function(el, styles) {
                    if (!el) return;
                    for (var k in styles) el.style.setProperty(k, styles[k], 'important');
                };
                var R = function(el, props) {
                    if (!el) return;
                    props.forEach(function(p) { el.style.removeProperty(p); });
                };

                document.documentElement.classList.toggle('light-mode', isLight);

                /* Icons */
                var sun = document.getElementById('theme-icon-sun');
                var moon = document.getElementById('theme-icon-moon');
                if (sun) sun.style.display = isLight ? 'none' : 'block';
                if (moon) moon.style.display = isLight ? 'block' : 'none';

                /* Chat panel - blue frosted glass */
                if (isLight) {
                    S(panel, {
                        'background': 'rgba(60, 100, 170, 0.28)',
                        'border': '1px solid rgba(80, 130, 200, 0.35)',
                        'box-shadow': '0 8px 32px rgba(0, 40, 100, 0.15), inset 0 1px 0 rgba(255,255,255,0.15)',
                        'backdrop-filter': 'blur(16px)',
                        '-webkit-backdrop-filter': 'blur(16px)'
                    });
                } else {
                    S(panel, {
                        'background': 'rgba(13, 17, 23, 0.25)',
                        'border': '1px solid rgba(0, 212, 255, 0.15)',
                        'box-shadow': '0 8px 32px rgba(0, 0, 0, 0.4), inset 0 1px 0 rgba(0, 212, 255, 0.05)',
                        'backdrop-filter': 'blur(12px)',
                        '-webkit-backdrop-filter': 'blur(12px)'
                    });
                }

                /* Bot bubbles + all inner text */
                var darkBlue = '#1a3a6e';
                document.querySelectorAll('#chatbot [data-testid="bot"], #chatbot .bot').forEach(function(el) {
                    if (isLight) {
                        S(el, { 'background': 'rgba(9, 105, 218, 0.15)', 'border': '1px solid rgba(26, 58, 110, 0.30)',
                                'border-left': '3px solid ' + darkBlue, 'color': '#1a1a2e',
                                'box-shadow': '0 2px 8px rgba(9, 105, 218, 0.10), inset 0 1px 0 rgba(255,255,255,0.4)' });
                    } else {
                        S(el, { 'background': 'rgba(0, 212, 255, 0.40)', 'border': '1px solid rgba(0, 212, 255, 0.50)',
                                'border-left': '3px solid #00d4ff', 'color': '#e6edf3',
                                'box-shadow': '0 2px 8px rgba(0, 212, 255, 0.15), inset 0 1px 0 rgba(255,255,255,0.05)' });
                    }
                    el.querySelectorAll('*').forEach(function(child) {
                        child.style.setProperty('color', isLight ? '#1a1a2e' : '#e6edf3', 'important');
                    });
                });

                /* User bubbles + all inner text - lighter, more transparent */
                document.querySelectorAll('#chatbot [data-testid="user"], #chatbot .user').forEach(function(el) {
                    if (isLight) {
                        S(el, { 'background': 'rgba(255, 255, 255, 0.45)', 'border': '1px solid rgba(26, 58, 110, 0.18)',
                                'border-right': '3px solid rgba(26, 58, 110, 0.4)', 'border-left': 'none', 'color': '#1a1a2e',
                                'box-shadow': '0 2px 8px rgba(0, 0, 0, 0.05), inset 0 1px 0 rgba(255,255,255,0.5)' });
                    } else {
                        S(el, { 'background': 'rgba(255, 255, 255, 0.35)', 'border': '1px solid rgba(255, 255, 255, 0.40)',
                                'border-right': 'none', 'border-left': 'none', 'color': '#e6edf3',
                                'box-shadow': '0 2px 8px rgba(255, 255, 255, 0.08)' });
                    }
                    el.querySelectorAll('*').forEach(function(child) {
                        child.style.setProperty('color', isLight ? '#1a1a2e' : '#e6edf3', 'important');
                    });
                });

                /* Selected node display */
                var selNode = document.getElementById('selected-node-display');
                if (selNode) {
                    if (isLight) {
                        S(selNode, { 'color': darkBlue, 'border-color': 'rgba(26, 58, 110, 0.25)', 'background': 'rgba(26, 58, 110, 0.08)' });
                        selNode.querySelectorAll('*').forEach(function(c) { c.style.setProperty('color', darkBlue, 'important'); });
                    } else {
                        S(selNode, { 'color': '#e6edf3', 'border-color': 'rgba(0, 212, 255, 0.15)', 'background': 'rgba(0, 212, 255, 0.08)' });
                        selNode.querySelectorAll('*').forEach(function(c) { c.style.setProperty('color', '#e6edf3', 'important'); });
                    }
                }

                /* Refresh graph button */
                var refreshBtn = document.getElementById('refresh-btn');
                if (refreshBtn) {
                    if (isLight) {
                        S(refreshBtn, { 'background': 'rgba(26, 58, 110, 0.10)', 'border': '1px solid rgba(26, 58, 110, 0.25)', 'color': darkBlue });
                    } else {
                        S(refreshBtn, { 'background': 'rgba(0, 212, 255, 0.1)', 'border': '1px solid rgba(0, 212, 255, 0.2)', 'color': '#00d4ff' });
                    }
                }

                /* Resize grip color */
                if (typeof gripColor === 'function') gripColor(false);

                /* Suggestions label */
                var sugLabel = document.getElementById('suggestions-label');
                if (sugLabel) {
                    sugLabel.querySelectorAll('*').forEach(function(c) { c.style.setProperty('color', isLight ? darkBlue : '#8b949e', 'important'); });
                }

                /* Conversation label - match suggestions color in light mode */
                panel.querySelectorAll('label, .label-wrap').forEach(function(el) {
                    el.style.setProperty('color', isLight ? darkBlue : '#8b949e', 'important');
                    el.querySelectorAll('*').forEach(function(c) { c.style.setProperty('color', isLight ? darkBlue : '#8b949e', 'important'); });
                });

                /* Theme toggle button */
                var themeBtn = document.getElementById('theme-toggle');
                if (themeBtn) {
                    if (isLight) {
                        S(themeBtn, { 'background': 'rgba(255, 255, 255, 0.75)', 'border': '1px solid rgba(9, 105, 218, 0.35)', 'color': '#0550ae' });
                    } else {
                        S(themeBtn, { 'background': 'rgba(13, 17, 23, 0.7)', 'border': '1px solid rgba(0, 212, 255, 0.4)', 'color': '#00d4ff' });
                    }
                }

                /* Input field */
                panel.querySelectorAll('textarea, input[type="text"]').forEach(function(el) {
                    if (isLight) {
                        S(el, { 'background': 'rgba(255, 255, 255, 0.6)', 'border': '1px solid rgba(9, 105, 218, 0.2)', 'color': '#1a1a2e' });
                    } else {
                        S(el, { 'background': 'rgba(22, 27, 34, 0.3)', 'border': '1px solid rgba(0, 212, 255, 0.2)', 'color': '#e6edf3' });
                    }
                });

                /* Suggestion buttons */
                document.querySelectorAll('.suggestion-btn').forEach(function(el) {
                    if (isLight) {
                        S(el, { 'background': 'rgba(26, 58, 110, 0.06)', 'border': '1px solid rgba(26, 58, 110, 0.22)', 'color': darkBlue });
                    } else {
                        S(el, { 'background': 'rgba(0, 212, 255, 0.06)', 'border': '1px solid rgba(0, 212, 255, 0.15)', 'color': '#b0d4e8' });
                    }
                });

                /* Send button */
                var sendBtn = document.getElementById('send-btn');
                if (sendBtn) {
                    if (isLight) {
                        S(sendBtn, { 'background': 'linear-gradient(135deg, #0969da 0%, #0550ae 100%)' });
                    } else {
                        S(sendBtn, { 'background': 'linear-gradient(135deg, #00d4ff 0%, #0088cc 100%)' });
                    }
                }

                /* Notify graph iframe */
                var iframe = document.querySelector('#graph-bg iframe');
                if (iframe && iframe.contentWindow) {
                    iframe.contentWindow.postMessage({ type: 'themeChange', theme: isLight ? 'light' : 'dark' }, '*');
                }

                console.log('[GBAIA] Theme applied:', isLight ? 'light' : 'dark');
            }

            /* Watch for new chat messages and apply light mode styles */
            var chatEl = document.getElementById('chatbot');
            if (chatEl) {
                var observer = new MutationObserver(function() {
                    if (document.documentElement.classList.contains('light-mode')) {
                        var db = '#1a3a6e';
                        chatEl.querySelectorAll('[data-testid="bot"], .bot').forEach(function(el) {
                            el.style.setProperty('border-left', '3px solid ' + db, 'important');
                            el.style.setProperty('background', 'rgba(9, 105, 218, 0.15)', 'important');
                            el.style.setProperty('border-color', 'rgba(26, 58, 110, 0.30)', 'important');
                        });
                        chatEl.querySelectorAll('[data-testid="user"], .user').forEach(function(el) {
                            el.style.setProperty('border-left', 'none', 'important');
                            el.style.setProperty('border-right', '3px solid rgba(26, 58, 110, 0.4)', 'important');
                            el.style.setProperty('background', 'rgba(255, 255, 255, 0.45)', 'important');
                            el.style.setProperty('border-color', 'rgba(26, 58, 110, 0.18)', 'important');
                        });
                        chatEl.querySelectorAll('[data-testid="bot"] *, .bot *, [data-testid="user"] *, .user *').forEach(function(child) {
                            child.style.setProperty('color', '#1a1a2e', 'important');
                        });
                    }
                });
                observer.observe(chatEl, { childList: true, subtree: true });
            }

            themeBtn.addEventListener('click', function() {
                var isLight = !document.documentElement.classList.contains('light-mode');
                localStorage.setItem('gbaia-theme', isLight ? 'light' : 'dark');
                applyLightMode(isLight);
            });

            /* Apply saved theme */
            var saved = localStorage.getItem('gbaia-theme');
            if (saved === 'light') {
                applyLightMode(true);
                /* Retry iframe notification after it loads */
                setTimeout(function() { applyLightMode(true); }, 2000);
            }
            console.log('[GBAIA] Theme toggle attached, saved:', saved);
        }
    }
    attach();
}
"""

# --- Gradio Interface ---
with gr.Blocks(theme=gr.themes.Base(), title="Network AI Agent", css=custom_css, head=js_code, js=drag_resize_js) as demo:

    # State for dynamic suggestions
    suggestions_state = gr.State(initial_suggestions)

    # Hidden component to receive node click data from iframe (hidden via CSS, not visible=False)
    node_click_data = gr.Textbox(elem_id="node-click-data", label="", container=False)

    # Layer 1: Fullscreen 3D graph background
    graph_html = gr.HTML(
        value=generate_graph_html,
        elem_id="graph-bg"
    )

    # Layer 2: Transparent overlay with UI elements
    with gr.Column(elem_id="overlay-container"):

        # Header bar
        gr.HTML(
            value="""<div class="header-inner">
                <div class="header-title">
                    <span class="header-accent">Network AI Agent</span>
                    <span class="header-subtitle">Graph-Based AI Agent for modern networks</span>
                </div>
                <button id="theme-toggle" title="Toggle light/dark mode">
                    <svg id="theme-icon-sun" width="22" height="22" viewBox="0 0 24 24" fill="#ffcc00" stroke="#ffcc00" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><line x1="12" y1="1" x2="12" y2="4"/><line x1="12" y1="20" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="6.34" y2="6.34"/><line x1="17.66" y1="17.66" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="4" y2="12"/><line x1="20" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="6.34" y2="17.66"/><line x1="17.66" y1="6.34" x2="19.78" y2="4.22"/></svg>
                    <svg id="theme-icon-moon" width="22" height="22" viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="1" stroke-linecap="round" stroke-linejoin="round" style="display:none"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
                </button>
            </div>""",
            elem_id="header-bar"
        )

        # Glassmorphic chat panel (left sidebar, draggable + resizable)
        with gr.Column(elem_id="chat-panel"):

            # Drag handle
            gr.HTML(value='<div class="chat-drag-handle" id="chat-drag-handle"><span class="drag-dots"></span></div>')

            # Selected node display
            selected_node_display = gr.Markdown(
                value="*Click on a graph node to explore it*",
                elem_id="selected-node-display"
            )

            chatbot = gr.Chatbot(
                label="Conversation",
                height=None,
                type="messages",
                elem_id="chatbot",
                value=[{"role": "assistant", "content": "Welcome! I'm your Network AI Agent. Ask me about tenants, fabrics, anomalies, faults, or click a node in the graph to explore it."}]
            )

            with gr.Row(elem_classes=["input-row"]):
                msg_input = gr.Textbox(
                    label="",
                    placeholder="Ask about the network...",
                    scale=4,
                    container=False,
                )
                submit_button = gr.Button("Send", variant="primary", scale=1, elem_id="send-btn")

            # Suggestions
            gr.Markdown("**Suggestions** *(click to ask)*", elem_id="suggestions-label")
            with gr.Row():
                suggestion_btns = []
                for i in range(4):
                    btn = gr.Button(
                        value=initial_suggestions[i] if i < len(initial_suggestions) else "",
                        size="sm",
                        variant="secondary",
                        visible=True,
                        elem_classes=["suggestion-btn"]
                    )
                    suggestion_btns.append(btn)

            refresh_btn = gr.Button("Refresh Graph", variant="secondary", size="sm", elem_id="refresh-btn")

        # Footer bar
        gr.HTML(
            value="""<div class="footer-inner">
                Data sources: Cisco APIC &middot; Nexus Dashboard &middot; Neo4j Knowledge Graph
            </div>""",
            elem_id="footer-bar"
        )

    # Function to update suggestion buttons
    def update_buttons(suggestions):
        updates = []
        for i in range(4):
            if i < len(suggestions) and suggestions[i]:
                updates.append(gr.update(value=suggestions[i], visible=True))
            else:
                updates.append(gr.update(value="", visible=False))
        return updates

    # State for storing last question/answer for async suggestions
    last_question = gr.State("")
    last_answer = gr.State("")

    # Loading state for suggestions
    loading_suggestions = ["Loading suggestions...", "", "", ""]

    # --- Event Handlers ---
    def handle_submit_streaming(question, history, source="user"):
        """Handle question submission with streaming progress"""
        if not question or not question.strip():
            yield [history, "", "", ""] + update_buttons(initial_suggestions)
            return

        final_q, final_a = "", ""
        for hist, cleared, q, a in get_agent_response_streaming(question, history, source=source):
            final_q, final_a = q, a
            yield [hist, cleared, q, a] + update_buttons(loading_suggestions)

        # Final yield with actual answer for suggestions
        yield [hist, "", final_q, final_a] + update_buttons(loading_suggestions)

    def handle_user_submit(question, history):
        """Handle direct user input - concise response"""
        for result in handle_submit_streaming(question, history, source="user"):
            yield result

    def handle_suggestion_submit(question, history):
        """Handle suggestion button click - concise response"""
        for result in handle_submit_streaming(question, history, source="suggestion"):
            yield result

    def handle_graph_submit(question, history):
        """Handle graph node click - detailed report"""
        for result in handle_submit_streaming(question, history, source="graph_click"):
            yield result

    def handle_submit(question, history):
        """Handle question submission - non-streaming fallback"""
        if not question or not question.strip():
            return [history, "", "", ""] + update_buttons(initial_suggestions)
        history, cleared_input, q, a = get_agent_response(question, history)
        return [history, cleared_input, q, a] + update_buttons(loading_suggestions)

    def handle_suggestions_update(question, answer):
        """Fetch and update suggestions asynchronously"""
        if not question or not answer:
            return update_buttons(initial_suggestions)
        suggestions = fetch_suggestions(question, answer)
        return update_buttons(suggestions)

    def handle_node_click_prepare(node_data_json):
        """Parse node click and prepare query - first step"""
        if not node_data_json:
            return "", "*Click on a graph node to explore it*"

        try:
            node_data = json.loads(node_data_json)
            node_type = node_data.get("nodeType", "Unknown")
            node_label = node_data.get("nodeLabel", "Unknown")

            # Generate contextual query
            template = NODE_QUERY_TEMPLATES.get(node_type, NODE_QUERY_TEMPLATES["Unknown"])
            question = template.format(name=node_label)

            # Update selected node display
            selected_display = f"**Selected:** {node_type} - *{node_label}*"

            return question, selected_display

        except (json.JSONDecodeError, KeyError) as e:
            print(f"Error handling node click: {e}")
            return "", "*Click on a graph node to explore it*"

    def handle_node_submit(question, history):
        """Submit the node query - detailed report for graph clicks"""
        if not question:
            yield [history, "", "", ""] + update_buttons(initial_suggestions)
            return
        for result in handle_graph_submit(question, history):
            yield result

    # Submit button click - user input gets concise response
    submit_button.click(
        fn=handle_user_submit,
        inputs=[msg_input, chatbot],
        outputs=[chatbot, msg_input, last_question, last_answer] + suggestion_btns
    ).then(
        fn=handle_suggestions_update,
        inputs=[last_question, last_answer],
        outputs=suggestion_btns
    )

    # Enter key submit - user input gets concise response
    msg_input.submit(
        fn=handle_user_submit,
        inputs=[msg_input, chatbot],
        outputs=[chatbot, msg_input, last_question, last_answer] + suggestion_btns
    ).then(
        fn=handle_suggestions_update,
        inputs=[last_question, last_answer],
        outputs=suggestion_btns
    )

    # Suggestion button clicks - suggestions get concise response
    for btn in suggestion_btns:
        btn.click(
            fn=handle_suggestion_submit,
            inputs=[btn, chatbot],
            outputs=[chatbot, msg_input, last_question, last_answer] + suggestion_btns
        ).then(
            fn=handle_suggestions_update,
            inputs=[last_question, last_answer],
            outputs=suggestion_btns
        )

    # Node click handler - three-step chain:
    # 1. Prepare query from node data (instant)
    # 2. Submit query and get response (shows loading dots)
    # 3. Fetch suggestions async
    node_click_data.change(
        fn=handle_node_click_prepare,
        inputs=[node_click_data],
        outputs=[msg_input, selected_node_display]
    ).then(
        fn=handle_node_submit,
        inputs=[msg_input, chatbot],
        outputs=[chatbot, msg_input, last_question, last_answer] + suggestion_btns
    ).then(
        fn=handle_suggestions_update,
        inputs=[last_question, last_answer],
        outputs=suggestion_btns
    )

    # Refresh graph button
    refresh_btn.click(
        fn=refresh_graph,
        outputs=[graph_html]
    )

demo.launch(server_name="0.0.0.0", server_port=7860, pwa=False)
