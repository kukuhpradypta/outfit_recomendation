"""Thin wrapper around the google-genai SDK.

Keeps model IDs configurable (never hardcoded assumptions) and exposes two
capabilities: text generation (JSON reasoning) and image editing.
"""
import json
import logging
import time
from dataclasses import dataclass

from google import genai
from google.genai import types
from google.genai import errors as genai_errors

from app.config import settings

logger = logging.getLogger(__name__)

# Transient server statuses worth retrying. Kept short: a customer is waiting,
# and the recommendation service has a deterministic fallback.
_RETRYABLE_STATUS = {500, 503}
_MAX_RETRIES = 2
_BASE_DELAY_SEC = 1.0


@dataclass
class GeneratedImage:
    data: bytes
    mime_type: str


class GeminiClient:
    def __init__(self) -> None:
        if not settings.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY belum diset di environment.")
        self._client = genai.Client(
            api_key=settings.gemini_api_key,
            http_options=types.HttpOptions(timeout=settings.gemini_timeout_sec * 1000),
        )
        self.text_model = settings.gemini_text_model
        self.image_model = settings.gemini_image_model
        # Disabled automatically if the configured model rejects thinking_level.
        self._thinking_level = (settings.gemini_thinking_level or "").strip().upper() or None

    def _json_config(self, max_output_tokens: int) -> types.GenerateContentConfig:
        """JSON config with a capped output size and a low thinking level."""
        kwargs = dict(
            response_mime_type="application/json",
            temperature=0.4,
            max_output_tokens=max_output_tokens,
        )
        if self._thinking_level:
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=self._thinking_level)
        return types.GenerateContentConfig(**kwargs)

    # --------------------------------------------------------------- retry
    def _generate_with_retry(self, *, contents, config):
        """Call generate_content, retrying transient 500/503 errors."""
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                return self._client.models.generate_content(
                    model=self.text_model, contents=contents, config=config
                )
            except genai_errors.APIError as exc:
                status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
                # Model does not support thinking_level: drop it and retry once.
                if (
                    status == 400
                    and config.thinking_config is not None
                    and "think" in str(exc).lower()
                ):
                    logger.warning("Model menolak thinking_level, dinonaktifkan: %s", exc)
                    self._thinking_level = None
                    config = config.model_copy(update={"thinking_config": None})
                    continue
                if status in _RETRYABLE_STATUS and attempt < _MAX_RETRIES - 1:
                    delay = _BASE_DELAY_SEC * (2 ** attempt)
                    logger.warning(
                        "Gemini %s (percobaan %d/%d), retry dalam %.1fs",
                        status, attempt + 1, _MAX_RETRIES, delay,
                    )
                    time.sleep(delay)
                    last_exc = exc
                    continue
                raise
        if last_exc:
            raise last_exc
        raise RuntimeError("Gemini tidak merespons.")

    # ------------------------------------------------------------------ text
    def generate_json(self, prompt: str, max_output_tokens: int = 1024) -> dict:
        """Call the text model and parse a JSON object from the response."""
        response = self._generate_with_retry(
            contents=prompt, config=self._json_config(max_output_tokens),
        )
        text = (response.text or "").strip()
        if not text:
            raise ValueError("Model teks tidak mengembalikan konten.")
        return self._parse_json(text)

    def generate_json_with_image(
        self, *, prompt: str, image: bytes, image_mime: str, max_output_tokens: int = 1024
    ) -> dict:
        """Call the text model with an image (vision) and parse JSON output."""
        response = self._generate_with_retry(
            contents=[
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_bytes(data=image, mime_type=image_mime),
                        types.Part.from_text(text=prompt),
                    ],
                )
            ],
            config=self._json_config(max_output_tokens),
        )
        text = (response.text or "").strip()
        if not text:
            raise ValueError("Model vision tidak mengembalikan konten.")
        return self._parse_json(text)

    @staticmethod
    def _parse_json(text: str) -> dict:
        # Strip markdown fences if present.
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
            text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                return json.loads(text[start : end + 1])
            raise

    # ----------------------------------------------------------------- image
    def edit_image(
        self,
        *,
        prompt: str,
        base_image: bytes,
        base_image_mime: str,
        reference_images: list[tuple[bytes, str]] | None = None,
    ) -> GeneratedImage:
        """Edit a base image using the image model.

        `reference_images` are additional product images the model can use to
        dress the subject. Returns the first image part found in the response.
        """
        parts: list[types.Part] = [
            types.Part.from_bytes(data=base_image, mime_type=base_image_mime)
        ]
        for data, mime in reference_images or []:
            parts.append(types.Part.from_bytes(data=data, mime_type=mime))
        parts.append(types.Part.from_text(text=prompt))

        response = self._client.models.generate_content(
            model=self.image_model,
            contents=[types.Content(role="user", parts=parts)],
        )

        candidates = getattr(response, "candidates", None) or []
        for candidate in candidates:
            content = getattr(candidate, "content", None)
            if not content:
                continue
            for part in getattr(content, "parts", None) or []:
                inline = getattr(part, "inline_data", None)
                if inline and getattr(inline, "data", None):
                    return GeneratedImage(
                        data=inline.data,
                        mime_type=getattr(inline, "mime_type", "image/png") or "image/png",
                    )
        raise ValueError("Model gambar tidak mengembalikan data gambar.")


_client_instance: GeminiClient | None = None


def get_gemini_client() -> GeminiClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = GeminiClient()
    return _client_instance
