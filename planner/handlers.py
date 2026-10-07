"""One error shape for every API failure: {"error": {"code": ..., "message": ...}}."""

import logging

from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import exception_handler

from .exceptions import PlannerError

logger = logging.getLogger(__name__)


def _body(code, message, **extra):
    return {'error': {'code': code, 'message': message, **extra}}


def api_exception_handler(exc, context):
    if isinstance(exc, PlannerError):
        return Response(_body(exc.code, exc.message), status=exc.status_code)

    # DRF's own handler covers its exceptions (bad JSON, wrong method, throttled)
    # and sets headers such as Retry-After; only the body is reshaped here.
    response = exception_handler(exc, context)
    if response is None:
        logger.exception('Unhandled error in %s', context['view'].__class__.__name__)
        return Response(_body('internal_error', 'Unexpected server error.'), status=500)
    if isinstance(exc, ValidationError):
        response.data = _body('invalid_request', 'Invalid request.', fields=exc.detail)
    else:
        detail = response.data.get('detail', '')
        response.data = _body(getattr(detail, 'code', 'error'), str(detail))
    return response
