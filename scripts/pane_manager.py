#!/usr/bin/env python3
"""Application-neutral CLI over the local Herdr socket API."""
import argparse
import html
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import unicodedata
import uuid

PROTOCOL = 22
GROUP_PREFIX = 'pane-manager: '
MAX_PAGE = 200
MAX_TAIL = 2000
FRAME_LIMIT = 8 * 1024 * 1024
# Preserve output without opening a command interpreter after the app exits.
LAUNCH = '/bin/sh -c "$1"; rc=$?; printf "\\n[pane-manager] command exited: %s\\n" "$rc"; while IFS= read -r _; do :; done'


class Failure(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.data = dict(code=code, message=message, **details)


def require(condition, code, message):
    if not condition:
        raise Failure(code, message)


def obj(value):
    require(isinstance(value, dict), 'incompatible_response', 'Expected a Herdr object')
    return value


def objects(value):
    require(isinstance(value, list), 'incompatible_response', 'Expected a Herdr array')
    return [obj(item) for item in value]


def string(value):
    require(isinstance(value, str) and bool(value), 'incompatible_response', 'Expected a Herdr identifier')
    return value


def label(value, name, *, allow_empty=False):
    require((allow_empty or value) and value.strip() == value and len(value) <= 120
            and all(unicodedata.category(ch) not in ('Cc', 'Cf', 'Cs') for ch in value),
            'invalid_argument', f'{name} must be at most 120 characters, without edge spaces or control characters')
    return value


class Transport:
    def __init__(self, instance):
        require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', instance), 'invalid_argument', 'Use an explicit local Herdr session name')
        binary = shutil.which('herdr')
        require(binary is not None, 'instance_unavailable', 'Cannot find herdr on PATH')
        # An inherited pane/socket must never silently retarget a named session.
        env = {k: v for k, v in os.environ.items() if not k.startswith('HERDR_') or k == 'HERDR_CONFIG_PATH'}
        try:
            res = subprocess.run([binary, '--session', instance, 'status', 'server', '--json'],
                                 env=env, capture_output=True, timeout=5, check=True)
            status = obj(json.loads(res.stdout))
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            raise Failure('instance_unavailable', 'Could not resolve the named running Herdr session') from e
        require(status.get('session') == instance and status.get('running') is True and status.get('status') == 'running',
                'instance_unavailable', 'Named Herdr session is not running')
        require(status.get('endpoint_compatible') is True and status.get('protocol') == PROTOCOL,
                'incompatible_protocol', f'Expected Herdr socket protocol {PROTOCOL}')
        self.path = Path(string(status.get('socket')))
        require(self.path.is_absolute(), 'instance_unavailable', 'Expected an absolute local socket path')
        self.identity = self.socket_identity()

    def socket_identity(self):
        try:
            info = self.path.lstat()
        except OSError as e:
            raise Failure('instance_unavailable', 'Herdr socket disappeared or cannot be inspected') from e
        require(stat.S_ISSOCK(info.st_mode) and info.st_uid == os.geteuid(), 'instance_unavailable', 'Herdr socket must be owned by this user')
        return info.st_dev, info.st_ino, info.st_ctime_ns

    def request(self, method, *, changes=False, **params):
        token = uuid.uuid4().hex
        frame = (json.dumps(dict(id=token, method=method, params=params)) + '\n').encode()
        attempted = False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(5)
                require(self.socket_identity() == self.identity, 'instance_changed', 'Herdr endpoint changed')
                conn.connect(str(self.path))
                require(self.socket_identity() == self.identity, 'instance_changed', 'Herdr endpoint changed')
                attempted = True
                conn.sendall(frame)
                with conn.makefile('rb') as reader:
                    raw = reader.readline(FRAME_LIMIT + 1)
            if not raw.endswith(b'\n') or len(raw) > FRAME_LIMIT:
                raise ValueError('incomplete or oversized reply')
            reply = json.loads(raw)
            if not isinstance(reply, dict) or reply.get('id') != token:
                raise ValueError('uncorrelated reply')
            if isinstance(reply.get('error'), dict):
                error = reply['error']
                raise Failure('herdr_rejected', 'Herdr rejected the operation', native_code=error.get('code'))
            if not isinstance(reply.get('result'), dict):
                raise ValueError('no result object')
            return reply['result']
        except (OSError, ValueError) as e:
            code = 'outcome_unknown' if changes and attempted else 'transport_error'
            raise Failure(code, f'No verified reply for {method}; inspect state before retrying a mutation') from e


class Manager:
    def __init__(self, transport, workspace):
        self.rpc = transport.request
        self.workspace = workspace

    def snapshot(self):
        snap = obj(self.rpc('session.snapshot').get('snapshot'))
        require(snap.get('protocol') == PROTOCOL, 'incompatible_protocol', 'Herdr snapshot protocol changed')
        matching = lambda row: row.get('workspace_id') == self.workspace
        require(sum(map(matching, objects(snap.get('workspaces')))) == 1, 'workspace_not_found', 'Workspace ID is not present in this instance')
        return {key: [row for row in objects(snap.get(key)) if matching(row)]
                for key in ('tabs', 'panes', 'layouts')}

    @staticmethod
    def select(rows, field, key, code):
        matches = [row for row in rows if row.get(field) == key]
        require(len(matches) == 1, code, f'Cannot uniquely resolve {field} in the requested workspace')
        return matches[0]

    def pane(self, snap, pane_id):
        return self.select(snap['panes'], 'pane_id', pane_id, 'pane_not_found')

    def view(self, snap, tab_id):
        view = self.select(snap['layouts'], 'tab_id', tab_id, 'lock_unknown')
        zoom = view.get('zoomed')
        require(type(zoom) is bool, 'lock_unknown', 'Herdr did not report an unambiguous zoom state')
        if zoom:
            focused = view.get('focused_pane_id')
            require(isinstance(focused, str) and any(p.get('pane_id') == focused and p.get('tab_id') == tab_id
                                                    for p in snap['panes']),
                    'lock_unknown', 'Herdr did not identify the zoomed pane')
        return view

    @staticmethod
    def locked(view, pane_id):
        return view['zoomed'] and view['focused_pane_id'] == pane_id

    def guard(self, snap, pane_id, *, writable=False, rearranges=False):
        row = self.pane(snap, pane_id)
        view = self.view(snap, string(row.get('tab_id')))
        locked = self.locked(view, pane_id)
        if writable:
            require(not locked, 'user_locked', 'The user has zoomed this pane')
            if rearranges:
                require(not view['zoomed'], 'tab_user_locked', 'Changing this tab could disturb a zoomed pane')
        return row, locked

    def create(self, command, cwd, group, suffix):
        label(group, 'group')
        label(suffix, 'suffix', allow_empty=True)
        require(command.strip() and '\0' not in command and len(command.encode()) <= 32768,
                'invalid_argument', 'Expected a nonempty command line of at most 32 KiB')
        require(Path(cwd).is_absolute() and Path(cwd).is_dir(), 'invalid_argument', 'cwd must be an existing absolute local directory')
        self.snapshot()  # Never let Herdr choose a workspace implicitly.
        tab_label = GROUP_PREFIX + group
        created = self.rpc('layout.apply', changes=True, workspace_id=self.workspace,
                           tab_label=tab_label, focus=False, root=dict(type='pane', cwd=cwd,
                           command=['/bin/sh', '-c', LAUNCH, 'pane-manager', command]))
        pane_id = None
        try:
            layout = obj(created.get('layout'))
            require(layout.get('workspace_id') == self.workspace, 'scope_mismatch', 'Herdr created a tab outside the requested workspace')
            pane_id = string(obj(layout.get('root')).get('pane_id'))
            tab_id = string(layout.get('tab_id'))
            self.rename(pane_id, suffix, expected_tab=tab_id)
        except Failure as e:
            raise Failure('partial_create', 'Herdr launched a pane but its identity or label could not be verified; do not retry blindly',
                          pane_id=pane_id, cause=e.data['code']) from e
        # Grouping is optional. A standalone tab still satisfies a successful create.
        try:
            grouped = self.group(pane_id, tab_id, tab_label)
        except Failure as e:
            return dict(pane_id=pane_id, label=self.name(pane_id, suffix), grouping='unconfirmed',
                        grouping_error=e.data['code'])
        return dict(pane_id=pane_id, label=self.name(pane_id, suffix), grouping=grouped)

    def group(self, pane_id, source_tab, tab_label):
        snap = self.snapshot()
        source, _ = self.guard(snap, pane_id, writable=True, rearranges=True)
        require(source.get('tab_id') == source_tab, 'scope_mismatch', 'New pane changed tabs before grouping')
        terminal = string(source.get('terminal_id'))
        for tab in snap['tabs']:
            tab_id = string(tab.get('tab_id'))
            if tab_id == source_tab or tab.get('label') != tab_label:
                continue
            members = [p for p in snap['panes'] if p.get('tab_id') == tab_id]
            if not (1 <= len(members) < 4):
                continue
            # Don't split arbitrary user panes in a tab renamed to match a group.
            if any(not isinstance(p.get('label'), str) or not p['label'].startswith(string(p.get('pane_id')) + ' ')
                   and p['label'] != p['pane_id'] for p in members):
                continue
            view = self.view(snap, tab_id)
            if view['zoomed']:
                continue
            options = []
            member_ids = {p['pane_id'] for p in members}
            for row in objects(view.get('panes')):
                target = string(row.get('pane_id'))
                rect = obj(row.get('rect'))
                w, h = rect.get('width'), rect.get('height')
                if target in member_ids and type(w) is int and type(h) is int and (w >= 48 or h >= 16):
                    direction = 'right' if w >= 48 and (w >= 3*h or h < 16) else 'down'
                    options.append((w*h, target, direction))
            if not options:
                continue
            _, target, direction = max(options)
            try:
                moved_reply = self.rpc('pane.move', changes=True, pane_id=pane_id, focus=False,
                                       destination=dict(type='tab', tab_id=tab_id, split=direction,
                                                        target_pane_id=target, ratio=0.5))
                if moved_reply.get('changed') is False:
                    return 'standalone'
            except Failure as e:
                if e.data['code'] == 'herdr_rejected':
                    return 'standalone'
                raise
            moved = self.pane(self.snapshot(), pane_id)
            require(moved.get('tab_id') == tab_id and moved.get('terminal_id') == terminal,
                    'outcome_unknown', 'Could not verify the same terminal in the chosen group')
            return 'grouped'
        return 'standalone'

    @staticmethod
    def name(pane_id, suffix):
        return f'{pane_id} {suffix}' if suffix else pane_id

    def rename(self, pane_id, suffix, expected_tab=None):
        label(suffix, 'suffix', allow_empty=True)
        row, _ = self.guard(self.snapshot(), pane_id, writable=True)
        terminal = string(row.get('terminal_id'))
        require(expected_tab is None or row.get('tab_id') == expected_tab, 'scope_mismatch', 'Pane changed tabs before labeling')
        name = self.name(pane_id, suffix)
        self.rpc('pane.rename', changes=True, pane_id=pane_id, label=name)
        try:
            renamed = self.pane(self.snapshot(), pane_id)
            require(renamed.get('terminal_id') == terminal and renamed.get('label') == name,
                    'outcome_unknown', 'Rename was not confirmed')
        except Failure as e:
            raise Failure('outcome_unknown', 'Rename was acknowledged but not verified', pane_id=pane_id, cause=e.data['code']) from e
        return dict(pane_id=pane_id, label=name)

    def close(self, pane_id):
        self.guard(self.snapshot(), pane_id, writable=True, rearranges=True)
        self.rpc('pane.close', changes=True, pane_id=pane_id)
        try:
            snap = self.snapshot()
            require(not any(p.get('pane_id') == pane_id for p in snap['panes']), 'outcome_unknown', 'Pane remains present')
        except Failure as e:
            if e.data['code'] != 'workspace_not_found':
                raise Failure('outcome_unknown', 'Close was acknowledged but not verified', pane_id=pane_id, cause=e.data['code']) from e
        return dict(pane_id=pane_id, closed=True)

    def send(self, pane_id, text):
        require(text and len(text.encode()) <= 65536, 'invalid_argument', 'Expected nonempty text of at most 64 KiB')
        self.guard(self.snapshot(), pane_id, writable=True)
        result = self.rpc('pane.send_text', changes=True, pane_id=pane_id, text=text)
        require(result.get('type') == 'ok', 'outcome_unknown', 'Send acknowledgement was not recognized')
        return dict(pane_id=pane_id, accepted=True)

    def read(self, pane_id, source, limit, before):
        require(1 <= limit <= MAX_PAGE and 0 <= before < MAX_TAIL and limit + before <= MAX_TAIL,
                'invalid_argument', f'Request up to {MAX_PAGE} lines within the most recent {MAX_TAIL} lines')
        row = self.pane(self.snapshot(), pane_id)
        read = obj(self.rpc('pane.read', pane_id=pane_id, source=source, format='text',
                           strip_ansi=True, lines=min(MAX_TAIL, limit + before + 1)).get('read'))
        require(read.get('pane_id') == pane_id and read.get('workspace_id') == self.workspace
                and read.get('tab_id') == row.get('tab_id'), 'scope_mismatch', 'Read returned a different pane')
        require(isinstance(read.get('text'), str), 'incompatible_response', 'Herdr did not return terminal text')
        lines = read['text'].splitlines(keepends=True)
        end = max(0, len(lines) - before)
        start = max(0, end - limit)
        return dict(pane_id=pane_id, source=source, text=''.join(lines[start:end]),
                    next_before=before + limit if start and before + limit < MAX_TAIL else None,
                    revision=read.get('revision'), truncated=read.get('truncated'))

    def lock_status(self, pane_id):
        _, locked = self.guard(self.snapshot(), pane_id)
        return dict(pane_id=pane_id, user_locked=locked)

    def layout(self):
        snap = self.snapshot()
        tabs = []
        for tab in snap['tabs']:
            tab_id = string(tab.get('tab_id'))
            view = self.view(snap, tab_id)
            geometry = {row.get('pane_id'): row.get('rect') for row in objects(view.get('panes'))}
            panes = [dict(pane_id=p['pane_id'], label=p.get('label'), focused=p.get('focused') is True,
                          user_locked=self.locked(view, p['pane_id']), rect=geometry.get(p['pane_id']))
                     for p in snap['panes'] if p.get('tab_id') == tab_id]
            tabs.append(dict(tab_id=tab_id, label=tab.get('label'), zoomed=view['zoomed'],
                             area=view.get('area'), panes=panes))
        return dict(schema_version=1, workspace_id=self.workspace, tabs=tabs)


def json_text(value):
    return json.dumps(value, ensure_ascii=False)


def html_table(layout):
    esc = lambda value: html.escape(str(value)) if value is not None else ''
    lines = ['<!doctype html>', '<html lang="en"><meta charset="utf-8"><title>Herdr pane layout</title>',
             '<body><table>', f'<caption>Workspace {esc(layout["workspace_id"])}</caption>',
             '<thead><tr><th>Tab</th><th>Pane ID</th><th>Pane label</th><th>Focused</th><th>User locked</th><th>x</th><th>y</th><th>Width</th><th>Height</th></tr></thead>', '<tbody>']
    for tab in layout['tabs']:
        for p in tab['panes']:
            rect = p['rect'] or {}
            cells = [f'{tab["tab_id"]} {tab["label"] or ""}', p['pane_id'], p['label'],
                     'yes' if p['focused'] else 'no', 'yes' if p['user_locked'] else 'no',
                     *(rect.get(k) for k in ('x', 'y', 'width', 'height'))]
            lines.append('<tr>' + ''.join(f'<td>{esc(v)}</td>' for v in cells) + '</tr>')
    return '\n'.join([*lines, '</tbody></table></body></html>'])


RENDERERS = {'json': json_text, 'html': html_table}


def write_output(path, contents):
    # Replacing atomically avoids a partial file and does not follow a destination symlink.
    target = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(contents + '\n')
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def parser():
    p = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = p.add_subparsers(dest='operation', required=True)
    for op in ('create', 'send', 'read', 'rename', 'lock-status', 'close', 'layout'):
        sub = commands.add_parser(op, allow_abbrev=False)
        sub.add_argument('--instance', required=True, help='explicit local Herdr session name')
        sub.add_argument('--workspace', required=True, help='exact Herdr workspace ID, not a label')
        if op not in ('create', 'layout'):
            sub.add_argument('--pane', required=True, help='exact Herdr pane ID')
        if op == 'create':
            sub.add_argument('--command', required=True)
            sub.add_argument('--cwd', required=True)
            sub.add_argument('--group', required=True)
        if op in ('create', 'rename'):
            sub.add_argument('--suffix', required=True)
        if op == 'send':
            text = sub.add_mutually_exclusive_group(required=True)
            text.add_argument('--text')
            text.add_argument('--stdin', action='store_true')
        if op == 'read':
            sub.add_argument('--limit', type=int, default=50)
            sub.add_argument('--before', type=int, default=0)
            sub.add_argument('--source', choices=('detection', 'visible'), default='detection')
        if op == 'layout':
            sub.add_argument('--format', choices=RENDERERS, default='json')
            sub.add_argument('--output', help='write to this file instead of stdout')
    return p


def main():
    a = parser().parse_args()
    try:
        manager = Manager(Transport(a.instance), a.workspace)
        if a.operation == 'layout':
            layout = dict(instance=a.instance, **manager.layout())
            rendered = RENDERERS[a.format](layout)
            if a.output is None:
                print(rendered)
                return 0
            write_output(a.output, rendered)
            result = dict(output=a.output, format=a.format)
        elif a.operation == 'create':
            result = manager.create(a.command, a.cwd, a.group, a.suffix)
        elif a.operation == 'send':
            text = sys.stdin.buffer.read(65537).decode('utf-8') if a.stdin else a.text
            result = manager.send(a.pane, text)
        elif a.operation == 'read':
            result = manager.read(a.pane, a.source, a.limit, a.before)
        elif a.operation == 'rename':
            result = manager.rename(a.pane, a.suffix)
        else:
            result = getattr(manager, a.operation.replace('-', '_'))(a.pane)
        print(json_text(dict(ok=True, result=result)))
        return 0
    except (Failure, OSError, UnicodeError, ValueError) as e:
        data = e.data if isinstance(e, Failure) else dict(code='local_error', message='Local path, output, or text error')
        print(json_text(dict(ok=False, error=data)))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
