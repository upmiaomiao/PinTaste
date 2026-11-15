"""
AIService singleton manager for optimizing provider initialization

This module provides a singleton pattern implementation for AIService to avoid
repeated initialization of AI providers (TextProvider and ImageProvider) on every request.

Benefits:
- Reuses AI provider instances across requests
- Reduces initialization overhead
- Better resource management
- Thread-safe for Flask multi-threaded environment

Usage:
    from services.ai_service_manager import get_ai_service
    
    # In your controller
    ai_service = get_ai_service()
    outline = ai_service.generate_outline(project_context)
"""

import logging
from threading import Lock
from typing import Optional
from flask import current_app, has_app_context
from .ai_service import AIService
from .ai_providers import get_text_provider, get_image_provider, get_image_refine_provider, get_caption_provider, TextProvider, ImageProvider

logger = logging.getLogger(__name__)

# Global singleton instance
_ai_service_instance: Optional[AIService] = None
_lock = Lock()

# Provider cache to avoid re-initialization when models don't change
_text_provider_cache: dict = {}
_image_provider_cache: dict = {}
_image_refine_provider_cache: dict = {}
_caption_provider_cache: dict = {}
_cache_lock = Lock()


def _get_cached_text_provider(model: str) -> TextProvider:
    """
    Get or create a cached text provider instance
    
    Args:
        model: Model name to use
        
    Returns:
        Cached or new TextProvider instance
    """
    with _cache_lock:
        if model not in _text_provider_cache:
            logger.info(f"Creating new TextProvider for model: {model}")
            _text_provider_cache[model] = get_text_provider(model=model)
        else:
            logger.debug(f"Reusing cached TextProvider for model: {model}")
        return _text_provider_cache[model]


def _get_cached_image_provider(model: str) -> ImageProvider:
    """
    Get or create a cached image provider instance
    
    Args:
        model: Model name to use
        
    Returns:
        Cached or new ImageProvider instance
    """
    with _cache_lock:
        if model not in _image_provider_cache:
            logger.info(f"Creating new ImageProvider for model: {model}")
            _image_provider_cache[model] = get_image_provider(model=model)
        else:
            logger.debug(f"Reusing cached ImageProvider for model: {model}")
        return _image_provider_cache[model]


def _get_cached_caption_provider(model: str) -> TextProvider:
    """Get or create a cached caption provider instance"""
    with _cache_lock:
        if model not in _caption_provider_cache:
            logger.info(f"Creating new CaptionProvider for model: {model}")
            _caption_provider_cache[model] = get_caption_provider(model=model)
        return _caption_provider_cache[model]


def _get_cached_image_refine_provider(model: str) -> Optional[ImageProvider]:
    """Get or create a cached image-refine provider; returns None on init failure
    so callers can transparently fall back to the single-step image provider."""
    with _cache_lock:
        if model not in _image_refine_provider_cache:
            try:
                logger.info(f"Creating new ImageRefineProvider for model: {model}")
                _image_refine_provider_cache[model] = get_image_refine_provider(model=model)
            except Exception as e:
                logger.warning(f"Failed to init ImageRefineProvider for model {model}: {e}")
                _image_refine_provider_cache[model] = None
        return _image_refine_provider_cache[model]


