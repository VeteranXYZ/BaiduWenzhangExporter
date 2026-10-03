"""Keep every repository Python source importable on the advertised minimum version."""
from __future__ import annotations

import ast
import importlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_repository_uses_python39_syntax_and_delayed_union_annotations():
    ignored = {'.git', '.venv', 'venv', 'archive', 'site', 'build', 'dist',
               '__pycache__', '.pytest_cache', 'node_modules'}
    files = []
    for directory, children, names in os.walk(ROOT):
        children[:] = [name for name in children if name not in ignored]
        files.extend(Path(directory) / name for name in names if name.endswith('.py'))
    assert files
    for path in files:
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path), feature_version=(3, 9))
        delayed = any(isinstance(node, ast.ImportFrom) and node.module == '__future__'
                      and any(alias.name == 'annotations' for alias in node.names) for node in tree.body)
        annotations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.arg) and node.annotation:
                annotations.append(node.annotation)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns:
                annotations.append(node.returns)
            elif isinstance(node, ast.AnnAssign):
                annotations.append(node.annotation)
        if any(isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr)
               for annotation in annotations for node in ast.walk(annotation)):
            assert delayed, f'{path}: union annotations require future annotations on Python 3.9'


def test_runtime_imports_and_cli_smoke():
    for source in (ROOT / 'baidu_archive').glob('*.py'):
        if source.stem not in {'__main__', 'gui'}:  # GUI's optional webview dependency requires a display.
            importlib.import_module('baidu_archive.' + source.stem)
    result = subprocess.run([sys.executable, '-m', 'baidu_archive', '--help'], cwd=ROOT,
                            capture_output=True, text=True, check=True)
    assert 'export-selection' in result.stdout and 'serve' in result.stdout
