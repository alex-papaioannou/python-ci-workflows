import unittest

from app import slugify


class SlugifyTests(unittest.TestCase):
    def test_spaces_collapse(self):
        self.assertEqual(slugify("  A  b "), "a-b")

    def test_empty(self):
        self.assertEqual(slugify(""), "")
