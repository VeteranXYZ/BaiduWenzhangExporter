"""Conservative response classification, including imperfect CDN MIME headers."""
from __future__ import annotations

import re


def image_extension(body: bytes) -> str | None:
    if body.startswith(b'\xff\xd8\xff'): return '.jpg'
    if body.startswith(b'\x89PNG\r\n\x1a\n'): return '.png'
    if body.startswith((b'GIF87a', b'GIF89a')): return '.gif'
    if body.startswith(b'RIFF') and body[8:12] == b'WEBP': return '.webp'
    if body.startswith(b'BM'): return '.bmp'
    if body.startswith((b'II*\x00', b'MM\x00*')): return '.tiff'
    if body.startswith(b'\x00\x00\x01\x00'): return '.ico'
    if body[4:8] == b'ftyp' and any(x in body[8:40] for x in (b'avif', b'avis')): return '.avif'
    prefix = body[:8192].decode('utf-8-sig', 'replace').lstrip()
    prefix = re.sub(r'^(?:<\?xml.*?\?>\s*|<!--.*?-->\s*)+', '', prefix, flags=re.S)
    if re.match(r'<svg(?:\s|>)', prefix, re.I): return '.svg'
    return None


def _jpeg_stream_complete(body: bytes) -> bool:
    """Find EOI on a JPEG marker boundary, excluding metadata and escaped scan bytes."""
    if not body.startswith(b'\xff\xd8'):
        return False
    position, in_scan, saw_scan = 2, False, False
    while position < len(body):
        if in_scan:
            position = body.find(b'\xff', position)
            if position < 0:
                return False
        if body[position] != 0xff:
            return False
        while position < len(body) and body[position] == 0xff:
            position += 1
        if position == len(body):
            return False
        marker = body[position]
        position += 1
        if in_scan and (marker == 0 or 0xd0 <= marker <= 0xd7):
            continue  # Escaped entropy byte or restart marker.
        if marker == 0xd9:
            return saw_scan  # Bytes after the complete stream belong to its trailer.
        if marker == 0 or marker == 0xd8 or 0xd0 <= marker <= 0xd7:
            return False
        if marker == 0x01:  # Standalone TEM marker.
            continue
        if position + 2 > len(body):
            return False
        length = int.from_bytes(body[position:position + 2], 'big')
        if length < 2 or position + length > len(body):
            return False
        position += length
        in_scan = marker == 0xda or (in_scan and marker == 0xdc)
        saw_scan |= marker == 0xda
    return False


def _image_structure(body: bytes, extension: str) -> str:
    import io
    import struct
    import zlib
    from xml.etree import ElementTree
    if extension == '.svg':
        try:
            root = ElementTree.fromstring(body)
            return 'accepted' if root.tag.split('}')[-1] == 'svg' else 'rejected'
        except ElementTree.ParseError:
            return 'rejected'
    if extension == '.png':
        position, kinds = 8, []
        while position + 12 <= len(body):
            length = int.from_bytes(body[position:position+4], 'big')
            end = position + 12 + length
            if end > len(body): return 'rejected'
            kind = body[position+4:position+8]
            payload = body[position+8:position+8+length]
            if zlib.crc32(kind + payload) & 0xffffffff != int.from_bytes(body[end-4:end], 'big'):
                return 'rejected'
            kinds.append(kind)
            if len(kinds) == 1:
                if kind != b'IHDR' or length != 13: return 'rejected'
                width, height = struct.unpack('>II', payload[:8])
                if not width or not height: return 'rejected'
            position = end
            if kind == b'IEND':
                if length or b'IDAT' not in kinds: return 'rejected'
                break
        if not kinds or kinds[-1] != b'IEND': return 'rejected'
    # Pillow verifies container structure and decodes every frame. Identification
    # alone is insufficient. Decoder limits remain enabled, including truncation.
    if extension == '.jpg':
        from PIL import ImageFile
        # Never certify a JPEG using Pillow's optional truncated-image recovery.
        if ImageFile.LOAD_TRUNCATED_IMAGES or not _jpeg_stream_complete(body):
            return 'rejected'
    if extension == '.gif' and not body.endswith(b';'):
        return 'rejected'
    if extension == '.webp' and (len(body) < 12 or int.from_bytes(body[4:8], 'little') + 8 != len(body)):
        return 'rejected'
    from PIL import Image, UnidentifiedImageError
    try:
        with Image.open(io.BytesIO(body)) as image:
            image.verify()
        with Image.open(io.BytesIO(body)) as image:
            for frame in range(getattr(image, 'n_frames', 1)):
                image.seek(frame)
                image.load()
        return 'accepted'
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        return 'rejected'


