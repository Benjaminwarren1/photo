# photorecover

A free tool for rescuing deleted photos from memory cards. It avoids the "grey box" that other recovery
tools leave when a photo was stored on the card in more than one piece.

**Status: early development, not yet tried on many real cards.** It reads FAT16, FAT32 and exFAT cards, rebuilds
fragmented JPEG photos and MP4/MOV/3GP videos, and has a step-by-step wizard. A card that was reformatted is
read the way it was laid out before, and a drive in a format it cannot read (NTFS, Mac, Linux, or damaged)
is searched directly for photos. Still to come: optional AI help.

## Why it is different

Most recovery tools assume every deleted file sat in one unbroken run on the card. Fragmented photos then
come back with a grey or garbled lower half, or with part of another photo spliced in.

photorecover follows each photo through the card piece by piece. Every candidate piece has to pass two tests:

1. **Structure.** A resumable JPEG decoder must run through it without the data ceasing to be valid.
2. **Picture.** What it adds must match the small preview that phones and cameras embed in every photo.

Videos are rebuilt from their own index: every frame and audio packet it lists is a checkpoint that must
match where the rebuilt file says it is.

A photo is only called complete when the decoder reaches the exact number of blocks the header promises and
finds the end marker exactly where the file size says it is. Everything is audited again before it is saved,
and the report says plainly which files are verified, which are partial and which are not on the card.

## Use

**The wizard.** Run `photorecover` with no arguments (or double-click `PhotoRecovery.exe` from a Windows
build). It finds the card when you plug it in, copies it to an image file, and recovers from the copy.
The card is opened read-only and you can unplug it as soon as the copy is done.

**From the command line**, if you already have an image of the card:

```
pip install .
photorecover recover card.img --out recovered
```

`--json` prints a machine-readable summary, and `report.json` in the output folder lists every file.

`photorecover pilot card.img` runs the same recovery and then takes the close calls as JSON lines on standard input (`unresolved`, `candidates`, `pick`), so a script or an AI assistant can look at the candidate pictures and choose. A choice is only kept if the finished photo passes the same checks as any other.

Output:

| Folder | What goes in it |
|---|---|
| `Recovered` | Files that passed every check |
| `Recovered - Partial` | The good part of files that are incomplete or need a person to decide |
| `Recovered - Preview filled` | A second copy of each partial photo, with the missing part filled in from the photo's own small preview. The filled part is blurry, but it is the real picture |
| `report.html` | One line per file: result, how it was recovered, and notes |

The image is opened read-only and is never changed.

## Develop

```
pip install -e ".[dev]"
pytest
```

The tests build synthetic cards (`photorecover.synth`) with realistic fragmentation, delete everything, and
check the tool rebuilds each file byte for byte and never delivers a wrong one. No real photos are used or
stored in this repository.

A small C extension (`src/photorecover/_scan.c`) does the JPEG decoding. If it cannot be built, a pure-Python
twin is used instead; it gives identical results but is far slower.

## Licence

GPL-3.0-or-later. Free for everyone, forever.
