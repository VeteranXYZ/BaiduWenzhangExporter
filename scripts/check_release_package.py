"""Build, inspect and install the wheel without using the source checkout at runtime."""
from __future__ import annotations

import json
import ast
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path
from zipfile import ZipFile

REQUIRED = ('ui/index.html', 'ui/app.css', 'ui/app.js', 'dashboard/index.html',
            'dashboard/dashboard.css', 'dashboard/dashboard.js', 'dashboard/logo.svg',
            'web/style.css', 'web/app.js')


def tracked_manifest(repo: Path) -> set[str]:
    output = subprocess.check_output(['git', 'ls-files', '-z'], cwd=repo)
    return set(output.decode('utf-8').rstrip('\0').split('\0'))


def audit_required_files(repo: Path, manifest: set[str]) -> None:
    required = {'pyproject.toml', 'README.md', 'requirements-dev.txt', 'requirements.txt',
                'baidu_archive/truth.py', 'baidu_archive/media.py', 'baidu_archive/safe_files.py',
                'baidu_archive/legacy.py', 'tests/fixtures/tiny.png', 'tests/fixtures/tiny.mp4',
                'tests/fixtures/tiny.wav'}
    required.update('baidu_archive/' + name for name in REQUIRED)
    for directory, pattern in [('baidu_archive', '*.py'), ('tests', '*.py'), ('tests/fixtures', '*')]:
        required.update(str(p.relative_to(repo)) for p in (repo / directory).glob(pattern) if p.is_file())
    # Also find relative runtime imports; an absent module must not escape the scan.
    for source in (repo / 'baidu_archive').glob('*.py'):
        for node in ast.walk(ast.parse(source.read_text(encoding='utf-8'))):
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
                module = node.module.replace('.', '/')
                required.add('baidu_archive/' + module + '.py')
    missing = sorted(name for name in required if name not in manifest or not (repo / name).is_file())
    if missing:
        raise RuntimeError('Required release files missing from Git manifest: ' + ', '.join(missing))


