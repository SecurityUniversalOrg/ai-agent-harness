from __future__ import annotations

import asyncio
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Any
import os

from . import sandbox


def _blocks_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _truncate_tool_results(msg: dict) -> dict:
    if msg.get("type") != "user":
        return msg
    inner = msg.get("message", {})
    content = inner.get("content")
    if not isinstance(content, list):
        return msg
    clipped = []
    for b in content:
        if isinstance(b, dict) and b.get("type") == "tool_result":
            c = b.get("content")
            if isinstance(c, str):
                b = {**b, "content": c[:5000]}
            elif isinstance(c, list):
                b = {**b, "content": [
                    ({**x, "text": x.get("text", "")[:5000]} if isinstance(x, dict) else x)
                    for x in c[:10]
                ]}
        clipped.append(b)
    return {**msg, "message": {**inner, "content": clipped}}


def _progress_line(msg: dict, prefix: str) -> None:
    if msg.get("type") != "assistant":
        return
    for b in msg.get("message", {}).get("content", []):
        if not isinstance(b, dict):
            continue
        if b.get("type") == "tool_use":
            inp = b.get("input") or {}
            arg = (inp.get("command") or inp.get("file_path") or inp.get("path")
                   or inp.get("pattern") or "")
            arg = str(arg).replace("\n", " ")[:120]
            print(f"{prefix}  -> {b.get('name')}: {arg}", file=sys.stderr, flush=True)
        elif b.get("type") == "text":
            t = (b.get("text") or "").strip().replace("\n", " ")
            if t:
                print(f"{prefix}    {t[:140]}", file=sys.stderr, flush=True)


@dataclass
class AgentResult:
    """Collected output of one agent run"""
    messages: list[dict] = field(default_factory=list)  # raw stream-json dicts
    result_message: dict | None = None                  # terminal {"type":"result",...}
    session_id: str | None = None                       # for resume on transient failure
    error: str | None = None                            # if the agent loop died
    resume_count = int = 0                              # how many times we auto-resumed

    def find_tagged_message(self, tag: str) -> str:
        """Return the most-recent assistant message text containing <tag>"""
        needle = f"<{tag}>"
        last_assistant = ""
        for msg in reversed(self.messages):
            if msg.get("type") != "assistant":
                continue
            text = _blocks_to_text(msg.get("message", {}).get("content"))
            if not last_assistant:
                last_assistant = text
            if needle in text:
                return text
        return last_assistant

    @property
    def last_assistant_message(self) -> str:
        for msg in reversed(self.messages):
            if msg.get("type") == "assistant":
                return _blocks_to_text(msg.get("message", {}).get("content"))
        return ""

    def transcript(self) -> list[dict]:
        """JSON-serializable transcript for persistence."""
        return [_truncate_tool_results(m) for m in self.messages]


# The core wrapper

DEFAULT_TOOLS = ["Read", "Write", "Bash"]


