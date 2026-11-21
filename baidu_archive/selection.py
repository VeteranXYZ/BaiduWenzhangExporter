"""面向 CLI 与未来界面的离线选择导出，不启动抓取或读取凭证。"""

from __future__ import annotations

import json
import os
import tempfile
import zipfile
from pathlib import Path
from uuid import uuid4

from .config import Config
from .export import html_to_markdown, html_to_text
from .safe_files import read_resource
from .state import ArchiveState
from .storage import IntegrityError, current_content_path, read_json
from .utils import safe_key_dirname, sha256_bytes, sha256_file, utcnow_iso
from .tasks import TaskControl
from .truth import missing_resource, validate_downloaded_asset, validated_reading_content


class SelectionError(ValueError):
    pass


def export_preservation(config: Config, state: ArchiveState, output: Path) -> dict:
    """Keep the private archive evidence and database in a restorable ZIP."""
    files = []
    with tempfile.TemporaryDirectory(prefix='.state-snapshot-', dir=output.parent) as temporary:
        snapshot = Path(temporary) / 'state.db'
        state.backup_to(snapshot)
        with zipfile.ZipFile(output, 'x', zipfile.ZIP_DEFLATED) as bundle:
            def include(name: str, body: bytes) -> None:
                bundle.writestr(name, body)
                files.append({'path': name, 'bytes': len(body), 'sha256': sha256_bytes(body)})

            include('state.db', snapshot.read_bytes())
            for root in (config.raw_dir, config.items_dir):
                for current, dirs, names in os.walk(root):
                    folder = Path(current)
                    # Diagnostic runs aren't part of the archive and may include login responses.
                    if folder == config.raw_dir and 'diagnostics' in dirs:
                        dirs.remove('diagnostics')
                    if any((folder / name).is_symlink() for name in dirs):
                        raise IntegrityError('原始档案存在未验证的目录链接')
                    dirs.sort()
                    for name in sorted(names):
                        path = (folder / name).relative_to(config.root)
                        body, _ = read_resource(config.root, path)
                        include(path.as_posix(), body)
            bundle.writestr('manifest.json', json.dumps({'schema_version': 1, 'files': files},
                                                       ensure_ascii=False, indent=2) + '\n')
    return {'files': len(files), 'sha256': sha256_file(output)}


def _export_asset(config: Config, asset: dict) -> tuple[dict | None, dict | None]:
    result = validate_downloaded_asset(config, asset)
    if result['ok']:
        return result, None
    return None, missing_resource(asset, result)


def _publish_zip(temporary: str, target: Path, control: TaskControl) -> str:
    """保留原子写入的持久化语义，并在发布边界响应停止。"""
    control.check()
    with open(temporary, 'rb') as handle:
        os.fsync(handle.fileno())
    digest = sha256_file(temporary)
    control.check()
    os.link(temporary, target)
    try:
        control.check()
        if os.name == 'posix':
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        control.check()
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return digest


def export_images(config: Config, state: ArchiveState, *, keys: list[str],
                  output: Path | None = None, control: TaskControl | None = None,
                  log=None) -> dict:
    """只打包所选文章当前已下载的图片；未下载引用写入清单，不触发网络请求。"""
    wanted = set(keys)
    records = [r for r in state.iter_records() if r['key'] in wanted]
    if not wanted or {r['key'] for r in records} != wanted or any(r['type'] != 'page' for r in records):
        raise SelectionError('请选择本人收藏中的空间文章')
    target = Path(output) if output else config.root / 'exports' / f'images-{uuid4().hex}.zip'
    if target.suffix.lower() != '.zip' or target.exists():
        raise SelectionError('请使用尚不存在的 .zip 输出文件')
    control = control or TaskControl()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.images-', suffix='.tmp', dir=target.parent)
    os.close(fd)
    entries, files = [], []
    missing_total = 0
    published = False
    try:
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_STORED) as bundle:
            for index, record in enumerate(records, 1):
                control.check()
                key = record['key']
                folder = config.item_dir(key)
                metadata = read_json(folder / 'metadata.json', {}) or {}
                title = metadata.get('title') or record.get('title') or '无标题'
                prefix = f"articles/{index:04d}-{safe_key_dirname(title)[:60]}-{safe_key_dirname(key)}/"
                seen, missing, references, exported = set(), [], 0, 0
                for asset in state.assets_for(key):
                    control.check()
                    if asset['kind'] != 'image':
                        continue
                    references += 1
                    validation, issue = _export_asset(config, asset)
                    if issue:
                        missing.append({'reference': references, **issue})
                        continue
                    relative = Path(asset['local_path'])
                    body = validation['body']
                    digest = sha256_bytes(body)
                    if relative.as_posix() in seen:
                        continue
                    seen.add(relative.as_posix())
                    exported += 1
                    name = prefix + f'images/{exported:04d}-{safe_key_dirname(relative.name)}'
                    bundle.writestr(name, body)
                    files.append({'path': name, 'bytes': len(body), 'sha256': digest})
                missing_total += len(missing)
                entries.append({'key': key, 'title': title, 'directory': prefix,
                                'image_references': references, 'exported_images': exported,
                                'not_downloaded': missing})
                if log:
                    log('EXPORT', f'{index}/{len(records)} 篇文章已处理，已打包 {len(files)} 张图片')
            if not files:
                raise SelectionError('所选文章没有已下载的图片，请先抓取正文与图片或补齐图片')
            manifest = {'schema_version': 1, 'kind': 'images', 'generated_at': utcnow_iso(),
                        'count': len(entries), 'images': len(files), 'not_downloaded': missing_total,
                        'entries': entries, 'files': files}
            bundle.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
        # 同一目录中原子发布，不覆盖已有导出；失败或停止均清除中间文件。
        digest = _publish_zip(temporary, target, control)
        published = True
        control.check()
        return {'path': str(target), 'count': len(entries), 'images': len(files),
                'not_downloaded': missing_total, 'sha256': digest}
    except BaseException:
        if published:
            target.unlink(missing_ok=True)
        raise
    finally:
        Path(temporary).unlink(missing_ok=True)


