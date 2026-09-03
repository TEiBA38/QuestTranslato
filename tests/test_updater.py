import unittest
from updater import parse_version_tuple, is_newer_version

class TestUpdater(unittest.TestCase):
    def test_parse_version_tuple(self):
        self.assertEqual(parse_version_tuple("v1.5.1"), (1, 5, 1))
        self.assertEqual(parse_version_tuple("1.5.2"), (1, 5, 2))
        self.assertEqual(parse_version_tuple("v2.0.0-beta"), (2, 0, 0))
        self.assertEqual(parse_version_tuple(""), (0, 0, 0))

    def test_is_newer_version(self):
        self.assertTrue(is_newer_version("v1.5.1", "v1.5.2"))
        self.assertTrue(is_newer_version("1.5.1", "1.6.0"))
        self.assertTrue(is_newer_version("v1.5.1", "v2.0.0"))
        self.assertFalse(is_newer_version("v1.5.2", "v1.5.1"))
        self.assertFalse(is_newer_version("v1.5.2", "v1.5.2"))

if __name__ == "__main__":
    unittest.main()
