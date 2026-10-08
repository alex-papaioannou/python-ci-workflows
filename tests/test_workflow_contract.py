"""A fixture exercised on each supported Python runtime by the shared workflow."""
import csv
import io
import unittest


class RuntimeSmokeTest(unittest.TestCase):
    def test_stdlib_csv_handles_quoted_delimiters(self):
        self.assertEqual(list(csv.reader(io.StringIO('"a,b",c\n'))), [["a,b", "c"]])