def export_selection(config: Config, state: ArchiveState, *, keys: list[str],
                     formats: list[str], output: Path | None = None,
                     control: TaskControl | None = None) -> dict:
    """导出明确选择的文章为独立 ZIP。先验证全部输入，再原子发布一个完整文件。"""
    control = control or TaskControl()
    control.check()
    wanted = set(keys)
    if not wanted:
        raise SelectionError("请选择至少一篇文章")
    if not formats or not set(formats) <= {"md", "txt"}:
        raise SelectionError("选择导出支持 md / txt 格式")
    records = [record for record in state.iter_records() if record["key"] in wanted]
    if {record["key"] for record in records} != wanted:
        raise SelectionError("选择包含不在本人索引中的 key")
    if any(record["type"] != "page" for record in records):
        raise SelectionError("选择包含尚未归档正文的其他收藏")
    from .site import offline_reading_html

    files = {}
    entries = []
    missing_total = 0
    portable_media = 'md' in formats
    for record in records:
        control.check()
        key = record["key"]
        item_dir = config.item_dir(key)
        source = current_content_path(item_dir, prefer_local=False)
        if not source.is_file():
            raise SelectionError(f"文章尚未保存正文：{key}")
        metadata = read_json(item_dir / "metadata.json")
        if not isinstance(metadata, dict):
            raise SelectionError(f"文章缺少有效元数据：{key}")
        available_media, missing, origin_by_path = {}, [], {}
        for asset in state.assets_for(key):
            control.check()
            if asset['kind'] == 'link' or asset.get('scheme_kind') == 'data':
                continue
            if not portable_media:
                result = validate_downloaded_asset(config, asset)
                if not result['ok']:
                    missing.append(missing_resource(asset, result))
                continue
            validation, issue = _export_asset(config, asset)
            if issue:
                missing.append(issue)
            else:
                available_media[asset['local_path']] = validation['body']
            if asset.get('local_path'):
                origin_by_path[asset['local_path']] = asset.get('resolved_url') or asset['original_url']
        content = validated_reading_content(config, state, key)
        base = metadata.get('iframe_url_original') or metadata.get('detail_url')
        offline = offline_reading_html(content, available_media, base_url=base, origin_by_path=origin_by_path)
        prefix = f"articles/{safe_key_dirname(key)}/"
        for fmt in dict.fromkeys(formats):
            control.check()
            text = html_to_markdown(offline) if fmt == "md" else html_to_text(content)
            if not text:
                text = '［此篇正文没有可提取的文字。媒体和原始地址请查阅归档及缺失清单。］'
            files[prefix + "content." + fmt] = (text + "\n").encode("utf-8")
        exported_metadata = {name: metadata.get(name) for name in (
            "key", "title", "list_title", "content_title", "list_date", "content_date",
            "tags", "content_tags", "source", "list_source", "content_source", "note",
            "global_position", "content_sha256", "parsed_sha256", "warnings",
        )}
        files[prefix + "metadata.json"] = (json.dumps(exported_metadata, ensure_ascii=False, indent=2) + "\n").encode()
        for relative, body in available_media.items():
            files[prefix + relative] = body
        missing_total += len(missing)
        entries.append({"key": key, "title": metadata.get("title"), "directory": prefix,
                        "global_position": record.get("global_position"), "missing_resources": missing})

    manifest = {"schema_version": 1, "generated_at": utcnow_iso(), "count": len(entries),
                "formats": list(dict.fromkeys(formats)), "entries": entries, "missing_resources": missing_total,
                "files": [{"path": name, "bytes": len(body), "sha256": sha256_bytes(body)}
                          for name, body in files.items()]}
    files["manifest.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
    target = Path(output) if output else config.root / "exports" / f"selection-{uuid4().hex}.zip"
    if target.suffix.lower() != ".zip":
        raise SelectionError("输出文件必须是 .zip")
    if target.exists():
        raise SelectionError("输出文件已存在，请使用新的文件名")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.selection-', suffix='.tmp', dir=target.parent)
    os.close(fd)
    published = False
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, body in files.items():
                control.check()
                archive.writestr(name, body)
        digest = _publish_zip(temporary, target, control)
        published = True
        control.check()
        return {"path": str(target), "count": len(entries), "files": len(files),
                "missing_resources": missing_total, "sha256": digest}
    except BaseException:
        if published:
            target.unlink(missing_ok=True)
        raise
    finally:
        Path(temporary).unlink(missing_ok=True)
