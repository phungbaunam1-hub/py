"""Build the complete Python application as one reviewable, self-extracting .py file.

The payload is an explicit allowlist of source packages and documentation. User
configuration, browser profiles, credentials, installed libraries and build outputs
are never read. Third-party dependencies are installed only on an explicit request.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
from pathlib import Path
import textwrap
import zipfile


ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = ROOT / 'python'
PACKAGES = ('original', 'aimt_orderflow', 'aimt_market')
DOCUMENTS = ('README.md', 'RELEASE.md', 'requirements.txt', 'requirements-web.txt', 'requirements-dev.txt')


def source_files() -> dict[str, bytes]:
    """Read only known source locations; preserve the original file's exact bytes."""
    files = {'run_aimt.py': (PYTHON_ROOT / 'run_aimt.py').read_bytes()}
    for package in PACKAGES:
        for path in sorted((PYTHON_ROOT / package).rglob('*.py')):
            relative = path.relative_to(PYTHON_ROOT)
            if '__pycache__' in relative.parts:
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(PYTHON_ROOT.resolve()):
                raise ValueError(f'Source file must remain inside the source tree: {relative}')
            files[relative.as_posix()] = path.read_bytes()
    for name in DOCUMENTS:
        files[name] = (PYTHON_ROOT / name).read_bytes()
    for name in ('MARKET_METHODS.md', 'PUBLIC_DATA.md'):
        path = ROOT / 'docs' / name
        if path.is_file():
            files[f'docs/{name}'] = path.read_bytes()
    return files


def flattened_requirements(files: dict[str, bytes], name: str = 'requirements-web.txt') -> str:
    """Resolve the project's pinned include files without invoking pip."""
    result: list[str] = []
    active: set[str] = set()

    def read(filename: str) -> None:
        if filename in active:
            raise ValueError(f'Recursive requirements include: {filename}')
        active.add(filename)
        for raw in files[filename].decode('utf-8').splitlines():
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('-r '):
                read(line[3:].strip())
            elif line not in result:
                result.append(line)
        active.remove(filename)

    read(name)
    return '\n'.join(result) + '\n'


LAUNCHER = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI MT Trading: the complete original app and additive market workspace.

Run with Python 3.12. First install dependencies explicitly with:
    python AI_MT_Trading.py --install-deps
Then launch:
    python AI_MT_Trading.py
    python AI_MT_Trading.py --demo

