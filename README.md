# ROM Library

ROM Library is a Decky plugin that brings your console and arcade games into
Steam's game mode.

- Present console games like real Steam games, with artwork, stats, history and more.
- Play a game or change its settings from its page, without complex frontend
  menus.
- Add your favourite games to Steam's main library.
- Change a setting or quit a game directly from the Decky context menu.

## Get it

Install ROM Library in Decky from this URL:

```
https://github.com/dbarth/decky-rom-library/releases/latest/download/rom-library.zip
```

![ROM Library's games on Steam's home](assets/home.png)

## Install

ROM Library needs:

- Decky Loader 3, tested with 3.2.10-pre1.
- An emulator for each system, for example installed by Loadout.
- The games in `~/Emulation/roms/<system>`, such as `~/Emulation/roms/switch`,
  with [ES-DE's system names](https://gitlab.com/es-de/emulationstation-de/-/blob/master/USERGUIDE.md#supported-game-systems).
  Games on an SD card are found too, in `Emulation/roms/<system>` on the card, such as
  `/run/media/steamos/MicroSD/Emulation/roms/switch`.

1. In Decky's settings, open General and turn on Developer mode.
2. Open Developer. Under Install Plugin from URL, enter the URL of Get it and
   select Install.

To update, install from the same URL again.

## What it does

### Library

- Present games (ROMs) as a grid of tiles, with Steam's capsules and controls.
- Group them per console system, as a list of tabs, using L1 and R1 to navigate.
- A opens a game's page, and X adds the game to Steam's library or
  takes it out.
- Artwork from the libretro thumbnails, SteamGridDB or ES-DE's scraped media.
- The Emulators tab presents installed emulators, can hide or show them in the Steam library and can also uninstall an unused emulator.

![The arcade games in ROM Library](assets/library.png)

### Game page

- Games are presented with a choice of visuals, tiles, and animated backgrounds.
- Show contextual game information like play time or ROM size on the first page.
- When available, display game compatibility information, history, tips and tricks for arcade games on a second page.
- The gear icon opens the game's Properties, general or console specific, for example:
  - Eden: language, region, console mode, resolution, graphics, A and B swap
    and gyroscope.
  - Dolphin: aspect ratio, resolution, renderer, Wii Remote layout and
    gyroscope.
  - RetroArch: bezel, run-ahead and rewind.

![A game's page](assets/game.png)

![A Switch game's Properties](assets/properties.png)

### Decky menu

- The quick-access menu is context-aware:
  - In game mode, it provides access to Properties or helps quit the game cleanly.
  - In library mode, it has shortcuts for adding a game or emulator.

### Systems and emulators

ROM Library builds on ES-DE 3.5's list of 195 systems, with their file extensions
and their emulators, and starts each game as ES-DE would. ES-DE need not be
installed. When it is, ROM Library also reads its ROM folder, its scraped names
and media, and the emulator chosen per system or game.

## History and roadmap

I made ROM Library for playing on the SteamOS ARM port.
Loadout installs the emulators and stops there. The other frontends were
too complex to set up or to use. I wanted my favourite games in Steam's
library, so I wrote this plugin. I have tested it mostly on my AYN Odin 2 Portal.
The goal is to keep it simple, light and easy to use, to play right away.

Next:

- Test on more handhelds, the Steam Deck included.
- More per-game settings, for example PPSSPP's and Flycast's.
- ROM Library's Properties in Steam's own game menu.

## Build

```sh
npm install
tools/deploy.sh user@host    # builds out/rom-library.zip and installs it on the device
tools/deploy.sh -            # builds the zip only
for t in src/tests/test_*.py; do python3 -I "$t"; done
```

To iterate quickly on the UI, I also made [decky-vite](https://github.com/dbarth/decky-vite), to see
changes instantly on screen and in a realistic mock environment.

## License

MIT. ES-DE's files in `src/backend/defaults/esde/` are under ES-DE's MIT license.
