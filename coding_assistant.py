#!/usr/bin/env python3
"""
coding_assistant.py — A minimal single-file coding assistant powered by Ollama.

Inspired by Sebastian Raschka's mini-coding-agent. Zero external dependencies;
uses only the Python standard library and Ollama's HTTP API.

Architecture (6 components):
  1. Live Repo Context        — WorkspaceContext
  2. Prompt Shape & Cache     — build_prefix / build_turn
  3. Structured Tools         — parse / validate / approve / run
  4. Context Reduction        — clip / compress_history
  5. Transcripts & Memory     — SessionStore
  6. Delegation & Subagents   — delegate tool
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import secrets
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# ═══════════════════════════════════════════════════════════════════════════════
# Component 1: Live Repo Context
# ═══════════════════════════════════════════════════════════════════════════════

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", ".coding-assistant"}


class WorkspaceContext:
    """Collect workspace metadata for injection into the system prompt."""

    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)

    # --- helpers -----------------------------------------------------------

    def _run_git(self, *args: str) -> str:
        try:
            result = subprocess.run(
                ["git", *args],
                capture_output=True,
                text=True,
                cwd=self.root,
                timeout=10,
            )
            return result.stdout.strip()
        except Exception:
            return ""

    def _file_tree(self, depth: int = 2) -> str:
        lines: list[str] = []

        def _walk(directory: str, prefix: str, level: int) -> None:
            if level > depth:
                return
            try:
                entries = sorted(os.listdir(directory))
            except OSError:
                return
            dirs = [e for e in entries if os.path.isdir(os.path.join(directory, e)) and e not in SKIP_DIRS]
            files = [e for e in entries if os.path.isfile(os.path.join(directory, e))]
            for f in files:
                lines.append(f"{prefix}{f}")
            for d in dirs:
                lines.append(f"{prefix}{d}/")
                _walk(os.path.join(directory, d), prefix + "  ", level + 1)

        _walk(self.root, "", 0)
        return "\n".join(lines)

    def _read_guide(self, name: str, max_lines: int = 200) -> str | None:
        path = os.path.join(self.root, name)
        if not os.path.isfile(path):
            return None
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()[:max_lines]
            return "".join(lines)
        except OSError:
            return None

    # --- public API --------------------------------------------------------

    def snapshot(self) -> str:
        """Return a compact text block describing the workspace."""
        parts: list[str] = []
        parts.append(f"Workspace root: {self.root}")

        branch = self._run_git("rev-parse", "--abbrev-ref", "HEAD")
        if branch:
            parts.append(f"Git branch: {branch}")
        status = self._run_git("status", "--short")
        if status:
            parts.append(f"Git status:\n{status}")

        parts.append(f"File tree:\n{self._file_tree()}")

        for guide in ("README.md", "AGENTS.md", "CLAUDE.md"):
            content = self._read_guide(guide)
            if content:
                parts.append(f"--- {guide} ---\n{content}")

        return "\n\n".join(parts)


# ═══════════════════════════════════════════════════════════════════════════════
# Component 3: Tool Definitions
# ═══════════════════════════════════════════════════════════════════════════════

TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "list_files",
        "description": "List files in a directory.",
        "args": {"path": {"type": "str", "required": True}},
        "risky": False,
    },
    {
        "name": "read_file",
        "description": "Read the contents of a file.",
        "args": {"path": {"type": "str", "required": True}},
        "risky": False,
    },
    {
        "name": "search",
        "description": "Grep-like search across files.",
        "args": {
            "pattern": {"type": "str", "required": True},
            "path": {"type": "str", "required": False},
        },
        "risky": False,
    },
    {
        "name": "write_file",
        "description": "Write or overwrite a file.",
        "args": {
            "path": {"type": "str", "required": True},
            "content": {"type": "str", "required": True},
        },
        "risky": True,
    },
    {
        "name": "shell",
        "description": "Run a shell command.",
        "args": {"command": {"type": "str", "required": True}},
        "risky": True,
    },
    {
        "name": "note",
        "description": "Add a note to session memory.",
        "args": {"text": {"type": "str", "required": True}},
        "risky": False,
    },
    {
        "name": "delegate",
        "description": "Spawn a bounded sub-agent to handle a task.",
        "args": {"task": {"type": "str", "required": True}},
        "risky": False,
    },
]

TOOL_NAMES = {t["name"] for t in TOOL_DEFS}
RISKY_TOOLS = {t["name"] for t in TOOL_DEFS if t["risky"]}


def _tool_schema_text() -> str:
    """Render tool definitions for the system prompt."""
    lines: list[str] = []
    for t in TOOL_DEFS:
        args_desc = ", ".join(
            f'{k} ({v["type"]}, {"required" if v.get("required") else "optional"})'
            for k, v in t["args"].items()
        )
        risky = " [RISKY — requires approval]" if t["risky"] else ""
        lines.append(f'- {t["name"]}({args_desc}): {t["description"]}{risky}')
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# Component 2: Prompt Shape & Cache Reuse
# ═══════════════════════════════════════════════════════════════════════════════

SYSTEM_INSTRUCTIONS = textwrap.dedent("""\
    You are a coding assistant operating inside a workspace.
    You MUST respond with EITHER a single <tool>...</tool> block OR a single <answer>...</answer> block.
    Never include both in the same response. Never omit both.

    Think step by step but keep reasoning concise.
    Prefer reading files and searching before making changes.
    Explain what you are doing before acting.

    Available tools:
    {tools}

    To call a tool, output exactly:
    <tool>{{"name": "tool_name", "args": {{"key": "value"}}}}</tool>

    To give a final answer, output exactly:
    <answer>Your answer here.</answer>