def get_ai_service(force_new: bool = False) -> AIService:
    """
    Get the singleton AIService instance with optimized provider caching
    
    This function creates and returns a singleton AIService instance that reuses
    AI providers (TextProvider and ImageProvider) across requests, significantly
    reducing initialization overhead.
    
    Args:
        force_new: If True, forces creation of a new instance (useful for testing)
        
    Returns:
        AIService singleton instance with cached providers
        
    Note:
        The providers are cached per model name. If TEXT_MODEL or IMAGE_MODEL
        changes in Flask config, new providers will be created automatically.
    """
    global _ai_service_instance
    
    if force_new:
        with _lock:
            logger.info("Force creating new AIService instance")
            _ai_service_instance = None
    
    if _ai_service_instance is None:
        with _lock:
            # Double-check locking pattern
            if _ai_service_instance is None:
                logger.info("Initializing AIService singleton with provider caching")
                
                # Get model names from Flask config or use defaults
                from config import get_config
                config = get_config()
                
                if has_app_context() and current_app and hasattr(current_app, "config"):
                    text_model = current_app.config.get("TEXT_MODEL", config.TEXT_MODEL)
                    image_model = current_app.config.get("IMAGE_MODEL", config.IMAGE_MODEL)
                    caption_model = current_app.config.get("IMAGE_CAPTION_MODEL", config.IMAGE_CAPTION_MODEL)
                    image_mode = (current_app.config.get("IMAGE_MODE") or config.IMAGE_MODE or 'single').lower()
                    image_refine_model = current_app.config.get("IMAGE_REFINE_MODEL", config.IMAGE_REFINE_MODEL)
                else:
                    text_model = config.TEXT_MODEL
                    image_model = config.IMAGE_MODEL
                    caption_model = config.IMAGE_CAPTION_MODEL
                    image_mode = (config.IMAGE_MODE or 'single').lower()
                    image_refine_model = config.IMAGE_REFINE_MODEL

                # Get cached providers
                text_provider = _get_cached_text_provider(text_model)
                image_provider = _get_cached_image_provider(image_model)
                caption_provider = _get_cached_caption_provider(caption_model)
                image_refine_provider = (
                    _get_cached_image_refine_provider(image_refine_model)
                    if image_mode == 'refine' else None
                )

                # Create AIService with cached providers
                _ai_service_instance = AIService(
                    text_provider=text_provider,
                    image_provider=image_provider,
                    caption_provider=caption_provider,
                    image_refine_provider=image_refine_provider,
                )

                logger.info(
                    f"AIService singleton created with models: text={text_model}, image={image_model}, "
                    f"caption={caption_model}, image_mode={image_mode}"
                    + (f", refine={image_refine_model}" if image_mode == 'refine' else "")
                )
    
    return _ai_service_instance


# 图片生成预设模式（用户可选的三档）：
#   stable   = 稳定模式：仅 gpt-image-2
#   creative = 创意模式：仅 gemini-3-pro-image
#   best     = 最佳模式：gemini-3-pro-image 出图 + gpt-image-2 精修（refine）
IMAGE_PRESET_MODES: dict = {
    'stable':   {'mode': 'single', 'model': 'gpt-image-2'},
    'creative': {'mode': 'single', 'model': 'gemini-3-pro-image'},
    'best':     {'mode': 'refine', 'model': 'gemini-3-pro-image', 'refine_model': 'gpt-image-2'},
}


def get_ai_service_for_mode(mode: Optional[str]) -> AIService:
    """Return an AIService bound to a per-project image preset mode.

    Falls back to the global singleton when `mode` is None/unknown, so existing
    projects (with NULL image_mode) keep the current global behaviour.
    """
    preset = IMAGE_PRESET_MODES.get(mode) if mode else None
    if not preset:
        return get_ai_service()

    image_model = preset['model']
    image_mode = preset['mode']
    image_refine_model = preset.get('refine_model')

    image_provider = _get_cached_image_provider(image_model)
    image_refine_provider = (
        _get_cached_image_refine_provider(image_refine_model)
        if image_mode == 'refine' and image_refine_model else None
    )

    # AIService 从全局 config 读 model/mode，这里创建后按预设覆盖到实例属性，
    # 保证 generate_image 用对 provider 且按 single/refine 分支执行。
    svc = AIService(
        image_provider=image_provider,
        image_refine_provider=image_refine_provider,
    )
    svc.image_mode = image_mode
    svc.image_model = image_model
    svc.image_refine_model = image_refine_model

    logger.info(
        "AIService created for image preset mode=%s: image=%s, image_mode=%s%s",
        mode, image_model, image_mode,
        (f", refine={image_refine_model}" if image_refine_model else ""),
    )
    return svc


def clear_ai_service_cache():
    """
    Clear the AIService singleton and provider cache
    
    This is useful when:
    - Configuration changes (API keys, endpoints, models)
    - Testing scenarios requiring fresh instances
    - Memory cleanup needed
    
    Note:
    - Uses nested locks to ensure atomic cache clearing operation
    - Prevents race conditions where new instances could be created
      with stale cached providers during the clearing process
    """
    global _ai_service_instance
    
    with _lock:
        _ai_service_instance = None
        logger.info("AIService singleton cache cleared")
        with _cache_lock:
            _text_provider_cache.clear()
            _image_provider_cache.clear()
            _image_refine_provider_cache.clear()
            _caption_provider_cache.clear()
            logger.info("Provider cache cleared")


def get_provider_cache_info() -> dict:
    """
    Get information about cached providers (for debugging/monitoring)
    
    Returns:
        Dictionary with cache statistics
    """
    with _cache_lock:
        return {
            "text_providers": list(_text_provider_cache.keys()),
            "image_providers": list(_image_provider_cache.keys()),
            "caption_providers": list(_caption_provider_cache.keys()),
            "total_cached": len(_text_provider_cache) + len(_image_provider_cache) + len(_caption_provider_cache)
        }
