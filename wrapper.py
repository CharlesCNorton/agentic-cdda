#!/usr/bin/env python3
"""
CDDA tmux screen parser.
Strips ASCII map tiles and minimap, preserves all text content.

Usage (from WSL):
    wrapper.py                # capture screen, parse, print text
    wrapper.py do KEY...      # send key(s), wait for change, capture+parse
    wrapper.py do_tracked K   # do + report {moved, blocked_reason}
    wrapper.py send KEY...    # send key(s), no capture (small inter-key delay)
    wrapper.py diff           # capture, show only lines changed since last
    wrapper.py diff do KEY    # send, wait, capture, show only changes
    wrapper.py raw            # dump raw tmux pane (for debugging)
    wrapper.py bail           # hammer Escape until main menu or game screen
    wrapper.py batch K1,K2    # send comma-separated keys with adaptive waits
    wrapper.py status         # in chargen, DESCRIPTION tab content (auto tab back)
    wrapper.py filter TEXT    # in chargen, open filter and commit
    wrapper.py trait_select N # find + toggle trait, verify selection flipped
    wrapper.py trait_state    # dump selected positive/negative traits
    wrapper.py parse_fixture P# parse a saved capture file (test fixtures)

  Atomic gameplay actions (one wrapper call replaces a multi-prompt menu chain):
    wrapper.py pickup [NAME]     # g + mark + confirm
    wrapper.py examine DIR       # e + direction
    wrapper.py consume [NAME]    # E + first match
    wrapper.py sleep [HOURS]     # $ + Y + alarm + auto-dismiss distractions
    wrapper.py wait SPEC         # | + w + time-key, auto-dismiss distractions
                                 #   (SPEC: 1m, 5m, 30m, 1h, 2h, 3h, 6h,
                                 #    daylight, noon, night, midnight)
    wrapper.py character         # JSON dump of stats/HP/needs/wielded
    wrapper.py keys [MODE]       # keybinding cheat sheet (game, chargen,
                                 #   mod_manager, distraction)

Add --json before any subcommand to get a structured response instead of
prose. Useful for agentic callers that want to grep state programmatically.
"""
import json
import os
import re
import subprocess
import sys
import time

SESSION = os.environ.get('CDDA_TMUX_SESSION', 'cdda')

BOX_VERT = '\u2502'
BOX_TL = '\u250c'
BOX_TR = '\u2510'
BOX_BL = '\u2514'
BOX_BR = '\u2518'
BOX_L = '\u251c'
BOX_R = '\u2524'
BOX_H = '\u2500'
BOX_T = '\u252c'
BOX_B = '\u2534'
BOX_X = '\u253c'
SELECTED_MARK = '\u00bb'

# Map/minimap characters. Includes @ (player/NPC map marker).
# The is_map_segment() function special-cases lone @ to preserve it in
# compass directions and NPC lists.
MAP_CHARS = frozenset(
    '.#@>{}+"*F~%^v'
    + BOX_VERT + BOX_TL + BOX_TR + BOX_BL + BOX_BR + BOX_L + BOX_R
    + BOX_H + BOX_T + BOX_B + BOX_X
    + '\u2550\u2551\u2554\u2557\u255a\u255d'
)

BORDER = BOX_VERT + BOX_TL + BOX_TR + BOX_BL + BOX_BR + BOX_L + BOX_R + BOX_H + BOX_T + BOX_B + BOX_X

LAST_CAPTURE = os.environ.get('CDDA_LAST_CAPTURE', '/tmp/cdda_last_capture.txt')
STOP_MODES = {'confirm', 'death', 'keybindings', 'locked', 'loading', 'main_menu', 'pause_menu', 'popup'}
STATUS_TAIL_MARKERS = (
    'Focus:', 'Move:', 'Power:', 'Safe:', 'Activity:', 'Weary Malus:',
    'Thirst:', 'Hunger:', 'Weight:', 'Temperature:', 'Weather:',
    'Moon:', 'Date:', 'Time:', 'Wind:',
)


class TmuxError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Core I/O
# ---------------------------------------------------------------------------

def run_tmux(*args):
    result = subprocess.run(
        ['tmux', *args],
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
    )
    if result.returncode == 0:
        return result

    detail = (result.stderr or result.stdout or '').strip()
    if not detail:
        detail = f'tmux {" ".join(args)} failed with exit code {result.returncode}'
    raise TmuxError(detail)


def capture_raw():
    return run_tmux('capture-pane', '-t', SESSION, '-p').stdout


def capture_colored():
    """Capture pane with ANSI escape codes preserved (-e). Use when you
    need to know which cells are highlighted/colored — e.g. to detect
    chargen trait selection state, which CDDA expresses only via color."""
    return run_tmux('capture-pane', '-t', SESSION, '-e', '-p').stdout


_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')


def strip_ansi(text):
    return _ANSI_RE.sub('', text)


def colored_runs(text):
    """Yield (color_code, run_text) tuples for a colored string. color_code
    is the SGR code preceding the run ('' for default/uncolored)."""
    pos = 0
    current = ''
    out = []
    for m in _ANSI_RE.finditer(text):
        if m.start() > pos:
            out.append((current, text[pos:m.start()]))
        current = m.group(0)
        pos = m.end()
    if pos < len(text):
        out.append((current, text[pos:]))
    return out


# CDDA marks a chargen trait you have selected with bold green for positives
# and bold red for negatives. When the cursor is also on a selected trait an
# extra `[44m` (blue background) appears between the color and the text:
#   selected-positive          : \x1b[1m\x1b[32mFast Reader
#   selected-positive + cursor : \x1b[1m\x1b[32m\x1b[44mFast Reader
# The optional `(?:\x1b\[44m)?` accommodates both forms.
_BOLD_GREEN_RUN = re.compile(r'\x1b\[1m\x1b\[32m(?:\x1b\[44m)?([^\x1b\n]+)')

# CDDA paints the Lifestyle/Knowledge/Offense/Defense/Social ratings in
# bold green when they're "strong"/"powerful"/"overpowered" — these aren't
# traits and would otherwise pollute the selected-trait list. Defense-in-depth:
# the parser also drops anything from the Summary-bar line entirely.
_SUMMARY_RATINGS = frozenset({
    'weak', 'underpowered', 'average', 'strong', 'powerful', 'overpowered',
    'fragile', 'sturdy', 'overwhelming',
})

# Selected NEGATIVE traits are red (NOT bold by default — CDDA renders the
# selected-negative state as plain `[31m`, not `[1m[31m`). The `[1m` prefix
# only appears when the cursor is also on the trait, in which case the
# escape sequence becomes `[1m[31m[44m`. Allow both with optional bold and
# optional blue-background:
#   selected-negative          : \x1b[31mAsthmatic
#   selected-negative + cursor : \x1b[1m\x1b[31m\x1b[44mAsthmatic
_BOLD_RED_RUN = re.compile(
    r'(?:\x1b\[1m)?\x1b\[31m(?:\x1b\[44m)?([^\x1b\n]+)'
)


def _strip_summary_lines(colored_raw):
    """Remove the Summary | Lifestyle: ... line(s) from the colored capture
    so the trait-color regexes can't match the rating words on it."""
    out_lines = []
    for line in colored_raw.split('\n'):
        plain = strip_ansi(line)
        if 'Summary' in plain and 'Lifestyle' in plain:
            continue
        out_lines.append(line)
    return '\n'.join(out_lines)


def selected_traits_from_colored(colored_raw, include_negatives=True):
    """Names of currently-selected chargen traits, extracted from a colored
    capture. CDDA renders selected positive traits in bold green and selected
    negatives in bold red. Arrows, scrollbars, the cursor highlight, and the
    Summary-bar rating words are filtered out.

    If include_negatives is True (default), the returned list contains both;
    callers needing to distinguish should use selected_trait_panes()."""
    scoped = _strip_summary_lines(colored_raw)
    seen = set()
    out = []
    runs = list(_BOLD_GREEN_RUN.findall(scoped))
    if include_negatives:
        runs.extend(_BOLD_RED_RUN.findall(scoped))
    for text in runs:
        name = text.strip()
        if len(name) <= 1 or name in ('^', 'v'):
            continue
        if name.lower() in _SUMMARY_RATINGS:
            continue
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def selected_trait_panes(colored_raw):
    """Dict {'positive': [...], 'negative': [...]} of currently-selected
    chargen traits, partitioned by which pane they live in (positive=green,
    negative=red)."""
    scoped = _strip_summary_lines(colored_raw)
    def collect(regex):
        seen = set()
        out = []
        for text in regex.findall(scoped):
            name = text.strip()
            if len(name) <= 1 or name in ('^', 'v'):
                continue
            if name.lower() in _SUMMARY_RATINGS:
                continue
            if name in seen:
                continue
            seen.add(name)
            out.append(name)
        return out
    return {
        'positive': collect(_BOLD_GREEN_RUN),
        'negative': collect(_BOLD_RED_RUN),
    }


