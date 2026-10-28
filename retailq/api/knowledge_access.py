"""Local console access checks run before parsing any management request body."""
from ipaddress import ip_address
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.routing import APIRoute

from retailq.errors import ServiceError


def forbidden():
    return ServiceError('knowledge_console_forbidden', 403, '知识管理仅接受本机同源操作。')


def require_console_local(request: Request):
    try:
        if request.client is None or not ip_address(request.client.host).is_loopback:
            raise forbidden()
        if len(request.headers.getlist('host')) != 1 or request.url.hostname not in ('localhost','127.0.0.1','::1'):
            raise forbidden()
        request.url.port  # Validate the authority's port, too.
    except (ValueError, TypeError):
        raise forbidden() from None


def require_console_write(request: Request):
    require_console_local(request)
    if request.headers.get('content-type','').split(';',1)[0].strip().lower() != 'application/json':
        raise forbidden()
    if request.headers.getlist('x-retailq-console') != ['1'] or len(request.headers.getlist('origin')) != 1:
        raise forbidden()
    try:
        origin = urlsplit(request.headers['origin'])
        if (origin.scheme not in ('http','https') or origin.username is not None or origin.password is not None
                or origin.path or origin.query or origin.fragment):
            raise forbidden()
        default = lambda scheme: 443 if scheme == 'https' else 80
        expected = (request.url.scheme, request.url.hostname, request.url.port or default(request.url.scheme))
        actual = (origin.scheme, origin.hostname, origin.port or default(origin.scheme))
        if actual != expected:
            raise forbidden()
    except (ValueError, TypeError):
        raise forbidden() from None


class ConsoleRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handle(request: Request):
            require_console_local(request)
            if request.method not in ('GET','HEAD','OPTIONS'):
                require_console_write(request)
            return await original(request)
        return handle
