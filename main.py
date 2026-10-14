"""
AnimeStream backend: Aniboom (плеер AnimeGO) через anime-parsers-ru >= 1.18.

Запуск:  uvicorn main:app --host 0.0.0.0 --port 8000
Эндпоинты:
  GET /api/aniboom?shikimori_id=ID
      -> { success, series_count, dubs: [{id, name, type, episodes: [1..N]}] }
  GET /api/aniboom/stream?shikimori_id=ID&translation_id=TID&episode=N
      -> { success, videos: [{type:"dash", quality:1080, qualities:[360,..,1080], url:"/api/aniboom/mpd?..."}] }
         (если у серии только HLS: type:"m3u8", url + proxy_url)
  GET /api/aniboom/mpd?shikimori_id=ID&translation_id=TID&episode=N
      -> MPD-манифест; ссылки на сегменты заменены на подписанные ссылки /api/aniboom/seg/...
  GET /api/aniboom/seg/{token}/{path}
      -> потоковый прокси сегмента (с Range) с нужными Referer/Origin
  GET /api/aniboom/proxy-m3u8?url=<m3u8>[&skip_first=N]
      -> очищенный HLS-плейлист (запасной путь)
  GET /api/aniboom/resolve?shikimori_id=ID   -> какой тайтл AnimeGO подобран (для отладки)
  GET /health

Почему так: Aniboom ищется по своему animego_id (а не по shikimori_id) и отдаёт DASH (MPD).
Один MPD содержит все качества до 1080p — переключением управляет плеер (dash.js).
"""
import os
import re
import time
import math
import asyncio
import logging
import secrets
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote, urlencode, urljoin, urlparse

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask
from anime_parsers_ru import AnimegoParser

import aniboom_core as core

log = logging.getLogger("animestream")
logging.basicConfig(level=logging.INFO)

ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
DUBS_TTL = int(os.getenv("DUBS_TTL", "21600"))       # озвучки меняются редко (6 ч)
STREAM_TTL = int(os.getenv("STREAM_TTL", "600"))     # MPD держим недолго
ID_TTL = int(os.getenv("ID_TTL", "86400"))           # соответствие shikimori_id -> animego_id (сутки)
ID_MISS_TTL = int(os.getenv("ID_MISS_TTL", "600"))   # «не нашли» помним недолго

ANIMEGO_MIRROR = os.getenv("ANIMEGO_MIRROR") or None   # зеркало animego, если основной домен недоступен
ANIMEGO_PROXY = os.getenv("ANIMEGO_PROXY") or None     # http://host:port или socks5://user:pass@host:port
SHIKIMORI_API = os.getenv("SHIKIMORI_API", "https://shikimori.one/api").rstrip("/")
ANIMEGO_OVERRIDES = core.parse_overrides(os.getenv("ANIMEGO_OVERRIDES", ""))  # "21:2239,5114:100"

# CDN Aniboom отдаёт сегменты только со «своим» Referer. Браузер подставить его не может,
# поэтому по умолчанию сегменты идут через этот бэкенд. PROXY_SEGMENTS=0 — отдавать плееру прямые
# ссылки на CDN (быстрее и дешевле по трафику, но работает, только если CDN не проверяет Referer).
PROXY_SEGMENTS = os.getenv("PROXY_SEGMENTS", "1").lower() not in ("0", "false", "no", "")
MEDIA_REFERER = os.getenv("ANIBOOM_REFERER", "https://aniboom.one/")
MEDIA_ORIGIN = os.getenv("ANIBOOM_ORIGIN") or MEDIA_REFERER.rstrip("/")
_secret_env = os.getenv("SEG_SECRET")
if not _secret_env:
    log.warning("SEG_SECRET не задан: ключ подписи ссылок на сегменты сгенерирован случайно "
                "(после перезапуска старые ссылки перестанут работать). Задайте SEG_SECRET в окружении.")
SEG_SECRET = (_secret_env or secrets.token_hex(32)).encode()

MPD_PATH = "/api/aniboom/mpd"
SEG_PREFIX = "/api/aniboom/seg"

