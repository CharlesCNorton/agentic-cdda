#!/usr/bin/env python3
"""
Windows-side shim for the CDDA wrapper.
Handles wrapper launch selection, named key mapping, and stderr suppression.

Usage:
    cdda.py                     # capture screen
    cdda.py do k                # move south
    cdda.py do semicolon        # send ; (look mode)
    cdda.py do greater          # send > (go downstairs)
    cdda.py do backspace        # send Backspace (mapped to tmux BSpace)
    cdda.py diff do k           # move south, show only changes
    cdda.py send Enter          # send Enter, no capture
    cdda.py raw                 # raw screen dump
    cdda.py bail                # escape to main menu or game
    cdda.py batch "Enter,Tab,f" # send multiple keys with delays
    cdda.py back                # context-aware back (BTab or Escape)
    cdda.py status              # in chargen, show DESCRIPTION tab content
                                # (auto tabs there and back)
    cdda.py filter "Trivia"     # in chargen, filter list and commit
    cdda.py trait_select Asthmatic  # in chargen TRAITS, find + toggle a trait
    cdda.py trait_state         # dump selected positive/negative traits
    cdda.py doctor              # check WSL distro + tmux + python + wrapper

Add --json before any subcommand to get a structured response.

Environment:
    CDDA_WRAPPER_MODE   wsl | native (default: wsl)
    CDDA_WRAPPER_CMD    Full command override for launching wrapper.py
    CDDA_WSL_DISTRO     WSL distro name (default: auto-detect; falls back to
                        Ubuntu, WSLExperiments, then any running distro)
    CDDA_WSL_PYTHON     Python inside WSL (default: python3)
    CDDA_WSL_WRAPPER    Wrapper path inside WSL (default: derived from this
                        script's location, e.g. /mnt/d/agentic-cdda/wrapper.py)
    CDDA_NATIVE_PYTHON  Native Python executable (default: current interpreter)
    CDDA_NATIVE_WRAPPER Native wrapper.py path (default: repo-local wrapper.py)
    CDDA_TMUX_SESSION   Forwarded to wrapper.py
    CDDA_LAST_CAPTURE   Forwarded to wrapper.py

Daemon mode (skip wsl.exe spawn cost per call):
    CDDA_DAEMON         "1" / "true" / "yes" to try daemon first; falls back
                        to subprocess if daemon is not reachable
    CDDA_DAEMON_HOST    Daemon host (default: 127.0.0.1)
    CDDA_DAEMON_PORT    Daemon port (default: 9876)
    CDDA_DAEMON_TIMEOUT Connect timeout in seconds (default: 0.3)

    Start the daemon manually inside WSL:
        wsl -d <distro> -- python3 /mnt/d/<repo>/wrapper_daemon.py
"""
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys

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

# Common key names normalized to tmux send-keys names. The wrapper passes
# multi-char tokens straight to tmux, which uses BSpace / BTab / IC / DC /
# PageUp / PageDown rather than the more common spellings users type.
NAMED_KEY_MAP = {
    'backspace': 'BSpace',
    'bspace':    'BSpace',
    'delete':    'DC',
    'del':       'DC',
    'insert':    'IC',
    'ins':       'IC',
    'pageup':    'PageUp',
    'pgup':      'PageUp',
    'pagedown':  'PageDown',
    'pgdn':      'PageDown',
    'pgdown':    'PageDown',
    'home':      'Home',
    'end':       'End',
    'space':     'Space',
    'btab':      'BTab',
    'shifttab':  'BTab',
}

FORWARDED_ENV_VARS = (
    'CDDA_TMUX_SESSION',
    'CDDA_LAST_CAPTURE',
)

DAEMON_HOST = os.environ.get('CDDA_DAEMON_HOST', '127.0.0.1')
DAEMON_PORT = int(os.environ.get('CDDA_DAEMON_PORT', '9876'))
DAEMON_TIMEOUT = float(os.environ.get('CDDA_DAEMON_TIMEOUT', '0.3'))


def daemon_enabled():
    return os.environ.get('CDDA_DAEMON', '').strip().lower() in ('1', 'true', 'yes', 'on')


def args_to_daemon_request(args):
    """Translate sys.argv-style args into a daemon JSON request.
    Returns None for commands the daemon cannot handle (e.g. diff, which
    needs LAST_CAPTURE state and is rare enough to not optimize)."""
    if not args:
        return {'cmd': 'capture'}
    cmd = args[0].lower()
    rest = args[1:]
    if cmd in ('raw', 'capture', 'bail', 'ping', 'status'):
        return {'cmd': cmd}
    if cmd == 'do':
        return {'cmd': 'do', 'args': rest}
    if cmd == 'send':
        return {'cmd': 'send', 'args': rest}
    if cmd == 'batch' and rest:
        return {'cmd': 'batch', 'args': [rest[0]]}
    if cmd == 'filter' and rest:
        return {'cmd': 'filter', 'args': [rest[0]]}
    return None


