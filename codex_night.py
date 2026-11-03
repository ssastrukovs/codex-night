#!/usr/bin/env python3
"""Run one Codex exec job, resuming its exact session after model capacity errors."""
import argparse
import codecs
import datetime
import fcntl
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import random
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time
import uuid


CONTINUE = (
    "[Автоповтор после временной ошибки model is at capacity.] "
    "Продолжай прежнюю задачу в этом же диалоге с места остановки, сохраняя все "
    "указания и ограничения пользователя. Перед повтором действий проверь, "
    "не завершились ли они уже и не продолжаются ли фоновые команды."
)
CAPACITY = re.compile(r"\b(?:model|server|service)\b.{0,100}\b(?:at\s+capacity|overloaded)\b|"
                      r"\b(?:server[_ -]?(?:is[_ -]?)?overloaded|model[_ -]?capacity[_ -]?exceeded)\b", re.I)
PERMANENT = re.compile(r"unauthori[sz]ed|invalid.?api.?key|insufficient.?quota|usage.?limit|"
                       r"session.?budget|credit.?balance|context.?window|context.?length|"
                       r"permission.?denied|approval.?denied|forbidden|rate.?limit", re.I)


class StopRequested(Exception):
    def __init__(self, number):
        self.number = number


def flattened(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def is_capacity(error):
    text = flattened(error)
    return (not PERMANENT.search(text)
            and not re.search(r'"http_?status_?code"\s*:\s*(?:401|403)\b', text, re.I)
            and bool(CAPACITY.search(text)))


def retry_after(error):
    if isinstance(error, dict):
        for key, value in error.items():
            if key in ("retry_after", "retryAfterSeconds"):
                try:
                    seconds = float(value)
                    if math.isfinite(seconds) and seconds > 0:
                        return seconds
                except (ValueError, TypeError):
                    pass
        return max([retry_after(v) for v in error.values()] or [0])
    if isinstance(error, list):
        return max([retry_after(v) for v in error] or [0])
    match = re.search(r"\bretry-after\s*:\s*(\d+)", str(error), re.I)
    return float(match.group(1)) if match else 0


class Outcome:
    def __init__(self, session):
        self.session = session
        self.terminal = None
        self.error = None
        self.last_error = None
        self.progress = False

    def event(self, value):
        kind = value.get("type")
        if kind == "thread.started":
            identity = str(uuid.UUID(value["thread_id"]))
            if self.session and identity != self.session:
                raise RuntimeError("Codex changed session ID; refusing to continue")
            self.session = identity
        elif kind == "turn.started":
            self.progress = True
            self.terminal = None
            self.error = None
            self.last_error = None
        elif isinstance(kind, str) and kind.startswith("item."):
            self.progress = True
        elif kind == "turn.completed":
            self.terminal, self.error = "completed", None
        elif kind == "turn.failed":
            self.terminal, self.error = "failed", value.get("error")
        elif kind == "error":
            self.last_error = value.get("error", value.get("message"))

    def failure(self, code, stderr):
        if self.terminal == "completed" or code < 0 or code in (130, 143):
            return None
        if self.terminal == "failed":
            return self.error or self.last_error
        if code != 0:
            if self.last_error:
                return self.last_error
            # Match only native CLI error lines, never agent text or tool output.
            lines = [s for s in stderr.splitlines() if re.match(r"^\s*error\s*:", s, re.I)]
            return lines[-1] if lines else None
        return None


def private_open(path, mode="w"):
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | (os.O_APPEND if mode == "a" else os.O_TRUNC), 0o600)
    return os.fdopen(fd, mode) if "b" in mode else os.fdopen(fd, mode, encoding="utf-8")


def save(path, value):
    temporary = path.with_name(path.name + ".%d.tmp" % os.getpid())
    with private_open(temporary) as out:
        json.dump(value, out, ensure_ascii=False, indent=2)
        out.write("\n")
    temporary.replace(path)


def stop_child(child):
    if child.poll() is not None:
        return
    for number, timeout in ((signal.SIGINT, 15), (signal.SIGTERM, 5), (signal.SIGKILL, 2)):
        try:
            os.killpg(child.pid, number)
        except ProcessLookupError:
            return
        try:
            child.wait(timeout=timeout)
            return
        except subprocess.TimeoutExpired:
            pass


def preview(text, limit=6, width=180):
    """Short terminal preview; the complete event remains in events.jsonl."""
    lines = str(text or "").splitlines()
    shown = lines[:limit]
    for line in shown:
        line = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line)
        line = "".join(c for c in line if c.isprintable() or c == "\t")
        print("  " + line[:width] + (" …" if len(line) > width else ""), flush=True)
    if len(lines) > limit or any(len(line) > width for line in shown):
        hidden = max(0, len(lines) - limit)
        note = f"ещё {hidden} строк; " if hidden else ""
        print("  … " + note + "полный вывод в журнале", flush=True)


