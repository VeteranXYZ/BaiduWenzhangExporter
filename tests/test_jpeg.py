"""JPEG trailers must not weaken stream completeness or archive validation."""
from __future__ import annotations

import io

import pytest
from PIL import Image, ImageFile

from baidu_archive import cli, server
from baidu_archive.media import validate_media_response
from baidu_archive.truth import article_truth
from test_rc_regressions import archive, download


def jpeg_bytes(*, progressive=False):
    stream = io.BytesIO()
    Image.new('RGB', (8, 8), 'blue').save(stream, format='JPEG', progressive=progressive)
    return stream.getvalue()


JPEG = jpeg_bytes()
SOF = JPEG.index(b'\xff\xc0')
CASES = [
    ('exact-eoi', JPEG, True),
    ('crlf', JPEG + b'\r\n', True),
    ('nul', JPEG + b'\x00' * 4, True),
    ('combined-padding', JPEG + b'\r\n\x00\r\n\x00', True),
    ('other-trailer', JPEG + b'harmless trailing data', True),
    ('progressive-padding', jpeg_bytes(progressive=True) + b'\r\n\x00', True),
    ('truncated-before-eoi', JPEG[:-20], False),
    ('missing-eoi', JPEG[:-2], False),
    ('header-only', b'\xff\xd8\xff\xe0', False),
    ('corrupt-segment', JPEG[:4] + b'\xff\xff' + JPEG[6:], False),
    ('corrupt-frame', JPEG[:SOF + 5] + b'\x00\x00' + JPEG[SOF + 7:], False),
    ('random-containing-eoi', b'random bytes \xff\xd9 garbage', False),
    # APP metadata containing EOI cannot certify a missing actual stream terminator.
    ('metadata-eoi-only', JPEG[:2] + b'\xff\xe1\x00\x04\xff\xd9' + JPEG[2:-2], False),
]


@pytest.mark.parametrize('name,body,accepted', CASES, ids=[case[0] for case in CASES])
def test_jpeg_validation_and_archive_completion(archive, name, body, accepted):
    cfg, state = archive
    assert not ImageFile.LOAD_TRUNCATED_IMAGES
    if accepted:
        with Image.open(io.BytesIO(body)) as image:
            image.verify()
        with Image.open(io.BytesIO(body)) as image:
            image.load()
            assert image.size == (8, 8)
    validation = validate_media_response('image', body, 'image/jpeg')
    assert (validation['status'] == 'accepted') == accepted
    assert validation['structurally_valid'] == accepted
    stats, requests = download(cfg, state, body, 'image/jpeg')
    assert len(requests) == stats['attempted'] == 1
    assert stats['downloaded'] == int(accepted)
    assert stats['failed'] == int(not accepted)
    assert stats['incomplete_items'] == int(not accepted)
    assert state.get_item('one')['status'] == ('complete' if accepted else 'assets_partial')
    assert article_truth(cfg, state, 'one')['complete'] == accepted
    assert cli.main(['--root', str(cfg.root), 'verify']) == (0 if accepted else 1)
    app = server.Application(cfg)
    app.start({'kind': 'verify'})
    app.thread.join(5)
    assert not app.thread.is_alive()
    assert app.snapshot()['status'] == ('completed' if accepted else 'partial')
    assert app.snapshot()['result']['ok'] == accepted
    assert not ImageFile.LOAD_TRUNCATED_IMAGES


def test_jpeg_rejects_when_truncated_recovery_is_enabled(archive, monkeypatch):
    cfg, state = archive
    broken = JPEG[:-2]
    with pytest.raises(OSError):
        with Image.open(io.BytesIO(broken)) as image:
            image.load()
    monkeypatch.setattr(ImageFile, 'LOAD_TRUNCATED_IMAGES', True)
    with Image.open(io.BytesIO(broken)) as image:
        image.load()  # This input is recoverable only under the forbidden policy.
    assert validate_media_response('image', broken, 'image/jpeg')['status'] == 'rejected'
    stats, _ = download(cfg, state, broken, 'image/jpeg')
    assert stats['failed'] == 1 and stats['downloaded'] == 0
    assert not article_truth(cfg, state, 'one')['complete']
    assert ImageFile.LOAD_TRUNCATED_IMAGES  # Validator never changes the global flag.