# --- прокси HLS (запасной путь, если у серии нет MPD) ---
PROXY_PATH = "/api/aniboom/proxy-m3u8"
# Домены, которые прокси плейлистов считает «своими» (плюс домены из ссылок, которые вернула библиотека).
PROXY_ALLOWED_HOSTS = [
    h.strip().lower()
    for h in os.getenv("PROXY_ALLOWED_HOSTS", "aniboom.one,animego.me,animego.org,animego.one").split(",")
    if h.strip()
]
PREROLL_MAX_SECONDS = float(os.getenv("PREROLL_MAX_SECONDS", "120"))  # макс. длина отбрасываемого начала
PLAYLIST_TTL = int(os.getenv("PLAYLIST_TTL", "300"))
MAX_PLAYLIST_BYTES = 2 * 1024 * 1024
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# Домены CDN, которые периодически меняются. Запоминаем родительский домен из ссылок,
# которые вернула библиотека (им мы доверяем), чтобы HLS-прокси не ломался при смене CDN.
_learned_domains: set[str] = set()


def learn_domain(url: str) -> None:
    host = (urlparse(url).hostname or "").lower()
    parts = host.split(".")
    if len(parts) >= 2:
        _learned_domains.add(".".join(parts[-2:]))


_http: httpx.AsyncClient | None = None        # плейлисты (короткие запросы)
_media_http: httpx.AsyncClient | None = None  # сегменты (потоковые, долгие)


def get_http() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=10, follow_redirects=False)  # редиректы проверяем вручную
    return _http


def get_media_http() -> httpx.AsyncClient:
    global _media_http
    if _media_http is None:
        _media_http = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=30, write=10, pool=15),
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=40),
            follow_redirects=True,
        )
    return _media_http


_sync_http = httpx.Client(timeout=10, headers={"User-Agent": UA, "Accept": "application/json"})


async def _warmup() -> None:
    try:
        await asyncio.to_thread(get_parser)
        log.info("AnimeGO parser готов")
    except Exception as e:  # noqa: BLE001
        log.warning("Инициализация парсера не удалась: %s", e)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    asyncio.create_task(_warmup())
    yield
    for c in (_http, _media_http):
        if c is not None:
            await c.aclose()


app = FastAPI(title="AnimeStream Aniboom API", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET"],
    allow_headers=["*"],
    expose_headers=["Content-Length", "Content-Range", "Accept-Ranges"],
)

_parser: AnimegoParser | None = None
_parser_lock = threading.Lock()


def get_parser() -> AnimegoParser:
    global _parser
    with _parser_lock:
        if _parser is None:
            kwargs: dict[str, Any] = {}
            if ANIMEGO_MIRROR:
                kwargs["mirror"] = ANIMEGO_MIRROR
            if ANIMEGO_PROXY:
                kwargs["proxy"] = ANIMEGO_PROXY
            _parser = AnimegoParser(**kwargs)
        return _parser


# ---------- мини-кэш с TTL ----------
_cache: dict[str, tuple[float, Any]] = {}


def cache_get(key: str) -> Any:
    hit = _cache.get(key)
    return hit[1] if hit and hit[0] > time.time() else None


def cache_set(key: str, ttl: int, value: Any) -> None:
    if len(_cache) > 2000:  # защита от разрастания
        _cache.clear()
    _cache[key] = (time.time() + ttl, value)


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def cached(key: str, ttl: int, factory: Callable[[], Any]) -> Any:
    value = cache_get(key)
    if value is not None:
        return value
    with _locks_guard:
        lock = _locks.setdefault(key, threading.Lock())
    with lock:  # второй такой же запрос дождётся первого и возьмёт готовый результат из кэша
        value = cache_get(key)
        if value is None:
            value = factory()
            cache_set(key, ttl, value)
    with _locks_guard:
        _locks.pop(key, None)
    return value


# ---------- ошибки ----------
class NotFound(Exception):
    """Тайтл / озвучка / серия не найдены на Aniboom."""


