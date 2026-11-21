"""Shared, read-only decisions about current index, resource and article facts."""
from __future__ import annotations

import json
from pathlib import Path

from .storage import IntegrityError, current_content_path, read_json, read_raw
from .utils import redact_url, sha256_bytes, safe_key_dirname


def resource_identity(asset: dict) -> str:
    # References/attributes may alias one download. The resolved URL is its identity.
    value = [asset.get('key'), asset.get('kind'), asset.get('resolved_url') or asset.get('original_url')]
    return sha256_bytes(json.dumps(value, ensure_ascii=False).encode())


def resource_fingerprint(body_sha: str | None, base: str | None, assets: list[dict]) -> str:
    references = sorted((a.get('kind') or '', a.get('attribute') or '', a.get('original_url') or '',
                         a.get('resolved_url') or '') for a in assets)
    return sha256_bytes(json.dumps([body_sha, base, references], ensure_ascii=False).encode())


def validate_downloaded_asset(config, asset: dict, *, expected_identity: str | None = None) -> dict:
    """Never turn a database success into a usable path without checking actual bytes.

    Rows read from an *unmigrated*, read-only legacy database have no identity column.
    They can be audited against their recorded URL. A migrated row with NULL identity
    is explicitly unverified until offline evidence rebuilding proves its relation.
    """
    def bad(reason):
        return {'ok': False, 'reason': reason, 'path': None}

    if asset.get('download_status') != 'ok':
        return bad('not_downloaded')
    identity = expected_identity or resource_identity(asset)
    relative = Path(asset.get('local_path') or '')
    folder = config.item_dir(asset['key'])
    allowed = folder / 'assets'
    target = folder / relative
    if (relative.is_absolute() or '..' in relative.parts or not relative.parts or
            relative.parts[0] != 'assets' or len(relative.parts) < 2 or
            not folder.resolve().is_relative_to(config.items_dir.resolve()) or
            not allowed.resolve().is_relative_to(folder.resolve()) or
            not target.resolve().is_relative_to(allowed.resolve()) or target.is_symlink()):
        return bad('asset_path_outside_item')
    try:
        from .safe_files import read_resource
        body, info = read_resource(config.root, target.relative_to(config.root))
        if asset.get('size') is not None and info.st_size != asset['size']:
            return bad('size_mismatch')
    except (OSError, ValueError):
        return bad('unsafe_or_missing_file')
    digest = asset.get('sha256')
    if not isinstance(digest, str) or len(digest) != 64 or sha256_bytes(body) != digest:
        return bad('sha256_mismatch')
    from .media import validate_media_response
    validation = validate_media_response(asset.get('kind'), body, asset.get('content_type'))
    if validation['status'] != 'accepted':
        return bad('invalid_media_' + validation['status'])
    if 'download_identity' in asset and asset.get('download_identity') != identity:
        return bad('legacy_unverified' if asset.get('download_identity') is None else 'resource_identity_mismatch')
    if asset.get('resource_identity') and asset['resource_identity'] != identity:
        return bad('resource_identity_mismatch')
    return {'ok': True, 'reason': None, 'path': target, 'body': body}


def is_source_placeholder(asset: dict, reason: str | None = None) -> bool:
    if reason == 'invalid_media_placeholder':
        return True
    error = str(asset.get('error') or '')
    return reason in (None, 'not_downloaded') and (
        'source_deleted_placeholder' in error or 'invalid_media_placeholder' in error)


