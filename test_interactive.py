import copy
import datetime
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

import interactive

spec = importlib.util.spec_from_file_location('night', Path(__file__).with_name('codex_night.py'))
night = importlib.util.module_from_spec(spec)
spec.loader.exec_module(night)


def event(kind='task_complete', error=None, seconds=100):
    return {'timestamp': datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).isoformat(),
            'type': 'event_msg', 'payload': {'type': kind, 'turn_id': str(seconds), 'error': error}}


CAPACITY = {'message': 'Selected model is at capacity. Please try a different model.',
            'codex_error_info': 'server_overloaded'}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.state = {}
        self.saved = []
        self.args = types.SimpleNamespace(initial_delay=60, max_delay=600, jitter=0, max_retries=0)
        self.retry = interactive.Retry(self.state, lambda: self.saved.append(copy.deepcopy(self.state)), self.args,
                                       night.is_capacity, night.retry_after)
        self.terminal = mock.Mock()
        self.terminal.idle.return_value = True
        self.terminal.send.return_value = True

    def failure(self, seconds=100, error=CAPACITY):
        with mock.patch.object(interactive.time, 'time', return_value=seconds):
            self.retry.event(event(error=error, seconds=seconds), started=10)

    def test_terminal_capacity_waits_then_sends_once(self):
        self.failure()
        self.assertEqual(self.state['next_retry_at'], 160)
        self.retry.tick(self.terminal, 159)
        self.terminal.send.assert_not_called()
        self.retry.tick(self.terminal, 160)
        self.terminal.send.assert_called_once()
        self.assertEqual(self.state['retries'], 1)
        self.retry.tick(self.terminal, 200)
        self.assertEqual(self.terminal.send.call_count, 1)
        before_send = next(s for s in self.saved if s['status'] == 'submitting')
        self.assertEqual(before_send['last_submitted'], self.state['last_submitted'])

    def test_repeated_capacity_uses_backoff_and_retry_after(self):
        self.failure()
        self.retry.tick(self.terminal, 160)
        self.failure(seconds=200)
        self.assertEqual(self.state['next_retry_at'], 320)
        self.retry.tick(self.terminal, 320)
        self.failure(seconds=400, error=dict(CAPACITY, retry_after=900))
        self.assertEqual(self.state['next_retry_at'], 1300)

    def test_permanent_errors_and_tool_output_never_trigger(self):
        for error in ({'message': 'Forbidden: model at capacity'}, {'message': 'usage limit server_overloaded'},
                      {'message': 'rate limit exceeded server overloaded'},
                      {'message': 'model overloaded', 'codex_error_info': {'http_connection_failed': {'http_status_code': 403}}},
                      {'message': 'unrelated network failure'}):
            with self.subTest(error=error):
                self.failure(error=error)
                self.assertIsNone(self.retry.pending)
        self.retry.event({'timestamp': '1970-01-01T00:01:40Z', 'type': 'response_item',
                          'payload': {'type': 'function_call_output', 'output': 'server_overloaded'}}, 10)
        self.assertIsNone(self.retry.pending)

    def test_history_new_turn_and_manual_interrupt_cancel_retry(self):
        self.retry.event(event(error=CAPACITY), started=101)
        self.assertIsNone(self.retry.pending)
        for kind in ('task_started', 'turn_aborted'):
            self.failure()
            self.retry.event(event(kind=kind, seconds=120), started=10)
            self.assertIsNone(self.retry.pending)

    def test_busy_composer_waits_without_overwriting_input(self):
        self.failure()
        self.terminal.idle.return_value = False
        self.retry.tick(self.terminal, 200)
        self.terminal.send.assert_not_called()
        self.assertIsNotNone(self.retry.pending)

    def test_saved_submission_survives_monitor_restart(self):
        self.failure()
        self.retry.tick(self.terminal, 200)
        restored = interactive.Retry(self.state, lambda: None, self.args, night.is_capacity, night.retry_after)
        restored.event(event(error=CAPACITY), 10)
        self.assertIsNone(restored.pending)

    def test_pending_retry_survives_restart_with_original_deadline(self):
        self.failure()
        state = copy.deepcopy(self.state)
        restored = interactive.Retry(state, lambda: None, self.args, night.is_capacity, night.retry_after)
        restored.tick(self.terminal, 159)
        self.terminal.send.assert_not_called()
        restored.tick(self.terminal, 160)
        self.terminal.send.assert_called_once()

    def test_restored_pending_respects_lowered_retry_limit(self):
        self.failure()
        state = copy.deepcopy(self.state)
        state['retries'] = 10
        self.args.max_retries = 1
        restored = interactive.Retry(state, lambda: None, self.args, night.is_capacity, night.retry_after)
        restored.tick(self.terminal, 200)
        self.terminal.send.assert_not_called()
        self.assertEqual(state['status'], 'retry_limit')

    def test_success_and_retry_limit(self):
        self.args.max_retries = 1
        self.failure()
        self.retry.tick(self.terminal, 200)
        self.failure(seconds=250)
        self.assertEqual(self.state['status'], 'retry_limit')
        self.retry.event(event(seconds=260), 10)
        self.assertEqual(self.state['consecutive_capacity'], 0)

    def test_send_failure_is_not_replayed(self):
        self.failure()
        self.terminal.send.side_effect = RuntimeError('draft changed')
        with self.assertRaises(RuntimeError):
            self.retry.tick(self.terminal, 200)
        self.assertIsNone(self.retry.pending)
        restored = interactive.Retry(self.state, lambda: None, self.args, night.is_capacity, night.retry_after)
        restored.event(event(error=CAPACITY), 10)
        self.assertIsNone(restored.pending)