async def run_agent(
    prompt: str,
    *,
    container: str,
    max_turns: int,
    model: str,
    max_resume_attempts: int = 20,
    transcript_path: str | None = None,
    heartbeat_every: int = 25,
    progress_prefix: str | None = None,
    tools: list[str] | None = None,
    system_prompt: str | None = None,
    resume_session_id: str | None = None,
) -> AgentResult:
    """Run a Claude Code agent session via headless CLI inside ``container``"""
    # API Key / HTTPS_PROXY are on the container's env (set at docker_ops.run time)
    # CLAUDECODE="" stops the nested-session check
    # IS_SANDBOX=1 lets the CLI accept bypassPermissions
    cli_argv = ["docker", "exec", "-i",
                "-e", "CLAUDECODE=", 
                "-e", "IS_SANDBOX=1",
                "-e", f"ANTHROPIC_AWS_API_KEY={os.environ.get('ANTHROPIC_AWS_API_KEY', '')}",
                "-e", f"CLAUDE_CODE_USE_ANTHROPIC_AWS={os.environ.get('CLAUDE_CODE_USE_ANTHROPIC_AWS', '')}",
                "-e", f"ANTHROPIC_AWS_WORKSPACE_ID={os.environ.get('ANTHROPIC_AWS_WORKSPACE_ID', '')}",
                "-e", f"AWS_REGION={os.environ.get('AWS_REGION', '')}",
                "-w", "/work", "--",
                container, "claude"]
    result = AgentResult()
    attempt = 0
    assistant_count = 0
    tool_call_count = 0

    transcript_file = open(transcript_path, "w") if transcript_path else None
    try:
        while True:
            cmd = [
                *cli_argv, "-p", "--verbose",
                "--output-format", "stream-json",
                "--permission-mode", sandbox.permission_mode(),
                "--model", model,
                "--max-turns", str(max_turns),
                "--tools", ",".join(tools if tools is not None else DEFAULT_TOOLS) or '""',
                "--strict-mcp-config",
                "--setting-sources", "",
            ]
            if system_prompt:
                cmd += ["--system-prompt", system_prompt]
            if attempt > 0 and result.session_id:
                cmd += ["--resume", result.session_id, "continue"]
            elif resume_session_id:
                cmd += ["--resume", resume_session_id, prompt]
            else:
                cmd += [prompt]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=16 * 1024 * 1024,
            )
            assert proc.stdout

            try:
                async for raw in proc.stdout:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    result.messages.append(msg)
                    if progress_prefix:
                        _progress_line(msg, progress_prefix)
                    if transcript_file:
                        transcript_file.write(
                            json.dumps(_truncate_tool_results(msg)) + "\n"
                        )
                        transcript_file.flush()

                    mtype = msg.get("type")
                    if mtype == "assistant":
                        assistant_count += 1
                        tool_call_count += sum(
                            1 for b in msg.get("message", {}).get("content", [])
                            if isinstance(b, dict) and b.get("type") == "tool_use"
                        )
                        if assistant_count % heartbeat_every == 0:
                            printf(f"  [agent] {tool_call_count} tool calls "
                                   f"({assistant_count} msgs)")
                    elif mtype == "system" and msg.get("subtype") == "init":
                        sid = msg.get("session_id")
                        if sid and result.session_id is None:
                            result.session_id = sid
                    elif mtype == "result":
                        result.result_message = msg
                        # Agents with run_in_background bash tasks keep the CLI
                        # stream alive past the result message: each pending
                        # task_notification re-inits the session inline. Break
                        # on the FIRST result instead of waiting for stream
                        # exhaustion - otherwise a fuzzing agent with many 
                        # background tasks never terminates.
                        if msg.get("subtype") == "error_max_turns":
                            result.error = (
                                f"turn budget exhausted (--max-turns "
                                f"{max_turns}); raise the cap and re-run"
                            )
                            proc.terminate()
                            await proc.wait()
                            return result
                        if msg.get("is_error"):
                            raise RuntimeError(
                                f"CLI result {msg.get('subtype')}: "
                                f"{msg.get('errors') or msg.get('result')}"
                            )
                        proc.terminate()
                        await proc.wait()
                        return result

                # Stream ended without a result message - process died.
                rc = await proc.wait()
                stderr = b""
                if proc.stderr:
                    stderr = await proc.stderr.read()
                raise RuntimeError(
                    f"CLI exited rc={rc} without result: "
                    f"{stderr.decode(errors='replace')[:2000]}"
                )

            except Exception as e:
                if proc.returncode is None:
                    proc.terminate()
                    await proc.wait()
                # 429 rate-limit, upstream 5xx, or CLI crash all surface here.
                # The attempt cap bounds wasted retries on a genuine bug.
                attempt += 1
                if result.session_id is None or attempt > max_resume_attempts:
                    result.error = f"{type(e).__name__} after {attempt} attempt(s): {e}"
                    return result
                backoff = min(2 ** attempt, 300)
                print(
                    f"[agent] {type(e).__name__} on attempt {attempt}, "
                    f"resuming session {result.session_id} in {backoff}s: {e}",
                    file=sys.stderr,
                )
                result.resume_count = attempt
                await asyncio.sleep(backoff)
    finally:
        if transcript_file:
            transcript_file.close()
