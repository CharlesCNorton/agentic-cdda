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
