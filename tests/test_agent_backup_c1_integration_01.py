"""Consumer integration of the frozen C1 exclusion API; synthetic stores only."""
import hashlib
import io
import json
import os
from pathlib import Path

import pytest
from core import agent_backup as ab
from core import agent_backup_export as ex
from core import suspension_backup as c1
from core import suspension as S
from core import operator_ingress as oi
from test_agent_backup_verified_export_01 import source, read_zip, NO_CREDENTIALS, NO_IDENTITY_TRAILERS


def export(source):
    data, base, destination = source
    return ex.export_incomplete_state(str(data), str(base), str(destination),
        owner_acknowledged=True, credentials_path=NO_CREDENTIALS,
        identity_trailers_path=NO_IDENTITY_TRAILERS)


def initialized(data):
    (data / 'memory').chmod(0o700)
    store = S.initialize(data, ingress=oi.local_suspension_control('init'))
    store.trip(store.capture(S.OWNER_SCOPE), trigger=S.Trigger.BREAKER_TRIPPED,
               evidence={'synthetic_selector': 'C1-SELECTOR-NEVER-ARCHIVE'})
    assert store.scope_state(S.OWNER_SCOPE).suspended is True
    store.close()


def protected_hashes(data):
    return {str(p.relative_to(data)): (hashlib.sha256(p.read_bytes()).hexdigest(),
            p.stat().st_ino, p.stat().st_mode, p.stat().st_mtime_ns)
            for p in (data / 'memory').rglob('*') if p.is_file()
            and c1.is_protected_relpath(str(p.relative_to(data)))}


@pytest.mark.parametrize('state', ['active', 'damaged', 'unreadable', 'future_pin', 'absent'])
def test_real_exclusion_exports_without_opening_protected_state(source, monkeypatch, state):
    data, _, destination = source
    if state != 'absent':
        initialized(data)
    if state == 'damaged':
        (data / 'memory' / S.STORE_NAME).write_bytes(b'DAMAGED-DO-NOT-PARSE')
    if state == 'future_pin':
        (data / 'memory' / S.IDENTITY_NAME).write_bytes(b'{"future_schema":999}')
    before = protected_hashes(data)
    if state == 'unreadable':
        (data / 'memory' / S.STORE_NAME).chmod(0)
    descriptor = c1.describe_exclusions(data)
    observations = []
    real_describe = c1.describe_exclusions
    def describe(root):
        result = real_describe(root)
        observations.append(result)
        return result
    monkeypatch.setattr(c1, 'describe_exclusions', describe)
    real_open = os.open
    def tripwire(path, *args, **kwargs):
        if isinstance(path, (str, bytes, os.PathLike)):
            try:
                st = os.stat(path, dir_fd=kwargs.get('dir_fd'))
            except FileNotFoundError:
                pass
            else:
                assert (st.st_dev, st.st_ino) not in descriptor.denied_identities(), 'protected C1 opened'
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(os, 'open', tripwire)
    manifest, receipt = export(source)
    assert ex.verify_export(io.BytesIO(destination.read_bytes()))['valid']
    assert len(observations) == 2
    assert manifest['c1_exclusion'] == observations[0].manifest_record()
    assert receipt['full_agent_coverage'] is False
    assert receipt['restorable_verified'] is False
    assert 'suspension' in receipt['coverage_blockers']
    assert receipt['c1_safety_coverage']['enforcement_compatibility'] == 'unproven'
    assert receipt['c1_safety_coverage']['exclusion'] == manifest['c1_exclusion']
    assert ex.confirm_export_result(str(destination), manifest, receipt)['verification'] == 'PASS'
    payloads = read_zip(destination)
    assert b'C1-SELECTOR-NEVER-ARCHIVE' not in b''.join(payloads.values())
    assert not any('suspension' in n or 'soul_identity' in n for n in payloads)
    with pytest.raises(ex.ExportUnavailable, match='suspension'):
        ex.export_verified_agent_backup(str(data), str(source[1]), str(destination.with_name('full.zip')))
    assert not destination.with_name('full.zip').exists()
    if state == 'unreadable':
        (data / 'memory' / S.STORE_NAME).chmod(0o600)
    assert protected_hashes(data) == before