def fail(e: Exception) -> HTTPException:
    if isinstance(e, HTTPException):
        return e
    name = type(e).__name__
    log.warning("Aniboom error: %s: %s", name, e)
    if isinstance(e, NotFound) or name == "NoResults":
        return HTTPException(404, str(e) if isinstance(e, NotFound) and str(e) else "Ничего не найдено")
    if name in ("ContentBlocked",):
        return HTTPException(404, "Видео заблокировано правообладателем или недоступно")
    if name == "AgeRestricted":
        return HTTPException(403, "Возрастное ограничение: для этого тайтла нужна авторизация на источнике")
    if name == "TooManyRequests":
        return HTTPException(429, "Источник ограничил частоту запросов, попробуйте через минуту")
    if name == "ServiceIsOverloaded":
        return HTTPException(503, "Источник перегружен, попробуйте позже")
    if isinstance(e, httpx.HTTPError):
        return HTTPException(502, "Не удалось связаться с источником")
    if name in ("TokenError", "ServiceError", "UnexpectedBehaviour", "UnexpectedBehavior", "DecryptionFailure"):
        return HTTPException(502, f"AnimeGO/Aniboom недоступен или изменил формат ({name}). Обновите anime-parsers-ru.")
    return HTTPException(500, f"{name}: {e}")


def _is(e: Exception, *names: str) -> bool:
    return type(e).__name__ in names


# ---------- хелперы ----------
def proxy_url(url: str, skip_first: int = 0) -> str:
    extra = f"&skip_first={skip_first}" if skip_first else ""
    return f"{PROXY_PATH}?url={quote(url, safe='')}{extra}"


# =====================================================================
#          shikimori_id -> animego_id (у Aniboom свои идентификаторы)
# =====================================================================
def shikimori_meta(shikimori_id: int) -> dict:
    """Названия и число серий из Shikimori (кэш на сутки)."""

    def load() -> dict:
        r = _sync_http.get(f"{SHIKIMORI_API}/animes/{shikimori_id}")
        if r.status_code == 404:
            raise NotFound("Аниме не найдено на Shikimori")
        r.raise_for_status()
        d = r.json()

        def flat(v: Any) -> list[str]:
            if isinstance(v, str):
                return [v]
            return [x for x in (v or []) if isinstance(x, str)]

        ordered = [*flat(d.get("russian")), *flat(d.get("name")), *flat(d.get("english")),
                   *flat(d.get("synonyms")), *flat(d.get("japanese"))]
        titles = list(dict.fromkeys(t.strip() for t in ordered if t and t.strip()))
        return {
            "titles": titles,
            "queries": titles[:3],  # русское + оригинальное + английское — этого хватает для поиска
            "kind": d.get("kind"),
            "status": d.get("status"),
            "episodes": int(d.get("episodes") or 0),
            "episodes_aired": int(d.get("episodes_aired") or 0),
        }

    return cached(f"shiki:{shikimori_id}", ID_TTL, load)


def resolve_animego(shikimori_id: int) -> dict:
    """{animego_id, title, score, source} или {animego_id: None}, если подходящего тайтла нет."""
    if shikimori_id in ANIMEGO_OVERRIDES:
        return {"animego_id": ANIMEGO_OVERRIDES[shikimori_id], "title": None, "score": 1.0, "source": "override"}

    key = f"ago:{shikimori_id}"
    hit = cache_get(key)
    if hit is not None:
        return hit

    meta = shikimori_meta(shikimori_id)
    found: dict[str, dict] = {}
    best = None
    for q in meta["queries"]:
        try:
            res = get_parser().search(query=q)
        except Exception as e:  # noqa: BLE001
            if _is(e, "NoResults"):
                continue
            raise
        for it in res or []:
            if it.get("id") is not None:
                found.setdefault(str(it["id"]), it)
        best = core.pick_best(list(found.values()), meta["titles"])
        if best and best[1] >= 0.92:  # однозначное совпадение — дальше не ищем
            break

    if best is None:
        out = {"animego_id": None, "title": None, "score": 0.0, "source": "search"}
        cache_set(key, ID_MISS_TTL, out)
        return out
    cand, sc = best
    out = {
        "animego_id": str(cand["id"]),
        "title": cand.get("title"),
        "score": round(sc, 3),
        "source": "search",
    }
    cache_set(key, ID_TTL, out)
    log.info("shikimori %s -> animego %s (%s, score %.2f)", shikimori_id, out["animego_id"], out["title"], sc)
    return out


def require_animego_id(shikimori_id: int) -> str:
    ref = resolve_animego(shikimori_id)
    if not ref.get("animego_id"):
        raise NotFound("Этого аниме нет на Aniboom")
    return ref["animego_id"]


