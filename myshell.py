#!/usr/bin/env python3
"""
MyShell - A feature-rich command-line shell
Supports: built-ins, I/O redirection, piping, scripting, history, tab completion
"""

import os
import sys
import shlex
import subprocess
try:
    import readline
except ImportError:
    class MockReadline:
        def __init__(self):
            self.history = []
        def get_line_buffer(self) -> str:
            return ""
        def read_history_file(self, path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self.history = [line.rstrip("\n") for line in f.readlines()]
            except Exception:
                pass
        def write_history_file(self, path):
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.writelines(line + "\n" for line in self.history)
            except Exception:
                pass
        def set_history_length(self, length):
            pass
        def set_completer(self, completer):
            pass
        def set_completer_delims(self, delims):
            pass
        def parse_and_bind(self, binding):
            pass
        def get_current_history_length(self) -> int:
            return len(self.history)
        def get_history_item(self, index) -> str:
            if 1 <= index <= len(self.history):
                return self.history[index - 1]
            return ""
    readline = MockReadline()
import glob
import signal
import time
import re
from pathlib import Path
from datetime import datetime

# ─────────────────────────────────────────────
#  Configuration & State
# ─────────────────────────────────────────────

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Try loading .env manually at startup
try:
    _env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(_env_path):
        with open(_env_path, "r", encoding="utf-8") as _env_f:
            for _line in _env_f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    os.environ[_k.strip()] = _v.strip()
except Exception:
    pass

HISTORY_FILE = os.path.expanduser("~/.myshell_history")
MAX_HISTORY   = 500
ALIASES: dict[str, str] = {}
ENV_VARS: dict[str, str] = {**os.environ}
LAST_EXIT_CODE: int = 0
BACKGROUND_JOBS: dict[int, subprocess.Popen] = {}   # job_id -> process
_JOB_COUNTER = 0

PROMPT_COLORS = {
    "reset":  "\033[0m",
    "bold":   "\033[1m",
    "red":    "\033[31m",
    "green":  "\033[32m",
    "yellow": "\033[33m",
    "blue":   "\033[34m",
    "cyan":   "\033[36m",
    "white":  "\033[37m",
}

DEFAULT_PROMPT_TEMPLATE = "{bold}{green}{user}{reset}@{cyan}{host}{reset}:{blue}{cwd}{reset}$ "

# ─────────────────────────────────────────────
#  Prompt Builder
# ─────────────────────────────────────────────

def build_prompt() -> str:
    cwd  = os.getcwd()
    home = os.path.expanduser("~")
    cwd  = cwd.replace(home, "~", 1)
    tpl  = ENV_VARS.get("PS1", DEFAULT_PROMPT_TEMPLATE)
    user = os.getenv("USER") or os.getenv("USERNAME") or "user"
    host = os.uname().nodename if hasattr(os, "uname") else os.getenv("COMPUTERNAME") or "localhost"
    return tpl.format(
        user=user,
        host=host,
        cwd=cwd,
        **PROMPT_COLORS,
    )

# ─────────────────────────────────────────────
#  Tab Completion
# ─────────────────────────────────────────────

class ShellCompleter:
    def __init__(self):
        self.matches: list[str] = []

    def complete(self, text: str, state: int) -> str | None:
        if state == 0:
            self.matches = self._get_matches(text)
        return self.matches[state] if state < len(self.matches) else None

    def _get_matches(self, text: str) -> list[str]:
        buf     = readline.get_line_buffer()
        tokens  = buf.split()
        is_cmd  = not tokens or (len(tokens) == 1 and not buf.endswith(" "))

        if is_cmd:
            return self._complete_command(text)
        return self._complete_path(text)

    def _complete_command(self, text: str) -> list[str]:
        matches = [cmd for cmd in BUILTINS if cmd.startswith(text)]
        matches += [alias for alias in ALIASES if alias.startswith(text)]
        for directory in os.get_exec_path():
            try:
                for name in os.listdir(directory):
                    if name.startswith(text):
                        path = os.path.join(directory, name)
                        if os.access(path, os.X_OK):
                            matches.append(name)
            except PermissionError:
                pass
        return sorted(set(matches))

    def _complete_path(self, text: str) -> list[str]:
        if text.startswith("~"):
            text = os.path.expanduser(text)
        pattern = text + "*"
        matches = glob.glob(pattern)
        return [m + "/" if os.path.isdir(m) else m for m in matches]


_completer = ShellCompleter()

def setup_readline():
    try:
        readline.read_history_file(HISTORY_FILE)
    except FileNotFoundError:
        pass
    readline.set_history_length(MAX_HISTORY)
    readline.set_completer(_completer.complete)
    readline.set_completer_delims(" \t\n;|&<>")
    readline.parse_and_bind("tab: complete")
    readline.parse_and_bind('"\\e[A": previous-history')   # Up
    readline.parse_and_bind('"\\e[B": next-history')       # Down


def save_history():
    try:
        readline.write_history_file(HISTORY_FILE)
    except Exception:
        pass


# ─────────────────────────────────────────────
#  Token / Pipeline Parser
# ─────────────────────────────────────────────

def expand_vars(token: str) -> str:
    """Expand $VAR, ${VAR}, $? in a token."""
    token = token.replace("$?", str(LAST_EXIT_CODE))
    token = os.path.expanduser(token)
    return re.sub(
        r'\$\{?(\w+)\}?',
        lambda m: ENV_VARS.get(m.group(1), ""),
        token,
    )


def tokenize(line: str) -> list[str]:
    """Split with shlex, then expand variables/globs."""
    try:
        raw = shlex.split(line)
    except ValueError as e:
        print(f"myshell: parse error: {e}")
        return []

    result = []
    for tok in raw:
        expanded = expand_vars(tok)
        globbed  = glob.glob(expanded)
        if globbed and any(c in expanded for c in "*?["):
            result.extend(sorted(globbed))
        else:
            result.append(expanded)
    return result


def split_pipes(line: str) -> list[str]:
    """Split on unquoted | characters."""
    parts, current, in_q, q_char = [], [], False, ""
    i = 0
    while i < len(line):
        ch = line[i]
        if in_q:
            current.append(ch)
            if ch == q_char:
                in_q = False
        elif ch in ('"', "'"):
            in_q, q_char = True, ch
            current.append(ch)
        elif ch == "|" and (i + 1 >= len(line) or line[i + 1] != "|"):
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
        i += 1
    parts.append("".join(current).strip())
    return [p for p in parts if p]


def parse_redirects(args: list[str]):
    """Extract redirection tokens; return (clean_args, stdin_file, stdout_file, append)."""
    stdin_file = stdout_file = None
    append = False
    clean, i = [], 0
    while i < len(args):
        tok = args[i]
        if tok == "<" and i + 1 < len(args):
            stdin_file = args[i + 1]; i += 2
        elif tok == ">>" and i + 1 < len(args):
            stdout_file = args[i + 1]; append = True; i += 2
        elif tok == ">" and i + 1 < len(args):
            stdout_file = args[i + 1]; i += 2
        elif tok.startswith(">>"):
            stdout_file = tok[2:]; append = True; i += 1
        elif tok.startswith(">"):
            stdout_file = tok[1:]; i += 1
        elif tok.startswith("<"):
            stdin_file = tok[1:]; i += 1
        else:
            clean.append(tok); i += 1
    return clean, stdin_file, stdout_file, append


# ─────────────────────────────────────────────
#  Built-in Commands
# ─────────────────────────────────────────────

def builtin_cd(args: list[str]) -> int:
    target = args[1] if len(args) > 1 else os.path.expanduser("~")
    target = os.path.expanduser(target)
    try:
        os.chdir(target)
        ENV_VARS["PWD"] = os.getcwd()
    except FileNotFoundError:
        print(f"cd: no such file or directory: {target}")
        return 1
    except PermissionError:
        print(f"cd: permission denied: {target}")
        return 1
    return 0


def builtin_pwd(_args) -> int:
    print(os.getcwd())
    return 0


def builtin_echo(args: list[str]) -> int:
    newline = True
    start   = 1
    if len(args) > 1 and args[1] == "-n":
        newline = False
        start   = 2
    out = " ".join(args[start:])
    print(out, end="\n" if newline else "")
    return 0


def builtin_export(args: list[str]) -> int:
    for token in args[1:]:
        if "=" in token:
            key, _, val = token.partition("=")
            ENV_VARS[key] = val
            os.environ[key] = val
        else:
            print(f"export: invalid argument: {token}")
    return 0


def builtin_unset(args: list[str]) -> int:
    for key in args[1:]:
        ENV_VARS.pop(key, None)
        os.environ.pop(key, None)
    return 0


def builtin_env(_args) -> int:
    for k, v in sorted(ENV_VARS.items()):
        print(f"{k}={v}")
    return 0


def builtin_alias(args: list[str]) -> int:
    if len(args) == 1:
        for name, val in sorted(ALIASES.items()):
            print(f"alias {name}='{val}'")
        return 0
    for token in args[1:]:
        if "=" in token:
            name, _, val = token.partition("=")
            ALIASES[name.strip()] = val.strip().strip("'\"")
        else:
            if token in ALIASES:
                print(f"alias {token}='{ALIASES[token]}'")
            else:
                print(f"alias: {token}: not found")
    return 0


def builtin_unalias(args: list[str]) -> int:
    for name in args[1:]:
        if name not in ALIASES:
            print(f"unalias: {name}: not found")
        else:
            del ALIASES[name]
    return 0


def builtin_history(args: list[str]) -> int:
    n = int(args[1]) if len(args) > 1 else readline.get_current_history_length()
    total = readline.get_current_history_length()
    start = max(1, total - n + 1)
    for i in range(start, total + 1):
        print(f"  {i:4d}  {readline.get_history_item(i)}")
    return 0


def builtin_jobs(_args) -> int:
    _reap_jobs()
    if not BACKGROUND_JOBS:
        print("No background jobs.")
        return 0
    for jid, proc in BACKGROUND_JOBS.items():
        status = "Running" if proc.poll() is None else f"Done ({proc.returncode})"
        print(f"[{jid}]  {status:20s}  PID {proc.pid}")
    return 0


def builtin_kill(args: list[str]) -> int:
    if len(args) < 2:
        print("kill: usage: kill %<job_id> | <pid>")
        return 1
    target = args[1]
    try:
        if target.startswith("%"):
            jid  = int(target[1:])
            proc = BACKGROUND_JOBS.get(jid)
            if not proc:
                print(f"kill: no such job: {jid}")
                return 1
            proc.terminate()
        else:
            os.kill(int(target), signal.SIGTERM)
    except Exception as e:
        print(f"kill: {e}")
        return 1
    return 0


def builtin_source(args: list[str]) -> int:
    if len(args) < 2:
        print("source: usage: source <script>")
        return 1
    return run_script(args[1])


def builtin_type(args: list[str]) -> int:
    for name in args[1:]:
        if name in BUILTINS:
            print(f"{name} is a shell built-in")
        elif name in ALIASES:
            print(f"{name} is aliased to '{ALIASES[name]}'")
        else:
            path = find_executable(name)
            if path:
                print(f"{name} is {path}")
            else:
                print(f"{name}: not found")
    return 0


def builtin_which(args: list[str]) -> int:
    for name in args[1:]:
        path = find_executable(name)
        if path:
            print(path)
        else:
            print(f"{name} not found")
            return 1
    return 0


def builtin_true(_args)  -> int: return 0
def builtin_false(_args) -> int: return 1


def builtin_sleep(args: list[str]) -> int:
    if len(args) < 2:
        print("sleep: usage: sleep <seconds>")
        return 1
    try:
        time.sleep(float(args[1]))
    except ValueError:
        print(f"sleep: invalid time interval: {args[1]}")
        return 1
    return 0


def call_openrouter(prompt: str) -> int:
    import urllib.request
    import json
    
    api_key = ENV_VARS.get("OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("ai: Error: OPENROUTER_API_KEY environment variable is not set.")
        return 1
        
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/myshell",
        "X-Title": "MyShell AI Assistant"
    }
    
    data = {
        "model": "openrouter/free",
        "messages": [
            {"role": "user", "content": prompt}
        ]
    }
    
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode('utf-8'),
        headers=headers,
        method="POST"
    )
    
    try:
        # Limit urlopen connection to 15 seconds to avoid infinite hangs
        with urllib.request.urlopen(req, timeout=15) as response:
            res_data = json.loads(response.read().decode('utf-8'))
            choices = res_data.get("choices", [])
            if choices:
                answer = choices[0].get("message", {}).get("content", "")
                # Safely encode/decode printing to stdout to avoid Windows UnicodeEncodeErrors (e.g. for emojis)
                safe_encoding = sys.stdout.encoding or 'utf-8'
                safe_answer = answer.encode(safe_encoding, errors='replace').decode(safe_encoding)
                print(safe_answer)
                return 0
            else:
                print("ai: Error: No response choices returned by OpenRouter API.")
                return 1
    except Exception as e:
        print(f"ai: Error calling OpenRouter API: {e}")
        return 1


