# CISCO Network AI Agent

This project is a comprehensive demonstration of a conversational AI agent designed for network operations. The agent leverages a knowledge graph to provide intelligent, context-aware answers about a network's configuration and real-time operational state.

The system is built as a multi-container application orchestrated with `podman-compose`, integrating data from **Cisco APIC** and **Cisco Nexus Dashboard** into a **Neo4j** graph database, and exposing the AI's capabilities through a **Gradio** web interface.

<p align="center">
  <img src="architecture.png" alt="Architecture Diagram" width="800"/>
</p>

Special thanks to three great Cisconians, without their help this creation would have not been possible: Rob van der Kind, Jara Osterfeld and Olaf Barning.

---
##  Architecture

The application is composed of four distinct, containerized services that work in concert:

1.  **🧠 The Brain (Backend API):** A **FastAPI** application that contains the core AI logic built with **LangChain**. It receives user questions, translates them into database queries, and synthesizes the results into natural language answers. It can be configured to use either a local, self-hosted LLM (like Phi) or the OpenAI API.
2.  **💾 The Memory (Graph Database):** A **Neo4j** database that stores the network knowledge graph. This graph unifies configuration data (Tenants, EPGs) with operational data (Fabrics, Anomalies).
3.  **Collector (Data Ingestor):** A recurring Python service that runs on a schedule. It connects to the APIC and Nexus Dashboard APIs, fetches the latest data, and synchronizes it with the Neo4j graph, ensuring the agent's knowledge is always up-to-date.
4.  **😊 The Face (Frontend UI):** A **Gradio** web application that provides an intuitive chat interface. It handles user interaction, displays conversation history, and presents dynamic, AI-generated suggestions for follow-up questions.

---
## Features

* **Unified Knowledge Graph:** Combines network configuration (from APIC) and operational state (from Nexus Dashboard) into a single, correlated data model.
* **Conversational AI:** Allows users to ask complex, multi-turn questions in natural language.
* **Recurring Data Sync:** An automated ingestion service keeps the knowledge graph synchronized with live network data, including a "sync and sweep" mechanism to remove stale objects.
* **Dynamic Suggestions:** The AI suggests relevant follow-up questions after each answer, guiding the user's investigation.
* **Flexible LLM Backend:** Easily switch between a local, self-hosted model (via a `vLLM`-compatible endpoint) and the OpenAI API using a single environment variable.
* **Fully Containerized:** The entire stack is orchestrated with `podman-compose` for easy setup and portability.

---
## Getting Started

Follow these steps to set up and run the entire application stack.

### Prerequisites

* Podman & `podman-compose` installed.
* Access credentials for Cisco APIC and Cisco Nexus Dashboard.
* An OpenAI API key or a running, self-hosted LLM with a vLLM-compatible API endpoint.

### 1. Clone the Repository

```bash
git clone <your-repo-url>
cd <your-repo-directory>
```

### 2. Create Directory Structure

Ensure your project has the necessary directories for the container volumes and source code:
```bash
# For Neo4j persistent data
mkdir -p ./neo4j/data ./neo4j/logs ./neo4j/conf

# For the source code
mkdir -p backend frontend
```
Place the `ingestor.py` and its `Containerfile` in the root. Place the `backend.py` and its files in the `backend/` directory, and the `frontend.py` and its files in the `frontend/` directory.

### 3. Configure Environment Variables

Edit the `podman-compose.yml` file and fill in all the placeholder values (`<...>`). This includes credentials for APIC, Nexus Dashboard, Neo4j, and your chosen LLM.

**Example: Using a local LLM**
```yaml
# In the backend-api service
environment:
  - LOCAL_LLM_URL=https://your-local-llm-url/v1
  - OPENAI_API_KEY='' # Can be empty
  # ...
```

**Example: Using OpenAI**
```yaml
# In the backend-api service
environment:
  # - LOCAL_LLM_URL=... (Comment out or remove this line)
  - OPENAI_API_KEY='sk-xxxxxxxxxxxx'
  # ...
```

### 4. Build and Run the Application

Run the following command to build all the container images and start the services in the background:
```bash
podman-compose up -d --build
```
This will start the Neo4j database, the backend API, and the frontend UI. The data ingestor will also start and begin its recurring sync process.

---
## How to Use

1.  **Access the Web Interface:** Open your web browser and navigate to `http://localhost:7860`.
2.  **Ask a Question:** Use the chat interface to ask questions about your network. You can start with one of the initial suggestions or type your own.

### Example Questions
* "How many tenants are there?"
* "List the specific anomalies affecting the '<YOUR_FABRIC_NAME>' fabric."
* "What is the health score of the fabric that the '<YOUR_TENANT_NAME>' tenant belongs to?"
* "What are the recommended actions for the issues on the '<YOUR_TENANT_NAME>' tenant?"

---
## Project Structure

```
.
├── backend/                  # Source code for the Backend API (The Brain)
│   ├── Containerfile
│   ├── main.py
│   └── requirements.txt
├── frontend/                 # Source code for the Frontend UI (The Face)
│   ├── Containerfile
│   ├── app.py
│   └── requirements.txt
├── neo4j/                    # Persistent data volumes for Neo4j
│   ├── data/
│   ├── logs/
│   └── conf/
├── compose.yml               # Main orchestration file for all services
├── ingest_data.py            # Python script for the Data Ingestor
├── Containerfile             # Dockerfile for the Data Ingestor
└── README.md                 # This file
```
