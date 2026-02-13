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
GRAPH_URL = "http://backend-api:8000/graph"

# Node styling configuration - Modern dark theme colors
NODE_COLORS = {
    "Tenant": "#58a6ff",      # Soft blue
    "AppProfile": "#7ee787",  # Mint green
    "EPG": "#56d364",         # Bright green
    "Fabric": "#f0883e",      # Warm orange
    "Anomaly": "#ff7b72",     # Soft red
    "HealthSummary": "#bc8cff", # Light purple
    "VRF": "#a371f7",         # Purple
    "BridgeDomain": "#39d353", # Green
    "Subnet": "#79c0ff",      # Light blue
    "Node": "#d2a8ff",        # Lavender
    "Fault": "#ffa657",       # Orange
    "Advisory": "#d29922",    # Gold
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
    <script src="https://unpkg.com/three@0.160.0/build/three.min.js"></script>
    <script src="https://unpkg.com/3d-force-graph"></script>
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
            top: 12px;
            left: 12px;
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
            background: rgba(88, 166, 255, 0.2);
            border-color: #58a6ff;
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
            bottom: 12px;
            left: 12px;
            background: rgba(33, 38, 45, 0.9);
            color: #8b949e;
            padding: 8px 14px;
            border-radius: 6px;
            font-size: 11px;
            border: 1px solid rgba(240,246,252,0.1);
        }}
        #reset-btn {{
            position: absolute;
            bottom: 12px;
            right: 12px;
            background: linear-gradient(135deg, #58a6ff 0%, #1f6feb 100%);
            color: #fff;
            padding: 8px 16px;
            border-radius: 6px;
            font-size: 12px;
            font-weight: 600;
            border: none;
            cursor: pointer;
            display: none;
            z-index: 200;
            box-shadow: 0 4px 12px rgba(88, 166, 255, 0.3);
        }}
        #reset-btn:hover {{
            transform: translateY(-2px);
            box-shadow: 0 6px 20px rgba(88, 166, 255, 0.4);
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
                    <rect x="2" y="2" width="12" height="12" fill="#58a6ff"/>
                </svg>Tenant
            </div>
            <div class="legend-item" data-type="Node">
                <svg width="16" height="16" viewBox="0 0 16 16" class="legend-shape">
                    <rect x="3" y="2" width="10" height="12" rx="3" fill="#d2a8ff"/>
                </svg>Node
            </div>
            <div class="legend-item" data-type="VRF">
                <svg width="16" height="16" viewBox="0 0 16 16" class="legend-shape">
                    <polygon points="8,1 14,4 14,12 8,15 2,12 2,4" fill="#a371f7"/>
                </svg>VRF
            </div>
            <div class="legend-item" data-type="BridgeDomain">
                <svg width="16" height="16" viewBox="0 0 16 16" class="legend-shape">
                    <rect x="1" y="5" width="14" height="6" fill="#39d353"/>
                </svg>BD
            </div>
            <div class="legend-item" data-type="Subnet">
                <svg width="16" height="16" viewBox="0 0 16 16" class="legend-shape">
                    <circle cx="8" cy="8" r="5" fill="#79c0ff"/>
                </svg>Subnet
            </div>
            <div class="legend-item" data-type="AppProfile">
                <svg width="16" height="16" viewBox="0 0 16 16" class="legend-shape">
                    <polygon points="8,1 13,4 13,11 8,15 3,11 3,4" fill="#7ee787"/>
                </svg>App
            </div>
            <div class="legend-item" data-type="EPG">
                <svg width="16" height="16" viewBox="0 0 16 16" class="legend-shape">
                    <circle cx="8" cy="8" r="6" fill="#56d364"/>
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
                            <polygon points="8,14 1,2 15,2" fill="#ffa657"/>
                        </svg>Fault
                    </div>
                    <div class="legend-item" data-type="Advisory">
                        <svg width="16" height="16" viewBox="0 0 16 16" class="legend-shape">
                            <circle cx="8" cy="8" r="5" fill="none" stroke="#d29922" stroke-width="3"/>
                        </svg>Advisory
                    </div>
                </div>
            </div>
        </div>
    </div>
    <button id="reset-btn">Reset View</button>
    <div id="click-hint">Click node to explore | Drag to rotate | Scroll to zoom</div>
    <script>
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
            context.fillStyle = 'rgba(13, 17, 23, 0.8)';
            context.roundRect(0, 0, canvas.width, canvas.height, 8);
            context.fill();

            // Text
            context.font = 'bold 24px Inter, Arial, sans-serif';
            context.fillStyle = color || '#ffffff';
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

            // Create material with glow effect
            material = new THREE.MeshLambertMaterial({{
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
            var label = createTextSprite(node.name, '#e6edf3');
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
                // HTML tooltip on hover
                var html = '<div style="background: rgba(22,27,34,0.95); padding: 10px 14px; border-radius: 8px; border: 1px solid rgba(240,246,252,0.2); max-width: 300px;">';
                html += '<div style="color: #58a6ff; font-size: 10px; text-transform: uppercase; margin-bottom: 4px;">' + node.nodeType + '</div>';
                html += '<div style="color: #fff; font-weight: 600; font-size: 14px; margin-bottom: 8px; padding-bottom: 8px; border-bottom: 1px solid rgba(240,246,252,0.1);">' + (node.originalName || node.name) + '</div>';
                if (node.properties) {{
                    for (var key in node.properties) {{
                        if (node.properties[key] !== null && node.properties[key] !== '') {{
                            var val = node.properties[key];
                            if (typeof val === 'string' && val.length > 40) val = val.substring(0, 40) + '...';
                            html += '<div style="color: #8b949e; font-size: 11px; margin: 2px 0;"><span style="color: #8b949e;">' + key + ':</span> <span style="color: #e6edf3;">' + val + '</span></div>';
                        }}
                    }}
                }}
                html += '</div>';
                return html;
            }})
            .nodeThreeObject(node => createNodeObject(node))
            .linkColor(() => 'rgba(88, 166, 255, 0.4)')
            .linkOpacity(0.6)
            .linkWidth(1.5)
            .linkDirectionalParticles(2)
            .linkDirectionalParticleSpeed(0.006)
            .linkDirectionalParticleWidth(2)
            .linkDirectionalParticleColor(() => '#58a6ff')
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
    </script>