def builtin_ai(args: list[str]) -> int:
    if len(args) < 2:
        print("ai: usage: ai <your question or prompt>")
        return 1
        
    prompt = " ".join(args[1:])
    if (prompt.startswith('"') and prompt.endswith('"')) or (prompt.startswith("'") and prompt.endswith("'")):
        prompt = prompt[1:-1]
        
    return call_openrouter(prompt)


def builtin_help(_args) -> int:
    print("""
╔══════════════════════════════════════════════════════╗
║              MyShell  Built-in Commands              ║
╠══════════════════════════════════════════════════════╣
║  cd [dir]          Change directory                  ║
║  pwd               Print working directory           ║
║  echo [-n] [args]  Print text                        ║
║  export VAR=val    Set environment variable          ║
║  unset VAR         Remove environment variable       ║
║  env               List all env variables            ║
║  alias [name=val]  Create / list aliases             ║
║  unalias name      Remove alias                      ║
║  history [n]       Show command history              ║
║  jobs              List background jobs              ║
║  kill %n | pid     Terminate a process/job           ║
║  source <file>     Execute a script                  ║
║  type <cmd>        Show command type/location        ║
║  which <cmd>       Locate a command                  ║
║  sleep <secs>      Pause for N seconds               ║
║  true / false      Return 0 / 1                      ║
║  ai <prompt>       Ask AI a question                 ║
║  exit [code]       Exit the shell                    ║
║  help              Show this help                    ║
╠══════════════════════════════════════════════════════╣
║  Redirection:   cmd > file   cmd >> file   cmd < f   ║
║  Pipe:          cmd1 | cmd2 | cmd3                   ║
║  Background:    cmd &                                ║
║  And / Or:      cmd1 && cmd2   cmd1 || cmd2          ║
║  Semicolon:     cmd1 ; cmd2                          ║
║  Variables:     $VAR  ${VAR}  $?                     ║
╚══════════════════════════════════════════════════════╝
""")
    return 0


