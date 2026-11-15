"""Keep legacy provider entrypoints from bypassing account metering."""
import re
from flask import current_app, request
from utils import error_response


def enforce_metered_generation():
    """All user generation uses the metered workflow, including administrator jobs."""
    if request.method == 'OPTIONS':
        return None
    path = request.path.rstrip('/')
    settings = path == '/api/settings' or path.startswith('/api/settings/')
    if settings and getattr(getattr(request, 'current_user', None), 'role', None) != 'admin':
        return error_response('FORBIDDEN', '只有管理员可以访问模型设置。', 403)
    if not current_app.config.get('WUYU_QUOTA_ENABLED', True):
        return None
    if not path.startswith('/api/') or re.match(r'^/api/projects/[^/]+/workflow(?:/|$)', path):
        return None
    if settings and request.method not in {'GET', 'HEAD'} and (path == '/api/settings/verify' or path.startswith('/api/settings/tests/')):
        return error_response('METERED_WORKFLOW_REQUIRED', '系统探测暂不支持收费生成，仅支持工作流程中的连接检查。', 409)
    blocked = False
    if request.method not in {'GET', 'HEAD'}:
        blocked = bool(re.search(r'/projects/[^/]+/(?:generate|refine)(?:/|$)', path)
            or re.search(r'/pages/[^/]+/(?:generate|edit|regenerate-renovation)(?:/|$)', path)
            or re.search(r'/materials/(?:generate|process)$', path)
            or re.search(r'/export/(?:editable-pptx|advanced-editable-pptx|video)$', path)
            or path == '/api/projects/renovation' or path == '/api/extract-style')
    # These old GET/upload routes lazily invoke a caption model.
    if path.startswith('/api/materials') and (path.endswith('/caption') or path.endswith('/by-url')):
        blocked = True
    if path.endswith('/materials/upload') or path == '/api/materials/upload':
        blocked = blocked or request.args.get('generate_caption', '').lower() in {'true', '1', 'yes'}
    if blocked:
        return error_response('METERED_WORKFLOW_REQUIRED', '请通过无隅 PPT 工作流程生成，以便正确记录账户额度。', 409)
    return None
