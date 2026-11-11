# photorecover

A free tool for rescuing deleted photos from memory cards. It avoids the "grey box" that other recovery
tools leave when a photo was stored on the card in more than one piece.

**Status: early development.** The recovery engine works on FAT16 and FAT32 card images from the command
line. The Windows wizard, exFAT and video rebuilding are still to come.

## Why it is different

Most recovery tools assume every deleted file sat in one unbroken run on the card. Fragmented photos then
come back with a grey or garbled lower half, or with part of another photo spliced in.

photorecover follows each photo through the card piece by piece. Every candidate piece has to pass two tests:

1. **Structure.** A resumable JPEG decoder must run through it without the data ceasing to be valid.
2. **Picture.** What it adds must match the small preview that phones and cameras embed in every photo.

A photo is only called complete when the decoder reaches the exact number of blocks the header promises and
finds the end marker exactly where the file size says it is. Everything is audited again before it is saved,
and the report says plainly which files are verified, which are partial and which are not on the card.

## Use

Work from an image of the card, never the card itself.

```
pip install .
photorecover recover card.img --out recovered
```

Output:

| Folder | What goes in it |
|---|---|
| `Recovered` | Files that passed every check |
| `Recovered - Partial` | The good part of files that are incomplete or need a person to decide |
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
