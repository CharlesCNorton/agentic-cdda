#!/usr/bin/env python3
"""
CDDA tmux screen parser.
Strips ASCII map tiles and minimap, preserves all text content.

Usage (from WSL):
    wrapper.py              # capture screen, parse, print text
    wrapper.py do KEY...    # send key(s), wait for change, capture+parse
    wrapper.py send KEY...  # send key(s) only, no capture
    wrapper.py diff         # capture, show only lines changed since last
    wrapper.py diff do KEY  # send, wait, capture, show only changes
    wrapper.py raw          # dump raw tmux pane (for debugging)
    wrapper.py bail         # hammer Escape until main menu or game screen
    wrapper.py batch K1,K2  # send comma-separated keys with delays
"""
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


def send_keys(*keys):
    """Send keys to tmux. Single characters use literal mode (-l) to avoid
    tmux interpreting ; and other metacharacters."""
    for key in keys:
        if len(key) == 1:
            run_tmux('send-keys', '-t', SESSION, '-l', key)
        else:
            run_tmux('send-keys', '-t', SESSION, key)


def wait_for_change(before=None, timeout=3.0, interval=0.15):
    """Poll until the screen content changes or timeout is reached."""
    if before is None:
        before = capture_raw()
    latest = before
    elapsed = 0.0
    while elapsed < timeout:
        time.sleep(interval)
        elapsed += interval
        latest = capture_raw()
        if latest != before:
            time.sleep(0.1)  # let the screen stabilize
            return capture_raw()
    return latest


def drive_keys(keys, timeout=3.0):
    """Send keys one at a time, waiting for each change and stopping on
    blocking modal states so later keys do not spill into the wrong screen."""
    raw = capture_raw()
    for key in keys:
        before = raw
        send_keys(key)
        raw = wait_for_change(before=before, timeout=timeout)
        if detect_mode(raw) in STOP_MODES:
            break
    return raw


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
    # Search dialog overlaid on chargen
    if 'Search:' in text and ('SCENARIO' in text or 'PROFESSION' in text):
        return 'chargen_search'
    # Achievement lock popup
    if 'You must complete the achievement' in text:
        return 'locked'
    # Character creation
    if 'SCENARIO' in text and 'PROFESSION' in text and 'STATS' in text:
        if '[TRAITS]' in text:
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
    if re.search(r'\[Y\]es\s+\[N\]o', text):
        return 'confirm'
    if 'The End' in text and 'In memory of:' in text:
        return 'death'
    if 'Your scores' in text and 'ACHIEVEMENTS' in text and 'KILLS' in text:
        return 'postmortem'
    if 'Loading files' in text or 'Verifying' in text or 'Finalizing' in text:
        return 'loading'
    if looks_like_popup(text):
        return 'popup'
    return 'game'


# ---------------------------------------------------------------------------
# Segment helpers
# ---------------------------------------------------------------------------

def clean_text(text, extra_strip=''):
    return text.strip(BORDER + extra_strip + ' ').strip()


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
        if line and (line[0].islower() or line[0] in ',.;:!?)\'"'):
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
            left.append(clean_text(r))
            continue
        if '[s] sort' in r or '[f, F' in r:
            continue
        if 'Press ?' in r or 'Press k,' in r or 'Press l,' in r or 'Press TAB' in r:
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
            continue
        if r.strip() == 'Survivor':
            continue
        if 'sort:' in r or 'filter' in r or 'Press ?' in r:
            continue

        if r.count(BOX_VERT) >= 2:
            parts = r.split(BOX_VERT)
            c1 = clean_text(parts[0], '^v')
            c2 = clean_text(parts[1], '^v') if len(parts) > 1 else ''
            c3 = clean_text(parts[2], '^v') if len(parts) > 2 else ''
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
    """Hammer Escape until we reach the main menu or game screen.
    Handles confirmation dialogs and popups along the way."""
    for _ in range(20):
        raw = capture_raw()
        mode = detect_mode(raw)
        if mode in ('game', 'main_menu'):
            return raw, mode
        if mode == 'confirm':
            send_keys('Y')
            wait_for_change(before=raw, timeout=1.5)
            continue
        if mode in {'chargen_search', 'keybindings', 'locked', 'pause_menu', 'popup', 'postmortem', 'overlay', 'messages'}:
            send_keys('Escape')
            wait_for_change(before=raw, timeout=1.0)
            continue
        send_keys('Escape')
        wait_for_change(before=raw, timeout=1.0)
    raw = capture_raw()
    return raw, detect_mode(raw)


def batch(keys_csv):
    """Send comma-separated keys with adaptive waits between each."""
    keys = [key.strip() for key in keys_csv.split(',') if key.strip()]
    return drive_keys(keys, timeout=1.5)


def main():
    args = list(sys.argv[1:])

    try:
        if args and args[0] == 'raw':
            print(capture_raw(), end='')
            return

        if args and args[0] == 'bail':
            raw, mode = bail()
            result = parse(raw, mode)
            result = [line for line in result if not line.startswith('wsl:')]
            print(f'[{mode}]')
            for line in result:
                print(line)
            return

        if args and args[0] == 'batch' and len(args) > 1:
            raw = batch(args[1])
            mode = detect_mode(raw)
            result = parse(raw, mode)
            result = [line for line in result if not line.startswith('wsl:')]
            print(f'[{mode}]')
            for line in result:
                print(line)
            return

        if args and args[0] == 'send':
            send_keys(*args[1:])
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
        result = parse(raw, mode)
        result = [line for line in result if not line.startswith('wsl:')]

        save_capture(result)

        if show_diff:
            result = [line for line in result if line not in prev]

        print(f'[{mode}]')
        for line in result:
            print(line)
    except TmuxError as exc:
        print(f'tmux error ({SESSION}): {exc}', file=sys.stderr)
        raise SystemExit(2)


if __name__ == '__main__':
    main()
