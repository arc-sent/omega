FROM python:3.10-slim

# ffmpeg нужен yt-dlp для склейки видео/аудио и перекодирования в H.264
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# YouTube требует решать JS-проверки при каждом запросе: без JavaScript-рантайма
# yt-dlp отдаёт неполный список форматов (ошибка «Requested format is not
# available»). Deno — единственный рантайм, который yt-dlp включает по умолчанию.
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py db.py vk.py scheduler.py source_reader.py downloader.py \
     account_source.py parser_tiktok.py parser_youtube.py crypto.py video_processor.py metrics.py ./

ENV DATA_DIR=/app/data
VOLUME ["/app/data"]

CMD ["python", "bot.py"]
