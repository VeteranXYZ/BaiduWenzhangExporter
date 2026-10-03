"""按实际字节检查快照、版本引用、正文与资源，不依赖成功状态推断完整性。"""

from __future__ import annotations

from pathlib import Path

from .config import Config
from .state import ArchiveState
from .truth import article_truth, validate_downloaded_asset
from .storage import IntegrityError, current_content_path, read_json, read_raw
from .utils import safe_key_dirname, sha256_file


def audit_stored_files(config: Config, state: ArchiveState) -> dict:
    checked = 0
    unverified = 0
    issues = []
    current_issues = []
    historical_issues = []

    def problem(path: Path, kind: str) -> None:
        issues.append({"path": str(path.relative_to(config.root)), "kind": kind})

    def check(path: Path, expected: str | None) -> None:
        nonlocal checked, unverified
        if not path.is_file():
            problem(path, "missing_file")
        elif not expected:
            unverified += 1
        elif sha256_file(path) != expected:
            problem(path, "sha256_mismatch")
        else:
            checked += 1

    for path in config.raw_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.name.endswith(".latest.json"):
            base = path.with_name(path.name.removesuffix(".latest.json"))
            try:
                read_raw(base)
            except (IntegrityError, OSError):
                problem(path, "invalid_snapshot_reference")
        elif path.name.endswith(".meta.json"):
            metadata = read_json(path)
            if not isinstance(metadata, dict) or not metadata.get("sha256"):
                problem(path, "invalid_snapshot_metadata")
            else:
                check(path.with_name(path.name.removesuffix(".meta.json")), metadata["sha256"])
        elif path.suffix in {".html", ".json", ".bin"} and not Path(str(path) + ".meta.json").exists():
            unverified += 1
    for directory in config.raw_dir.rglob("*.versions"):
        if directory.is_dir() and any(directory.iterdir()):
            base = directory.with_name(directory.name.removesuffix(".versions"))
            if not Path(str(base) + ".latest.json").exists():
                problem(directory, "missing_snapshot_reference")

    for record in state.iter_records("page"):
        item_dir = config.item_dir(record["key"])
        metadata_path = item_dir / "metadata.json"
        if not metadata_path.exists():
            if (state.get_item(record["key"]) or {}).get("status") in {"parsed", "assets_partial", "complete", "warning"}:
                problem(metadata_path, "missing_item_metadata")
            continue
        metadata = read_json(metadata_path)
        if not isinstance(metadata, dict):
            problem(metadata_path, "invalid_item_metadata")
            continue
        if metadata.get("has_content_container") is not False or metadata.get("original_sha256"):
            check(item_dir / "content.original.html", metadata.get("original_sha256"))
        if metadata.get("localized_sha256"):
            check(item_dir / "content.local.html", metadata["localized_sha256"])
        try:
            parsed = current_content_path(item_dir, prefer_local=False)
            if metadata.get("parsed_sha256"):
                check(parsed, metadata["parsed_sha256"])
        except IntegrityError:
            problem(metadata_path, "invalid_parsed_version")
        for kind, directory in (("content", config.content_dir), ("detail", config.detail_dir)):
            base = directory / f"{safe_key_dirname(record['key'])}.html"
            if not base.exists():
                problem(base, "missing_file")
                continue
            try:
                latest = read_raw(base)
                if metadata.get(f"{kind}_sha256") != latest.sha256:
                    problem(metadata_path, f"stale_{kind}_derivative")
            except (IntegrityError, OSError):
                problem(base, "invalid_snapshot")

        facts = article_truth(config, state, record['key'])
        for reason in facts['issues']:
            if reason in {'resource_generation_mismatch', 'localized_generation_mismatch'}:
                problem(metadata_path, reason)
    # Resources and retained versions are independent evidence. A missing article
    # metadata file must never skip the history audit or claim history is intact.
    for record in state.iter_records():
        metadata_path = config.item_dir(record['key']) / 'metadata.json'
        metadata = read_json(metadata_path, {}) or {}
        if not isinstance(metadata, dict):
            metadata = {}
        for asset in state.assets_for(record['key'], include_inactive=True):
            if asset.get('download_status') != 'ok':
                required = asset.get('download_attempted') or (asset.get('downloadable') and (
                    (asset.get('kind') == 'image' and config.download_images) or
                    (metadata.get('include_media') and asset.get('kind') not in {'image', 'link'})))
                if asset.get('active', 1) and required:
                    current_issues.append({'key': record['key'], 'id': asset['id'], 'reason': 'resource_not_downloaded'})
                continue
            result = validate_downloaded_asset(config, asset)
            if asset.get('active', 1) and not result['ok']:
                current_issues.append({'key': record['key'], 'id': asset['id'], 'reason': result['reason']})
            # Stored bytes are audited even when the new identity model is unverified.
            candidate = {k: v for k, v in asset.items() if k != 'download_identity'}
            result = validate_downloaded_asset(config, candidate)
            if result['ok']:
                checked += 1
            else:
                problem(metadata_path, result['reason'])
        for version in state.asset_history(record['key']):
            # Failed/damaged records retain diagnostic evidence only. Replaced good
            # downloads are retained archive versions and remain under the promise.
            if version['reason'] not in {'download_replaced', 'resolved_url_changed'}:
                continue
            asset = version['record']
            if asset.get('download_status') != 'ok' or not asset.get('local_path'):
                continue
            result = validate_downloaded_asset(config, {k: v for k, v in asset.items() if k != 'download_identity'})
            if result['ok']:
                checked += 1
            else:
                historical_issues.append({'key': record['key'], 'version': version['id'], 'reason': result['reason']})

    return {"scope": "stored_files_only", "ok": not issues and not historical_issues and unverified == 0,
            "checked_files": checked, "unverified_files": unverified,
            "issue_count": len(issues) + len(historical_issues), "issues": issues,
            "current_resource_integrity": {"ok": not current_issues, "issues": current_issues},
            "historical_retained_resource_integrity": {"ok": not historical_issues, "issues": historical_issues}}
