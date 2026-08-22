import tomllib
import unittest
from pathlib import Path


class ProjectMetadataTests(unittest.TestCase):
    def test_release_version_and_compatibility_ranges_are_exact(self) -> None:
        project_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
        with project_path.open("rb") as project_file:
            project = tomllib.load(project_file)["project"]

        self.assertEqual("0.11.5", project["version"])
        self.assertIn("hvbrowser>=0.9.3,<0.10", project["dependencies"])
        self.assertIn("hv-bie>=0.7.3,<0.8", project["dependencies"])
        self.assertIn(
            "ponychart-classifier>=0.12.1,<0.13",
            project["dependencies"],
        )


if __name__ == "__main__":
    unittest.main()