# ---------- эндпоинты ----------
@app.get("/health")
def health():
    return {"ok": True, "proxy_segments": PROXY_SEGMENTS}


@app.get("/api/aniboom/resolve")
def resolve_debug(shikimori_id: int = Query(..., gt=0)):
    """Отладка: какой тайтл AnimeGO подобран для shikimori_id (если выбран не тот — задайте ANIMEGO_OVERRIDES)."""
    try:
        meta = shikimori_meta(shikimori_id)
        return {"shikimori_titles": meta["titles"], **resolve_animego(shikimori_id)}
    except Exception as e:
        raise fail(e)


def load_dubs(shikimori_id: int) -> dict:
    ref = resolve_animego(shikimori_id)
    if not ref.get("animego_id"):
        return {"success": True, "series_count": 0, "dubs": []}

    resp = get_parser().get_voices(anime_id=ref["animego_id"], episode=1)
    voices = core.aniboom_voices(resp)
    if not voices:
        return {"success": True, "series_count": 0, "dubs": []}

    meta = shikimori_meta(shikimori_id)
    total = int((resp or {}).get("total_episodes") or 0)
    if total <= 0:  # у онгоинга берём число вышедших серий
        total = meta["episodes_aired"] or meta["episodes"]
    is_film = meta["kind"] == "movie" or total <= 0
    episodes = [0] if is_film else list(range(1, total + 1))  # 0 = фильм
    return {
        "success": True,
        "series_count": 0 if is_film else total,
        "dubs": [{**v, "episodes": episodes} for v in voices],
    }


@app.get("/api/aniboom")
def dubs(response: Response, shikimori_id: int = Query(..., gt=0)):
    """Список озвучек Aniboom и серий."""
    try:
        res = cached(f"dubs:{shikimori_id}", DUBS_TTL, lambda: load_dubs(shikimori_id))
        if res["dubs"]:
            response.headers["Cache-Control"] = "public, max-age=300"
        else:
            cache_set(f"dubs:{shikimori_id}", ID_MISS_TTL, res)  # пустой ответ помним недолго
        return res
    except Exception as e:
        if isinstance(e, NotFound):  # нет такого аниме — не ошибка для фронта: просто пустой список
            return {"success": True, "series_count": 0, "dubs": []}
        raise fail(e)


def _fetch_text(url: str) -> str:
    r = _sync_http.get(url, headers={"Referer": MEDIA_REFERER, "Origin": MEDIA_ORIGIN}, timeout=15)
    r.raise_for_status()
    return r.text


def load_raw_stream(shikimori_id: int, translation_id: int, episode: int) -> dict:
    """Ответ библиотеки в виде {kind: 'mpd', content} | {kind: 'hls', url}."""
    animego_id = require_animego_id(shikimori_id)
    last: Exception | None = None
    for ep in ([1, 0] if episode == 0 else [episode]):  # у фильмов нумерация на AnimeGO — 1
        try:
            res = get_parser().aniboom_get_stream_for_voice(
                translation_id=str(translation_id), episode=ep, anime_id=animego_id
            )
        except Exception as e:  # noqa: BLE001
            last = e
            continue
        res = res or {}
        url = str(res.get("url") or "")
        content = res.get("content")
        if url.split("?")[0].endswith(".m3u8") or (isinstance(content, str) and content.lstrip().startswith("#EXTM3U")):
            if not url:
                last = NotFound("Пустая HLS-ссылка")
                continue
            return {"kind": "hls", "url": url}
        if not content and url:
            content = _fetch_text(url)
        if isinstance(content, str) and "<MPD" in content:
            return {"kind": "mpd", "content": content}
        last = NotFound("Источник не вернул поток для этой серии/озвучки")
    raise last or NotFound("Поток не найден")


def get_raw_stream(shikimori_id: int, translation_id: int, episode: int) -> dict:
    return cached(
        f"raw:{shikimori_id}:{translation_id}:{episode}",
        STREAM_TTL,
        lambda: load_raw_stream(shikimori_id, translation_id, episode),
    )


