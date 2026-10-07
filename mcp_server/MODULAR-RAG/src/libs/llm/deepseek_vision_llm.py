"""DeepSeek Vision provider using OpenAI-compatible image messages."""

from __future__ import annotations

import os
from typing import Any, Optional

from src.libs.llm.base_llm import ChatResponse, Message
from src.libs.llm.base_vision_llm import ImageInput
from src.libs.llm.openai_vision_llm import OpenAIVisionLLM, OpenAIVisionLLMError


class DeepSeekVisionLLMError(OpenAIVisionLLMError):
    """Raised when a DeepSeek Vision request fails or returns no final answer."""


class DeepSeekVisionLLM(OpenAIVisionLLM):
    """Caption images with DeepSeek, independently of text LLM credentials."""

    DEFAULT_BASE_URL = "https://api.deepseek.com"
    DEFAULT_TIMEOUT = 180.0
    DEFAULT_MAX_TOKENS = 16384
    PROVIDER_NAME = "DeepSeek Vision"
    ERROR_CLASS = DeepSeekVisionLLMError

    def __init__(
        self,
        settings: Any,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        max_image_size: Optional[int] = None,
        timeout: Optional[float] = None,
        **kwargs: Any,
    ) -> None:
        vision = getattr(settings, "vision_llm", None)
        model = getattr(vision, "model", None)
        if not isinstance(model, str) or not model.strip():
            raise ValueError("DeepSeek Vision requires vision_llm.model")

        key = api_key or getattr(vision, "api_key", None) or os.environ.get("DEEPSEEK_API_KEY")
        if not key:
            raise ValueError(
                "DeepSeek Vision API key not provided. Set DEEPSEEK_API_KEY, "
                "vision_llm.api_key, or pass api_key."
            )
        url = base_url or getattr(vision, "base_url", None) or self.DEFAULT_BASE_URL
        super().__init__(
            settings=settings,
            api_key=key,
            base_url=url,
            max_image_size=max_image_size,
            timeout=timeout,
            **kwargs,
        )
        # The shared provider supports Azure fallbacks. DeepSeek must use only
        # its vision model and Bearer auth, without Azure query parameters.
        self.model = model
        self.api_version = None
        self._use_azure_auth = False
        tokens = getattr(vision, "max_tokens", None)
        self.default_max_tokens = tokens if tokens is not None else self.DEFAULT_MAX_TOKENS
        thinking = getattr(vision, "thinking", None)
        self.thinking = thinking if thinking is not None else True

    def _build_request_payload(
        self, messages: list[dict], temperature: float, max_tokens: int,
    ) -> dict:
        payload = super()._build_request_payload(messages, temperature, max_tokens)
        payload["thinking"] = {"type": "enabled" if self.thinking else "disabled"}
        return payload

    def chat_with_image(
        self,
        text: str,
        image: ImageInput,
        messages: Optional[list[Message]] = None,
        trace: Optional[Any] = None,
        **kwargs: Any,
    ) -> ChatResponse:
        response = super().chat_with_image(text, image, messages, trace, **kwargs)
        if not isinstance(response.content, str) or not response.content.strip():
            raise DeepSeekVisionLLMError(
                "[DeepSeek Vision] Empty final content; reasoning_content cannot be used "
                "as an image caption. Check the response token budget."
            )
        return response
