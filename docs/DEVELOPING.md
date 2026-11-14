# For developers

Everything technical lives here; [README.md](../README.md) is written for the people the tool is for.

## Why it is different

Most recovery tools assume every deleted file sat in one unbroken run on the card. Fragmented photos then
come back with a grey or garbled lower half, or with part of another photo spliced in.

photolifeboat follows each photo through the card piece by piece. Every candidate piece has to pass two tests:

1. **Structure.** A resumable JPEG decoder must run through it without the data ceasing to be valid.
2. **Picture.** What it adds must match the small preview that phones and cameras embed in every photo.

Videos are rebuilt from their own index: every frame and audio packet it lists is a checkpoint that must
match where the rebuilt file says it is.

A photo is only called complete when the decoder reaches the exact number of blocks the header promises and
finds the end marker exactly where the file size says it is. Everything is audited again before it is saved,
and the report says plainly which files are verified, which are partial and which are not on the card.

## The Windows build

`packaging/PhotoLifeboat.spec` builds one folder holding two programs that share their libraries:
`PhotoLifeboat.exe`, the windowed app (the wizard in a window of its own, through pywebview and the Edge
WebView2 engine built into Windows), and `photolifeboat-cli.exe`, the command line. The app runs the command
line for helper jobs that need standard output, such as copying the card. `packaging/installer.iss` (Inno
Setup) wraps the folder in a per-user installer. CI builds both, smoke-tests the window, the installer and
the uninstaller, and publishes them (see [RELEASING.md](RELEASING.md)).

Without pywebview (`pip install ".[window]"` adds it), or without WebView2, the wizard opens in the browser
instead. `PHOTOLIFEBOAT_BROWSER=1` forces the browser.

## Command line

If you already have an image of the card (on Windows, `photolifeboat-cli.exe` from the build):

```
pip install .
photolifeboat recover card.img --out recovered
```

`--json` prints a machine-readable summary, and `report.json` in the output folder lists every file.

`photolifeboat pilot card.img` runs the same recovery and then takes the close calls as JSON lines on standard input (`unresolved`, `candidates`, `pick`), so a script or an AI assistant can look at the candidate pictures and choose. A choice is only kept if the finished photo passes the same checks as any other.

Output:

| Folder | What goes in it |
|---|---|
| `Recovered/<Photos, Videos, Documents, Music and sound, Other files>` | Files that came back whole, sorted by kind. Original folders are not rebuilt; name clashes get " (2)" |
| `Recovered/Small pictures (thumbnails and app images)` | Pictures under 640 px, kept apart so they do not bury real photos |
| `Recovered - Partial` | The good part of files that are incomplete or need a person to decide |
| `Recovered - Preview filled` | A second copy of each partial photo, with the missing part filled in from the photo's own small preview |
| `report.html`, `report.json` | One line per file: result, how it was recovered, where it used to be, and notes |

The image is opened read-only and is never changed.

## Develop

```
pip install -e ".[dev]"
pytest
```

The tests build synthetic cards (`photolifeboat.synth`) with realistic fragmentation, delete everything, and
check the tool rebuilds each file byte for byte and never delivers a wrong one. No real photos are used or
stored in this repository.

A small C extension (`src/photolifeboat/_scan.c`) does the JPEG decoding. If it cannot be built, a pure-Python
twin is used instead; it gives identical results but is far slower.


## Releasing

See [RELEASING.md](RELEASING.md).
