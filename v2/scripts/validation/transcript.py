"""Reading a run's Claude Code transcript (r<NNN>.jsonl) and stream output (stream.ndjson).

Shared by validate_runs.py and analyze_runs.py.
"""

import glob
import json
import os
import re


def load_jsonl(path):
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    return events


def run_files(run_dir):
    name = os.path.basename(run_dir)
    transcripts = glob.glob(os.path.join(run_dir, "r[0-9][0-9][0-9].jsonl"))
    return {"meta": os.path.join(run_dir, "meta.json"), "stream": os.path.join(run_dir, "stream.ndjson"),
            "transcript": transcripts[0] if transcripts else os.path.join(run_dir, f"r{name[-3:]}.jsonl")}


HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n\s*\2\b", re.S)


class ToolCall:
    def __init__(self, ts, name, input_, use_id):
        self.ts, self.name, self.input, self.id = ts, name, input_ or {}, use_id
        self.result = ""        # tool_result text, filled in afterwards
        self.is_error = False

    def shell_segments(self):
        """Top-level shell commands of a Bash call, heredoc bodies removed."""
        if self.name != "Bash":
            return []
        cmd = HEREDOC.sub("<<HEREDOC", self.input.get("command", ""))
        return [s for s in re.split(r"\n|&&|\|\||;|\|", cmd) if s.strip()]

    def device_text(self):
        """Everything in this call that could end up as a device command: the Bash command
        (heredoc scripts included), a written file's content, or an edit's new text."""
        if self.name == "Bash":
            return self.input.get("command", "")
        if self.name == "Write":
            return self.input.get("content", "")
        if self.name == "Edit":
            return self.input.get("new_string", "")
        return ""

    def __repr__(self):
        return f"<{self.name} {self.ts}>"


def extract_tool_calls(transcript):
    """Tool calls in order, each with its result text attached."""
    calls, by_id = [], {}
    for e in transcript:
        msg = e.get("message")
        if not isinstance(msg, dict) or e.get("isSidechain"):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if e.get("type") == "assistant" and b.get("type") == "tool_use":
                c = ToolCall(e.get("timestamp"), b.get("name"), b.get("input"), b.get("id"))
                calls.append(c)
                by_id[c.id] = c
            elif e.get("type") == "user" and b.get("type") == "tool_result" and b.get("tool_use_id") in by_id:
                r = b.get("content")
                if isinstance(r, list):
                    r = "\n".join(x.get("text", "") for x in r if isinstance(x, dict))
                by_id[b["tool_use_id"]].result = r or ""
                by_id[b["tool_use_id"]].is_error = bool(b.get("is_error"))
    return calls


def assistant_texts(transcript):
    """(timestamp, text) of every assistant text block, in order."""
    out = []
    for e in transcript:
        if e.get("type") != "assistant" or e.get("isSidechain"):
            continue
        for b in (e.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip():
                out.append((e.get("timestamp"), b["text"]))
    return out


def final_report(stream):
    """The agent's report: the longest result text (a later wake-up only adds a short note)."""
    results = [e.get("result") or "" for e in stream if e.get("type") == "result"]
    return max(results, key=len) if results else ""
