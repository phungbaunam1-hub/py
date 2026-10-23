"""Package only the Python app and its tests/docs, excluding configs, caches and binaries."""
from pathlib import Path
import argparse
import hashlib
import zipfile

parser = argparse.ArgumentParser()
parser.add_argument('--output', default='downloads/AI-MT-Trading-Python-source.zip')
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
output = root / args.output
output.parent.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
    for item in sorted((root / 'python').rglob('*')):
        relative = item.relative_to(root / 'python')
        if any(part in {'__pycache__', '.pytest_cache', 'dist', 'build', 'webprofile'} for part in relative.parts):
            continue
        if item.is_file() and item.suffix in {'.py', '.txt', '.md', '.bat', '.ps1'}:
            archive.write(item, Path('AI-MT-Trading-Python') / relative)
with zipfile.ZipFile(output) as archive:
    assert archive.testzip() is None
with output.open('rb') as stream:
    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
output.with_suffix('.sha256.txt').write_text(f'{digest}  {output.name}\n')
print(f'{output}: {output.stat().st_size:,} bytes; SHA-256 {digest}')
