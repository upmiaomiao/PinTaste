"""Public, credential-free errors for the existing Flask workflow."""


class PipelineError(RuntimeError):
    def __init__(self, code, message, retryable=False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def to_dict(self):
        return {'code': self.code, 'message': self.message, 'retryable': self.retryable}


# Failures of a saved model response that an explicit user "retry" may answer with
# ONE fresh paid request (see WuyuPipeline.trace_slide). Single definition shared by
# the pipeline.
EXPLICIT_RETRY_CODES = frozenset({
    'invalid_trace', 'invalid_model_json', 'trace_out_of_bounds', 'unsupported_trace_shape',
    'trace_text_missing', 'text_ghosting_risk', 'trace_repair_changed_content',
    'trace_repair_limit', 'trace_cache_unavailable'})

# Subset reported to the user as "retryable" on the job record. Generic structured-output
# failures (planning, reading) stay non-retryable on purpose: repeating them blindly
# costs money without changing anything. Only per-page tracing has the explicit,
# once-per-input retry path, so only its codes are offered as retryable.
USER_RETRYABLE_TRACE_CODES = frozenset({
    'invalid_trace', 'trace_out_of_bounds', 'unsupported_trace_shape', 'trace_text_missing',
    'text_ghosting_risk', 'trace_repair_changed_content', 'trace_repair_limit'})


def check_cancel(cancel_check=None):
    if cancel_check and cancel_check():
        raise PipelineError('cancelled', '任务已取消。')
