"""Unit tests for coding_assistant.py tool parsing and validation."""

import os
import sys
import tempfile
import unittest

# Ensure the repo root is on sys.path so we can import the single-file module.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from coding_assistant import (
    WorkspaceContext,
    clip,
    parse_model_output,
    validate_tool,
)


class TestParseModelOutput(unittest.TestCase):
    """Tests for parse_model_output."""

    def test_extracts_tool_call(self) -> None:
        text = 'Some thinking.\n<tool>{"name": "read_file", "args": {"path": "main.py"}}</tool>'
        kind, payload = parse_model_output(text)
        self.assertEqual(kind, "tool")
        assert isinstance(payload, dict)
        self.assertEqual(payload["name"], "read_file")
        self.assertEqual(payload["args"]["path"], "main.py")

    def test_extracts_tool_call_without_args(self) -> None:
        text = '<tool>{"name": "list_files", "args": {"path": "."}}</tool>'
        kind, payload = parse_model_output(text)
        self.assertEqual(kind, "tool")
        assert isinstance(payload, dict)
        self.assertEqual(payload["name"], "list_files")

    def test_extracts_answer(self) -> None:
        text = "<answer>The tests pass now. I fixed the import on line 12.</answer>"
        kind, payload = parse_model_output(text)
        self.assertEqual(kind, "answer")
        self.assertEqual(payload, "The tests pass now. I fixed the import on line 12.")

    def test_extracts_multiline_answer(self) -> None:
        text = "<answer>Line one.\nLine two.\nLine three.</answer>"
        kind, payload = parse_model_output(text)
        self.assertEqual(kind, "answer")
        assert isinstance(payload, str)
        self.assertIn("Line two.", payload)

    def test_malformed_output_no_tags(self) -> None:
        text = "I think we should read the file first."
        kind, payload = parse_model_output(text)
        self.assertEqual(kind, "error")

    def test_malformed_tool_json(self) -> None:
        text = "<tool>not valid json</tool>"
        kind, payload = parse_model_output(text)
        self.assertEqual(kind, "error")

    def test_tool_missing_name(self) -> None:
        text = '<tool>{"args": {"path": "."}}</tool>'
        kind, payload = parse_model_output(text)
        self.assertEqual(kind, "error")


class TestValidateTool(unittest.TestCase):
    """Tests for validate_tool."""

    def test_rejects_unknown_tool(self) -> None:
        ok, msg = validate_tool("nonexistent_tool", {})
        self.assertFalse(ok)
        self.assertIn("Unknown tool", msg)

    def test_rejects_missing_required_arg(self) -> None:
        ok, msg = validate_tool("read_file", {})
        self.assertFalse(ok)
        self.assertIn("Missing required argument", msg)

    def test_accepts_valid_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a target file so the path resolves inside the workspace
            target = os.path.join(tmpdir, "test.py")
            with open(target, "w") as f:
                f.write("")
            ok, msg = validate_tool("read_file", {"path": "test.py"}, tmpdir)
            self.assertTrue(ok)
            self.assertEqual(msg, "ok")

    def test_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ok, msg = validate_tool("read_file", {"path": "../../etc/passwd"}, tmpdir)
            self.assertFalse(ok)
            self.assertIn("Path escapes workspace", msg)

    def test_rejects_absolute_path_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ok, msg = validate_tool("read_file", {"path": "/etc/passwd"}, tmpdir)
            self.assertFalse(ok)
            self.assertIn("Path escapes workspace", msg)


class TestClip(unittest.TestCase):
    """Tests for clip."""

    def test_short_text_unchanged(self) -> None:
        text = "Hello, world!"
        self.assertEqual(clip(text), text)

    def test_long_text_truncated(self) -> None:
        text = "A" * 10000
        result = clip(text, max_chars=100)
        self.assertIn("[…clipped…]", result)
        self.assertLess(len(result), 10000)

    def test_exact_limit_unchanged(self) -> None:
        text = "B" * 4000
        self.assertEqual(clip(text, max_chars=4000), text)

    def test_clipped_has_start_and_end(self) -> None:
        text = "START" + "x" * 10000 + "END"
        result = clip(text, max_chars=200)
        self.assertTrue(result.startswith("START"))
        self.assertTrue(result.endswith("END"))


class TestWorkspaceContext(unittest.TestCase):
    """Smoke test for WorkspaceContext."""

    def test_snapshot_returns_string(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ctx = WorkspaceContext(tmpdir)
            snap = ctx.snapshot()
            self.assertIsInstance(snap, str)
            self.assertIn("Workspace root:", snap)

    def test_snapshot_includes_file_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a test file
            with open(os.path.join(tmpdir, "hello.py"), "w") as f:
                f.write("print('hello')")
            ctx = WorkspaceContext(tmpdir)
            snap = ctx.snapshot()
            self.assertIn("hello.py", snap)


if __name__ == "__main__":
    unittest.main()
