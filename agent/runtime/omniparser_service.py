from __future__ import annotations

import base64
import hashlib
import importlib
import io
import logging
from collections import OrderedDict
from typing import Any

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)


def _load_requests_module() -> Any:
    return importlib.import_module("requests")


class OmniParserService:
    """Encapsula a chamada da API local do OmniParser com fallbacks seguros."""

    def __init__(
        self,
        api_url: str = "http://localhost:5000/parse",
        enabled: bool = True,
        *,
        timeout_seconds: float = 5.0,
        cache_enabled: bool = True,
        max_cache_entries: int = 64,
        requests_module: Any | None = None,
    ) -> None:
        self.api_url = str(api_url or "http://localhost:5000/parse").strip()
        self.enabled = bool(enabled)
        self.timeout_seconds = max(float(timeout_seconds), 0.1)
        self.cache_enabled = bool(cache_enabled)
        self.max_cache_entries = max(int(max_cache_entries), 1)
        self._requests_module = requests_module
        self._cache: OrderedDict[str, dict[str, tuple[int, int]]] = OrderedDict()
        logger.info(
            "[OMNIPARSER] Inicializado - api=%s enabled=%s",
            self.api_url,
            self.enabled,
        )

    def parse_screen(self, image: np.ndarray) -> dict[str, tuple[int, int]]:
        if not self.enabled:
            logger.debug("[OMNIPARSER] Desabilitado - retornando vazio")
            return {}

        try:
            cache_key = self._build_cache_key(image) if self.cache_enabled else None
        except Exception as exc:
            logger.error("[OMNIPARSER] Erro ao preparar cache do frame: %s", exc)
            return {}

        if cache_key is not None:
            cached = self._cache.get(cache_key)
            if cached is not None:
                self._cache.move_to_end(cache_key)
                logger.info("[OMNIPARSER] Cache hit - elements=%d", len(cached))
                return dict(cached)

        image_b64 = self.encode_image_to_base64_png(image)
        if not image_b64:
            return {}

        requests_module = self._get_requests_module()
        if requests_module is None:
            return {}

        try:
            response = requests_module.post(
                self.api_url,
                json={"image": image_b64},
                timeout=self.timeout_seconds,
            )
        except self._timeout_exception(requests_module):
            logger.error("[OMNIPARSER] Timeout ao chamar API")
            return {}
        except Exception as exc:
            logger.error("[OMNIPARSER] Erro ao chamar API: %s", exc)
            return {}

        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code != 200:
            logger.error("[OMNIPARSER] API retornou erro - status=%d", status_code)
            return {}

        try:
            payload = response.json()
        except Exception as exc:
            logger.error("[OMNIPARSER] Erro ao fazer parse do JSON: %s", exc)
            return {}

        elements = self.parse_api_response(payload)
        logger.info("[OMNIPARSER] ✅ Análise completa - elements=%d", len(elements))
        for label, coords in elements.items():
            logger.info("[OMNIPARSER] Element: %s @ %s", label, coords)

        if cache_key is not None:
            self._store_cache_entry(cache_key, elements)
        return elements

    def encode_image_to_base64_png(self, image: np.ndarray) -> str:
        try:
            pil_image = self._numpy_to_pil_image(image)
        except Exception as exc:
            logger.error("[OMNIPARSER] Erro ao converter imagem: %s", exc)
            return ""

        try:
            buffer = io.BytesIO()
            pil_image.save(buffer, format="PNG")
            encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
            logger.info("[OMNIPARSER] Imagem convertida para base64 PNG - bytes=%d", len(encoded))
            return encoded
        except Exception as exc:
            logger.error("[OMNIPARSER] Erro ao encodar imagem em PNG base64: %s", exc)
            return ""

    def parse_api_response(self, payload: object) -> dict[str, tuple[int, int]]:
        if not isinstance(payload, dict):
            logger.error("[OMNIPARSER] Payload da API invalido - esperado dict")
            return {}

        raw_elements = payload.get("elements", {})
        if not isinstance(raw_elements, dict):
            logger.error("[OMNIPARSER] Campo 'elements' invalido - esperado dict")
            return {}

        parsed: dict[str, tuple[int, int]] = {}
        for raw_label, raw_coords in raw_elements.items():
            label = str(raw_label or "").strip()
            coords = self._normalize_coords(raw_coords)
            if not label or coords is None:
                logger.debug(
                    "[OMNIPARSER] Ignorando elemento invalido - label=%r coords=%r",
                    raw_label,
                    raw_coords,
                )
                continue
            parsed[label] = coords
        return parsed

    def _get_requests_module(self) -> Any | None:
        if self._requests_module is not None:
            return self._requests_module
        try:
            self._requests_module = _load_requests_module()
            return self._requests_module
        except ModuleNotFoundError:
            logger.error("[OMNIPARSER] Pacote requests nao instalado. Execute: pip install requests")
            return None

    @staticmethod
    def _timeout_exception(requests_module: Any) -> type[BaseException]:
        exceptions = getattr(requests_module, "exceptions", None)
        timeout_exc = getattr(exceptions, "Timeout", None) if exceptions is not None else None
        return timeout_exc if isinstance(timeout_exc, type) else TimeoutError

    def _numpy_to_pil_image(self, image: np.ndarray) -> Image.Image:
        if not isinstance(image, np.ndarray):
            raise TypeError("frame precisa ser numpy.ndarray")
        if image.size == 0:
            raise ValueError("frame vazio")
        if image.dtype != np.uint8:
            raise ValueError("frame precisa estar em uint8")
        if image.ndim == 2:
            return Image.fromarray(image, mode="L")
        if image.ndim != 3:
            raise ValueError("frame precisa ter 2 ou 3 dimensoes")

        channels = int(image.shape[2])
        if channels == 3:
            image_rgb = image[:, :, ::-1]
            return Image.fromarray(np.ascontiguousarray(image_rgb), mode="RGB")
        if channels == 4:
            image_rgba = image[:, :, [2, 1, 0, 3]]
            return Image.fromarray(np.ascontiguousarray(image_rgba), mode="RGBA")
        raise ValueError(f"frame com quantidade de canais nao suportada: {channels}")

    @staticmethod
    def _normalize_coords(value: object) -> tuple[int, int] | None:
        if isinstance(value, dict):
            if "x" not in value or "y" not in value:
                return None
            return int(value["x"]), int(value["y"])
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            return int(value[0]), int(value[1])
        return None

    def _build_cache_key(self, image: np.ndarray) -> str:
        if not isinstance(image, np.ndarray):
            raise TypeError("frame precisa ser numpy.ndarray")
        contiguous = np.ascontiguousarray(image)
        digest = hashlib.sha256()
        digest.update(str(contiguous.dtype).encode("ascii"))
        digest.update(str(tuple(contiguous.shape)).encode("ascii"))
        digest.update(contiguous.tobytes())
        return digest.hexdigest()

    def _store_cache_entry(self, cache_key: str, elements: dict[str, tuple[int, int]]) -> None:
        self._cache[cache_key] = dict(elements)
        self._cache.move_to_end(cache_key)
        while len(self._cache) > self.max_cache_entries:
            self._cache.popitem(last=False)


__all__ = ["OmniParserService"]
