"""Package canonical examples without copying user runtime files."""
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        if self.target_name != "wheel":
            return
        for directory in sorted((Path(self.root) / "examples").glob("refactor*")):
            if not directory.is_dir() or directory.is_symlink():
                continue
            for file in sorted(directory.iterdir()):
                if file.is_file() and not file.is_symlink() and not file.name.startswith(".env") and (file.suffix in {".conf", ".py"} or file.name == "README.md"):
                    build_data["force_include"][str(file)] = f"src/frame/assets/examples/{directory.name}/{file.name}"