def render_event(value, show_tools=False):
    kind, item = value.get("type"), value.get("item", {})
    item_type, identity = item.get("type"), item.get("id", "")
    if kind == "item.completed" and item_type == "agent_message":
        print(item.get("text", ""), flush=True)
    elif item_type == "command_execution" and show_tools:
        if kind == "item.started":
            print(f"[команда {identity}]", flush=True)
            preview(item.get("command", ""), limit=3)
        elif kind == "item.completed":
            print(f"[команда {identity}: {item.get('status', 'completed')}, код {item.get('exit_code')}]", flush=True)
            preview(item.get("aggregated_output", ""))
    elif kind == "item.completed" and item_type == "file_change" and show_tools:
        print(f"[файлы {identity}: {item.get('status', 'completed')}]", flush=True)
        preview("\n".join(f"{c.get('kind', 'change')} {c.get('path', '')}" for c in item.get("changes", [])))
    elif item_type == "mcp_tool_call" and kind in ("item.started", "item.completed") and show_tools:
        print(f"[инструмент {identity}: {item.get('server', '')}.{item.get('tool', '')}, {item.get('status', kind)}]", flush=True)
        if kind == "item.started":
            preview(flattened(item.get("arguments", {})), limit=3)
        else:
            result = item.get("result") or {}
            if isinstance(result, dict):
                preview("\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"))
            if item.get("error"):
                preview(flattened(item["error"]))
    elif kind == "item.completed" and item_type == "web_search" and show_tools:
        print("[поиск] " + str(item.get("query", "")), flush=True)
    elif kind in ("error", "turn.failed"):
        print("[codex-night] " + flattened(value.get("error", value.get("message"))), file=sys.stderr, flush=True)


def run_attempt(command, prompt, session, folder, attempt, json_output, on_session, cwd, show_tools=False):
    outcome = Outcome(session)
    tail, pending = bytearray(), bytearray()
    child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, bufsize=0, start_new_session=True, cwd=str(cwd))
    selector = selectors.DefaultSelector()
    decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def event(line):
        value = json.loads(line)
        outcome.event(value)
        if value.get("type") == "thread.started":
            on_session(outcome.session)
        if json_output:
            print(line.decode(), flush=True)
        else:
            render_event(value, show_tools=show_tools)

    try:
        try:
            data = memoryview(prompt.encode())
            while data:
                data = data[os.write(child.stdin.fileno(), data):]
        except BrokenPipeError:
            pass
        finally:
            child.stdin.close()
        selector.register(child.stdout, selectors.EVENT_READ, "stdout")
        selector.register(child.stderr, selectors.EVENT_READ, "stderr")
        exited = None
        with private_open(folder / ("attempt-%03d.stderr.log" % attempt), "wb") as errors, \
                private_open(folder / ("attempt-%03d.events.jsonl" % attempt), "wb") as events:
            while selector.get_map():
                if child.poll() is not None:
                    exited = exited or time.monotonic()
                    if time.monotonic() - exited > 3:
                        break  # An external background command can inherit the pipes.
                for key, _ in selector.select(0.2):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if key.data == "stderr":
                        errors.write(chunk)
                        errors.flush()
                        tail.extend(chunk)
                        del tail[:-65536]
                        sys.stderr.write(decoder.decode(chunk))
                        sys.stderr.flush()
                    else:
                        events.write(chunk)
                        events.flush()
                        pending.extend(chunk)
                        if len(pending) > 64 * 1024 * 1024:
                            raise RuntimeError("oversized Codex JSON event")
                        while b"\n" in pending:
                            line, _, rest = pending.partition(b"\n")
                            pending = bytearray(rest)
                            if line.strip():
                                event(line)
            if pending.strip():
                event(pending)
        return outcome, child.wait(), tail.decode(errors="replace")
    finally:
        handlers = {s: signal.signal(s, signal.SIG_IGN) for s in (signal.SIGINT, signal.SIGTERM)}
        try:
            stop_child(child)
        finally:
            for number, handler in handlers.items():
                signal.signal(number, handler)
        selector.close()
        for stream in (child.stdin, child.stdout, child.stderr):
            if stream and not stream.closed:
                stream.close()


def command_for(args, executable, session):
    command = [executable, "exec", "--json", "-C", str(args.cd)]
    for name, value in (("--model", args.model), ("--profile", args.profile), ("--sandbox", args.sandbox)):
        if value:
            command += [name, value]
    if args.approve_for_me:
        command.append("--approve-for-me")
    for value in args.config:
        command += ["-c", value]
    if session:
        command += ["resume", session]
    return command + ["-"]


