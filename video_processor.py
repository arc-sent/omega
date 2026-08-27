"""Обработка видео перед публикацией в VK.

Перекодирует ролик через libx264 (реальный ре-энкод: меняется каждый байт файла,
а не только контейнер) и подменяет метаданные. Исходные теги (creation_time
TikTok, GPS, encoder, следы приложения) не просто стираются — пустые метаданные
сами по себе выглядят неестественно, — а заменяются правдоподобным набором
«снято на телефон»: свежий creation_time, консистентная пара make/model,
мобильные handler_name. Следы ffmpeg (Lavf/Lavc в тегах) глушатся bitexact.

Модуль синхронный (как downloader и vk) — вызывать из executor'а.
process_video НИКОГДА не бросает исключение: при любой беде возвращает исходный
путь со статусом, а решение о публикации оригинала принимает вызывающий.
"""

import logging
import os
import random
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# ─── Настройки из окружения ───────────────────────────────────────────────────
# Ролики длиннее лимита не обрабатываем: на 1 ядре перекод такого ролика съедает
# слот целиком. Публикуются как есть.
MAX_DURATION = int(os.getenv("VIDEO_PROCESS_MAX_DURATION", "60"))
TIMEOUT = int(os.getenv("VIDEO_PROCESS_TIMEOUT", "300"))
# Приоритет ffmpeg: на 1 ядре без nice перекод отбирает CPU у event loop бота.
NICE = int(os.getenv("VIDEO_PROCESS_NICE", "10"))
# Короткая сторона при включённом даунскейле (апскейла не делаем).
DOWNSCALE_SHORT_SIDE = int(os.getenv("VIDEO_DOWNSCALE_SIDE", "720"))

# ─── Пресеты ──────────────────────────────────────────────────────────────────
# Порядок = порядок кнопок в боте. Оценки — для вертикали 1080x1920, 60 c, 1 ядро.
# Выше veryfast/faster не поднимаемся: medium на одном ядре это 2–4 минуты.
OFF = "off"

# Ключи в db.settings: режим перекода и тумблер даунскейла.
SETTING_MODE = "video_process_mode"
SETTING_DOWNSCALE = "video_downscale"

PRESETS: dict[str, dict] = {
    "ultrafast": {"label": "Быстро",          "x264": "ultrafast", "crf": 26, "eta": "~10–20 с"},
    "superfast": {"label": "Сбалансированно", "x264": "superfast", "crf": 24, "eta": "~20–35 с"},
    "veryfast":  {"label": "Качественно",     "x264": "veryfast",  "crf": 23, "eta": "~35–70 с"},
    "faster":    {"label": "Максимум",        "x264": "faster",    "crf": 23, "eta": "~60–110 с"},
}

# Правдоподобные наборы «производитель, модель, версия Android, аппаратный
# энкодер». Энкодер подобран под реальный чип модели: имя OMX/Codec2 из тега
# encoder — то, что пишет камера телефона, и оно перекрывает ffmpeg'овый «Lavc».
_DEVICES = [
    ("samsung", "SM-A536B",   "13", "c2.exynos.h264.encoder"),
    ("samsung", "SM-G991B",   "14", "c2.exynos.h264.encoder"),
    ("samsung", "SM-A346E",   "14", "c2.mtk.avc.encoder"),
    ("Xiaomi",  "2201123G",   "13", "c2.qti.avc.encoder"),
    ("Xiaomi",  "23021RAA2Y", "14", "c2.qti.avc.encoder"),
    ("Xiaomi",  "22101316G",  "13", "c2.mtk.avc.encoder"),
    ("realme",  "RMX3630",    "13", "OMX.MTK.VIDEO.ENCODER.AVC"),
    ("HUAWEI",  "STG-LX1",    "12", "OMX.hisi.video.encoder.avc"),
    ("OPPO",    "CPH2481",    "13", "c2.qti.avc.encoder"),
    ("vivo",    "V2111",      "13", "c2.mtk.avc.encoder"),
]


def is_enabled(mode: str | None) -> bool:
    return bool(mode) and mode in PRESETS


def preset_label(mode: str | None) -> str:
    return PRESETS[mode]["label"] if is_enabled(mode) else "выключено"


# ─── Пробинг ──────────────────────────────────────────────────────────────────

def _probe(path: str) -> dict:
    """Длительность и размер кадра. Пустой dict, если ffprobe не смог."""
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height:format=duration",
             "-of", "default=noprint_wrappers=1:nokey=0", path],
            capture_output=True, text=True, timeout=60,
        )
    except Exception:
        logger.exception("ffprobe не сработал — обрабатываю вслепую")
        return {}

    out = {}
    for line in res.stdout.splitlines():
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if not val or val == "N/A":
            continue
        try:
            out[key] = float(val) if key == "duration" else int(val)
        except ValueError:
            pass
    return out


