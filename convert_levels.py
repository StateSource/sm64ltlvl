from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


SCRIPT_DIR = Path(__file__).resolve().parent
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else SCRIPT_DIR
RESOURCE_DIR = (
    Path(getattr(sys, "_MEIPASS")) / "res"
    if getattr(sys, "frozen", False)
    else SCRIPT_DIR / "res"
)
ICON_PNG = RESOURCE_DIR / "app.png"
MODULE_DIR = APP_DIR / "module"
DEFAULT_MOD_DIR = APP_DIR.parent
DEFAULT_DECOMP_LEVELS = Path(r"D:\1\SM64 decomp\HackerSM64\levels")
CONFIG_PATH = APP_DIR / "config.json"
LEVEL_ENTRY = re.compile(r"\b(level_[A-Za-z0-9_]+_entry)\s*\[")
LVL_HEADER = b"DYNOSBIN"
COMPILE_TIMEOUT_SECONDS = 300
ALL_LEVELS = "All levels"
CASTLE_FLOWING_WATER = re.compile(
    r"(COL_TRI_INIT\s*\(\s*SURFACE_FLOWING_WATER\s*,\s*\d+\s*\)\s*,)"
    r"(.*?)"
    r"(?=COL_TRI_INIT\s*\(|COL_TRI_STOP\s*\(|COL_END\s*\()",
    re.DOTALL,
)
COL_TRIANGLE = re.compile(r"\bCOL_TRI\s*\(([^()]*)\)")
FLOOMBA_MACRO = re.compile(
    r"^[ \t]*MACRO_OBJECT_WITH_BEH_PARAM\s*\(\s*"
    r"macro_floomba_triplet_spawner\b[^\r\n]*(?:\r?\n|$)",
    re.MULTILINE,
)


class ConversionError(Exception):
    pass


def default_settings() -> dict[str, str]:
    source_path = DEFAULT_DECOMP_LEVELS
    selected = "castle_grounds"
    if not source_path.is_dir():
        source_path = DEFAULT_MOD_DIR / "levels"
        selected = ALL_LEVELS
    return {
        "coopdx_executable": str(MODULE_DIR / "sm64coopdx.exe"),
        "baserom_file": str(
            Path.home() / "AppData" / "Roaming" / "sm64coopdx" / "baserom.us.z64"
        ),
        "mod_folder": str(DEFAULT_MOD_DIR),
        "source_levels_folder": str(source_path),
        "selected_level": selected,
    }


def load_settings() -> dict[str, str]:
    defaults = default_settings()
    if not CONFIG_PATH.exists():
        return defaults
    try:
        settings = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConversionError(f"Could not read settings file:\n{CONFIG_PATH}\n{exc}") from exc
    if not isinstance(settings, dict):
        raise ConversionError(f"Settings must be a JSON object:\n{CONFIG_PATH}")
    for key in defaults:
        value = settings.get(key, defaults[key])
        if not isinstance(value, str):
            raise ConversionError(f"Setting {key!r} must be a string in:\n{CONFIG_PATH}")
        defaults[key] = value
    return defaults


def save_settings(settings: dict[str, str]) -> None:
    temporary = CONFIG_PATH.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(settings, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, CONFIG_PATH)


def source_level_dirs(source_root: Path) -> list[Path]:
    if not source_root.is_dir():
        raise ConversionError(f"Source folder not found:\n{source_root}")
    if (source_root / "script.c").is_file():
        return [source_root]
    return sorted(
        child
        for child in source_root.iterdir()
        if child.is_dir() and (child / "script.c").is_file()
    )


def selected_source_dirs(source_root: Path, selected_level: str) -> list[Path]:
    available = source_level_dirs(source_root)
    if selected_level == ALL_LEVELS:
        selected = available
    else:
        selected = [level for level in available if level.name == selected_level]
    if not selected:
        raise ConversionError(
            f"No source level named {selected_level!r} was found in:\n{source_root}"
        )
    return selected


