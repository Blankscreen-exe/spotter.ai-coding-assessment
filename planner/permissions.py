"""Who may change what. Reading is open to everyone; the route endpoint has no permissions at all."""

from rest_framework.permissions import SAFE_METHODS, BasePermission

from .exceptions import NotAllowed, NotSignedIn

CHANGE_SETTINGS = 'planner.change_setting'
SET_PROVIDER_KEYS = 'planner.change_providercredential'


class CanChangeSettings(BasePermission):
    """Anyone may read the server settings. Changing them takes an account that is
    allowed to, because they apply to every client.

    The two refusals are raised rather than returned so that a client is told
    which it is: 401 to sign in, 403 when signing in would not help.
    """

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        if not request.user.is_authenticated:
            raise NotSignedIn('Sign in to change the server settings.')
        if not request.user.has_perm(CHANGE_SETTINGS):
            raise NotAllowed('This account may not change the server settings.')
        return True
