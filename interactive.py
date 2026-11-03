"""Capacity recovery for a live Codex TUI in tmux, without restarting the CLI."""
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import random
import signal
import subprocess
import time


MESSAGE = ('Автоповтор после временной capacity ошибки. Продолжай прежнюю задачу в этом же диалоге '
           'с места остановки. Перед повтором действий проверь фактические результаты и фоновые '
           'операции. Сохраняй все указания пользователя и не создавай повторный job.')


def identity(pid):
    root = Path('/proc') / str(pid)
    fields = (root / 'stat').read_text().rsplit(')', 1)[1].split()
    return fields[19], (root / 'cmdline').read_bytes().split(b'\0')


def foreground(pid):
    fields = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
    return fields[0] not in ('T', 't', 'Z', 'X') and fields[2] == fields[5] and int(fields[5]) > 0


def stop_tui(pid, expected):
    for number, delay in ((signal.SIGTERM, 10), (signal.SIGKILL, 2)):
        try:
            if identity(pid) != expected:
                return
            os.kill(pid, number)
        except (ProcessLookupError, FileNotFoundError):
            return
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            try:
                if identity(pid) != expected:
                    return
                state = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()[0]
                if state in ('Z', 'X'):
                    return
            except FileNotFoundError:
                return
            time.sleep(.1)
    raise RuntimeError('owned Codex process did not stop')


def started_at(pid):
    ticks, _ = identity(pid)
    boot = next(int(line.split()[1]) for line in Path('/proc/stat').read_text().splitlines()
                if line.startswith('btime '))
    return boot + int(ticks) / os.sysconf('SC_CLK_TCK')


def find_tui(pane, root=None):
    try:
        root = root or int(subprocess.check_output(['tmux', 'display-message', '-p', '-t', pane, '#{pane_pid}'], stderr=subprocess.DEVNULL))
        tty = subprocess.check_output(['tmux', 'display-message', '-p', '-t', pane, '#{pane_tty}'], text=True, stderr=subprocess.DEVNULL).strip()
    except subprocess.CalledProcessError as error:
        raise RuntimeError('tmux pane is unavailable') from error
    pending, found = [root], []
    while pending:
        pid = pending.pop()
        try:
            _, argv = identity(pid)
            if (argv and Path(os.fsdecode(argv[0])).name == 'codex' and b'exec' not in argv
                    and str((Path('/proc') / str(pid) / 'fd/0').resolve()) == tty):
                found.append(pid)
            pending.extend(map(int, (Path('/proc') / str(pid) / 'task' / str(pid) / 'children').read_text().split()))
        except (OSError, ValueError):
            continue
    if len(found) > 1:
        raise RuntimeError('multiple matching Codex processes; no input sent')
    return found[0] if found else None


def active_rollout(pid):
    roots = set()
    for fd in (Path('/proc') / str(pid) / 'fd').iterdir():
        try:
            path = fd.resolve()
            if not path.name.startswith('rollout-') or path.suffix != '.jsonl':
                continue
            with path.open() as stream:
                meta = json.loads(stream.readline())
            data = meta['payload']
            if (meta['type'] == 'session_meta' and not data.get('parent_thread_id')
                    and not (isinstance(data.get('source'), dict) and 'subagent' in data['source'])):
                roots.add((path, data['id']))
        except (OSError, ValueError, KeyError):
            continue
    if len(roots) > 1:
        raise RuntimeError('multiple top-level sessions open; no input sent')
    return next(iter(roots), None)


def composer(screen):
    lines = screen.splitlines()
    indices = [n for n, line in enumerate(lines) if line.lstrip().startswith('›')]
    if not indices:
        return None
    lines = lines[indices[-1]:]
    text = [lines[0].lstrip()[1:].strip()]
    for line in lines[1:]:
        if not line.strip():
            break
        text.append(line.strip())
    return ' '.join(text)


class Terminal:
    def __init__(self, pane, thread, pid, rollout):
        self.pane, self.thread, self.pid, self.rollout = pane, thread, pid, rollout
        self.identity = identity(pid)
        self.tty = subprocess.check_output(['tmux', 'display-message', '-p', '-t', pane, '#{pane_tty}'], text=True).strip()

    def alive(self):
        try:
            return (identity(self.pid) == self.identity and foreground(self.pid) and find_tui(self.pane) == self.pid
                    and str((Path('/proc') / str(self.pid) / 'fd/0').resolve()) == self.tty
                    and active_rollout(self.pid) == (self.rollout, self.thread))
        except (OSError, RuntimeError, ValueError):
            return False

    def screen(self):
        return subprocess.check_output(['tmux', 'capture-pane', '-p', '-t', self.pane], text=True)

    def idle(self):
        mode = subprocess.check_output(['tmux', 'display-message', '-p', '-t', self.pane, '#{pane_in_mode}'], text=True)
        screen = self.screen()
        return (self.alive() and mode.strip() == '0' and 'esc to interrupt' not in screen
                and 'Working (' not in screen and composer(screen) == 'Ask Codex to do anything')

    def send(self):
        if not self.idle():
            return False
        name = 'codex-night-capacity-' + str(self.pid)
        subprocess.run(['tmux', 'set-buffer', '-b', name, MESSAGE], check=True)
        try:
            subprocess.run(['tmux', 'paste-buffer', '-p', '-t', self.pane, '-b', name], check=True)
        finally:
            subprocess.run(['tmux', 'delete-buffer', '-b', name], check=True)
        time.sleep(.6)
        if not self.alive() or composer(self.screen()) != MESSAGE:
            raise RuntimeError('composer changed; automatic submission cancelled')
        subprocess.run(['tmux', 'send-keys', '-t', self.pane, 'Enter'], check=True)
        return True


