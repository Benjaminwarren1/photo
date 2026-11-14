# Photo Recovery

**Free software that gets deleted photos back from memory cards.**

Deleted the photos on your camera or phone card by mistake? Formatted the wrong card? Got a "this card
needs formatting" message? As long as nothing new has been saved over them, your photos are usually
still on the card. This program finds them and copies them back onto your computer.

It is free, it has no adverts, and it never asks you to pay to "unlock" your photos.

> **Early version.** It has rescued thousands of photos in testing, but it has not yet been tried on many
> different cards and cameras. If it does not work for you, please [tell us](../../issues).

## First: stop using the card

This matters more than anything else. Until your photos are rescued:

- don't take any more photos on the card,
- don't copy anything onto it,
- don't format it, even if the computer or camera asks you to.

Each new photo can land on top of an old one, and a photo that has been written over is gone for good.
No program anywhere can bring it back.

If your card has a small lock switch on its side, slide it to **Lock**. The program never changes the
card, but locking it keeps everything else from changing it too.

## How to use it (Windows 10 and 11)

1. Download **PhotoRecovery-…-setup.exe** from the [latest release](../../releases/latest) and open it.
2. Windows may show a blue box saying "Windows protected your PC". This appears for any new program that
   has not paid for a security certificate yet. Click **More info**, then **Run anyway**.
3. Click through the installer. It takes a few seconds and doesn't need an administrator. Photo Recovery
   then opens, and you'll find it in the Start menu and on your desktop.
4. Choose where your rescued photos should go: your **Pictures** folder, your **Desktop**, or somewhere else
   such as an external drive.
5. Follow the steps on screen. The program will ask you to unplug the card and plug it back in, so it can
   tell which drive is your card. Windows will then ask for permission to read the card: choose **Yes**.

First it makes a copy of your card on the computer, then it rescues the photos from that copy. When it says
the copy is finished you can take the card out. You will need enough free space for the copy: about the size
of the card.

When it has finished, click **Open the folder** to see your photos. Once you're happy with them, the program
offers to delete the copy of the card to free up the space.

To remove the program later, uninstall **Photo Recovery** from Settings > Apps, like any other program. Your
rescued photos are not touched.

Rather not install anything? The release also has a **portable** zip: extract it and double-click
**PhotoRecovery**.

Mac and Linux versions are planned but not ready yet.

## What you get back

Everything goes in a new folder called **Rescued photos** followed by the date, in the place you chose.

| Folder | What is in it |
|---|---|
| **Recovered** | Files that came back complete, sorted into Photos, Videos, Documents, Music and sound, and Other files |
| **Recovered → Small pictures** | Thumbnails and little pictures that apps keep for themselves, kept apart so they don't bury your real photos |
| **Recovered - Partial** | Photos where only part could be saved. The top of the picture is real, and the rest is missing |
| **Recovered - Preview filled** | The same partial photos with the missing part filled in from the photo's own small preview. That part looks blurry, but it is still your real picture |
| **report.html** | A list of every file, what happened to it and why. Open it in your web browser |

### It rescues files, not folders

You get your photos back, but not the folders they were kept in on the card. Everything comes back sorted
by type into the folders above. The report shows where each file used to be, in case you need it.

### What it cannot do

- It cannot rescue photos that have been written over. If you carried on taking photos after deleting, some of
  the old ones will be gone. The report says so plainly instead of handing you a broken file.
- It cannot read cards that are physically broken, such as snapped or water-damaged cards that no computer can see.
  For those you need a professional data recovery service.
- It cannot read encrypted cards, such as some Android phones' internal-storage cards, without the phone.

### After you're done: leave the card alone for now

Keep the memory card as it is, and don't save anything new to it, until you're sure you have everything you
want. If something is missing, another recovery program may still find it, but only while the card stays
untouched. That's why Photo Recovery copies the card first and only ever works from the copy.

## Is it safe?

- **Your card is only ever read.** The program never writes anything to it.
- **Nothing leaves your computer.** Your photos are not uploaded anywhere.
- **The code is open.** Anyone can read exactly what it does. It is published under the GPL-3.0 licence, so it
  stays free for everyone, forever, and nobody can take it and sell it.

## Why it can do better than other free tools

When a card gets full, cameras sometimes store a photo in several pieces in different places. Most recovery
tools assume each photo is in one piece, so these photos come back with a grey or scrambled bottom half.
This program follows each photo piece by piece and checks every piece against the small preview that cameras
save inside every photo, so more of them come back whole. It also tells you honestly which photos it could
check fully and which it could not.

## Help and feedback

If something goes wrong or a message is confusing, please [open an issue](../../issues) and describe what
happened. Please don't attach your personal photos.

## Supporting the project

Photo Recovery is free and always will be. Donations, if you want to give one, go towards a code-signing
certificate (so Windows stops showing its warning) and towards keeping the program maintained.
*(Donation link coming soon.)*

## For developers

Technical details, the command-line version and how to build it are in [docs/DEVELOPING.md](docs/DEVELOPING.md).

Licence: GPL-3.0-or-later.