@app.get("/api/aniboom/stream")
def stream(
    shikimori_id: int = Query(..., gt=0),
    translation_id: int = Query(..., gt=0),
    episode: int = Query(1, ge=0),
):
    """Описание потока для выбранной озвучки и серии (MPD или запасной HLS)."""
    try:
        raw = get_raw_stream(shikimori_id, translation_id, episode)
    except Exception as e:
        raise fail(e)

    if raw["kind"] == "mpd":
        heights = core.mpd_heights(raw["content"])
        qs = urlencode({"shikimori_id": shikimori_id, "translation_id": translation_id, "episode": episode})
        video = {
            "type": "dash",
            "quality": heights[-1] if heights else 0,  # максимальное доступное (1080 для Full HD)
            "qualities": heights,
            "url": f"{MPD_PATH}?{qs}",
        }
    else:
        learn_domain(raw["url"])
        video = {"type": "m3u8", "quality": 0, "url": raw["url"], "proxy_url": proxy_url(raw["url"])}
    return {"success": True, "videos": [video]}


@app.get(MPD_PATH)
def mpd(
    shikimori_id: int = Query(..., gt=0),
    translation_id: int = Query(..., gt=0),
    episode: int = Query(1, ge=0),
):
    """MPD-манифест. Ссылки на сегменты заменены на подписанные ссылки нашего прокси."""
    try:
        raw = get_raw_stream(shikimori_id, translation_id, episode)
    except Exception as e:
        raise fail(e)
    if raw["kind"] != "mpd":
        raise HTTPException(409, "Для этой серии доступен только HLS")
    body = core.rewrite_mpd(raw["content"], SEG_SECRET, SEG_PREFIX) if PROXY_SEGMENTS else raw["content"]
    return Response(
        content=body,
        media_type="application/dash+xml",
        headers={"Cache-Control": "public, max-age=60"},
    )


_PASS_HEADERS = ("content-type", "content-length", "content-range", "accept-ranges", "etag", "last-modified")


@app.get(SEG_PREFIX + "/{token}/{rest:path}")
async def segment(token: str, rest: str, request: Request):
    """Потоковый прокси сегмента с Range. Открывает только ссылки с нашей подписью (никакого open-proxy)."""
    directory = core.decode_dir(SEG_SECRET, token)
    if not directory:
        raise HTTPException(403, "Неверная подпись ссылки")
    target = directory + rest
    if request.url.query:
        target += "?" + request.url.query

    headers = {
        "User-Agent": UA,
        "Referer": MEDIA_REFERER,
        "Origin": MEDIA_ORIGIN,
        "Accept": "*/*",
        "Accept-Encoding": "identity",  # видео не сжимаем — отдаём «как есть» без перекодирования
    }
    if request.headers.get("range"):
        headers["Range"] = request.headers["range"]

    client = get_media_http()
    try:
        upstream = await client.send(client.build_request("GET", target, headers=headers), stream=True)
    except httpx.HTTPError as e:
        log.warning("seg upstream error: %s", e)
        raise HTTPException(502, "Не удалось получить сегмент")

    if upstream.status_code >= 400:
        log.warning("seg upstream HTTP %s: %s", upstream.status_code, target.split("?")[0])
        await upstream.aclose()
        raise HTTPException(404 if upstream.status_code == 404 else 502, f"Источник ответил HTTP {upstream.status_code}")

    out = {k: v for k, v in upstream.headers.items() if k.lower() in _PASS_HEADERS}
    out["Cache-Control"] = "public, max-age=3600"
    out["Content-Encoding"] = "identity"  # чтобы GZipMiddleware не пытался сжимать видео
    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,  # 200 или 206 для Range
        headers=out,
        background=BackgroundTask(upstream.aclose),
    )


# =====================================================================
#                    HLS: очистка и проксирование
# =====================================================================
URI_ATTR = re.compile(r'URI="([^"]*)"')
PER_SEGMENT_TAGS = ("#EXT-X-BYTERANGE", "#EXT-X-PROGRAM-DATE-TIME", "#EXT-X-GAP", "#EXT-X-BITRATE")


@dataclass
class Seg:
    idx: int            # позиция в исходном плейлисте
    extinf: str         # исходная строка #EXTINF
    duration: float
    uri: str
    tags: list[str]     # теги, относящиеся к одному сегменту (BYTERANGE и т.п.)
    key: str | None     # действующий на момент сегмента #EXT-X-KEY
    map: str | None     # действующий #EXT-X-MAP
    block: int          # номер блока между DISCONTINUITY
    in_cue: bool        # сегмент внутри CUE-OUT ... CUE-IN


