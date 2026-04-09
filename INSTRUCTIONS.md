# Claude Code Instruction: Build a Mini Coding Assistant

## Context

You are working in the repo `dummy_001` (https://github.com/BrendanJamesLynskey/dummy_001).
This is currently an empty/dummy repo. We are building a minimal coding assistant CLI tool
inspired by Sebastian Raschka’s mini-coding-agent, but as our own clean-room implementation.

The backend is **Ollama** (local models). The default model is `qwen3.5:4b`.

## Goal

Create a fully working, single-file Python coding assistant with these six components:

1. Live Repo Context
1. Prompt Shape & Cache Reuse
1. Structured Tools with Validation & Permissions
1. Context Reduction & Output Management
1. Transcripts, Memory & Session Resumption
1. Delegation & Bounded Subagents

The project should have **zero external Python dependencies** — stdlib only + Ollama’s HTTP API.

## File Structure to Create

```
dummy_001/
├── README.md
├── LICENSE                  # Apache-2.0
├── pyproject.toml
├── coding_assistant.py      # The single-file agent (all logic here)
├── EXAMPLE.md               # A walkthrough example session
└── tests/
    └── test_tools.py        # Basic unit tests for tool parsing & validation
```

## Detailed Spec for coding_assistant.py

### Architecture Overview

The agent runs an interactive REPL loop:

1. User types a task
1. Agent builds a prompt (system prefix + workspace context + memory + conversation history + user request)
1. Sends to Ollama `/api/generate` endpoint
1. Parses model output for `<tool>...</tool>` or `<answer>...</answer>` tags
1. If tool call: validate, approve if risky, execute, append result to history, loop back to step 3
1. If answer: display to user, wait for next input
1. Enforce a max-steps limit per user request (default 8)

### Component 1: Live Repo Context (class WorkspaceContext)

- On startup, collect:
  - Git root, branch, status (via subprocess)
  - File tree (2 levels deep, skip .git, node_modules, **pycache**, .venv)
  - Contents of README.md, AGENTS.md, CLAUDE.md if they exist (first 200 lines each)
- Serialize as a compact text block for the system prompt
- Method: `snapshot() -> str`

### Component 2: Prompt Shape & Cache Reuse

- Split the prompt into two parts:
  - **Stable prefix**: system instructions + workspace snapshot + tool definitions (doesn’t change between turns)
  - **Turn state**: memory summary + conversation history + current user request
- The stable prefix is built once per session and reused
- Function: `build_prefix(workspace: WorkspaceContext, tools: list[dict]) -> str`
- Function: `build_turn(prefix: str, memory: str, history: list[dict], user_msg: str) -> str`

### Component 3: Structured Tools, Validation & Permissions

Implement these tools:

|Tool Name |Args                               |Risky?|Description                  |
|----------|-----------------------------------|------|-----------------------------|
|list_files|path (str)                         |No    |List files in a directory    |
|read_file |path (str)                         |No    |Read file contents           |
|search    |pattern (str), path (str, optional)|No    |Grep-like search across files|
|write_file|path (str), content (str)          |Yes   |Write/overwrite a file       |
|shell     |command (str)                      |Yes   |Run a shell command          |
|note      |text (str)                         |No    |Add a note to session memory |
|delegate  |task (str)                         |No    |Spawn a bounded sub-agent    |

Each tool call from the model must be inside `<tool>` XML tags with JSON body:

```xml
<tool>{"name": "read_file", "args": {"path": "src/main.py"}}</tool>
```

Final answers must be in:

```xml
<answer>The tests pass now. I fixed the import on line 12.</answer>
```

Implement:

- `parse_model_output(text: str) -> tuple[str, dict | str | None]`
  Returns (“tool”, {name, args}) or (“answer”, answer_text) or (“error”, raw_text)
- `validate_tool(name: str, args: dict) -> tuple[bool, str]`
  Check tool exists, required args present, paths don’t escape workspace
- `approve_tool(name: str, args: dict, mode: str) -> bool`
  If mode==“ask”: print the action and prompt y/n
  If mode==“auto”: return True
  If mode==“never”: return False for risky tools
- `run_tool(name: str, args: dict, workspace_root: str) -> str`
  Execute and return result string (truncated if over 4000 chars)

Path safety: all file paths must resolve inside the workspace root. Reject any path with `..` that escapes.

### Component 4: Context Reduction & Output Management

- `clip(text: str, max_chars: int = 4000) -> str`
  Truncate long outputs, keeping first and last portions with a “[…clipped…]” marker
- `compress_history(history: list[dict], max_entries: int = 20) -> list[dict]`
  When history exceeds max_entries, summarize older entries into a compact block
  Keep the most recent 10 entries verbatim, summarize the rest as bullet points
- Deduplicate consecutive reads of the same file (keep only the latest)

### Component 5: Transcripts, Memory & Session Resumption

- Class `SessionStore`:
  - Sessions saved as JSON in `.coding-assistant/sessions/{session_id}.json`
  - Session ID format: `YYYYMMDD-HHMMSS-{6 random hex chars}`
  - Each session stores: id, started_at, workspace_root, history, memory_notes, model_name
  - `save()`, `load(session_id)`, `latest()` methods
- Distilled memory:
  - Separate from raw history — a list of short notes (auto-generated + user-added via `note` tool)
  - Auto-notes: track which files were modified, which commands were run
  - Method: `memory_text() -> str` returns formatted memory for prompt injection

### Component 6: Delegation & Bounded Subagents

- Tool `delegate`:
  - Spawns a sub-agent loop with its own step counter (max 4 steps)
  - Sub-agent gets: parent’s workspace context + the delegated task only (not full parent history)
  - Sub-agent can use all tools EXCEPT delegate (no recursive delegation)
  - Returns the sub-agent’s final answer to the parent
  - If sub-agent hits max steps without an answer, return a summary of what it tried

### Interactive REPL Commands

Slash commands handled by the REPL, not sent to the model:

- `/help` — list commands
- `/memory` — show distilled memory
- `/session` — show session file path
- `/reset` — clear history and memory, stay in REPL
- `/exit` or `/quit` — exit

### CLI Arguments (use argparse)

- `--cwd` — workspace directory (default: `.`)
- `--model` — Ollama model name (default: `qwen3.5:4b`)
- `--host` — Ollama server URL (default: `http://127.0.0.1:11434`)
- `--timeout` — Ollama request timeout in seconds (default: 300)
- `--resume` — resume session by ID or `latest`
- `--approval` — `ask`, `auto`, or `never` (default: `ask`)
- `--max-steps` — max tool turns per user request (default: 8)
- `--max-tokens` — max model output tokens per step (default: 512)
- `--temperature` — sampling temperature (default: 0.2)

### System Prompt Template

The system prompt should instruct the model that:

- It is a coding assistant operating inside a workspace
- It must respond with EITHER a `<tool>` block OR an `<answer>` block, never both, never neither
- It should think step by step but keep reasoning concise
- Available tools are listed with their schemas
- It should prefer reading files and searching before making changes
- It should explain what it’s doing before acting

### Ollama Integration

- Use `urllib.request` to POST to `{host}/api/generate`
- Request body: `{"model": model, "prompt": full_prompt, "stream": false, "options": {"temperature": temp, "num_predict": max_tokens, "top_p": 0.9}}`
- Parse response JSON for the `response` field
- Handle connection errors gracefully with retry (1 retry after 2s)

## README.md Content

Write a clear README covering:

- What this project is (a minimal coding assistant for learning/experimentation)
- Requirements (Python 3.10+, Ollama)
- Setup instructions (clone, install Ollama, pull a model)
- Usage examples
- CLI flags reference
- Architecture overview referencing the 6 components
- Credit to Sebastian Raschka’s mini-coding-agent as inspiration
- Apache-2.0 license

## pyproject.toml

- Name: `coding-assistant`
- Version: `0.1.0`
- Python requires: `>=3.10`
- Entry point: `coding-assistant = "coding_assistant:main"`
- No dependencies

## EXAMPLE.md

Write a realistic example session showing:

1. User asks to “list the files and read the README”
1. Agent uses list_files, then read_file
1. User asks to “create a hello world Python script”
1. Agent uses write_file (with approval prompt shown)
1. User asks to “run the script”
1. Agent uses shell tool

## tests/test_tools.py

Write tests using unittest (stdlib only) covering:

- parse_model_output correctly extracts tool calls
- parse_model_output correctly extracts answers
- parse_model_output handles malformed output
- validate_tool rejects unknown tools
- validate_tool rejects path traversal
- clip truncates long strings correctly
- WorkspaceContext.snapshot returns a string (basic smoke test)

## Execution Order

1. Create all files in the order: LICENSE, pyproject.toml, coding_assistant.py, README.md, EXAMPLE.md, tests/test_tools.py
1. Run `python -m pytest tests/ -v` to verify tests pass
1. Run `python coding_assistant.py --help` to verify CLI works
1. Git add, commit with message “feat: initial coding assistant with 6 core agent components”, and push

## Important Notes

- Everything in coding_assistant.py must be in ONE file — no splitting into modules
- Zero external dependencies — stdlib + Ollama HTTP API only
- Keep the code well-commented, referencing which of the 6 components each section implements
- Use type hints throughout
- Target ~600-800 lines for coding_assistant.py — enough to be complete but readable
