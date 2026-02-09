"""Quick test script to call the agent directly without Telegram."""

import asyncio
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from agent import Agent
from notion_service import NotionTaskCreator

notion = NotionTaskCreator(api_key=os.environ["NOTION_API_KEY"])
agent = Agent(api_key=os.environ["ANTHROPIC_API_KEY"], notion=notion)


async def main():
    query = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "UIKit 관련 태스크 중에 본문이 있는 거 있어?"
    print(f">>> {query}\n")

    messages = [{"role": "user", "content": query}]
    result = await agent.run(messages, mode="chat")

    if result.text:
        print(result.text)
    if result.confirmation_request:
        print(f"\n[confirmation_request]: {result.confirmation_request}")


if __name__ == "__main__":
    asyncio.run(main())