This source file embeds only application source and documentation. Configuration
and browser profiles persist in the user's application-data directory. Third-party
libraries are separate dependencies; --print-requirements shows their exact pins.
Use --extract-source DIRECTORY to review the embedded source before running it.
"""
from __future__ import annotations

import atexit
import base64
import hashlib
import io
import multiprocessing
import os
from pathlib import Path, PurePosixPath
import sys
import tempfile
import zipfile

# PyInstaller dispatches its worker processes here, before CLI parsing or Qt.
if __name__ == '__main__':
    multiprocessing.freeze_support()

_BUNDLE_FORMAT_VERSION = 1
_PAYLOAD_SHA256 = {payload_sha256!r}
_SOURCE_SHA256 = {source_hashes!r}
_PINNED_REQUIREMENTS = {requirements!r}
_ARCHIVE_FILES = {archive_files!r}
_PAYLOAD_B85 = (
{encoded}
)
_SOURCE_TEMPORARY_DIRECTORY = None
_SOURCE_DIRECTORY = None


def _archive_bytes():
    payload = base64.b85decode(_PAYLOAD_B85)
    if hashlib.sha256(payload).hexdigest() != _PAYLOAD_SHA256:
        raise RuntimeError('The embedded application is damaged. Download a fresh copy.')
    return payload


def _extract_payload(directory):
    directory = Path(directory).expanduser().resolve()
    if directory.exists() and (not directory.is_dir() or any(directory.iterdir())):
        raise ValueError('The source destination must be a new or empty directory: ' + str(directory))
    with zipfile.ZipFile(io.BytesIO(_archive_bytes())) as archive:
        names = archive.namelist()
        if tuple(sorted(names)) != _ARCHIVE_FILES or len(set(names)) != len(names):
            raise RuntimeError('The embedded source manifest is invalid.')
        if sum(item.file_size for item in archive.infolist()) > 64 * 1024 * 1024:
            raise RuntimeError('The embedded source exceeds its extraction limit.')
        for item in archive.infolist():
            path = PurePosixPath(item.filename)
            mode = (item.external_attr >> 16) & 0o170000
            if (path.is_absolute() or '..' in path.parts or '\\\\' in item.filename
                    or mode not in (0, 0o100000)):
                raise RuntimeError('An unsafe embedded source path was rejected.')
        directory.mkdir(parents=True, exist_ok=True)
        for item in archive.infolist():
            destination = directory.joinpath(*PurePosixPath(item.filename).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open('xb') as target:
                target.write(archive.read(item))
    return directory


def _prepare_sources():
    global _SOURCE_TEMPORARY_DIRECTORY, _SOURCE_DIRECTORY
    if getattr(sys, 'frozen', False):
        return None  # The Windows executable already bundles all source modules.
    if _SOURCE_DIRECTORY is None:
        temporary = tempfile.TemporaryDirectory(prefix='aimt_single_')
        try:
            directory = _extract_payload(temporary.name)
        except BaseException:
            temporary.cleanup()
            raise
        _SOURCE_TEMPORARY_DIRECTORY = temporary
        _SOURCE_DIRECTORY = directory
        atexit.register(temporary.cleanup)
        sys.path.insert(0, str(directory))
    return _SOURCE_DIRECTORY


def _persistent_config():
    configured = os.environ.get('AIMT_CONFIG')
    if configured:
        config = Path(configured).expanduser().resolve()
    elif sys.platform == 'win32':
        base = Path(os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA')
                    or str(Path.home() / 'AppData' / 'Local'))
        config = base / 'AI_MT_Trading' / 'ai_mt_trading_config.json'
    else:
        base = Path(os.environ.get('XDG_DATA_HOME') or str(Path.home() / '.local' / 'share'))
        config = base / 'AI_MT_Trading' / 'ai_mt_trading_config.json'
    config.parent.mkdir(parents=True, exist_ok=True)
    os.environ['AIMT_CONFIG'] = str(config)
    return config


# Spawn imports the entry script as __mp_main__. Install package paths in that
# import too, and retain the TemporaryDirectory for the entire process lifetime.
if not getattr(sys, 'frozen', False):
    _prepare_sources()


def main():
    import argparse
    import subprocess

    parser = argparse.ArgumentParser(description='AI MT Trading: original app + crypto/order-flow workspace')
    utility = parser.add_mutually_exclusive_group()
    utility.add_argument('--install-deps', action='store_true', help='Install the pinned full dependencies with pip, then exit.')
    utility.add_argument('--extract-source', metavar='DIRECTORY', help='Extract reviewable source into a new or empty directory, then exit.')
    utility.add_argument('--print-requirements', action='store_true', help='Print pinned dependencies without installing or starting the app.')
    parser.add_argument('--demo', action='store_true', help='Use the original simulated broker; do not connect MT5.')
    parser.add_argument('--smoke-test', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.print_requirements:
        print(_PINNED_REQUIREMENTS, end='')
        return 0
    if args.extract_source:
        try:
            directory = _extract_payload(args.extract_source)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        print('Source extracted to: ' + str(directory))
        return 0
    if args.install_deps:
        if getattr(sys, 'frozen', False):
            parser.error('The Windows executable bundles its dependencies; use the .py file to install Python dependencies.')
        requirements = _prepare_sources() / 'requirements-web.txt'
        completed = subprocess.run([sys.executable, '-m', 'pip', 'install', '-r', str(requirements)], check=False)
        if completed.returncode == 0:
            print('Dependencies installed. Run this file again to open the application.')
        return completed.returncode
    _persistent_config()
    try:
        import run_aimt
        return run_aimt.main()
    except ModuleNotFoundError as exc:
        print('Missing Python dependency: ' + str(exc.name) + '. Run: python AI_MT_Trading.py --install-deps', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
'''


def build(output: Path) -> tuple[int, str]:
    files = source_files()
    compressed = io.BytesIO()
    with zipfile.ZipFile(compressed, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.create_system = 3  # Fixed ZIP metadata on Windows and Unix builders.
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    payload = compressed.getvalue()
    encoded = '\n'.join('    ' + repr(line) for line in textwrap.wrap(base64.b85encode(payload).decode('ascii'), width=100))
    source = LAUNCHER.format(
        payload_sha256=hashlib.sha256(payload).hexdigest(),
        source_hashes={name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())},
        requirements=flattened_requirements(files),
        archive_files=tuple(sorted(files)),
        encoded=encoded,
    )
    compile(source, str(output), 'exec')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(source, encoding='utf-8', newline='\n')
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix('.sha256.txt').write_text(f'{digest}  {output.name}\n', encoding='utf-8')
    return output.stat().st_size, digest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='downloads/AI_MT_Trading.py')
    args = parser.parse_args()
    output = Path(args.output)
    if not output.is_absolute():
        output = ROOT / output
    size, digest = build(output)
    print(f'{output}: {size:,} bytes; SHA-256 {digest}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
