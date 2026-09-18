"""Minimal Claude Agent SDK run: Claude Code's harness + built-in tools as a library.

Needs the `claude` CLI on PATH (installed by fedora-deploy). Run: uv run agent.py
Docs: https://code.claude.com/docs/en/agent-sdk
"""
import asyncio

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, query


async def main() -> None:
    options = ClaudeAgentOptions(
        allowed_tools=["Read", "Glob", "Grep"],  # read-only: safe to try anywhere
        max_turns=10,
    )
    async for message in query(prompt="Summarize what this folder contains.", options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(block.text)
        elif isinstance(message, ResultMessage):
            print(f"\n[done: {message.num_turns} turns, ${message.total_cost_usd or 0:.4f}]")


asyncio.run(main())
