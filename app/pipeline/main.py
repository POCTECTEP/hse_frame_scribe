"""VideoNotes: видео -> .md нейроконспект.

Команды:
  python app/pipeline/main.py process <video>   — обработать одно видео
  python app/pipeline/main.py watch             — следить за папкой runtime/in/
  python app/pipeline/main.py selftest          — проверка установки (модели, ffmpeg, Ollama)
"""
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import ROOT, VIDEO_EXTS, fmt_ts, is_video, load_config, sanitize, setup_logging


def process_video(video: Path, cfg: dict, log) -> Path:
    """Полный пайплайн. Возвращает путь к конспект.md."""
    t_start = time.time()
    video = video.resolve()
    if not is_video(video):
        raise ValueError(f"Не видео: {video}")
    log.info("Видео: %s (%.1f МБ)", video.name, video.stat().st_size / 1e6)

    import audio as A
    duration = A.ffprobe_duration(video)
    log.info("Длительность: %s", fmt_ts(duration))

    work = ROOT / "runtime" / "tmp" / f"{sanitize(video.stem)}_{datetime.now():%H%M%S}"
    work.mkdir(parents=True, exist_ok=True)

    # --- 1. Аудио ---
    log.info("[1/5] Извлечение аудио...")
    wav = work / "audio.wav"
    A.extract_audio(video, wav)

    # --- 2. VAD ---
    log.info("[2/5] Поиск речи (Silero VAD)...")
    segments = A.vad_segments(wav, threshold=cfg["asr"].get("vad_threshold", 0.5))
    chunks = A.merge_split_segments(segments, max_len=cfg["asr"].get("max_segment_seconds", 22))
    log.info("Речевых сегментов: %d, чанков для ASR: %d", len(segments), len(chunks))

    # --- 3. ASR ---
    log.info("[3/5] Транскрипция (GigaAM %s)...", cfg["asr"].get("model", "v3_e2e_rnnt"))
    from asr import GigaAMASR
    asr = GigaAMASR(cfg["asr"], log)
    asr.load()
    lines: list[tuple[float, str]] = []
    try:
        for i, (cs, ce) in enumerate(chunks, 1):
            chunk_wav = work / f"chunk_{i:04d}.wav"
            A.cut_wav(wav, cs, ce, chunk_wav)
            res = asr.transcribe_chunk(chunk_wav, cs)
            if res.text:
                lines.append((cs, res.text))
            if i % 10 == 0 or i == len(chunks):
                log.info("  ASR: %d/%d (%.0f%%)", i, len(chunks), 100 * i / max(1, len(chunks)))
    finally:
        asr.unload()
    log.info("Транскрипция готова: %d строк, ~%d слов", len(lines), sum(len(l[1].split()) for l in lines))

    # --- 4. Кадры ---
    log.info("[4/5] Кадры: детект сцен + извлечение...")
    import frames as F
    scenes = F.detect_scenes(video, cfg["frames"].get("scene_threshold", 0.3))
    plan = F.plan_frames(duration, scenes,
                         cfg["frames"].get("interval_seconds", 90),
                         cfg["frames"].get("max_frames", 100))
    log.info("Сцен найдено: %d, кадров в плане: %d", len(scenes), len(plan))
    frames_dir = work / "frames"
    frames_dir.mkdir(exist_ok=True)
    from vlm import make_llm
    llm = make_llm(cfg["llm"], log)
    frames: list[dict] = []
    for i, t in enumerate(plan, 1):
        jpg = frames_dir / f"frame_{i:03d}.jpg"
        if not F.extract_frame(video, t, jpg, cfg["frames"].get("width", 1280)):
            continue
        try:
            importance, desc = llm.describe_frame(jpg)
        except Exception as e:
            log.warning("  кадр %d: VLM ошибка: %s", i, e)
            importance, desc = "низкая", "(описание не удалось)"
        frames.append({"t": t, "file": jpg.name, "importance": importance, "desc": desc})
        log.info("  кадр %d [%s]: важность=%s | %s", i, fmt_ts(t), importance, desc[:100])
        if i % 10 == 0 or i == len(plan):
            log.info("  кадры: %d/%d (%.0f%%)", i, len(plan), 100 * i / max(1, len(plan)))

    # --- 5. Конспект ---
    log.info("[5/5] Нейроконспект (map-reduce)...")
    import summarize as S
    concept_body = S.summarize(llm, lines, frames, log,
                               max_words=cfg["llm"].get("chunk_words", 2000))

    out_root = Path(cfg["output"]["dir"])
    if not out_root.is_absolute():
        out_root = (ROOT / out_root).resolve()
    out_dir = out_root / sanitize(video.stem)
    n = 2
    while out_dir.exists():
        out_dir = out_root / f"{sanitize(video.stem)}_{n}"
        n += 1
    (out_dir / "кадры").mkdir(parents=True)
    for f in frames:
        shutil.copy2(frames_dir / f["file"], out_dir / "кадры" / f["file"])

    import report as R
    R.write_transcript(out_dir / "транскрипция.md", sanitize(video.stem), duration,
                       lines, cfg["asr"].get("model", "v3_e2e_rnnt"))
    concept = R.write_concept(out_dir, sanitize(video.stem), video.name, duration,
                              concept_body, frames, cfg["llm"].get("vlm_model", ""),
                              cfg["asr"].get("model", "v3_e2e_rnnt"))
    log.info("Готово за %.0f мин: %s", (time.time() - t_start) / 60, concept)
    return concept


