"""用 HTTP MockTransport 检查实际出站请求及重定向，不接触真实凭证。"""

import httpx
import pytest

from baidu_archive.auth import Credentials
from baidu_archive.client import ArchiveClient, FetchError
from baidu_archive.config import Config


def make_client(handler, log=None):
    client = ArchiveClient(Config(delay=0, jitter=0, max_retries=1),
                           Credentials(cookie_header="BDUSS=FAKE_SESSION"), logger=log)
    client._client.close()
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=True,
        event_hooks={"request": [client._scope_credentials]},
    )
    return client


@pytest.mark.parametrize("destination", [
    "https://images.example/image.jpg", "https://wenzhang.bdstatic.com/page/content/",
    "https://wenzhang.baidu.com.example/image.jpg", "http://wenzhang.baidu.com/image.jpg",
])
def test_cookie_never_reaches_assets_or_redirect_targets(destination):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(302, headers={"location": destination})
        return httpx.Response(200, content=b"body")

    with make_client(handler) as client:
        client.get("https://wenzhang.baidu.com/page/view?key=fixture", expect="raw")
    assert requests[0].headers["cookie"] == "BDUSS=FAKE_SESSION"
    assert "cookie" not in requests[1].headers


def test_direct_external_request_strips_sensitive_headers():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=b"image")

    with make_client(handler) as client:
        client._client.headers.update({"Cookie": "BDUSS=FAKE_SESSION", "Authorization": "FAKE",
                                       "Referer": "https://wenzhang.baidu.com/?sign=FAKE"})
        client.get("https://images.example/photo.jpg", expect="raw")
    for name in ("cookie", "authorization", "referer"):
        assert name not in requests[0].headers


def test_transport_exception_does_not_expose_signed_url():
    messages = []

    def handler(request):
        raise httpx.ConnectError("https://images.example/?sign=PRIVATE_VALUE", request=request)

    with make_client(handler, lambda tag, message: messages.append(message)) as client:
        with pytest.raises(FetchError) as error:
            client.get("https://images.example/?sign=PRIVATE_VALUE")
    assert "PRIVATE_VALUE" not in str(error.value) + "".join(messages)