class TerminalTests(unittest.TestCase):
    def setUp(self):
        self.terminal = interactive.Terminal.__new__(interactive.Terminal)
        self.terminal.pane, self.terminal.thread, self.terminal.pid = '%1', 'session', 123
        self.terminal.alive = mock.Mock(return_value=True)
        self.empty = '■ Selected model is at capacity\n\n› Ask Codex to do anything\n\n GPT Sol\n'
        self.terminal.screen = mock.Mock(return_value=self.empty)

    def test_busy_draft_copy_mode_and_changed_process_are_rejected(self):
        for screen, mode, alive in ((self.empty, '1', True), (self.empty, '0', False),
                                   ('• Working (8s • esc to interrupt)\n' + self.empty, '0', True),
                                   ('› operator draft\n\n', '0', True)):
            with self.subTest(screen=screen, mode=mode, alive=alive):
                self.terminal.screen.return_value = screen
                self.terminal.alive.return_value = alive
                with mock.patch.object(interactive.subprocess, 'check_output', return_value=mode):
                    self.assertFalse(self.terminal.idle())

    def test_literal_paste_then_enter_without_ctrl_c(self):
        self.terminal.idle = mock.Mock(return_value=True)
        self.terminal.screen.return_value = '› ' + interactive.MESSAGE + '\n\n footer'
        with mock.patch.object(interactive.subprocess, 'run') as run, mock.patch.object(interactive.time, 'sleep'):
            self.assertTrue(self.terminal.send())
            calls = [c.args[0] for c in run.call_args_list]
            self.assertEqual(calls[-1], ['tmux', 'send-keys', '-t', '%1', 'Enter'])
            self.assertFalse(any('C-c' in c for c in calls))

    def test_composer_change_during_paste_never_submits(self):
        self.terminal.idle = mock.Mock(return_value=True)
        self.terminal.screen.return_value = '› changed draft\n\n'
        with mock.patch.object(interactive.subprocess, 'run') as run, mock.patch.object(interactive.time, 'sleep'):
            with self.assertRaises(RuntimeError):
                self.terminal.send()
            self.assertFalse(any(c.args[0][-1] == 'Enter' for c in run.call_args_list))

    def test_wrapped_composer(self):
        self.assertEqual(interactive.composer('old\n› one two\n  three\n\n footer'), 'one two three')

    def test_stopped_or_background_process_is_not_foreground(self):
        for process_state, pgrp, tpgid, expected in (('S', 10, 10, True), ('T', 10, 10, False),
                                                   ('S', 10, 11, False), ('Z', 10, 10, False)):
            fields = [process_state, '0', str(pgrp), '0', '0', str(tpgid)] + ['0'] * 20
            stat = '123 (codex with spaces) ' + ' '.join(fields)
            with self.subTest(stat=stat), mock.patch.object(Path, 'read_text', return_value=stat):
                self.assertEqual(interactive.foreground(123), expected)


