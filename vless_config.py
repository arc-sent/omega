"""Конфиг Xray из VLESS-ключа или подписки — локальный SOCKS-прокси для скачивания видео.

yt-dlp ходит только через HTTP/SOCKS-прокси и протокол VLESS не знает. Поэтому рядом с
ботом запускается контейнер Xray (docker-compose, профиль «vless»), который открывает
SOCKS5 на xray:1080 и гонит трафик через VLESS. Этот скрипт собирает для него
xray/config.json. Запускается на хосте, зависимостей нет (только стандартная библиотека):

    python3 vless_config.py              # берёт VLESS_URL (окружение или .env)
    python3 vless_config.py "vless://…"  # или аргументом (останется в истории shell)

VLESS_URL — один ключ vless://… или адрес подписки https://… (base64-список или строки).
Из подписки берутся все подходящие vless-серверы; заглушки вида 0.0.0.0 («⬇️ Внизу
Домашние ⬇️») отбрасываются. Отбор по названию — части имён через запятую, без учёта
регистра: VLESS_PICK (оставить только эти), VLESS_SKIP (убрать эти). Несколько серверов
кладутся в один конфиг с балансировщиком: Xray сам проверяет их (observatory) и ведёт
трафик через живой с наименьшей задержкой — умер сервер, переключится без перезапуска.
Лучше оставить через VLESS_PICK 3–4 сервера, а не всю подписку: меньше лишних проверок.

Поддержаны транспорты tcp, ws, grpc и защита none / tls / reality. Остальное (xhttp,
httpupgrade, http-заголовки tcp) скрипт отвергает с понятным сообщением, а не генерирует
конфиг наугад. Файл с ключами создаётся с правами 600, а папка xray/ — в .gitignore.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request
from urllib.parse import parse_qs, unquote, urlsplit

SOCKS_PORT = 1080
SUPPORTED_NETWORKS = ("tcp", "ws", "grpc")
SUPPORTED_SECURITY = ("none", "tls", "reality")
# Чем Xray проверяет серверы подписки: нужен именно TikTok, ради него прокси и заведён.
PROBE_URL = "https://www.tiktok.com/robots.txt"
PROBE_INTERVAL = "10m"
PLACEHOLDER_HOSTS = ("0.0.0.0", "127.0.0.1", "localhost", "::", "::1")


class VlessError(ValueError):
    """Ссылку нельзя превратить в рабочий конфиг."""


def parse_vless(url: str) -> dict:
    """Разобрать vless://uuid@host:port?параметры#имя. Бросает VlessError."""
    parts = urlsplit((url or "").strip())
    if parts.scheme != "vless":
        raise VlessError("Ссылка должна начинаться с vless://")
    if not parts.username or not parts.hostname or not parts.port:
        raise VlessError("В ссылке нет uuid, адреса сервера или порта (vless://uuid@host:port?…)")
    q = {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
    network = (q.get("type") or "tcp").lower()
    security = (q.get("security") or "none").lower()
    if network not in SUPPORTED_NETWORKS:
        raise VlessError(
            f"Транспорт type={network} пока не поддержан (есть: {', '.join(SUPPORTED_NETWORKS)}).")
    if security not in SUPPORTED_SECURITY:
        raise VlessError(
            f"Защита security={security} пока не поддержана (есть: {', '.join(SUPPORTED_SECURITY)}).")
    if network == "tcp" and (q.get("headerType") or "none") not in ("", "none"):
        raise VlessError("tcp с http-заголовками (headerType) не поддержан.")
    if security == "reality" and not q.get("pbk"):
        raise VlessError("Для security=reality в ссылке нужен параметр pbk (публичный ключ).")
    return {
        "uuid": unquote(parts.username), "host": parts.hostname, "port": parts.port,
        "network": network, "security": security, "query": q,
        "name": unquote(parts.fragment or ""),
    }


def _stream_settings(link: dict) -> dict:
    q, network, security = link["query"], link["network"], link["security"]
    stream: dict = {"network": network, "security": security}
    sni = q.get("sni") or q.get("host") or link["host"]
    if security == "tls":
        tls = {"serverName": sni, "fingerprint": q.get("fp") or "chrome"}
        if q.get("alpn"):
            tls["alpn"] = [a for a in q["alpn"].split(",") if a]
        stream["tlsSettings"] = tls
    elif security == "reality":
        stream["realitySettings"] = {
            "serverName": sni,
            "fingerprint": q.get("fp") or "chrome",
            "publicKey": q["pbk"],
            "shortId": q.get("sid", ""),
            "spiderX": unquote(q.get("spx", "")),
        }
    if network == "ws":
        ws = {"path": unquote(q.get("path") or "/")}
        if q.get("host"):
            ws["headers"] = {"Host": q["host"]}
        stream["wsSettings"] = ws
    elif network == "grpc":
        stream["grpcSettings"] = {"serviceName": unquote(q.get("serviceName") or ""),
                                  "multiMode": q.get("mode") == "multi"}
    return stream


def _outbound(url: str, tag: str) -> dict:
    link = parse_vless(url)
    user = {"id": link["uuid"], "encryption": "none"}
    if link["query"].get("flow"):
        user["flow"] = link["query"]["flow"]
    return {
        "tag": tag, "protocol": "vless",
        "settings": {"vnext": [{"address": link["host"], "port": link["port"], "users": [user]}]},
        "streamSettings": _stream_settings(link),
    }


def build_config(urls: str | list[str]) -> dict:
    """Конфиг Xray: SOCKS5-вход на 0.0.0.0:1080 (наружу порт НЕ публикуется) → VLESS-выход.

    Один ключ — один выход. Несколько — выходы proxy-0…N за балансировщиком leastPing:
    observatory раз в PROBE_INTERVAL проверяет каждый запросом к PROBE_URL. Чаще не надо:
    у всех серверов подписки один ключ, и поток проверок провайдер может счесть злоупотреблением."""
    urls = [urls] if isinstance(urls, str) else list(urls)
    if not urls:
        raise VlessError("Нет ни одного ключа vless://")
    outbounds = [_outbound(u, f"proxy-{i}") for i, u in enumerate(urls)]
    config = {
        "log": {"loglevel": "warning"},
        "inbounds": [{
            "tag": "socks-in", "listen": "0.0.0.0", "port": SOCKS_PORT, "protocol": "socks",
            "settings": {"auth": "noauth", "udp": False},
        }],
        "outbounds": outbounds,
    }
    if len(outbounds) > 1:
        config["observatory"] = {
            "subjectSelector": ["proxy-"], "probeURL": PROBE_URL,
            "probeInterval": PROBE_INTERVAL, "enableConcurrency": True,
        }
        config["routing"] = {
            "balancers": [{"tag": "auto", "selector": ["proxy-"],
                           "strategy": {"type": "leastPing"}, "fallbackTag": "proxy-0"}],
            "rules": [{"type": "field", "inboundTag": ["socks-in"], "balancerTag": "auto"}],
        }
    return config


def describe(url: str) -> str:
    """Что за ключ — без uuid и публичных ключей (безопасно печатать)."""
    link = parse_vless(url)
    return (f"{link['host']}:{link['port']} · {link['network']} · {link['security']}"
            + (f" · «{link['name']}»" if link["name"] else ""))


# ─── Подписка ─────────────────────────────────────────────────────────────────

def split_subscription(text: str) -> list[str]:
    """Тело подписки → ссылки vless://. Понимает base64 целиком и простые строки."""
    text = (text or "").strip()
    if "://" not in text.split("\n", 1)[0]:
        try:
            s = text.replace("-", "+").replace("_", "/")
            decoded = base64.b64decode(s + "=" * (-len(s) % 4)).decode("utf-8", "replace")
            if "://" in decoded:
                text = decoded
        except ValueError:
            pass  # не base64 — строки как есть
    return [ln.strip() for ln in text.splitlines() if ln.strip().lower().startswith("vless://")]


def fetch_subscription(url: str, timeout: int = 25) -> str:
    # Многие панели отдают список только «своим» клиентам — представляемся v2rayN.
    req = urllib.request.Request(url, headers={"User-Agent": "v2rayN/7.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        # Адрес подписки — секрет: в сообщение не попадает.
        raise VlessError(f"Подписка не скачалась: {getattr(exc, 'reason', exc)}") from exc


def _name_matches(name: str, parts: list[str]) -> bool:
    low = name.lower()
    return any(p in low for p in parts)


def _split_names(value: str | None) -> list[str]:
    return [p.strip().lower() for p in (value or "").split(",") if p.strip()]


def select_links(links: list[str], pick: str | None = None, skip: str | None = None
                 ) -> tuple[list[str], list[str]]:
    """(годные ссылки, причины отказа для остальных — без секретов)."""
    pick_parts, skip_parts = _split_names(pick), _split_names(skip)
    good, rejected = [], []
    for i, url in enumerate(links, 1):
        try:
            link = parse_vless(url)
        except VlessError as exc:
            rejected.append(f"#{i}: {exc}")
            continue
        label = f"#{i} «{link['name'] or link['host']}»"
        if link["host"] in PLACEHOLDER_HOSTS:
            rejected.append(f"{label}: заглушка (адрес {link['host']})")
        elif pick_parts and not _name_matches(link["name"], pick_parts):
            rejected.append(f"{label}: не подходит под VLESS_PICK")
        elif skip_parts and _name_matches(link["name"], skip_parts):
            rejected.append(f"{label}: убран VLESS_SKIP")
        else:
            good.append(url)
    return good, rejected


def resolve_links(source: str, pick: str | None = None, skip: str | None = None
                  ) -> tuple[list[str], list[str]]:
    """VLESS_URL → (ссылки для конфига, отказы). Один ключ берётся как есть, без фильтров."""
    source = (source or "").strip()
    if source.lower().startswith(("http://", "https://")):
        links = split_subscription(fetch_subscription(source))
        if not links:
            raise VlessError("В подписке нет ни одной ссылки vless://")
    elif "\n" in source or not source.lower().startswith("vless://"):
        links = split_subscription(source)
    else:
        parse_vless(source)
        return [source], []
    good, rejected = select_links(links, pick, skip)
    if not good:
        raise VlessError("Из подписки не подошёл ни один сервер:\n   " + "\n   ".join(rejected))
    return good, rejected


def _read_dotenv_value(path: str, key: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("export "):
                    line = line[len("export "):]
                name, sep, value = line.partition("=")
                if sep and name.strip() == key:
                    return value.strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def _setting(key: str) -> str | None:
    return os.getenv(key) or _read_dotenv_value(".env", key)


def main(argv: list[str]) -> int:
    source = argv[1] if len(argv) > 1 else _setting("VLESS_URL")
    if not source:
        print("Нет ключа: задайте VLESS_URL (vless://… или адрес подписки https://…) в .env "
              "или окружении либо передайте аргументом.")
        return 1
    try:
        links, rejected = resolve_links(source, _setting("VLESS_PICK"), _setting("VLESS_SKIP"))
        config = build_config(links)
        summary = [describe(u) for u in links]
    except VlessError as exc:
        print(f"❌ {exc}")
        return 1
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xray")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "config.json")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows
    print(f"✅ {path}")
    print(f"   серверов: {len(links)}" + (" (балансировщик: живой с меньшей задержкой)"
                                          if len(links) > 1 else ""))
    for line in summary:
        print(f"   · {line}")
    if rejected:
        print(f"   пропущено: {len(rejected)}")
        for line in rejected:
            print(f"   × {line}")
    print(f"   SOCKS5 внутри docker-сети: xray:{SOCKS_PORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
