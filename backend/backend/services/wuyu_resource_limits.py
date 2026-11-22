"""Deployment-only RAM admission for durable jobs and local image/export work.

Limits are shared by one app/worker and are independent of frozen model settings.
Waiting holds only metadata. Once a model call starts the existing paid-result
preservation contract remains unchanged.
"""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager, nullcontext
from pathlib import Path, PurePosixPath
import json
import threading
from .wuyu_errors import PipelineError


def cgroup_memory_current():
    """Read our own cgroup v2, never substitute the whole host's memory usage."""
    rows = Path('/proc/self/cgroup').read_text(encoding='ascii').splitlines()
    own = next(line[3:] for line in rows if line.startswith('0::'))
    parts = PurePosixPath(own).parts
    if not own.startswith('/') or '..' in parts:
        raise ValueError('Invalid memory cgroup')
    path = Path('/sys/fs/cgroup').joinpath(*parts[1:], 'memory.current')
    value = path.read_text(encoding='ascii').strip()
    if not value.isdecimal():
        raise ValueError('Invalid cgroup memory counter')
    return int(value)



def cgroup_reclaimable_file(current):
    """Conservative clean-inactive-file credit; missing data earns no credit.

    Dirty/writeback counters cover ALL file lists. Subtract both from inactive
    before giving credit, since their overlap/location cannot be established.
    Shared memory is not evictable file cache. Never subtract anonymous/active
    pages or assume that memory.stat's type and LRU counters partition equally.
    """
    try:
        rows = Path('/proc/self/cgroup').read_text(encoding='ascii').splitlines()
        own = next(line[3:] for line in rows if line.startswith('0::'))
        parts = PurePosixPath(own).parts
        if not own.startswith('/') or '..' in parts:
            return 0
        path = Path('/sys/fs/cgroup').joinpath(*parts[1:], 'memory.stat')
        pairs = [line.split() for line in path.read_text(encoding='ascii').splitlines()]
        stats = {}
        for pair in pairs:
            if len(pair) != 2 or pair[0] in stats or not pair[1].isdecimal():
                return 0
            stats[pair[0]] = int(pair[1])
        required = ('file', 'shmem', 'inactive_file', 'file_dirty', 'file_writeback')
        if type(current) is not int or current < 0:
            return 0
        if any(key not in stats or not 0 <= stats[key] <= current for key in required):
            return 0
        if any(stats[key] > stats['file'] for key in required[1:]):
            return 0  # Inconsistent/non-atomic observations keep raw admission.
        clean_inactive = max(0, stats['inactive_file'] - stats['file_dirty'] - stats['file_writeback'])
        return min(current, clean_inactive, stats['file'] - stats['shmem'])
    except (OSError, ValueError, StopIteration, UnicodeError):
        return 0


def memory_admission(config):
    high = int(config.get('WUYU_WORKFLOW_MEMORY_HIGH_BYTES', 0))
    reserve = int(config.get('WUYU_WORKFLOW_MEMORY_RESERVE_BYTES', 0))
    if high <= 0:
        return {'allowed': True, 'reason': 'disabled'}
    try:
        current = cgroup_memory_current()
    except (OSError, ValueError, StopIteration):
        return {'allowed': False, 'reason': 'memory_state_unknown'}
    reclaimable = cgroup_reclaimable_file(current)
    working_set = current - reclaimable
    allowed = working_set + max(0, reserve) <= high
    return {'allowed': allowed,
            'reason': 'available' if allowed else 'memory_wait',
            'current_bytes': current, 'working_set_bytes': working_set,
            'reclaimable_file_bytes': reclaimable,
            'admission_bytes': high, 'reserve_bytes': reserve}


class ByteBudget:
    """FIFO weighted leases: actual bytes, not a hidden model-request ceiling."""
    def __init__(self, capacity):
        self.capacity = int(capacity)
        if self.capacity <= 0:
            raise ValueError('Positive resource budget required')
        self.used = 0
        self.peak = 0
        self._condition = threading.Condition()
        self._queue = deque()

    @contextmanager
    def lease(self, weight, check_cancel=None):
        weight = max(1, int(weight))
        if weight > self.capacity:
            raise PipelineError('capacity_memory_limit',
                '单项工作超过当前内存预算，未发起新的模型请求；请减少参考材料或分批处理。')
        ticket = object()
        acquired = False
        with self._condition:
            self._queue.append(ticket)
            try:
                while self._queue[0] is not ticket or self.used + weight > self.capacity:
                    if check_cancel is not None:
                        check_cancel()
                    self._condition.wait(.05)
                if check_cancel is not None:
                    check_cancel()
                self._queue.popleft()
                self.used += weight
                self.peak = max(self.peak, self.used)
                acquired = True
                self._condition.notify_all()
            finally:
                if not acquired:
                    self._queue.remove(ticket)
                    self._condition.notify_all()
        try:
            yield
        finally:
            with self._condition:
                self.used -= weight
                self._condition.notify_all()


class ExecutionResources:
    def __init__(self, config):
        capacity = int(config.get('WUYU_PAGE_RESOURCE_BYTES', 0))
        exports = int(config.get('WUYU_LOCAL_EXPORT_WORKERS', 0))
        if capacity < 0 or not 0 <= exports <= 20:
            raise ValueError('Invalid local resource limits')
        self.budget = ByteBudget(capacity) if capacity else None
        self.exports = threading.BoundedSemaphore(exports) if exports else None


