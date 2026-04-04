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
import subprocess, sys, re, time, os

SESSION = 'cdda'

# Map/minimap characters. Includes @ (player/NPC map marker).
# The is_map_segment() function special-cases lone @ to preserve it in
# compass directions and NPC lists.
MAP_CHARS = frozenset('.#@>{}+\"*F~%│┘┌└┬┤├┴┐┼─═║╔╗╚╝^v')

BORDER = '│┌┐└┘├┤─┬┴┼'

LAST_CAPTURE = '/tmp/cdda_last_capture.txt'

# ---------------------------------------------------------------------------
# Core I/O
# ---------------------------------------------------------------------------

def capture_raw():
    r = subprocess.run(
        ['tmux', 'capture-pane', '-t', SESSION, '-p'],
        capture_output=True, text=True,
    )
    return r.stdout


def send_keys(*keys):
    """Send keys to tmux. Single characters use literal mode (-l) to avoid
    tmux interpreting ; and other metacharacters."""
    for key in keys:
        if len(key) == 1:
            subprocess.run(['tmux', 'send-keys', '-t', SESSION, '-l', key])
        else:
            subprocess.run(['tmux', 'send-keys', '-t', SESSION, key])


def wait_for_change(timeout=3.0, interval=0.15):
    """Poll until the screen content changes or timeout is reached."""
    before = capture_raw()
    elapsed = 0.0
    while elapsed < timeout:
        time.sleep(interval)
        elapsed += interval
        after = capture_raw()
        if after != before:
            time.sleep(0.1)          # let the screen stabilize
            return capture_raw()
    return capture_raw()             # timeout — return whatever we have

# ---------------------------------------------------------------------------
# Mode detection
# ---------------------------------------------------------------------------

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
    if re.search(r'Items \(\d+\)', text) and 'Monsters' in text:
        return 'surroundings'
    if 'Press f, F, or / to filter' in text and re.search(r'^\s*[v^]\s', text, re.MULTILINE):
        return 'messages'
    if 'What to do with' in text or 'What do you want to do' in text:
        return 'overlay'
    if 'Select your language' in text:
        return 'lang_menu'
    if 'Custom Character' in text or 'Play Now' in text:
        return 'main_menu'
    if re.search(r'\[Y\]es\s+\[N\]o', text):
        return 'confirm'
    if 'Loading files' in text or 'Verifying' in text or 'Finalizing' in text:
        return 'loading'
    return 'game'

# ---------------------------------------------------------------------------
# Segment helpers
# ---------------------------------------------------------------------------

def is_map_segment(s):
    """True if segment is purely map/minimap characters (no readable text).
    A lone @ is kept — it marks the player in compass / NPC lists."""
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
            if not seg:
                continue
            if is_map_segment(seg):
                continue
            cleaned = seg.strip(BORDER).strip()
            if cleaned and any(c.isalpha() for c in cleaned):
                msgs.append(cleaned)
    return msgs

# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def parse_default(raw):
    """Universal parser: strip map, keep text, prepend messages."""
    out = []

    # Top-of-screen messages
    top_msgs = extract_top_messages(raw)
    for m in top_msgs:
        out.append(f'> {m}')

    seen_msgs = set(top_msgs)

    for line in raw.split('\n'):
        r = line.rstrip()
        if not r.strip():
            continue
        segments = re.split(r' {2,}', r)
        kept = []
        for seg in segments:
            seg = seg.strip()
            if not seg:
                continue
            if is_map_segment(seg):
                continue
            cleaned = seg.strip(BORDER).strip()
            if cleaned:
                kept.append(cleaned)
        if kept:
            joined = '  '.join(kept)
            # skip if it duplicates a message we already printed
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
            if ch == '│' and 30 <= i <= 40:
                border_col = i
                break
        if border_col > 0:
            left = r[:border_col].strip('│├┤┌┐└┘─^v ').strip()
            left = re.sub(r'^\d+\s+seconds?\s+', '', left)
            left = left.strip(BORDER).strip()
            if left:
                msgs.append(left)
            right_segs = re.split(r' {2,}', r[border_col + 1:])
            kept = []
            for seg in right_segs:
                seg = seg.strip()
                if not seg or is_map_segment(seg):
                    continue
                cleaned = seg.strip(BORDER).strip()
                if cleaned:
                    kept.append(cleaned)
            if kept:
                sidebar.append('  '.join(kept))
        else:
            cleaned = r.strip().strip(BORDER).strip()
            if cleaned and any(c.isalnum() for c in cleaned):
                msgs.append(cleaned)

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
    """Join continuation lines (start with lowercase or punctuation) to the
    previous line so that wrapped descriptions read as coherent paragraphs."""
    if not lines:
        return lines
    out = [lines[0]]
    for line in lines[1:]:
        if line and (line[0].islower() or line[0] in ',.;:!?)\'\"'):
            out[-1] += ' ' + line
        else:
            out.append(line)
    return out


def _find_selected(right_lines):
    """Return the name shown in the Identity: line of the details panel."""
    for line in right_lines:
        m = re.match(r'Identity:\s*(.+?)(?:\s*\(male\)|\s*\(female\))', line)
        if m:
            return m.group(1).strip()
    return ''


