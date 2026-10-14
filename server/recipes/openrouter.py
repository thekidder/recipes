"""OpenRouter Chat Completions adapter using the shared durable call lifecycle."""
from .document import ExtractedRecipe, RecipeError
from .openai import OpenAIExtractor, PROMPT


class OpenRouterExtractor(OpenAIExtractor):
    provider = "openrouter"
    endpoint = "https://openrouter.ai/api/v1/chat/completions"
    adapter_version = "openrouter-chat-httpx-v1"

    @staticmethod
    def provider_error(payload):
        if payload.get("error"):
            return payload["error"]
        choices = payload.get("choices")
        if isinstance(choices, list):
            return next((choice["error"] for choice in choices if isinstance(choice, dict) and choice.get("error")), None)

    def build_request(self, evidence):
        content = []
        for part in evidence:
            if part["type"] == "input_text":
                content.append({"type": "text", "text": part["text"]})
            elif part["type"] == "input_image":
                content.append({"type": "image_url", "image_url": {
                    "url": part["image_url"], "detail": part.get("detail", "auto")}})
            else:
                raise RecipeError("invalid_source", "Unsupported extraction evidence type.")
        return {"model": self.config.model, "stream": False,
                "messages": [{"role": "system", "content": PROMPT}, {"role": "user", "content": content}],
                "max_tokens": self.config.max_output_tokens,
                "provider": {"require_parameters": True},
                "response_format": {"type": "json_schema", "json_schema": {
                    "name": "recipe", "strict": True, "schema": ExtractedRecipe.model_json_schema()}}}

    def response_diagnostics(self, payload):
        details = super().response_diagnostics(payload)
        if not isinstance(payload, dict):
            return details
        details["upstream_provider"] = self.diagnostic_label(payload.get("provider"))
        error = self.provider_error(payload)
        if isinstance(error, dict):
            code = error.get("code")
            details["provider_error_code"] = code if type(code) is int else self.diagnostic_label(code)
            metadata = error.get("metadata")
            if isinstance(metadata, dict):
                for key in ("error_type", "provider_code"):
                    details[f"upstream_{key}"] = self.diagnostic_label(metadata.get(key))
        choices = payload.get("choices")
        if isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], dict):
            for field in ("finish_reason", "native_finish_reason"):
                details[field] = self.diagnostic_label(choices[0].get(field))
            reason = details.get("finish_reason")
            details["provider_status"] = "completed" if reason == "stop" else "incomplete"
            if reason and reason != "stop":
                details["incomplete_reason"] = "max_output_tokens" if reason == "length" else reason
        usage = payload.get("usage")
        if isinstance(usage, dict):
            counters = details.setdefault("usage", {})
            for source, target in (("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens")):
                if type(usage.get(source)) is int and usage[source] >= 0:
                    counters[target] = usage[source]
            for group, field in (("prompt_tokens_details", "cached_tokens"), ("completion_tokens_details", "reasoning_tokens")):
                values = usage.get(group)
                if isinstance(values, dict) and type(values.get(field)) is int and values[field] >= 0:
                    counters[field] = values[field]
        if error:
            details["provider_status"] = "failed"
        return {key: value for key, value in details.items() if value is not None}

    def output_text(self, payload):
        # OpenRouter can return a provider error in an HTTP 200 body.
        error = self.provider_error(payload)
        if error:
            code = error.get("code") if isinstance(error, dict) else None
            retryable = str(code) in {"408", "409", "429", "500", "502", "503", "504"}
            raise RecipeError("provider_error", "OpenRouter returned a provider error. Inspect the saved provider response.",
                              retryable=retryable)
        choices = payload.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ValueError("Expected a single completion")
        choice = choices[0]
        message = choice.get("message")
        if isinstance(message, dict) and message.get("refusal"):
            raise RecipeError("provider_refusal", "OpenRouter's model declined to process this source. Review the source or saved response.")
        reason = choice.get("finish_reason")
        if reason != "stop":
            description = {"content_filter": "OpenRouter's model stopped extraction because of its content filter.",
                           "length": "OpenRouter's model reached the output token limit before completing extraction."}.get(
                               reason, "OpenRouter did not return a complete extraction.")
            raise RecipeError("incomplete_response", description + " No recipe was accepted.", retryable=True)
        if not isinstance(message, dict) or message.get("tool_calls") or not isinstance(message.get("content"), str):
            raise ValueError("Expected recipe text")
        return message["content"]
