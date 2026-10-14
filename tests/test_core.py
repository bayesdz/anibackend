import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import aniboom_core as core  # noqa: E402

SECRET = b"test-secret"
PREFIX = "/api/aniboom/seg"

MPD = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT24M">
 <Period>
  <AdaptationSet mimeType="video/mp4">
   <Representation id="v1" bandwidth="800000" width="640" height="360" codecs="avc1.4d401e">
    <BaseURL>https://cdn.example.com/v/123/360/</BaseURL>
    <SegmentList duration="6">
     <Initialization sourceURL="https://cdn.example.com/v/123/360/init.mp4"/>
     <SegmentURL media="https://cdn.example.com/v/123/360/seg-1.m4s?tok=a&amp;exp=9"/>
     <SegmentURL media="seg-2.m4s"/>
    </SegmentList>
   </Representation>
   <Representation id="v4" bandwidth="4000000" width="1920" height="1080">
    <SegmentTemplate initialization="https://cdn.example.com/v/123/1080/init.mp4"
        media="https://cdn.example.com/v/123/$RepresentationID$/seg-$Number$.m4s" startNumber="1"/>
   </Representation>
   <Representation id="v3" bandwidth="2500000" width="1280" height="720"/>
  </AdaptationSet>
 </Period>
</MPD>"""


class Titles(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_ok("Ёж  и-Лиса!"), "еж и лиса")

    def test_season_detection(self):
        self.assertEqual(core.season_of("Re:Zero 2nd Season"), 2)
        self.assertEqual(core.season_of("Атака титанов (второй сезон)"), 2)
        self.assertEqual(core.season_of("Жизнь в другом мире с нуля 3"), 3)
        self.assertEqual(core.season_of("Mushoku Tensei II"), 2)
        self.assertEqual(core.season_of("Naruto"), 1)
        self.assertEqual(core.season_of("Mob Psycho 100"), 1)  # 100 — не номер сезона

    def test_pick_exact_ru(self):
        cands = [
            {"id": "1", "title": "Наруто", "original_title": "Naruto"},
            {"id": "2", "title": "Наруто: Ураганные хроники", "original_title": "Naruto: Shippuuden"},
        ]
        best = core.pick_best(cands, ["Наруто", "Naruto"])
        self.assertEqual(best[0]["id"], "1")
        best = core.pick_best(cands, ["Наруто: Ураганные хроники", "Naruto: Shippuuden"])
        self.assertEqual(best[0]["id"], "2")

    def test_season_does_not_merge(self):
        cands = [
            {"id": "10", "title": "Жизнь в другом мире с нуля", "original_title": "Re:Zero kara Hajimeru Isekai Seikatsu"},
            {"id": "11", "title": "Жизнь в другом мире с нуля 2", "original_title": "Re:Zero kara Hajimeru Isekai Seikatsu 2nd Season"},
        ]
        wanted = ["Re:Zero kara Hajimeru Isekai Seikatsu 2nd Season", "Re:Zero: Жизнь с нуля (второй сезон)"]
        self.assertEqual(core.pick_best(cands, wanted)[0]["id"], "11")
        self.assertEqual(core.pick_best(cands, ["Re:Zero kara Hajimeru Isekai Seikatsu"])[0]["id"], "10")

    def test_no_match_returns_none(self):
        cands = [{"id": "1", "title": "Совсем другое аниме", "original_title": "Totally Different"}]
        self.assertIsNone(core.pick_best(cands, ["Bleach", "Блич"]))

    def test_overrides(self):
        self.assertEqual(core.parse_overrides("21:2239, 5114:100, bad, x:1"), {21: "2239", 5114: "100"})
        self.assertEqual(core.parse_overrides(""), {})


def normalize_ok(s):
    return core.normalize(s)


class Voices(unittest.TestCase):
    def test_filters_aniboom_only(self):
        resp = {
            "voices": [
                {"label": "AniLibria", "translation_id": "10", "player": "aniboom"},
                {"label": "Субтитры", "translation_id": "11", "player": "Aniboom"},
                {"label": "Kodik voice", "translation_id": "12", "player": "kodik"},
                {"label": "dup", "translation_id": "10", "player": "aniboom"},
                {"label": "bad id", "translation_id": "abc", "player": "aniboom"},
                {"label": "", "translation_id": "13", "player": "aniboom"},
            ]
        }
        out = core.aniboom_voices(resp)
        self.assertEqual([v["id"] for v in out], [10, 11, 13])
        self.assertEqual(out[1]["type"], "subtitles")
        self.assertEqual(out[0]["type"], "voice")
        self.assertEqual(out[2]["name"], "Озвучка")

    def test_empty(self):
        self.assertEqual(core.aniboom_voices(None), [])
        self.assertEqual(core.aniboom_voices({"voices": None}), [])


class Segments(unittest.TestCase):
    def test_roundtrip(self):
        u = "https://cdn.example.com/a/b/seg-1.m4s?x=1"
        out = core.make_seg_url(SECRET, u, PREFIX)
        self.assertTrue(out.startswith(PREFIX + "/"))
        self.assertTrue(out.endswith("/seg-1.m4s?x=1"))
        token = out[len(PREFIX) + 1 :].split("/")[0]
        self.assertEqual(core.decode_dir(SECRET, token), "https://cdn.example.com/a/b/")

    def test_bad_signature_rejected(self):
        out = core.make_seg_url(SECRET, "https://cdn.example.com/a/s.m4s", PREFIX)
        token = out[len(PREFIX) + 1 :].split("/")[0]
        self.assertIsNone(core.decode_dir(b"other-secret", token))
        sig, _, b = token.partition(".")
        self.assertIsNone(core.decode_dir(SECRET, "0" * len(sig) + "." + b))
        self.assertIsNone(core.decode_dir(SECRET, "garbage"))
        self.assertIsNone(core.decode_dir(SECRET, ""))

    def test_forged_non_http_dir_rejected(self):
        forged = core.encode_dir(SECRET, "file:///etc/")  # даже с верной подписью схема должна быть http(s)
        self.assertIsNone(core.decode_dir(SECRET, forged))

    def test_template_vars_stay_in_tail(self):
        u = "https://cdn.example.com/v/123/$RepresentationID$/seg-$Number$.m4s"
        out = core.make_seg_url(SECRET, u, PREFIX)
        self.assertTrue(out.endswith("/$RepresentationID$/seg-$Number$.m4s"))
        token = out[len(PREFIX) + 1 :].split("/")[0]
        self.assertEqual(core.decode_dir(SECRET, token), "https://cdn.example.com/v/123/")

    def test_no_path_returns_none(self):
        self.assertIsNone(core.make_seg_url(SECRET, "https://cdn.example.com", PREFIX))


class Mpd(unittest.TestCase):
    def test_heights(self):
        self.assertEqual(core.mpd_heights(MPD), [360, 720, 1080])
        self.assertEqual(core.mpd_heights("<MPD/>"), [])

    def test_rewrite(self):
        out = core.rewrite_mpd(MPD, SECRET, PREFIX)
        self.assertNotIn("https://cdn.example.com", out)  # наружу прямых ссылок на CDN нет
        self.assertIn("seg-$Number$.m4s", out)             # шаблоны сохранены
        self.assertIn('media="seg-2.m4s"', out)            # относительные не трогаем
        self.assertIn("&amp;exp=9", out)                   # & в атрибуте экранирован
        self.assertEqual(out.count(PREFIX + "/"), 5)       # BaseURL + init + 2 media + template init/media... см. ниже
        self.assertIn("<BaseURL>" + PREFIX + "/", out)
        self.assertTrue(out.split("<BaseURL>")[1].split("</BaseURL>")[0].endswith("/"))
        self.assertEqual(core.mpd_heights(out), [360, 720, 1080])

    def test_rewrite_is_deterministic(self):
        self.assertEqual(core.rewrite_mpd(MPD, SECRET, PREFIX), core.rewrite_mpd(MPD, SECRET, PREFIX))


if __name__ == "__main__":
    unittest.main()
