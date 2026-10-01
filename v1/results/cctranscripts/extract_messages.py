#!/usr/bin/env python3
"""
Extract Claude Code text/thinking messages from JSONL transcript files.

Usage:
    python extract_messages.py <jsonl_file> [options]

Options:
    --with-thinking     Also include thinking blocks (often empty/redacted)
    --text-only         Show only the raw message text (no headers)
    --json              Output as JSON array
    --filter <keyword>  Only show messages containing <keyword>
    --role <role>       Filter by role: assistant (default), user, or all
"""

import json
import sys
import argparse
import os
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


def extract_messages(lines, with_thinking=False, role_filter="assistant"):
    """
    Walk through transcript lines and pull out text and (optionally) thinking blocks.

    Returns a list of dicts:
      {
        "index": int,           # sequential message number (1-based)
        "role": str,            # "user" or "assistant"
        "type": str,            # "text" or "thinking"
        "content": str,         # the message text
        "timestamp": str|None,
      }
    """
    messages = []
    for entry in lines:
        msg = entry.get("message", {})
        role = msg.get("role")
        if not role:
            continue

        # Apply role filter
        if role_filter != "all" and role != role_filter:
            continue

        content = msg.get("content", [])
        timestamp = entry.get("timestamp")

        # User messages can be a plain string
        if isinstance(content, str):
            if role_filter == "all" or role == role_filter:
                messages.append({
                    "role": role,
                    "type": "text",
                    "content": content,
                    "timestamp": timestamp,
                })
            continue

        if not isinstance(content, list):
            continue

        for block in content:
            if not isinstance(block, dict):
                continue

            block_type = block.get("type")

            if block_type == "text":
                text = block.get("text", "")
                if text:
                    messages.append({
                        "role": role,
                        "type": "text",
                        "content": text,
                        "timestamp": timestamp,
                    })

            elif block_type == "thinking" and with_thinking:
                text = block.get("thinking", "")
                if text:
                    messages.append({
                        "role": role,
                        "type": "thinking",
                        "content": text,
                        "timestamp": timestamp,
                    })

    # Add 1-based index
    for i, m in enumerate(messages, 1):
        m["index"] = i

    return messages


def print_messages(messages, text_only=False, as_json=False, keyword=None):
    if keyword:
        kw = keyword.lower()
        messages = [m for m in messages if kw in (m["content"] or "").lower()]

    if as_json:
        print(json.dumps(messages, indent=2, ensure_ascii=False))
        return

    if not messages:
        print("No messages found.")
        return

    total = len(messages)
    print(f"Found {total} message(s):\n")

    for m in messages:
        if text_only:
            print(m["content"])
            print()
            continue

        header = f"── Message #{m['index']} "
        header += f"| {m['role']} "
        if m["type"] == "thinking":
            header += "| 💭 thinking "
        if m["timestamp"]:
            header += f"| {m['timestamp']} "
        print(header)
        print(f"{'─' * min(len(header), 80)}")

        for line in m["content"].splitlines():
            print(f"  {line}")

        print()


def list_available_files(directory):
    """List all .jsonl files in the directory."""
    files = sorted(f for f in os.listdir(directory) if f.endswith(".jsonl"))
    return files


def main():
    parser = argparse.ArgumentParser(
        description="Extract text/thinking messages from Claude Code JSONL transcripts."
    )
    parser.add_argument(
        "file",
        nargs="?",
        help="Path to a .jsonl transcript file. If omitted, lists available files.",
    )
    parser.add_argument(
        "--with-thinking", action="store_true",
        help="Also include thinking blocks (often empty/redacted).",
    )
    parser.add_argument(
        "--text-only", action="store_true",
        help="Print only the raw message text, nothing else.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output results as a JSON array.",
    )
    parser.add_argument(
        "--filter", type=str, default=None,
        help="Only show messages containing this keyword.",
    )
    parser.add_argument(
        "--role", type=str, default="assistant",
        choices=["assistant", "user", "all"],
        help="Which role to include (default: assistant).",
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
    messages = extract_messages(lines, with_thinking=args.with_thinking, role_filter=args.role)
    print_messages(
        messages,
        text_only=args.text_only,
        as_json=args.json,
        keyword=args.filter,
    )


if __name__ == "__main__":
    main()
