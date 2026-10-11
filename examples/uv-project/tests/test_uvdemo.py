import unittest

from uvdemo import PARSER


class LockedDependencyTests(unittest.TestCase):
    def test_locked_dependency_importable(self):
        self.assertEqual(PARSER.__name__, "IniConfig")
