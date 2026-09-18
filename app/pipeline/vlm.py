"""LLM/VLM через Ollama REST API (+ stub-провайдер для тестов без моделей)."""
import base64
import time
from pathlib import Path

import requests

FRAME_PROMPT = """Ты анализируешь кадр из видео (запись экрана или лекция).
Оцени, что показано: слайд с новым материалом, код, диаграмма, таблица, интерфейс, спикер.
Ответ строго в формате двух строк:
ВАЖНОСТЬ: <высокая|средняя|низкая>  (высокая = новый материал: слайд, код, диаграмма, таблица)
ОПИСАНИЕ: <2-4 предложения на русском: что на экране, ключевые данные — заголовки, термины, цифры, код>"""


class Ollama:
    def __init__(self, cfg: dict, log):
        self.host = cfg.get("ollama_url", "http://127.0.0.1:11434").rstrip("/")
        self.vlm_model = cfg.get("vlm_model", "qwen2.5vl:7b")
        self.llm_model = cfg.get("llm_model", "qwen2.5:14b-instruct-q4_K_M")
        self.temperature = cfg.get("temperature", 0.2)
        self.log = log

    def available(self) -> bool:
        try:
            r = requests.get(f"{self.host}/v1/models", timeout=5)
            r.raise_for_status()
            return True
        except Exception:
            return False

    def _chat(self, model: str, messages: list, timeout: int = 900) -> str:
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": self.temperature},
        }
        last_err = None
        for attempt in range(3):
            try:
                r = requests.post(f"{self.host}/api/chat", json=payload, timeout=timeout)
                r.raise_for_status()
                return r.json()["message"]["content"].strip()
            except Exception as e:
                last_err = e
                self.log.warning("Ollama %s: попытка %d/3 не удалась: %s", model, attempt + 1, e)
                time.sleep(5)
        raise RuntimeError(f"Ollama {model}: {last_err}")

    def describe_frame(self, image: Path) -> tuple[str, str]:
        """-> (важность, описание)."""
        b64 = base64.b64encode(image.read_bytes()).decode()
        raw = self._chat(self.vlm_model, [
            {"role": "user", "content": FRAME_PROMPT, "images": [b64]},
        ])
        importance, description = "средняя", raw
        lines = raw.strip().splitlines()
        for i, line in enumerate(lines):
            if line.strip().upper().startswith("ВАЖНОСТЬ"):
                importance = line.split(":", 1)[1].strip().lower() or "средняя"
                rest = " ".join(l for l in lines[i + 1:] if l.strip())
                if rest.upper().startswith("ОПИСАНИЕ"):
                    rest = rest.split(":", 1)[1].strip()
                if rest:
                    description = rest
                break
        return importance, description

    def chat(self, prompt: str) -> str:
        return self._chat(self.llm_model, [{"role": "user", "content": prompt}])


class LMStudio:
    """LLM/VLM через LM Studio (OpenAI-совместимый API)."""

    def __init__(self, cfg: dict, log):
        self.host = cfg.get("lmstudio_url", "http://127.0.0.1:1234").rstrip("/")
        self.vlm_model = cfg.get("vlm_model", "qwen2.5vl:7b")
        self.llm_model = cfg.get("llm_model", "qwen2.5:14b-instruct-q4_K_M")
        self.temperature = cfg.get("temperature", 0.2)
        self.log = log

    def available(self) -> bool:
        try:
            r = requests.get(f"{self.host}/v1/models", timeout=5)
            r.raise_for_status()
            return True
        except Exception:
            return False

    def _chat(self, model: str, messages: list, timeout: int = 900) -> str:
        payload = {
            "model": model,
            "messages": messages,
            "temperature": self.temperature,
        }
        last_err = None
        for attempt in range(3):
            try:
                r = requests.post(f"{self.host}/v1/chat/completions", json=payload, timeout=timeout)
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"].strip()
            except Exception as e:
                last_err = e
                self.log.warning("LMStudio %s: попытка %d/3 не удалась: %s", model, attempt + 1, e)
                time.sleep(5)
        raise RuntimeError(f"LMStudio {model}: {last_err}")

    def describe_frame(self, image: Path) -> tuple[str, str]:
        """-> (важность, описание)."""
        b64 = base64.b64encode(image.read_bytes()).decode()
        # LM Studio поддерживает изображения через image_url в content
        raw = self._chat(self.vlm_model, [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": FRAME_PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            },
        ])
        importance, description = "средняя", raw
        lines = raw.strip().splitlines()
        for i, line in enumerate(lines):
            if line.strip().upper().startswith("ВАЖНОСТЬ"):
                importance = line.split(":", 1)[1].strip().lower() or "средняя"
                rest = " ".join(l for l in lines[i + 1:] if l.strip())
                if rest.upper().startswith("ОПИСАНИЕ"):
                    rest = rest.split(":", 1)[1].strip()
                if rest:
                    description = rest
                break
        return importance, description

    def chat(self, prompt: str) -> str:
        return self._chat(self.llm_model, [{"role": "user", "content": prompt}])


class StubLLM:
    """Детерминированный провайдер для smoke-тестов (без Ollama/моделей)."""

    def __init__(self, cfg: dict, log):
        self.log = log

    def available(self) -> bool:
        return True

    def describe_frame(self, image: Path) -> tuple[str, str]:
        return "средняя", f"[stub] Кадр {image.name}: тестовое описание контента экрана."

    def chat(self, prompt: str) -> str:
        return "[stub] Тестовая выжимка фрагмента.\n- Тезис 1 [00:00]\n- Тезис 2 [01:00]"


def make_llm(cfg: dict, log):
    provider = cfg.get("provider", "ollama")
    if provider == "stub":
        return StubLLM(cfg, log)
    if provider == "lmstudio":
        return LMStudio(cfg, log)
    return Ollama(cfg, log)
