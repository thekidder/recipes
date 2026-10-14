from server.recipes.db import Problem


class ApiError(Problem):
    def __init__(self, code, message, status=422, *, retryable=False, details=None, headers=None):
        super().__init__(message, status)
        self.code, self.retryable = code, retryable
        self.details, self.headers = details or {}, headers or {}

    def detail(self):
        return dict(code=self.code, message=str(self), retryable=self.retryable, details=self.details)


def error_detail(value):
    if not value:
        return None
    import json
    if isinstance(value, str):
        value = json.loads(value)
    aliases = {'missing_credentials': 'configuration_missing', 'missing_api_key': 'configuration_missing',
               'openai_not_configured': 'configuration_missing', 'incomplete_recipe': 'invalid_extraction'}
    return dict(code=aliases.get(value.get('code'), value.get('code', 'processing_error')),
                message=value.get('message', 'Processing failed.'),
                retryable=bool(value.get('retryable', False)), details=value.get('details', {}))