def _scale_size(width: int, height: int) -> tuple[int, int] | None:
    """Новый размер для даунскейла или None, если ролик и так небольшой."""
    short = min(width, height)
    if short <= DOWNSCALE_SHORT_SIDE:
        return None  # апскейлом ничего не улучшим
    k = DOWNSCALE_SHORT_SIDE / short
    # libx264 требует чётных сторон (yuv420p)
    return max(2, round(width * k / 2) * 2), max(2, round(height * k / 2) * 2)


# ─── Метаданные ───────────────────────────────────────────────────────────────

def _fake_metadata(now: datetime | None = None) -> list[str]:
    """Аргументы -metadata: «снято на телефон незадолго до публикации»."""
    now = now or datetime.now(timezone.utc)
    make, model, android, encoder = random.choice(_DEVICES)
    # Снято за 20 минут … 6 часов до публикации — не ровно в секунду слота.
    shot_at = now - timedelta(seconds=random.randint(20 * 60, 6 * 3600))
    stamp = shot_at.strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    return [
        "-metadata", f"creation_time={stamp}",
        "-metadata", f"com.android.version={android}",
        "-metadata", f"com.android.manufacturer={make}",
        "-metadata", f"com.android.model={model}",
        "-metadata", f"make={make}",
        "-metadata", f"model={model}",
        "-metadata:s:v", f"creation_time={stamp}",
        "-metadata:s:v", "handler_name=VideoHandle",
        # перекрывает тег «Lavc libx264», который иначе остаётся в потоке
        "-metadata:s:v", f"encoder={encoder}",
        "-metadata:s:a", f"creation_time={stamp}",
        "-metadata:s:a", "handler_name=SoundHandle",
    ]


# ─── Сборка команды ───────────────────────────────────────────────────────────

def build_command(src: str, dst: str, mode: str, *, scale: tuple[int, int] | None = None,
                  metadata: list[str] | None = None) -> list[str]:
    """Полная командная строка ffmpeg для перекодирования."""
    preset = PRESETS[mode]
    cmd = []
    if NICE > 0 and shutil.which("nice"):
        cmd += ["nice", "-n", str(NICE)]
    cmd += ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", src]
    cmd += ["-map_metadata", "-1"]  # снести всё исходное: TikTok, GPS, encoder
    if scale:
        cmd += ["-vf", f"scale={scale[0]}:{scale[1]}"]
    cmd += [
        "-c:v", "libx264",
        "-preset", preset["x264"],
        "-crf", str(preset["crf"]),
        "-pix_fmt", "yuv420p",
        "-profile:v", "high",
        # На 1 ядре многопоточность x264 только добавляет переключений контекста.
        "-threads", "1",
        "-c:a", "aac", "-b:a", "128k",
        # bitexact глушит теги Lavf/Lavc — следы перекодирования ffmpeg.
        "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact",
        # use_metadata_tags нужен, чтобы mp4 принял нестандартные ключи (com.android.*)
        "-movflags", "+faststart+use_metadata_tags",
    ]
    cmd += metadata if metadata is not None else _fake_metadata()
    cmd.append(dst)
    return cmd


# ─── Точка входа ──────────────────────────────────────────────────────────────

def process_video(path: str, mode: str, downscale: bool = False) -> tuple[str, str, str]:
    """Обработать ролик перед публикацией.

    Возвращает (путь, статус, деталь):
      ("…исх",  "off",     "")                — обработка выключена;
      ("…новый","done",    "superfast, 720p") — перекодировано, публиковать новый файл;
      ("…исх",  "skipped", "> 60 с")          — сознательно пропущено, не ошибка;
      ("…исх",  "failed",  "текст ошибки")    — сбой, публиковать оригинал.
    Исходный файл при успехе удаляет вызывающий (см. scheduler._publish_job).
    """
    if not is_enabled(mode):
        return path, OFF, ""

    info = _probe(path)
    duration = info.get("duration")
    # Неизвестную длительность лимит не отбрасывает (как и фильтры правил).
    if duration and duration > MAX_DURATION:
        return path, "skipped", f"> {MAX_DURATION} с"

    scale = None
    if downscale and info.get("width") and info.get("height"):
        scale = _scale_size(info["width"], info["height"])

    out_path = f"{path}.proc.mp4"
    cmd = build_command(path, out_path, mode, scale=scale)
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        cleanup(out_path)
        logger.warning("Обработка видео: ffmpeg не уложился в %s с (%s)", TIMEOUT, path)
        return path, "failed", f"ffmpeg не уложился в {TIMEOUT} с"
    except Exception as exc:
        cleanup(out_path)
        logger.exception("Обработка видео: не удалось запустить ffmpeg")
        return path, "failed", f"не удалось запустить ffmpeg: {exc}"

    if res.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        cleanup(out_path)
        err = (res.stderr or "").strip().replace("\n", " ")[-500:] or f"код возврата {res.returncode}"
        logger.warning("Обработка видео не удалась: %s", err)
        return path, "failed", err

    detail = PRESETS[mode]["x264"]
    if scale:
        detail += f", {min(scale)}p"
    logger.info("Видео обработано (%s): %s", detail, out_path)
    return out_path, "done", detail


def cleanup(path: str | None) -> None:
    """Удалить временный файл, молча пережив ошибку файловой системы."""
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass
