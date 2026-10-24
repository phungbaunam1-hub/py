"""Verify the delivered single .py, including actual GUI startup and spawn imports."""
from __future__ import annotations

import ast
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[2]
ORIGINAL_SHA256 = 'b6265fb39c54e0235926be87544c5cdf177ad7728e33d582ff0fdc3554fc0909'


@pytest.fixture(scope='module')
def single_file(tmp_path_factory):
    destination = tmp_path_factory.mktemp('single-file') / 'AI_MT_Trading.py'
    completed = subprocess.run(
        [sys.executable, str(ROOT / 'scripts' / 'package-python-single.py'), '--output', str(destination)],
        cwd=ROOT, capture_output=True, text=True, timeout=20,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return destination


def constants(path):
    tree = ast.parse(path.read_text(encoding='utf-8'))
    return {node.targets[0].id: ast.literal_eval(node.value) for node in tree.body
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in {'_BUNDLE_FORMAT_VERSION', '_PAYLOAD_B85', '_PAYLOAD_SHA256',
                                       '_ARCHIVE_FILES', '_SOURCE_SHA256', '_PINNED_REQUIREMENTS'}}


def isolated_env(directory):
    env = os.environ.copy()
    # This is a launch of the delivered file, without the repository's PYTHONPATH.
    env.pop('PYTHONPATH', None)
    env['QT_QPA_PLATFORM'] = 'offscreen'
    env['XDG_CACHE_HOME'] = str(directory / 'cache')
    env['XDG_DATA_HOME'] = str(directory / 'data')
    env['AIMT_CONFIG'] = str(directory / 'persistent' / 'ai_mt_trading_config.json')
    return env


def test_payload_keeps_exact_original_and_all_feature_modules(single_file):
    metadata = constants(single_file)
    payload = base64.b85decode(metadata['_PAYLOAD_B85'])
    assert hashlib.sha256(payload).hexdigest() == metadata['_PAYLOAD_SHA256']
    assert metadata['_BUNDLE_FORMAT_VERSION'] == 1
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert archive.testzip() is None
        assert tuple(sorted(archive.namelist())) == metadata['_ARCHIVE_FILES']
        assert hashlib.sha256(archive.read('original/AI_MT_Trading.py')).hexdigest() == ORIGINAL_SHA256
        for name, digest in metadata['_SOURCE_SHA256'].items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == digest
        for package in ('original', 'aimt_orderflow', 'aimt_market'):
            for path in (ROOT / 'python' / package).rglob('*.py'):
                if '__pycache__' not in path.parts:
                    relative = path.relative_to(ROOT / 'python').as_posix()
                    assert archive.read(relative) == path.read_bytes(), relative
        assert archive.read('run_aimt.py') == (ROOT / 'python' / 'run_aimt.py').read_bytes()
        forbidden = {'.git', '.env', '__pycache__', 'webprofile', 'venv', '.venv', 'build', 'dist'}
        for name in archive.namelist():
            path = Path(name)
            assert not forbidden.intersection(path.parts)
            assert path.suffix in {'.py', '.txt', '.md'}
            assert path.suffix.lower() != '.json'
        assert 'aimt_orderflow/async_dispatch.py' in archive.namelist()
        assert 'aimt_market/backtest.py' in archive.namelist()


def test_generator_is_reproducible_and_writes_matching_checksum(single_file, tmp_path):
    other = tmp_path / 'same.py'
    completed = subprocess.run(
        [sys.executable, str(ROOT / 'scripts' / 'package-python-single.py'), '--output', str(other)],
        capture_output=True, text=True, timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    assert other.read_bytes() == single_file.read_bytes()
    digest = hashlib.sha256(other.read_bytes()).hexdigest()
    assert other.with_suffix('.sha256.txt').read_text().strip() == f'{digest}  same.py'


def test_requirements_are_reviewable_without_gui_or_config(single_file, tmp_path):
    completed = subprocess.run(
        [sys.executable, str(single_file), '--print-requirements'], cwd=tmp_path,
        env=isolated_env(tmp_path), capture_output=True, text=True, timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == constants(single_file)['_PINNED_REQUIREMENTS']
    assert 'PyQt6-WebEngine==6.10.0' in completed.stdout
    assert 'MetaTrader5==5.0.6231; sys_platform == "win32"' in completed.stdout
    assert 'websocket-client==1.9.0' in completed.stdout
    assert not (tmp_path / 'persistent').exists()


def test_extract_source_is_complete_and_refuses_overwrite(single_file, tmp_path):
    directory = tmp_path / 'review'
    command = [sys.executable, str(single_file), '--extract-source', str(directory)]
    first = subprocess.run(command, cwd=tmp_path, env=isolated_env(tmp_path),
                           capture_output=True, text=True, timeout=10)
    assert first.returncode == 0, first.stdout + first.stderr
    original = directory / 'original' / 'AI_MT_Trading.py'
    assert hashlib.sha256(original.read_bytes()).hexdigest() == ORIGINAL_SHA256
    assert (directory / 'requirements-web.txt').is_file()
    assert (directory / 'aimt_market' / 'workspace.py').is_file()
    second = subprocess.run(command, cwd=tmp_path, env=isolated_env(tmp_path),
                            capture_output=True, text=True, timeout=10)
    assert second.returncode != 0
    assert 'new or empty' in second.stderr
    assert hashlib.sha256(original.read_bytes()).hexdigest() == ORIGINAL_SHA256


def test_spawn_child_imports_all_packages_with_temporary_sources_alive(single_file, tmp_path):
    helper = tmp_path / 'spawn_check.py'
    helper.write_text(textwrap.dedent('''\
        import json
        import multiprocessing
        from pathlib import Path
        import runpy
        import sys

        # The same name used by Windows multiprocessing's source bootstrap.
        bundle = runpy.run_path(sys.argv[1], run_name='__mp_main__')

        def child(queue):
            import aimt_market.quant
            import aimt_market.stream
            import aimt_orderflow.async_dispatch
            import original
            import run_aimt
            root = Path(run_aimt.__file__).parent
            queue.put({'source_exists': root.is_dir(),
                       'original_exists': (root / 'original' / 'AI_MT_Trading.py').is_file(),
                       'child_bootstrapped': str(bundle['_SOURCE_DIRECTORY']) == str(root)})

        if __name__ == '__main__':
            context = multiprocessing.get_context('spawn')
            queue = context.Queue()
            process = context.Process(target=child, args=(queue,))
            process.start()
            result = queue.get(timeout=15)
            process.join(timeout=15)
            assert process.exitcode == 0
            print(json.dumps(result))
        '''), encoding='utf-8')
    completed = subprocess.run(
        [sys.executable, str(helper), str(single_file)], cwd=tmp_path,
        env=isolated_env(tmp_path), capture_output=True, text=True, timeout=35,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout) == {'source_exists': True, 'original_exists': True,
                                          'child_bootstrapped': True}


def test_frozen_launch_uses_bundled_modules_without_unpacking(single_file, tmp_path):
    # Exercise the frozen branch without requiring a Windows binary on Linux.
    script = textwrap.dedent('''\
        import json
        import os
        import runpy
        import sys
        import tempfile
        from types import SimpleNamespace

        def forbidden_unpack(*args, **kwargs):
            raise AssertionError('Frozen launch must use bundled source modules')

        tempfile.TemporaryDirectory = forbidden_unpack
        sys.frozen = True
        launcher = sys.argv[1]
        state = runpy.run_path(launcher, run_name='single_frozen_import')
        assert state['_SOURCE_DIRECTORY'] is None
        sys.modules['run_aimt'] = SimpleNamespace(main=lambda: 27)
        sys.argv = [launcher]
        assert state['main']() == 27
        print(json.dumps({'config': os.environ['AIMT_CONFIG']}))
        ''')
    completed = subprocess.run(
        [sys.executable, '-c', script, str(single_file)], cwd=tmp_path,
        env=isolated_env(tmp_path), capture_output=True, text=True, timeout=10,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout)['config'] == str(tmp_path / 'persistent' / 'ai_mt_trading_config.json')


def test_single_file_smoke_starts_full_original_gui_from_isolated_directory(single_file, tmp_path):
    env = isolated_env(tmp_path)
    report = tmp_path / 'smoke' / 'report.json'
    env['AIMT_SMOKE_REPORT'] = str(report)
    completed = subprocess.run(
        [sys.executable, str(single_file), '--smoke-test'], cwd=tmp_path,
        env=env, capture_output=True, text=True, timeout=45,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(report.read_text(encoding='utf-8'))
    assert result['ok']
    assert result['original_tabs'] == ['Phân tích', 'Tài khoản', 'Lệnh', 'Nhật ký']
    assert result['analysis_results'] == 8
    assert result['has_panel_chat_web'] and result['research_demo_chart_loaded']
    assert result['chart_tab_labels'][-1] == '+'
    assert result['research_chart_blocks_mt5_entries']
    assert result['ai_context_has_workspace']
    assert result['hotkey_confirmation_default']
    assert (tmp_path / 'persistent' / 'ai_mt_trading_config.json').is_file()
    assert report.with_suffix('.png').is_file()
    assert not (tmp_path / 'ai_mt_trading_config.json').exists()
