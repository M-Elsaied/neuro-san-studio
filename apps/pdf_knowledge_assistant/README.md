# PDF Knowledge Assistant

A Flask-based web application that enables users to upload PDF documents, extract knowledge into a topic-based long-term memory system, and query both document-specific information and synthesized insights.

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Key Design Decisions](#key-design-decisions)
- [Component Breakdown](#component-breakdown)
- [Data Flow](#data-flow)
- [File Structure](#file-structure)
- [Implementation Status](#implementation-status)
- [Dependencies](#dependencies)
- [Usage](#usage)
- [Development Notes](#development-notes)

---

## Overview

### Purpose

This application combines the power of:
- **RAG (Retrieval-Augmented Generation)**: For document-specific queries
- **Topic-Based Memory System**: For long-term knowledge persistence and synthesis

Unlike the `conscious_assistant` which runs in a continuous thinking loop, this application operates on-demand, making it more token-efficient while still maintaining long-term memory capabilities.

### Core Capabilities

1. **PDF Upload & Processing**: Upload PDF documents via web interface
2. **Knowledge Extraction**: Automatically extract key facts and organize them by topics
3. **Dual-Layer Retrieval**:
   - **Layer 1 (RAG)**: Fast, specific document queries using vector similarity
   - **Layer 2 (Topic Memory)**: Synthesized insights from accumulated knowledge
4. **Persistent Knowledge Base**: All knowledge persists across sessions
5. **Interactive Querying**: Ask questions that leverage both layers intelligently

---

## Architecture

### High-Level Design

```
┌─────────────────────────────────────────────────────────────────┐
│                        User Interface (Flask)                    │
│  - PDF Upload (drag-and-drop)                                   │
│  - Chat Interface (Q&A)                                          │
│  - Topic Explorer                                                │
│  - Document Manager                                              │
└────────────────┬────────────────────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────────────────────┐
│              PDF Knowledge Agent (neuro-san)                     │
│  - Decision making: Which layer(s) to query?                    │
│  - Synthesis: Combine RAG + Memory results                      │
└────────┬──────────────────────────────────┬─────────────────────┘
         │                                  │
         ▼                                  ▼
┌─────────────────────┐          ┌──────────────────────┐
│   RAG Layer (L1)    │          │  Topic Memory (L2)   │
│                     │          │                      │
│ - Vector Store      │          │ - Topic-based facts  │
│ - PDF chunks        │          │ - Timestamped        │
│ - Similarity search │          │ - Organized          │
│ - Fast retrieval    │          │ - Synthesized        │
└─────────────────────┘          └──────────────────────┘
         │                                  │
         ▼                                  ▼
  pdf_vector_store.json            TopicMemory.json
```

### Agent Network Architecture

```
Front-Man Agent (pdf_knowledge_assistant)
├── PDF Tools
│   ├── add_pdf_to_knowledge       # Add PDFs to vector store
│   ├── query_pdf_knowledge        # Query vector store
│   └── extract_pdf_knowledge      # Extract structured facts
└── Memory Tools
    ├── commit_to_memory           # Store facts by topic
    ├── recall_memory              # Retrieve facts by topic
    ├── list_topics                # List all topics
    └── reorganize_memory          # Derive new insights
```

---

## Key Design Decisions

### 1. Dual-Layer Approach (Option A)

**Why?**
- **Performance**: RAG excels at finding specific passages; Topic Memory excels at synthesis
- **Flexibility**: Agent can choose which layer(s) to use based on query intent
- **Scalability**: Each layer can be optimized independently

**Trade-off**: Slightly more complex than unified approach, but significantly more powerful

### 2. Reuse BaseRag Infrastructure

**Decision**: Create new specialized coded tools that extend `BaseRag` rather than reusing `pdf_rag.hocon`

**Rationale**:
- `pdf_rag.hocon` designed for URL-based PDFs with hardcoded sources
- We need file upload support and accumulative knowledge
- Need separation: Add PDF ≠ Query PDF
- Better integration with topic memory system
- Consistent with `kwik_agents` architecture pattern

**What We Reuse**:
- ✅ `coded_tools/base_rag.py` - All vector store logic
- ✅ `coded_tools/kwik_agents/*_memory.py` - All memory tools
- ✅ Document loading, chunking, embedding infrastructure

**What We Build New**:
- 🆕 Three specialized coded tools for our workflow
- 🆕 Agent network configuration (HOCON)
- 🆕 Flask application with file upload
- 🆕 Web interface

### 3. On-Demand Execution (No Continuous Thinking)

**Why?**
- **Token Efficiency**: Only runs when user submits query or uploads PDF
- **Cost-Effective**: No background processing consuming API calls
- **Appropriate for Use Case**: Knowledge base doesn't need to "think" continuously

**Difference from Conscious Assistant**:
| Feature | Conscious Assistant | PDF Knowledge Assistant |
|---------|---------------------|-------------------------|
| Execution | Continuous loop | On-demand |
| Silence handling | Processes silence | No background processing |
| Token usage | High | Low |
| Use case | Conversational AI | Knowledge retrieval |

### 4. File Upload Workflow

```
User uploads PDF
    ↓
Flask saves to ./static/uploads/
    ↓
File path sent to agent
    ↓
Agent calls add_pdf_to_knowledge(file_path)
    ↓
Tool uses BaseRag to process PDF
    ↓
Parallel operations:
    ├── Add to vector store (RAG layer)
    └── Extract facts → commit_to_memory (Topic layer)
    ↓
User gets confirmation + summary
```

---

## Component Breakdown

### New Coded Tools (coded_tools/kwik_agents/)

#### 1. `add_pdf_to_knowledge.py`

**Purpose**: Add uploaded PDF to the persistent knowledge base

**Parameters**:
```json
{
  "file_path": "path/to/uploaded.pdf"
}
```

**Behavior**:
1. Load existing vector store OR create new one
2. Process PDF using `BaseRag.load_documents()` (PyMuPDFLoader)
3. Add new document chunks to vector store
4. Save updated vector store to disk
5. Update DocumentRegistry.json with metadata

**Returns**: `"✅ Added {filename} - {N} pages, {M} chunks processed"`

**Extends**: `CodedTool`, `BaseRag`

---

#### 2. `query_pdf_knowledge.py`

**Purpose**: Query the RAG layer for document-specific information

**Parameters**:
```json
{
  "query": "What are the key requirements?"
}
```

**Behavior**:
1. Load existing vector store (fast, no document processing)
2. Perform similarity search using embeddings
3. Retrieve top-k relevant chunks
4. Return concatenated results

**Returns**: Relevant document excerpts

**Extends**: `CodedTool`, `BaseRag`

---

#### 3. `extract_pdf_knowledge.py`

**Purpose**: Extract structured knowledge from PDFs for topic memory

**Parameters**:
```json
{
  "file_path": "path/to/uploaded.pdf",
  "focus_areas": ["requirements", "pricing", "timeline"]  // optional
}
```

**Behavior**:
1. Use RAG to query document for summaries/key points
2. LLM analyzes content and identifies:
   - Key topics (e.g., "budget", "requirements", "stakeholders")
   - Important facts under each topic
   - Relationships between concepts
3. Structures output for consumption by `commit_to_memory`
4. Agent then calls `commit_to_memory` for each fact

**Returns**: Structured JSON of topics and facts

**Extends**: `CodedTool`, `BaseRag`

**Example Output**:
```json
{
  "topics": [
    {
      "topic": "project_requirements",
      "facts": [
        "Must support 1000+ concurrent users",
        "Response time < 200ms for 95th percentile",
        "SOC2 compliance required"
      ]
    },
    {
      "topic": "budget",
      "facts": [
        "Total budget: $500K",
        "Phase 1 allocation: $200K",
        "Contingency: 15%"
      ]
    }
  ]
}
```

---

### Agent Network Configuration

**File**: `registries/pdf_knowledge_agent.hocon`

**Key Sections**:

```hocon
{
    "llm_config": {
        "model_name": "gpt-4o",  # or your preferred model
    },
    "max_iterations": 40000,
    "max_execution_seconds": 6000,

    "tools": [
        {
            "name": "pdf_knowledge_assistant",
            "function": {
                "description": "I help you extract and query knowledge from PDF documents."
            },
            "instructions": """
            You are a knowledge extraction and retrieval assistant.

            When a user uploads a PDF:
            1. Call add_pdf_to_knowledge with the file path
            2. Call extract_pdf_knowledge to analyze content
            3. For each topic/fact pair, call commit_to_memory
            4. Summarize what was learned

            When a user asks a question:
            1. Call list_topics to see what knowledge exists
            2. Call recall_memory for relevant topics
            3. Call query_pdf_knowledge for document-specific details
            4. Synthesize a comprehensive answer from both sources

            Always indicate which source(s) your answer comes from.
            """,
            "tools": [
                "add_pdf_to_knowledge",
                "query_pdf_knowledge",
                "extract_pdf_knowledge",
                "commit_to_memory",
                "recall_memory",
                "list_topics",
                "reorganize_memory"
            ]
        },
        # Tool definitions for each coded tool...
    ]
}
```

---

### Flask Application

#### `pdf_knowledge_assistant.py`

**Purpose**: Core session management and processing logic

**Key Functions**:
- `set_up_pdf_knowledge_assistant()`: Initialize agent session
- `process_query(session, thread, user_input)`: Handle user queries
- `process_pdf_upload(session, thread, file_path)`: Handle PDF uploads
- `tear_down_pdf_knowledge_assistant(session)`: Cleanup

**Pattern**: Similar to `conscious_assistant.py` but on-demand instead of continuous

---

#### `interface_flask.py`

**Purpose**: Web server with file upload and chat interface

**Key Routes**:
- `GET /`: Main interface
- `POST /upload`: Handle PDF uploads
- `WebSocket /chat`: Real-time Q&A
- `GET /topics`: List all topics (AJAX)
- `GET /documents`: List uploaded docs (AJAX)
- `DELETE /document/<id>`: Remove document
- `GET /shutdown`: Cleanup and shutdown

**Features**:
- File upload with drag-and-drop support
- Real-time chat using Socket.IO
- Topic browsing
- Document management

---

### Web Interface

#### `templates/index.html`

**Sections**:
1. **Header**: App title and stats (# docs, # topics)
2. **Upload Section**:
   - Drag-and-drop area
   - File picker button
   - Upload progress indicator
3. **Chat Section**:
   - Question input
   - Answer display
   - Source attribution (RAG vs Memory)
4. **Topic Explorer** (collapsible):
   - Tree view of topics
   - Click to expand facts
   - Timestamps
5. **Document Manager** (collapsible):
   - List of uploaded PDFs
   - Delete option
   - Re-process option

**Technology**: HTML5, Socket.IO, vanilla JavaScript (no heavy frameworks)

---

#### `static/style.css`

Modern, clean design similar to conscious_assistant but adapted for:
- File upload zones
- Document cards
- Topic tree navigation
- Split-pane layout (chat + knowledge explorer)

---

## Data Flow

### PDF Upload Flow

```
┌──────────────────────────────────────────────────────────────────────┐
│ 1. User uploads PDF via web interface                                │
└───────────────────────────┬──────────────────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 2. Flask saves to ./static/uploads/{unique_filename}.pdf             │
└───────────────────────────┬──────────────────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 3. Agent invokes add_pdf_to_knowledge(file_path)                     │
│    - PyMuPDFLoader extracts text                                     │
│    - RecursiveCharacterTextSplitter chunks text                      │
│    - OpenAIEmbeddings creates vectors                                │
│    - InMemoryVectorStore adds chunks                                 │
│    - Saved to pdf_vector_store.json                                  │
└───────────────────────────┬──────────────────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 4. Agent invokes extract_pdf_knowledge(file_path)                    │
│    - Queries RAG for document summary                                │
│    - LLM identifies topics and facts                                 │
│    - Returns structured data                                         │
└───────────────────────────┬──────────────────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 5. For each topic/fact pair:                                         │
│    Agent invokes commit_to_memory(topic, fact)                       │
│    - Adds timestamped fact to TopicMemory.json                       │
└───────────────────────────┬──────────────────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 6. Update DocumentRegistry.json                                      │
│    - Filename, upload date, page count, topics                       │
└───────────────────────────┬──────────────────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 7. Return success message to user                                    │
│    "✅ Processed RFP_Template.pdf - 25 pages, 8 topics extracted"    │
└──────────────────────────────────────────────────────────────────────┘
```

### Query Flow

```
┌──────────────────────────────────────────────────────────────────────┐
│ 1. User asks: "What are the key requirements from the RFP documents?"│
└───────────────────────────┬──────────────────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 2. Agent analyzes query intent                                       │
│    - Needs specific details → Use RAG                                │
│    - Needs synthesis → Use Memory                                    │
│    - This query needs BOTH                                           │
└───────────────────────────┬──────────────────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 3. Agent invokes list_topics()                                       │
│    Returns: ["requirements", "budget", "timeline", ...]              │
└───────────────────────────┬──────────────────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 4. Agent invokes recall_memory(topic="requirements")                 │
│    Returns: All facts stored under "requirements" topic              │
└───────────────────────────┬──────────────────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 5. Agent invokes query_pdf_knowledge(query="key requirements")       │
│    Returns: Relevant document chunks from vector store               │
└───────────────────────────┬──────────────────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 6. Agent synthesizes answer from both sources                        │
│    - Lists requirements from memory                                  │
│    - Adds specific details from RAG                                  │
│    - Indicates sources                                               │
└───────────────────────────┬──────────────────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 7. Returns to user:                                                  │
│    "Based on your RFP documents, the key requirements are:           │
│     1. Support 1000+ concurrent users [from: RFP_Template.pdf]       │
│     2. Response time < 200ms [from: memory]                          │
│     ..."                                                             │
└──────────────────────────────────────────────────────────────────────┘
```

---

## File Structure

```
neuro-san-studio/
│
├── apps/
│   └── pdf_knowledge_assistant/
│       ├── __init__.py
│       ├── README.md                          # This file
│       ├── requirements.txt                   # Python dependencies
│       ├── pdf_knowledge_assistant.py         # Core logic
│       ├── interface_flask.py                 # Web server
│       ├── templates/
│       │   └── index.html                     # Web UI
│       └── static/
│           ├── style.css                      # Styling
│           └── uploads/                       # Temporary PDF storage
│               └── .gitkeep
│
├── coded_tools/
│   ├── base_rag.py                            # ✅ Existing - REUSE
│   ├── pdf_rag.py                             # ✅ Existing - REFERENCE
│   └── kwik_agents/
│       ├── __init__.py                        # ✅ Existing
│       ├── commit_to_memory.py                # ✅ Existing
│       ├── recall_memory.py                   # ✅ Existing
│       ├── list_topics.py                     # ✅ Existing
│       ├── reorganize_memory.py               # ✅ Existing
│       ├── add_pdf_to_knowledge.py            # 🆕 NEW
│       ├── query_pdf_knowledge.py             # 🆕 NEW
│       └── extract_pdf_knowledge.py           # 🆕 NEW
│
├── registries/
│   ├── manifest.hocon                         # ✏️ UPDATE (add our agent)
│   ├── conscious_agent.hocon                  # ✅ Existing - REFERENCE
│   ├── pdf_rag.hocon                          # ✅ Existing - REFERENCE
│   └── pdf_knowledge_agent.hocon              # 🆕 NEW
│
└── Data files (created at runtime):
    ├── TopicMemory.json                       # Topic-based knowledge
    ├── pdf_vector_store.json                  # RAG vector store
    └── DocumentRegistry.json                  # Uploaded file metadata
```

---

## Implementation Status

### ✅ Completed
- [x] Architecture design
- [x] RAG infrastructure analysis
- [x] Design decisions documented
- [x] Directory structure created
- [x] README documentation
- [x] Coded tools implementation (3 new tools)
- [x] Agent network configuration (HOCON)
- [x] Flask application with file upload
- [x] Web interface (HTML + CSS)
- [x] Integration with neuro-san framework
- [x] Document registry and tracking
- [x] Statistics dashboard

### ⚠️ Partial / Issues
- [⚠️] Memory extraction workflow - Agent identifies topics but doesn't reliably call commit_to_memory
- [⚠️] Need to strengthen agent instructions or add sub-agent for extraction

### ⏳ Pending
- [ ] Fix memory extraction issue
- [ ] Comprehensive testing with various PDF types
- [ ] Performance optimization
- [ ] Production deployment configuration

---

## Dependencies

### Required Python Packages

```txt
# Core framework
neuro-san>=0.5.38

# Web framework
flask>=2.3.0
flask-socketio>=5.3.0

# PDF processing
pymupdf>=1.25.5

# RAG dependencies (from BaseRag)
langchain-community
langchain-core
langchain-openai
langchain-text-splitters
langchain-postgres  # Optional, for PostgreSQL vector store

# Vector store
asyncpg  # For PostgreSQL support

# Utilities
sqlalchemy
python-socketio
```

### Environment Variables

```bash
# Required for OpenAI embeddings and LLM
OPENAI_API_KEY=sk-...

# Required for agent network
AGENT_MANIFEST_FILE=registries/manifest.hocon
AGENT_TOOL_PATH=coded_tools

# Optional: PostgreSQL vector store
POSTGRES_USER=user
POSTGRES_PASSWORD=password
POSTGRES_HOST=localhost
POSTGRES_PORT=6024
POSTGRES_DB=vectorstore
```

---

## Usage

### Running the Application

```bash
# 1. Ensure you're in the neuro-san-studio directory
cd neuro-san-studio

# 2. Set environment variables
export OPENAI_API_KEY=sk-...
export AGENT_MANIFEST_FILE=registries/manifest.hocon
export AGENT_TOOL_PATH=coded_tools

# 3. Enable the agent in manifest.hocon
# Change "pdf_knowledge_agent.hocon": false to true

# 4. Run the Flask app
python apps/pdf_knowledge_assistant/interface_flask.py

# 5. Open browser to http://localhost:5002
```

### Uploading PDFs

1. Navigate to http://localhost:5002
2. Drag PDF file to upload area OR click to browse
3. Wait for processing (progress bar will show status)
4. View extracted topics in the Topic Explorer
5. Documents appear in Document Manager

### Querying Knowledge

1. Type question in chat input
2. Press Enter or click Send
3. Agent will:
   - Check relevant topics in memory
   - Query RAG for specific details
   - Synthesize comprehensive answer
4. Answer shows source attribution

### Managing Knowledge

**View Topics**:
- Click "Topic Explorer" to expand
- Browse organized knowledge by topic
- See timestamps for each fact

**Manage Documents**:
- Click "Document Manager" to expand
- View all uploaded PDFs
- Delete documents (removes from registry, not from knowledge base)
- Re-process to extract additional knowledge

---

## Development Notes

### Design Patterns Used

1. **CodedTool Pattern**: All custom tools extend `CodedTool` base class
2. **Multiple Inheritance**: Tools extend both `CodedTool` and `BaseRag` to reuse functionality
3. **Async/Await**: All I/O operations are asynchronous for performance
4. **Dependency Injection**: Configuration passed via HOCON, not hardcoded
5. **Separation of Concerns**:
   - Business logic in coded tools
   - Orchestration in agent network
   - Presentation in Flask/HTML

### Key Differences from Similar Apps

**vs. Conscious Assistant**:
- ❌ No continuous thinking loop
- ✅ File upload support
- ✅ Document-focused instead of conversation-focused
- ✅ Dual-layer retrieval (RAG + Memory)

**vs. PDF RAG**:
- ✅ Dynamic file uploads (not hardcoded URLs)
- ✅ Persistent, accumulative knowledge base
- ✅ Topic-based memory integration
- ✅ Separate add/query operations
- ✅ Long-term knowledge synthesis

### Testing Strategy

1. **Unit Tests**: Test each coded tool independently
2. **Integration Tests**: Test agent network workflows
3. **End-to-End Tests**: Test full upload → extract → query flow
4. **Manual Tests**:
   - Upload various PDF types
   - Test with large documents (100+ pages)
   - Test with multiple documents
   - Verify knowledge synthesis
   - Test edge cases (empty PDFs, scanned images, etc.)

### Known Limitations

1. **PDF Processing**: Only text-based PDFs (scanned images require OCR)
2. **Vector Store**: In-memory store loaded on each query (consider PostgreSQL for production)
3. **File Cleanup**: Uploaded files not automatically deleted (implement cleanup policy)
4. **Concurrent Users**: Single session per app instance (add session management for multi-user)
5. **Knowledge Updates**: Cannot update/delete facts from memory (only add)

### Future Enhancements

- [ ] OCR support for scanned PDFs
- [ ] Multi-user support with separate knowledge bases
- [ ] Knowledge graph visualization
- [ ] Export knowledge base (JSON, CSV, etc.)
- [ ] Batch PDF upload
- [ ] Document comparison ("What's different between doc A and B?")
- [ ] Citation tracking (which page/document a fact came from)
- [ ] Admin interface for memory management
- [ ] API endpoints for programmatic access
- [ ] PostgreSQL vector store for production use

---

## Troubleshooting

### Common Issues

**Issue**: "Failed to create vector store"
- **Solution**: Check OPENAI_API_KEY is set and valid

**Issue**: "PDF file not found"
- **Solution**: Check file was uploaded to ./static/uploads/

**Issue**: "NO TOPICS YET!" after uploading PDF
- **Symptom**: Agent says it will commit facts to memory but topics don't appear
- **Root Cause**: Agent identifies topics but doesn't actually call commit_to_memory tool
- **Solution**: This is a known issue with the agent's instruction following. The agent needs stronger prompting to actually execute tool calls vs just describing what it will do
- **Workaround**: Manually ask "Please extract and commit the topics from the uploaded document to memory" after upload
- **Status**: Under investigation - may need to adjust agent instructions or create a dedicated extraction sub-agent

**Issue**: Agent doesn't respond
- **Solution**: Check neuro-san server is running on port 30011

**Issue**: WebSocket connection failed
- **Solution**: Check Flask app is running and port 5002 is not blocked

**Issue**: Module import errors when running interface_flask.py
- **Solution**: Run from the app directory: `cd apps/pdf_knowledge_assistant && python interface_flask.py`

---

## References

### Related Files (for reference)

- `apps/conscious_assistant/` - Similar app structure, continuous thinking pattern
- `coded_tools/base_rag.py` - RAG infrastructure we're reusing
- `coded_tools/pdf_rag.py` - PDF processing pattern reference
- `coded_tools/kwik_agents/commit_to_memory.py` - Memory tool pattern
- `registries/conscious_agent.hocon` - Agent network pattern
- `registries/pdf_rag.hocon` - RAG agent pattern

### Documentation

- neuro-san SDK: [Internal documentation]
- LangChain RAG: https://python.langchain.com/docs/tutorials/rag/
- Flask-SocketIO: https://flask-socketio.readthedocs.io/

---

## License

Copyright (C) 2023-2025 Cognizant Digital Business, Evolutionary AI.
All Rights Reserved.
Issued under the Academic Public License.

---

## Changelog

### 2025-11-12 - Initial Implementation
- Created application structure and directory layout
- Documented architecture and design decisions
- Implemented 3 new coded tools (add_pdf_to_knowledge, query_pdf_knowledge, extract_pdf_knowledge)
- Created agent network configuration (pdf_knowledge_agent.hocon)
- Built Flask web application with file upload and WebSocket support
- Designed and implemented web interface (HTML + CSS)
- Added document registry and statistics tracking
- Integrated with existing kwik_agents memory tools
- Updated manifest.hocon to include new agent
- Known Issue: Agent instruction following for commit_to_memory needs improvement