def daemon_call(req):
    """Send req to the daemon. Returns parsed response dict, or None if
    the daemon is not reachable. Raises on protocol errors."""
    forwarded = {
        name: os.environ[name]
        for name in FORWARDED_ENV_VARS
        if os.environ.get(name)
    }
    if forwarded:
        req = {**req, 'env': forwarded}
    try:
        s = socket.socket()
        s.settimeout(DAEMON_TIMEOUT)
        s.connect((DAEMON_HOST, DAEMON_PORT))
        s.settimeout(None)
        s.sendall(json.dumps(req).encode('utf-8') + b'\n')
        chunks = []
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if b'\n' in chunk:
                break
        s.close()
        if not chunks:
            return None
        line = b''.join(chunks).partition(b'\n')[0]
        return json.loads(line.decode('utf-8'))
    except (ConnectionRefusedError, socket.timeout, socket.error, OSError):
        return None


def emit_daemon_response(resp):
    if 'error' in resp:
        write_text(sys.stderr, f'daemon error: {resp["error"]}\n')
        if 'traceback' in resp:
            write_text(sys.stderr, resp['traceback'])
        raise SystemExit(2)
    if 'raw' in resp:
        write_text(sys.stdout, resp['raw'])
        return
    if 'pong' in resp:
        write_text(sys.stdout, 'pong\n')
        return
    if 'ok' in resp:
        return
    if 'lines' in resp:
        if 'mode' in resp:
            write_text(sys.stdout, f'[{resp["mode"]}]\n')
        elif resp.get('status'):
            write_text(sys.stdout, '[chargen status]\n')
        for line in resp['lines']:
            write_text(sys.stdout, line + '\n')
        return
    # Unknown shape; dump raw JSON for debugging.
    write_text(sys.stdout, json.dumps(resp) + '\n')

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


def windows_to_wsl_path(p):
    """Translate D:\\agentic-cdda\\wrapper.py -> /mnt/d/agentic-cdda/wrapper.py."""
    p = Path(p).resolve()
    parts = p.parts
    if not parts:
        return str(p)
    drive = parts[0].rstrip(':\\').rstrip(':').lower()
    if not drive:
        return str(p).replace('\\', '/')
    rest = '/'.join(parts[1:]).replace('\\', '/')
    return f'/mnt/{drive}/{rest}'


def derived_wsl_wrapper():
    """Default wrapper path inside WSL: this script's sibling wrapper.py
    translated to /mnt/<drive>/... form. Replaces the prior hardcoded
    /mnt/d/cataclysm-dda/wrapper.py default."""
    here = Path(__file__).with_name('wrapper.py')
    return windows_to_wsl_path(here)


