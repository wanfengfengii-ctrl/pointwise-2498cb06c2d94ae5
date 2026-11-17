import unittest

import stitched_checks


class TestProvidedStitchedChecks(unittest.TestCase):
    """Run the repository's own smoke script as part of the regression."""

    def test_end_to_end_stitched_checks(self):
        stitched_checks.run(False)

    def test_fixtures_are_admitted_by_ordinary_v1(self):
        stitched_checks.run(True)


if __name__ == "__main__":
    unittest.main()
