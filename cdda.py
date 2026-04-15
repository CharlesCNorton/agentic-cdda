#!/usr/bin/env python3
"""
Windows-side shim for the CDDA wrapper.
Handles wrapper launch selection, named key mapping, and stderr suppression.

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

Environment:
    CDDA_WRAPPER_MODE   wsl | native (default: wsl)
    CDDA_WRAPPER_CMD    Full command override for launching wrapper.py
    CDDA_WSL_DISTRO     WSL distro name (default: Ubuntu)
    CDDA_WSL_PYTHON     Python inside WSL (default: python3)
    CDDA_WSL_WRAPPER    Wrapper path inside WSL
    CDDA_NATIVE_PYTHON  Native Python executable (default: current interpreter)
    CDDA_NATIVE_WRAPPER Native wrapper.py path (default: repo-local wrapper.py)
    CDDA_TMUX_SESSION   Forwarded to wrapper.py
    CDDA_LAST_CAPTURE   Forwarded to wrapper.py
"""
import os
from pathlib import Path
import shlex
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

FORWARDED_ENV_VARS = (
    'CDDA_TMUX_SESSION',
    'CDDA_LAST_CAPTURE',
)

def write_text(stream, text):
    if not text:
        return
    try:
        stream.write(text)
    except UnicodeEncodeError:
        buffer = getattr(stream, 'buffer', None)
        if buffer is None:
            raise
        buffer.write(text.encode('utf-8', errors='replace'))
    stream.flush()


def get_wrapper_command():
    custom_cmd = os.environ.get('CDDA_WRAPPER_CMD', '').strip()
    if custom_cmd:
        cmd = shlex.split(custom_cmd, posix=False)
        if not cmd:
            raise SystemExit('CDDA_WRAPPER_CMD is set but empty after parsing')
        return cmd

    mode = os.environ.get('CDDA_WRAPPER_MODE', 'wsl').strip().lower()
    if mode == 'native':
        native_python = os.environ.get('CDDA_NATIVE_PYTHON', sys.executable)
        native_wrapper = os.environ.get(
            'CDDA_NATIVE_WRAPPER',
            str(Path(__file__).with_name('wrapper.py')),
        )
        return [native_python, native_wrapper]

    if mode == 'wsl':
        distro = os.environ.get('CDDA_WSL_DISTRO', 'Ubuntu')
        wsl_python = os.environ.get('CDDA_WSL_PYTHON', 'python3')
        wsl_wrapper = os.environ.get(
            'CDDA_WSL_WRAPPER',
            '/mnt/d/cataclysm-dda/wrapper.py',
        )
        forwarded = [
            f'{name}={os.environ[name]}'
            for name in FORWARDED_ENV_VARS
            if os.environ.get(name)
        ]
        return [
            'wsl.exe', '-d', distro, '--', 'env',
            'PYTHONIOENCODING=utf-8',
            *forwarded,
            wsl_python, wsl_wrapper,
        ]

    raise SystemExit(f'Unsupported CDDA_WRAPPER_MODE: {mode!r}')

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

    cmd = get_wrapper_command() + args

    run_env = os.environ.copy()
    run_env.setdefault('PYTHONIOENCODING', 'utf-8')
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        env=run_env,
    )
    if result.stdout:
        write_text(sys.stdout, result.stdout)
    if result.returncode != 0:
        if result.stderr:
            write_text(sys.stderr, result.stderr)
        raise SystemExit(result.returncode)
    # stderr suppressed on success (wsl fstab warnings, etc.)

if __name__ == '__main__':
    main()
