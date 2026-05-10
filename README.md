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

Sits between tmux and the caller. Captures the ncurses screen, strips map tiles and minimap, returns only text: status bars, descriptions, menus, dialogue, inventory, confirmations, postmortem screens, and bordered popups.

```
# Capture current screen as text
python3 wrapper.py

# Send a key then capture
python3 wrapper.py do k

# Send a key without capturing
python3 wrapper.py send Enter
```

Detects screen mode automatically (game, main menu, look, inventory, dialogue, extended description, surroundings, messages, confirmation prompts, death/postmortem, loading, popups) and routes through the appropriate parser.

## cdda.py

`cdda.py` is the Windows-side launcher for `wrapper.py`. By default it launches `wrapper.py` from the same directory as `cdda.py` (translated to a `/mnt/<drive>/...` WSL path), and auto-detects a running WSL distro (preferring `Ubuntu`, then `WSLExperiments`, then any other running distro). Override any default with the env vars below.

Environment overrides:

- `CDDA_WRAPPER_MODE=wsl|native`
- `CDDA_WRAPPER_CMD=<full command>` to bypass the built-in modes entirely
- `CDDA_WSL_DISTRO`, `CDDA_WSL_PYTHON`, `CDDA_WSL_WRAPPER`
- `CDDA_NATIVE_PYTHON`, `CDDA_NATIVE_WRAPPER`
- `CDDA_TMUX_SESSION`, `CDDA_LAST_CAPTURE` forwarded to `wrapper.py`

Pass `--json` (or `-j`) before any subcommand to get a structured response instead of prose. Useful when an agent is consuming the output programmatically.

```
python cdda.py doctor              # smoke-test WSL + tmux + wrapper.py
python cdda.py --json status       # chargen state as JSON
python cdda.py trait_select Asthmatic  # find + toggle a trait, verify color flipped
python cdda.py trait_state         # dump selected positives/negatives
```

## Per-tab keybindings (chargen)

Bindings differ between the chargen tabs and the chargen-adjacent menus. The wrapper exposes high-level commands that paper over most of these, but if you're driving raw the cheat sheet is:

| Screen | Up/Down | Left/Right | Enter | Tab / BTab | `f` / `/` | `r` | `?` |
|---|---|---|---|---|---|---|---|
| Main menu | move within submenu | switch top-level menu | confirm | — | — | — | — |
| World creator | move row | adjust slider | confirm input edit | — | — | — | help |
| Mod manager | move within pane | switch pane | **add/remove mod** | switch tab | filter | — | help |
| Chargen `POINTS` / `SCENARIO` / `PROFESSION` / `BACKGROUND` / `SKILLS` | move within list | — | (no-op) | switch chargen tab | filter | reset filter | help |
| Chargen `STATS` | switch stat | adjust value | (no-op) | switch chargen tab | — | — | help |
| Chargen `TRAITS` | move within pane | **switch pane** (positive ↔ negative ↔ cosmetic) | **toggle trait** | switch chargen tab | filter | reset filter | help |
| Chargen `DESCRIPTION` | switch field | adjust value (gender, height) | edit field via popup | switch chargen tab | — | — | help |

Two pitfalls worth flagging:

- **`Esc` in chargen pops "Return to main menu?"** — there is no plain back-out. Use `cdda.py back` (alias for BTab) to step backward through tabs without losing the character. `cdda.py bail` will hammer through that confirm and is fine *only* if you actually want to abandon the character.
- **In `TRAITS`, the chargen filter applies to the focused pane only** — if you filter for a negative trait while focus is on the positive pane, it shows nothing. The wrapper's `trait_select NAME` handles the pane-switching for you.

## Setup

1. Have a terminal-only `cataclysm` binary available inside WSL.
   ```
   /path/to/cataclysm --version
   ```
   The tested path for this repo was an existing WSL binary launched from its own build directory. This trimmed checkout contains the wrapper and game source, but it is not documented here as a guaranteed standalone build target.

2. Launch inside tmux:
   ```
   tmux new-session -d -s cdda -x 80 -y 24 'cd /path/to/cdda && ./cataclysm'
   ```
   If you use a different session name, set `CDDA_TMUX_SESSION=<name>`.

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