BUILTINS: dict[str, callable] = {
    "cd":       builtin_cd,
    "pwd":      builtin_pwd,
    "echo":     builtin_echo,
    "export":   builtin_export,
    "unset":    builtin_unset,
    "env":      builtin_env,
    "alias":    builtin_alias,
    "unalias":  builtin_unalias,
    "history":  builtin_history,
    "jobs":     builtin_jobs,
    "kill":     builtin_kill,
    "source":   builtin_source,
    ".":        builtin_source,
    "type":     builtin_type,
    "which":    builtin_which,
    "true":     builtin_true,
    "false":    builtin_false,
    "sleep":    builtin_sleep,
    "ai":       builtin_ai,
    "help":     builtin_help,
}


# ─────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────

def find_executable(name: str) -> str | None:
    for directory in os.get_exec_path():
        path = os.path.join(directory, name)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def _reap_jobs():
    """Remove completed background jobs."""
    done = [jid for jid, p in BACKGROUND_JOBS.items() if p.poll() is not None]
    for jid in done:
        proc = BACKGROUND_JOBS.pop(jid)
        print(f"\n[{jid}]  Done  PID {proc.pid}  (exit {proc.returncode})")


# ─────────────────────────────────────────────
#  Command Execution
# ─────────────────────────────────────────────