def test_describe_refusal_precedes_any_capture(source, monkeypatch):
    def refuse(root):
        raise c1.ExclusionUnavailable('PROTECTED_ENTRY_UNOBSERVABLE')
    monkeypatch.setattr(c1, 'describe_exclusions', refuse)
    monkeypatch.setattr(ab, '_collect_members', lambda *a, **k: pytest.fail('capture before description'))
    with pytest.raises(ab.AgentBackupError, match='PROTECTED_ENTRY_UNOBSERVABLE'):
        export(source)
    assert not source[2].exists()


@pytest.mark.parametrize('alias_kind', ['hardlink', 'symlink'])
def test_protected_alias_never_opens_or_publishes(source, monkeypatch, alias_kind):
    data, base, dest = source
    initialized(data)
    descriptor = c1.describe_exclusions(data)
    protected = data / 'memory' / S.IDENTITY_NAME
    alias = base / 'skills/c1-alias.md'
    os.link(protected, alias) if alias_kind == 'hardlink' else alias.symlink_to(protected)
    before = protected_hashes(data)
    real_open = os.open
    def no_open(path, *a, **k):
        if os.fspath(path) == str(alias):
            pytest.fail('protected alias opened')
        return real_open(path, *a, **k)
    monkeypatch.setattr(os, 'open', no_open)
    with pytest.raises(ab.AgentBackupError, match='PROTECTED_ENTRY_ALIASED|C1_PROTECTED'):
        export(source)
    assert not dest.exists()
    assert protected_hashes(data) == before


def test_renamed_protected_inode_refused_before_open(source, monkeypatch):
    data, base, dest = source
    initialized(data)
    collect = ab._collect_members
    protected = data / 'memory' / S.IDENTITY_NAME
    alias = base / 'skills/moved.md'
    def move(*a, **k):
        protected.rename(alias)
        return collect(*a, **k)
    monkeypatch.setattr(ab, '_collect_members', move)
    real_open = os.open
    def tripwire(path, *a, **k):
        assert os.fspath(path) != str(alias), 'renamed protected inode opened'
        return real_open(path, *a, **k)
    monkeypatch.setattr(os, 'open', tripwire)
    with pytest.raises(ab.AgentBackupError, match='C1_PROTECTED'):
        export(source)
    assert not dest.exists()


@pytest.mark.parametrize('change', ['initialize', 'pin', 'store', 'evidence', 'memory', 'data_root'])
def test_boundary_change_at_last_publication_step_preserves_prior(source, monkeypatch, change):
    data, base, dest = source
    if change != 'initialize':
        initialized(data)
    if change == 'evidence':
        (data / 'memory' / S.EVIDENCE_DIRNAME).mkdir()
    prior = dest.with_name('prior.zip'); prior.write_bytes(b'PRIOR-BACKUP')
    real_fsync = os.fsync
    changed = False
    def mutate(fd):
        nonlocal changed
        real_fsync(fd)
        # Publication temp is fully written; mutate after all source reads.
        target = os.readlink('/proc/self/fd/' + str(fd))
        if '.incomplete_export_' in target and not changed:
            changed = True
            if change == 'initialize':
                initialized(data)
            elif change in ('pin', 'store'):
                p = data / 'memory' / (S.IDENTITY_NAME if change == 'pin' else S.STORE_NAME)
                p.rename(p.with_name('saved-original'))
                p.write_bytes(b'REPLACEMENT')
            elif change == 'evidence':
                p = data / 'memory' / S.EVIDENCE_DIRNAME
                p.rename(p.with_name('saved-evidence')); p.mkdir()
            elif change == 'memory':
                (data / 'memory').rename(data / 'old-memory'); (data / 'memory').mkdir()
            else:
                data.rename(data.with_name('old-data')); data.mkdir()
    monkeypatch.setattr(os, 'fsync', mutate)
    def no_link(*a, **k):
        pytest.fail('publication proceeded after boundary change')
    monkeypatch.setattr(os, 'link', no_link)
    with pytest.raises(ab.AgentBackupError, match='BOUNDARY_CHANGED'):
        export(source)
    assert changed and not dest.exists()
    assert prior.read_bytes() == b'PRIOR-BACKUP'
    assert not list(dest.parent.glob('.incomplete_export_*'))
    assert not list(dest.parent.glob('.agent_backup_*.zip.tmp'))