</body>
</html>
"""
    # Escape HTML for srcdoc attribute
    escaped_html = html.escape(inner_html, quote=True)

    # Wrap in iframe using srcdoc - allow-same-origin needed for postMessage to work
    return f'<iframe srcdoc="{escaped_html}" style="width: 100%; height: calc(100vh - 280px); min-height: 400px; border: none; border-radius: 8px;" sandbox="allow-scripts allow-same-origin" id="graph-iframe"></iframe>'

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
/* Responsive layout */
.gradio-container {
    max-width: 100% !important;
}
/* Graph container styling */
#graph-container {
    border: 1px solid var(--border-color-primary);
    border-radius: var(--radius-md);
    overflow: hidden;
    height: calc(100vh - 250px);
    min-height: 400px;
    background: var(--background-fill-primary);
}
#graph-container iframe {
    background: transparent;
}
/* Responsive chatbot */
#chatbot {
    height: calc(100vh - 420px) !important;
    min-height: 250px !important;
    max-height: none !important;
}
/* Stack columns on smaller screens */
@media (max-width: 900px) {
    #graph-container {
        height: 50vh;
        min-height: 300px;
    }
    #chatbot {
        height: 40vh !important;
        min-height: 200px !important;
    }
}
/* Suggestion buttons */
.suggestion-btn {
    margin: 2px;
    font-size: 12px;
}
/* Selected node indicator */
#selected-node-display {
    font-size: 12px;
    padding: 8px;
    background: var(--background-fill-secondary);
    border-radius: 4px;
    margin-bottom: 8px;
    color: var(--body-text-color);
}
/* Hide the node click data input */
#node-click-data {
    height: 0 !important;
    min-height: 0 !important;
    max-height: 0 !important;
    overflow: hidden !important;
    opacity: 0 !important;
    margin: 0 !important;
    padding: 0 !important;
    border: none !important;
}
/* Hide processing/progress indicators globally */
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

# --- Gradio Interface ---
with gr.Blocks(theme=gr.themes.Soft(), title="Network AI Agent", css=custom_css, head=js_code) as demo:
    gr.Markdown("# Network AI Agent Demo")

    # State for dynamic suggestions
    suggestions_state = gr.State(initial_suggestions)

    # Hidden component to receive node click data from iframe (hidden via CSS, not visible=False)
    node_click_data = gr.Textbox(elem_id="node-click-data", label="", container=False)

    with gr.Row():
        # Left Column - Graph Visualization (50%)
        with gr.Column(scale=1):
            gr.Markdown("### Network Topology Graph")

            graph_html = gr.HTML(
                value=generate_graph_html,
                elem_id="graph-container"
            )

            refresh_btn = gr.Button("Refresh Graph", variant="secondary", size="sm")

        # Right Column - Chat Interface (50%)
        with gr.Column(scale=1):
            gr.Markdown("### Chat with AI Agent")

            # Selected node display
            selected_node_display = gr.Markdown(
                value="*Click on a graph node to explore it*",
                elem_id="selected-node-display"
            )

            chatbot = gr.Chatbot(label="Conversation History", height=None, type="messages", elem_id="chatbot")

            with gr.Row():
                msg_input = gr.Textbox(
                    label="Your Question",
                    placeholder="Ask a question about the network...",
                    scale=4,
                )
                submit_button = gr.Button("Send", variant="primary", scale=1)

            # Dynamic Suggestions section using buttons
            gr.Markdown("**Suggestions** *(click to ask)*")
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

demo.launch(server_name="0.0.0.0", server_port=7860, pwa=True)
