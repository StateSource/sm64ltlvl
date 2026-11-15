# SM64 level to .lvl

Run `sm64ltlvl.exe` for the packaged app, or run `convert_levels.py` with Python
3.10 or newer. The converter window uses the paths in `config.json` (including
the CoopDX executable and baserom); changes are saved there when you generate a
level or close the window.

The `module` folder contains CoopDX and its runtime files. Keep those files
together; the converter uses `module\sm64coopdx.exe` by default. The `res` folder
contains the original SVG icon and its PNG/ICO app assets.

To rebuild the Windows executable, run:

```powershell
python build_exe.py
```

This uses PyInstaller and creates `sm64ltlvl.exe` in this folder. Install
PyInstaller first if it is not already available in the Python environment.

To compile directly from the decomp, keep its `levels` folder in **Source
levels folder**, choose a level (for example `castle_grounds`), and click
**Generate / replace .lvl**. You can also choose the mod's own `levels` folder
or **All levels**. The generated `.lvl` files go into the selected destination
mod's `levels` folder. Decomp sources are read-only.

Compilation temporarily copies the selected source into the destination mod,
uses the configured baserom's folder as CoopDX's save/config path, then restores
the original source. If CoopDX is running, the window can wait and start
automatically after you close it. The Castle Grounds copy is adapted for DynOS:
flowing-water triangles are given the required force value, the unsupported
Floomba macro is omitted, and duplicate legacy-unused collisions are skipped.
The decomp source tree is never changed.

Existing `.lvl` files are restored if compilation fails or a fresh valid output
is missing. Compiler output is shown in the window and saved as
`last_compile.log`; parse errors are reported immediately.