class Retry:
    def __init__(self, state, save, args, is_capacity, retry_after):
        self.state, self.save, self.args = state, save, args
        self.is_capacity, self.retry_after = is_capacity, retry_after
        self.pending = state.get('pending')

    def event(self, event, started):
        if event.get('type') != 'event_msg':
            return
        timestamp = datetime.datetime.fromisoformat(event['timestamp'].replace('Z', '+00:00')).timestamp()
        if timestamp < started:
            return
        item = event['payload']
        kind = item.get('type')
        if kind in ('task_started', 'turn_aborted'):
            self.pending = None
            self.state.update(status='watching', next_retry_at=None)
        elif kind == 'task_complete':
            self.pending = None
            error = item.get('error')
            if not isinstance(error, dict) or not self.is_capacity(error):
                self.state.update(status='other_error' if error else 'completed', next_retry_at=None)
                if not error:
                    self.state['consecutive_capacity'] = 0
            else:
                key = hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()
                if key == self.state.get('last_submitted'):
                    return
                if self.args.max_retries and self.state.get('retries', 0) >= self.args.max_retries:
                    self.state.update(status='retry_limit', next_retry_at=None)
                else:
                    streak = self.state.get('consecutive_capacity', 0)
                    delay = min(self.args.max_delay, self.args.initial_delay * 2 ** min(streak, 20))
                    delay = min(self.args.max_delay, delay * random.uniform(1, 1 + self.args.jitter))
                    delay = max(delay, self.retry_after(error))
                    self.pending = key
                    self.state.update(status='waiting_capacity', next_retry_at=time.time() + delay)
        else:
            return
        self.state['pending'] = self.pending
        self.save()

    def tick(self, terminal, now):
        if self.pending and self.args.max_retries and self.state.get('retries', 0) >= self.args.max_retries:
            self.pending = None
            self.state.update(status='retry_limit', pending=None, next_retry_at=None)
            self.save()
            return
        if not self.pending or now < self.state['next_retry_at'] or not terminal.idle():
            return
        key = self.pending
        self.state.update(status='submitting', last_submitted=key, next_retry_at=None, pending=None)
        self.save()  # Persist before sending; a monitor crash must not submit this failure twice.
        self.pending = None
        if terminal.send():
            self.state.update(status='submitted', consecutive_capacity=self.state.get('consecutive_capacity', 0) + 1,
                              retries=self.state.get('retries', 0) + 1, last_sent_at=now)
        else:
            self.state['status'] = 'input_changed'
        self.save()


def watch(args, update, remember, is_capacity, retry_after, write_state, pid=None):
    if pid is not None:
        launcher, deadline = pid, time.monotonic() + 30
        while not (pid := find_tui(args.pane, root=launcher)):
            state = (Path('/proc') / str(launcher) / 'stat').read_text().rsplit(')', 1)[1].split()[0]
            if state in ('Z', 'X') or time.monotonic() >= deadline:
                raise RuntimeError('launcher did not produce an interactive Codex')
            time.sleep(.2)
    else:
        pid = find_tui(args.pane)
    if not pid:
        raise RuntimeError('matching interactive Codex not found in the pane')
    process = identity(pid)
    # New interactive sessions may create their rollout shortly after the TUI starts.
    while not (opened := active_rollout(pid)):
        if identity(pid) != process:
            raise RuntimeError('Codex process changed')
        time.sleep(.2)
    rollout, thread = opened
    if args.resume and thread != args.resume:
        raise RuntimeError('active session differs from requested session')
    if args.watch:
        with rollout.open() as stream:
            cwd = json.loads(stream.readline())['payload']['cwd']
        if Path(cwd).resolve() != args.cd:
            raise RuntimeError('watch directory differs from the active TUI workspace; use -C')
    remember(thread)
    terminal = Terminal(args.pane, thread, pid, rollout)
    locks = args.state_dir / 'locks'
    locks.mkdir(exist_ok=True, mode=0o700)
    lock = (locks / ('interactive-' + str(pid) + '-' + process[0] + '.lock')).open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    durable = args.state_dir / ('interactive-' + str(pid) + '-' + process[0] + '.json')
    state = json.loads(durable.read_text()) if durable.exists() else {}
    state.update(codex_pid=pid, session_id=thread)
    state.setdefault('status', 'watching')
    state.setdefault('next_retry_at', None)
    def save():
        write_state(durable, state)
        update(**state)
    save()
    retry = Retry(state, save, args, is_capacity, retry_after)
    inode, started = rollout.stat().st_ino, started_at(pid)
    if state.get('rollout_inode', inode) != inode:
        raise RuntimeError('rollout replaced; saved cursor cannot be reused')
    state['rollout_inode'] = inode
    try:
        with rollout.open('rb') as stream:
            stream.seek(state.get('cursor', 0))
            while terminal.alive():
                if rollout.stat().st_ino != inode or rollout.stat().st_size < stream.tell():
                    raise RuntimeError('rollout replaced or truncated; no input sent')
                while True:
                    position = stream.tell()
                    line = stream.readline()
                    if not line or not line.endswith(b'\n'):
                        stream.seek(position)
                        break
                    state['cursor'] = stream.tell()
                    retry.event(json.loads(line), started)
                save()
                retry.tick(terminal, time.time())
                time.sleep(2)
        state['status'] = 'tui_closed_or_changed'
        save()
    finally:
        lock.close()