def expected_level_files(level_dirs: list[Path]) -> list[str]:
    outputs: set[str] = set()

    for level_dir in level_dirs:
        script = level_dir / "script.c"
        try:
            source = script.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ConversionError(f"Could not read {script}:\n{exc}") from exc
        outputs.update(f"{entry}.lvl" for entry in LEVEL_ENTRY.findall(source))

    if not outputs:
        raise ConversionError(
            "No level entry scripts were found in:\n"
            + "\n".join(str(level_dir) for level_dir in level_dirs)
        )
    return sorted(outputs)


def valid_lvl(path: Path) -> bool:
    try:
        with path.open("rb") as file:
            return file.read(len(LVL_HEADER)) == LVL_HEADER and path.stat().st_size > 16
    except OSError:
        return False


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_outputs(
    levels_dir: Path, backup_dir: Path, names: set[str]
) -> dict[str, str]:
    backup_dir.mkdir(parents=True)
    saved: dict[str, str] = {}
    for name in names:
        output = levels_dir / name
        if not output.is_file():
            continue
        backup = backup_dir / output.name
        shutil.copy2(output, backup)
        saved[output.name] = file_digest(backup)
    return saved


def restore_outputs(
    levels_dir: Path,
    backup_dir: Path,
    names: set[str],
    saved: dict[str, str],
) -> None:
    for name in names:
        output = levels_dir / name
        if name not in saved:
            if output.exists():
                output.unlink()
            continue
        backup = backup_dir / output.name
        if not output.is_file() or file_digest(output) != saved[output.name]:
            shutil.copy2(backup, output)

    for name in saved:
        output = levels_dir / name
        if not output.exists():
            shutil.copy2(backup_dir / name, output)


def stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def running_coopdx_processes() -> list[str]:
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq sm64coopdx.exe", "/FO", "CSV", "/NH"],
            check=True,
            capture_output=True,
            text=True,
            encoding="mbcs",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ConversionError(f"Could not check for running CoopDX processes:\n{exc}") from exc

    processes: list[str] = []
    for row in csv.reader(result.stdout.splitlines()):
        if len(row) >= 2 and row[0].lower() == "sm64coopdx.exe":
            processes.append(row[1])
    return processes


def outputs_are_fresh(levels_dir: Path, expected: list[str], started_ns: int) -> bool:
    for name in expected:
        output = levels_dir / name
        if not valid_lvl(output):
            return False
        try:
            if output.stat().st_mtime_ns < started_ns:
                return False
        except OSError:
            return False
    return True


def adapt_castle_grounds_for_dynos(level_dir: Path, log_line: Callable[[str], None]) -> None:
    script = level_dir / "script.c"
    collision = level_dir / "area_1" / "collision.inc.c"
    macro = level_dir / "area_1" / "macro.inc.c"
    if not script.is_file() or not collision.is_file() or not macro.is_file():
        return
    if "TERRAIN(castle_grounds_area_1_collision)" not in script.read_text(
        encoding="utf-8", errors="replace"
    ):
        return

    collision_text = collision.read_text(encoding="utf-8", errors="replace")
    converted_count = 0

    def convert_water_block(match: re.Match[str]) -> str:
        nonlocal converted_count
        triangles, converted_count = COL_TRIANGLE.subn(
            r"COL_TRI_SPECIAL(\1, 0x0)", match.group(2)
        )
        return match.group(1) + triangles

    adapted_collision, block_count = CASTLE_FLOWING_WATER.subn(
        convert_water_block, collision_text
    )
    if block_count and converted_count:
        collision.write_text(adapted_collision, encoding="utf-8")
        log_line(
            f"Adapted {converted_count} flowing-water triangles for CoopDX."
        )

    macro_text = macro.read_text(encoding="utf-8", errors="replace")
    adapted_macro, removed_count = FLOOMBA_MACRO.subn("", macro_text)
    if removed_count:
        macro.write_text(adapted_macro, encoding="utf-8")
        log_line(
            "Omitted the unsupported macro_floomba_triplet_spawner from the "
            "temporary compile copy."
        )

    for relative_path in (
        Path("areas") / "1" / "collision.inc.c",
        Path("areas") / "1" / "7" / "collision.inc.c",
        Path("areas") / "1" / "8" / "collision.inc.c",
    ):
        unused_collision = level_dir / relative_path
        if unused_collision.is_file():
            unused_collision.unlink()
            log_line(f"Skipped duplicate legacy-unused collision: {relative_path}")


