"""Exercise the shipped CLI on two isolated, headless Herdr servers."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/pane_manager.py'
PRIMARY = 'default'
SECONDARY = 'pm-b'
ECHO = '''import os, sys
print('APP:' + os.getcwd(), flush=True)
for line in sys.stdin:
    print('RECEIVED:' + line.rstrip('\\r\\n'), flush=True)
'''


class Live(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        binary = shutil.which('herdr')
        if not binary:
            raise unittest.SkipTest('Herdr executable required')
        cls.binary = binary
        temp = tempfile.TemporaryDirectory(prefix='pm-', dir=os.environ.get('HERDR_TEST_TMPDIR'))
        cls.addClassCleanup(cls.cleanup_temp, temp)
        cls.root = Path(temp.name).resolve()
        cls.cwd = cls.root / 'working dir'
        cls.cwd.mkdir()
        cls.env = {k: v for k, v in os.environ.items() if not k.startswith('HERDR_')}
        cls.env.update({k: str(cls.root) for k in (
            'HOME', 'XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'XDG_DATA_HOME', 'XDG_RUNTIME_DIR')})
        cls.env['PYTHONIOENCODING'] = 'utf-8'
        if os.name != 'nt':
            cls.env['SHELL'] = '/bin/sh'
        for session in (PRIMARY, SECONDARY):
            status = cls.native(session, 'status', 'server', '--json', json_result=True)
            socket_path = Path(status['socket']).resolve()
            if status['running'] or not socket_path.is_relative_to(cls.root):
                raise RuntimeError(f'Refusing to touch non-isolated Herdr endpoint: {socket_path}')
            log_path = cls.root / f'{session}.log'
            with log_path.open('wb') as log:
                proc = subprocess.Popen([cls.binary, '--session', session, 'server'], cwd=cls.cwd,
                                        env=cls.env, stdout=log, stderr=subprocess.STDOUT)
            cls.addClassCleanup(cls.stop, proc, session)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and proc.poll() is None:
                if cls.native(session, 'status', 'server', '--json', json_result=True).get('running'):
                    break
                time.sleep(.1)
            else:
                raise RuntimeError(f'Disposable server {session} did not start: {log_path.read_text(errors="replace")}')

    @staticmethod
    def cleanup_temp(temp):
        # Windows child consoles may take a moment to let go of their working directory.
        deadline = time.monotonic() + 5
        while True:
            try:
                temp.cleanup()
                return
            except PermissionError:
                if os.name != 'nt' or time.monotonic() >= deadline:
                    raise
                time.sleep(.15)

    @classmethod
    def stop(cls, proc, session):
        try:
            if proc.poll() is None:
                cls.native(session, 'server', 'stop')
                proc.wait(timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)

    @classmethod
    def native(cls, session, *args, json_result=False):
        # Fixture lifecycle and simulated human zoom only, not the workflows under test.
        r = subprocess.run([cls.binary, '--session', session, *args], env=cls.env,
                           capture_output=True, encoding='utf-8', timeout=10, check=True)
        return json.loads(r.stdout) if json_result else {}

    def setUp(self):
        self.workspace = self.new_workspace(PRIMARY, self._testMethodName)['workspace_id']

    def call(self, op, *args, session=PRIMARY, workspace=None, code=None, stdin=None, env=None, cli_cwd=None):
        scope = [] if op in ('workspace-create', 'workspaces') else ['--workspace', workspace or self.workspace]
        r = subprocess.run([sys.executable, str(SCRIPT), op, '--instance', session, *scope, *args],
                           env={**self.env, **(env or {})}, cwd=cli_cwd, input=stdin, capture_output=True,
                           encoding='utf-8', timeout=25)
        self.assertIn(r.returncode, (0, 1), r.stderr)
        self.assertTrue(r.stdout, r.stderr)
        data = json.loads(r.stdout)
        self.assertEqual(r.returncode, 0 if code is None else 1, data)
        if code:
            self.assertEqual(data['error']['code'], code, data)
            return data['error']
        return data.get('result', data)

    def new_workspace(self, session, label, *flags, **kwargs):
        return self.call('workspace-create', '--label', label, '--cwd', str(self.cwd), *flags,
                         session=session, **kwargs)

    def create(self, suffix, group='test', *, argv=None, stdin=False, cmd=None, app_cwd=None, config=None, **kwargs):
        if cmd is not None:
            args = ['--command', cmd]
        else:
            payload = json.dumps(argv if argv is not None else [sys.executable, '-u', '-c', ECHO])
            args = ['--argv-stdin'] if stdin else ['--argv', payload]
            if stdin:
                kwargs['stdin'] = payload
        if config is not None:
            args += ['--config', str(config)]
        return self.call('create', '--cwd', str(app_cwd or self.cwd), '--group', group, '--suffix', suffix, *args,
                         **kwargs)['pane_id']

    def read(self, pane, source='recent_unwrapped', limit=100, **kwargs):
        return self.call('read', '--pane', pane, '--source', source, '--limit', str(limit), **kwargs)['text']

    def wait_output(self, pane, marker, source='recent_unwrapped'):
        deadline = time.monotonic() + 8
        page = ''
        while time.monotonic() < deadline:
            page = self.read(pane, source=source)
            if marker in page:
                return page
            time.sleep(.1)
        self.fail(f'{marker!r} missing from pane output: {page!r}')

    def test_workspace_discovery_and_creation(self):
        name = 'spaced workspace 示例'
        receipt = self.new_workspace(PRIMARY, name, '--focus', env={'PYTHONIOENCODING': 'cp1252'})
        workspace, pane = receipt['workspace_id'], receipt['root_pane_id']
        listing = self.call('workspaces', env={'PYTHONIOENCODING': 'cp1252',
                                              'HERDR_SOCKET_PATH': str(self.root / 'wrong-target')})
        self.assertEqual(listing['instance'], PRIMARY)
        self.assertIn({'workspace_id': workspace, 'label': name, 'focused': True}, listing['workspaces'])
        layout = self.call('layout', workspace=workspace)
        self.assertTrue(any(p['pane_id'] == pane for t in layout['tabs'] for p in t['panes']))
        self.new_workspace(PRIMARY, name, code='workspace_exists')
        self.assertNotIn(name, [w['label'] for w in self.call('workspaces', session=SECONDARY)['workspaces']])

    def layout_config(self, rows, cols):
        config = self.root / f'layout-{rows}-{cols}.json'
        config.write_text(json.dumps({'layout': {'max_rows': rows, 'max_cols': cols}}), encoding='utf-8')
        return config

    def group_rectangles(self, group):
        return [[pane['rect'] for pane in tab['panes']] for tab in self.call('layout')['tabs']
                if tab['label'] == f'pane-manager: {group}']

    def test_configured_layout_limits_enforce_each_axis_and_overflow(self):
        for rows, cols in ((1, 1), (1, 2), (2, 1)):
            with self.subTest(rows=rows, cols=cols):
                group = f'limits-{rows}-{cols}'
                config = self.layout_config(rows, cols)
                for i in range(rows * cols + 1):
                    self.create(f'app-{i}', group=group, config=config)
                tabs = self.group_rectangles(group)
                self.assertEqual(sorted(map(len, tabs)), [1, rows * cols])
                full = max(tabs, key=len)
                self.assertEqual(len({r['x'] for r in full}), cols)
                self.assertEqual(len({r['y'] for r in full}), rows)

    def test_default_layout_limits_allow_four_but_not_a_third_row_or_column(self):
        group = 'default-limits'
        for i in range(5):
            self.create(f'app-{i}', group=group, cli_cwd=self.cwd)
        tabs = self.group_rectangles(group)
        self.assertEqual(sorted(map(len, tabs)), [1, 4])
        for tab in tabs:
            for axis, span in (('x', 'width'), ('y', 'height')):
                self.assertLessEqual(max(sum(rect[axis] <= start[axis] < rect[axis] + rect[span] for rect in tab)
                                         for start in tab), 2)

    def test_concurrent_creates_respect_the_same_tab_limit(self):
        group = 'parallel'
        config = self.layout_config(1, 2)
        self.create('existing', group=group, config=config)
        with ThreadPoolExecutor(max_workers=4) as pool:
            panes = list(pool.map(lambda i: self.create(f'parallel-{i}', group=group, config=config), range(4)))
        sizes = [len(tab) for tab in self.group_rectangles(group)]
        self.assertEqual(sum(sizes), 1 + len(panes))
        self.assertLessEqual(max(sizes), 2)

    def test_invalid_layout_config_refuses_create_without_launching(self):
        invalid = self.root / 'invalid-layout.json'
        before = self.call('layout')
        for content in ('{"layout":', '{"layout":{"max_rows":true,"max_cols":2}}',
                        '{"layout":{"max_rows":0,"max_cols":2}}', '{"layout":{"max_cols":2}}'):
            with self.subTest(content=content):
                invalid.write_text(content, encoding='utf-8')
                self.call('create', '--cwd', str(self.cwd), '--group', 'invalid', '--suffix', 'invalid',
                          '--argv', json.dumps([sys.executable, '-V']), '--config', str(invalid),
                          code='invalid_config')
                self.assertEqual(self.call('layout'), before)

    def test_create_launch_group_and_preserve_focus(self):
        self.new_workspace(PRIMARY, 'focused workspace', '--focus')
        first = self.create('one', group='create')
        literal = 'quoted "value" with spaces \\path\\'
        second = self.create('two', group='create', stdin=True,
                             argv=[sys.executable, '-u', '-c', 'import sys; print(repr(sys.argv[1:]))', literal, ''])
        self.assertNotEqual(first, second)
        self.wait_output(first, f'APP:{self.cwd}')
        self.wait_output(second, repr([literal, '']))
        layout = self.call('layout')
        tab = next(t for t in layout['tabs'] if any(p['pane_id'] == first for p in t['panes']))
        self.assertIn(f'{second} two', {p['label'] for p in tab['panes']})
        self.assertFalse(any(p['focused'] for p in tab['panes']))
        self.assertEqual([w['label'] for w in self.call('workspaces')['workspaces'] if w['focused']],
                         ['focused workspace'])

    def test_short_command_leaves_output_for_reading(self):
        shell = 'printf "DONE:%s\\n" "$PWD"; exit 7' if os.name != 'nt' else None
        pane = self.create('short', cmd=shell, argv=[sys.executable, '-u', '-c',
                           'import os,sys; print("DONE:" + os.getcwd()); sys.exit(7)'])
        self.assertIn(f'DONE:{self.cwd}', self.wait_output(pane, 'command exited: 7'))
        self.call('send', '--pane', pane, '--text', 'echo bad > post-exit-marker\r')
        time.sleep(.25)
        self.assertFalse((self.cwd / 'post-exit-marker').exists(), 'Post-exit input must not run in a shell')
        self.call('close', '--pane', pane)

    @unittest.skipUnless(os.name == 'nt', 'Windows executable resolution')
    def test_windows_discovery_ignores_current_directory(self):
        other_dir = self.root / 'cli cwd'
        other_dir.mkdir()
        (other_dir / 'herdr.exe').touch()
        self.assertEqual(self.call('layout', cli_cwd=other_dir)['workspace_id'], self.workspace)

    def test_argv_wrapper_does_not_import_from_working_directory(self):
        other_dir = self.root / 'app cwd'
        other_dir.mkdir()
        (other_dir / 'subprocess.py').write_text('raise RuntimeError("Local module must not load")', encoding='utf-8')
        pane = self.create('isolated', app_cwd=other_dir)
        self.wait_output(pane, f'APP:{other_dir}')

    @unittest.skipUnless(os.name == 'nt', 'Windows batch targets are not literal argv')
    def test_windows_argv_rejects_batch_files(self):
        self.call('create', '--cwd', str(self.cwd), '--group', 'validation', '--suffix', 'batch',
                  '--argv', '["test.CmD", "one & two"]', code='invalid_argument')

    @unittest.skipUnless(os.name == 'nt', 'Windows executable resolution')
    def test_windows_argv_resolves_bare_names_from_path(self):
        other_dir = self.root / 'bare executable cwd'
        other_dir.mkdir()
        (other_dir / 'whoami.exe').touch()
        pane = self.create('path', app_cwd=other_dir, argv=['whoami'])
        self.wait_output(pane, 'command exited: 0')

    @unittest.skipIf(os.name == 'nt', 'Headless Windows ConPTY does not reliably deliver synthetic Ctrl+C')
    def test_argv_child_can_handle_interrupt_and_finish(self):
        app = '''import signal, time, sys
interrupted = False
def handle(sig, frame):
    global interrupted
    interrupted = True
signal.signal(signal.SIGINT, handle)
print('APP:READY', flush=True)
while not interrupted:
    time.sleep(.05)
time.sleep(.8)
print('APP:FINISHED', flush=True)
sys.exit(9)
'''
        pane = self.create('signal', argv=[sys.executable, '-u', '-c', app])
        self.wait_output(pane, 'APP:READY')
        self.call('send', '--pane', pane, '--text', '\x03')
        self.assertIn('APP:FINISHED', self.wait_output(pane, 'command exited: 9'))

    def test_zoom_blocks_send_rename_close_and_allows_read(self):
        first = self.create('locked', group='zoom')
        second = self.create('sibling', group='zoom')
        self.wait_output(first, 'APP:')
        self.native(PRIMARY, 'pane', 'zoom', first, '--on')
        self.assertTrue(self.call('lock-status', '--pane', first)['user_locked'])
        self.assertFalse(self.call('lock-status', '--pane', second)['user_locked'])
        self.call('send', '--pane', first, '--text', 'forbidden\r', code='user_locked')
        self.call('rename', '--pane', first, '--suffix', 'forbidden', code='user_locked')
        self.call('close', '--pane', first, code='user_locked')
        self.call('close', '--pane', second, code='tab_user_locked')
        self.assertNotIn('forbidden', self.read(first))
        third = self.create('overflow', group='zoom')
        tabs = {p['pane_id']: t['tab_id'] for t in self.call('layout')['tabs'] for p in t['panes']}
        self.assertNotEqual(tabs[first], tabs[third])
        self.assertTrue(self.call('lock-status', '--pane', first)['user_locked'])
        self.native(PRIMARY, 'pane', 'zoom', first, '--off')
        self.call('send', '--pane', first, '--text', 'literal $PWD; <x>\r')
        self.wait_output(first, 'RECEIVED:literal $PWD; <x>')
        self.assertEqual(self.call('rename', '--pane', first, '--suffix', 'changed')['label'], f'{first} changed')
        self.call('close', '--pane', first)
        self.call('lock-status', '--pane', first, code='pane_not_found')

    def test_scope_paging_and_renderers(self):
        pane = self.create('<script>bad</script>', group='formats', argv=[sys.executable, '-u', '-c',
                           'import sys; print("first\\nlast", flush=True); sys.stdin.read()'])
        self.wait_output(pane, 'last')
        last = self.call('read', '--pane', pane, '--limit', '1')
        prev = self.call('read', '--pane', pane, '--limit', '1', '--before', str(last['next_before']))
        self.assertEqual(last['text'], 'last\n')
        self.assertEqual(prev['text'], 'first\n')
        another = self.new_workspace(PRIMARY, 'different workspace')['workspace_id']
        self.call('close', '--pane', pane, workspace=another, code='pane_not_found')
        self.call('close', '--pane', pane, session=SECONDARY, code='workspace_not_found')
        for fmt in ('json', 'html'):
            target = self.root / f'layout.{fmt}'
            self.call('layout', '--format', fmt, '--output', str(target))
            data = target.read_text(encoding='utf-8')
            if fmt == 'html':
                self.assertIn('&lt;script&gt;bad&lt;/script&gt;', data)
                self.assertNotIn('<script>', data)
            else:
                layout = json.loads(data)
                self.assertEqual(layout['workspace_id'], self.workspace)
                self.assertTrue(any(p['pane_id'] == pane and p['rect'] for t in layout['tabs'] for p in t['panes']))

    @unittest.skipUnless(os.name == 'nt' and os.environ.get('HERDR_TEST_WSL') == '1',
                         'Set HERDR_TEST_WSL=1 to test with a configured WSL distro and top')
    def test_wsl_live_top_to_proc_status(self):
        # PID 1 is stable. Filter top to PID 1 so unattended tests do not race transient processes.
        wsl = shutil.which('wsl.exe')
        self.assertIsNotNone(wsl)
        foo = self.create('foo', group='wsl', argv=[wsl, '--cd', '~', '--exec', '/usr/bin/env', 'LC_ALL=C',
                          'TERM=xterm-256color', '/usr/bin/top', '-d', '1', '-p', '1'])
        bar = self.create('bar', group='wsl', argv=[wsl, '--cd', '~', '--exec', '/usr/bin/env', 'PS1=PM-READY$ ',
                          '/bin/bash', '--noprofile', '--norc', '-i'])
        from_top = self.wait_output(foo, 'PID USER', source='visible')
        row = re.search(r'^\s*PID\s+[^\n]+\n\s*([1-9][0-9]*)[ \t]', from_top, re.M)
        if row is None:
            self.fail(f'Process row missing from top: {from_top!r}')
        self.wait_output(bar, 'PM-READY$')
        pid = row[1]
        self.call('send', '--pane', bar, '--text', f'cat /proc/{pid}/status && printf "PM-DONE\\n"\r')
        status = self.wait_output(bar, '\nPM-DONE')
        self.assertRegex(status, rf'(?m)^Pid:\s+{pid}$')
        self.assertRegex(status, r'(?m)^Name:\s+\S')


if __name__ == '__main__':
    unittest.main()