def extract_audio_track(video: Path, cfg: dict, log) -> Path:
    """Видео -> WAV без обращения к ASR, VLM или LLM."""
    video = video.resolve()
    if not is_video(video):
        raise ValueError(f"Не видео: {video}")
    import audio as A
    duration = A.ffprobe_duration(video)
    out_root = Path(cfg["output"]["dir"])
    if not out_root.is_absolute():
        out_root = (ROOT / out_root).resolve()
    out_dir = out_root / sanitize(video.stem)
    n = 2
    while out_dir.exists():
        out_dir = out_root / f"{sanitize(video.stem)}_{n}"
        n += 1
    out_dir.mkdir(parents=True)
    audio_path = out_dir / "audio.wav"
    log.info("[1/1] Извлечение аудиодорожки (%s)...", fmt_ts(duration))
    A.extract_audio(video, audio_path)
    log.info("Аудиодорожка сохранена: %s", audio_path)
    return audio_path


def watch(cfg: dict, log) -> None:
    in_dir = ROOT / "runtime" / "in"
    in_dir.mkdir(exist_ok=True)
    poll = cfg.get("watch", {}).get("poll_seconds", 3)
    move_on_success = cfg.get("watch", {}).get("move_on_success", True)
    log.info("Слежу за %s (раз в %d с). Бросай видео сюда, Ctrl+C — стоп.", in_dir, poll)
    status = in_dir / "СТАТУС.txt"
    while True:
        pending = sorted(p for p in in_dir.iterdir() if is_video(p))
        if pending:
            v = pending[0]
            status.write_text(f"Обработка: {v.name}\nНачато: {datetime.now():%Y-%m-%d %H:%M}\n",
                              encoding="utf-8")
            try:
                out = process_video(v, cfg, log)
                if move_on_success:
                    (in_dir / "processed").mkdir(exist_ok=True)
                    shutil.move(str(v), str(in_dir / "processed" / v.name))
                status.write_text(f"✓ {v.name} -> {out}\n", encoding="utf-8")
            except Exception as e:
                log.exception("Ошибка обработки %s: %s", v.name, e)
                (in_dir / "failed").mkdir(exist_ok=True)
                shutil.move(str(v), str(in_dir / "failed" / v.name))
                status.write_text(f"✗ {v.name}: {e}\n", encoding="utf-8")
        else:
            time.sleep(poll)