_ASSIGNMENT_RE = re.compile(r'^([A-Za-z_]\w*)=(.*)$')

def execute_single(args: list[str],
                   stdin=None, stdout=None,
                   background=False) -> int:
    """Execute one command (built-in or external)."""
    global LAST_EXIT_CODE, _JOB_COUNTER

    if not args:
        return 0

    # Bare variable assignment: VAR=value
    if _ASSIGNMENT_RE.match(args[0]) and len(args) == 1:
        m = _ASSIGNMENT_RE.match(args[0])
        ENV_VARS[m.group(1)] = m.group(2)
        os.environ[m.group(1)] = m.group(2)
        return 0

    # Alias expansion (single level)
    if args[0] in ALIASES:
        expanded = shlex.split(ALIASES[args[0]]) + args[1:]
        args = expanded

    cmd = args[0]

    # Built-ins (cannot be piped easily for stdout, handled upstream)
    if cmd in BUILTINS and stdout is None and stdin is None and not background:
        rc = BUILTINS[cmd](args)
        LAST_EXIT_CODE = rc
        return rc

    # External command
    try:
        env_copy = {**os.environ, **ENV_VARS}
        proc = subprocess.Popen(
            args,
            stdin=stdin,
            stdout=stdout,
            env=env_copy,
            start_new_session=background,
        )
        if background:
            _JOB_COUNTER += 1
            BACKGROUND_JOBS[_JOB_COUNTER] = proc
            print(f"[{_JOB_COUNTER}] {proc.pid}")
            LAST_EXIT_CODE = 0
            return 0
        proc.wait()
        LAST_EXIT_CODE = proc.returncode
        return proc.returncode
    except FileNotFoundError:
        # Try built-in fallback (e.g. when piped)
        if cmd in BUILTINS:
            rc = BUILTINS[cmd](args)
            LAST_EXIT_CODE = rc
            return rc
        print(f"myshell: command not found: {cmd}")
        LAST_EXIT_CODE = 127
        return 127
    except PermissionError:
        print(f"myshell: permission denied: {cmd}")
        LAST_EXIT_CODE = 126
        return 126