def missing_resource(asset: dict, validation: dict) -> dict:
    """Record why one reference is absent from an export."""
    reason = validation['reason']
    if is_source_placeholder(asset, reason):
        status = 'source_placeholder'
    elif reason == 'legacy_unverified':
        status = 'unverified'
    elif reason != 'not_downloaded':
        status = 'failed_integrity'
    elif asset.get('download_attempted') or asset.get('download_status') == 'failed':
        status = 'download_failed'
        if asset.get('http_status') and asset['http_status'] != 200:
            reason = f"http_{asset['http_status']}"
        elif asset.get('http_status') == 200:
            reason = 'response_not_usable'
        elif (asset.get('error') or '').startswith(('ConnectError', 'ReadTimeout', 'ConnectTimeout')):
            reason = asset['error'].split(':', 1)[0]
    elif asset.get('kind') in {'embed', 'iframe', 'object'}:
        status = 'external_embed'
        reason = 'external_media_not_preserved'
    else:
        status = 'not_downloaded'
    url = asset.get('resolved_url') or asset.get('original_url') or ''
    return {'id': asset.get('id'), 'kind': asset.get('kind'), 'attribute': asset.get('attribute'),
            'status': status, 'reason': reason, 'http_status': asset.get('http_status'),
            'url': redact_url(url) if not url.startswith('data:') else None}


def index_truth(state) -> dict:
    generation = state.get_meta('index_generation')
    if not isinstance(generation, dict):
        return {'ok': False, 'run_id': None, 'expected': state.get_meta('total_count'),
                'indexed': 0, 'seen_keys': [], 'reason': 'generation_unverified'}
    keys = generation.get('seen_keys', [])
    expected = generation.get('declared_total')
    ok = (generation.get('complete') is True and generation.get('status') == 'completed'
          and generation.get('pagination_complete') is True and not generation.get('errors')
          and isinstance(expected, int) and not isinstance(expected, bool)
          and len(set(keys)) == len(keys) == expected)
    return {'ok': ok, 'run_id': generation.get('run_id'), 'expected': expected,
            'indexed': len(set(keys)), 'seen_keys': keys, 'reason': None if ok else 'index_incomplete'}


def article_truth(config, state, key: str) -> dict:
    """Effective status for CLI, Server, GUI and verify; does not mutate archives."""
    item = state.get_item(key) or {}
    status = item.get('status', 'pending')
    folder = config.item_dir(key)
    meta = read_json(folder / 'metadata.json', {}) or {}
    assets = state.assets_for(key)
    issues = []
    usable = {}
    try:
        source = current_content_path(folder)
        parsed = current_content_path(folder, prefer_local=False)
        if meta.get('original_sha256'):
            if sha256_bytes((folder / 'content.original.html').read_bytes()) != meta['original_sha256']:
                raise IntegrityError('original_body_mismatch')
        available = source.is_file() and meta.get('has_content_container') is not False
    except (IntegrityError, OSError):
        available = False
        issues.append('invalid_body')
    if meta.get('parsed_sha256'):
        for kind, directory in (('content', config.content_dir), ('detail', config.detail_dir)):
            base_path = directory / (safe_key_dirname(key) + '.html')
            expected_raw = meta.get(kind + '_sha256')
            if kind == 'detail' and not expected_raw:
                continue  # Offline content-only imports may lack detail snapshots.
            try:
                if read_raw(base_path).sha256 != expected_raw:
                    issues.append('stale_' + kind + '_generation')
            except (IntegrityError, OSError):
                issues.append('invalid_' + kind + '_snapshot')
    fingerprint = resource_fingerprint(meta.get('parsed_sha256'), meta.get('iframe_url_original'), assets)
    if meta.get('resource_fingerprint'):
        from .parsers import extract_assets
        if available:
            discovered = extract_assets(parsed.read_text(encoding='utf-8'), base_url=meta.get('iframe_url_original'))
            body_fingerprint = resource_fingerprint(meta.get('parsed_sha256'), meta.get('iframe_url_original'), discovered)
        else:
            body_fingerprint = resource_fingerprint(meta.get('parsed_sha256'), meta.get('iframe_url_original'), []) if meta.get('has_content_container') is False else None
        if meta['resource_fingerprint'] != fingerprint or fingerprint != body_fingerprint:
            issues.append('resource_generation_mismatch')
        if status == 'complete' and meta.get('localized_resource_fingerprint') != fingerprint:
            issues.append('localized_generation_mismatch')
    for asset in assets:
        expected = (asset.get('download_attempted') or
                    (asset.get('kind') == 'image' and config.download_images and asset.get('downloadable')) or
                    (meta.get('include_media') and asset.get('kind') not in {'image', 'link'} and asset.get('downloadable')))
        if asset.get('download_status') == 'ok':
            result = validate_downloaded_asset(config, asset)
            if result['ok']:
                usable[asset['id']] = result['path']
            else:
                issues.append(result['reason'])
        elif expected:
            issues.append('resource_not_downloaded')
    if status == 'complete' and not meta.get('parsed_sha256'):
        issues.append('body_generation_unverified')
    if status == 'complete' and (issues or not available):
        status = ('legacy_unverified' if 'legacy_unverified' in issues or 'body_generation_unverified' in issues else 'assets_partial') if available else 'warning'
    index = index_truth(state)
    if status == 'complete' and (not index['ok'] or key not in index['seen_keys']):
        status = 'parsed'
    complete = status == 'complete' and not issues and available and index['ok'] and key in index['seen_keys']
    return {'status': status, 'complete': complete, 'available': available,
            'issues': issues, 'usable_assets': usable, 'fingerprint': fingerprint}


