"""Общие утилиты: конфиг, логирование, форматирование времени."""
import json
import logging
import os
import re
import sys
from pathlib import Path

def _find_root() -> Path:
    """Корень проекта: ближайшая вверх папка с config/config.json или config/docker.json."""
    here = Path(__file__).resolve().parent
    for candidate in (here, *here.parents[:5]):
        if (candidate / "config" / "config.json").exists() or (candidate / "config" / "docker.json").exists():
            return candidate
    return here.parent.parent  # фолбэк: три уровня вверх от common.py


_data_root = os.environ.get("VIDEONOTES_DATA_DIR")
ROOT = Path(_data_root).expanduser().resolve() if _data_root else _find_root()
CONFIG_PATH = Path(os.environ.get("VIDEONOTES_CONFIG", str(ROOT / "config" / "config.json")))
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".ts", ".flv", ".wmv", ".mpg", ".mpeg"}


def load_config() -> dict:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    # Переопределения через переменные окружения (Docker)
    if os.environ.get("OLLAMA_URL"):
        cfg.setdefault("llm", {})["ollama_url"] = os.environ["OLLAMA_URL"]
    if os.environ.get("LMSTUDIO_URL"):
        cfg.setdefault("llm", {})["lmstudio_url"] = os.environ["LMSTUDIO_URL"]
    if os.environ.get("ASR_DEVICE"):
        cfg.setdefault("asr", {})["device"] = os.environ["ASR_DEVICE"]
    if os.environ.get("LLM_PROVIDER"):
        cfg.setdefault("llm", {})["provider"] = os.environ["LLM_PROVIDER"]
    settings_file = ROOT / "runtime" / "tmp" / "model_settings.json"
    try:
        settings = json.loads(settings_file.read_text(encoding="utf-8"))
        cfg.setdefault("asr", {})["model"] = settings.get("asr_model", cfg["asr"].get("model"))
        cfg.setdefault("llm", {})["vlm_model"] = settings.get("vlm_model", cfg["llm"].get("vlm_model"))
        cfg.setdefault("llm", {})["llm_model"] = settings.get("llm_model", cfg["llm"].get("llm_model"))
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def setup_logging(log_path: Path | None = None) -> logging.Logger:
    logger = logging.getLogger("videonotes")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    return logger


def fmt_ts(seconds: float) -> str:
    """0:00:00 или MM:SS."""
    seconds = max(0, int(seconds))
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def sanitize(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|]+", " ", name)
    return re.sub(r"\s+", " ", name).strip() or "video"


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTS and path.is_file()
