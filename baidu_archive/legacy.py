"""Explicit offline rebuilding of legacy resource evidence; never fetch or replace bytes."""
from __future__ import annotations

from .parsers import extract_assets, parse_content_html, parse_detail_html
from .storage import (IntegrityError, current_content_path, read_json, read_raw,
                      write_json_atomic, write_text_atomic, write_once)
from .truth import resource_identity, resource_fingerprint, validate_downloaded_asset, index_truth
from .utils import sha256_bytes, safe_key_dirname
from .tasks import TaskControl


def rebuild_legacy_evidence(config, state, *, control=None) -> dict:
    control = control or TaskControl()
    result = {'verified': 0, 'legacy_unverified': 0, 'reasons': [], 'verified_articles': 0, 'legacy_unverified_articles': 0}
    for record in state.iter_records('page'):
        control.check()
        key = record['key']
        legacy = [a for a in state.assets_for(key) if a.get('download_status') == 'ok'
                  and a.get('download_identity') is None]
        folder = config.item_dir(key)
        metadata_path = folder / 'metadata.json'
        meta = read_json(metadata_path, {}) or {}
        if not legacy and meta.get('parsed_sha256'):
            continue
        proofs = []
        try:
            content = read_raw(config.content_dir / (safe_key_dirname(key) + '.html'))
            detail = read_raw(config.detail_dir / (safe_key_dirname(key) + '.html'))
            if (not meta.get('content_sha256') or meta['content_sha256'] != content.sha256 or
                    not meta.get('detail_sha256') or meta['detail_sha256'] != detail.sha256):
                raise IntegrityError('raw_snapshot_evidence_missing_or_stale')
            parsed = parse_content_html(content.text)
            body = (parsed['content_html'] or '').encode('utf-8')
            digest = sha256_bytes(body)
            if not parsed['has_container']:
                raise IntegrityError('saved_raw_has_no_body')
            source = current_content_path(folder, prefer_local=False)
            if not source.is_file() or source.read_bytes() != body:
                raise IntegrityError('saved_body_does_not_match_raw')
            original = folder / 'content.original.html'
            if not meta.get('original_sha256') or sha256_bytes(original.read_bytes()) != meta['original_sha256']:
                raise IntegrityError('original_body_evidence_missing_or_invalid')
            if meta.get('parsed_sha256') and meta['parsed_sha256'] != digest:
                raise IntegrityError('parsed_body_evidence_mismatch')
            base = parse_detail_html(detail.text, base_url=record.get('detail_url')).get('iframe_url')
            if not base or base != meta.get('iframe_url_original'):
                raise IntegrityError('saved_base_url_evidence_missing_or_mismatched')
            observed = extract_assets(body.decode('utf-8'), base_url=base)
            references = {(a['original_url'], a['attribute']): a for a in observed}
            saved = state.assets_for(key)
            if resource_fingerprint(digest, base, saved) != resource_fingerprint(digest, base, observed):
                raise IntegrityError('article_resource_relation_mismatch')
            for asset in legacy:
                control.check()
                reference = references.get((asset['original_url'], asset['attribute']))
                if not reference or resource_identity({**reference, 'key': key}) != resource_identity(asset):
                    raise IntegrityError('resolved_resource_identity_mismatch')
                candidate = {k: v for k, v in asset.items() if k != 'download_identity'}
                validation = validate_downloaded_asset(config, candidate)
                if not validation['ok']:
                    raise IntegrityError(validation['reason'])
                if asset.get('size') is None:
                    raise IntegrityError('saved_size_evidence_missing')
                proofs.append((asset, resource_identity(asset)))
        except (IntegrityError, OSError, ValueError) as exc:
            result['legacy_unverified'] += len(legacy)
            result['legacy_unverified_articles'] += 1
            result['reasons'].append({'key': key, 'reason': str(exc)})
            continue
        # Publish body evidence first: a crash can only leave resources unverified.
        write_once(folder / 'parsed' / (digest + '.html'), body)
        meta.update(parsed_sha256=digest, parsed_content_path='parsed/' + digest + '.html',
                    resource_fingerprint=resource_fingerprint(digest, base, saved))
        write_json_atomic(metadata_path, meta)
        with state._tx() as conn:
            for asset, identity in proofs:
                conn.execute('UPDATE assets SET resource_identity=?, download_identity=?, error=NULL WHERE id=?',
                             (identity, identity, asset['id']))
        result['verified'] += len(proofs)
        result['verified_articles'] += 1
        from .assets import localize_html, LOCALIZATION_VERSION
        refreshed = state.assets_for(key)
        mapping = {}
        for asset in refreshed:
            if validate_downloaded_asset(config, asset)['ok']:
                mapping[asset['original_url']] = asset['local_path']
                mapping[asset['resolved_url'] or asset['original_url']] = asset['local_path']
        localized = localize_html(body.decode('utf-8'), mapping)
        meta.update(localized_sha256=write_text_atomic(folder / 'content.local.html', localized),
                    localized_from_sha256=digest, localized_resource_fingerprint=meta['resource_fingerprint'],
                    localization_version=LOCALIZATION_VERSION)
        # The existing download scope remains binding; no failed resource is upgraded.
        required = [a for a in refreshed if a.get('download_attempted') or
                    (a.get('downloadable') and ((a['kind'] == 'image' and config.download_images) or
                     (meta.get('include_media') and a['kind'] not in {'image', 'link'})))]
        index = index_truth(state)
        status = ('assets_partial' if any(not validate_downloaded_asset(config, a)['ok'] for a in required)
                  else 'warning' if (state.get_item(key) or {}).get('warning')
                  else 'complete' if index['ok'] and key in index['seen_keys'] else 'parsed')
        meta['status'] = status
        write_json_atomic(metadata_path, meta)
        state.update_item(key, status=status)
    return result
