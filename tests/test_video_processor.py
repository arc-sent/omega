"""Тесты обработки видео: сборка команды ffmpeg и поведение при сбоях.

Реальный ffmpeg не запускается — subprocess.run подменяется. Проверяем то, что
можно сломать незаметно: набор аргументов, чистку метаданных, лимит длительности
и то, что при любой беде возвращается ИСХОДНЫЙ файл (публикация не срывается).
"""

import subprocess

import pytest

import db
import video_processor as vp


class _Res:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def video(tmp_path):
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"x" * 1024)
    return str(p)


def _fake_ffmpeg(monkeypatch, *, probe: str, returncode=0, stderr="", write_out=True):
    """Подменить subprocess.run: ffprobe отдаёт probe, ffmpeg создаёт файл."""
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "ffprobe":
            return _Res(stdout=probe)
        out = cmd[-1]
        if write_out and returncode == 0:
            with open(out, "wb") as f:
                f.write(b"y" * 2048)
        return _Res(returncode=returncode, stderr=stderr)

    monkeypatch.setattr(subprocess, "run", run)
    return calls


# ─── Сборка команды ───────────────────────────────────────────────────────────

def test_build_command_uses_preset_and_strips_metadata():
    cmd = vp.build_command("in.mp4", "out.mp4", "superfast", metadata=[])
    assert cmd[-1] == "out.mp4"
    assert "libx264" in cmd
    assert cmd[cmd.index("-preset") + 1] == "superfast"
    assert cmd[cmd.index("-crf") + 1] == "24"
    # исходные метаданные (TikTok, GPS, creation_time) сносятся полностью
    assert cmd[cmd.index("-map_metadata") + 1] == "-1"
    # следы ffmpeg в тегах глушатся
    assert "+bitexact" in cmd
    # на 1 ядре x264 не должен плодить потоки
    assert cmd[cmd.index("-threads") + 1] == "1"
    # без даунскейла фильтра нет
    assert "-vf" not in cmd


def test_build_command_scale_only_when_asked():
    cmd = vp.build_command("in.mp4", "out.mp4", "ultrafast", scale=(720, 1280), metadata=[])
    assert cmd[cmd.index("-vf") + 1] == "scale=720:1280"


def test_all_presets_build():
    for mode in vp.PRESETS:
        cmd = vp.build_command("in.mp4", "out.mp4", mode, metadata=[])
        assert cmd[cmd.index("-preset") + 1] == vp.PRESETS[mode]["x264"]


def test_fake_metadata_is_plausible():
    args = vp._fake_metadata()
    values = " ".join(args)
    # ставим правдоподобное «снято на телефон», а не пустоту
    assert any(a.startswith("creation_time=") for a in args)
    assert "make=" in values and "model=" in values
    # и никаких следов источника/перекодировщика
    assert "tiktok" not in values.lower()
    assert "ffmpeg" not in values.lower() and "lavf" not in values.lower()
    assert "-metadata:s:v" in args  # теги потоков тоже подменяются
    # ffmpeg иначе оставляет в потоке тег «Lavc libx264» — перекрываем именем
    # аппаратного энкодера телефона
    assert any(a.startswith("encoder=") and "lavc" not in a.lower() for a in args)


# ─── Даунскейл ────────────────────────────────────────────────────────────────

def test_scale_size_downscales_vertical():
    assert vp._scale_size(1080, 1920) == (720, 1280)


def test_scale_size_never_upscales():
    assert vp._scale_size(720, 1280) is None
    assert vp._scale_size(480, 854) is None


def test_scale_size_is_even():
    w, h = vp._scale_size(1079, 1921)
    assert w % 2 == 0 and h % 2 == 0


# ─── process_video ────────────────────────────────────────────────────────────

