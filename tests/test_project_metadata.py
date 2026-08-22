import tomllib
import unittest
from pathlib import Path


class ProjectMetadataTests(unittest.TestCase):
    def test_hvbrowser_compatibility_range_is_exact(self) -> None:
        project_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
        with project_path.open("rb") as project_file:
            project = tomllib.load(project_file)["project"]

        self.assertIn("hvbrowser>=0.9.1,<0.10", project["dependencies"])


if __name__ == "__main__":
    unittest.main()