def execute_pipeline(commands: list[str]) -> int:
    """Execute a list of piped command strings."""
    if len(commands) == 1:
        return execute_command_with_redirects(commands[0])

    parsed = []
    for segment in commands:
        args = tokenize(segment)
        args, sin, sout, app = parse_redirects(args)
        parsed.append((args, sin, sout, app))

    procs   = []
    prev_stdout = None

    for i, (args, sin_f, sout_f, append) in enumerate(parsed):
        is_last = i == len(parsed) - 1

        # stdin
        if sin_f:
            try:
                stdin_handle = open(sin_f, "r")
            except FileNotFoundError:
                print(f"myshell: {sin_f}: No such file")
                return 1
        elif prev_stdout is not None:
            stdin_handle = prev_stdout
        else:
            stdin_handle = None

        # stdout
        if is_last:
            if sout_f:
                mode = "a" if append else "w"
                stdout_handle = open(sout_f, mode)
            else:
                stdout_handle = None
        else:
            stdout_handle = subprocess.PIPE

        if not args:
            continue

        # Built-ins mid-pipe: run with captured I/O via subprocess trick
        env_copy = {**os.environ, **ENV_VARS}
        try:
            proc = subprocess.Popen(
                args,
                stdin=stdin_handle,
                stdout=stdout_handle,
                env=env_copy,
            )
            procs.append(proc)
            if stdout_handle == subprocess.PIPE:
                prev_stdout = proc.stdout
            else:
                prev_stdout = None
        except FileNotFoundError:
            print(f"myshell: command not found: {args[0]}")
            return 127

    rc = 0
    for proc in procs:
        proc.wait()
        rc = proc.returncode
    LAST_EXIT_CODE = rc
    return rc