def copy_tracked_tree(repo: Path, project: Path, manifest: set[str]) -> None:
    project.mkdir()
    for name in sorted(manifest):
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise RuntimeError('Unsafe Git manifest entry')
        source = repo / relative
        if source.is_symlink() or not source.is_file():
            raise RuntimeError('Release manifest requires regular files: ' + name)
        destination = project / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def main() -> None:
    repo = Path(__file__).resolve().parents[1]
    manifest = tracked_manifest(repo)
    audit_required_files(repo, manifest)
    print(f'Git required-file audit PASS: {len(manifest)} tracked input files; untracked input files: 0', flush=True)
    with tempfile.TemporaryDirectory(prefix='baidu-package-check-') as temporary:
        root = Path(temporary)
        project = root / 'source'
        copy_tracked_tree(repo, project, manifest)
        wheels = root / 'wheels'
        subprocess.run([sys.executable, '-m', 'pip', 'wheel', '--no-deps', '--no-cache-dir',
                        '--wheel-dir', str(wheels), str(project)], check=True, cwd=root)
        wheel, = wheels.glob('*.whl')
        with ZipFile(wheel) as bundle:
            missing = [name for name in REQUIRED if 'baidu_archive/' + name not in bundle.namelist()]
            missing += [name for name in manifest if name.startswith('baidu_archive/') and name.endswith('.py') and name not in bundle.namelist()]
            if missing:
                raise RuntimeError('Wheel missing resources: ' + ', '.join(missing))
        environment = root / 'installed-env'
        venv.EnvBuilder(with_pip=True).create(environment)
        python = environment / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
        subprocess.run([str(python), '-m', 'pip', 'install', '--no-cache-dir', str(wheel)], check=True, cwd=root)
        subprocess.run([str(python), '-m', 'pip', 'install', '--no-cache-dir', 'pytest>=8'], check=True, cwd=root)
        # Tests import the installed wheel, from the clean manifest test directory.
        subprocess.run([str(python), '-m', 'pytest', '-p', 'no:cacheprovider',
                        '--basetemp=' + str(root / 'test-output')], check=True, cwd=project)
        node = shutil.which('node')
        if not node:
            raise RuntimeError('Node is required for the release hard gate')
        subprocess.run([node, '--test', 'tests/test_dashboard.cjs'], check=True, cwd=project)
        probe = '''
import importlib, importlib.resources, json, pathlib, re
import baidu_archive
package = importlib.resources.files('baidu_archive')
required = json.loads(REQUIRED)
for name in required:
    assert package.joinpath(name).is_file(), name
for folder in ('ui', 'dashboard'):
    html = package.joinpath(folder, 'index.html').read_text(encoding='utf-8')
    for name in re.findall(r'(?:src|href)=["\\\']([^"\\\']+)["\\\']', html):
        if not name.startswith(('#', '/', 'http:', 'https:', 'data:')):
            assert package.joinpath(folder, name).is_file(), name
for name in ('cli', 'server', 'selection', 'integrity', 'assets', 'auth', 'client',
             'config', 'export', 'locking', 'parsers', 'pipeline', 'site', 'state',
             'storage', 'tasks', 'utils', 'verify', 'media', 'truth', 'safe_files', 'legacy'):
    importlib.import_module('baidu_archive.' + name)
assert 'site-packages' in str(pathlib.Path(baidu_archive.__file__)), baidu_archive.__file__
print('Installed wheel: imports and all GUI/dashboard/reading resources PASS')
import threading, urllib.request
from baidu_archive.config import Config
from baidu_archive.server import Application, LocalServer
cfg = Config(root=pathlib.Path.cwd() / 'smoke-archive')
app = Application(cfg, pathlib.Path.cwd() / 'smoke-config.json')
service = LocalServer(('127.0.0.1', 0), app)
thread = threading.Thread(target=service.serve_forever)
thread.start()
try:
    for path in ('/', '/dashboard.css', '/dashboard.js', '/logo.svg',
                 '/api/bootstrap', '/api/library', '/api/job'):
        with urllib.request.urlopen('http://127.0.0.1:' + str(service.server_port) + path) as response:
            assert response.status == 200, path
            response.read()
finally:
    service.shutdown(); service.server_close(); thread.join(5)
    assert not thread.is_alive()
print('Installed Server startup and 7 HTTP endpoints PASS; process stopped')
# A real installed pipeline fixture; only its HTTP transport is synthetic.
import httpx
from baidu_archive.state import ArchiveState
from baidu_archive.client import ArchiveClient
from baidu_archive.auth import Credentials
from baidu_archive.pipeline import run_index
from baidu_archive.storage import save_raw
cfg.list_url_template = 'https://wenzhang.baidu.com/fav/list?start={start}&limit={limit}'
listing = json.dumps({'status': 0, 'data': {
    'html': '<li class="unit page" id="key_one"><h4>Package smoke</h4></li>',
    'totalCount': 1, 'currentCount': 1, 'start': 0, 'limit': 30}}).encode()
with ArchiveState(cfg.db_path) as state, ArchiveClient(cfg, Credentials()) as client:
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=listing)))
    assert run_index(cfg, state, client, force=True)['index_complete']
for folder, body in ((cfg.detail_dir, b'<iframe class="pcs-article-iframe" src="https://example.test/body"></iframe>'),
                     (cfg.content_dir, b'<h1 id="detailArticleTitle_x">Package smoke</h1><div id="detailArticleContent_x"><p>Installed archive body</p></div><div id="detailArticleFooter_x"></div>')):
    save_raw(folder / 'one.html', body, url='https://example.test/body', status_code=200)
'''.replace('REQUIRED', repr(json.dumps(REQUIRED)))
        subprocess.run([str(python), '-I', '-c', probe], check=True, cwd=root)
        subprocess.run([str(python), '-I', '-m', 'baidu_archive', '--help'], check=True, cwd=root,
                       stdout=subprocess.DEVNULL)
        executable = python.parent / ('baidu-archive.exe' if sys.platform == 'win32' else 'baidu-archive')
        subprocess.run([str(executable), '--version'], check=True, cwd=root)
        for command in ('parse', 'export', 'status'):
            subprocess.run([str(executable), '--root', str(root / 'smoke-archive'), command],
                           check=True, cwd=root, stdout=subprocess.DEVNULL)
        finish = """
from pathlib import Path
from baidu_archive.config import Config
from baidu_archive.state import ArchiveState
from baidu_archive.client import ArchiveClient
from baidu_archive.auth import Credentials
from baidu_archive.assets import run_assets
cfg = Config(root=Path.cwd() / 'smoke-archive')
folder = cfg.item_dir('one')
for name in ('content.md', 'content.txt'):
    assert 'Installed archive body' in (folder / name).read_text(), name
with ArchiveState(cfg.db_path) as state, ArchiveClient(cfg, Credentials()) as client:
    assert run_assets(cfg, state, client)['incomplete_items'] == 0
"""
        subprocess.run([str(python), '-I', '-c', finish], check=True, cwd=root)
        subprocess.run([str(executable), '--root', str(root / 'smoke-archive'), 'verify'],
                       check=True, cwd=root, stdout=subprocess.DEVNULL)
        result = subprocess.run([str(executable), '--root', str(root / 'empty-archive'), 'verify'],
                                cwd=root, stdout=subprocess.DEVNULL)
        if result.returncode != 1:
            raise RuntimeError('Unverified empty generation must fail verify')
        subprocess.run([str(python), '-m', 'pip', 'check'], check=True, cwd=root)
        print('Wheel build / contents / isolated install / CLI smoke / resources: PASS')


if __name__ == '__main__':
    main()
