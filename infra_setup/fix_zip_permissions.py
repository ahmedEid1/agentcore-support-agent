"""Marks executables in the starter toolkit's cached dependencies.zip as Unix-executable.

The toolkit zips dependencies with zipfile.write(). On Windows that records no Unix mode bits, so on the
Linux runtime Playwright's bundled Node.js driver (playwright/driver/node) is not executable and the
AgentCore Browser tool fails with PermissionError. `agentcore deploy` reuses this cached zip while
requirements.txt is unchanged and copies each entry's attributes into the deployment package, so fixing
the cache once is enough. Re-run after anything forces a dependency rebuild.
"""
import pathlib
import shutil
import stat
import sys
import zipfile

ZIP = pathlib.Path(__file__).resolve().parent.parent / "agent" / ".bedrock_agentcore" / "csai_agent" / "dependencies.zip"
UNIX = 3  # ZipInfo.create_system value for Unix, so the runtime honours the mode bits


def is_executable(name: str) -> bool:
    return name.endswith("/driver/node") or name.startswith("bin/")


def main(path=ZIP):
    fixed = path.with_suffix(".fixed.zip")
    marked = []
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(fixed, "w", zipfile.ZIP_DEFLATED) as out:
        for info in src.infolist():
            data = src.read(info)
            info.create_system = UNIX
            mode = 0o755 if is_executable(info.filename) else 0o644
            info.external_attr = ((stat.S_IFDIR if info.is_dir() else stat.S_IFREG) | mode) << 16
            if mode == 0o755:
                marked.append(info.filename)
            out.writestr(info, data)
    shutil.move(fixed, path)
    print(f"{path.name}: {len(marked)} entries marked executable, e.g. {[m for m in marked if 'node' in m] or marked[:3]}")


if __name__ == "__main__":
    main(pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else ZIP)