def compiler_parse_errors(log_path: Path) -> str | None:
    try:
        contents = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    if "error(s): unable to parse data" not in contents.lower():
        return None
    errors = [
        line.strip()
        for line in contents.splitlines()
        if "ERROR:" in line or "error(s): Unable to parse data" in line
    ]
    return "\n".join(errors[-12:]) or "CoopDX could not parse the level data."


def run_compiler(
    executable: Path,
    mod_name: str,
    levels_dir: Path,
    expected: list[str],
    save_path: Path,
    log_path: Path,
    log_line: Callable[[str], None],
    status: Callable[[str], None],
    cancel: threading.Event,
) -> None:
    running_processes = running_coopdx_processes()
    if running_processes:
        raise ConversionError(
            "Close CoopDX before compiling. Running process ID(s): "
            + ", ".join(running_processes)
        )

    command = [
        str(executable),
        "--console",
        "--disable-mods",
        "--enable-mod",
        mod_name,
        "--savepath",
        str(save_path),
    ]
    log_line("Running: " + subprocess.list2cmdline(command))

    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    started = time.monotonic()
    started_ns = time.time_ns()
    with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
        process = subprocess.Popen(
            command,
            cwd=executable.parent,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=creation_flags,
        )
        try:
            with log_path.open("r", encoding="utf-8", errors="replace") as reader:
                previous_signature: tuple[tuple[int, int], ...] | None = None
                stable_since: float | None = None

                while True:
                    for line in reader:
                        log_line(line.rstrip())

                    if cancel.is_set():
                        raise ConversionError("Conversion cancelled.")

                    parse_errors = compiler_parse_errors(log_path)
                    if parse_errors:
                        raise ConversionError(
                            "CoopDX could not parse the selected level:\n"
                            f"{parse_errors}\n\nFull log: {log_path}"
                        )

                    if outputs_are_fresh(levels_dir, expected, started_ns):
                        outputs = [levels_dir / name for name in expected]
                        signature = tuple(
                            (path.stat().st_size, path.stat().st_mtime_ns)
                            for path in outputs
                        )
                        now = time.monotonic()
                        if signature == previous_signature:
                            if stable_since is not None and now - stable_since >= 2:
                                status("Levels compiled; stopping CoopDX...")
                                stop_process(process)
                                break
                        else:
                            previous_signature = signature
                            stable_since = now

                    return_code = process.poll()
                    if return_code is not None:
                        if return_code != 0:
                            raise ConversionError(
                                f"CoopDX exited with code {return_code}. "
                                f"See the log:\n{log_path}"
                            )
                        if not outputs_are_fresh(levels_dir, expected, started_ns):
                            raise ConversionError(
                                "CoopDX exited before generating fresh LVL files. "
                                f"See the log:\n{log_path}"
                            )
                        break
                    if time.monotonic() - started > COMPILE_TIMEOUT_SECONDS:
                        raise ConversionError(
                            "CoopDX did not finish compiling within five minutes. "
                            f"See the log:\n{log_path}"
                        )
                    status("Compiling level sources...")
                    time.sleep(0.2)

                for line in reader:
                    log_line(line.rstrip())
        finally:
            stop_process(process)