class _BrokenMedia(ValueError):
    pass


def _boxes(body: bytes, start: int, end: int) -> list:
    boxes = []
    while start < end:
        if end - start < 8: raise _BrokenMedia('truncated_box')
        size = int.from_bytes(body[start:start+4], 'big')
        header = 8
        if size == 1:
            if end - start < 16: raise _BrokenMedia('truncated_large_box')
            size = int.from_bytes(body[start+8:start+16], 'big'); header = 16
        elif size == 0:
            size = end - start
        if size < header or start + size > end: raise _BrokenMedia('invalid_box_size')
        boxes.append((body[start+4:start+8], start+header, start+size))
        start += size
    return boxes


def _mp4_structure(body: bytes) -> tuple[str, str | None]:
    def child(parent, name):
        matches = [box for box in _boxes(body, parent[1], parent[2]) if box[0] == name]
        if len(matches) != 1: raise _BrokenMedia('missing_or_duplicate_' + name.decode())
        return matches[0]
    def payload(box): return body[box[1]:box[2]]
    def table(box, width):
        data = payload(box)
        if len(data) < 8 or data[:4] != b'\x00'*4: raise _BrokenMedia('bad_table_header')
        count = int.from_bytes(data[4:8], 'big')
        if count == 0 or len(data) != 8 + count * width * 4: raise _BrokenMedia('bad_table_length')
        return [tuple(int.from_bytes(data[p+i*4:p+i*4+4], 'big') for i in range(width))
                for p in range(8, len(data), width*4)]
    try:
        top = _boxes(body, 0, len(body))
        moovs = [b for b in top if b[0] == b'moov']
        ftyp = [b for b in top if b[0] == b'ftyp']
        mdats = [(b[1], b[2]) for b in top if b[0] == b'mdat' and b[2] > b[1]]
        if len(moovs) != 1 or len(ftyp) != 1 or ftyp[0][2]-ftyp[0][1] < 8 or not mdats:
            raise _BrokenMedia('missing_container_or_media_data')
        tracks = [b for b in _boxes(body, moovs[0][1], moovs[0][2]) if b[0] == b'trak']
        if not tracks: raise _BrokenMedia('no_tracks')
        if any(b[0] == b'moof' for b in top): return 'unverifiable', None
        kinds = set()
        for track in tracks:
            mdia = child(track, b'mdia')
            handler = payload(child(mdia, b'hdlr'))
            if len(handler) < 12: raise _BrokenMedia('truncated_handler')
            if handler[8:12] not in {b'vide', b'soun'}: continue
            kind = 'video' if handler[8:12] == b'vide' else 'audio'
            stbl = child(child(mdia, b'minf'), b'stbl')
            sd = payload(child(stbl, b'stsd'))
            if len(sd) < 8 or sd[:4] != b'\x00'*4 or int.from_bytes(sd[4:8], 'big') != 1:
                return 'unverifiable', kind
            entry = _boxes(body, child(stbl, b'stsd')[1]+8, child(stbl, b'stsd')[2])
            if len(entry) != 1: raise _BrokenMedia('invalid_sample_description')
            # Internal fallback supports classic H.264/AVC sample tables. Other
            # codecs/fragmentation need an actual parser, never a magic-byte PASS.
            if entry[0][0] != b'avc1' or kind != 'video': return 'unverifiable', kind
            if entry[0][2]-entry[0][1] < 78: raise _BrokenMedia('short_visual_entry')
            visual = payload(entry[0])
            if not int.from_bytes(visual[24:26], 'big') or not int.from_bytes(visual[26:28], 'big'):
                raise _BrokenMedia('invalid_frame_dimensions')
            avccs = [b for b in _boxes(body, entry[0][1]+78, entry[0][2]) if b[0] == b'avcC']
            if len(avccs) != 1: raise _BrokenMedia('missing_avc_configuration')
            avcc = payload(avccs[0])
            if len(avcc) < 7 or avcc[0] != 1: raise _BrokenMedia('bad_avc_configuration')
            length_size = (avcc[4] & 3) + 1
            pos = 6
            for count, nal_type in [(avcc[5] & 31, 7), (None, 8)]:
                if count is None:
                    if pos >= len(avcc): raise _BrokenMedia('missing_pps')
                    count = avcc[pos]; pos += 1
                if not count: raise _BrokenMedia('missing_parameter_set')
                for _ in range(count):
                    size = int.from_bytes(avcc[pos:pos+2], 'big'); pos += 2
                    if size < (4 if nal_type == 7 else 2) or pos+size > len(avcc) or avcc[pos] & 31 != nal_type:
                        raise _BrokenMedia('invalid_parameter_set')
                    pos += size
            sizes = payload(child(stbl, b'stsz'))
            if len(sizes) < 12 or sizes[:4] != b'\x00'*4: raise _BrokenMedia('bad_sample_sizes')
            fixed, count = int.from_bytes(sizes[4:8], 'big'), int.from_bytes(sizes[8:12], 'big')
            if not count or count > len(body): raise _BrokenMedia('no_samples')
            if len(sizes) != (12 if fixed else 12+4*count): raise _BrokenMedia('bad_sample_sizes')
            sample_sizes = [fixed]*count if fixed else [int.from_bytes(sizes[i:i+4], 'big') for i in range(12,len(sizes),4)]
            if any(not n for n in sample_sizes): raise _BrokenMedia('empty_sample')
            timing = table(child(stbl,b'stts'),2)
            if any(not n or not d for n,d in timing) or sum(n for n,d in timing) != count:
                raise _BrokenMedia('bad_sample_timing')
            chunks = table(child(stbl,b'stco'),1)
            sc = table(child(stbl,b'stsc'),3)
            if sc[0][0] != 1 or any(not n or d != 1 for _,n,d in sc) or any(sc[i][0] >= sc[i+1][0] for i in range(len(sc)-1)) or sc[-1][0] > len(chunks):
                raise _BrokenMedia('bad_chunk_map')
            sample_index = 0
            for number,(offset,) in enumerate(chunks,1):
                per_chunk = next(n for first,n,d in reversed(sc) if first <= number)
                for _ in range(per_chunk):
                    if sample_index >= count: raise _BrokenMedia('excess_samples')
                    size = sample_sizes[sample_index]; sample_index += 1
                    if not any(start <= offset and offset+size <= end for start,end in mdats):
                        raise _BrokenMedia('sample_outside_media_data')
                    stop = offset+size; has_frame = False
                    while offset < stop:
                        if offset+length_size > stop: raise _BrokenMedia('truncated_nal_size')
                        n = int.from_bytes(body[offset:offset+length_size],'big'); offset += length_size
                        if n < 2 or offset+n > stop or body[offset] & 128: raise _BrokenMedia('invalid_nal')
                        has_frame |= body[offset] & 31 in {1,5}
                        offset += n
                    if not has_frame: raise _BrokenMedia('sample_without_frame')
            if sample_index != count: raise _BrokenMedia('missing_samples')
            kinds.add(kind)
        if not kinds: raise _BrokenMedia('no_media_tracks')
        return 'accepted', 'video' if 'video' in kinds else 'audio'
    except (_BrokenMedia, IndexError, StopIteration):
        return 'rejected', None


