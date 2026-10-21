import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.observability import HTTP_REQUESTS, HTTP_SECONDS, route_label


class MetricsMiddleware:
    """Counts requests and times them, by method, bounded route and status."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        status = 500
        started = time.perf_counter()

        async def capture(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            method, route = scope["method"], route_label(scope["path"])
            HTTP_REQUESTS.labels(method, route, str(status)).inc()
            HTTP_SECONDS.labels(method, route).observe(time.perf_counter() - started)
