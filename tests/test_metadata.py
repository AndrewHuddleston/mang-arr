"""metadata.lookup with the providers stubbed out - no network."""
import os
import tempfile
import unittest
from unittest import mock

from mangarr import core, metadata
from mangarr.matching import disambiguator, norm
from mangarr.model import Series


def S(**kw):
    base = dict(anilist_id=None, mangadex_id=None, format="MANGA", popularity=100)
    base.update(kw)
    return Series(**base)


WB_KR = S(anilist_id=86099, english="Wind Breaker", country="KR", authors=["Jo Yongseok"], popularity=5000)
WB_JP = S(anilist_id=135083, romaji="WIND BREAKER", english="Wind Breaker", country="JP",
          authors=["Satoru Nii", "にいさとる"], popularity=6000)
APOTH_MANGA = S(anilist_id=99022, english="The Apothecary Diaries", romaji="Kusuriya no Hitorigoto", popularity=9000)
APOTH_NOVEL = S(anilist_id=99026, english="The Apothecary Diaries", format="NOVEL", popularity=8000)
SAINT = S(anilist_id=192094, english="The Abandoned Saintess' Vow: To Deny the Prince's Love in Her Next Life")
CAT = S(anilist_id=121647, english="Stray Cat & Wolf", romaji="Noraneko to Ookami")


class NormTest(unittest.TestCase):
    def test_folder_sanitising(self):
        self.assertEqual(norm("The Abandoned Saintess' Vow_ To Deny"), norm("The Abandoned Saintess' Vow: To Deny"))
        self.assertEqual(norm("Villainess_"), norm("Villainess?"))

    def test_ampersand(self):
        self.assertEqual(norm("Stray Cat & Wolf"), norm("Stray Cat and Wolf"))

    def test_disambiguator(self):
        self.assertEqual(disambiguator("Wind Breaker (NII Satoru)"), "NII Satoru")
        self.assertIsNone(disambiguator("Wind Breaker"))


class LookupTest(unittest.TestCase):
    def run_lookup(self, query, anilist_hits, mangadex_hits=()):
        with mock.patch.object(metadata.anilist, "search", lambda q, limit=8: list(anilist_hits)), \
             mock.patch.object(metadata.mangadex, "search", lambda q, limit=8: list(mangadex_hits)):
            return metadata.lookup(query)

    def test_same_title_is_ambiguous(self):
        pick, cands = self.run_lookup("Wind Breaker", [WB_KR, WB_JP])
        self.assertIsNone(pick)
        self.assertEqual(len(cands), 2)

    def test_author_disambiguator_resolves(self):
        pick, _ = self.run_lookup("Wind Breaker (NII Satoru)", [WB_KR, WB_JP])
        self.assertIs(pick, WB_JP)

    def test_novel_excluded(self):
        pick, _ = self.run_lookup("The Apothecary Diaries", [APOTH_MANGA, APOTH_NOVEL])
        self.assertIs(pick, APOTH_MANGA)

    def test_underscore_folder_name(self):
        pick, _ = self.run_lookup("The Abandoned Saintess' Vow_ To Deny the Prince's Love in Her Next Life", [SAINT])
        self.assertIs(pick, SAINT)

    def test_ampersand(self):
        pick, _ = self.run_lookup("Stray Cat and Wolf", [CAT])
        self.assertIs(pick, CAT)

    def test_mangadex_fallback(self):
        md = S(mangadex_id="abc", english="Let's Play", country="US")
        pick, _ = self.run_lookup("Let's Play", [S(anilist_id=1, english="Let's Play a Bunch!")], [md])
        self.assertIs(pick, md)

    def test_no_match(self):
        pick, cands = self.run_lookup("Nothing Like This", [S(anilist_id=1, english="Something Else")])
        self.assertIsNone(pick)
        self.assertEqual(len(cands), 1)


ITS_MINE = S(anilist_id=118601, romaji="It's Mine", english="It's Mine", native="이츠마인", country="KR",
             popularity=6369)
ITS_MINE_MD = S(mangadex_id="0c1e7a57-4bd4-4c0e-9d5c-6a0f3f7e8b21", english="It’s Mine", country="KR")


class TypographicQuotesTest(unittest.TestCase):
    """The live library adopted the Suwayomi folder "It’s Mine" as a MangaDex series while "It's Mine"
    (anilist:118601) was tracked: AniList's search finds nothing like it for the curly apostrophe (checked by
    hand), so MangaDex's was the one exact hit. The query is now searched with plain quotes."""

    @staticmethod
    def anilist_search(q, limit=8):          # as AniList answers: its series only for the plain apostrophe
        return [ITS_MINE] if "It's Mine" in q else []

    def providers(self):
        return (mock.patch.object(metadata.anilist, "search", self.anilist_search),
                mock.patch.object(metadata.mangadex, "search", lambda q, limit=8: [ITS_MINE_MD]))

    def test_lookup_finds_the_anilist_series(self):
        for folder in ("It’s Mine", "Itʼs Mine", "It‛s Mine", "It＇s Mine", "It´s Mine", "It`s Mine"):
            al, md = self.providers()
            with al, md:
                pick, cands = metadata.lookup(folder)
            self.assertIs(pick, ITS_MINE, folder)

    def test_adopt_scan_finds_the_anilist_series(self):
        class NoEntries:
            def mangas_page(self, offset, first):
                return [], False
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "Weeb Central", "It’s Mine"))
            al, md = self.providers()
            with al, md, mock.patch("mangarr.config.STAGING_ROOT", tmp):
                items = core.plan_adopt(NoEntries())
        self.assertEqual([(i.folder_name, i.series.ref) for i in items], [("It’s Mine", "anilist:118601")])


if __name__ == "__main__":
    unittest.main()


class ProviderFailureTest(unittest.TestCase):
    """Round 3 (medium): a provider failure was swallowed, so "nobody answered" looked like "nobody knows
    this title" to the caller."""

    @staticmethod
    def down(q, limit=8):
        raise OSError("network down")

    def test_nobody_answering_is_an_error(self):
        with mock.patch.object(metadata.anilist, "search", self.down), \
                mock.patch.object(metadata.mangadex, "search", self.down), self.assertLogs("mangarr.metadata"), \
                self.assertRaises(metadata.LookupError_) as cm:
            metadata.lookup("One Piece")
        self.assertEqual(str(cm.exception), "AniList and MangaDex could not be reached")

    def test_one_provider_down(self):
        md = [S(mangadex_id="u1", english="Berserk of Gluttony")]
        with mock.patch.object(metadata.anilist, "search", self.down), \
                mock.patch.object(metadata.mangadex, "search", lambda q, limit=8: list(md)), \
                self.assertLogs("mangarr.metadata"):
            self.assertEqual(metadata.lookup("Berserk"), (None, md))          # the Add page still shows these
            unreached = []
            self.assertEqual(metadata.lookup("Berserk", unreached=unreached), (None, md))
            self.assertEqual(unreached, ["AniList"])                           # ... but AniList was not asked
            unreached = []
            self.assertEqual(metadata.lookup("Berserk of Gluttony", unreached=unreached)[0], md[0])
            self.assertEqual(unreached, [])                                    # a pick is a pick

