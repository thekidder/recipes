"""Bounded multipart parsing with exact part identity and MIME validation."""
from io import BytesIO
import json
from PIL import Image, UnidentifiedImageError
from python_multipart.multipart import MultipartParser, parse_options_header
from .contract import LIMITS, validate_named
from .errors import ApiError


def load_json(data):
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError('Duplicate key')
            result[key]=value
        return result
    try:
        value=json.loads(data,object_pairs_hook=unique,parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        # Reject lone surrogate codepoints, which cannot be preserved as UTF-8.
        json.dumps(value,ensure_ascii=False).encode('utf-8')
        return value
    except (ValueError,UnicodeError,RecursionError):
        raise ApiError('invalid_request','Body must be valid UTF-8 JSON without duplicate keys.',400)


def multipart(content_type,body):
    _,options=parse_options_header(content_type)
    boundary=options.get(b'boundary')
    if not boundary or len(boundary)>200:
        raise ApiError('invalid_request','Missing or invalid multipart boundary.',400)
    parts={};part={};ended=False
    def start():
        part.clear();part.update(headers={},field=bytearray(),value=bytearray(),data=bytearray(),header_bytes=0)
        if len(parts)>=7:raise ApiError('payload_too_large','Too many multipart parts.',413)
    def header_field(data,start,end):
        part['field'].extend(data[start:end]);part['header_bytes']+=end-start
        if part['header_bytes']>8192:raise ApiError('payload_too_large','Multipart headers are too large.',413)
    def header_value(data,start,end):
        part['value'].extend(data[start:end]);part['header_bytes']+=end-start
        if part['header_bytes']>8192:raise ApiError('payload_too_large','Multipart headers are too large.',413)
    def header_end():
        field=bytes(part['field']).lower()
        if field in part['headers']:raise ApiError('invalid_request','Duplicate multipart header.',400)
        part['headers'][field]=bytes(part['value']);part['field'].clear();part['value'].clear()
    def headers_finished():
        disposition,options=parse_options_header(part['headers'].get(b'content-disposition',b''))
        try:name=options[b'name'].decode('ascii')
        except (KeyError,UnicodeError):raise ApiError('invalid_request','Every part needs a valid name.',400)
        if disposition!=b'form-data' or name not in {'metadata',*[f'image_{i}' for i in range(1,7)]} or name in parts:
            raise ApiError('invalid_request','Unexpected or duplicate multipart part.',400)
        mime=part['headers'].get(b'content-type',b'').decode('ascii','replace').lower()
        if name=='metadata' and mime!='application/json':raise ApiError('unsupported_media_type','Metadata must be application/json.',415)
        if name!='metadata' and mime not in LIMITS['image_media_types']:raise ApiError('unsupported_media_type','Only still JPEG and PNG images are supported.',415)
        part.update(name=name,mime=mime,limit=16384 if name=='metadata' else LIMITS['image_file_bytes'])
    def data(data,start,end):
        if len(part['data'])+end-start>part['limit']:raise ApiError('payload_too_large','Multipart part exceeds its limit.',413)
        part['data'].extend(data[start:end])
    def finish():parts[part['name']]={'data':bytes(part['data']),'mime':part['mime']}
    def end():
        nonlocal ended
        ended=True
    callbacks=dict(on_part_begin=start,on_header_field=header_field,on_header_value=header_value,on_header_end=header_end,
                   on_headers_finished=headers_finished,on_part_data=data,on_part_end=finish,on_end=end)
    try:
        parser=MultipartParser(boundary,callbacks);parser.write(body);parser.finalize()
    except ApiError:raise
    except Exception:raise ApiError('invalid_request','Malformed multipart body.',400)
    if not ended or 'metadata' not in parts:raise ApiError('invalid_request','Incomplete multipart upload.',400)
    metadata=load_json(parts.pop('metadata')['data'])
    if isinstance(metadata,dict) and isinstance(metadata.get('attachments'),list) and len(metadata['attachments'])>LIMITS['image_count']:
        raise ApiError('payload_too_large','Too many image attachments.',413)
    metadata=validate_named('ImageMetadata',metadata)
    names=[x['part_name'] for x in metadata['attachments']]
    if len(names)!=len(set(names)) or set(names)!=set(parts):raise ApiError('invalid_request','Manifest must reference each file exactly once.',400)
    if sum(len(x['data']) for x in parts.values())>LIMITS['image_total_bytes']:raise ApiError('payload_too_large','Images exceed the total upload limit.',413)
    files=[]
    for item in metadata['attachments']:
        part=parts[item['part_name']]
        try:
            with Image.open(BytesIO(part['data'])) as im:
                if im.width*im.height>LIMITS['image_pixels']:raise ApiError('payload_too_large','An image exceeds the pixel limit.',413)
                if {'JPEG':'image/jpeg','PNG':'image/png'}.get(im.format)!=part['mime'] or getattr(im,'n_frames',1)!=1:
                    raise ApiError('unsupported_media_type','File content must match a still JPEG or PNG type.',415)
                im.verify()
            # JPEG verify() only checks the container header. Decode as well so
            # damaged uploads fail here instead of creating an unusable recipe.
            with Image.open(BytesIO(part['data'])) as im:
                im.load()
        except ApiError:raise
        except Image.DecompressionBombError:raise ApiError('payload_too_large','An image exceeds the pixel limit.',413)
        except (UnidentifiedImageError,OSError,ValueError,SyntaxError,EOFError):raise ApiError('unsupported_media_type','Image could not be decoded.',415)
        files.append((item['filename'],part['data'],part['mime']))
    return metadata,files