class WrapperTests(unittest.TestCase):
    def test_explicit_stop_cleans_bound_native_and_launcher(self):
        args = types.SimpleNamespace(pane='%1', watch=False, cd=Path('/tmp'), model=None,
                                     profile=None, sandbox=None, approve_for_me=False, config=[], resume=None)
        child = mock.Mock()
        child.pid = 123
        child.poll.return_value = None
        child.wait.return_value = 0
        helper = mock.Mock()
        helper.identity.return_value = ('100', [b'codex'])
        def interrupted(_args, update, *_extra, **_kwargs):
            update(codex_pid=456)
            raise night.StopRequested(15)
        helper.watch.side_effect = interrupted
        spec = mock.Mock()
        with mock.patch.object(night.importlib.util, 'spec_from_file_location', return_value=spec), \
             mock.patch.object(night.importlib.util, 'module_from_spec', return_value=helper), \
             mock.patch.object(night.sys.stdin, 'isatty', return_value=True), \
             mock.patch.object(night.sys.stdout, 'isatty', return_value=True), \
             mock.patch.object(night.subprocess, 'Popen', return_value=child):
            with self.assertRaises(night.StopRequested):
                night.interactive(args, 'codex', None, lambda **_: None, lambda _: None)
        helper.stop_tui.assert_called_once_with(456, ('100', [b'codex']))
        child.terminate.assert_called_once()

    def test_monitor_state_and_warning_failures_do_not_terminate_tui(self):
        args = types.SimpleNamespace(pane='%1', watch=False, cd=Path('/tmp'), model=None,
                                     profile=None, sandbox=None, approve_for_me=False, config=[], resume=None)
        child = mock.Mock()
        child.wait.return_value = 0
        child.poll.return_value = 0
        helper = mock.Mock()
        helper.watch.side_effect = OSError('disk full')
        spec = mock.Mock()
        with mock.patch.object(night.importlib.util, 'spec_from_file_location', return_value=spec), \
             mock.patch.object(night.importlib.util, 'module_from_spec', return_value=helper), \
             mock.patch.object(night.sys.stdin, 'isatty', return_value=True), \
             mock.patch.object(night.sys.stdout, 'isatty', return_value=True), \
             mock.patch.object(night.subprocess, 'Popen', return_value=child), \
             mock.patch('builtins.print', side_effect=BrokenPipeError):
            with self.assertRaises(OSError):
                night.interactive(args, 'codex', None, mock.Mock(side_effect=OSError('disk full')), lambda _: None)
        child.wait.assert_called_once()
        child.terminate.assert_not_called()

    def test_restart_reads_only_new_rollout_events_and_keeps_backoff(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rollout = root / 'rollout-test.jsonl'
            meta = {'type': 'session_meta', 'payload': {'id': 'session', 'cwd': str(root)}}
            old = ''.join(json.dumps(e) + '\n' for e in (meta, event(seconds=30),
                                                       event(error=CAPACITY, seconds=40),
                                                       event(error=CAPACITY, seconds=50)))
            rollout.write_text(old + json.dumps(event(error=CAPACITY, seconds=400)) + '\n')
            state = {'cursor': len(old.encode()), 'rollout_inode': rollout.stat().st_ino,
                     'retries': 2, 'consecutive_capacity': 2}
            durable = root / 'interactive-123-100.json'
            durable.write_text(json.dumps(state))
            args = types.SimpleNamespace(pane='%1', resume='session', cd=root, watch=True, state_dir=root,
                                         initial_delay=60, max_delay=600, jitter=0, max_retries=0)
            terminal = mock.Mock()
            terminal.alive.side_effect = [True, False]
            terminal.idle.return_value = True
            with mock.patch.object(interactive, 'find_tui', return_value=123), \
                 mock.patch.object(interactive, 'identity', return_value=('100', [b'codex'])), \
                 mock.patch.object(interactive, 'active_rollout', return_value=(rollout, 'session')), \
                 mock.patch.object(interactive, 'started_at', return_value=0), \
                 mock.patch.object(interactive, 'Terminal', return_value=terminal), \
                 mock.patch.object(interactive.time, 'time', return_value=500), \
                 mock.patch.object(interactive.time, 'sleep'):
                interactive.watch(args, lambda **_: None, lambda _: None, night.is_capacity,
                                  night.retry_after, night.save)
            saved = json.loads(durable.read_text())
            self.assertEqual(saved['consecutive_capacity'], 2)
            self.assertEqual(saved['next_retry_at'], 740)
            terminal.send.assert_not_called()

    def test_subagents_are_excluded_even_without_top_level_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            top = root / 'rollout-root.jsonl'
            child = root / 'rollout-child.jsonl'
            top.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'root', 'source': 'cli'}}) + '\n')
            child.write_text(json.dumps({'type': 'session_meta', 'payload': {
                'id': 'child', 'source': {'subagent': {'spawn': {'parent_thread_id': 'root'}}}}}) + '\n')
            with mock.patch.object(Path, 'iterdir', return_value=iter([top, child])):
                self.assertEqual(interactive.active_rollout(123), (top, 'root'))


if __name__ == '__main__':
    unittest.main()
