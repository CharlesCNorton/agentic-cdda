#!/usr/bin/env python3
"""
CDDA tmux screen parser.
Strips ASCII map tiles and minimap, preserves all text content.

Usage:
    wrapper.py              # capture screen, parse, print text
    wrapper.py do KEY...    # send key(s) to tmux, wait, then capture+parse
    wrapper.py send KEY...  # send key(s) only, no capture
"""
import subprocess, sys, re, time

SESSION = 'cdda'

# Characters that appear in the ASCII map / minimap but not in normal text.
# Deliberately excludes letters, digits, colon, comma, pipe (|) so that
# sidebar labels, health bars, and punctuation survive.
MAP_CHARS = frozenset('.#@>{}+\"*F~%│┘┌└┬┤├┴┐┼─═║╔╗╚╝^v')

# Characters to strip from segment edges (box-drawing borders)
BORDER = '│┌┐└┘├┤─┬┴┼'


def capture_raw():
    r = subprocess.run(
        ['tmux', 'capture-pane', '-t', SESSION, '-p'],
        capture_output=True, text=True,
    )
    return r.stdout


def detect_mode(text):
    """Identify current screen state from raw text."""
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
    if 'Press f, F, or / to filter' in text:
        return 'messages'
    if 'What to do with' in text or 'What do you want to do' in text:
        return 'overlay'
    if 'Select your language' in text:
        return 'lang_menu'
    if 'Custom Character' in text or 'Play Now' in text:
        return 'main_menu'
    return 'game'


def is_map_segment(s):
    """True if segment is purely map/minimap characters (no readable text)."""
    return len(s) > 0 and all(c in MAP_CHARS or c == ' ' for c in s)


def parse_default(raw):
    """
    Universal parser.
    For every line: split on runs of 2+ spaces, discard segments that are
    pure map characters, strip box-drawing borders from edges of kept
    segments, rejoin.
    """
    out = []
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
            out.append('  '.join(kept))
    return out


def parse_messages(raw):
    """
    Message log has a bordered left panel (messages) and right sidebar
    (status) sharing the same lines, separated by │.  Split them and
    present each section separately.
    """
    msgs, sidebar = [], []
    for line in raw.split('\n'):
        r = line.rstrip()
        if not r.strip():
            continue
        # The message panel's right border │ sits at roughly column 34-35.
        # Find the border that separates panel from sidebar.
        # Strategy: find the │ closest to column 34.
        border_col = -1
        for i, ch in enumerate(r):
            if ch == '│' and 30 <= i <= 40:
                border_col = i
                break
        if border_col > 0:
            left = r[:border_col]
            right = r[border_col + 1:]
            # Clean left (message text): strip borders and scroll indicators
            left = left.strip('│├┤┌┐└┘─^v ').strip()
            # Strip leading timestamps like "12 seconds"
            left = re.sub(r'^\d+\s+seconds?\s+', '', left)
            left = left.strip(BORDER).strip()
            if left:
                msgs.append(left)
            # Clean right (sidebar): strip minimap
            right_segs = re.split(r' {2,}', right)
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
            # Bottom border or non-split line
            cleaned = r.strip()
            cleaned = cleaned.strip(BORDER).strip()
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


def parse(raw, mode):
    """Route to the right parser."""
    if mode == 'messages':
        return parse_messages(raw)
    return parse_default(raw)


def send_keys(*keys):
    subprocess.run(['tmux', 'send-keys', '-t', SESSION] + list(keys))


def main():
    args = sys.argv[1:]

    # --- send-only mode ---
    if args and args[0] == 'send':
        send_keys(*args[1:])
        return

    # --- do mode: send then capture ---
    if args and args[0] == 'do':
        send_keys(*args[1:])
        time.sleep(0.8)

    # --- capture + parse ---
    raw = capture_raw()
    mode = detect_mode(raw)
    result = parse(raw, mode)

    # Drop WSL mount warnings
    result = [l for l in result if not l.startswith('wsl:')]

    print(f'[{mode}]')
    for l in result:
        print(l)


if __name__ == '__main__':
    main()
