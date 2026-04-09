# Coding Assistant

A minimal, single-file coding assistant CLI tool powered by [Ollama](https://ollama.com/) (local models). Inspired by [Sebastian Raschka's mini-coding-agent](https://github.com/rasbt/mini-coding-agent).

This project is designed for learning and experimentation with agentic coding patterns. It has **zero external Python dependencies** — it uses only the Python standard library and Ollama's HTTP API.

## Requirements

- **Python 3.10+**
- **Ollama** installed and running locally

## Setup

```bash
# 1. Clone the repo
git clone https://github.com/BrendanJamesLynskey/dummy_001.git
cd dummy_001

# 2. Install Ollama (see https://ollama.com/download)
# 3. Pull a model
ollama pull qwen3.5:4b

# 4. Start Ollama (if not already running)
ollama serve
```

## Usage

```bash
# Basic usage — runs in the current directory
python coding_assistant.py

# Specify a workspace and model
python coding_assistant.py --cwd /path/to/project --model llama3.2:3b

# Auto-approve risky tool calls (for trusted workspaces)
python coding_assistant.py --approval auto

# Resume the most recent session
python coding_assistant.py --resume

# Resume a specific session by ID
python coding_assistant.py --resume 20250401-143022-a1b2c3
```

## CLI Flags

| Flag             | Default                    | Description                              |
|------------------|----------------------------|------------------------------------------|
| `--cwd`          | `.`                        | Workspace directory                      |
| `--model`        | `qwen3.5:4b`               | Ollama model name                        |
| `--host`         | `http://127.0.0.1:11434`   | Ollama server URL                        |
| `--timeout`      | `300`                      | Request timeout in seconds               |
| `--resume`       | —                          | Resume session by ID or `latest`         |
| `--approval`     | `ask`                      | Tool approval mode: `ask`, `auto`, `never` |
| `--max-steps`    | `8`                        | Max tool turns per user request          |
| `--max-tokens`   | `512`                      | Max model output tokens per step         |
| `--temperature`  | `0.2`                      | Sampling temperature                     |

## Interactive Commands

| Command    | Description                    |
|------------|--------------------------------|
| `/help`    | List available commands        |
| `/memory`  | Show distilled memory notes    |
| `/session` | Show session file path         |
| `/reset`   | Clear history and memory       |
| `/exit`    | Exit the assistant             |

## Architecture

The assistant is built around **6 core components**, all in a single file (`coding_assistant.py`):

1. **Live Repo Context** (`WorkspaceContext`) — Collects git info, file tree, and guide files (README, AGENTS.md, CLAUDE.md) to give the model workspace awareness.

2. **Prompt Shape & Cache Reuse** (`build_prefix` / `build_turn`) — Splits the prompt into a stable prefix (system instructions + workspace snapshot) reused across turns, and a dynamic turn payload (memory + history + user request).

3. **Structured Tools with Validation & Permissions** — Seven tools (`list_files`, `read_file`, `search`, `write_file`, `shell`, `note`, `delegate`) with XML-based invocation, argument validation, path-safety checks, and configurable approval for risky operations.

4. **Context Reduction & Output Management** (`clip` / `compress_history`) — Truncates long outputs and compresses conversation history to stay within context limits.

5. **Transcripts, Memory & Session Resumption** (`SessionStore`) — Persists sessions as JSON for resumption. Maintains distilled memory notes (auto-generated and user-added) separate from raw history.

6. **Delegation & Bounded Subagents** (`delegate` tool) — Spawns sub-agent loops with independent step counters (max 4 steps), restricted tool access (no recursive delegation), and automatic summarization on timeout.

## License

This project is licensed under the Apache License 2.0 — see the [LICENSE](LICENSE) file for details.

## Acknowledgements

Inspired by [Sebastian Raschka's mini-coding-agent](https://github.com/rasbt/mini-coding-agent).
