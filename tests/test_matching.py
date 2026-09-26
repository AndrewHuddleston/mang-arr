import unittest

from mangarr.matching import (
    ACCEPTED,
    AUTHOR_AGREE,
    AUTHOR_DIFFER,
    AUTHOR_UNKNOWN,
    EXACT,
    EXACT_BASE,
    NONE,
    author_level,
    match_level,
    norm,
    query_score,
)


class NormTest(unittest.TestCase):
    def test_quotes_and_punctuation(self):
        self.assertEqual(norm("Monthly Girls’ Nozaki-kun"), norm("monthly girls' nozaki kun"))
        self.assertEqual(norm("  Let's   Play!! "), "let s play")

    def test_accents(self):
        self.assertEqual(norm("Café Liégeois"), "cafe liegeois")


class MatchTest(unittest.TestCase):
    known = ["Okuremashite Seishun", "Hitting Rewind With You", "おくれまして青春"]

    def test_exact(self):
        self.assertEqual(match_level("hitting rewind with you", self.known)[0], EXACT)

    def test_disambiguator(self):
        lvl, matched = match_level("Let's Play (Mongie)", ["Let's Play"])
        self.assertEqual(lvl, EXACT_BASE)
        self.assertEqual(matched, "Let's Play")

    def test_anthology_rejected(self):
        self.assertEqual(match_level("Gekkan Shoujo Nozaki-kun Anthology", ["Gekkan Shoujo Nozaki-kun"])[0], NONE)

    def test_prefix_rejected(self):
        self.assertEqual(match_level("Let's Play a Bunch!", ["Let's Play"])[0], NONE)

    def test_accepted_levels(self):
        self.assertIn(EXACT, ACCEPTED)
        self.assertNotIn(NONE, ACCEPTED)


class AuthorTest(unittest.TestCase):
    def test_agree_any_order(self):
        self.assertEqual(author_level("Nii Satoru", ["Satoru Nii"]), AUTHOR_AGREE)

    def test_placeholder_is_unknown(self):
        self.assertEqual(author_level("Unknown", ["Godago"]), AUTHOR_UNKNOWN)
        self.assertEqual(author_level("", ["Godago"]), AUTHOR_UNKNOWN)

    def test_differ(self):
        self.assertEqual(author_level("Kira Ito", ["Godago"]), AUTHOR_DIFFER)


class QueryScoreTest(unittest.TestCase):
    def test_order(self):
        self.assertLess(query_score("Let's Play", "let's play"), query_score("Let's Play a Bunch!", "let's play"))


if __name__ == "__main__":
    unittest.main()
