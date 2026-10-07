from rest_framework.authentication import SessionAuthentication


class CsrfCheckedSessionAuthentication(SessionAuthentication):
    """Session authentication that checks the CSRF token even when nobody is signed in.

    REST framework's own class checks it only for a signed-in user, which leaves
    out the one request where it matters before that: signing in. Requests that
    change nothing (GET and the like) are let through as usual.
    """

    def authenticate(self, request):
        self.enforce_csrf(request)
        return super().authenticate(request)