""")


def build_prefix(workspace: WorkspaceContext, tools: list[dict[str, Any]]) -> str:
    """Build the stable prompt prefix (system instructions + workspace snapshot + tool schemas)."""
    instructions = SYSTEM_INSTRUCTIONS.format(tools=_tool_schema_text())
    snap = workspace.snapshot()
    return f"{instructions}\n\n--- Workspace Context ---\n{snap}\n"


def build_turn(prefix: str, memory: str, history: list[dict[str, str]], user_msg: str) -> str:
    """Assemble the full prompt for a single turn."""
    parts = [prefix]
    if memory:
        parts.append(f"--- Memory Notes ---\n{memory}\n")
    if history:
        parts.append("--- Conversation History ---")
        for entry in history:
            parts.append(f'[{entry["role"]}] {entry["content"]}')
        parts.append("")
    parts.append(f"[user] {user_msg}")
    parts.append("[assistant]")
    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════════════════════
# Component 3 (cont.): Parse / Validate / Approve / Run
# ═══════════════════════════════════════════════════════════════════════════════

def parse_model_output(text: str) -> tuple[str, dict[str, Any] | str | None]:
    """Parse model output for <tool> or <answer> tags.

    Returns:
        ("tool", {"name": ..., "args": ...})
        ("answer", answer_text)
        ("error", raw_text)
    """
    tool_match = re.search(r"<tool>(.*?)</tool>", text, re.DOTALL)
    if tool_match:
        try:
            payload = json.loads(tool_match.group(1).strip())
            if "name" in payload:
                payload.setdefault("args", {})
                return ("tool", payload)
        except json.JSONDecodeError:
            pass
        return ("error", text)

    answer_match = re.search(r"<answer>(.*?)</answer>", text, re.DOTALL)
    if answer_match:
        return ("answer", answer_match.group(1).strip())

    return ("error", text)


def validate_tool(name: str, args: dict[str, Any], workspace_root: str = ".") -> tuple[bool, str]:
    """Validate that a tool call is well-formed and safe.

    Returns (ok, message).
    """
    if name not in TOOL_NAMES:
        return False, f"Unknown tool: {name}"

    tool_def = next(t for t in TOOL_DEFS if t["name"] == name)
    for arg_name, spec in tool_def["args"].items():
        if spec.get("required") and arg_name not in args:
            return False, f"Missing required argument: {arg_name}"

    # Path safety check
    for key in ("path",):
        if key in args:
            resolved = os.path.realpath(os.path.join(workspace_root, args[key]))
            ws_real = os.path.realpath(workspace_root)
            if not resolved.startswith(ws_real + os.sep) and resolved != ws_real:
                return False, f"Path escapes workspace: {args[key]}"

    return True, "ok"


def approve_tool(name: str, args: dict[str, Any], mode: str) -> bool:
    """Check whether a risky tool call is approved."""
    if name not in RISKY_TOOLS:
        return True
    if mode == "auto":
        return True
    if mode == "never":
        return False
    # mode == "ask"
    print(f"\n[approval required] {name}({json.dumps(args, indent=2)})")
    answer = input("Allow? [y/N] ").strip().lower()
    return answer in ("y", "yes")


def run_tool(
    name: str,
    args: dict[str, Any],
    workspace_root: str,
    memory_notes: list[str],
) -> str:
    """Execute a tool and return its result string."""
    root = os.path.realpath(workspace_root)

    if name == "list_files":
        target = os.path.realpath(os.path.join(root, args["path"]))
        try:
            entries = sorted(os.listdir(target))
            return "\n".join(entries) if entries else "(empty directory)"
        except OSError as exc:
            return f"Error: {exc}"

    if name == "read_file":
        target = os.path.realpath(os.path.join(root, args["path"]))
        try:
            with open(target, encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError as exc:
            return f"Error: {exc}"

    if name == "search":
        pattern = args["pattern"]
        search_root = os.path.realpath(os.path.join(root, args.get("path", ".")))
        results: list[str] = []
        try:
            compiled = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            return f"Invalid regex: {exc}"
        for dirpath, dirnames, filenames in os.walk(search_root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                fp = os.path.join(dirpath, fn)
                try:
                    with open(fp, encoding="utf-8", errors="replace") as fh:
                        for i, line in enumerate(fh, 1):
                            if compiled.search(line):
                                rel = os.path.relpath(fp, root)
                                results.append(f"{rel}:{i}: {line.rstrip()}")
                except OSError:
                    continue
                if len(results) >= 200:
                    break
            if len(results) >= 200:
                break
        return "\n".join(results) if results else "(no matches)"

    if name == "write_file":
        target = os.path.realpath(os.path.join(root, args["path"]))
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(args["content"])
            memory_notes.append(f"Wrote file: {args['path']}")
            return f"Wrote {len(args['content'])} bytes to {args['path']}"
        except OSError as exc:
            return f"Error: {exc}"

    if name == "shell":
        cmd = args["command"]
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, cwd=root, timeout=60
            )
            output = result.stdout
            if result.stderr:
                output += "\n[stderr]\n" + result.stderr
            if result.returncode != 0:
                output += f"\n[exit code {result.returncode}]"
            memory_notes.append(f"Ran command: {cmd}")
            return output.strip() if output.strip() else "(no output)"
        except subprocess.TimeoutExpired:
            return "Error: command timed out after 60s"
        except OSError as exc:
            return f"Error: {exc}"

    if name == "note":
        memory_notes.append(args["text"])
        return f"Noted: {args['text']}"

    if name == "delegate":
        # Handled separately in the agent loop
        return ""

    return f"Tool not implemented: {name}"


# ═══════════════════════════════════════════════════════════════════════════════
# Component 4: Context Reduction & Output Management
# ═══════════════════════════════════════════════════════════════════════════════

def clip(text: str, max_chars: int = 4000) -> str:
    """Truncate long text, keeping first and last portions."""
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half] + "\n[…clipped…]\n" + text[-half:]


def compress_history(history: list[dict[str, str]], max_entries: int = 20) -> list[dict[str, str]]:
    """Compress history when it exceeds max_entries.

    Keep the most recent 10 entries verbatim and summarize the rest as bullet points.
    Also deduplicates consecutive reads of the same file.
    """
    # Deduplicate consecutive reads of the same file
    deduped: list[dict[str, str]] = []
    for entry in history:
        if (
            deduped
            and entry["role"] == "tool_result"
            and deduped[-1]["role"] == "tool_result"
            and entry.get("tool") == "read_file"
            and deduped[-1].get("tool") == "read_file"
            and entry.get("path") == deduped[-1].get("path")
        ):
            deduped[-1] = entry  # keep only the latest
        else:
            deduped.append(entry)

    if len(deduped) <= max_entries:
        return deduped

    keep = 10
    old = deduped[:-keep]
    recent = deduped[-keep:]

    summary_lines = []
    for entry in old:
        role = entry["role"]
        content = entry["content"][:120]
        summary_lines.append(f"- [{role}] {content}")

    summary_entry: dict[str, str] = {
        "role": "summary",
        "content": "Earlier conversation (summarized):\n" + "\n".join(summary_lines),
    }
    return [summary_entry] + recent


# ═══════════════════════════════════════════════════════════════════════════════
# Component 5: Transcripts, Memory & Session Resumption
# ═══════════════════════════════════════════════════════════════════════════════

class SessionStore:
    """Persist sessions as JSON files for resumption."""

    def __init__(self, workspace_root: str) -> None:
        self.base_dir = os.path.join(workspace_root, ".coding-assistant", "sessions")
        os.makedirs(self.base_dir, exist_ok=True)

    @staticmethod
    def new_id() -> str:
        now = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        rand = secrets.token_hex(3)
        return f"{now}-{rand}"

    def _path(self, session_id: str) -> str:
        return os.path.join(self.base_dir, f"{session_id}.json")

    def save(
        self,
        session_id: str,
        workspace_root: str,
        history: list[dict[str, str]],
        memory_notes: list[str],
        model_name: str,
    ) -> None:
        data = {
            "id": session_id,
            "started_at": session_id.rsplit("-", 1)[0],
            "workspace_root": workspace_root,
            "history": history,
            "memory_notes": memory_notes,
            "model_name": model_name,
        }
        with open(self._path(session_id), "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)

    def load(self, session_id: str) -> dict[str, Any]:
        with open(self._path(session_id), encoding="utf-8") as fh:
            return json.load(fh)

    def latest(self) -> str | None:
        files = sorted(Path(self.base_dir).glob("*.json"))
        return files[-1].stem if files else None

    @staticmethod
    def memory_text(memory_notes: list[str]) -> str:
        if not memory_notes:
            return ""
        return "\n".join(f"- {n}" for n in memory_notes)


# ═══════════════════════════════════════════════════════════════════════════════
# Ollama Integration
# ═══════════════════════════════════════════════════════════════════════════════

def call_ollama(
    prompt: str,
    model: str,
    host: str,
    timeout: int,
    temperature: float,
    max_tokens: int,
) -> str:
    """Send a prompt to the Ollama /api/generate endpoint."""
    url = f"{host}/api/generate"
    body = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
            "top_p": 0.9,
        },
    }).encode()

    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})

    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
            return data.get("response", "")
        except (urllib.error.URLError, OSError) as exc:
            if attempt == 0:
                print(f"[ollama] Connection error ({exc}), retrying in 2s…")
                time.sleep(2)
            else:
                return f"<answer>Error: could not reach Ollama at {host} — {exc}</answer>"


# ═══════════════════════════════════════════════════════════════════════════════
# Component 6: Delegation & Bounded Subagents
# ═══════════════════════════════════════════════════════════════════════════════

def run_subagent(
    task: str,
    workspace: WorkspaceContext,
    model: str,
    host: str,
    timeout: int,
    temperature: float,
    max_tokens: int,
    approval_mode: str,
    memory_notes: list[str],
) -> str:
    """Run a bounded sub-agent with its own step counter (max 4 steps)."""
    # Build a fresh prefix for the sub-agent (no delegate tool)
    sub_tools = [t for t in TOOL_DEFS if t["name"] != "delegate"]
    prefix = build_prefix(workspace, sub_tools)
    history: list[dict[str, str]] = []
    max_steps = 4
    tried: list[str] = []

    for step in range(max_steps):
        prompt = build_turn(prefix, "", history, task if step == 0 else "Continue with the task.")
        raw = call_ollama(prompt, model, host, timeout, temperature, max_tokens)
        kind, payload = parse_model_output(raw)

        if kind == "answer":
            return str(payload)

        if kind == "tool":
            assert isinstance(payload, dict)
            name = payload["name"]
            args = payload.get("args", {})

            if name == "delegate":
                history.append({"role": "assistant", "content": raw})
                history.append({"role": "tool_result", "content": "Error: sub-agents cannot delegate."})
                tried.append(f"Step {step + 1}: tried to delegate (blocked)")
                continue

            ok, msg = validate_tool(name, args, workspace.root)
            if not ok:
                history.append({"role": "assistant", "content": raw})
                history.append({"role": "tool_result", "content": f"Validation error: {msg}"})
                tried.append(f"Step {step + 1}: {name} — validation error: {msg}")
                continue

            if not approve_tool(name, args, approval_mode):
                history.append({"role": "assistant", "content": raw})
                history.append({"role": "tool_result", "content": "Tool call denied by user."})
                tried.append(f"Step {step + 1}: {name} — denied")
                continue

            result = clip(run_tool(name, args, workspace.root, memory_notes))
            history.append({"role": "assistant", "content": raw})
            history.append({"role": "tool_result", "content": result, "tool": name, **({} if "path" not in args else {"path": args["path"]})})
            tried.append(f"Step {step + 1}: {name}({json.dumps(args)}) → {result[:80]}")
        else:
            history.append({"role": "assistant", "content": raw})
            tried.append(f"Step {step + 1}: (unparseable output)")

    return "Sub-agent hit max steps. Summary:\n" + "\n".join(tried)


# ═══════════════════════════════════════════════════════════════════════════════
# Main Agent Loop & REPL
# ═══════════════════════════════════════════════════════════════════════════════

def agent_loop(
    user_msg: str,
    workspace: WorkspaceContext,
    prefix: str,
    history: list[dict[str, str]],
    memory_notes: list[str],
    model: str,
    host: str,
    timeout: int,
    temperature: float,
    max_tokens: int,
    max_steps: int,
    approval_mode: str,
) -> str:
    """Run the agent loop for a single user request. Returns the final answer."""
    history.append({"role": "user", "content": user_msg})

    for step in range(max_steps):
        mem_text = SessionStore.memory_text(memory_notes)
        compressed = compress_history(history)
        prompt = build_turn(prefix, mem_text, compressed, user_msg)
        raw = call_ollama(prompt, model, host, timeout, temperature, max_tokens)

        kind, payload = parse_model_output(raw)

        if kind == "answer":
            answer_text = str(payload)
            history.append({"role": "assistant", "content": answer_text})
            return answer_text

        if kind == "tool":
            assert isinstance(payload, dict)
            name = payload["name"]
            args = payload.get("args", {})
            history.append({"role": "assistant", "content": raw})

            # Validate
            ok, msg = validate_tool(name, args, workspace.root)
            if not ok:
                err = f"Validation error: {msg}"
                print(f"  [{step + 1}/{max_steps}] {name} — {err}")
                history.append({"role": "tool_result", "content": err})
                continue

            # Approve
            if not approve_tool(name, args, approval_mode):
                history.append({"role": "tool_result", "content": "Tool call denied by user."})
                print(f"  [{step + 1}/{max_steps}] {name} — denied")
                continue

            # Delegate
            if name == "delegate":
                print(f"  [{step + 1}/{max_steps}] delegating: {args.get('task', '')[:60]}")
                result = run_subagent(
                    args["task"], workspace, model, host, timeout,
                    temperature, max_tokens, approval_mode, memory_notes,
                )
            else:
                print(f"  [{step + 1}/{max_steps}] {name}({', '.join(f'{k}={v!r}' for k, v in args.items())})")
                result = run_tool(name, args, workspace.root, memory_notes)

            result = clip(result)
            entry: dict[str, str] = {"role": "tool_result", "content": result, "tool": name}
            if "path" in args:
                entry["path"] = args["path"]
            history.append(entry)
        else:
            # Unparseable output — show it and let the model retry
            print(f"  [{step + 1}/{max_steps}] (model output not parseable, retrying)")
            history.append({"role": "assistant", "content": raw})
            history.append({"role": "tool_result", "content": "Error: respond with <tool> or <answer> tags."})

    return "(max steps reached — no final answer from model)"


def repl(args: argparse.Namespace) -> None:
    """Interactive REPL."""
    workspace = WorkspaceContext(args.cwd)
    store = SessionStore(workspace.root)

    # Resume or new session
    if args.resume:
        sid = args.resume if args.resume != "latest" else store.latest()
        if sid is None:
            print("No sessions found to resume.")
            sid = store.new_id()
            history: list[dict[str, str]] = []
            memory_notes: list[str] = []
        else:
            data = store.load(sid)
            history = data.get("history", [])
            memory_notes = data.get("memory_notes", [])
            print(f"Resumed session {sid} ({len(history)} history entries)")
    else:
        sid = store.new_id()
        history = []
        memory_notes = []

    prefix = build_prefix(workspace, TOOL_DEFS)
    print(f"Coding Assistant (model={args.model}, session={sid})")
    print("Type /help for commands.\n")

    while True:
        try:
            user_input = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_input:
            continue

        # Slash commands
        if user_input.startswith("/"):
            cmd = user_input.lower().split()[0]
            if cmd in ("/exit", "/quit"):
                break
            elif cmd == "/help":
                print("  /help    — show this help")
                print("  /memory  — show distilled memory")
                print("  /session — show session file path")
                print("  /reset   — clear history and memory")
                print("  /exit    — exit")
            elif cmd == "/memory":
                mem = store.memory_text(memory_notes)
                print(mem if mem else "(no memory notes)")
            elif cmd == "/session":
                print(store._path(sid))
            elif cmd == "/reset":
                history.clear()
                memory_notes.clear()
                print("History and memory cleared.")
            else:
                print(f"Unknown command: {cmd}")
            continue

        # Run agent loop
        answer = agent_loop(
            user_input, workspace, prefix, history, memory_notes,
            args.model, args.host, args.timeout, args.temperature,
            args.max_tokens, args.max_steps, args.approval,
        )
        print(f"\nassistant> {answer}\n")

        # Auto-save
        store.save(sid, workspace.root, history, memory_notes, args.model)


# ═══════════════════════════════════════════════════════════════════════════════
# CLI Entry Point
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Minimal coding assistant powered by Ollama",
    )
    parser.add_argument("--cwd", default=".", help="Workspace directory (default: .)")
    parser.add_argument("--model", default="qwen3.5:4b", help="Ollama model name (default: qwen3.5:4b)")
    parser.add_argument("--host", default="http://127.0.0.1:11434", help="Ollama server URL")
    parser.add_argument("--timeout", type=int, default=300, help="Ollama request timeout in seconds")
    parser.add_argument("--resume", nargs="?", const="latest", help="Resume session by ID or 'latest'")
    parser.add_argument("--approval", choices=["ask", "auto", "never"], default="ask", help="Tool approval mode")
    parser.add_argument("--max-steps", type=int, default=8, help="Max tool turns per user request")
    parser.add_argument("--max-tokens", type=int, default=512, help="Max model output tokens per step")
    parser.add_argument("--temperature", type=float, default=0.2, help="Sampling temperature")
    args = parser.parse_args()
    repl(args)


if __name__ == "__main__":
    main()