def validated_reading_content(config, state, key: str) -> str:
    """Rebuild the reading copy from the current body and only validated assets.

    A damaged/missing local asset must not survive merely because an old local HTML
    still references it. This is read-only; raw and all saved derivative files remain.
    """
    from .assets import localize_html
    from .parsers import extract_assets
    folder = config.item_dir(key)
    meta = read_json(folder / 'metadata.json', {}) or {}
    if not meta.get('parsed_sha256'):
        source = current_content_path(folder)
        return source.read_text(encoding='utf-8') if source.is_file() else ''
    source = current_content_path(folder, prefer_local=False)
    content = source.read_text(encoding='utf-8')
    assets = state.assets_for(key)
    fingerprint = resource_fingerprint(meta['parsed_sha256'], meta.get('iframe_url_original'), assets)
    observed = resource_fingerprint(meta['parsed_sha256'], meta.get('iframe_url_original'),
                                    extract_assets(content, base_url=meta.get('iframe_url_original')))
    if fingerprint != observed or (meta.get('resource_fingerprint') and fingerprint != meta['resource_fingerprint']):
        raise IntegrityError('当前正文与资源集合不一致')
    mapping = {}
    for asset in assets:
        if validate_downloaded_asset(config, asset)['ok']:
            mapping[asset['original_url']] = asset['local_path']
            if asset.get('resolved_url'):
                mapping[asset['resolved_url']] = asset['local_path']
        elif asset['original_url'].startswith('assets/') and asset.get('resolved_url', '').startswith(('http:', 'https:')):
            mapping[asset['original_url']] = asset['resolved_url']
    return localize_html(content, mapping)


def resource_state(config, asset: dict) -> dict:
    """Expose saved legacy bytes separately from absent/failed/current verified resources."""
    result = validate_downloaded_asset(config, asset)
    if result['ok']:
        return {'state': 'verified', 'reason': None, 'saved': True}
    if result['reason'] == 'legacy_unverified':
        candidate = {k: v for k, v in asset.items() if k != 'download_identity'}
        bytes_result = validate_downloaded_asset(config, candidate)
        return {'state': 'legacy_unverified', 'saved': bytes_result['ok'],
                'reason': '文件来自旧版本，尚未验证文章与资源关系；可从已保存 raw 数据离线补证。'
                if bytes_result['ok'] else bytes_result['reason']}
    return {'state': 'missing' if result['reason'] in {'not_downloaded', 'unsafe_or_missing_file'} else 'failed',
            'reason': result['reason'], 'saved': False}
