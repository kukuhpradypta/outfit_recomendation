"""Thin wrapper around the google-genai SDK.

Keeps model IDs configurable (never hardcoded assumptions) and exposes two
capabilities: text generation (JSON reasoning) and image editing.
"""
import json
import logging
import time
from dataclasses import dataclass

import httpx
from google import genai
from google.genai import types
from google.genai import errors as genai_errors

from app.config import settings

logger = logging.getLogger(__name__)

# Statuses that mean "this model is unavailable right now": move on to the
# next model in the chain instead of making the customer wait.
_RETRYABLE_STATUS = {429, 500, 503, 504}
# How long a failing model is skipped before it is tried again.
_COOLDOWN_SEC = 300


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
        backups = [m.strip() for m in settings.gemini_fallback_models.split(",") if m.strip()]
        self.text_models = [self.text_model] + [m for m in backups if m != self.text_model]
        # model -> time until which it is skipped after a timeout/overload.
        self._cooldown_until: dict[str, float] = {}
        # Models that rejected thinking_level (400); sent without it afterwards.
        self._no_thinking: set[str] = set()
        self._thinking_level = (settings.gemini_thinking_level or "").strip().upper() or None

    def _json_config(
        self, max_output_tokens: int, temperature: float = 0.4
    ) -> types.GenerateContentConfig:
        """JSON config with a capped output size and a low thinking level."""
        kwargs = dict(
            response_mime_type="application/json",
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
        if self._thinking_level:
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=self._thinking_level)
        return types.GenerateContentConfig(**kwargs)

    # ------------------------------------------------- model fallback chain
    def _generate_with_retry(self, *, contents, config):
        """Try the main model, then each backup model.

        A model that times out or is overloaded (429/500/503/504) is skipped
        for _COOLDOWN_SEC so later requests do not wait on it again. Other
        errors (e.g. a bad request) are raised immediately.
        """
        now = time.monotonic()
        models = [m for m in self.text_models if self._cooldown_until.get(m, 0) <= now]
        if not models:  # everything is cooling down: try them all anyway
            models = list(self.text_models)

        last_exc: Exception | None = None
        for model in models:
            cfg = config
            if model in self._no_thinking and cfg.thinking_config is not None:
                cfg = cfg.model_copy(update={"thinking_config": None})
            for _ in range(2):  # 2nd pass only after dropping thinking_level
                try:
                    return self._client.models.generate_content(
                        model=model, contents=contents, config=cfg
                    )
                except genai_errors.APIError as exc:
                    status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
                    if status == 400 and cfg.thinking_config is not None and "think" in str(exc).lower():
                        logger.warning("Model %s menolak thinking_level, dikirim tanpa itu.", model)
                        self._no_thinking.add(model)
                        cfg = cfg.model_copy(update={"thinking_config": None})
                        continue
                    if status in _RETRYABLE_STATUS:
                        logger.warning("Gemini %s gagal (%s), coba model berikutnya.", model, status)
                        self._cooldown_until[model] = time.monotonic() + _COOLDOWN_SEC
                        last_exc = exc
                        break
                    raise
                except httpx.TimeoutException as exc:
                    logger.warning("Gemini %s timeout, coba model berikutnya.", model)
                    self._cooldown_until[model] = time.monotonic() + _COOLDOWN_SEC
                    last_exc = exc
                    break
        raise last_exc or RuntimeError("Gemini tidak merespons.")

    # ------------------------------------------------------------------ text
    def generate_json(
        self, prompt: str, max_output_tokens: int = 1024, temperature: float = 0.4
    ) -> dict:
        """Call the text model and parse a JSON object from the response."""
        response = self._generate_with_retry(
            contents=prompt, config=self._json_config(max_output_tokens, temperature),
        )
        text = (response.text or "").strip()
        if not text:
            raise ValueError("Model teks tidak mengembalikan konten.")
        return self._parse_json(text)

    def generate_json_with_image(
        self, *, prompt: str, image: bytes, image_mime: str,
        max_output_tokens: int = 1024, temperature: float = 0.4,
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
            config=self._json_config(max_output_tokens, temperature),
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