def convert_levels(
    mod_dir: Path,
    source_root: Path,
    selected_level: str,
    executable: Path,
    baserom_file: Path,
    log_line: Callable[[str], None],
    status: Callable[[str], None],
    cancel: threading.Event,
) -> list[Path]:
    mod_dir = mod_dir.expanduser().resolve()
    source_root = source_root.expanduser().resolve()
    executable = executable.expanduser().resolve()
    baserom_file = baserom_file.expanduser().resolve()
    levels_dir = mod_dir / "levels"

    if not mod_dir.is_dir():
        raise ConversionError(f"Mod folder not found:\n{mod_dir}")
    if not executable.is_file():
        raise ConversionError(f"CoopDX executable not found:\n{executable}")
    if not baserom_file.is_file():
        raise ConversionError(f"Baserom file not found:\n{baserom_file}")
    running_processes = running_coopdx_processes()
    if running_processes:
        raise ConversionError(
            "Close CoopDX before generating levels. Running process ID(s): "
            + ", ".join(running_processes)
        )
    if mod_dir.parent.name.lower() != "mods":
        raise ConversionError(
            "Choose a mod folder located directly inside CoopDX's mods folder."
        )

    selected_sources = selected_source_dirs(source_root, selected_level)
    expected = expected_level_files(selected_sources)
    log_path = APP_DIR / "last_compile.log"
    expected_names = set(expected)

    with tempfile.TemporaryDirectory(prefix="sm64-level-convert-") as temp_name:
        temp_dir = Path(temp_name)
        previous_levels = temp_dir / "previous-levels"
        saved = snapshot_outputs(levels_dir, previous_levels, expected_names)
        previous_sources = temp_dir / "previous-sources"
        previous_sources.mkdir()
        staged_sources = temp_dir / "staged-sources"
        staged_sources.mkdir()
        source_existed: dict[str, bool] = {}
        try:
            status(f"Preparing {len(expected)} level source(s)...")
            for source in selected_sources:
                destination = levels_dir / source.name
                staged_source = staged_sources / source.name
                shutil.copytree(source, staged_source)
                source_existed[source.name] = destination.exists()
                if destination.exists():
                    shutil.copytree(destination, previous_sources / source.name)
                    shutil.rmtree(destination)
                shutil.copytree(staged_source, destination)
                if source.name == "castle_grounds":
                    adapt_castle_grounds_for_dynos(destination, log_line)
            for name in expected:
                output = levels_dir / name
                if output.exists():
                    output.unlink()
            run_compiler(
                executable,
                mod_dir.name,
                levels_dir,
                expected,
                baserom_file.parent,
                log_path,
                log_line,
                status,
                cancel,
            )

            missing = [
                name
                for name in expected
                if not valid_lvl(levels_dir / name)
            ]
            if missing:
                raise ConversionError(
                    "CoopDX did not produce valid LVL files for:\n"
                    + "\n".join(missing)
                    + f"\n\nCompiler log:\n{log_path}"
                )

            for name, existed in source_existed.items():
                destination = levels_dir / name
                shutil.rmtree(destination)
                if existed:
                    shutil.copytree(previous_sources / name, destination)
        except Exception:
            for name, existed in source_existed.items():
                destination = levels_dir / name
                if destination.exists():
                    shutil.rmtree(destination)
                if existed:
                    shutil.copytree(previous_sources / name, destination)
            restore_outputs(levels_dir, previous_levels, expected_names, saved)
            raise

    return [levels_dir / name for name in expected]


