from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def main() -> None:
    icon = ROOT / "res" / "app.ico"
    png = ROOT / "res" / "app.png"
    if not icon.is_file() or not png.is_file():
        raise FileNotFoundError("Expected res\\app.ico and res\\app.png beside this script.")

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name",
        "sm64ltlvl",
        "--icon",
        str(icon),
        "--add-data",
        f"{png};res",
        "--distpath",
        str(ROOT),
        "--workpath",
        str(ROOT / "build"),
        "--specpath",
        str(ROOT / "build"),
        str(ROOT / "convert_levels.py"),
    ]
    subprocess.run(command, cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
