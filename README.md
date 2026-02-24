# DocGuard AI — Intelligent Document Compliance Analyzer

An AI-powered document analysis platform that uses **agentic AI** to analyze policies, contracts, and procedures for compliance gaps, security issues, and risks.

## Architecture

- **Backend:** Python, Flask, LangChain, LangGraph, AWS Bedrock (Claude)
- **Frontend:** HTML, CSS, JavaScript, Chart.js
- **Database:** PostgreSQL 16
- **Deployment:** Docker Compose

### Agent System
An **Orchestrator Agent** manages 8 specialized sub-agents via LangGraph:

| Agent | Responsibility |
|-------|---------------|
| Compliance Agent | Checks compliance gaps |
| Security Agent | Finds security vulnerabilities |
| Risk Agent | Identifies operational/legal/financial risks |
| Framework Mapping Agent | Maps against ISO 27001, SOC2, NIST, CIS, GDPR, HIPAA |
| Gap Detection Agent | Detects missing encryption, password, backup, incident response, vendor policies |
| Scoring Agent | Scores completeness, security strength, coverage, clarity, enforcement |
| Best Practices Agent | Compares against industry standards |
| Auto-Suggest Agent | Generates improvements, missing clauses, better wording |

## Quick Start

### 1. Configure Environment
```bash
cp .env.example .env
# Edit .env with your AWS credentials and Bedrock model ID
```

### 2. Build & Run
```bash
docker-compose up --build -d
```

### 3. Access
- **Frontend:** http://localhost:3002
- **Backend API:** http://localhost:5002
- **Health Check:** http://localhost:5002/health

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/upload` | Upload & analyze document |
| GET | `/api/documents` | List all documents |
| GET | `/api/documents/<id>` | Get document details |
| DELETE | `/api/documents/<id>` | Delete document |
| POST | `/api/documents/<id>/save` | Save to knowledge base |
| GET | `/api/analysis/<id>` | Full analysis results |
| POST | `/api/chat` | Knowledge base chat |
| GET | `/api/chat/<id>/history` | Chat history |
| GET | `/api/trends` | Score trend data |

## Features

- 📊 **Compliance scoring** with detailed breakdowns
- 🛡️ **Security analysis** with vulnerability detection
- ⚠️ **Risk assessment** with severity ratings
- 📋 **Framework mapping** (ISO 27001, SOC2, NIST, CIS, GDPR, HIPAA)
- 🔍 **Gap detection** for missing policies
- 💡 **Auto-suggestions** for improvements
- 📈 **Trend tracking** over time
- 💬 **Knowledge base chat** — ask questions about saved documents
- 🎨 **Premium dark-mode UI** with glassmorphism design