# 图床在原图被删除后，会返回一张结构完全合法的「图片不存在」占位图，HTTP 200。
# 只看结构的话它会被当成下载成功 —— 实测某个真实归档里 219 张「已核实」图片中
# 有 89 张其实是这一张占位图（200×200，4383 字节，字节完全相同），涉及 12 篇文章。
# 按内容指纹识别：占位图本身仍原样保存在磁盘上，作为「原图已被删除」的证据。
KNOWN_PLACEHOLDERS = {
    "9aeb6ef45f38674f8552b598219929db3d310409229ef6048ab69a9db51786d7":
        "百度图床「你查看的图片不存在」占位图",
}


def placeholder_reason(body: bytes) -> str | None:
    import hashlib
    return KNOWN_PLACEHOLDERS.get(hashlib.sha256(body).hexdigest())


def validate_media_response(kind, body: bytes, content_type: str | None) -> dict:
    """Only proven structure contributes to complete. Bytes alone never do."""
    import io
    import wave
    mime = (content_type or '').split(';')[0].strip().lower()
    prefix = body[:8192].decode('utf-8-sig', 'replace').lstrip().lower()
    def result(status, extension=None, reason=None):
        return {'status': status, 'extension': extension, 'reason': reason,
                'downloaded': bool(body), 'structurally_valid': status == 'accepted'}
    if (not body or mime in {'text/html', 'application/xhtml+xml', 'application/json'} or
            re.match(r'(?:<!doctype\s+html|<html\b|<head\b|<body\b)', prefix) or
            ('<form' in prefix and any(x in prefix for x in ('captcha','passport.baidu','登录')))):
        return result('rejected', reason='empty_or_error_response')
    if kind == 'image':
        extension = image_extension(body)
        allowed = not mime or mime.startswith('image/') or mime in {'application/octet-stream','binary/octet-stream','text/plain','application/xml','text/xml'}
        if not extension or not allowed: return result('rejected', reason='not_an_image')
        status = _image_structure(body, extension)
        if status == 'accepted' and placeholder_reason(body):
            # 结构合法但内容是「图片不存在」：原图已被删除，不能算作保存成功
            return result('placeholder', reason='source_deleted_placeholder')
        return result(status, extension if status == 'accepted' else None, None if status == 'accepted' else 'invalid_image_structure')
    if kind not in {'audio','video','media-source'}:
        return result('unverifiable', reason='unsupported_media_kind')
    allowed = not mime or mime.startswith(('audio/','video/')) or mime in {'application/octet-stream','binary/octet-stream','application/ogg','text/plain'}
    if not allowed: return result('rejected', reason='not_a_media_mime')
    if body.startswith(b'RIFF') and body[8:12] == b'WAVE':
        try:
            if len(body) < 44 or int.from_bytes(body[4:8],'little')+8 != len(body): raise ValueError()
            with wave.open(io.BytesIO(body), 'rb') as wav:
                frames = wav.readframes(wav.getnframes())
                if not frames or len(frames) != wav.getnframes()*wav.getnchannels()*wav.getsampwidth(): raise ValueError()
            status, actual, extension = 'accepted', 'audio', '.wav'
        except (wave.Error, EOFError, ValueError): return result('rejected', reason='invalid_wav_structure')
    elif body[4:8] == b'ftyp':
        status, actual = _mp4_structure(body)
        extension = '.mp4' if actual == 'video' else '.m4a'
    elif body in {b'ID3',b'fLaC',b'OggS',b'FLV'}:
        return result('rejected', reason='header_only_media')
    else:
        return result('unverifiable', reason='unsupported_or_unknown_media_structure')
    if status != 'accepted': return result(status, reason='invalid_or_unproven_media_structure')
    if kind != 'media-source' and kind != actual: return result('rejected', reason='wrong_media_kind')
    return result('accepted', extension)