def test_sidecar_churn_is_available_and_excluded(source, monkeypatch):
    data, _, dest = source
    initialized(data)
    collect = ab._collect_members
    def churn(*a, **k):
        result = collect(*a, **k)
        (data / 'memory/suspension.db-churn').write_bytes(b'CHURN-NEVER-CAPTURE')
        (data / 'memory/soul_identity.json.abcd.tmp').write_bytes(b'TEMP-PIN-NEVER-CAPTURE')
        return result
    monkeypatch.setattr(ab, '_collect_members', churn)
    export(source)
    assert b'CHURN-NEVER-CAPTURE' not in b''.join(read_zip(dest).values())


@pytest.mark.parametrize('failure', ['existing', 'racing_destination', 'link_failure', 'write_failure'])
def test_failed_publication_never_overwrites_backup(source, monkeypatch, failure):
    dest = source[2]
    if failure == 'existing':
        dest.write_bytes(b'PRIOR')
    elif failure == 'write_failure':
        def fail_fsync(fd):
            if '.incomplete_export_' in os.readlink('/proc/self/fd/' + str(fd)):
                raise OSError('synthetic fsync failure')
        monkeypatch.setattr(os, 'fsync', fail_fsync)
    else:
        link = os.link
        def fail(src, dst, **kw):
            if failure == 'racing_destination':
                dest.write_bytes(b'PRIOR')
                return link(src, dst, **kw)
            raise OSError('synthetic publication failure')
        monkeypatch.setattr(os, 'link', fail)
    with pytest.raises((ab.AgentBackupError, OSError)):
        export(source)
    if failure in ('existing', 'racing_destination'):
        assert dest.read_bytes() == b'PRIOR'
    else:
        assert not dest.exists()
    assert not list(dest.parent.glob('.incomplete_export_*'))


@pytest.mark.parametrize('field,value', [('snapshot_captured', True), ('enforcement_compatibility', 'proven'),
    ('status', 'clear'), ('restore_fence', 'none'), ('suspended_at_capture', False),
    ('version', True), ('extra', 'authority')])
def test_c1_manifest_record_is_closed_and_non_authoritative(source, field, value):
    manifest, _ = export(source)
    manifest['c1_exclusion'][field] = value
    assert ex.validate_export_schema(manifest)


def test_no_duplicate_inventory_or_runtime_imports():
    text = Path(ex.__file__).read_text()
    assert '_TRACES' not in text and 'safety_observation' not in text
    assert 'from core.suspension import' not in text
    assert 'suspension_runtime' not in text


def test_version_probe_cannot_read_renamed_protected_inode(source, monkeypatch):
    import builtins
    data, base, dest = source
    initialized(data)
    original = ab._build_forbidden_identities
    target = base / 'main.py'
    def move(*args, **kwargs):
        result = original(*args, **kwargs)
        (data / 'memory' / S.IDENTITY_NAME).rename(target)
        return result
    monkeypatch.setattr(ab, '_build_forbidden_identities', move)
    real_open = builtins.open
    def tripwire(path, *args, **kwargs):
        assert os.fspath(path) != str(target), 'version probe opened C1 state'
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(builtins, 'open', tripwire)
    with pytest.raises(ab.AgentBackupError, match='C1_PROTECTED'):
        export(source)
    assert not dest.exists()


