"""metadata.lookup with the providers stubbed out - no network."""
import unittest
from unittest import mock

from mangarr import metadata
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

