# Backend (FastAPI + anime-parsers-ru, плеер Aniboom)

Нужен Python 3.10+.

```bash
cd backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export SEG_SECRET=$(python -c "import secrets; print(secrets.token_hex(32))")
uvicorn main:app --reload --port 8000
```
Переменные окружения читаются из окружения процесса (на хостинге задайте их в панели). Список — в `.env.example`.

Фронтенд: `npm install && npm run dev` — vite проксирует `/api/aniboom*` на `http://localhost:8000` (или на `BACKEND_URL`).

## Как это работает
1. Aniboom не знает `shikimori_id`: бэкенд берёт названия тайтла из Shikimori, ищет на AnimeGO и выбирает
   лучшее совпадение (учитывая номер сезона). Результат кэшируется на сутки.
   Подобрано не то аниме → `GET /api/aniboom/resolve?shikimori_id=ID` покажет выбор, исправить можно
   через `ANIMEGO_OVERRIDES`.
2. Aniboom отдаёт **DASH (MPD)**: все качества (до 1080p) лежат в одном манифесте, переключает их плеер (dash.js).
   По умолчанию плеер стартует с максимального качества.
3. CDN Aniboom проверяет Referer, который браузер подставить не может → ссылки на сегменты в MPD
   заменяются на подписанные ссылки `/api/aniboom/seg/...`, и бэкенд стримит сегменты (с поддержкой Range).
   Подпись (HMAC, `SEG_SECRET`) не даёт использовать прокси для чужих адресов.
4. Если у серии вместо MPD только HLS — отдаётся запасной `m3u8` через `/api/aniboom/proxy-m3u8`.

## Тесты
```bash
python -m unittest discover -s tests -v
```

## Прод
1. Задеплойте `backend/` (VPS / Railway / Render). Старт: `uvicorn main:app --host 0.0.0.0 --port $PORT --proxy-headers`.
   **Render:** Root Directory = `backend`, Build Command = `pip install --upgrade pip && pip install --prefer-binary -r requirements.txt`,
   переменная `PYTHON_VERSION=3.12.8` (без неё Render берёт самый новый Python, для которого нет готовых колёс → бесконечная сборка).
   Либо просто подключите репозиторий как Blueprint — настройки лежат в `render.yaml`.
2. Обязательно задайте `SEG_SECRET`.
3. В Vercel добавьте `VITE_BACKEND_URL=https://ваш-бэкенд` (https, без слэша) и пересоберите фронтенд.
4. При желании ограничьте `ALLOWED_ORIGINS=https://ваш-сайт`.

> Видео проходит через бэкенд, поэтому скорость 1080p упирается в его канал. Бесплатный Render для этого слабоват;
> лучше VPS с хорошим исходящим каналом либо `PROXY_SEGMENTS=0`, если CDN отдаёт без Referer.

Если AnimeGO/Aniboom перестал отвечать — сначала `pip install -U anime-parsers-ru`.