class ConverterApp:
    def __init__(self, root: tk.Tk, settings: dict[str, str]) -> None:
        self.root = root
        self.root.title("SM64 level to .lvl")
        self.root.geometry("560x700")
        self.root.minsize(500, 600)
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.pending_conversion: tuple[Path, Path, str, Path, Path] | None = None

        self.mod_dir = tk.StringVar(value=settings["mod_folder"])
        self.source_root = tk.StringVar(value=settings["source_levels_folder"])
        self.selected_level = tk.StringVar(value=settings["selected_level"])
        self.executable = tk.StringVar(value=settings["coopdx_executable"])
        self.baserom_file = tk.StringVar(value=settings["baserom_file"])
        self.status_text = tk.StringVar(value="Ready")

        self.apply_icon()

        frame = ttk.Frame(root, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(3, weight=1)

        paths = ttk.LabelFrame(frame, text="Paths and tools", padding=(10, 6))
        paths.grid(row=0, column=0, columnspan=3, sticky="ew")
        paths.columnconfigure(1, weight=1)
        path_rows = (
            ("Source levels", self.source_root, self.browse_source),
            ("Destination mod", self.mod_dir, self.browse_mod),
            ("CoopDX app", self.executable, self.browse_executable),
            ("SM64 baserom", self.baserom_file, self.browse_baserom),
        )
        for row, (label, variable, browse) in enumerate(path_rows):
            ttk.Label(paths, text=label).grid(
                row=row, column=0, sticky="w", padx=(0, 10), pady=3
            )
            ttk.Entry(paths, textvariable=variable).grid(
                row=row, column=1, sticky="ew", pady=3
            )
            ttk.Button(paths, text="Browse...", command=browse).grid(
                row=row, column=2, sticky="e", padx=(8, 0), pady=3
            )

        selection = ttk.LabelFrame(frame, text="Conversion", padding=(10, 6))
        selection.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        selection.columnconfigure(1, weight=1)
        ttk.Label(selection, text="Source level").grid(
            row=0, column=0, sticky="w", padx=(0, 12)
        )
        self.level_picker = ttk.Combobox(
            selection, textvariable=self.selected_level, state="readonly"
        )
        self.level_picker.grid(row=0, column=1, sticky="ew")
        ttk.Button(selection, text="Refresh list", command=self.refresh_levels).grid(
            row=0, column=2, padx=(8, 0)
        )
        ttk.Label(
            selection,
            text="The .lvl is written to the destination mod's levels folder.",
        ).grid(row=1, column=1, columnspan=2, sticky="w", pady=(6, 0))

        buttons = ttk.Frame(frame)
        buttons.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(8, 2))
        self.convert_button = ttk.Button(
            buttons,
            text="Generate / replace .lvl",
            command=self.start_conversion,
            style="Accent.TButton",
        )
        self.convert_button.pack(side=tk.LEFT)
        self.cancel_button = ttk.Button(
            buttons,
            text="Cancel",
            command=self.cancel_conversion,
            state=tk.DISABLED,
        )
        self.cancel_button.pack(side=tk.LEFT, padx=(8, 0))
        self.progress = ttk.Progressbar(
            buttons, mode="indeterminate", length=180
        )
        self.progress.pack(side=tk.RIGHT, padx=(12, 0))
        ttk.Label(buttons, textvariable=self.status_text).pack(
            side=tk.RIGHT, fill=tk.X, expand=True
        )

        log_panel = ttk.LabelFrame(frame, text="Progress details", padding=8)
        log_panel.grid(row=3, column=0, columnspan=3, sticky="nsew", pady=(6, 0))
        log_panel.columnconfigure(0, weight=1)
        log_panel.rowconfigure(0, weight=1)
        self.log = tk.Text(
            log_panel,
            height=14,
            wrap=tk.WORD,
            state=tk.DISABLED,
            borderwidth=0,
            padx=8,
            pady=8,
        )
        self.log.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_panel, orient=tk.VERTICAL, command=self.log.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scrollbar.set)
        ttk.Style(root).configure("Accent.TButton", padding=(14, 8))
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh_levels()
        self.append_log(f"Mod: {self.mod_dir.get()}")
        self.append_log(f"Baserom file: {self.baserom_file.get()}")
        self.append_log(f"Compiler: {self.executable.get()}")

    def apply_icon(self) -> None:
        if ICON_PNG.is_file():
            self.logo_image = tk.PhotoImage(file=str(ICON_PNG))
            self.root.iconphoto(True, self.logo_image)

    def refresh_levels(self) -> None:
        try:
            levels = source_level_dirs(Path(self.source_root.get()))
        except (ConversionError, OSError) as exc:
            self.level_picker.configure(values=())
            self.selected_level.set("")
            self.status_text.set(str(exc))
            return

        names = [level.name for level in levels]
        values = [ALL_LEVELS, *names]
        self.level_picker.configure(values=values)
        if self.selected_level.get() not in values:
            self.selected_level.set(
                "castle_grounds" if "castle_grounds" in names else ALL_LEVELS
            )
        self.status_text.set(f"Found {len(names)} level folder(s).")

    def save_current_settings(self) -> None:
        save_settings(
            {
                "coopdx_executable": self.executable.get().strip(),
                "baserom_file": self.baserom_file.get().strip(),
                "mod_folder": self.mod_dir.get().strip(),
                "source_levels_folder": self.source_root.get().strip(),
                "selected_level": self.selected_level.get(),
            }
        )

    def close(self) -> None:
        if self.pending_conversion:
            if not messagebox.askyesno(
                "Conversion waiting",
                "Stop waiting for CoopDX to close and exit?",
                parent=self.root,
            ):
                return
            self.pending_conversion = None
            self.cancel_event.set()
            self.finish_closing()
            return
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(
                "Conversion running",
                "Cancel the conversion and close the window?",
                parent=self.root,
            ):
                return
            self.cancel_event.set()
            self.root.after(100, self.close_when_finished)
            return
        self.finish_closing()

    def close_when_finished(self) -> None:
        if self.worker and self.worker.is_alive():
            self.root.after(100, self.close_when_finished)
            return
        self.finish_closing()

    def finish_closing(self) -> None:
        try:
            self.save_current_settings()
        except OSError as exc:
            messagebox.showerror("Could not save settings", str(exc))
        self.root.destroy()

    def browse_mod(self) -> None:
        selected = filedialog.askdirectory(
            title="Select a CoopDX mod folder",
            initialdir=self.mod_dir.get(),
        )
        if selected:
            self.mod_dir.set(selected)

    def browse_source(self) -> None:
        selected = filedialog.askdirectory(
            title="Select a levels folder from the mod or decomp",
            initialdir=self.source_root.get(),
        )
        if selected:
            self.source_root.set(selected)
            self.refresh_levels()

    def use_mod_levels(self) -> None:
        self.source_root.set(str(Path(self.mod_dir.get()) / "levels"))
        self.refresh_levels()

    def browse_executable(self) -> None:
        selected = filedialog.askopenfilename(
            title="Select sm64coopdx.exe",
            filetypes=(("CoopDX executable", "sm64coopdx.exe"), ("All files", "*.*")),
            initialdir=str(Path(self.executable.get()).parent),
        )
        if selected:
            self.executable.set(selected)

    def browse_baserom(self) -> None:
        selected = filedialog.askopenfilename(
            title="Select the SM64 baserom",
            filetypes=(("Nintendo 64 ROM", "*.z64;*.n64;*.v64"), ("All files", "*.*")),
            initialdir=str(Path(self.baserom_file.get()).parent),
        )
        if selected:
            self.baserom_file.set(selected)

    def append_log(self, line: str) -> None:
        self.log.configure(state=tk.NORMAL)
        self.log.insert(tk.END, line + "\n")
        line_count = int(self.log.index("end-1c").split(".")[0])
        if line_count > 1000:
            self.log.delete("1.0", f"{line_count - 1000}.0")
        self.log.see(tk.END)
        self.log.configure(state=tk.DISABLED)

    def start_conversion(self) -> None:
        if (self.worker and self.worker.is_alive()) or self.pending_conversion:
            return
        try:
            self.save_current_settings()
        except OSError as exc:
            messagebox.showerror("Could not save settings", str(exc))
            return
        if not self.selected_level.get():
            messagebox.showerror(
                "No level selected", "Choose a level source folder first."
            )
            return
        mod_dir = Path(self.mod_dir.get())
        source_root = Path(self.source_root.get())
        selected_level = self.selected_level.get()
        executable = Path(self.executable.get())
        baserom_file = Path(self.baserom_file.get())
        conversion = (mod_dir, source_root, selected_level, executable, baserom_file)

        try:
            processes = running_coopdx_processes()
        except ConversionError as exc:
            messagebox.showerror("Could not check CoopDX", str(exc))
            return

        self.cancel_event.clear()
        if processes:
            if not messagebox.askyesno(
                "CoopDX is running",
                "Close the game to avoid changing files it may be using. "
                "Wait here and start converting automatically after CoopDX closes?",
            ):
                return
            self.pending_conversion = conversion
            self.convert_button.configure(state=tk.DISABLED)
            self.cancel_button.configure(state=tk.NORMAL)
            self.progress.start(12)
            self.status_text.set("Waiting for CoopDX to close...")
            self.append_log(
                "Waiting for CoopDX process(es) "
                + ", ".join(processes)
                + " to close."
            )
            self.root.after(1000, self.wait_for_coopdx_to_close)
            return

        self.begin_conversion(conversion)

    def wait_for_coopdx_to_close(self) -> None:
        conversion = self.pending_conversion
        if conversion is None:
            return
        if self.cancel_event.is_set():
            self.pending_conversion = None
            self.convert_button.configure(state=tk.NORMAL)
            self.cancel_button.configure(state=tk.DISABLED)
            self.progress.stop()
            self.status_text.set("Conversion cancelled.")
            self.append_log("Cancelled while waiting for CoopDX to close.")
            return
        try:
            processes = running_coopdx_processes()
        except ConversionError as exc:
            self.pending_conversion = None
            self.convert_button.configure(state=tk.NORMAL)
            self.cancel_button.configure(state=tk.DISABLED)
            self.progress.stop()
            self.status_text.set("Could not check CoopDX.")
            self.append_log(str(exc))
            messagebox.showerror("Could not check CoopDX", str(exc))
            return

        if processes:
            self.status_text.set(
                "Waiting for CoopDX to close (PID "
                + ", ".join(processes)
                + ")..."
            )
            self.root.after(1000, self.wait_for_coopdx_to_close)
            return

        self.pending_conversion = None
        self.append_log("CoopDX closed. Starting conversion.")
        self.begin_conversion(conversion)

    def begin_conversion(
        self, conversion: tuple[Path, Path, str, Path, Path]
    ) -> None:
        self.cancel_event.clear()
        self.convert_button.configure(state=tk.DISABLED)
        self.cancel_button.configure(state=tk.NORMAL)
        self.progress.start(12)
        self.status_text.set("Starting conversion...")
        self.append_log("Starting conversion.")
        self.worker = threading.Thread(
            target=self.convert_in_background,
            args=conversion,
            daemon=True,
        )
        self.worker.start()

    def cancel_conversion(self) -> None:
        self.cancel_event.set()
        if self.pending_conversion:
            self.pending_conversion = None
            self.convert_button.configure(state=tk.NORMAL)
            self.cancel_button.configure(state=tk.DISABLED)
            self.progress.stop()
            self.status_text.set("Conversion cancelled.")
            self.append_log("Cancelled while waiting for CoopDX to close.")
        else:
            self.status_text.set("Cancelling...")

    def convert_in_background(
        self,
        mod_dir: Path,
        source_root: Path,
        selected_level: str,
        executable: Path,
        baserom_file: Path,
    ) -> None:
        try:
            outputs = convert_levels(
                mod_dir,
                source_root,
                selected_level,
                executable,
                baserom_file,
                lambda line: self.root.after(0, self.append_log, line),
                lambda text: self.root.after(0, self.status_text.set, text),
                self.cancel_event,
            )
            self.root.after(0, self.finish, outputs, None)
        except Exception as exc:
            self.root.after(0, self.finish, None, str(exc))

    def finish(
        self, outputs: list[Path] | None, error: str | None
    ) -> None:
        self.convert_button.configure(state=tk.NORMAL)
        self.cancel_button.configure(state=tk.DISABLED)
        self.progress.stop()
        if error is not None:
            self.status_text.set("Conversion failed.")
            self.append_log(error)
            messagebox.showerror("Conversion failed", error)
            return

        assert outputs is not None
        details = "\n".join(
            f"{path.name} ({path.stat().st_size:,} bytes)" for path in outputs
        )
        self.status_text.set(f"Converted {len(outputs)} level(s) successfully.")
        self.append_log("Conversion complete:\n" + details)
        messagebox.showinfo("Conversion complete", details)


def main() -> None:
    root = tk.Tk()
    root.withdraw()
    try:
        settings = load_settings()
    except ConversionError as exc:
        messagebox.showerror("Invalid converter settings", str(exc), parent=root)
        root.destroy()
        return
    ConverterApp(root, settings)
    root.deiconify()
    root.mainloop()


if __name__ == "__main__":
    main()
