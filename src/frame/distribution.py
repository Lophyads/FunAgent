"""Access to configuration examples shipped with the installed package."""
from pathlib import Path
from importlib.resources import files
import argparse
import shutil
import sys
from types import MappingProxyType

from .contracts import DomainError


def _examples():
    resource = files("src.frame").joinpath("assets/examples")
    if resource.is_dir():
        return resource
    checkout = Path(__file__).resolve().parents[2]
    if (checkout / "pyproject.toml").is_file() and (checkout / "examples").is_dir():
        return checkout / "examples"
    raise DomainError("RESOURCE_MISSING", "Packaged examples are unavailable")


def AvailableExamples() -> tuple[str, ...]:
    return tuple(sorted(item.name for item in _examples().iterdir()
                        if item.name.startswith("refactor") and item.is_dir()))


def ExampleFiles(name: str):
    if name not in AvailableExamples():
        raise DomainError("INVALID_ARGUMENT", "Unknown configuration example")
    resource = _examples().joinpath(name)
    return MappingProxyType({item.name: item.read_bytes() for item in resource.iterdir()
                             if item.is_file() and not item.name.startswith(".env")
                             and (Path(item.name).suffix in {".conf", ".py"} or item.name == "README.md")})


def CopyExample(name: str, directory: str | Path) -> Path:
    contents = ExampleFiles(name)
    target = Path(directory).absolute()
    try:
        target.mkdir()
    except FileExistsError:
        raise DomainError("CONFLICT", "Example destination already exists") from None
    except OSError:
        raise DomainError("RESOURCE_WRITE", "Cannot create example destination") from None
    try:
        for filename, content in contents.items():
            (target / filename).write_bytes(content)
    except BaseException:
        shutil.rmtree(target)
        raise
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", nargs="?", help="Example name, such as refactor-cli")
    parser.add_argument("directory", nargs="?", help="New destination directory")
    parser.add_argument("--list", action="store_true", help="List packaged examples")
    args = parser.parse_args(argv)
    try:
        if args.list:
            print("\n".join(AvailableExamples()))
        elif args.name and args.directory:
            print(CopyExample(args.name, args.directory))
        else:
            parser.error("Supply an example and a new destination, or --list")
    except DomainError as error:
        print(f"{error.code}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