def execute_command_with_redirects(segment: str) -> int:
    """Parse redirects and execute a single command."""
    args = tokenize(segment)
    if not args:
        return 0

    args, sin_f, sout_f, append = parse_redirects(args)
    background = args and args[-1] == "&"
    if background:
        args = args[:-1]

    stdin_handle = stdout_handle = None
    try:
        if sin_f:
            stdin_handle = open(sin_f, "r")
        if sout_f:
            stdout_handle = open(sout_f, "a" if append else "w")

        return execute_single(args,
                              stdin=stdin_handle,
                              stdout=stdout_handle,
                              background=background)
    finally:
        if stdin_handle:  stdin_handle.close()
        if stdout_handle: stdout_handle.close()


def execute_line(line: str) -> int:
    """Handle ; && || operators, then delegate to pipeline executor."""
    global LAST_EXIT_CODE

    # Split on ; (naive – ignores quoting for simplicity)
    # We handle && and || as well
    rc = 0
    for stmt in split_statements(line):
        operator, command = stmt
        if operator == "&&" and rc != 0:
            continue
        if operator == "||" and rc == 0:
            continue
        pipes = split_pipes(command)
        rc    = execute_pipeline(pipes)
    return rc


def split_statements(line: str):
    """Yield (operator, command) pairs for ; && || separators."""
    ops    = ["&&", "||", ";"]
    result = []
    rest   = line.strip()
    op     = None

    while rest:
        # Find earliest operator
        best_pos, best_op = len(rest), None
        for o in ops:
            p = _find_op(rest, o)
            if p != -1 and p < best_pos:
                best_pos, best_op = p, o

        if best_op is None:
            result.append((op, rest.strip()))
            break
        result.append((op, rest[:best_pos].strip()))
        op   = best_op
        rest = rest[best_pos + len(best_op):].strip()

    return result


def _find_op(s: str, op: str) -> int:
    """Find operator index outside of quotes."""
    in_q, q_char = False, ""
    i = 0
    while i < len(s):
        ch = s[i]
        if in_q:
            if ch == q_char:
                in_q = False
        elif ch in ('"', "'"):
            in_q, q_char = True, ch
        elif s[i:i+len(op)] == op:
            return i
        i += 1
    return -1


# ─────────────────────────────────────────────
#  Scripting
# ─────────────────────────────────────────────

def run_script(path: str) -> int:
    """Execute a shell script file line by line."""
    try:
        with open(path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        print(f"myshell: {path}: No such file")
        return 1
    except PermissionError:
        print(f"myshell: {path}: Permission denied")
        return 1

    rc = 0
    for lineno, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        rc = execute_line(line)
    return rc


# ─────────────────────────────────────────────
#  Signal Handling
# ─────────────────────────────────────────────

def handle_sigint(sig, frame):
    print()   # newline after ^C


signal.signal(signal.SIGINT,  handle_sigint)
if hasattr(signal, "SIGTSTP"):
    signal.signal(signal.SIGTSTP, signal.SIG_IGN)   # ignore Ctrl-Z in shell itself


# ─────────────────────────────────────────────
#  REPL
# ─────────────────────────────────────────────

def repl():
    setup_readline()
    print("Welcome to \033[1m\033[36mMyShell\033[0m!  Type \033[33mhelp\033[0m for built-in commands.\n")

    while True:
        _reap_jobs()
        try:
            line = input(build_prompt()).strip()
        except EOFError:
            print("\nexit")
            break
        except KeyboardInterrupt:
            print()
            continue

        if not line:
            continue
        if line in ("exit", "quit"):
            save_history()
            sys.exit(0)
        if line.startswith("exit ") or line.startswith("quit "):
            try:
                code = int(line.split()[1])
            except (IndexError, ValueError):
                code = 0
            save_history()
            sys.exit(code)

        execute_line(line)
        save_history()


# ─────────────────────────────────────────────
#  Entry Point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) > 1:
        # Script mode
        rc = run_script(sys.argv[1])
        sys.exit(rc)
    else:
        repl()