def send_keys(*keys, delay_ms=20):
    """Send keys to tmux. Single characters use literal mode (-l) to avoid
    tmux interpreting ; and other metacharacters. Small inter-key delay
    avoids races where CDDA hasn't processed the previous key before the
    next arrives."""
    for key in keys:
        if len(key) == 1:
            run_tmux('send-keys', '-t', SESSION, '-l', key)
        else:
            run_tmux('send-keys', '-t', SESSION, key)
        if delay_ms:
            time.sleep(delay_ms / 1000.0)


def wait_for_change(before=None, timeout=3.0, interval=0.15,
                    settle_ms=200, settle_timeout=1.0):
    """Poll until the screen content changes, then keep polling until it
    stabilizes for settle_ms before returning. Prevents the next key in a
    drive sequence from racing the redraw of the current one."""
    if before is None:
        before = capture_raw()
    elapsed = 0.0
    while elapsed < timeout:
        time.sleep(interval)
        elapsed += interval
        latest = capture_raw()
        if latest != before:
            # Wait until screen is stable for settle_ms (no further changes).
            stable = latest
            stable_for = 0.0
            settle_target = settle_ms / 1000.0
            settle_elapsed = 0.0
            settle_interval = 0.05
            while stable_for < settle_target and settle_elapsed < settle_timeout:
                time.sleep(settle_interval)
                settle_elapsed += settle_interval
                cur = capture_raw()
                if cur == stable:
                    stable_for += settle_interval
                else:
                    stable = cur
                    stable_for = 0.0
            return stable
    return before


def drive_keys(keys, timeout=3.0, auto_dismiss=True):
    """Send keys one at a time, waiting for each change and stopping on
    blocking modal states so later keys do not spill into the wrong screen.
    When auto_dismiss is True (default), interrupting overlays that have a
    safe answer (Y on "Stop moving items?" / "Really step into raspberry
    bush?" / "You are freezing!", Escape on a stray pause menu or Actions
    overlay) get handled before each key, so a long key chain doesn't get
    silently swallowed by the first nuisance prompt that fires mid-walk."""
    raw = capture_raw()
    for key in keys:
        if auto_dismiss:
            raw = dismiss_blocking_overlays(raw)
        before = raw
        send_keys(key)
        raw = wait_for_change(before=before, timeout=timeout)
        if detect_mode(raw) in STOP_MODES:
            break
    return raw


# Confirms that have an obviously-safe Y answer when they fire during
# normal play. Each entry is a substring (case-sensitive) that, when found
# anywhere in the current capture together with `[Y]es [N]o`, gets a Y.
# All of these were observed wedging Claude's runs by silently freezing
# every subsequent keystroke until manually dismissed.
SAFE_Y_CONFIRMS = (
    'Stop moving items',
    'Stop hauling',
    'Really step into',          # raspberry bush, brambles, glass, etc.
    'You are freezing',          # case-sensitive Y to stop hauling/moving
    'Stop reading',
    'Stop crafting',
    'Stop construction',
    'Stop disassembling',
)

# In-grid overlay boxes that the box-vert mode-detector misses because they
# render as a small panel inside the gameplay grid rather than as a full-
# screen popup. Detected by literal-string match against the capture; the
# fix is always Escape.
OVERLAY_ESCAPE_MARKERS = (
    'MAIN MENU',                  # in-game pause menu
    '< Actions >',                # NPC interaction wheel
    'Wield item',                 # w menu when fired by accident
    'Wear item',                  # W menu when fired by accident
    'Use item',                   # ' menu (apostrophe collides with bash)
    'Eat',                        # E menu when fired by accident
    'Take off',                   # T overlap with travel-to
    'Construction',               # * menu when fired by accident
    'Safe mode manager',          # pause-menu sub-page
    'Auto pickup manager',
    'Distractions manager',
)


def dismiss_blocking_overlays(raw=None, max_attempts=4):
    """Walk through the current capture looking for SAFE_Y_CONFIRMS and
    OVERLAY_ESCAPE_MARKERS, answering or escaping them in turn. Returns
    the post-dismiss capture. Bounded by max_attempts so a popup that
    refuses to close (e.g. one that re-fires from the same state) doesn't
    spin forever."""
    if raw is None:
        raw = capture_raw()
    for _ in range(max_attempts):
        text = strip_ansi(raw)
        # Y-answerable confirms first; they're cheap and they're what
        # actually freezes long walks.
        if '[Y]es' in text and any(s in text for s in SAFE_Y_CONFIRMS):
            send_keys('Y')
            raw = wait_for_change(before=raw, timeout=1.0)
            continue
        # Stray menus / item overlays — Escape closes them.
        if any(marker in text for marker in OVERLAY_ESCAPE_MARKERS):
            send_keys('Escape')
            raw = wait_for_change(before=raw, timeout=1.2)
            continue
        return raw
    return raw


def safemode_status(raw=None):
    """Return True if the sidebar shows 'Safe: On'. Cheap; uses the same
    capture pattern as the rest of the wrapper."""
    if raw is None:
        raw = capture_raw()
    return 'Safe: On' in strip_ansi(raw)


def safemode_off(force=False):
    """Toggle CDDA's global safe mode off via the `!` keybinding. CDDA
    auto-re-enables safe mode whenever a hostile is in awareness range,
    so callers walking past a known monster need to call this between
    moves. With force=True we always send `!`; otherwise we only send it
    when 'Safe: On' is currently visible. Returns the new safemode state
    as a bool (True = still on)."""
    if not force and not safemode_status():
        return False
    send_keys('!')
    time.sleep(0.3)
    return safemode_status()


def cancel_activity():
    """Interrupt whatever long-running Activity (haul, craft, sleep, wait)
    is currently consuming the player's turn budget. CDDA prompts a
    case-sensitive Y to stop; we send the standard '.' interrupt then
    answer Y to any 'Stop X?' confirm that follows."""
    send_keys('.')
    time.sleep(0.3)
    raw = capture_raw()
    text = strip_ansi(raw)
    if '[Y]es' in text and 'Stop' in text:
        send_keys('Y')
        wait_for_change(before=raw, timeout=1.0)


# ---------------------------------------------------------------------------
# Mode detection
# ---------------------------------------------------------------------------