def list_running_wsl_distros():
    """Return list of currently-running WSL distro names. Empty on failure.

    wsl.exe emits UTF-16 LE on Windows (with a BOM), and Python's text= mode
    treats it as Latin-1, splicing null bytes into every character. We must
    consume bytes and decode explicitly."""
    try:
        out = subprocess.run(
            ['wsl.exe', '--list', '--running', '--quiet'],
            capture_output=True, timeout=4,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    raw = out.stdout
    # Strip a UTF-16 LE BOM if present, then decode.
    if raw.startswith(b'\xff\xfe'):
        raw = raw[2:]
    try:
        text = raw.decode('utf-16-le')
    except UnicodeDecodeError:
        text = raw.decode('utf-8', errors='replace')
    # Some versions emit \r\n separators with stray \x00s; clean them up.
    text = text.replace('\x00', '').replace('\r', '')
    distros = [line.strip() for line in text.split('\n') if line.strip()]
    return distros


def auto_detect_distro():
    """Pick a WSL distro to use. Order: env override > Ubuntu (if running) >
    WSLExperiments > first running distro > Ubuntu (fallback)."""
    override = os.environ.get('CDDA_WSL_DISTRO', '').strip()
    if override:
        return override
    running = list_running_wsl_distros()
    for preferred in ('Ubuntu', 'WSLExperiments'):
        if preferred in running:
            return preferred
    if running:
        return running[0]
    return 'Ubuntu'


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
        distro = auto_detect_distro()
        wsl_python = os.environ.get('CDDA_WSL_PYTHON', 'python3')
        wsl_wrapper = os.environ.get('CDDA_WSL_WRAPPER', derived_wsl_wrapper())
        forwarded = [
            f'{name}={os.environ[name]}'
            for name in FORWARDED_ENV_VARS
            if os.environ.get(name)
        ]
        return [
            'wsl.exe', '-d', distro, '--', 'env',
            'PYTHONIOENCODING=utf-8',
            'LANG=C.UTF-8',
            'LC_ALL=C.UTF-8',
            *forwarded,
            wsl_python, wsl_wrapper,
        ]

    raise SystemExit(f'Unsupported CDDA_WRAPPER_MODE: {mode!r}')

def doctor():
    """Check the local environment that cdda.py / wrapper.py depend on.
    Prints PASS/FAIL lines with fix hints. Returns exit code (0 on all
    pass, 1 on any failure)."""
    failures = 0

    def check(label, ok, detail=''):
        nonlocal failures
        mark = 'PASS' if ok else 'FAIL'
        line = f'[{mark}] {label}'
        if detail:
            line += f'  ({detail})'
        print(line)
        if not ok:
            failures += 1

    distro = auto_detect_distro()
    running = list_running_wsl_distros()
    distro_running = distro in running
    check(
        f'WSL distro {distro!r} reachable',
        distro_running,
        'override with CDDA_WSL_DISTRO=...; running: ' + (', '.join(running) or '(none)'),
    )

    if distro_running:
        for tool in ('tmux', 'python3'):
            res = subprocess.run(
                ['wsl.exe', '-d', distro, '--', 'bash', '-lc', f'command -v {tool}'],
                capture_output=True, text=True, timeout=4,
            )
            check(
                f'{tool} present in {distro}',
                res.returncode == 0 and res.stdout.strip(),
                res.stdout.strip() or 'install it inside the distro',
            )

        wrapper_path = os.environ.get('CDDA_WSL_WRAPPER', derived_wsl_wrapper())
        res = subprocess.run(
            ['wsl.exe', '-d', distro, '--', 'bash', '-lc', f'test -f {shlex.quote(wrapper_path)}'],
            capture_output=True, timeout=4,
        )
        check(f'wrapper.py at {wrapper_path}', res.returncode == 0,
              'set CDDA_WSL_WRAPPER if your repo lives elsewhere')

    session = os.environ.get('CDDA_TMUX_SESSION', 'cdda')
    if distro_running:
        res = subprocess.run(
            ['wsl.exe', '-d', distro, '--', 'tmux', 'has-session', '-t', session],
            capture_output=True, timeout=4,
        )
        if res.returncode == 0:
            check(f'tmux session {session!r} exists', True)
        else:
            print(f'[INFO] tmux session {session!r} not found — start CDDA first')

    return 1 if failures else 0


def main():
    args = list(sys.argv[1:])

    # 'doctor' is local — does not call into the wrapper.
    if args and args[0].lower() == 'doctor':
        raise SystemExit(doctor())

    # Pull --json off the front so it survives unchanged through key mapping
    # and gets passed straight to wrapper.py.
    json_flag = []
    if args and args[0] in ('--json', '-j'):
        json_flag = [args[0]]
        args = args[1:]

    # 'back' is a synthetic command: send BTab (chargen back-tab)
    if args and args[0].lower() == 'back':
        args = ['do', 'BTab']

    def map_one(token):
        lower = token.lower()
        if lower in KEY_MAP:
            return KEY_MAP[lower]
        if lower in NAMED_KEY_MAP:
            return NAMED_KEY_MAP[lower]
        return token

    # Don't map keys for commands that take string args (or no args). All
    # gameplay-action subcommands belong here too — pickup/examine/sleep/wait/
    # consume take a NAME or duration spec, not a CDDA keystroke.
    skip_map = {'bail', 'raw', 'status', 'filter', 'trait_select',
                'trait_state', 'parse_fixture',
                'pickup', 'examine', 'consume', 'sleep', 'wait',
                'character', 'keys',
                'set_stats', 'set_name', 'finalize'}
    if args and args[0].lower() in skip_map:
        pass
    elif args and args[0].lower() == 'batch':
        # Map keys inside the comma-separated batch string
        if len(args) > 1:
            keys = args[1].split(',')
            mapped_keys = [map_one(k.strip()) for k in keys]
            args = ['batch', ','.join(mapped_keys)]
    else:
        # Map named keys in send/do arguments
        args = [map_one(a) for a in args]

    # Try daemon first if enabled. Falls back to subprocess if not reachable.
    # The daemon already speaks JSON natively, so --json over daemon is a
    # no-op (the existing emit_daemon_response prints prose; JSON over daemon
    # would need a separate code path which is not yet implemented).
    if daemon_enabled() and not json_flag:
        req = args_to_daemon_request(args)
        if req is not None:
            resp = daemon_call(req)
            if resp is not None:
                emit_daemon_response(resp)
                return

    base_cmd = get_wrapper_command()
    # wsl.exe -- env <vars> python3 wrapper.py <args> passes <args> straight
    # to env (which exec's python3) without any bash in the loop, so shell
    # metachars and spaces are inert. An earlier version shlex.quote'd these
    # for "bash safety", which actually leaked literal POSIX single quotes
    # through to wrapper.py — `cdda.py filter "Middle of Nowhere"` would
    # filter for `'Middle of Nowhere'` (matches nothing). Pass args raw.
    forwarded_args = json_flag + args
    cmd = base_cmd + forwarded_args

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
