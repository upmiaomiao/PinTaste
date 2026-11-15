"""Transport previews of verified originals. Original media is never overwritten."""
import hashlib
import io
from pathlib import Path
from PIL import Image, ImageOps
from .wuyu_errors import PipelineError, check_cancel
from .wuyu_materials import allowed_file


def verify_reader_image_integrity(task, config, cancel_check=None):
    """Cached image ACKs remain valid only for the same accessible originals."""
    roots = config.get('WUYU_ALLOWED_MATERIAL_ROOTS') or [config.get('UPLOAD_FOLDER')]
    roots = [roots] if isinstance(roots, str) else roots
    for asset in task.get('visual_inputs', []):
        check_cancel(cancel_check)
        try:
            path = allowed_file(asset['path'], roots)
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                while block := stream.read(1024 * 1024):
                    check_cancel(cancel_check);digest.update(block)
            if digest.hexdigest() != asset['sha256']:
                raise PipelineError('source_asset_hash_mismatch','已缓存阅读的原图已变化，不能复用旧图片确认。')
        except (PipelineError, OSError) as error:
            if isinstance(error, PipelineError) and error.code != 'material_not_found':failure = error
            else:failure = PipelineError('source_image_unreadable','已缓存阅读的原图缺失或不可读取。可替换或明确排除此图。')
            failure.source_asset_id = asset['id'];failure.material_id = asset.get('material_id')
            raise failure from None


def reader_images(task, config, cancel_check=None):
    images = []
    # Leave body room for base64 expansion, full original text and provider
    # protocol overhead. This transport limit is independent of context size.
    remaining = 34 * 1024 * 1024 - len(__import__('json').dumps(task.get('brief',{})).encode())
    roots = config.get('WUYU_ALLOWED_MATERIAL_ROOTS') or [config.get('UPLOAD_FOLDER')]
    roots = [roots] if isinstance(roots,str)else roots
    for asset in task.get('visual_inputs',[]):
        check_cancel(cancel_check)
        try:
            path = allowed_file(asset['path'], roots)
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != asset['sha256']:
                raise PipelineError('source_asset_hash_mismatch','阅读原图已变化；未使用其他图片替代。')
            with Image.open(io.BytesIO(raw)) as source:
                image = ImageOps.exif_transpose(source).convert('RGB')
                image.thumbnail((3072,3072),Image.Resampling.LANCZOS)
                if min(image.size)<32:
                    scale=32/min(image.size)
                    image=image.resize((round(image.width*scale),round(image.height*scale)),Image.Resampling.LANCZOS)
                output = io.BytesIO();image.save(output,'JPEG',quality=94,subsampling=0)
        except Exception as error:
            if isinstance(error, PipelineError) and error.code != 'material_not_found':failure = error
            elif isinstance(error, PipelineError):
                failure = PipelineError('source_image_unreadable','这一张原图文件缺失。可替换此图，或明确排除此图后继续读取其余文字和图片。')
            elif isinstance(error, (OSError, ValueError)):
                failure = PipelineError('source_image_unreadable','这一张原图无法解码。可替换此图，或明确排除此图后继续读取其余文字和图片。')
            else:raise
            failure.source_asset_id = asset['id']
            failure.material_id = asset.get('material_id')
            raise failure from None
        preview = output.getvalue()
        remaining -= (len(preview)+2)//3*4
        if remaining < 0 or len(images)>=600:
            raise PipelineError('reading_visual_transport_limit','原图参考超过单次图文请求容量；原件保留，尚未发起模型调用。')
        images.append(preview)
    return images