def parse_chargen(raw):
    """Two-column chargen (scenario, profession, background, skills, desc).
    Splits left list from right details, rejoins wrapped text, marks the
    currently selected item."""
    left, right = [], []
    tabs = ''

    for line in raw.split('\n'):
        r = line.rstrip()
        if not r.strip():
            continue

        # Tab bar
        if 'SCENARIO' in r and 'PROFESSION' in r:
            active = re.search(r'\[([A-Z]+)\]', r)
            active_tab = active.group(1) if active else ''
            tab_names = re.findall(r'[A-Z]{3,}', r)
            tabs = ' | '.join(
                f'[{t}]' if t == active_tab else t for t in tab_names)
            continue

        # Summary / filter lines
        if 'Summary |' in r or 'Lifestyle:' in r:
            left.append(r.strip(BORDER).strip())
            continue
        if '[s] sort' in r or '[f, F' in r:
            continue
        if 'Press ?' in r or 'Press k,' in r or 'Press l,' in r or 'Press TAB' in r:
            continue

        # Split at │ divider near column 40
        divider = -1
        for i, ch in enumerate(r):
            if ch == '│' and 35 <= i <= 45:
                divider = i
                break

        if divider > 0:
            l = r[:divider].strip(BORDER + ' ^v').strip()
            ri = r[divider + 1:].strip(BORDER + ' ^v').strip()
            if l:
                left.append(l)
            if ri:
                right.append(ri)
        else:
            cleaned = r.strip(BORDER + ' ^v').strip()
            if cleaned and any(c.isalnum() for c in cleaned):
                indent = len(r) - len(r.lstrip())
                if indent > 30:
                    right.append(cleaned)
                else:
                    left.append(cleaned)

    # Rejoin wrapped right-panel text
    right = _rejoin_wrapped(right)

    # Detect lock messages in right panel
    locked = False
    for line in right:
        if 'You must complete' in line or 'to unlock' in line:
            locked = True
            break

    # Mark the selected item (and flag if locked)
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
    """Three-column trait screen: positive | negative | cosmetic,
    with a description for the selected trait at the bottom."""
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
                f'[{t}]' if t == active_tab else t for t in tab_names)
            continue

        if 'Summary' in r and 'Lifestyle' in r:
            continue
        if r.strip() == 'Survivor':
            continue
        if 'sort:' in r or 'filter' in r or 'Press ?' in r:
            continue

        # Three-column rows have at least 2 │ separators
        if r.count('│') >= 2:
            parts = r.split('│')
            c1 = parts[0].strip(' ^v').strip()
            c2 = parts[1].strip(' ^v').strip() if len(parts) > 1 else ''
            c3 = parts[2].strip(' ^v').strip() if len(parts) > 2 else ''
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
        out.extend(f'  {t}' for t in col1)
    if col2:
        out.append('--- Negative ---')
        out.extend(f'  {t}' for t in col2)
    if col3:
        out.append('--- Cosmetic ---')
        out.extend(f'  {t}' for t in col3)
    if description:
        out.append('')
        out.append('Selected: ' + ' '.join(description))
    return out

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
    return parse_default(raw)

# ---------------------------------------------------------------------------
# Diff support
# ---------------------------------------------------------------------------

def load_prev():
    try:
        with open(LAST_CAPTURE, 'r') as f:
            return set(f.read().strip().split('\n'))
    except FileNotFoundError:
        return set()


def save_capture(lines):
    with open(LAST_CAPTURE, 'w') as f:
        f.write('\n'.join(lines))

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def bail():
    """Hammer Escape until we reach the main menu or game screen.
    Handles confirmation dialogs (Y/N) along the way."""
    for _ in range(20):
        raw = capture_raw()
        mode = detect_mode(raw)
        if mode in ('game', 'main_menu'):
            return raw, mode
        # Handle Y/N confirmation dialogs
        if 'Return to main menu?' in raw or re.search(r'\[Y\]es\s+\[N\]o', raw):
            send_keys('Y')
            time.sleep(0.5)
            continue
        # Handle search dialogs
        if 'Search:' in raw:
            send_keys('Escape')
            time.sleep(0.3)
            continue
        # Handle lock popups
        if 'You must complete the achievement' in raw:
            send_keys('Escape')
            time.sleep(0.3)
            continue
        send_keys('Escape')
        time.sleep(0.4)
    return capture_raw(), detect_mode(capture_raw())


def batch(keys_csv):
    """Send comma-separated keys with short delays between each."""
    keys = [k.strip() for k in keys_csv.split(',') if k.strip()]
    for key in keys:
        send_keys(key)
        time.sleep(0.25)
    time.sleep(0.3)
    return capture_raw()


def main():
    args = list(sys.argv[1:])

    # --- raw dump ---
    if args and args[0] == 'raw':
        print(capture_raw(), end='')
        return

    # --- bail: escape to main menu or game ---
    if args and args[0] == 'bail':
        raw, mode = bail()
        result = parse(raw, mode)
        result = [l for l in result if not l.startswith('wsl:')]
        print(f'[{mode}]')
        for l in result:
            print(l)
        return

    # --- batch: send multiple keys ---
    if args and args[0] == 'batch' and len(args) > 1:
        raw = batch(args[1])
        mode = detect_mode(raw)
        result = parse(raw, mode)
        result = [l for l in result if not l.startswith('wsl:')]
        print(f'[{mode}]')
        for l in result:
            print(l)
        return

    # --- send-only ---
    if args and args[0] == 'send':
        send_keys(*args[1:])
        return

    # --- diff flag ---
    show_diff = False
    if args and args[0] == 'diff':
        show_diff = True
        args = args[1:]

    # --- load previous capture for diff before anything changes ---
    prev = load_prev() if show_diff else set()

    # --- do mode: send then adaptive wait ---
    if args and args[0] == 'do':
        send_keys(*args[1:])
        raw = wait_for_change()
    else:
        raw = capture_raw()

    mode = detect_mode(raw)
    result = parse(raw, mode)
    result = [l for l in result if not l.startswith('wsl:')]

    # --- save for future diffs ---
    save_capture(result)

    # --- apply diff filter ---
    if show_diff:
        result = [l for l in result if l not in prev]

    print(f'[{mode}]')
    for l in result:
        print(l)


if __name__ == '__main__':
    main()