_creation_lock = threading.Lock()


def resources(app):
    with _creation_lock:
        if not hasattr(app, 'extensions'):
            app.extensions = {}
        if 'wuyu_execution_resources' not in app.extensions:
            app.extensions['wuyu_execution_resources'] = ExecutionResources(app.config)
        return app.extensions['wuyu_execution_resources']


def _catalog_reference_paths(plan, config):
    """Resolve the same selected catalog metadata without decoding any images."""
    configured = (config.get('WUYU_TEMPLATE_LIBRARY_PATH') or
                  config.get('WUYU_TEMPLATE_LIBRARY_ROOT') or config.get('WUYU_TEMPLATE_CATALOG'))
    if not configured or not isinstance(plan, dict) or not isinstance(plan.get('recipe'), dict):
        return [], None
    try:
        path = Path(configured).resolve()
        if path.is_dir():
            path /= 'catalog.json'
        catalog = json.loads(path.read_text(encoding='utf-8-sig'))
        root = path.parent
        from .wuyu_template_context import choose_page_references, normalize_template_reference
        original = plan['recipe']
        recipe = dict(original, style_anchor=normalize_template_reference(original.get('style_anchor'), catalog),
                      references=[normalize_template_reference(item, catalog)
                                  for item in original.get('references') or []])
        approved = recipe.get('approved_sample_refs') or []
        approved = sorted(approved, key=lambda ref: 'first_body' not in str(ref['id']))
        candidates = [ref['id'] for ref in approved] + ([recipe['style_anchor']] if recipe.get('style_anchor') else []) + list(recipe.get('references') or [])
        spec = plan.get('spec') or {}
        if spec.get('layout') or spec.get('kind') or spec.get('number'):
            candidates, _, _ = choose_page_references(recipe, catalog, root, spec)
        unique = []
        for item in candidates:
            ident = item.get('page_id') or item.get('source_id') if isinstance(item, dict) else item
            if ident not in unique:
                unique.append(ident)
        images = []
        for ident in unique[:5]:
            for source in catalog.get('sources', []):
                if source.get('id') == ident and source.get('cover'):
                    images.append(root / source['cover'])
                images.extend(root / page['image'] for page in source.get('pages', [])
                              if page.get('id') == ident and page.get('image'))
        return images, root
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        # Original template validation still reports its own precise error.
        return [], None


def page_weight(plan, upload_folder, config=None, *, stage='image'):
    """Trusted call-site stage; include every trace input and decoded pixels."""
    if stage not in {'image', 'trace'}:
        raise ValueError('Invalid page resource stage')
    root = Path(upload_folder).resolve()
    catalog_paths, catalog_root = _catalog_reference_paths(plan, config or {})
    catalog_images = {path.resolve() for path in catalog_paths
                      if catalog_root is not None and path.resolve().is_relative_to(catalog_root)}
    sizes = {}
    def visit(value, key=''):
        if isinstance(value, dict):
            for name, item in value.items():
                visit(item, name)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item, key)
        elif isinstance(value, str) and key in {
                'path', 'source', 'previous_image_path', 'image_path', 'preview_path'}:
            path = Path(value)
            if path.is_absolute() and path.suffix.lower() in {'.png', '.jpg', '.jpeg', '.webp'}:
                try:
                    path = path.resolve()
                    if (path.is_relative_to(root) or path in catalog_images) and path.is_file():
                        size = path.stat().st_size
                        try:
                            from PIL import Image
                            with Image.open(path) as image:
                                pixels = min(40_000_000, image.width * image.height)
                        except (OSError, ValueError, Image.DecompressionBombError):
                            # Invalid input remains for the pipeline to classify;
                            # reserve the existing maximum instead of guessing low.
                            pixels = 40_000_000
                        sizes[str(path)] = 6 * size + 2 * pixels * 4
                except OSError:
                    pass  # The pipeline still performs its original file validation.
    visit(plan)
    for path in catalog_paths:
        visit(str(path), 'path')
    # Existing image contract permits at most nine reference images and 40M
    # pixels per response; keep space for SDK/base64/RGB and PNG intermediates.
    if stage == 'trace':
        # Trace consumes an existing image and bounded JSON, not a new 40MP
        # image response. Retain conservative compressed/decoded input weights.
        return 64 * 1024**2 + sum(sizes.values())
    return 384 * 1024**2 + sum(sorted(sizes.values(), reverse=True)[:9])


def page_slot(app, plan, check_cancel, pipeline_config=None, *, stage='image'):
    limits = resources(app)
    return (limits.budget.lease(page_weight(plan, app.config.get('UPLOAD_FOLDER', '.'), pipeline_config or app.config, stage=stage), check_cancel)
            if limits.budget is not None else nullcontext())


@contextmanager
def export_slot(app, check_cancel):
    limits = resources(app)
    acquired = False
    try:
        if limits.exports is not None:
            while not limits.exports.acquire(timeout=.05):
                check_cancel()
            acquired = True
        check_cancel()
        with (limits.budget.lease(768 * 1024**2, check_cancel)
              if limits.budget is not None else nullcontext()):
            yield
    finally:
        if acquired:
            limits.exports.release()