def test_off_returns_original_untouched(video, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("ffmpeg не должен запускаться при выключенной обработке")

    monkeypatch.setattr(subprocess, "run", boom)
    assert vp.process_video(video, "off") == (video, vp.OFF, "")


def test_done_returns_new_file(video, monkeypatch):
    _fake_ffmpeg(monkeypatch, probe="width=1080\nheight=1920\nduration=30.0")
    path, status, detail = vp.process_video(video, "superfast")
    assert status == "done"
    assert path != video and path.endswith(".proc.mp4")
    assert detail == "superfast"


def test_downscale_detail_and_filter(video, monkeypatch):
    calls = _fake_ffmpeg(monkeypatch, probe="width=1080\nheight=1920\nduration=30.0")
    path, status, detail = vp.process_video(video, "superfast", downscale=True)
    assert status == "done"
    assert detail == "superfast, 720p"
    ffmpeg_cmd = [c for c in calls if "ffmpeg" in c][0]
    assert ffmpeg_cmd[ffmpeg_cmd.index("-vf") + 1] == "scale=720:1280"


def test_long_video_skipped(video, monkeypatch):
    calls = _fake_ffmpeg(monkeypatch, probe="width=1080\nheight=1920\nduration=95.0")
    path, status, detail = vp.process_video(video, "veryfast")
    assert (path, status) == (video, "skipped")
    assert not [c for c in calls if "ffmpeg" in c]  # перекода не было


def test_unknown_duration_is_processed(video, monkeypatch):
    # инвариант проекта: неизвестная длительность не отбрасывает видео
    _fake_ffmpeg(monkeypatch, probe="width=1080\nheight=1920\nduration=N/A")
    _, status, _ = vp.process_video(video, "veryfast")
    assert status == "done"


def test_ffmpeg_failure_falls_back_to_original(video, monkeypatch):
    _fake_ffmpeg(monkeypatch, probe="duration=10.0", returncode=1,
                 stderr="Invalid data found", write_out=False)
    path, status, detail = vp.process_video(video, "ultrafast")
    assert (path, status) == (video, "failed")
    assert "Invalid data" in detail


def test_ffmpeg_timeout_falls_back(video, monkeypatch):
    def run(cmd, **kwargs):
        if cmd[0] == "ffprobe":
            return _Res(stdout="duration=10.0")
        raise subprocess.TimeoutExpired(cmd, vp.TIMEOUT)

    monkeypatch.setattr(subprocess, "run", run)
    path, status, detail = vp.process_video(video, "ultrafast")
    assert (path, status) == (video, "failed")
    assert "не уложился" in detail


def test_missing_ffmpeg_falls_back(video, monkeypatch):
    def run(cmd, **kwargs):
        if cmd[0] == "ffprobe":
            return _Res(stdout="duration=10.0")
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(subprocess, "run", run)
    path, status, _ = vp.process_video(video, "ultrafast")
    assert (path, status) == (video, "failed")


def test_empty_output_treated_as_failure(video, monkeypatch, tmp_path):
    def run(cmd, **kwargs):
        if cmd[0] == "ffprobe":
            return _Res(stdout="duration=10.0")
        open(cmd[-1], "wb").close()  # файл есть, но пустой
        return _Res(returncode=0)

    monkeypatch.setattr(subprocess, "run", run)
    path, status, _ = vp.process_video(video, "ultrafast")
    assert (path, status) == (video, "failed")


# ─── Хранение режима ──────────────────────────────────────────────────────────

def test_setting_roundtrip_and_default():
    assert db.get_setting(vp.SETTING_MODE, vp.OFF) == vp.OFF
    db.set_setting(vp.SETTING_MODE, "veryfast")
    assert db.get_setting(vp.SETTING_MODE) == "veryfast"
    db.set_setting(vp.SETTING_MODE, "ultrafast")  # перезапись, а не второй ключ
    assert db.get_setting(vp.SETTING_MODE) == "ultrafast"


def test_is_enabled():
    assert not vp.is_enabled("off")
    assert not vp.is_enabled(None)
    assert not vp.is_enabled("medium")  # такого пресета у нас нет
    assert vp.is_enabled("veryfast")
