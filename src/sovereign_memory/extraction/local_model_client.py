"""Small OpenAI-compatible HTTP client for local extraction bridges."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


DEFAULT_EXTRACTOR_URL = "http://127.0.0.1:11437/v1/chat/completions"
DEFAULT_EXTRACTOR_MODEL = "apple-foundation-models"


class LocalModelClientError(RuntimeError):
    """Raised when the local extraction bridge cannot satisfy a request."""


class LocalModelClient:
    """OpenAI-compatible chat-completions client using only the Python stdlib."""

    def __init__(
        self,
        url: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.url = url or os.environ.get("SOVEREIGN_EXTRACTOR_URL", DEFAULT_EXTRACTOR_URL)
        self.model = model or os.environ.get("SOVEREIGN_EXTRACTOR_MODEL", DEFAULT_EXTRACTOR_MODEL)
        self.timeout = timeout or float(os.environ.get("SOVEREIGN_EXTRACTOR_TIMEOUT", "120"))

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.1,
        max_tokens: int = 4096,
    ) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise LocalModelClientError(f"local extractor returned HTTP {exc.code}: {detail[:500]}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise LocalModelClientError(f"local extractor request failed: {exc}") from exc

        return self._message_content(data)

    @staticmethod
    def _message_content(data: dict[str, Any]) -> str:
        try:
            return str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise LocalModelClientError("local extractor response did not include choices[0].message.content") from exc
