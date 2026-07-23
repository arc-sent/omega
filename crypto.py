"""Прозрачное шифрование секретов (VK-токенов) перед записью в БД.

Цель — чтобы в дампе базы токен не лежал открытым текстом. Реальную защиту даёт
контроль доступа к серверу; это лишь дополнительный слой «на диске».

Принципы (чтобы ничего не сломать):
- Обратная совместимость: старые НЕзашифрованные токены читаются как есть.
  Зашифрованные значения помечены префиксом ``enc:`` — обычный VK-токен
  (``vk1.a.…`` или длинная hex-строка) с этого префикса начаться не может.
- Мягкая деградация: если пакет ``cryptography`` не установлен, шифрование
  выключается (токен хранится как раньше), а не роняет бота. В лог — предупреждение.
- Стабильный ключ: берётся из env ``TOKEN_KEY``; если не задан — генерируется один
  раз и хранится в ``DATA_DIR/token.key`` (та же папка, что БД, вне git). Так ключ
  переживает рестарты и настройка не требуется.
"""

import logging
import os

logger = logging.getLogger(__name__)

_PREFIX = "enc:"  # маркер зашифрованного значения

# DATA_DIR определяем так же, как в db.py (модули лежат в одной папке).
_DATA_DIR = os.getenv("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
_KEY_FILE = os.path.join(_DATA_DIR, "token.key")

try:
    from cryptography.fernet import Fernet, InvalidToken
    _HAVE_CRYPTO = True
except Exception:  # pragma: no cover - зависит от окружения
    _HAVE_CRYPTO = False
    logger.warning(
        "Пакет 'cryptography' не установлен — VK-токены хранятся без шифрования "
        "(как раньше). Установи cryptography, чтобы включить шифрование."
    )

_fernet = None  # ленивая инициализация: ключ читаем/создаём при первом обращении


def _load_or_create_key() -> bytes:
    """Вернуть ключ Fernet: из env TOKEN_KEY, иначе из файла, иначе создать файл."""
    env_key = os.getenv("TOKEN_KEY")
    if env_key:
        return env_key.encode()
    if os.path.isfile(_KEY_FILE):
        with open(_KEY_FILE, "rb") as f:
            return f.read().strip()
    key = Fernet.generate_key()
    os.makedirs(_DATA_DIR, exist_ok=True)
    with open(_KEY_FILE, "wb") as f:
        f.write(key)
    try:  # на POSIX ограничим доступ к ключу владельцем
        os.chmod(_KEY_FILE, 0o600)
    except OSError:
        pass
    logger.info("Сгенерирован новый ключ шифрования токенов: %s", _KEY_FILE)
    return key


def _get_fernet():
    global _fernet
    if _fernet is None and _HAVE_CRYPTO:
        _fernet = Fernet(_load_or_create_key())
    return _fernet


def is_encrypted(value: str | None) -> bool:
    return bool(value) and value.startswith(_PREFIX)


def encrypt(text: str | None) -> str | None:
    """Зашифровать токен для хранения. При отсутствии crypto — вернуть как есть."""
    if not text:
        return text
    f = _get_fernet()
    if f is None:
        return text  # crypto недоступен — храним как раньше
    if is_encrypted(text):
        return text  # уже зашифровано — не шифруем повторно
    return _PREFIX + f.encrypt(text.encode()).decode()


def decrypt(stored: str | None) -> str | None:
    """Расшифровать значение из БД. Старые (без префикса) — вернуть как есть."""
    if not stored or not is_encrypted(stored):
        return stored  # legacy plaintext или пусто
    f = _get_fernet()
    if f is None:
        # Значение зашифровано, а расшифровать нечем — безопаснее вернуть None
        # (бот попросит задать токен заново), чем отдать мусор в VK.
        logger.error("Токен зашифрован, но 'cryptography' недоступен — не могу расшифровать")
        return None
    try:
        return f.decrypt(stored[len(_PREFIX):].encode()).decode()
    except InvalidToken:
        logger.error("Не удалось расшифровать токен (сменился TOKEN_KEY?) — нужен повторный ввод")
        return None
