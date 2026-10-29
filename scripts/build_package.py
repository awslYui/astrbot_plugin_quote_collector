"""Build a clean, self-contained AstrBot plugin ZIP using only stdlib."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    destination = root.parent / f"{root.name}.zip"
    root_files = {
        "__init__.py", "main.py", "core.py", "forwarding.py", "store.py", "web_api.py", "replica.py", "catalog.py", "exporting.py",
        "metadata.yaml", "_conf_schema.json", "README.md", "LICENSE", ".gitignore",
    }
    sources = [root / name for name in sorted(root_files)]
    sources.extend(sorted((root / "tests").glob("test_*.py")))
    sources.extend(sorted(path for path in (root / "views").rglob("*") if path.is_file()))
    sources.extend(sorted(path for path in (root / ".astrbot-plugin").rglob("*.json")))
    sources.append(Path(__file__).resolve())
    with ZipFile(destination, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for source in sources:
            archive.write(source, f"{root.name}/{source.relative_to(root).as_posix()}")
    with ZipFile(destination) as archive:
        if archive.testzip() is not None:
            raise RuntimeError("ZIP integrity check failed")
        names = archive.namelist()
        assert f"{root.name}/main.py" in names
        assert f"{root.name}/views/manager/index.html" in names
        assert f"{root.name}/views/manager/app.js" in names
        assert not any("__pycache__" in name or name.endswith((".db", ".pyc")) for name in names)
    print(f"Package: {destination}")
    print(f"Files: {len(sources)}; size: {destination.stat().st_size} bytes; integrity: OK")


if __name__ == "__main__":
    main()