def _abs_attr(line: str, base: str, wrap: Callable[[str], str] | None = None) -> str:
    def sub(m: re.Match) -> str:
        u = urljoin(base, m.group(1))
        return f'URI="{wrap(u) if wrap else u}"'

    return URI_ATTR.sub(sub, line)


def _tag(line: str) -> str:
    return line.split(":", 1)[0]


def rewrite_master(lines: list[str], base: str) -> str:
    """Мастер-плейлист: варианты качества/дорожек тоже пускаем через прокси, чтобы они чистились."""
    out: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if not line.startswith("#"):
            out.append(proxy_url(urljoin(base, line)))
        elif "URI=" in line:
            is_playlist = line.startswith(("#EXT-X-MEDIA", "#EXT-X-I-FRAME-STREAM-INF"))
            out.append(_abs_attr(line, base, proxy_url if is_playlist else None))
        else:
            out.append(line)
    return "\n".join(out) + "\n"


def clean_media_playlist(lines: list[str], base: str, skip_first: int = 0) -> str:
    """
    Убирает #EXT-X-DISCONTINUITY (+ -SEQUENCE), CUE-теги и сегменты рекламных вставок
    (между CUE-OUT и CUE-IN), отрезает короткий пре-ролл до основного блока серии.
    Все URI делает абсолютными, чтобы плейлист работал с другого адреса.
    Рассчитано на VOD-плейлисты.
    """
    version = target = media_seq = ptype = None
    extras: list[str] = []
    segs: list[Seg] = []
    pending: list[str] = []
    pending_extinf: str | None = None
    key = cur_map = None
    block = 0
    in_cue = False
    endlist = False

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if not line.startswith("#"):  # URI сегмента
            if pending_extinf is None:
                continue
            try:
                dur = float(pending_extinf.split(":", 1)[1].split(",")[0])
            except (IndexError, ValueError):
                dur = 0.0
            segs.append(Seg(len(segs), pending_extinf, dur, line, pending, key, cur_map, block, in_cue))
            pending, pending_extinf = [], None
            continue

        tag = _tag(line)
        if tag == "#EXTM3U":
            continue
        elif tag == "#EXTINF":
            pending_extinf = line
        elif tag == "#EXT-X-DISCONTINUITY":
            block += 1
        elif tag == "#EXT-X-DISCONTINUITY-SEQUENCE" or tag == "#EXT-OATCLS-SCTE35":
            continue
        elif tag.startswith("#EXT-X-CUE-OUT"):
            in_cue = True
        elif tag.startswith("#EXT-X-CUE-IN"):
            in_cue = False
        elif tag.startswith("#EXT-X-CUE"):
            continue
        elif tag == "#EXT-X-KEY":
            key = line
        elif tag == "#EXT-X-MAP":
            cur_map = line
        elif tag == "#EXT-X-ENDLIST":
            endlist = True
        elif tag == "#EXT-X-VERSION":
            version = line
        elif tag == "#EXT-X-TARGETDURATION":
            target = line
        elif tag == "#EXT-X-MEDIA-SEQUENCE":
            media_seq = line
        elif tag == "#EXT-X-PLAYLIST-TYPE":
            ptype = line
        elif tag in PER_SEGMENT_TAGS:
            pending.append(line)
        else:
            extras.append(line)

    if not segs:
        raise HTTPException(502, "В плейлисте нет сегментов")

    # 1) рекламные вставки (CUE-OUT ... CUE-IN)
    kept = [s for s in segs if not s.in_cue] or segs

    # 2) пре-ролл: короткие блоки до самого длинного (основного) блока
    durations: dict[int, float] = {}
    for s in kept:
        durations[s.block] = durations.get(s.block, 0.0) + s.duration
    if len(durations) > 1:
        main = max(durations, key=lambda b: durations[b])
        lead = [b for b in durations if b < main]
        if lead and sum(durations[b] for b in lead) <= PREROLL_MAX_SECONDS:
            kept = [s for s in kept if s.block >= main]

    # 3) ручной пропуск первых N сегментов
    if 0 < skip_first < len(kept):
        kept = kept[skip_first:]

    # MEDIA-SEQUENCE сдвигаем на число отброшенных сегментов в начале
    seq0 = 0
    if media_seq:
        try:
            seq0 = int(media_seq.split(":", 1)[1])
        except (IndexError, ValueError):
            seq0 = 0
    seq = seq0 + kept[0].idx

    out = ["#EXTM3U"]
    if version:
        out.append(version)
    out.append(target or f"#EXT-X-TARGETDURATION:{math.ceil(max(s.duration for s in kept))}")
    out.append(f"#EXT-X-MEDIA-SEQUENCE:{seq}")
    if ptype:
        out.append(ptype)
    out.extend(extras)

    emitted_key = emitted_map = None
    for s in kept:
        if s.key and s.key != emitted_key:
            out.append(_abs_attr(s.key, base))
            emitted_key = s.key
        if s.map and s.map != emitted_map:
            out.append(_abs_attr(s.map, base))
            emitted_map = s.map
        out.extend(s.tags)
        out.append(s.extinf)
        out.append(urljoin(base, s.uri))
    if endlist:
        out.append("#EXT-X-ENDLIST")
    return "\n".join(out) + "\n"


