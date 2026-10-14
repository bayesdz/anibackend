"""
Чистая логика для Aniboom-бэкенда (только стандартная библиотека — легко тестировать).

  * матчинг тайтла Shikimori -> страница AnimeGO (у Aniboom свои id, по shikimori_id искать нельзя);
  * фильтр озвучек плеера aniboom из ответа AnimegoParser.get_voices();
  * переписывание MPD: сегменты идут через наш прокси /seg (подписанные ссылки, без SSRF);
  * разбор доступных качеств из MPD.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import re
from difflib import SequenceMatcher
from typing import Iterable

# ---------------------------------------------------------------- матчинг тайтлов

_ROMAN = {"ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6}
_ORD_RU = {"второй": 2, "третий": 3, "четвертый": 4, "пятый": 5, "шестой": 6}
_SEASON_PATTERNS = (
    r"(\d+) ?(?:st|nd|rd|th)? season",
    r"season (\d+)",
    r"(\d+) сезон",
    r"сезон (\d+)",
    r"(\d+) часть",
    r"часть (\d+)",
    r"part (\d+)",
)


def normalize(s: str | None) -> str:
    s = (s or "").lower().replace("ё", "е")
    s = re.sub(r"[\W_]+", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip()


def season_of(title: str | None) -> int:
    """Номер сезона из названия (1, если не указан). Нужен, чтобы 2-й сезон не склеился с 1-м."""
    t = normalize(title)
    for pat in _SEASON_PATTERNS:
        m = re.search(pat, t)
        if m:
            return int(m.group(1))
    words = t.split()
    if "сезон" in words:
        for w, n in _ORD_RU.items():
            if w in words:
                return n
    if len(words) > 1:
        if words[-1] in _ROMAN:
            return _ROMAN[words[-1]]
        if re.fullmatch(r"[2-9]", words[-1]):
            return int(words[-1])
    return 1


def score(cand_titles: Iterable[str | None], wanted: Iterable[str | None], wanted_season: int) -> float:
    cands = [c for c in cand_titles if normalize(c)]
    best = 0.0
    for c in cands:
        nc = normalize(c)
        for w in wanted:
            nw = normalize(w)
            if not nw:
                continue
            r = 1.0 if nc == nw else SequenceMatcher(None, nc, nw).ratio()
            best = max(best, r)
    if cands and max(season_of(c) for c in cands) != wanted_season:
        best -= 0.35
    return best


def pick_best(candidates: list[dict], wanted_titles: list[str], min_score: float = 0.62) -> tuple[dict, float] | None:
    """candidates — элементы AnimegoParser.search(): {id, title, original_title, ...}."""
    wanted_season = max((season_of(t) for t in wanted_titles), default=1)
    best: tuple[dict, float] | None = None
    for c in candidates:
        s = score([c.get("title"), c.get("original_title")], wanted_titles, wanted_season)
        if best is None or s > best[1]:
            best = (c, s)
    return best if best and best[1] >= min_score else None


def parse_overrides(raw: str) -> dict[int, str]:
    """'21:2239, 5114:100' -> {21: '2239', 5114: '100'} (ручная привязка shikimori_id -> animego_id)."""
    out: dict[int, str] = {}
    for part in (raw or "").split(","):
        left, _, right = part.strip().partition(":")
        if left.strip().isdigit() and right.strip().isdigit():
            out[int(left)] = right.strip()
    return out


# ---------------------------------------------------------------- озвучки

_SUB_RE = re.compile(r"субт|\bsub(?:s|titles)?\b", re.IGNORECASE)


def aniboom_voices(resp: dict | None) -> list[dict]:
    """Из ответа get_voices() оставляет только плеер aniboom: [{id:int, name, type}]."""
    out: list[dict] = []
    seen: set[str] = set()
    for v in (resp or {}).get("voices") or []:
        if "aniboom" not in str(v.get("player") or "").lower():
            continue
        tid = str(v.get("translation_id") or "").strip()
        if not tid.isdigit() or tid in seen:
            continue
        seen.add(tid)
        label = str(v.get("label") or "").strip() or "Озвучка"
        out.append({"id": int(tid), "name": label, "type": "subtitles" if _SUB_RE.search(label) else "voice"})
    return out


# ---------------------------------------------------------------- подписанные ссылки на сегменты

def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(secret: bytes, raw: bytes) -> str:
    return hmac.new(secret, raw, hashlib.sha256).hexdigest()[:20]


def encode_dir(secret: bytes, directory: str) -> str:
    raw = directory.encode()
    return f"{_sign(secret, raw)}.{_b64e(raw)}"


def decode_dir(secret: bytes, token: str) -> str | None:
    """Возвращает исходную папку, только если подпись верна (иначе None) — прокси не станет открытым."""
    try:
        sig, _, b = token.partition(".")
        raw = _b64d(b)
        if not b or not hmac.compare_digest(sig, _sign(secret, raw)):
            return None
        d = raw.decode()
    except Exception:  # noqa: BLE001
        return None
    return d if re.match(r"https?://[^/]+/", d) else None


def make_seg_url(secret: bytes, url: str, prefix: str) -> str | None:
    """
    https://cdn/x/y/seg-1.m4s?t=1  ->  {prefix}/{подпись.папка}/seg-1.m4s?t=1
    Шаблонные части ($Number$ и т.п.) остаются в «хвосте», чтобы плеер мог их подставлять.
    """
    cut = url.find("$")
    head = url if cut < 0 else url[:cut]
    i = head.rfind("/")
    if i < 8:  # короче 'https://x' — нет пути
        return None
    return f"{prefix}/{encode_dir(secret, url[: i + 1])}/{url[i + 1:]}"


# ---------------------------------------------------------------- MPD

_ATTR_RE = re.compile(r'\b(sourceURL|media|initialization|index)="([^"]*)"')
_BASE_RE = re.compile(r"(<BaseURL[^>]*>)([^<]*)(</BaseURL>)")
_REP_HEIGHT_RE = re.compile(r'<Representation\b[^>]*?\bheight="(\d+)"')


def rewrite_mpd(xml: str, secret: bytes, prefix: str) -> str:
    """Все абсолютные ссылки внутри MPD заменяет на подписанные ссылки нашего прокси."""

    def fix(raw: str) -> str | None:
        u = html.unescape(raw).strip()
        if not re.match(r"https?://", u):
            return None
        new = make_seg_url(secret, u, prefix)
        return html.escape(new, quote=True) if new else None

    def attr(m: re.Match) -> str:
        new = fix(m.group(2))
        return f'{m.group(1)}="{new}"' if new else m.group(0)

    def base(m: re.Match) -> str:
        new = fix(m.group(2))
        return f"{m.group(1)}{new}{m.group(3)}" if new else m.group(0)

    return _BASE_RE.sub(base, _ATTR_RE.sub(attr, xml))


def mpd_heights(xml: str) -> list[int]:
    """Доступные высоты кадра (по возрастанию), напр. [360, 480, 720, 1080]."""
    return sorted({int(h) for h in _REP_HEIGHT_RE.findall(xml)})
