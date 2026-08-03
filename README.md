# Image Catalyst

Lossless PNG, JPEG and GIF image optimization / compression for **Windows and Linux**.

|![Adobe Photoshop](https://cloud.githubusercontent.com/assets/3890881/12113971/831d0e22-b3b7-11e5-8f6d-a5cc8f993767.png)|![Image Catalyst](https://cloud.githubusercontent.com/assets/3890881/12110952/70ce4462-b3a2-11e5-8b29-a3822b246dfe.png)|
|:----------|:----------|
|Adobe Photoshop CC 2015 (Export as) — 56.10 KB|Image Catalyst (Xtreme) — 51.25 KB|

Created by [**Lorents**](https://github.com/lorents17) & [**Res2001**](https://github.com/res2001)

> **Version 3.0 is a rewrite.** The orchestrator moved from a 1300-line Windows
> batch script to a Python 3 core, which is what makes Linux support possible and
> what kills a long-standing bug where files with national characters in their
> names were silently skipped. Version 2.7 is still in this repository and still
> works; see [Migrating from 2.7](#migrating-from-27).

### Tools

Image Catalyst compresses nothing itself — it orchestrates well-known optimizers
and keeps whichever result is smallest. Run `icatalyst --doctor` to see exactly
which ones were found on your machine and the exact commands that will be run.

##### PNG
- [oxipng](https://github.com/oxipng/oxipng), [OptiPNG](https://optipng.sourceforge.net/), [Zopfli](https://github.com/google/zopfli) (`zopflipng`), [AdvanceComp](https://github.com/amadvance/advancecomp) (`advdef`)
- Windows additionally: [TruePNG](http://x128.ho.ua/pngutils.html) 0.6.2.2, DeflOpt 2.07, [pngwolf-zopfli](https://github.com/jibsen/pngwolf-zopfli)

##### JPEG
- [MozJPEG](https://github.com/mozilla/mozjpeg) or [libjpeg-turbo](https://libjpeg-turbo.org/) (`jpegtran`)

##### GIF
- [GIFSicle](https://github.com/kohler/gifsicle)

### System requirements

- **Windows 10 or newer.** A release build of `iCatalyst.exe` needs nothing else.
  Running from source needs Python 3.9+.
  *(Version 2.7 supported Windows XP SP3; 3.0 does not, because Python 3.9 does not.)*
- **Linux**: Python 3.9 or newer, plus the optimizers:
  ```
  sudo apt install optipng zopfli advancecomp gifsicle libjpeg-turbo-progs
  make tools          # downloads oxipng, verifying its SHA-256
  ```
  `make apt` prints the exact package line for your distribution's naming, and
  `make check` tells you what is still missing and what it costs you.

### Drag and Drop

![Drag and Drop](https://cloud.githubusercontent.com/assets/3890881/7943598/28496fd4-096e-11e5-8df6-d6415e47caf8.png)

- **Windows**: drop folders or files onto `iCatalyst.exe`, or onto
  `iCatalyst-3.bat` when running from source.
- **Linux**: install `icatalyst.desktop` (`cp icatalyst.desktop
  ~/.local/share/applications/`) and drop folders onto its icon, or use
  "Open With" in your file manager.
- Images in sub-directories are optimized recursively.
- **Any characters are allowed in paths** — national alphabets, spaces,
  punctuation, emoji. This is the main behavioural change from 2.7.

### Command line options

```
icatalyst [options] [add directories \ add files]

Options:

/png:# PNG optimization mode (Non-Interlaced):
       1 - Compression level - Advanced
       2 - Compression level - Xtreme
       0 - Skip

/jpg:# JPEG optimization mode:
       1 - Encoding Process - Baseline
       2 - Encoding Process - Progressive
       3 - use settings of original image
       0 - Skip

/gif:# GIF optimization mode:
       1 - use settings of original image
       0 - Skip

"/outdir:#" image saving options:
       true  - ask where to save images (default)
       false - replace original image with optimized
       "full path to directory" - specify directory to save images to.
       If the destination directory does not exist, it will be created.

Additional options (new in 3.0):

--verify            compare PNG pixel data before and after (slow, thorough)
--strict-lossless   forbid changing RGB under fully transparent pixels
--threads N         number of parallel jobs (0 or absent = CPU count)
--stream            print rows in completion order instead of input order
--tsv               machine-readable output instead of the table
--doctor            show which tools were found and what will be run
--picker NAME       auto, tk, zenity, kdialog, osascript, terminal or none
--config FILE       use this config.ini instead of Tools/config.ini
--width N           force table width
--no-pause          do not wait for a key press at the end

Examples:
icatalyst /gif:1 "/outdir:/home/user/photos" "/home/user/images"
icatalyst /png:2 /jpg:2 "/outdir:true" "C:\images"
```

If a mode is not given on the command line, Image Catalyst asks — but only for
formats actually present in the input, exactly as 2.7 did.

### What "lossless" means here

Pixels and alpha are never altered, with one deliberate exception that has been
the shipped default since 2.7: `xtreme=/a1` in `Tools/config.ini` permits
rewriting the **RGB values underneath fully transparent pixels**. Such pixels are
invisible, so the image is visually identical, but the file is not byte-identical
in the pixel data. Pass `--strict-lossless` to forbid it.

Measured on 60 already-optimized system icons: `--strict-lossless` cost nothing
at all — it actually compressed marginally *better* (−8.73% vs −8.65%), because
the "dirty transparency" heuristic is not always a win and the candidate race
picks whichever result is smaller.

Every result is checked before it is accepted: the file must decode, keep its
dimensions, keep its critical chunks, and be **strictly smaller** than the input.
If it is not, the original is kept untouched.

### PNG optimization settings

|Advanced|Xtreme|
|:-------|:----------|
|Structural optimization plus one deflate pass. Seconds per image.|Exhaustive structural search plus Zopfli-class compression. 5–15× slower, a few percent smaller.|

Both modes race several independent chains and keep the smallest result, so
adding a tool can only improve the outcome, never worsen it. Xtreme is guaranteed
never to produce a larger file than Advanced.

`Interlaced` output is not supported; interlaced input is read correctly and
written non-interlaced.

### JPEG optimization settings

|Baseline|Progressive|
|:-------|:----------|
|For images < 10 KB ([read more](http://yuiblog.com/blog/2008/12/05/imageopt-4/))|For images > 10 KB ([read more](http://yuiblog.com/blog/2008/12/05/imageopt-4/))|

`Default` keeps whatever the original used. JPEG optimization is a lossless
coefficient transform: `jpegtran` never re-encodes pixel data.

### Cross-platform differences, honestly

PNG output is **not byte-identical between Windows and Linux**, and cannot be:
TruePNG and DeflOpt are closed-source Windows-only freeware with no equivalents.
Neither platform's output is "the correct" one; both are lossless.

| Step | Windows | Linux |
|---|---|---|
| PNG Advanced | TruePNG → DeflOpt → advdef → DeflOpt ‖ oxipng | optipng → advdef ‖ oxipng |
| PNG Xtreme | TruePNG → pngwolf (Zopfli) → DeflOpt ‖ oxipng | optipng → zopflipng ‖ optipng → advdef ‖ oxipng |
| JPEG | MozJPEG `jpegtran` | libjpeg-turbo `jpegtran`; MozJPEG optional via `make tools-build`, worth 2–4% on progressive |
| GIF | gifsicle | the same gifsicle, the same flags |
| Folder dialog | native Windows dialog | zenity, kdialog or a terminal prompt |

`‖` means the chains are raced and the smaller result wins.

### Config.ini

`Tools/config.ini` keeps every key it had in 2.7 — existing configurations work
unchanged. New optional keys: `profile` (`auto`/`windows`/`posix`), `timeout`,
`picker`, `preserve_mtime`, `xtreme_iterations`, and platform-neutral equivalents
of the TruePNG flag strings (`gamma`, `keep_colortype`, `keep_bitdepth`,
`keep_palette`, `advanced_dirty_transparency`, `xtreme_dirty_transparency`).
`pngtags`/`jpegtags`/`giftags` additionally accept `keep-icc`, which preserves
colour profiles that `true` would strip.

The file is never rewritten by the program.

### Migrating from 2.7

Both implementations currently live side by side. `iCatalyst.bat` is still
version 2.7 and still works; the new core is `python3 -m icatalyst`, or
`iCatalyst-3.bat` on Windows. The legacy files will be removed only after the
`parity-windows` job in CI confirms on real binaries that the new core compresses
no worse.

Behavioural changes worth knowing about:

- Paths with any characters now work. In 2.7 a folder named `Фото — копия`
  silently lost its entire contents, because the em dash does not exist in cp866.
- A GIF's loop count survives metadata removal. In 2.7 `giftags=true` passed
  `--no-extensions` to gifsicle, which dropped the NETSCAPE extension and turned
  an infinitely looping GIF into a play-once one.
- Interrupting with Ctrl-C now stops promptly and never leaves a truncated file.
- Two input files with the same name no longer overwrite each other in the output
  directory.
- Modification times are preserved by default (`preserve_mtime=false` restores
  the old behaviour).
- A batch run with no `/outdir` and no terminal to ask in now stops with an error
  instead of silently overwriting the originals.

### Additionally

- By default optimization runs in parallel, one job per CPU core. Use
  `--threads N` or the `thread` key in `config.ini` to change that. Measured on
  16 cores: 177 s → 38 s, with byte-identical output.
- To pause on Windows, click the right mouse button in the console window and
  choose "Select all"; click again to resume.

### Building

Nothing needs to be built to use Image Catalyst from source — it is plain Python.
The build targets exist to produce distributable artifacts:

```
make deb        # .deb package, checked with lintian
make binary     # self-contained Linux executable (no Python needed to run it)
make exe        # iCatalyst.exe (Windows only)
make tools      # download prebuilt optimizers, verifying their SHA-256
make test       # the full test suite; passes with no optimizers installed
```

`make binary` and `make exe` use PyInstaller, which is a **build-time**
dependency only — the program itself never needs anything beyond the standard
library. On Ubuntu 24.04 and newer, PEP 668 forbids installing into the system
Python, so `make binary` creates its own virtual environment.

GitHub Actions builds all of this on a tag, but only **after** the test suite and
the 2.7 parity harness are green: attaching a package built from knowingly broken
code to a release is the worst outcome available here. Releases are created as
drafts so the artifact list can be looked at before publishing.

| Artifact | For whom |
|---|---|
| `icatalyst_<version>_all.deb` | Debian, Ubuntu, Mint — installs the command, the menu entry, the man page, and pulls the optimizers in via `apt` |
| `iCatalyst-<tag>-linux-x86_64.tar.gz` | other distributions, or running without installing anything |
| `iCatalyst-<tag>-windows-x86_64.zip` | Windows: `iCatalyst.exe` plus the bundled optimizers next to it |
| `iCatalyst-tools-linux-x86_64.tar.gz` | prebuilt MozJPEG, pngwolf and oxipng, so Linux users need not compile |
| `third-party-sources.tar.gz` | corresponding sources of the GPL components, as their licenses require |

Artifacts are attached to a **draft** release, and only when there is a tag to
attach them to: either the workflow was triggered by pushing a tag, or a tag was
supplied to a manual run. A manual run without a tag still builds everything and
leaves the artifacts on the run page — that is what it is for. The run summary
states which of these happened, so a skipped publish step never has to be
guessed at.

### Thanks

- Thanks to the authors of the applications that are used in the project;
- Thanks to the participants of [encode.ru](http://encode.ru/), [forum.ru-board.com](http://forum.ru-board.com/), [forum.script-coding.com](http://script-coding.com/forum/), [forum.vingrad.ru](http://forum.vingrad.ru/) and [cyberforum.ru](http://www.cyberforum.ru/) for contribution to the development of the project;
- Thanks [**X128**](http://x128.ho.ua/) for his huge contribution to the development of the project.

### License

This software is released under the terms of the [MIT](LICENSE.md) license.
Third-party components and their licenses are listed in [THIRD-PARTY.md](THIRD-PARTY.md).

### Future plans

- add support of optimization of SVG;
- add support of optimization of PNG and JPEG lossy.