def process_playlist(text: str, base: str, skip_first: int = 0) -> str:
    lines = text.splitlines()
    if any(l.startswith("#EXT-X-STREAM-INF") for l in lines):
        return rewrite_master(lines, base)
    return clean_media_playlist(lines, base, skip_first)


def check_upstream(url: str) -> None:
    try:
        u = urlparse(url)
        host = (u.hostname or "").lower()
        port = u.port
    except ValueError:
        raise HTTPException(400, "Некорректный URL")
    if u.scheme != "https" or port not in (None, 443) or u.username or u.password:
        raise HTTPException(400, "Разрешены только https-ссылки без логина и нестандартного порта")
    if not any(host == h or host.endswith("." + h) for h in (*PROXY_ALLOWED_HOSTS, *_learned_domains)):
        log.warning("proxy-m3u8: хост не разрешён: %s (добавьте в PROXY_ALLOWED_HOSTS)", host)
        raise HTTPException(403, "Этот хост не разрешён для проксирования")


async def fetch_playlist(url: str) -> tuple[str, str]:
    """Скачивает плейлист; редиректы проходим вручную, проверяя каждый хост. Возвращает (текст, итоговый URL)."""
    client = get_http()
    for _ in range(4):
        check_upstream(url)
        async with client.stream("GET", url, headers={"User-Agent": UA, "Accept": "*/*", "Referer": MEDIA_REFERER, "Origin": MEDIA_ORIGIN}) as r:
            if r.is_redirect and r.headers.get("location"):
                url = urljoin(url, r.headers["location"])
                continue
            if r.status_code != 200:
                raise HTTPException(404 if r.status_code == 404 else 502, f"Источник ответил HTTP {r.status_code}")
            buf = bytearray()
            async for chunk in r.aiter_bytes():
                buf += chunk
                if len(buf) > MAX_PLAYLIST_BYTES:
                    raise HTTPException(502, "Плейлист слишком большой")
            return buf.decode("utf-8", "replace"), url
    raise HTTPException(502, "Слишком много редиректов")


@app.get(PROXY_PATH)
async def proxy_m3u8(
    url: str = Query(..., max_length=2048),
    skip_first: int = Query(0, ge=0, le=20, description="Дополнительно пропустить N первых сегментов"),
):
    """Скачивает .m3u8 (запасной путь), чистит его и отдаёт с CORS *. Сегменты плеер грузит напрямую с CDN."""
    check_upstream(url)
    key = f"m3u8:{skip_first}:{url}"
    body = cache_get(key)
    if body is None:
        try:
            text, final_url = await fetch_playlist(url)
        except httpx.HTTPError as e:
            log.warning("proxy-m3u8 upstream error: %s", e)
            raise HTTPException(502, "Не удалось скачать плейлист")
        if not text.lstrip("\ufeff \r\n\t").startswith("#EXTM3U"):
            raise HTTPException(502, "Ответ не является HLS-плейлистом")
        body = process_playlist(text, final_url, skip_first)
        cache_set(key, PLAYLIST_TTL, body)
    return Response(
        content=body,
        media_type="application/vnd.apple.mpegurl",
        headers={
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "public, max-age=60",
        },
    )
