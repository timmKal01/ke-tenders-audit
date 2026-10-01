"""A stand-in chat model that replays a fixed script. Used to test the graph
wiring, MCP calls, the human gate and logging without a real model."""

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class ScriptedLLM(BaseChatModel):
    script: list[Any]
    position: int = 0
    seen: list[Any] = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.seen.append(messages)
        step = self.script[self.position]
        self.position += 1
        message = step(messages) if callable(step) else step
        if isinstance(message, str):
            message = AIMessage(content=message)
        return ChatResult(generations=[ChatGeneration(message=message)])


def call(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])