@pytest.mark.parametrize('relpath', ['memory/suspension.db', 'memory/suspension.db-arbitrary',
    'memory/soul_identity.json', 'memory/soul_identity.json.abc123.tmp',
    'memory/suspension_evidence/selector.json'])
@pytest.mark.parametrize('boundary', ['plain', 'sqlite'])
def test_all_source_open_boundaries_reject_protected_paths(source, relpath, boundary):
    data, _, dest = source
    exclusion = c1.describe_exclusions(data)
    forbidden = ab._build_forbidden_identities(None, None, _c1_exclusion=exclusion, _data_dir=str(data))
    path = data / relpath
    path.parent.mkdir(exist_ok=True, parents=True)
    path.write_bytes(b'NEW-PROTECTED-STATE')
    with pytest.raises(ab.AgentBackupError, match='C1_PROTECTED_PATH'):
        if boundary == 'plain':
            ab._open_state_bearing(str(path), str(data), forbidden)
        else:
            ab._open_verified(str(path), str(data), forbidden)
    assert not dest.exists()


def test_every_collector_gets_all_protected_identities(source, monkeypatch):
    data, _, _ = source
    initialized(data)
    descriptor = c1.describe_exclusions(data)
    collectors = ['_collect_database_members', '_collect_preference_members',
        '_collect_custom_tool_members', '_collect_audit_members', '_collect_overlay_dir',
        '_collect_project_members', '_collect_metadata_members']
    called = set()
    import inspect
    for name in collectors:
        original = getattr(ab, name)
        signature = inspect.signature(original)
        def check(*args, _name=name, _original=original, _signature=signature, **kwargs):
            bound = _signature.bind(*args, **kwargs)
            forbidden = bound.arguments['forbidden_identities']
            assert descriptor.denied_identities() <= set(forbidden.values())
            called.add(_name)
            return _original(*args, **kwargs)
        monkeypatch.setattr(ab, name, check)
    export(source)
    assert called == set(collectors)


def test_sqlite_sidecar_renamed_protected_inode_refused_before_connect(source, monkeypatch):
    import sqlite3
    data, _, dest = source
    initialized(data)
    original = ab._snapshot_sqlite
    sidecar = data / 'memory/lumina.db-wal'
    def snapshot(path, *a, **k):
        if str(path).endswith('/lumina.db'):
            (data / 'memory' / S.IDENTITY_NAME).rename(sidecar)
        return original(path, *a, **k)
    monkeypatch.setattr(ab, '_snapshot_sqlite', snapshot)
    connect = sqlite3.connect
    def no_connect(path, *a, **k):
        if 'lumina.db' in str(path):
            pytest.fail('SQLite reached renamed protected sidecar')
        return connect(path, *a, **k)
    monkeypatch.setattr(sqlite3, 'connect', no_connect)
    with pytest.raises(ab.AgentBackupError, match='C1_PROTECTED'):
        export(source)
    assert not dest.exists()


def test_sqlite_connection_newly_acquired_protected_inode_is_refused(source, monkeypatch):
    import sqlite3
    data, _, dest = source
    initialized(data)
    connect = sqlite3.connect
    protected = data / 'memory' / S.IDENTITY_NAME
    class ConnectionWithProtectedFd:
        def __init__(self, conn):
            self.conn = conn
            self.fd = os.open(protected, os.O_RDONLY)
        def __getattr__(self, name):
            return getattr(self.conn, name)
        def close(self):
            os.close(self.fd)
            self.conn.close()
    def substituted(path, *a, **k):
        conn = connect(path, *a, **k)
        return ConnectionWithProtectedFd(conn) if str(path).startswith('file:') and 'lumina.db?mode=ro' in str(path) else conn
    monkeypatch.setattr(sqlite3, 'connect', substituted)
    with pytest.raises(ab.AgentBackupError, match='C1_PROTECTED'):
        export(source)
    assert not dest.exists()
