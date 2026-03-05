"""Modular LLM client interface."""

import os
from abc import ABC, abstractmethod

import httpx


class LLMClient(ABC):
    @abstractmethod
    def generate(self, prompt: str) -> str:
        """Send a prompt and return the LLM's text response."""


class AnthropicClient(LLMClient):
    def __init__(self, api_key: str, model: str = "claude-sonnet-4-20250514"):
        self.api_key = api_key
        self.model = model
        self.base_url = os.environ.get(
            "ANTHROPIC_BASE_URL", "https://api.anthropic.com"
        )

    def generate(self, prompt: str) -> str:
        resp = httpx.post(
            f"{self.base_url}/v1/messages",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": self.model,
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=120.0,
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"]


class OpenAIClient(LLMClient):
    def __init__(self, api_key: str, model: str = "gpt-4o"):
        self.api_key = api_key
        self.model = model
        self.base_url = os.environ.get(
            "OPENAI_BASE_URL", "https://api.openai.com"
        )

    def generate(self, prompt: str) -> str:
        resp = httpx.post(
            f"{self.base_url}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 4096,
            },
            timeout=120.0,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


class MockClient(LLMClient):
    """Mock client for testing without an API key."""

    def generate(self, prompt: str) -> str:
        return (
            "```python\n"
            "print('Hello from the UAS sandbox!')\n"
            "```\n"
        )


def get_llm_client() -> LLMClient:
    """Factory: select client based on environment variables."""
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if anthropic_key:
        model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
        print(f"Using Anthropic API (model: {model})")
        return AnthropicClient(anthropic_key, model)

    openai_key = os.environ.get("OPENAI_API_KEY")
    if openai_key:
        model = os.environ.get("OPENAI_MODEL", "gpt-4o")
        print(f"Using OpenAI API (model: {model})")
        return OpenAIClient(openai_key, model)

    print("No API key found. Using mock LLM client.")
    return MockClient()