def execute(args):
    executable = shutil.which(args.codex_bin)
    if not executable:
        raise RuntimeError("codex executable not found")
    args.cd = args.cd.resolve(strict=True)
    args.state_dir = args.state_dir.expanduser().resolve()
    if not args.cd.is_dir():
        raise RuntimeError("working directory is not a directory")
    prompt = args.prompt
    if args.prompt_file:
        prompt = args.prompt_file.read_text()
    if prompt == "-":
        prompt = sys.stdin.read()
    if not prompt:
        if args.resume:
            prompt = CONTINUE
        elif not (args.interactive or args.watch):
            raise RuntimeError("provide a prompt, --prompt-file, or --resume UUID")
    args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    locks = args.state_dir / "locks"
    locks.mkdir(exist_ok=True, mode=0o700)
    lock_path = locks / (hashlib.sha256(os.fsencode(args.cd)).hexdigest() + ".lock")
    lock = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    sessions = {}
    try:
        try:
            if not args.watch:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another codex-night job already owns this working directory")

        def pin_session(identity):
            if args.watch or identity in sessions:
                return
            fd = os.open(str(locks / ("session-" + identity + ".lock")), os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                raise RuntimeError("another codex-night job already owns this session")
            except BaseException:
                os.close(fd)
                raise
            sessions[identity] = fd

        if args.resume:
            pin_session(args.resume)
        name = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + str(os.getpid())
        folder = args.state_dir / name
        folder.mkdir(mode=0o700)
        status = {"pid": os.getpid(), "cwd": str(args.cd), "session_id": args.resume,
                  "attempt": 0, "capacity_retries": 0, "log_dir": str(folder), "status": "starting"}
        save(args.state_dir / "latest.json", {"status_file": str(folder / "status.json")})

        def update(**values):
            status.update(values)
            save(folder / "status.json", status)

        def remember(identity):
            pin_session(identity)
            update(session_id=identity)

        print("[codex-night] журналы: " + str(folder), file=sys.stderr, flush=True)
        if args.interactive or args.watch:
            try:
                return interactive(args, executable, prompt, update, remember)
            except StopRequested as error:
                update(status="interrupted", exit_code=128 + error.number, next_retry_at=None)
                raise
            except Exception as error:
                update(status="controller_error", controller_error=type(error).__name__, next_retry_at=None)
                raise
        session, retries = args.resume, 0
        try:
            while True:
                attempt = status["attempt"] + 1
                update(status="running", attempt=attempt, session_id=session, next_retry_at=None)
                outcome, code, stderr = run_attempt(command_for(args, executable, session), prompt,
                                                   session, folder, attempt, args.json, remember, args.cd, show_tools=args.show_tools)
                session = outcome.session
                update(session_id=session, child_exit_code=code)
                if code == 0 and outcome.terminal == "completed":
                    update(status="completed", exit_code=0)
                    return 0
                error = outcome.failure(code, stderr)
                if not error or not is_capacity(error):
                    result = code if code > 0 else 128 - code if code < 0 else 1
                    update(status="failed", exit_code=result)
                    print("[codex-night] остановлен: это не повторяемая ошибка capacity", file=sys.stderr)
                    return result
                if outcome.progress and not session:
                    raise RuntimeError("progress exists without a session ID; refusing to restart the task")
                if args.max_retries and retries >= args.max_retries:
                    update(status="retry_limit", exit_code=75)
                    return 75
                retries += 1
                delay = min(args.max_delay, args.initial_delay * 2 ** min(retries - 1, 20))
                delay = min(args.max_delay, delay * random.uniform(1, 1 + args.jitter))
                delay = max(delay, retry_after(error))
                deadline = time.time() + delay
                update(status="waiting_capacity", capacity_retries=retries, next_retry_at=deadline)
                print("[codex-night] capacity: повтор %d через %.1f с, session=%s" %
                      (retries, delay, session or "not started"), file=sys.stderr, flush=True)
                if args.json:
                    print(json.dumps({"type": "capacity.retry", "retry": retries,
                                      "delay_seconds": delay, "session_id": session}), flush=True)
                time.sleep(delay)
                # The initial instruction is replayed only if no thread/turn ever started.
                if session and outcome.progress:
                    prompt = CONTINUE
        except StopRequested as e:
            update(status="interrupted", exit_code=128 + e.number, next_retry_at=None)
            return 128 + e.number
        except BaseException:
            update(status="controller_error", next_retry_at=None)
            raise
    finally:
        for fd in sessions.values():
            os.close(fd)
        os.close(lock)


def interactive(args, executable, prompt, update, remember):
    if not args.pane:
        raise RuntimeError("interactive capacity recovery requires a tmux pane (--pane or TMUX_PANE)")
    module_path = Path(__file__).resolve().with_name("interactive.py")
    spec = importlib.util.spec_from_file_location("codex_night_interactive", module_path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    if args.watch:
        helper.watch(args, update, remember, is_capacity, retry_after, save)
        return 0
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError("--interactive requires a terminal")
    command = [executable, "--no-alt-screen", "-C", str(args.cd)]
    for name, value in (("--model", args.model), ("--profile", args.profile), ("--sandbox", args.sandbox)):
        if value:
            command += [name, value]
    if args.approve_for_me:
        command.append("--approve-for-me")
    for value in args.config:
        command += ["-c", value]
    if args.resume:
        command += ["resume", args.resume]
    if prompt:
        command.append(prompt)
    child = subprocess.Popen(command, cwd=str(args.cd))
    native = {}
    def progress(**values):
        if values.get("codex_pid") and not native:
            pid = values["codex_pid"]
            native.update(pid=pid, identity=helper.identity(pid))
        update(**values)
    try:
        try:
            helper.watch(args, progress, remember, is_capacity, retry_after, save, pid=child.pid)
        except StopRequested:
            raise
        except Exception as error:
            try:
                update(status="monitor_error", monitor_error=type(error).__name__)
            except Exception:
                pass
            try:
                print("[codex-night] capacity monitor stopped; the interactive CLI remains running", file=sys.stderr)
            except Exception:
                pass
        code = child.wait()
        update(status="closed", exit_code=code)
        return code
    finally:
        try:
            if not native and child.poll() is None:
                pid = helper.find_tui(args.pane, root=child.pid)
                if pid:
                    native.update(pid=pid, identity=helper.identity(pid))
            if native:
                helper.stop_tui(native["pid"], native["identity"])
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


def parse(argv=None):
    p = argparse.ArgumentParser(description="Codex CLI с автоповтором capacity в том же диалоге")
    p.add_argument("prompt", nargs="?", help="задание; '-' читает stdin")
    p.add_argument("--resume", type=lambda s: str(uuid.UUID(s)), help="UUID диалога для продолжения или --watch")
    p.add_argument("--prompt-file", type=Path)
    p.add_argument("-C", "--cd", type=Path, default=Path.cwd())
    p.add_argument("-m", "--model")
    p.add_argument("-p", "--profile")
    p.add_argument("-s", "--sandbox", choices=("read-only", "workspace-write", "danger-full-access"))
    p.add_argument("--approve-for-me", action="store_true")
    p.add_argument("-c", "--config", action="append", default=[])
    p.add_argument("--initial-delay", type=float, default=60)
    p.add_argument("--max-delay", type=float, default=600)
    p.add_argument("--jitter", type=float, default=0.2)
    p.add_argument("--max-retries", type=int, default=0, help="0 — без ограничения для capacity")
    p.add_argument("--json", action="store_true", help="выводить исходные JSON-события")
    p.add_argument("--show-tools", action="store_true", help="показывать команды и краткий вывод инструментов")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--interactive", action="store_true", help="постоянный Codex TUI с capacity-повторами в tmux")
    mode.add_argument("--watch", action="store_true", help="добавить capacity-повторы уже открытому TUI, без запуска CLI")
    p.add_argument("--pane", default=os.environ.get("TMUX_PANE"), help="tmux pane для интерактивного режима")
    p.add_argument("--codex-bin", default="codex")
    default = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "codex-night"
    p.add_argument("--state-dir", type=Path, default=default)
    p.add_argument("--status", action="store_true", help="состояние последнего запуска, без старта Codex")
    args = p.parse_args(argv)
    if any(not math.isfinite(v) for v in (args.initial_delay, args.max_delay, args.jitter)) or args.initial_delay <= 0 or args.max_delay < args.initial_delay or not 0 <= args.jitter <= 1 or args.max_retries < 0:
        p.error("invalid retry intervals, jitter or limit")
    if (args.interactive or args.watch) and (args.json or args.show_tools):
        p.error("interactive modes display the native TUI; --json and --show-tools are batch options")
    if args.watch and (args.prompt or args.prompt_file or args.model or args.profile or args.sandbox or args.config or args.approve_for_me):
        p.error("--watch attaches without changing the existing TUI settings or sending an initial prompt")
    if args.prompt_file and args.prompt is not None:
        p.error("use either prompt or --prompt-file")
    return args


def main(argv=None):
    args = parse(argv)
    if args.status:
        pointer = args.state_dir / "latest.json"
        if not pointer.exists():
            print("codex-night ещё не запускался")
            return 0
        print(Path(json.loads(pointer.read_text())["status_file"]).read_text(), end="")
        return 0
    def stopped(number, _frame):
        raise StopRequested(number)
    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, stopped)
    try:
        return execute(args)
    except StopRequested as error:
        return 128 + error.number
    except (OSError, ValueError, RuntimeError) as error:
        print("[codex-night] " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
