"""
Backend configuration file
"""
import os
import sys
from datetime import timedelta
from database_runtime import runtime_engine_options, normalize_runtime_url

# 基础配置 - 使用更可靠的路径计算方式
# 在模块加载时立即计算并固定路径
_current_file = os.path.realpath(__file__)  # 使用realpath解析所有符号链接
BASE_DIR = os.path.dirname(_current_file)
PROJECT_ROOT = os.path.dirname(BASE_DIR)

# Flask配置
class Config:
    WUYU_QUOTA_ENABLED = os.getenv('WUYU_QUOTA_ENABLED', 'true').strip().lower() not in {'false', '0', 'off', 'no'}
    WUYU_QUOTA_INTERNAL_KEY = os.getenv('WUYU_QUOTA_INTERNAL_KEY', '')
    WUYU_TENANT_ACCESS_KEY = os.getenv('WUYU_TENANT_ACCESS_KEY', '')
    WUYU_PUBLIC_ORIGIN = os.getenv('WUYU_PUBLIC_ORIGIN', '')
    """Base configuration"""
    # Optional planner overrides; production sets its verified endpoint capacity.
    # None preserves existing SHENSI aliases, while the pipeline owns defaults.
    WUYU_PLANNER_MODEL = os.getenv('WUYU_PLANNER_MODEL','gemini-4-argon')
    WUYU_PLANNER_CONTEXT_TOKENS = os.getenv('WUYU_PLANNER_CONTEXT_TOKENS','1048576')
    WUYU_PLANNER_OUTPUT_TOKENS = os.getenv('WUYU_PLANNER_OUTPUT_TOKENS','32768')
    WUYU_PLANNER_MAX_RETRIES = os.getenv('WUYU_PLANNER_MAX_RETRIES', '0')
    WUYU_READING_SYNTHESIS_ENABLED = True
    WUYU_READING_INPUT_TOKENS = os.getenv('WUYU_READING_INPUT_TOKENS', '800000')
    # Alembic imports the application for its existing metadata. That process
    # must never claim queued provider jobs before the database is migrated.
    WUYU_DESIGN_SCHEDULER_DISABLED = os.getenv('WUYU_DESIGN_SCHEDULER_DISABLED', 'false').strip().lower() in {'1','true','yes','on'}
    WUYU_MIGRATION_PROCESS = os.getenv('WUYU_MIGRATION_PROCESS', '0')
    WUYU_WORKFLOW_EXECUTION_MODE = os.getenv('WUYU_WORKFLOW_EXECUTION_MODE', 'combined')
    # Deployment admission limits; never replace a historical model snapshot.
    WUYU_WORKFLOW_WORKERS = int(os.getenv('WUYU_WORKFLOW_WORKERS', '2'))
    WUYU_WORKFLOW_PER_ACTOR = int(os.getenv('WUYU_WORKFLOW_PER_ACTOR', '0'))
    WUYU_WORKFLOW_MEMORY_HIGH_BYTES = int(os.getenv('WUYU_WORKFLOW_MEMORY_HIGH_BYTES', '0'))
    WUYU_WORKFLOW_MEMORY_RESERVE_BYTES = int(os.getenv('WUYU_WORKFLOW_MEMORY_RESERVE_BYTES', '0'))
    WUYU_PAGE_RESOURCE_BYTES = int(os.getenv('WUYU_PAGE_RESOURCE_BYTES', '0'))
    WUYU_LOCAL_EXPORT_WORKERS = int(os.getenv('WUYU_LOCAL_EXPORT_WORKERS', '0'))
    # Separate server-owned policy for NEW jobs; historical model revisions stay frozen.
    WUYU_EXECUTION_PAGE_WORKERS = int(os.getenv('WUYU_EXECUTION_PAGE_WORKERS', '0'))
    WUYU_PAGE_IO_WORKERS = int(os.getenv('WUYU_PAGE_IO_WORKERS', '0'))
    WUYU_WORKFLOW_AUTOSTART = (
        os.getenv('WUYU_MIGRATION_PROCESS') != '1'
        and os.getenv('WUYU_WORKFLOW_AUTOSTART', 'true').strip().lower()
        in {'1', 'true', 'yes', 'on'}
    )
    SECRET_KEY = os.getenv('SECRET_KEY', 'your-secret-key-change-this')

    # JWT 认证配置
    JWT_SECRET_KEY = os.getenv('JWT_SECRET_KEY', '') or SECRET_KEY
    JWT_ACCESS_TOKEN_EXPIRES = int(os.getenv('JWT_ACCESS_TOKEN_EXPIRES', '86400'))  # 24小时
    JWT_ALGORITHM = 'HS256'

    WUYU_DATABASE_EXPECTED_DIALECT = os.getenv('WUYU_DATABASE_EXPECTED_DIALECT', 'sqlite')

    # 数据库配置
    # Use absolute path to avoid WSL path issues
    db_path = os.path.join(BASE_DIR, 'instance', 'database.db')
    SQLALCHEMY_DATABASE_URI = normalize_runtime_url(os.getenv(
        'DATABASE_URL', 
        f'sqlite:///{db_path}'
    ))
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
    # Preserve SQLite settings; PostgreSQL remains blocked until all readers
    # and accounting transactions have passed compatibility validation.
    SQLALCHEMY_ENGINE_OPTIONS = runtime_engine_options(SQLALCHEMY_DATABASE_URI)
    
    # 文件存储配置
    UPLOAD_FOLDER = os.path.join(PROJECT_ROOT, 'uploads')
    MAX_CONTENT_LENGTH = 502 * 1024 * 1024  # bounded 500MB workflow batch plus multipart framing
    ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
    ALLOWED_REFERENCE_FILE_EXTENSIONS = {'pdf', 'docx', 'pptx', 'doc', 'ppt', 'xlsx', 'xls', 'csv', 'txt', 'md'}
    
    # AI服务配置
    GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY', '')
    GOOGLE_API_BASE = os.getenv('GOOGLE_API_BASE', '')
    
    # Provider format: gemini | openai | vertex | lazyllm
    AI_PROVIDER_FORMAT = os.getenv('AI_PROVIDER_FORMAT', 'gemini')
    WUYU_MODEL_GATEWAY_ORIGIN = os.getenv('WUYU_MODEL_GATEWAY_ORIGIN', '')
    WUYU_MODEL_GATEWAY_EXPECTED_PORT = os.getenv('WUYU_MODEL_GATEWAY_EXPECTED_PORT', '8140')
    # Deployment-only transport fence; never serialized into historic jobs.
    WUYU_MODEL_GATEWAY_FORCE_ORIGIN = os.getenv('WUYU_MODEL_GATEWAY_FORCE_ORIGIN', '')
    WUYU_MODEL_GATEWAY_API_KEY = os.getenv('WUYU_MODEL_GATEWAY_API_KEY', '')

    # Google Cloud Vertex AI (requires AI_PROVIDER_FORMAT=vertex)
    VERTEX_PROJECT_ID = os.getenv('VERTEX_PROJECT_ID', '')
    VERTEX_LOCATION = os.getenv('VERTEX_LOCATION', 'us-central1')
    
    # GenAI (Gemini) 格式专用配置
    GENAI_TIMEOUT = float(os.getenv('GENAI_TIMEOUT', '300.0'))  # Gemini 超时时间（秒）
    GENAI_MAX_RETRIES = int(os.getenv('GENAI_MAX_RETRIES', '2'))  # Gemini 最大重试次数（应用层实现）
    
    # OpenAI 格式专用配置（当 AI_PROVIDER_FORMAT=openai 时使用）
    OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '')  # 当 AI_PROVIDER_FORMAT=openai 时必须设置
    OPENAI_API_BASE = os.getenv('OPENAI_API_BASE', 'https://aihubmix.com/v1')
    OPENAI_TIMEOUT = float(os.getenv('OPENAI_TIMEOUT', '480.0'))  # 8 分钟：留出 gpt-image-2 生图(~225s)+传输的余量
    OPENAI_MAX_RETRIES = int(os.getenv('OPENAI_MAX_RETRIES', '2'))  # 减少重试次数，避免过多重试导致累积超时

    # Anthropic 格式专用配置（当 AI_PROVIDER_FORMAT=anthropic 时使用）
    # 支持 ANTHROPIC_AUTH_TOKEN 作为 ANTHROPIC_API_KEY 的别名
    # 支持 ANTHROPIC_BASE_URL 作为 ANTHROPIC_API_BASE 的别名
    ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY', '') or os.getenv('ANTHROPIC_AUTH_TOKEN', '')
    ANTHROPIC_API_BASE = os.getenv('ANTHROPIC_API_BASE', '') or os.getenv('ANTHROPIC_BASE_URL', 'https://api.anthropic.com')
    ANTHROPIC_VERSION = os.getenv('ANTHROPIC_VERSION', '2023-06-01')
    ANTHROPIC_MAX_TOKENS = int(os.getenv('ANTHROPIC_MAX_TOKENS', '8192'))

    # Lazyllm 格式专用配置（当 AI_PROVIDER_FORMAT=lazyllm 时使用）
    TEXT_MODEL_SOURCE = os.getenv('TEXT_MODEL_SOURCE', '')                   # 文本生成模型厂商（留空则跟随全局 AI_PROVIDER_FORMAT）
    IMAGE_MODEL_SOURCE = os.getenv('IMAGE_MODEL_SOURCE', '')                   # 图片生成模型厂商（留空则跟随全局 AI_PROVIDER_FORMAT）
    IMAGE_CAPTION_MODEL_SOURCE = os.getenv('IMAGE_CAPTION_MODEL_SOURCE', '')   # 图片识别模型厂商（留空则跟随全局 AI_PROVIDER_FORMAT）

    # 各模型类型的独立 API 配置（优先级高于全局配置）
    # 文本模型独立配置
    TEXT_API_KEY = os.getenv('TEXT_API_KEY', '')
    TEXT_API_BASE = os.getenv('TEXT_API_BASE', '')
    # 图像模型独立配置
    IMAGE_API_KEY = os.getenv('IMAGE_API_KEY', '')
    IMAGE_API_BASE = os.getenv('IMAGE_API_BASE', '')
    # 图像精修模型独立配置（仅当 IMAGE_MODE=refine 时启用：第一步用 IMAGE_MODEL 生图，第二步用此模型重绘高清化、去除异常背景）
    IMAGE_REFINE_API_KEY = os.getenv('IMAGE_REFINE_API_KEY', '')
    IMAGE_REFINE_API_BASE = os.getenv('IMAGE_REFINE_API_BASE', '')
    IMAGE_REFINE_MODEL_SOURCE = os.getenv('IMAGE_REFINE_MODEL_SOURCE', '')
    # 图片识别模型独立配置
    IMAGE_CAPTION_API_KEY = os.getenv('IMAGE_CAPTION_API_KEY', '')
    IMAGE_CAPTION_API_BASE = os.getenv('IMAGE_CAPTION_API_BASE', '')
    
    # AI 模型配置
    TEXT_MODEL = os.getenv('TEXT_MODEL', 'gemini-3-flash-preview')
    IMAGE_MODEL = os.getenv('IMAGE_MODEL', 'gemini-3-pro-image-preview')
    # 图像生成模式: 'single' = 仅用 IMAGE_MODEL；'refine' = 两步生成（IMAGE_MODEL 出图 + IMAGE_REFINE_MODEL 精修）
    IMAGE_MODE = os.getenv('IMAGE_MODE', 'refine')
    IMAGE_REFINE_MODEL = os.getenv('IMAGE_REFINE_MODEL', 'gpt-image-2')
    ADVANCED_EDITABLE_TEXT_MODEL = os.getenv('ADVANCED_EDITABLE_TEXT_MODEL', 'gpt-5.5')
    ADVANCED_EDITABLE_ENGINE = os.getenv('ADVANCED_EDITABLE_ENGINE', 'builtin')
    ADVANCED_EDITABLE_IMAGE_MODEL = os.getenv('ADVANCED_EDITABLE_IMAGE_MODEL', 'gpt-image-2')
    ADVANCED_EDITABLE_USE_MINERU = os.getenv('ADVANCED_EDITABLE_USE_MINERU', 'true').lower() not in {'0', 'false', 'no', 'off'}
    ADVANCED_EDITABLE_USE_BAIDU_OCR = os.getenv('ADVANCED_EDITABLE_USE_BAIDU_OCR', 'true').lower() not in {'0', 'false', 'no', 'off'}
    ADVANCED_EDITABLE_USE_IMAGEGEN_ASSETS = os.getenv('ADVANCED_EDITABLE_USE_IMAGEGEN_ASSETS', 'true').lower() not in {'0', 'false', 'no', 'off'}
    LAYERD_WORKER_URL = os.getenv('LAYERD_WORKER_URL', '')
    LAYERD_WORKER_TOKEN = os.getenv('LAYERD_WORKER_TOKEN', '')
    LAYERD_WORKER_POLL_SECONDS = float(os.getenv('LAYERD_WORKER_POLL_SECONDS', '3'))
    # Codia cloud PDF-to-PPTX API (no local GPU needed)
    CODIA_API_KEY = os.getenv('CODIA_API_KEY', '')
    CODIA_API_BASE = os.getenv('CODIA_API_BASE', 'https://openapi.codia.ai')

    # MinerU 文件解析服务配置
    MINERU_TOKEN = os.getenv('MINERU_TOKEN', '')
    MINERU_API_BASE = os.getenv('MINERU_API_BASE', 'https://mineru.net')
    
    # 图片识别模型配置
    IMAGE_CAPTION_MODEL = os.getenv('IMAGE_CAPTION_MODEL', 'gemini-3-flash-preview')
    
    # 并发配置
    MAX_DESCRIPTION_WORKERS = int(os.getenv('MAX_DESCRIPTION_WORKERS', '20'))
    MAX_IMAGE_WORKERS = int(os.getenv('MAX_IMAGE_WORKERS', '20'))
    
    # 图片生成配置
    DEFAULT_ASPECT_RATIO = "16:9"
    DEFAULT_RESOLUTION = "2K"
    
    # 日志配置
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
    
    # CORS配置
    CORS_ORIGINS = os.getenv('CORS_ORIGINS', 'http://localhost:3000').split(',')
    
    # 输出语言配置
    # 可选值: 'zh' (中文), 'ja' (日本語), 'en' (English), 'auto' (自动)
    OUTPUT_LANGUAGE = os.getenv('OUTPUT_LANGUAGE', 'zh')
    
    # 火山引擎配置
    VOLCENGINE_ACCESS_KEY = os.getenv('VOLCENGINE_ACCESS_KEY', '')
    VOLCENGINE_SECRET_KEY = os.getenv('VOLCENGINE_SECRET_KEY', '')
    VOLCENGINE_INPAINTING_TIMEOUT = int(os.getenv('VOLCENGINE_INPAINTING_TIMEOUT', '60'))  # Inpainting 超时时间（秒）
    VOLCENGINE_INPAINTING_MAX_RETRIES = int(os.getenv('VOLCENGINE_INPAINTING_MAX_RETRIES', '3'))  # 最大重试次数

    # Inpainting Provider 配置（用于 InpaintingService 的单张图片修复）
    # 可选值: 'volcengine' (火山引擎), 'gemini' (Google Gemini)
    # 注意: 可编辑PPTX导出功能使用 ImageEditabilityService，其中 HybridInpaintProvider 会结合百度重绘和生成式质量增强
    INPAINTING_PROVIDER = os.getenv('INPAINTING_PROVIDER', 'gemini')  # 默认使用 Gemini

    # 百度 API 配置（用于 OCR 和图像修复）
    BAIDU_API_KEY = os.getenv('BAIDU_API_KEY', '') or os.getenv('BAIDU_OCR_API_KEY', '')

    # TTS 视频导出配置
    TTS_DEFAULT_VOICE_ZH = os.getenv('TTS_DEFAULT_VOICE_ZH', 'zh-CN-XiaoxiaoNeural')
    TTS_DEFAULT_VOICE_EN = os.getenv('TTS_DEFAULT_VOICE_EN', 'en-US-JennyNeural')
    TTS_DEFAULT_VOICE_JA = os.getenv('TTS_DEFAULT_VOICE_JA', 'ja-JP-NanamiNeural')
    TTS_DEFAULT_RATE = os.getenv('TTS_DEFAULT_RATE', '+0%')
    VIDEO_OUTPUT_WIDTH = int(os.getenv('VIDEO_OUTPUT_WIDTH', '1920'))
    VIDEO_OUTPUT_HEIGHT = int(os.getenv('VIDEO_OUTPUT_HEIGHT', '1080'))
    VIDEO_FPS = int(os.getenv('VIDEO_FPS', '25'))
    FFMPEG_PATH = os.getenv('FFMPEG_PATH', 'ffmpeg')
    DEFAULT_SILENT_CLIP_DURATION = float(os.getenv('DEFAULT_SILENT_CLIP_DURATION', '3.0'))


class DevelopmentConfig(Config):
    """Development configuration"""
    DEBUG = True


class ProductionConfig(Config):
    """Production configuration"""
    DEBUG = False


# 根据环境变量选择配置
config_map = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'default': DevelopmentConfig
}

def get_config():
    """Get configuration based on environment"""
    env = os.getenv('FLASK_ENV', 'development')
    return config_map.get(env, DevelopmentConfig)
