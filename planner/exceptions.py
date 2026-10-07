class PlannerError(Exception):
    """Base for errors the API reports to the client."""

    status_code = 500
    code = 'error'

    def __init__(self, message):
        super().__init__(message)
        self.message = message


class LocationError(PlannerError):
    status_code = 400
    code = 'location_not_found'


class InvalidRequest(PlannerError):
    status_code = 400
    code = 'invalid_request'


class InvalidLogin(PlannerError):
    status_code = 400
    code = 'invalid_login'


class NotSignedIn(PlannerError):
    status_code = 401
    code = 'not_signed_in'


class NotAllowed(PlannerError):
    status_code = 403
    code = 'not_allowed'


class RouteNotFound(PlannerError):
    status_code = 422
    code = 'route_not_found'


class NoFeasiblePlan(PlannerError):
    status_code = 422
    code = 'no_feasible_fuel_plan'


class RoutingProviderError(PlannerError):
    status_code = 502
    code = 'routing_provider_error'


class ProviderNotConfigured(PlannerError):
    status_code = 503
    code = 'routing_provider_not_configured'


class EncryptionNotConfigured(PlannerError):
    status_code = 503
    code = 'encryption_not_configured'
