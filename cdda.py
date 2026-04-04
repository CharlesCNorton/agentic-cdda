#!/usr/bin/env python3
"""
Windows-side shim for the CDDA wrapper.
Handles WSL invocation, named key mapping, and stderr suppression.

Usage:
    cdda.py                     # capture screen
    cdda.py do k                # move south
    cdda.py do semicolon        # send ; (look mode)
    cdda.py do greater          # send > (go downstairs)
    cdda.py diff do k           # move south, show only changes
    cdda.py send Enter          # send Enter, no capture
    cdda.py raw                 # raw screen dump
    cdda.py bail                # escape to main menu or game
    cdda.py batch "Enter,Tab,f" # send multiple keys with delays
    cdda.py back                # context-aware back (BTab or Escape)
"""
import subprocess, sys

# Named keys for characters that are dangerous in shell contexts
KEY_MAP = {
    'semicolon': ';',
    'greater':   '>',
    'less':      '<',
    'pipe':      '|',
    'amp':       '&',
    'hash':      '#',
    'at':        '@',
    'dollar':    '$',
    'bang':      '!',
    'tilde':     '~',
    'backslash': '\\',
    'quote':     "'",
    'dquote':    '"',
    'lparen':    '(',
    'rparen':    ')',
    'lbrace':    '{',
    'rbrace':    '}',
    'lbracket':  '[',
    'rbracket':  ']',
    'caret':     '^',
    'percent':   '%',
    'question':  '?',
    'comma':     ',',
    'period':    '.',
}

def main():
    args = list(sys.argv[1:])

    # 'back' is a synthetic command: send BTab (chargen back-tab)
    if args and args[0].lower() == 'back':
        args = ['do', 'BTab']

    # Don't map keys for commands that take string args (batch, bail, raw, diff alone)
    skip_map = {'bail', 'raw'}
    if args and args[0].lower() in skip_map:
        pass
    elif args and args[0].lower() == 'batch':
        # Map keys inside the comma-separated batch string
        if len(args) > 1:
            keys = args[1].split(',')
            mapped_keys = [KEY_MAP.get(k.strip().lower(), k.strip()) for k in keys]
            args = ['batch', ','.join(mapped_keys)]
    else:
        # Map named keys in send/do arguments
        mapped = []
        for a in args:
            mapped.append(KEY_MAP.get(a.lower(), a))
        args = mapped

    cmd = [
        'wsl.exe', '-d', 'Ubuntu', '--',
        'python3', '/mnt/d/cataclysm-dda/wrapper.py',
    ] + args

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout:
        print(result.stdout, end='')
    # stderr suppressed (wsl fstab warnings, etc.)

if __name__ == '__main__':
    main()
