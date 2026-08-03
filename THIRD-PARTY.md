# Third-party components

Image Catalyst is an orchestrator: it does not compress anything itself. Every
optimizer is launched as a **separate process** with a command line and files —
nothing is linked into, loaded by, or shares an address space with Image
Catalyst. That is the dividing line the FSF itself draws: separate programs
communicating at arm's length are aggregation, not a combined work. So invoking
GPL-licensed optimizers does not make Image Catalyst GPL, and its own code stays
MIT (see `LICENSE.md`). This paragraph exists so the question does not have to be
re-asked.

What *does* create obligations is **redistributing** the binaries — inside this
repository, inside `iCatalyst.exe`, or inside the Linux tools archive.

## Open-source components

| Component | Used as | License | Obligation when we ship the binary |
|---|---|---|---|
| [AdvanceComp](https://github.com/amadvance/advancecomp) (`advdef`) | PNG deflate recompression | GPL-2.0-or-later | license text + corresponding source, or a valid offer |
| [gifsicle](https://www.lcdf.org/gifsicle/) | GIF optimization | GPL-2.0 | same |
| [pngwolf-zopfli](https://github.com/jibsen/pngwolf-zopfli) | PNG Xtreme deflate stage | GPL-3.0 | license text + source; §6(d) permits a network location |
| [MozJPEG](https://github.com/mozilla/mozjpeg) (`jpegtran`) | JPEG lossless transforms | IJG + BSD-3-Clause + zlib | notice; the IJG part requires stating the software is "based in part on the work of the Independent JPEG Group" |
| [OptiPNG](https://optipng.sourceforge.net/) | PNG structural optimization (Linux) | zlib/libpng | notice only |
| [Zopfli / zopflipng](https://github.com/google/zopfli) | PNG Xtreme deflate (Linux) | Apache-2.0 | notice + NOTICE file |
| [oxipng](https://github.com/oxipng/oxipng) | PNG structural optimization, candidate in the race | MIT | notice |
| [PyInstaller](https://pyinstaller.org/) | build-time only | GPL-2.0-or-later with bootloader exception | the exception explicitly permits shipping non-GPL applications built with it |

We build the GPL components ourselves from pinned commits recorded in
`Tools/tools.lock.json`, so making their corresponding source available is nearly
free: the release workflow attaches `third-party-sources.tar.gz` to the **same
release** as the binaries. For the GPLv2 components that is the clean,
unarguable answer — "equivalent access to copy the source from the same place" —
rather than relying on links that may rot.

## Closed-source freeware, redistributed without a written grant

These are the real exposure, and they are Windows-only:

| Component | Author | Status |
|---|---|---|
| TruePNG 0.6.2.2 | [X128](http://x128.ho.ua/) | closed freeware, no license text in this repository |
| DeflOpt 2.07 | Ben Jos Walbeehm | closed freeware, author's site defunct |

"Freeware" grants a right to *use*, not automatically a right to
*redistribute*. The README has thanked X128 "for his huge contribution to the
development of the project" since 2010, which strongly suggests TruePNG's author
was a willing participant — but a credit line is not a license grant.

Version 3.0 reduces this surface from five files to two. `jpginfo.exe`,
`jpegstripper.exe` and `browsefolder.exe` are gone, replaced respectively by a
pure-Python JPEG marker scan (`icatalyst/imgcheck.py`), `jpegtran -copy none`,
and a native folder dialog (`icatalyst/picker.py`). Those replacements produce
the same results and cost nothing.

TruePNG and DeflOpt remain because on Windows they are free compression wins and
there is no reason to throw away quality users already have. If a written
redistribution permission cannot be obtained, the honest options are to ship them
as an optional separate download, or to drop them and rely on the oxipng
candidate — the candidate race means output would get slightly larger, never
broken.

## Reproducibility

`Tools/tools.lock.json` pins every external component by tag or commit and, for
downloads, by SHA-256. Hashes are verified on **every** download, without an
opt-out: we are fetching executable code over the network. Submodules of
pngwolf-zopfli are deliberately left at their 2017 commits — bumping them
resurfaces upstream issue #5, where a file moved inside libdeflate.