def looks_like_popup(text):
    lines = [line.rstrip() for line in text.split('\n') if line.strip()]
    if len(lines) < 3:
        return False
    first = lines[0].lstrip()
    if not first.startswith(BOX_TL):
        return False
    bordered = sum(
        1 for line in lines
        if line.lstrip().startswith((BOX_TL, BOX_VERT, BOX_BL))
    )
    return bordered >= max(3, len(lines) // 2)


def detect_mode(text):
    # Overlays first: a Y/N confirm or specific dialog can sit on top of any
    # underlying mode (main menu, chargen, game). If we let the structural
    # checks run first they classify by the visible underlying screen and
    # the overlay is invisible to the caller.
    if re.search(r'\[Y\]es\s+\[N\]o', text):
        return 'confirm'
    if 'Search:' in text and ('SCENARIO' in text or 'PROFESSION' in text):
        return 'chargen_search'
    # 'You must complete the achievement ...' appears in two distinct places:
    # (a) as the *body* of a locked-scenario/profession description while the
    # chargen tabs are still visible, and (b) in a standalone popup. Only
    # treat it as 'locked' when chargen's structural markers are absent —
    # otherwise wrappers like trait_select / chargen_filter would refuse to
    # operate on a chargen screen that just happens to highlight a locked
    # entry.
    if ('You must complete the achievement' in text
            and not ('SCENARIO' in text and 'PROFESSION' in text and 'STATS' in text)):
        return 'locked'
    if 'Choose a preset character template' in text:
        return 'popup'
    if 'Pick a world to enter game' in text or 'World selection' in text:
        return 'popup'

    # Death / postmortem / loading are also overlay-shaped, check before main
    # screens since the underlying state may still match.
    if 'The End' in text and 'In memory of:' in text:
        return 'death'
    if 'Your scores' in text and 'ACHIEVEMENTS' in text and 'KILLS' in text:
        return 'postmortem'
    if 'Loading files' in text or 'Verifying' in text or 'Finalizing' in text:
        return 'loading'

    # Structural modes
    if 'SCENARIO' in text and 'PROFESSION' in text and 'STATS' in text:
        # CDDA marks the active chargen tab as `<│TAB│>` (angle brackets +
        # box-vert separators), not `[TAB]`. Both detect_chargen_tab and
        # this branch must agree on the marker.
        if f'<{BOX_VERT}TRAITS{BOX_VERT}>' in text:
            return 'chargen_traits'
        return 'chargen'
    if '< Look around >' in text:
        return 'look'
    if 'Inventory' in text and ('Bulk Volume' in text or 'Total Weight' in text):
        return 'inventory'
    if 'Dialogue:' in text or 'Your response:' in text:
        return 'dialogue'
    if '[c] Creature' in text and '[t] Terrain' in text:
        return 'extended'
    if 'KEYBINDINGS' in text and '[f] Filter' in text:
        return 'keybindings'
    if 'MAIN MENU' in text and 'Save and quit' in text:
        return 'pause_menu'
    if 'Load character from "' in text and 'Back to Main Menu' in text:
        return 'load_menu'
    if re.search(r'Items \(\d+\)', text) and 'Monsters' in text:
        return 'surroundings'
    if 'Press f, F, or / to filter' in text and re.search(r'^\s*[v^]\s', text, re.MULTILINE):
        return 'messages'
    if 'What to do with' in text or 'What do you want to do' in text:
        return 'overlay'
    if 'Select your language' in text:
        return 'lang_menu'
    if (
        'Custom Character' in text or 'Play Now' in text
        or ('[MOTD]' in text and '[Quit]' in text)
        or ('[New Game]' in text and '[Load]' in text and '[Credits]' in text)
    ):
        return 'main_menu'
    if looks_like_popup(text):
        return 'popup'
    return 'game'


# ---------------------------------------------------------------------------
# Segment helpers
# ---------------------------------------------------------------------------

def clean_text(text, extra_strip=''):
    text = text.strip(BORDER + ' ')
    if extra_strip:
        chars = '[' + re.escape(extra_strip) + ']'
        # Strip a leading isolated extra (e.g. compass arrow at start of a message
        # row). Trailing extras are left for _rejoin_wrapped to handle, since a
        # trailing single letter may be a word fragment from a line wrap.
        text = re.sub(r'^' + chars + r'(?=\s|$)', '', text)
        text = text.strip()
    return text


def trim_status_tail(text):
    end = len(text)
    for marker in STATUS_TAIL_MARKERS:
        idx = text.find(marker)
        if idx != -1 and idx < end:
            end = idx
    return text[:end].rstrip('<> ').strip()


def unique_lines(lines):
    out = []
    seen = set()
    for line in lines:
        if not line or line in seen:
            continue
        seen.add(line)
        out.append(line)
    return out


def is_map_segment(s):
    """True if segment is purely map/minimap characters (no readable text).
    A lone @ is kept because it marks the player in compass and NPC lists."""
    stripped = s.strip()
    if stripped == '@':
        return False
    return len(stripped) > 0 and all(c in MAP_CHARS or c == ' ' for c in s)


# ---------------------------------------------------------------------------
# Message extraction (top-of-screen game messages)
# ---------------------------------------------------------------------------

def extract_top_messages(raw):
    """Game messages appear in the first few rows of the left (map) area.
    Extract readable text from there so messages aren't silently lost."""
    msgs = []
    for line in raw.split('\n')[:5]:
        left = line[:37].strip()
        if not left:
            continue
        for seg in re.split(r' {2,}', left):
            seg = seg.strip()
            if not seg or is_map_segment(seg):
                continue
            cleaned = clean_text(seg)
            if cleaned and any(c.isalpha() for c in cleaned):
                msgs.append(cleaned)
    return msgs


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def parse_default(raw):
    """Universal parser: strip map, keep text, prepend messages."""
    out = []

    top_msgs = extract_top_messages(raw)
    for message in top_msgs:
        out.append(f'> {message}')

    seen_msgs = set(top_msgs)

    for line in raw.split('\n'):
        r = line.rstrip()
        if not r.strip():
            continue
        segments = re.split(r' {2,}', r)
        kept = []
        for seg in segments:
            seg = seg.strip()
            if not seg or is_map_segment(seg):
                continue
            cleaned = clean_text(seg)
            if cleaned:
                kept.append(cleaned)
        if kept:
            joined = '  '.join(kept)
            if joined in seen_msgs:
                continue
            out.append(joined)
    return out


def parse_messages(raw):
    """Message log: split left panel (messages) from right panel (sidebar)."""
    msgs, sidebar = [], []
    for line in raw.split('\n'):
        r = line.rstrip()
        if not r.strip():
            continue
        border_col = -1
        for i, ch in enumerate(r):
            if ch == BOX_VERT and 30 <= i <= 40:
                border_col = i
                break
        if border_col > 0:
            left = clean_text(r[:border_col], '^v')
            left = re.sub(r'^\d+\s+seconds?\s+', '', left)
            if left:
                msgs.append(left)
            right_segs = re.split(r' {2,}', r[border_col + 1:])
            kept = []
            for seg in right_segs:
                seg = seg.strip()
                if not seg or is_map_segment(seg):
                    continue
                cleaned = clean_text(seg)
                if cleaned:
                    kept.append(cleaned)
            if kept:
                sidebar.append('  '.join(kept))
        else:
            cleaned = clean_text(r)
            if cleaned and any(c.isalnum() for c in cleaned):
                msgs.append(cleaned)

    msgs = _rejoin_wrapped(msgs)

    out = []
    if msgs:
        out.append('--- Messages ---')
        out.extend(msgs)
    if sidebar:
        out.append('')
        out.append('--- Status ---')
        out.extend(sidebar)
    return out


def _rejoin_wrapped(lines):
    """Join continuation lines so wrapped descriptions stay readable."""
    if not lines:
        return lines
    out = [lines[0]]
    for line in lines[1:]:
        if not line:
            continue
        prev = out[-1]
        # Mid-word wrap: prev ends with a space + single letter (the start of a
        # word that got broken by ncurses line wrap) and the next line begins
        # with a lowercase letter. Concatenate without inserting a space so
        # "strangely v" + "icious lately" becomes "strangely vicious lately".
        if (len(prev) >= 2 and prev[-2] == ' '
                and prev[-1].isalpha()
                and line[0].islower()):
            out[-1] = prev + line
        elif line[0].islower() or line[0] in ',.;:!?)\'"':
            out[-1] += ' ' + line
        else:
            out.append(line)
    return out


def _find_selected(right_lines):
    """Return the name shown in the Identity: line of the details panel."""
    for line in right_lines:
        match = re.match(r'Identity:\s*(.+?)(?:\s*\(male\)|\s*\(female\))', line)
        if match:
            return match.group(1).strip()
    return ''


def parse_chargen(raw):
    """Two-column chargen view with selection marked in the left column."""
    left, right = [], []
    tabs = ''
    seen_summary = False

    for line in raw.split('\n'):
        r = line.rstrip()
        if not r.strip():
            continue

        if 'SCENARIO' in r and 'PROFESSION' in r:
            active = re.search(r'\[([A-Z]+)\]', r)
            active_tab = active.group(1) if active else ''
            tab_names = re.findall(r'[A-Z]{3,}', r)
            tabs = ' | '.join(
                f'[{tab}]' if tab == active_tab else tab for tab in tab_names
            )
            continue

        if 'Summary |' in r or 'Lifestyle:' in r:
            seen_summary = True
            left.append(clean_text(r))
            continue
        if '[s] sort' in r or '[f, F' in r:
            continue
        if 'Press ?' in r or 'Press k,' in r or 'Press l,' in r or 'Press TAB' in r:
            continue
        # Skip pre-Summary header lines (the chargen meta-title that always
        # reads "Survivor" regardless of selected scenario/profession).
        if not seen_summary:
            continue

        divider = -1
        for i, ch in enumerate(r):
            if ch == BOX_VERT and 35 <= i <= 45:
                divider = i
                break

        if divider > 0:
            left_part = clean_text(r[:divider], '^v')
            right_part = clean_text(r[divider + 1:], '^v')
            if left_part:
                left.append(left_part)
            if right_part:
                right.append(right_part)
        else:
            cleaned = clean_text(r, '^v')
            if cleaned and any(c.isalnum() for c in cleaned):
                indent = len(r) - len(r.lstrip())
                if indent > 30:
                    right.append(cleaned)
                else:
                    left.append(cleaned)

    right = _rejoin_wrapped(right)

    locked = any('You must complete' in line or 'to unlock' in line for line in right)
    selected = _find_selected(right)
    lock_tag = ' [LOCKED]' if locked else ''
    if selected:
        marked = []
        for line in left:
            bare = line.strip()
            if bare == selected or selected.startswith(bare):
                marked.append(f'> {bare}{lock_tag}')
            else:
                marked.append(f'  {bare}')
        left = marked

    out = []
    if tabs:
        out.append(tabs)
        out.append('')
    out.extend(left)
    if right:
        out.append('')
        out.append('---')
        out.extend(right)
    return out


def parse_chargen_traits(raw):
    """Three-column trait screen with the selected description at the bottom."""
    col1, col2, col3 = [], [], []
    description = []
    tabs = ''
    seen_summary = False

    for line in raw.split('\n'):
        r = line.rstrip()
        if not r.strip():
            continue

        if 'SCENARIO' in r and 'PROFESSION' in r:
            active = re.search(r'\[([A-Z]+)\]', r)
            active_tab = active.group(1) if active else ''
            tab_names = re.findall(r'[A-Z]{3,}', r)
            tabs = ' | '.join(
                f'[{tab}]' if tab == active_tab else tab for tab in tab_names
            )
            continue

        if 'Summary' in r and 'Lifestyle' in r:
            seen_summary = True
            continue
        if 'sort:' in r or 'filter' in r or 'Press ?' in r:
            continue
        # Skip pre-Summary header lines (chargen meta-title "Survivor").
        if not seen_summary:
            continue

        if r.count(BOX_VERT) >= 2:
            # Row layout: │ POS_TRAIT │ NEG_TRAIT │ COSMETIC │
            # Splitting on │ yields: ['', ' POS ', ' NEG ', ' COSMETIC ', '']
            # The leading empty (before the first │) and trailing empty
            # (after the last │) must both be discarded so the three
            # cells align with the three pane labels below.
            parts = [clean_text(p, '^v') for p in r.split(BOX_VERT)]
            cells = [p for p in parts if p or len(parts) == 1]
            # Re-derive without filtering empties: keep order but drop
            # leading/trailing empties only.
            stripped = list(parts)
            while stripped and not stripped[0]:
                stripped.pop(0)
            while stripped and not stripped[-1]:
                stripped.pop()
            c1 = stripped[0] if len(stripped) > 0 else ''
            c2 = stripped[1] if len(stripped) > 1 else ''
            c3 = stripped[2] if len(stripped) > 2 else ''
            if c1:
                col1.append(c1)
            if c2:
                col2.append(c2)
            if c3:
                col3.append(c3)
        elif any(c.isalpha() for c in r):
            stripped = r.strip()
            if stripped and '--- Selection' not in stripped:
                description.append(stripped)

    out = []
    if tabs:
        out.append(tabs)
        out.append('')
    if col1:
        out.append('--- Positive ---')
        out.extend(f'  {trait}' for trait in col1)
    if col2:
        out.append('--- Negative ---')
        out.extend(f'  {trait}' for trait in col2)
    if col3:
        out.append('--- Cosmetic ---')
        out.extend(f'  {trait}' for trait in col3)
    if description:
        out.append('')
        out.append('Selected: ' + ' '.join(description))
    return out


def parse_main_menu(raw):
    profiles = []
    menu = []
    notices = []

    for line in raw.split('\n'):
        r = line.rstrip()
        if not r.strip():
            continue

        for cell in re.findall(rf'{BOX_VERT}([^{BOX_VERT}]+){BOX_VERT}', r):
            cleaned = cell.strip()
            if not cleaned or not any(ch.isalnum() for ch in cleaned):
                continue
            selected = cleaned.startswith(SELECTED_MARK)
            cleaned = cleaned.lstrip(SELECTED_MARK + ' ').rstrip()
            if not cleaned or len(cleaned) > 32:
                continue
            prefix = '> ' if selected else '  '
            profiles.append(prefix + cleaned)

        if '[' in r and ']' in r and '[Quit]' in r:
            menu = re.findall(r'\[([^\]]+)\]', r)
            continue

        cleaned = clean_text(r)
        if 'Tip of the day:' in cleaned or 'Bugs?' in cleaned:
            notices.append(cleaned)

    out = []
    if profiles:
        out.append('Profiles:')
        out.extend(unique_lines(profiles))
    if menu:
        if out:
            out.append('')
        out.append('Menu: ' + ' | '.join(menu))
    if notices:
        if out:
            out.append('')
        out.extend(unique_lines(notices))
    return out or parse_default(raw)


def parse_load_menu(raw):
    entries = []
    for line in raw.split('\n'):
        title = re.search(r'(Load character from "[^"]+")', line)
        if title:
            entries.append(title.group(1))
            continue
        option = re.search(r'(\d+\s+.+?\[\d{2}:\d{2}:\d{2}\])', line)
        if option:
            entries.append(re.sub(r'\s+', ' ', option.group(1)).strip())
            continue
        back = re.search(r'(q <- Back to Main Menu)', line)
        if back:
            entries.append(back.group(1))
    return unique_lines(entries) or parse_popup(raw)


def extract_question_candidates(line):
    candidates = []
    normalized = line.replace('(Case Sensitive)', ' ')
    for match in re.finditer(r'[A-Z]', normalized):
        tail = normalized[match.start():]
        qpos = tail.find('?')
        if qpos == -1:
            continue
        candidate = re.sub(r'\s+', ' ', tail[:qpos + 1]).strip()
        word_count = len(candidate.split())
        if 2 <= word_count <= 8 and len(candidate) <= 60:
            candidates.append(candidate)
    candidates.sort(key=lambda text: (len(text), text))
    return candidates


def parse_confirm(raw):
    questions = []
    for line in raw.split('\n'):
        if '?' not in line:
            continue
        candidates = extract_question_candidates(trim_status_tail(line))
        if candidates:
            questions.append(candidates[0])
    questions = unique_lines(questions)

    out = []
    if questions:
        out.extend(questions)
    out.append('Choices: [Y]es / [N]o')
    return out


def parse_death(raw):
    out = []
    if 'The End' in raw:
        out.append('The End')

    for label in ('In memory of:', 'Survived:', 'Kills:'):
        for line in raw.split('\n'):
            if label not in line:
                continue
            fragment = trim_status_tail(line[line.index(label):])
            if fragment:
                out.append(fragment)
            break
    return unique_lines(out) or parse_confirm(raw)


def parse_postmortem(raw):
    tabs = ''
    body = []

    for line in raw.split('\n'):
        r = line.rstrip()
        if not r.strip():
            continue
        if 'ACHIEVEMENTS' in r and 'CONDUCTS' in r and 'SCORES' in r and 'KILLS' in r:
            tab_names = re.findall(r'ACHIEVEMENTS|CONDUCTS|SCORES|KILLS', r)
            tabs = ' | '.join(tab_names)
            continue

        cleaned = clean_text(r)
        if not cleaned or is_map_segment(cleaned):
            continue
        if cleaned == 'Your scores':
            body.append(cleaned)
            continue
        if (
            cleaned.startswith('0/')
            or cleaned.startswith('Read ')
            or cleaned.startswith('Gain ')
            or cleaned.startswith('Better ')
            or cleaned.startswith('Righty ')
            or 'z level' in cleaned
            or 'mechanics skill' in cleaned
            or 'vehicle passenger' in cleaned
            or re.match(r"^[A-Z][A-Za-z0-9'.,!?:;\- ]+$", cleaned)
        ):
            body.append(cleaned)

    body = _rejoin_wrapped(body)

    out = []
    if tabs:
        out.append(tabs)
        out.append('')
    out.extend(unique_lines(body))
    return out or parse_default(raw)


def parse_popup(raw):
    out = []
    for line in raw.split('\n'):
        r = line.rstrip()
        if not r.strip():
            continue

        stripped = r.strip()
        if stripped and all(ch in BORDER + ' ' for ch in stripped):
            continue

        if BOX_VERT in r:
            cells = [clean_text(part) for part in r.split(BOX_VERT)]
            cells = [cell for cell in cells if cell]
            if cells:
                out.extend(cells)
                continue

        cleaned = clean_text(r)
        if cleaned:
            out.append(cleaned)

    return _rejoin_wrapped(unique_lines(out))


def parse_loading(raw):
    lines = []
    for line in raw.split('\n'):
        cleaned = clean_text(line)
        if cleaned and not is_map_segment(cleaned):
            lines.append(cleaned)
    return unique_lines(lines)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

def parse(raw, mode):
    if mode == 'messages':
        return parse_messages(raw)
    if mode == 'chargen':
        return parse_chargen(raw)
    if mode == 'chargen_traits':
        return parse_chargen_traits(raw)
    if mode == 'main_menu':
        return parse_main_menu(raw)
    if mode == 'load_menu':
        return parse_load_menu(raw)
    if mode == 'confirm':
        return parse_confirm(raw)
    if mode == 'death':
        return parse_death(raw)
    if mode == 'postmortem':
        return parse_postmortem(raw)
    if mode == 'keybindings':
        return parse_popup(raw)
    if mode == 'pause_menu':
        return parse_popup(raw)
    if mode in {'overlay', 'popup'}:
        return parse_popup(raw)
    if mode == 'loading':
        return parse_loading(raw)
    return parse_default(raw)


# ---------------------------------------------------------------------------
# Diff support
# ---------------------------------------------------------------------------

def load_prev():
    try:
        with open(LAST_CAPTURE, 'r', encoding='utf-8') as handle:
            return set(handle.read().strip().split('\n'))
    except FileNotFoundError:
        return set()


def save_capture(lines):
    with open(LAST_CAPTURE, 'w', encoding='utf-8') as handle:
        handle.write('\n'.join(lines))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def bail():
    """Hammer Escape until we reach the main menu or game screen. Y/N
    confirms get Y. Works from chargen too: Escape opens a "Return to main
    menu?" confirm which the next iteration answers Y."""
    for _ in range(20):
        raw = capture_raw()
        mode = detect_mode(raw)
        if mode in ('game', 'main_menu'):
            return raw, mode
        if mode == 'confirm':
            send_keys('Y')
            wait_for_change(before=raw, timeout=1.5)
            continue
        send_keys('Escape')
        wait_for_change(before=raw, timeout=1.0)
    raw = capture_raw()
    return raw, detect_mode(raw)


def batch(keys_csv):
    """Send comma-separated keys with adaptive waits between each."""
    keys = [key.strip() for key in keys_csv.split(',') if key.strip()]
    return drive_keys(keys, timeout=1.5)


CHARGEN_TABS = (
    'SCENARIO', 'PROFESSION', 'BACKGROUND',
    'STATS', 'TRAITS', 'SKILLS', 'DESCRIPTION',
)


def detect_chargen_tab(raw):
    """Active chargen tab from the < │NAME│ > markers in the tab row."""
    for tab in CHARGEN_TABS:
        if f'<{BOX_VERT}{tab}{BOX_VERT}>' in raw:
            return tab
    return None


def chargen_status():
    """Tab to DESCRIPTION, capture, and tab back to the originating tab.
    Also probes TRAITS for selection state (CDDA color-marks selected
    traits but doesn't list them on the description tab unless the pane
    is wide enough). Returns the parsed lines plus a selected-traits
    section. Caller must already be in chargen mode."""
    raw = capture_raw()
    mode = detect_mode(raw)
    if not mode.startswith('chargen'):
        raise RuntimeError(f'not in chargen (current mode: {mode})')

    current = detect_chargen_tab(raw)
    if current is None:
        raise RuntimeError('could not detect current chargen tab')

    def go_to(target):
        """Walk Tab/BTab toward target without wrapping past DESCRIPTION (which
        triggers a "Are you SURE you're finished?" finalize confirm) or past
        SCENARIO (which goes nowhere)."""
        target_idx = CHARGEN_TABS.index(target)
        while True:
            now = detect_chargen_tab(capture_raw()) or current
            now_idx = CHARGEN_TABS.index(now)
            if now_idx == target_idx:
                return
            if now_idx < target_idx:
                send_keys('Tab')
            else:
                send_keys('BTab')
            wait_for_change(timeout=1.0)

    # Probe DESCRIPTION
    go_to('DESCRIPTION')
    desc_raw = capture_raw()
    desc_mode = detect_mode(desc_raw)
    desc_lines = parse(desc_raw, desc_mode)

    # Probe TRAITS for color-encoded selection state
    go_to('TRAITS')
    traits_colored = capture_colored()
    selected = selected_trait_panes(traits_colored)

    # Return to originating tab
    go_to(current)

    out = list(desc_lines)
    if selected['positive']:
        out.append('')
        out.append('Selected positive traits: ' + ', '.join(selected['positive']))
    if selected['negative']:
        if not selected['positive']:
            out.append('')
        out.append('Selected negative traits: ' + ', '.join(selected['negative']))
    return out


def chargen_filter(text):
    """Open chargen list filter, type text, commit. On non-TRAITS tabs we
    send TWO Enters: the first closes the filter dialog and lands the cursor
    on the first match; the second is the CONFIRM action that
    src/newcharacter.cpp handles as `reset_scenario()` (or the equivalent
    for the active tab) and is what actually persists the selection into
    spawn-side state. The cursor moving to a row is just visual hover —
    without the second Enter the spawn keeps using the previously committed
    selection, typically the default (Evacuee + Survivor). For TRAITS,
    Enter toggles, so we suppress the second Enter; callers use
    trait_select() which handles toggling explicitly."""
    raw = capture_raw()
    mode = detect_mode(raw)
    if not mode.startswith('chargen'):
        raise RuntimeError(f'not in chargen (current mode: {mode})')

    send_keys('f')
    wait_for_change(before=raw, timeout=2.0)
    for ch in text:
        send_keys(ch)
    send_keys('Enter')
    raw = wait_for_change(timeout=2.0)
    if mode != 'chargen_traits':
        send_keys('Enter')
        raw = wait_for_change(timeout=2.0)
    return raw


def chargen_filter_reset():
    """Clear the chargen list filter (the bound 'r' / 'R' key)."""
    raw = capture_raw()
    send_keys('r')
    return wait_for_change(before=raw, timeout=1.0)


def _dismiss_chargen_popup():
    """Dismiss a 'Nothing found.' popup (or similar transient dialog) sitting
    on top of the chargen list. Escape clears it; if Escape changes nothing
    we fall back to Enter, which some popups want as acknowledgement."""
    raw = capture_raw()
    send_keys('Escape')
    after = wait_for_change(before=raw, timeout=0.6)
    if after == raw:
        send_keys('Enter')
        wait_for_change(timeout=0.6)


def _go_to_chargen_tab(target):
    """Walk Tab/BTab to a target chargen tab without overshooting past
    DESCRIPTION (which would trigger the finalize confirm). Returns True
    on success, False if the active tab couldn't be detected."""
    target_idx = CHARGEN_TABS.index(target)
    for _ in range(len(CHARGEN_TABS) * 2):
        now = detect_chargen_tab(capture_raw())
        if now is None:
            return False
        now_idx = CHARGEN_TABS.index(now)
        if now_idx == target_idx:
            return True
        if now_idx < target_idx:
            send_keys('Tab')
        else:
            send_keys('BTab')
        wait_for_change(timeout=1.0)
    return False


CHARGEN_STAT_NAMES = ('Strength', 'Dexterity', 'Intelligence', 'Perception')


def _read_stat_value(stat_name):
    text = strip_ansi(capture_raw())
    m = re.search(rf'{re.escape(stat_name)}:\s+(\d+)', text)
    return int(m.group(1)) if m else None


def chargen_set_stats(values):
    """Set chargen stats to target values. `values` is a dict keyed by
    'str'/'dex'/'int'/'per' (any subset). For each stat we re-anchor the
    cursor to the top of the stat list and walk down by index, so a single
    drifted Up/Down doesn't compound across stats. After each Left/Right
    we re-read the row's value: if the value didn't move (and the press
    wasn't bouncing off a min/max boundary), the cursor isn't on this stat
    and we abort that stat with whatever it currently reads."""
    if not _go_to_chargen_tab('STATS'):
        raise RuntimeError('could not navigate to STATS tab')

    actual = {}
    for idx, stat_name in enumerate(CHARGEN_STAT_NAMES):
        # Re-anchor: slam Up past the top of the list, then Down to row idx.
        for _ in range(len(CHARGEN_STAT_NAMES) + 2):
            send_keys('Up')
            time.sleep(0.06)
        for _ in range(idx):
            send_keys('Down')
            time.sleep(0.12)

        key = stat_name.lower()[:3]
        actual[key] = _read_stat_value(stat_name)
        if key not in values:
            continue
        target = int(values[key])

        prev = actual[key]
        stuck = 0
        for _ in range(30):
            cur = _read_stat_value(stat_name)
            if cur is None or cur == target:
                break
            send_keys('Right' if cur < target else 'Left')
            time.sleep(0.20)
            new = _read_stat_value(stat_name)
            if new == prev:
                stuck += 1
                if stuck >= 2:
                    # Two presses, no movement — cursor must be on a different
                    # row than this stat. Better to abort than keep nudging
                    # whichever stat we actually have focus on.
                    break
            else:
                stuck = 0
            prev = new
        actual[key] = _read_stat_value(stat_name)
    return actual


def chargen_set_name(name):
    """On DESCRIPTION tab, edit the Name field to a literal string. The
    cursor lands on Name by default when entering DESCRIPTION; Enter opens
    the input popup, the literal text is typed, and Enter commits."""
    if not _go_to_chargen_tab('DESCRIPTION'):
        raise RuntimeError('could not navigate to DESCRIPTION tab')
    raw = capture_raw()
    send_keys('Enter')
    wait_for_change(before=raw, timeout=1.0)
    for ch in name:
        send_keys(ch)
        time.sleep(0.04)
    send_keys('Enter')
    wait_for_change(timeout=1.0)
    text = strip_ansi(capture_raw())
    m = re.search(r'Name:\s+([^\n]+?)\s{2,}', text)
    return {'name': m.group(1).strip() if m else None}


def chargen_finalize(expected_scenario=None, expected_profession=None, verify=True):
    """Walk to DESCRIPTION and Tab past it to trigger the finalize confirm.
    Does NOT re-filter scenario or profession on the way: src/newcharacter.cpp
    `reset_scenario` cascade-resets the selected profession (and stats and
    traits) when a new scenario is committed, so re-asserting scenario at
    finalize time would silently wipe whatever profession the caller had
    already set. Caller must set scenario FIRST, then profession, then
    stats, then traits, then name — in that order — so each commit doesn't
    cascade-reset the next.

    With `verify` (default), the expected names are looked for in the
    DESCRIPTION dump before pressing past it; mismatches return ok=False
    with a reason instead of finalizing into a wrong build."""
    if not _go_to_chargen_tab('DESCRIPTION'):
        raise RuntimeError('could not navigate to DESCRIPTION tab')

    if verify:
        text = strip_ansi(capture_raw())
        issues = []
        if expected_scenario and expected_scenario not in text:
            issues.append(f'expected_scenario {expected_scenario!r} not visible in DESCRIPTION')
        if expected_profession and expected_profession not in text:
            issues.append(f'expected_profession {expected_profession!r} not visible in DESCRIPTION')
        if issues:
            return {'ok': False, 'mode': 'chargen', 'reason': '; '.join(issues)}

    send_keys('Tab')
    raw = wait_for_change(timeout=2.0)
    mode = detect_mode(raw)
    if mode != 'confirm':
        return {'ok': False, 'mode': mode, 'reason': 'no finalize confirm appeared'}
    send_keys('Y')
    raw = wait_for_change(timeout=10.0)
    return {'ok': True, 'mode': detect_mode(raw)}


def trait_state():
    """Return current TRAITS selection state. Caller must already be on the
    TRAITS tab. Returns dict {positive: [...], negative: [...]}."""
    raw = capture_raw()
    mode = detect_mode(raw)
    if mode != 'chargen_traits':
        raise RuntimeError(f'not on TRAITS tab (current mode: {mode})')
    return selected_trait_panes(capture_colored())


def trait_select(name):
    """Find a named trait across the positive and negative panes, toggle it,
    verify the selection state actually changed. Caller must be on TRAITS
    tab. Returns dict with keys: name, before (was it selected before),
    after (is it selected after), pane ('positive'/'negative'/'unknown'),
    ok (True iff a toggle was observed).

    Strategy: filter for the name in the current pane. If no match (cursor
    didn't move to it), pan to the next pane and try again. Stops after
    visiting both trait panes. The cosmetic pane (eye color, hair) is not
    a toggle target and is skipped."""
    raw = capture_raw()
    mode = detect_mode(raw)
    if mode != 'chargen_traits':
        raise RuntimeError(f'not on TRAITS tab (current mode: {mode})')

    before_state = selected_trait_panes(capture_colored())
    was_selected = (name in before_state['positive']
                    or name in before_state['negative'])

    chargen_filter_reset()

    # Try the current pane first, then pan right and retry. Three attempts
    # cover positive + negative + cosmetic; the cosmetic pane has no real
    # toggles so it short-circuits, but visiting it still rotates back to a
    # useful pane on the next pass.
    for attempt in range(3):
        send_keys('f')
        wait_for_change(timeout=1.0)
        for ch in name:
            send_keys(ch)
        send_keys('Enter')
        raw = wait_for_change(timeout=1.0)

        # If the filter found nothing, CDDA pops a "Nothing found." dialog
        # on top of the list. Older versions left it sitting, so subsequent
        # 'r'/Right/'f' keys went into the popup and corrupted the cosmetic
        # pane filter (where stray characters showed up as one-letter rows
        # like 'Facial hair: n'). Dismiss before continuing.
        if 'Nothing found' in strip_ansi(raw):
            _dismiss_chargen_popup()
            chargen_filter_reset()
            send_keys('Right')
            wait_for_change(timeout=0.5)
            continue

        # Filter committed. Cursor lands on first match — if the name shows
        # up in the visible plain capture, toggle it.
        if name in strip_ansi(raw):
            send_keys('Enter')  # toggle
            wait_for_change(timeout=1.0)
            after_state = selected_trait_panes(capture_colored())
            after_selected = (name in after_state['positive']
                              or name in after_state['negative'])
            pane = 'positive' if name in after_state['positive'] else (
                'negative' if name in after_state['negative'] else (
                    'positive' if name in before_state['positive'] else (
                        'negative' if name in before_state['negative'] else 'unknown'
                    )
                )
            )
            chargen_filter_reset()
            return {
                'name': name,
                'before': was_selected,
                'after': after_selected,
                'pane': pane,
                'ok': was_selected != after_selected,
            }

        # Filter committed but the name isn't on screen — match was probably
        # in a different pane. Reset and pan.
        chargen_filter_reset()
        send_keys('Right')
        wait_for_change(timeout=0.5)

    chargen_filter_reset()
    return {
        'name': name,
        'before': was_selected,
        'after': was_selected,
        'pane': 'unknown',
        'ok': False,
    }


# ---------------------------------------------------------------------------
# Atomic gameplay operations (single wrapper call → multi-prompt menu chain)
# ---------------------------------------------------------------------------

# Confirm-popup substrings the auto-dismiss layer recognizes as recurring
# nuisance distractions. When wait/sleep is interrupted with a "Stop ...?"
# popup containing one of these, the wrapper presses I (Ignore this
# distraction and continue) instead of stopping.
KNOWN_DISTRACTIONS = (
    'dehydrated', 'parched', 'thirsty',
    'hungry', 'famished', 'starving',
    'asthma attack', 'mouth feels so dry',
    'cold and shiver', 'getting chilly',
    'hypothermia', 'cruddy', 'feel cruddy',
)

DIRECTION_KEYS = frozenset('hjklyubn')

WAIT_SPEC_TO_KEY = {
    '20s': '1', '1m': '2', '5m': '3', '30m': '4',
    '1h': '5', '2h': '6', '3h': '7', '6h': '8',
    'daylight': 'd', 'noon': 'n', 'night': 'k', 'midnight': 'm',
    'weather': 'W',
}


def _looks_like_stop_confirm(raw):
    """True if the screen shows a 'Stop ...?' / case-sensitive Y/N confirm."""
    text = strip_ansi(raw)
    if 'Case Sensitive' not in text:
        return False
    return 'Stop ' in text or 'Stop?' in text or 'are you sure' in text.lower()


def _is_distraction_confirm(raw):
    text = strip_ansi(raw).lower()
    if not _looks_like_stop_confirm(raw):
        return False
    return any(d in text for d in KNOWN_DISTRACTIONS)


def _drain_distractions(deadline_s, ignore=True):
    """Poll for popup interrupts during a wait/sleep. Press I to dismiss
    known recurring distractions; press . to interrupt on novel events.
    Returns when the wait/sleep ends or the deadline expires."""
    end = time.time() + deadline_s
    last_seen = capture_raw()
    while time.time() < end:
        time.sleep(0.4)
        raw = capture_raw()
        if raw == last_seen:
            continue
        last_seen = raw
        text_lower = strip_ansi(raw).lower()
        if 'finish waiting' in text_lower or 'you wake up' in text_lower or 'you fall asleep' in text_lower:
            # natural completion; let the activity finish settling
            time.sleep(0.4)
            continue
        if _looks_like_stop_confirm(raw):
            if ignore and _is_distraction_confirm(raw):
                send_keys('I')
                time.sleep(0.3)
            else:
                # novel event; bail
                return raw
        if 'trouble sleeping' in text_lower:
            send_keys('C')  # Continue trying to sleep, don't ask again
            time.sleep(0.3)
    return capture_raw()


def pickup_atomic(name=None):
    """Open pickup, optionally filter to NAME, mark first match, confirm.
    Returns the post-pickup raw screen + a flag for whether anything was
    actually picked up."""
    raw = capture_raw()
    send_keys('g')
    raw = wait_for_change(before=raw, timeout=1.5)
    text = strip_ansi(raw)
    if 'There is nothing' in text or 'no items' in text.lower():
        return raw, False
    if 'Pickup' not in text and 'PICK UP' not in text.upper():
        return raw, False
    if name:
        send_keys('/')
        time.sleep(0.2)
        for ch in name:
            send_keys(ch)
        send_keys('Enter')
        time.sleep(0.4)
    send_keys('l')      # mark first item (cursor lands on it after filter)
    time.sleep(0.2)
    send_keys('Enter')  # confirm pickup
    raw = wait_for_change(timeout=2.0)
    text = strip_ansi(raw)
    return raw, ('pick up' in text.lower() or 'You pick up' in text)


def examine_dir(direction):
    """Press e, then a direction key. One wrapper call instead of two."""
    if direction not in DIRECTION_KEYS and direction.lower() not in (
            'north', 'south', 'east', 'west', 'ne', 'nw', 'se', 'sw'):
        raise ValueError(f'unknown direction: {direction!r}')
    name_to_key = {
        'north': 'k', 'south': 'j', 'east': 'l', 'west': 'h',
        'ne': 'u', 'nw': 'y', 'se': 'n', 'sw': 'b',
    }
    direction = name_to_key.get(direction.lower(), direction)
    raw = capture_raw()
    send_keys('e')
    wait_for_change(before=raw, timeout=1.0)
    send_keys(direction)
    return wait_for_change(timeout=1.5)


def consume_atomic(name=None):
    """Open eat/drink menu, optionally filter to NAME, consume first match."""
    raw = capture_raw()
    send_keys('E')
    raw = wait_for_change(before=raw, timeout=1.5)
    if 'Consume' not in strip_ansi(raw) and 'FOOD' not in strip_ansi(raw):
        return raw, False
    if name:
        send_keys('/')
        time.sleep(0.2)
        for ch in name:
            send_keys(ch)
        send_keys('Enter')
        time.sleep(0.3)
    send_keys('Enter')
    raw = wait_for_change(timeout=2.0)
    text = strip_ansi(raw)
    return raw, ('You drink' in text or 'You eat' in text)


def sleep_atomic(hours=8, ignore_distractions=True):
    """Send $, accept sleep prompt, set alarm, then auto-dismiss recurring
    distractions until the player wakes (or 90 seconds real-time pass)."""
    raw = capture_raw()
    send_keys('$')
    raw = wait_for_change(before=raw, timeout=1.5)
    text = strip_ansi(raw)
    if 'Are you sure you want to sleep' not in text:
        # $ may have opened pause menu (when wielded item conflicts) — back out.
        if 'MAIN MENU' in text or 'Save and quit' in text:
            send_keys('Escape')
            wait_for_change(timeout=0.5)
        return raw, False
    send_keys('Y')
    raw = wait_for_change(timeout=1.5)
    text = strip_ansi(raw)
    if 'alarm' in text.lower() and 'Set an alarm' in text:
        if hours and 3 <= hours <= 9:
            send_keys(str(int(hours)))
        else:
            send_keys('N')
        wait_for_change(timeout=1.0)
    raw = _drain_distractions(deadline_s=90, ignore=ignore_distractions)
    return raw, True


def wait_safe(spec='6h', ignore_known=True):
    """Open the wait dialog, pick a duration, and stay through recurring
    distractions. SPEC is a key from WAIT_SPEC_TO_KEY (e.g. '6h', 'daylight').
    Returns the post-wait raw screen."""
    key = WAIT_SPEC_TO_KEY.get(spec)
    if key is None:
        raise ValueError(f'unknown wait spec: {spec!r}; '
                         f'try one of: {", ".join(WAIT_SPEC_TO_KEY)}')
    raw = capture_raw()
    send_keys('|')
    wait_for_change(before=raw, timeout=1.0)
    text = strip_ansi(capture_raw())
    if 'You have an alarm clock' in text:
        send_keys('w')
        wait_for_change(timeout=1.0)
    send_keys(key)
    time.sleep(0.5)
    return _drain_distractions(deadline_s=90, ignore=ignore_known)


def do_tracked(keys):
    """drive_keys with a structured outcome: did the player actually move,
    and if not what blocked them. Returns (raw, dict)."""
    before = capture_raw()
    text_before = strip_ansi(before)
    after = drive_keys(keys)
    text_after = strip_ansi(after)
    moved = before != after
    block_reasons = (
        ("can't climb here", 'ceiling above'),
        ("can't go down", 'no stairs down'),
        ("can't go up", 'no stairs up'),
        ('There is nothing that can be examined', 'no examinable here'),
        ('Impassable', 'impassable terrain'),
        ('Move: 0', 'movement blocked'),
    )
    blocked_reason = None
    for needle, reason in block_reasons:
        if needle in text_after and text_after.count(needle) > text_before.count(needle):
            blocked_reason = reason
            break
    return after, {
        'moved': moved,
        'blocked_reason': blocked_reason,
        'mode': detect_mode(after),
    }


# ---------------------------------------------------------------------------
# Character state JSON
# ---------------------------------------------------------------------------

_HP_PARTS = ('L ARM', 'HEAD', 'R ARM', 'L LEG', 'TORSO', 'R LEG')
_NEED_LABELS = ('Hunger', 'Thirst', 'Pain', 'Rest', 'Heat', 'Mood',
                'Focus', 'Speed', 'Sound', 'Stam', 'Weariness', 'Weight')


def character_state():
    """Parse the in-game sidebar into a structured dict. Best effort: missing
    fields are simply absent. Caller decides what's required."""
    raw = capture_raw()
    text = strip_ansi(raw)
    out = {}

    stats = {}
    for stat in ('Str', 'Dex', 'Int', 'Per'):
        m = re.search(rf'\b{stat}:\s*(\d+)', text)
        if m:
            stats[stat.lower()] = int(m.group(1))
    if stats:
        out['stats'] = stats

    hp = {}
    for part in _HP_PARTS:
        m = re.search(rf'{re.escape(part)}\s+([|\\/-]+)', text)
        if m:
            bars = m.group(1)
            hp[part.lower().replace(' ', '_')] = bars.count('|')
    if hp:
        out['hp'] = hp

    needs = {}
    for label in _NEED_LABELS:
        # Value must not contain `:` (which would mean we drifted into the
        # next labelled field). Empty values stay absent.
        m = re.search(rf'{label}:\s+([A-Za-z][^\n:]*?)(?:\s{{2,}}|$)',
                      text, re.MULTILINE)
        if m:
            needs[label.lower()] = m.group(1).strip()
    if needs:
        out['needs'] = needs

    for label in ('Time', 'Date', 'Weather', 'Lighting', 'Wind', 'Temperature',
                  'Place', 'Wield', 'Style', 'Moon'):
        m = re.search(rf'{label}:\s+([^\n:]+?)(?:\s{{2,}}|$)',
                      text, re.MULTILINE)
        if m:
            out[label.lower()] = m.group(1).strip()

    out['mode'] = detect_mode(raw)
    return out


# ---------------------------------------------------------------------------
# Keybinding cheat sheet
# ---------------------------------------------------------------------------

KEYBIND_REFERENCE = {
    'game': {
        'movement': {
            'h/j/k/l': 'W/S/N/E (vi)',
            'y/u/b/n': 'NW/NE/SW/SE',
            'arrows': 'cardinal',
            'Shift+dir': 'single force-step (will smash/open obstacles, NOT auto-walk)',
            'auto_travel': 'open overmap (m), move cursor to destination, T to travel '
                           '(no default keybinding for in-pane auto-walk in this build)',
        },
        'pickup': 'g (then auto-handled by wrapper.py pickup)',
        'examine': 'e then direction (or wrapper.py examine DIR)',
        'inventory': 'i',
        'eat_or_drink': 'E (or wrapper.py consume [NAME])',
        'wait': '| then w then time-key (or wrapper.py wait 6h)',
        'sleep': '$ then Y then alarm-hour (or wrapper.py sleep 8)',
        'overmap': 'm (lowercase; M is missions)',
        'craft': '*',
        'construct': '* then category',
        'wield': 'w',
        'wear': 'W',
        'climb_up': '<',
        'climb_down': '>',
        'pause_menu': 'Esc (warning: opens "Save and quit?" prompt path)',
        'save_quit': 'S (case sensitive; conflicts with sleep-Y-S)',
        'quicksave': 'pause menu → b',
        'help': '?',
        'safe_mode_toggle': '! (must reach CDDA un-mangled — bash history '
                            'expansion can swallow it; route through '
                            'wrapper.py safemode_off, which calls '
                            "send_keys('!') from inside Python)",
        'ignore_one_monster': "' (apostrophe; collides with bash quoting AND "
                              "with CDDA's Use-item binding when Use-item is "
                              "active — same Python-direct routing applies)",
        'cancel_activity': '. (interrupt) then Y to confirm — wrapper.py '
                           'cancel_activity does both. Required after an '
                           'accidental hauling toggle, otherwise every '
                           'subsequent move is consumed by the auto-haul',
        'auto_dismiss': 'wrapper.py dismiss walks the screen for known '
                        'safe-Y confirms ("Stop moving items?", "Really '
                        'step into raspberry bush?", "You are freezing!") '
                        'and Escape-able overlays (pause menu, Actions, '
                        'Wield/Wear/Use overlays). drive_keys auto-runs '
                        'this before each key by default',
    },
    'chargen': {
        'next_tab': 'Tab',
        'prev_tab': 'BTab (Shift+Tab)',
        'filter_list': 'f or /',
        'reset_filter': 'r',
        'select_in_list': 'Enter on the cursored row IS the CONFIRM action on '
                          'SCENARIO / PROFESSION / BACKGROUND / SKILLS — without '
                          'it the cursor moves but the spawn-side selection '
                          'stays whatever was previously committed (default '
                          'Evacuee + Survivor). Filter+Enter only closes the '
                          'filter dialog; a SECOND Enter is the CONFIRM',
        'order_matters': 'Set scenario FIRST, then profession, then stats, then '
                         'traits, then name. CDDA reset_scenario cascade-wipes '
                         'profession/stats/traits when a new scenario is '
                         'committed, and reset_profession cascade-wipes stats '
                         'and traits — order any other way and earlier work is '
                         'silently lost',
        'finalize': 'Tab past DESCRIPTION (prompts confirm; case-sensitive Y)',
        'esc_warning': 'Esc opens "Return to main menu?" — use BTab to step '
                       'back without losing the character',
        'traits': {
            'switch_pane': 'Right/Left (positive ↔ negative ↔ cosmetic)',
            'toggle_trait': 'Enter (after panning to correct pane)',
            'shortcut': 'wrapper.py trait_select NAME finds + toggles + verifies',
        },
        'stats': 'Up/Down to switch stat, Right/Left to adjust value',
    },
    'mod_manager': {
        'add_or_remove_mod': 'Enter (NOT +/- — those reorder)',
        'switch_pane': 'Left/Right (Mod List ↔ Mod Load Order)',
        'switch_tab': 'Tab',
        'filter': 'f',
    },
    'distraction_popup': {
        'context': 'fires during wait/sleep when "Stop X?" overlay appears',
        'I': 'Ignore this distraction and continue (use to keep waiting)',
        'Y': 'Yes, stop waiting (case sensitive)',
        'N': 'No, keep waiting (case sensitive)',
        'M': 'Open distractions Manager (silence this distraction permanently)',
        'shortcut': 'wrapper.py wait/sleep auto-dismiss the recurring ones',
    },
    'examine_popup': {
        'context': 'after pressing e in game mode',
        'h/j/k/l/y/u/b/n': 'direction to examine',
        'shortcut': 'wrapper.py examine DIR (one call)',
    },
    'pickup_popup': {
        'context': 'after pressing g',
        'l_or_RIGHT_or_6': 'mark item under cursor',
        'Enter': 'confirm pickup of marked items',
        'Escape': 'cancel',
        'shortcut': 'wrapper.py pickup [NAME] (one call)',
    },
    'sleep_popup': {
        'context': 'after pressing $',
        'Y': 'Yes, sleep',
        'S': 'Yes, save before sleeping (case sensitive; collides with global save)',
        'N': 'No',
        'after_yes': 'alarm-clock submenu: 3-9 hours or N for no alarm',
        'shortcut': 'wrapper.py sleep [HOURS]',
    },
}


def _emit_keybind(ref, indent=0):
    pad = '  ' * indent
    if isinstance(ref, dict):
        for k, v in ref.items():
            if isinstance(v, dict):
                print(f'{pad}{k}:')
                _emit_keybind(v, indent + 1)
            else:
                print(f'{pad}{k}: {v}')
    else:
        print(f'{pad}{ref}')


def emit(payload, as_json):
    """Emit a result. payload is a dict with {mode, lines, ...}. Prose mode
    prints `[mode]` then each line; JSON mode prints one JSON document."""
    if as_json:
        print(json.dumps(payload, ensure_ascii=False))
        return
    if 'raw' in payload:
        print(payload['raw'], end='')
        return
    if 'header' in payload:
        print(payload['header'])
    elif 'mode' in payload:
        print(f'[{payload["mode"]}]')
    for line in payload.get('lines', []):
        print(line)
    if 'note' in payload:
        print(payload['note'])


def main():
    args = list(sys.argv[1:])

    as_json = False
    if args and args[0] in ('--json', '-j'):
        as_json = True
        args = args[1:]

    try:
        if args and args[0] == 'raw':
            emit({'raw': capture_raw()}, as_json)
            return

        if args and args[0] == 'bail':
            raw, mode = bail()
            result = [line for line in parse(raw, mode) if not line.startswith('wsl:')]
            emit({'mode': mode, 'lines': result}, as_json)
            return

        if args and args[0] == 'batch' and len(args) > 1:
            raw = batch(args[1])
            mode = detect_mode(raw)
            result = [line for line in parse(raw, mode) if not line.startswith('wsl:')]
            emit({'mode': mode, 'lines': result}, as_json)
            return

        if args and args[0] == 'status':
            lines = [line for line in chargen_status() if not line.startswith('wsl:')]
            emit({'header': '[chargen status]', 'lines': lines}, as_json)
            return

        if args and args[0] == 'safemode_off':
            still_on = safemode_off(force=True)
            if as_json:
                print(json.dumps({'safe_on': still_on}))
            else:
                print(f'[safemode_off] safe_on={still_on}')
            return

        if args and args[0] == 'dismiss':
            raw = dismiss_blocking_overlays()
            mode = detect_mode(raw)
            if as_json:
                print(json.dumps({'mode': mode}))
            else:
                print(f'[dismiss] mode={mode}')
            return

        if args and args[0] == 'cancel_activity':
            cancel_activity()
            if as_json:
                print(json.dumps({'ok': True}))
            else:
                print('[cancel_activity] sent')
            return

        if args and args[0] == 'filter' and len(args) > 1:
            raw = chargen_filter(args[1])
            mode = detect_mode(raw)
            result = [line for line in parse(raw, mode) if not line.startswith('wsl:')]
            emit({'mode': mode, 'lines': result}, as_json)
            return

        if args and args[0] == 'trait_select' and len(args) > 1:
            outcome = trait_select(args[1])
            if as_json:
                print(json.dumps(outcome, ensure_ascii=False))
            else:
                state = 'on' if outcome['after'] else 'off'
                if outcome['ok']:
                    print(f'[trait_select] {outcome["name"]!r} ({outcome["pane"]}) -> {state}')
                else:
                    print(f'[trait_select] {outcome["name"]!r} unchanged (was {state}, pane: {outcome["pane"]})')
            return

        if args and args[0] == 'trait_state':
            panes = trait_state()
            if as_json:
                print(json.dumps(panes, ensure_ascii=False))
            else:
                print('[trait_state]')
                print('positive: ' + (', '.join(panes['positive']) or '(none)'))
                print('negative: ' + (', '.join(panes['negative']) or '(none)'))
            return

        if args and args[0] == 'set_stats' and len(args) >= 5:
            values = {'str': int(args[1]), 'dex': int(args[2]),
                      'int': int(args[3]), 'per': int(args[4])}
            actual = chargen_set_stats(values)
            if as_json:
                print(json.dumps(actual, ensure_ascii=False))
            else:
                print('[set_stats]')
                for k in ('str', 'dex', 'int', 'per'):
                    print(f'{k}: {actual.get(k)}')
            return

        if args and args[0] == 'set_name' and len(args) > 1:
            outcome = chargen_set_name(args[1])
            if as_json:
                print(json.dumps(outcome, ensure_ascii=False))
            else:
                print(f'[set_name] {outcome.get("name")!r}')
            return

        if args and args[0] == 'finalize':
            scenario = args[1] if len(args) > 1 else None
            profession = args[2] if len(args) > 2 else None
            outcome = chargen_finalize(expected_scenario=scenario,
                                       expected_profession=profession)
            if as_json:
                print(json.dumps(outcome, ensure_ascii=False))
            else:
                print(f'[finalize] ok={outcome.get("ok")} mode={outcome.get("mode")}')
                if outcome.get('reason'):
                    print(outcome['reason'])
            return

        if args and args[0] == 'parse_fixture' and len(args) > 1:
            with open(args[1], 'r', encoding='utf-8') as handle:
                raw = handle.read()
            mode = detect_mode(raw)
            result = [line for line in parse(raw, mode) if not line.startswith('wsl:')]
            emit({'mode': mode, 'lines': result}, as_json)
            return

        if args and args[0] == 'pickup':
            name = args[1] if len(args) > 1 else None
            raw, ok = pickup_atomic(name)
            mode = detect_mode(raw)
            result = [line for line in parse(raw, mode) if not line.startswith('wsl:')]
            if as_json:
                print(json.dumps({'mode': mode, 'picked_up': ok, 'lines': result}, ensure_ascii=False))
            else:
                print(f'[pickup] picked_up={ok}')
                for line in result:
                    print(line)
            return

        if args and args[0] == 'examine' and len(args) > 1:
            raw = examine_dir(args[1])
            mode = detect_mode(raw)
            result = [line for line in parse(raw, mode) if not line.startswith('wsl:')]
            emit({'mode': mode, 'lines': result}, as_json)
            return

        if args and args[0] == 'consume':
            name = args[1] if len(args) > 1 else None
            raw, ok = consume_atomic(name)
            mode = detect_mode(raw)
            result = [line for line in parse(raw, mode) if not line.startswith('wsl:')]
            if as_json:
                print(json.dumps({'mode': mode, 'consumed': ok, 'lines': result}, ensure_ascii=False))
            else:
                print(f'[consume] consumed={ok}')
                for line in result:
                    print(line)
            return

        if args and args[0] == 'sleep':
            hours = int(args[1]) if len(args) > 1 and args[1].isdigit() else 8
            raw, ok = sleep_atomic(hours=hours)
            mode = detect_mode(raw)
            result = [line for line in parse(raw, mode) if not line.startswith('wsl:')]
            if as_json:
                print(json.dumps({'mode': mode, 'slept': ok, 'lines': result}, ensure_ascii=False))
            else:
                print(f'[sleep] slept={ok}')
                for line in result:
                    print(line)
            return

        if args and args[0] == 'wait' and len(args) > 1:
            raw = wait_safe(args[1])
            mode = detect_mode(raw)
            result = [line for line in parse(raw, mode) if not line.startswith('wsl:')]
            emit({'mode': mode, 'lines': result}, as_json)
            return

        if args and args[0] == 'character':
            state = character_state()
            if as_json:
                print(json.dumps(state, ensure_ascii=False))
            else:
                print('[character]')
                for k, v in state.items():
                    if isinstance(v, dict):
                        print(f'{k}:')
                        for sk, sv in v.items():
                            print(f'  {sk}: {sv}')
                    else:
                        print(f'{k}: {v}')
            return

        if args and args[0] == 'keys':
            mode = args[1] if len(args) > 1 else None
            ref = KEYBIND_REFERENCE.get(mode) if mode else KEYBIND_REFERENCE
            if as_json:
                print(json.dumps(ref, ensure_ascii=False, indent=2))
            else:
                print(f'[keys{":"+mode if mode else ""}]')
                _emit_keybind(ref, indent=0)
            return

        if args and args[0] == 'do_tracked':
            raw, info = do_tracked(args[1:])
            mode = detect_mode(raw)
            result = [line for line in parse(raw, mode) if not line.startswith('wsl:')]
            payload = {'mode': mode, **info, 'lines': result}
            if as_json:
                print(json.dumps(payload, ensure_ascii=False))
            else:
                print(f'[do_tracked moved={info["moved"]} blocked={info["blocked_reason"]}]')
                for line in result:
                    print(line)
            return

        if args and args[0] == 'send':
            send_keys(*args[1:])
            if as_json:
                print(json.dumps({'ok': True}))
            return

        show_diff = False
        if args and args[0] == 'diff':
            show_diff = True
            args = args[1:]

        prev = load_prev() if show_diff else set()

        if args and args[0] == 'do':
            raw = drive_keys(args[1:])
        else:
            raw = capture_raw()

        mode = detect_mode(raw)
        result = [line for line in parse(raw, mode) if not line.startswith('wsl:')]

        save_capture(result)

        if show_diff:
            result = [line for line in result if line not in prev]

        emit({'mode': mode, 'lines': result}, as_json)
    except TmuxError as exc:
        if as_json:
            print(json.dumps({'error': str(exc), 'session': SESSION}))
        else:
            print(f'tmux error ({SESSION}): {exc}', file=sys.stderr)
        raise SystemExit(2)


if __name__ == '__main__':
    main()
