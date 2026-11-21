"""Exercises the CLI against two disposable, explicitly named Herdr servers."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/pane_manager.py'


class Live(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which('herdr'):
            raise unittest.SkipTest('Herdr executable required')
        temp = tempfile.TemporaryDirectory(prefix='pm-')
        cls.addClassCleanup(temp.cleanup)
        cls.root = Path(temp.name)
        cls.env = {k: v for k, v in os.environ.items() if not k.startswith('HERDR_')}
        cls.env.update({k: temp.name for k in (
            'HOME','XDG_CONFIG_HOME','XDG_STATE_HOME','XDG_DATA_HOME','XDG_RUNTIME_DIR')})
        cls.env['SHELL'] = '/bin/sh'
        for session in ('pm-a', 'pm-b'):
            proc = subprocess.Popen(['herdr', '--session', session, 'server'], env=cls.env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            cls.addClassCleanup(cls.stop, proc)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and proc.poll() is None:
                if cls.native(session, 'status', 'server', '--json').get('running'):
                    break
                time.sleep(.05)
            else:
                raise RuntimeError(f'Disposable server {session} did not start')
        cls.workspace = cls.new_workspace('pm-a')
        cls.other_workspace = cls.new_workspace('pm-a')
        cls.same_id = cls.new_workspace('pm-b')

    @staticmethod
    def stop(proc):
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)

    @classmethod
    def native(cls, session, *args):
        r = subprocess.run(['herdr', '--session', session, *args], env=cls.env,
                           capture_output=True, text=True, timeout=5)
        return json.loads(r.stdout or r.stderr)

    @classmethod
    def new_workspace(cls, session):
        return cls.native(session, 'workspace', 'create', '--cwd', str(cls.root))['result']['workspace']['workspace_id']

    def call(self, op, *args, session='pm-a', workspace=None, code=None):
        r = subprocess.run([sys.executable,str(SCRIPT),op,'--instance',session,
                           '--workspace',workspace or self.workspace,*args], env=self.env,
                           capture_output=True, text=True, timeout=12)
        self.assertIn(r.returncode, (0, 1), r.stderr)
        self.assertTrue(r.stdout, r.stderr)
        data = json.loads(r.stdout)
        self.assertEqual(r.returncode, 0 if code is None else 1, data)
        if code:
            self.assertEqual(data['error']['code'], code, data)
            return data['error']
        return data['result']

    def create(self, suffix, group='test', cmd='stty -echo; printf "APP:%s\\n" "$PWD"; exec cat'):
        return self.call('create','--cwd',str(self.root),'--group',group,'--suffix',suffix,'--command',cmd)['pane_id']

    def wait_output(self, pane, marker):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            page = self.call('read', '--pane', pane, '--limit', '50')
            if marker in page['text']:
                return page
            time.sleep(.05)
        self.fail(f'{marker!r} missing from pane output: {page!r}')

    def test_create_launch_group_and_preserve_focus(self):
        before = self.native('pm-a','api','snapshot')['result']['snapshot']['focused_pane_id']
        one = self.create('one', group='create')
        two = self.create('two', group='create')
        self.assertNotEqual(one,two)
        self.wait_output(one, f'APP:{self.root}')
        self.wait_output(two, f'APP:{self.root}')
        live = self.native('pm-a','api','snapshot')['result']['snapshot']
        lookup = {p['pane_id']:p for p in live['panes']}
        self.assertEqual(lookup[one]['tab_id'],lookup[two]['tab_id'])
        self.assertEqual(lookup[two]['label'],f'{two} two')
        self.assertEqual(live['focused_pane_id'],before)

    def test_short_command_leaves_output_for_reading(self):
        one = self.create('short', group='short', cmd="printf 'DONE:%s\\n' \"$PWD\"; exit 7")
        self.wait_output(one, 'command exited: 7')
        self.assertIn(f'DONE:{self.root}', self.call('read', '--pane', one)['text'])
        marker = self.root / 'post-exit-marker'
        try:
            self.call('send', '--pane', one, '--text', 'touch ./post-exit-marker\n')
            time.sleep(.25)
            self.assertFalse(marker.exists(), 'Post-exit input must never run as shell commands')
        finally:
            self.call('close', '--pane', one)

    def test_zoom_blocks_send_rename_close_and_allows_read(self):
        one = self.create('locked',group='zoom')
        two = self.create('sibling',group='zoom')
        self.wait_output(one,'APP:')
        self.native('pm-a','pane','zoom',one,'--on')
        self.assertEqual(self.call('lock-status','--pane',one)['user_locked'],True)
        self.assertEqual(self.call('lock-status','--pane',two)['user_locked'],False)
        self.call('send','--pane',one,'--text','forbidden\n',code='user_locked')
        self.call('rename','--pane',one,'--suffix','forbidden',code='user_locked')
        self.call('close','--pane',one,code='user_locked')
        self.call('close','--pane',two,code='tab_user_locked')
        snapshot = self.native('pm-a','api','snapshot')['result']['snapshot']
        self.assertEqual(next(p['label'] for p in snapshot['panes'] if p['pane_id']==one),f'{one} locked')
        self.assertNotIn('forbidden', self.call('read','--pane',one)['text'])
        third = self.create('overflow',group='zoom')
        snapshot = self.native('pm-a','api','snapshot')['result']['snapshot']
        panes = {p['pane_id']:p for p in snapshot['panes']}
        self.assertNotEqual(panes[one]['tab_id'],panes[third]['tab_id'])
        self.assertEqual(self.call('lock-status','--pane',one)['user_locked'],True)
        self.native('pm-a','pane','zoom',one,'--off')
        self.call('send','--pane',one,'--text','literal $PWD; <x>\n')
        self.wait_output(one,'literal $PWD; <x>')
        self.call('rename','--pane',one,'--suffix','changed')
        snapshot = self.native('pm-a','api','snapshot')['result']['snapshot']
        self.assertEqual(next(p['label'] for p in snapshot['panes'] if p['pane_id']==one),f'{one} changed')
        self.call('close','--pane',one)
        self.call('lock-status','--pane',one,code='pane_not_found')

    def test_scope_paging_and_renderers(self):
        one = self.create('<script>bad</script>',group='formats',cmd="printf 'first\\nlast\\n'; exec cat")
        self.wait_output(one,'last')
        last = self.call('read','--pane',one,'--limit','1')
        prev = self.call('read','--pane',one,'--limit','1','--before',str(last['next_before']))
        self.assertEqual(last['text'],'last\n')
        self.assertEqual(prev['text'],'first\n')
        self.call('close','--pane',one,workspace=self.other_workspace,code='pane_not_found')
        self.assertEqual(self.workspace,self.same_id)
        self.call('close','--pane',one,session='pm-b',code='pane_not_found')
        for fmt in ('json','html'):
            target=self.root / f'layout.{fmt}'
            self.call('layout','--format',fmt,'--output',str(target))
            data=target.read_text()
            if fmt=='html':
                self.assertIn('<table>',data)
                self.assertIn('&lt;script&gt;bad&lt;/script&gt;',data)
                self.assertNotIn('<script>',data)
            else:
                layout=json.loads(data)
                self.assertEqual(layout['workspace_id'],self.workspace)
                self.assertTrue(any(p['pane_id']==one and p['rect'] for t in layout['tabs'] for p in t['panes']))


if __name__=='__main__':
    unittest.main()
