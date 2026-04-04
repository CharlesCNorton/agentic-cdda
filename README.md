# Cataclysm: DDA — Terminal-Only Fork

Stripped fork of [CleverRaven/Cataclysm-DDA](https://github.com/CleverRaven/Cataclysm-DDA) built for headless, text-only play via an LLM agent over tmux.

## What this is

A trimmed copy of CDDA with everything removed that isn't needed for terminal gameplay or source-level modification. No tilesets, no sounds, no Android, no CI, no screenshots. Just the engine, the game data, and a Python wrapper that strips ASCII map output into clean text so a language model can play the game without visual rendering.

## What's included

| Path | Purpose |
|---|---|
| `src/` | C++ game engine source |
| `data/json/` | Core game content (items, monsters, terrain, recipes) |
| `data/mods/` | Mod content |
| `data/raw/` | Keybindings and raw definitions |
| `data/font/` | Terminus terminal font |
| `Makefile` / `CMakeLists.txt` | Build system |
| `wrapper.py` | Screen parser for text-only play |

## wrapper.py

Sits between tmux and the caller. Captures the ncurses screen, strips map tiles and minimap, returns only text: status bars, descriptions, menus, dialogue, inventory.

```
# Capture current screen as text
python3 wrapper.py

# Send a key then capture
python3 wrapper.py do k

# Send a key without capturing
python3 wrapper.py send Enter
```

Detects screen mode automatically (game, look, inventory, dialogue, extended description, surroundings, messages) and routes through the appropriate parser.

## Setup

1. Build the terminal-only binary on Linux:
   ```
   make TILES=0 SOUND=0 RELEASE=1 LOCALIZE=1 -j$(nproc)
   ```
   Or download a `cdda-linux-terminal-only-x64` release from upstream.

2. Launch inside tmux:
   ```
   tmux new-session -d -s cdda -x 80 -y 24 ./cataclysm
   ```

3. Play through the wrapper:
   ```
   python3 wrapper.py         # read screen
   python3 wrapper.py do x    # look mode
   python3 wrapper.py do e    # extended description
   python3 wrapper.py do i    # inventory
   python3 wrapper.py do V    # surroundings list
   ```

## License

Cataclysm: DDA is licensed under [CC BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0/). This fork retains that license. See `LICENSE.txt` for details.

Terminus Font is licensed under the SIL Open Font License. See `LICENSE-OFL-Terminus-Font.txt`.
