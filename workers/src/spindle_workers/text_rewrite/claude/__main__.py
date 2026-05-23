"""Boot the Claude text-rewrite worker.

Invoked by the runtime supervisor as:
    module: spindle_workers.text_rewrite.claude
"""
import asyncio

from .worker import ClaudeTextRewriteWorker


def main() -> None:
    asyncio.run(ClaudeTextRewriteWorker.boot())


if __name__ == "__main__":
    main()
