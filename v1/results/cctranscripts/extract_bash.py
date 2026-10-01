#!/usr/bin/env python3
"""
Extract Bash commands from Claude Code JSONL transcript files.

Usage:
    python extract_bash.py <jsonl_file> [options]

Options:
    --no-output         Hide command output/results (shown by default)
    --cmd-only          Show only the raw command text (no headers)
    --json              Output as JSON array
    --filter <keyword>  Only show commands whose command text or description contains <keyword>
"""

import json
import sys
import argparse
import os
import textwrap
import io

# Fix Windows console encoding
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


def load_lines(filepath):
    """Load all JSON lines from a JSONL file."""
    lines = []
    with open(filepath, "r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                lines.append(json.loads(raw))
            except json.JSONDecodeError:
                print(f"  [warning] skipping malformed JSON on line {lineno}", file=sys.stderr)
    return lines


def extract_bash_commands(lines):
    """
    Walk through transcript lines and pull out every Bash tool_use block,
    then match it to its tool_result (the output) if present.

    Returns a list of dicts:
      {
        "index": int,            # sequential command number (1-based)
        "tool_use_id": str,
        "command": str,
        "description": str|None,
        "timeout": int|None,
        "timestamp": str|None,
        "output": str|None,      # stdout from the tool result
        "stderr": str|None,
        "is_error": bool|None,
      }
    """
    # First pass: collect all Bash tool_use blocks
    commands = []
    for entry in lines:
        msg = entry.get("message", {})
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        timestamp = entry.get("timestamp")
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") == "Bash"
            ):
                inp = block.get("input", {})
                commands.append({
                    "tool_use_id": block.get("id"),
                    "command": inp.get("command", ""),
                    "description": inp.get("description"),
                    "timeout": inp.get("timeout"),
                    "timestamp": timestamp,
                    "output": None,
                    "stderr": None,
                    "is_error": None,
                })

    # Second pass: match tool_result blocks to their tool_use_id
    for entry in lines:
        msg = entry.get("message", {})
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tuid = block.get("tool_use_id")
                # Also check toolUseResult at the entry level (richer info)
                tool_result = entry.get("toolUseResult") or {}
                for cmd in commands:
                    if cmd["tool_use_id"] == tuid:
                        # The content field in tool_result block is the main output
                        result_content = block.get("content", "")
                        if isinstance(result_content, list):
                            # Sometimes content is a list of text blocks
                            result_content = "\n".join(
                                b.get("text", "") for b in result_content if isinstance(b, dict)
                            )
                        stdout = tool_result.get("stdout") if isinstance(tool_result, dict) else None
                        cmd["output"] = stdout or result_content
                        cmd["stderr"] = tool_result.get("stderr") if isinstance(tool_result, dict) else None
                        cmd["is_error"] = block.get("is_error", False)
                        break

    # Add 1-based index
    for i, cmd in enumerate(commands, 1):
        cmd["index"] = i

    return commands


def print_commands(commands, with_output=False, cmd_only=False, as_json=False, keyword=None):
    if keyword:
        kw = keyword.lower()
        commands = [
            c for c in commands
            if kw in (c["command"] or "").lower()
            or kw in (c["description"] or "").lower()
        ]

    if as_json:
        print(json.dumps(commands, indent=2, ensure_ascii=False))
        return

    if not commands:
        print("No Bash commands found.")
        return

    total = len(commands)
    print(f"Found {total} Bash command(s):\n")

    for cmd in commands:
        if cmd_only:
            print(cmd["command"])
            print()
            continue

        header = f"── Command #{cmd['index']} "
        if cmd["description"]:
            header += f"| {cmd['description']} "
        if cmd["timestamp"]:
            header += f"| {cmd['timestamp']} "
        print(header)
        print(f"{'─' * min(len(header), 80)}")

        # Print the command, indented
        for line in cmd["command"].splitlines():
            print(f"  $ {line}")

        if cmd["is_error"]:
            print("  ⚠ Command returned an error")

        if with_output and cmd["output"]:
            print()
            print("  ┌─ Output:")
            for line in cmd["output"].splitlines():
                print(f"  │ {line}")
            print("  └─")
            if cmd["stderr"]:
                print("  ┌─ Stderr:")
                for line in cmd["stderr"].splitlines():
                    print(f"  │ {line}")
                print("  └─")

        print()


def list_available_files(directory):
    """List all .jsonl files in the directory."""
    files = sorted(f for f in os.listdir(directory) if f.endswith(".jsonl"))
    return files


def main():
    parser = argparse.ArgumentParser(
        description="Extract Bash commands from Claude Code JSONL transcripts."
    )
    parser.add_argument(
        "file",
        nargs="?",
        help="Path to a .jsonl transcript file. If omitted, lists available files.",
    )
    parser.add_argument(
        "--no-output", action="store_true",
        help="Hide command output/results (shown by default).",
    )
    parser.add_argument(
        "--cmd-only", action="store_true",
        help="Print only the raw command text, nothing else.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output results as a JSON array.",
    )
    parser.add_argument(
        "--filter", type=str, default=None,
        help="Only show commands containing this keyword (in command or description).",
    )
    parser.add_argument(
        "--dir", type=str, default=None,
        help="Directory containing .jsonl files (for interactive selection).",
    )

    args = parser.parse_args()

    # If no file given, offer interactive selection
    if not args.file:
        search_dir = args.dir or os.path.dirname(os.path.abspath(__file__))
        files = list_available_files(search_dir)
        if not files:
            print(f"No .jsonl files found in {search_dir}")
            sys.exit(1)

        print(f"Available transcripts in {search_dir}:\n")
        for i, f in enumerate(files, 1):
            print(f"  [{i:>2}] {f}")
        print()

        try:
            choice = input("Select a file number (or 'q' to quit): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)

        if choice.lower() == "q":
            sys.exit(0)

        try:
            idx = int(choice) - 1
            if idx < 0 or idx >= len(files):
                raise ValueError
        except ValueError:
            print("Invalid selection.")
            sys.exit(1)

        filepath = os.path.join(search_dir, files[idx])
    else:
        filepath = args.file

    if not os.path.isfile(filepath):
        print(f"Error: file not found: {filepath}", file=sys.stderr)
        sys.exit(1)

    print(f"Loading: {filepath}\n")
    lines = load_lines(filepath)
    commands = extract_bash_commands(lines)
    print_commands(
        commands,
        with_output=not args.no_output,
        cmd_only=args.cmd_only,
        as_json=args.json,
        keyword=args.filter,
    )


if __name__ == "__main__":
    main()
