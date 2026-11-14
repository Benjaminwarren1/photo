# Using Photo Lifeboat with an AI assistant

An AI assistant can run Photo Lifeboat for someone: find the card, copy it, run the recovery, and explain
the results in plain words. It can also do something the program can't do on its own: **look at the
rescued photos**. It can spot a photo that came back garbled, and help with the close calls, where the
program has found a few pieces that might come next in a photo and won't guess which one is right.

## Which assistants can do it

The assistant has to be able to run programs on the computer the card is plugged into. That means:

- **Assistants that run commands on your computer**, such as Claude Code, the Claude desktop app, OpenAI's
  Codex CLI or Gemini CLI. They use the command line (`photolifeboat-cli.exe` on Windows), which can give all
  its answers as JSON.
- **Desktop chat apps that support MCP** (the Model Context Protocol). Photo Lifeboat has a built-in MCP
  server, so the app gets tools such as `list_drives`, `recover` and `contact_sheet`.

A chat in a web browser can't reach your card. You can still upload a few rescued photos to one and ask what
it thinks.

## Before you let an AI look at your photos

**When an AI assistant looks at a photo, that photo is sent to the company that runs the AI.** Photo
Lifeboat itself never sends anything anywhere. It only happens when the assistant views a photo, its
preview or a contact sheet. The guide built into the program tells the assistant to ask you first. If you'd
rather it didn't look, say so: it can still run everything and read the numbers and the automatic checks,
and you can look at the photos yourself.

## The safety rules

Photo Lifeboat exists because an AI assistant wiped an SD card. So:

- **Nothing in the program can write to a card.** There is no format, repair or delete command for an
  assistant to run by mistake. The card is only read, once, to make a copy, and everything else works on the
  copy.
- The program refuses to save the copy onto the card itself, or onto any drive directly (only into a file in
  a folder). It also refuses to treat the computer's own system disk as a card.
- Recovery refuses to run directly on a drive. It always works on the copy.
- The built-in guide (`photolifeboat-cli guide`, or the `read_guide` tool) tells the assistant never to
  format, repair or write to the card in any other way, and to explain each step to the person.

## Command line

On Windows the program is `photolifeboat-cli.exe` in the folder Photo Lifeboat was installed to (normally
`%LOCALAPPDATA%\Programs\Photo Lifeboat`). From source it is `photolifeboat`. Point your assistant at it and
ask it to run `photolifeboat-cli guide` first.

| Command | What it does |
|---|---|
| `guide` | The rules and steps for an assistant, in order. |
| `drives --json` | Attached drives, with size, name, and whether each is removable or holds the system. |
| `clone <drive> <file> --json` | Copies the card into an image file, read-only. Progress comes as JSON lines; the last line has the result. On Windows, one permission prompt needs a click on Yes. |
| `recover <image> --out <folder> --json --progress` | The recovery. Progress comes as JSON lines on standard error; the summary is one JSON object on standard output. |
| `pilot <image> --out <folder>` | The same recovery, then takes decisions on close calls as JSON lines on standard input. |
| `sheet <folder> --json` | Contact sheets: numbered grids of 20 rescued photos each, and which file each number is. |
| `check <files>` | The final automatic check on photo files. |

Exit codes: 0 done, 1 a check or copy failed, 2 bad arguments (for example a drive was given where a file was
expected), 3 the image can't be read as a card, 4 recovered but not everything could be saved (usually a full
disk).

## MCP server

`photolifeboat-cli mcp` runs the MCP server on standard input and output. For the Claude desktop app, add it
to `claude_desktop_config.json` (Settings, Developer, Edit config); other MCP apps have a similar setting:

```json
{
  "mcpServers": {
    "photo-lifeboat": {
      "command": "C:\\Users\\YOU\\AppData\\Local\\Programs\\Photo Lifeboat\\photolifeboat-cli.exe",
      "args": ["mcp"]
    }
  }
}
```

Its tools: `read_guide`, `list_drives`, `copy_card`, `recover`, `job_status`, `results`, `close_calls`,
`show_close_call`, `decide_close_call`, `contact_sheet`, `view_photo` and `check_photos`. Copying and
recovering run in the background, and `job_status` reports progress. The pictures tools return images
the assistant can see directly.

## What a good session looks like

1. The assistant asks you to plug the card in, runs `drives` before and after, and checks with you that the
   new drive is the card (by its name and size).
2. It copies the card to your computer's own disk, and asks you to click Yes on the Windows prompt.
3. It runs the recovery and tells you how it's going.
4. It asks whether it may look at the photos. If you say yes, it settles the close calls and looks over the
   contact sheets for anything that came back wrong.
5. It tells you where your photos are, what couldn't be saved and why, and reminds you to keep the card as
   it is until you're happy with what you've got.
