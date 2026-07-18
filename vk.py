"""Публикация видео в VK и вспомогательные вызовы VK API.

Логика перенесена из ручного бота: video.save → загрузка файла → wall.post,
с откатом видео при ошибке публикации записи. VKError несёт код и этап, чтобы
отличать временные сбои (ретраить) от фатальных.
"""

import logging

import requests

logger = logging.getLogger(__name__)

VK_API_VERSION = "5.199"

# Коды ошибок VK, при которых имеет смысл повторить запрос.
VK_RETRYABLE_ERROR_CODES = {1, 6, 9, 10}  # неизвестная / too many / flood / internal


class VKError(RuntimeError):
    """Ошибка публикации в VK с кодом и этапом.

    network=True помечает сетевой сбой (а не ответ VK с error_code) — такие
    ошибки тоже имеет смысл повторять.
    """

    def __init__(self, code: int | None, message: str, *, stage: str | None = None, network: bool = False):
        self.code = code
        self.stage = stage
        self.network = network
        super().__init__(message)


# ─── Резолв группы по ссылке/имени ────────────────────────────────────────────

def resolve_screen_name(vk_token: str, screen_name: str) -> dict | None:
    """utils.resolveScreenName: короткое имя -> {type, object_id}. None при сбое."""
    try:
        resp = requests.get(
            "https://api.vk.com/method/utils.resolveScreenName",
            params={"access_token": vk_token, "v": VK_API_VERSION, "screen_name": screen_name},
            timeout=15,
        ).json()
    except Exception:
        logger.exception("Ошибка resolveScreenName")
        return None
    if "error" in resp:
        logger.info("resolveScreenName error: %s", resp["error"])
        return None
    return resp.get("response") or None


def fetch_group_name(vk_token: str, group_id: int) -> str | None:
    """Название группы через groups.getById. None — если не удалось."""
    try:
        resp = requests.get(
            "https://api.vk.com/method/groups.getById",
            params={"access_token": vk_token, "v": VK_API_VERSION, "group_id": group_id},
            timeout=15,
        ).json()
    except Exception:
        logger.exception("Ошибка groups.getById")
        return None
    if "error" in resp:
        logger.info("groups.getById error: %s", resp["error"])
        return None
    response = resp.get("response")
    try:
        if isinstance(response, list):
            return response[0]["name"]
        if isinstance(response, dict):
            return response["groups"][0]["name"]
    except (KeyError, IndexError, TypeError):
        pass
    return None


# ─── Публикация ───────────────────────────────────────────────────────────────

def upload_to_vk(
    vk_token: str,
    vk_group_id: int,
    file_path: str,
    title: str,
    description: str,
) -> None:
    """Загружает видео в VK и сразу публикует запись на стене группы.

    Публикует немедленно — планирование времени делается на стороне бота, а не
    через publish_date, чтобы видео не появилось в разделе «Видео» раньше времени.
    """
    group_id = abs(int(vk_group_id))
    logger.info("upload_to_vk: group_id=%s description=%r", group_id, description)

    # ── Этап 1: video.save ────────────────────────────────────────────────
    stage = "VK video.save"
    save_data = {
        "access_token": vk_token,
        "v": VK_API_VERSION,
        "group_id": group_id,
        "name": title,
        "wallpost": 0,
    }
    if description:
        save_data["description"] = description
    try:
        save_resp = requests.post(
            "https://api.vk.com/method/video.save", data=save_data, timeout=30
        ).json()
    except requests.exceptions.RequestException as exc:
        raise VKError(None, f"сетевая ошибка: {exc}", stage=stage, network=True) from exc
    logger.info("video.save response: %s", save_resp)

    if "error" in save_resp:
        e = save_resp["error"]
        raise VKError(e.get("error_code"), f"VK {e.get('error_code')}: {e.get('error_msg')}", stage=stage)

    video_id = save_resp["response"]["video_id"]
    owner_id = save_resp["response"]["owner_id"]
    upload_url = save_resp["response"]["upload_url"]

    # ── Этап 2: загрузка файла на upload-сервер ───────────────────────────
    stage = "загрузка файла в VK"
    try:
        with open(file_path, "rb") as f:
            upload_resp = requests.post(upload_url, files={"video_file": f}, timeout=300)
            upload_resp.raise_for_status()
            logger.info("video upload response: %s", upload_resp.text[:500])
    except requests.exceptions.RequestException as exc:
        raise VKError(None, f"сетевая ошибка: {exc}", stage=stage, network=True) from exc

    # ── Этап 3: wall.post ─────────────────────────────────────────────────
    stage = "VK wall.post"
    wall_params = {
        "access_token": vk_token,
        "v": VK_API_VERSION,
        "owner_id": f"-{group_id}",
        "message": description,
        "attachments": f"video{owner_id}_{video_id}",
        "from_group": 1,
    }
    try:
        wall_resp = requests.post(
            "https://api.vk.com/method/wall.post", data=wall_params, timeout=30
        ).json()
    except requests.exceptions.RequestException as exc:
        raise VKError(None, f"сетевая ошибка: {exc}", stage=stage, network=True) from exc
    logger.info("wall.post response: %s", wall_resp)

    if "error" in wall_resp:
        e = wall_resp["error"]
        # Откатываем залитое видео, чтобы оно не висело без записи.
        try:
            requests.post(
                "https://api.vk.com/method/video.delete",
                data={"access_token": vk_token, "v": VK_API_VERSION,
                      "owner_id": owner_id, "video_id": video_id},
                timeout=30,
            )
        except Exception:
            logger.exception("Не удалось откатить видео")
        raise VKError(e.get("error_code"), f"VK {e.get('error_code')}: {e.get('error_msg')}", stage=stage)