def selftest(cfg: dict, log) -> int:
    """Проверка установки: ffmpeg, GigaAM (транскрипция), кадры, Ollama."""
    import urllib.request
    ok = True

    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        log.info("✓ ffmpeg/ffprobe на месте")
    else:
        log.error("✗ ffmpeg не найден в PATH"); ok = False

    log.info("Проверка GigaAM (первый запуск скачает модель 449,2 МБ)...")
    try:
        from asr import GigaAMASR
        asr = GigaAMASR(cfg["asr"], log)
        asr.load()
        sample = ROOT / "runtime" / "tmp" / "example.wav"
        sample.parent.mkdir(parents=True, exist_ok=True)
        if not sample.exists():
            urllib.request.urlretrieve(
                "https://cdn.chatwm.opensmodel.sberdevices.ru/GigaAM/example.wav", str(sample))
        res = asr.transcribe_chunk(sample, 0.0)
        asr.unload()
        if res.text:
            log.info("✓ ASR работает: «%s»", res.text[:100])
        else:
            log.error("✗ ASR вернул пустоту"); ok = False
    except Exception as e:
        log.error("✗ GigaAM: %s", e); ok = False

    log.info("Проверка кадров (синтетическое видео 30 с)...")
    try:
        import subprocess
        vid = ROOT / "runtime" / "tmp" / "selftest_video.mp4"
        font = ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
                if Path("/usr/share/fonts").exists() else "C:/Windows/Fonts/arial.ttf")
        subprocess.run([
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"color=c=white:s=1280x720:d=30:r=10",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=30",
            "-vf", f"drawtext=fontfile={font}:text=Slide1_test:fontsize=48:x=100:y=300",
            "-shortest", str(vid),
        ], check=True)
        import frames as F
        plan = F.plan_frames(30, F.detect_scenes(vid, 0.3), 10, 5)
        jpg = ROOT / "runtime" / "tmp" / "selftest_frame.jpg"
        if plan and F.extract_frame(vid, plan[0], jpg, 640):
            log.info("✓ Кадры: план %d, извлечён %s", len(plan), jpg.name)
        else:
            log.error("✗ Кадры: не удалось извлечь"); ok = False
    except Exception as e:
        log.error("✗ Кадры: %s", e); ok = False

    from vlm import make_llm
    llm = make_llm(cfg["llm"], log)
    if llm.available():
        provider = cfg.get("llm", {}).get("provider", "ollama")
        url = cfg["llm"].get("lmstudio_url" if provider == "lmstudio" else "ollama_url", "")
        provider_name = "LM Studio" if provider == "lmstudio" else "Ollama"
        log.info("✓ %s доступна: %s", provider_name, url)
        try:
            r = llm.chat("Ответь одним словом: тест")
            log.info("✓ LLM отвечает: %s", r[:60])
        except Exception as e:
            log.error("✗ LLM запрос: %s", e); ok = False
    else:
        if cfg["llm"].get("provider") == "stub":
            log.info("✓ LLM: stub (тестовый режим)")
        else:
            provider = cfg.get("llm", {}).get("provider", "ollama")
            url = cfg["llm"].get("lmstudio_url" if provider == "lmstudio" else "ollama_url", "")
            provider_name = "LM Studio" if provider == "lmstudio" else "Ollama"
            log.error("✗ %s недоступна по %s — запусти %s",
                      provider_name, url, provider_name if provider == "lmstudio" else "ollama serve")

    log.info("SELFTEST %s", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


def main() -> int:
    cfg = load_config()
    log = setup_logging(ROOT / "runtime" / "logs" / f"run_{datetime.now():%Y%m%d_%H%M%S}.log")
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1
    cmd = args[0]
    if cmd == "process":
        if len(args) < 2:
            log.error("Нужен путь к видео: process <video>")
            return 1
        process_video(Path(args[1]), cfg, log)
        return 0
    if cmd == "extract-audio":
        if len(args) < 2:
            log.error("Нужен путь к видео: extract-audio <video>")
            return 1
        extract_audio_track(Path(args[1]), cfg, log)
        return 0
    if cmd == "watch":
        watch(cfg, log)
        return 0
    if cmd == "selftest":
        return selftest(cfg, log)
    log.error("Неизвестная команда: %s", cmd)
    return 1


if __name__ == "__main__":
    sys.exit(main())
