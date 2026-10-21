"""Allowlisted HTTPS retrieval with DNS pinning, bounded redirects and bytes."""
import http.client
import ipaddress
import socket
import ssl
from urllib.parse import urljoin, urlsplit

from app.institution_errors import fail


def _fetch(url, allowed_hosts, maximum=10485760):
    for _ in range(4):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.hostname.lower() not in allowed_hosts or parsed.username or parsed.password or parsed.port not in {None,443}:
            fail(422,"UNSAFE_SOURCE_URL","Source URL must use an explicitly approved public HTTPS host.")
        answers = socket.getaddrinfo(parsed.hostname,443,type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(answer[4][0] for answer in answers))
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            fail(422,"UNSAFE_SOURCE_URL","Source DNS cannot resolve to private, local or reserved addresses.")
        hostname, address = parsed.hostname, addresses[0]
        class PinnedHTTPS(http.client.HTTPSConnection):
            def connect(self):
                plain = socket.create_connection((address,443),timeout=20)
                self.sock = ssl.create_default_context().wrap_socket(plain,server_hostname=hostname)
        connection = PinnedHTTPS(hostname,timeout=20)
        try:
            path = parsed.path or "/"
            if parsed.query:
                path += "?"+parsed.query
            connection.request("GET",path,headers={"Accept":"application/pdf","User-Agent":"TerraTrust-PublicCorpus/1"})
            response = connection.getresponse()
            if response.status in {301,302,303,307,308}:
                destination = response.getheader("Location")
                if not destination:
                    fail(422,"SOURCE_REDIRECT_INVALID","Source redirect has no destination.")
                url = urljoin(url,destination)
                continue
            if response.status != 200:
                fail(503,"PUBLIC_SOURCE_UNAVAILABLE","The approved public source did not return its document.",retryable=response.status in {429,503})
            if response.getheader("Content-Type","").split(";")[0].strip().lower() != "application/pdf":
                fail(422,"UNSUPPORTED_SOURCE_FORMAT","The reviewed public source must return a PDF document.")
            length = response.getheader("Content-Length")
            if length and (not length.isdecimal() or int(length)>maximum):
                fail(413,"SOURCE_TOO_LARGE","Public source exceeds the approved byte limit.")
            data = response.read(maximum+1)
            if len(data)>maximum:
                fail(413,"SOURCE_TOO_LARGE","Public source exceeds the approved byte limit.")
            return data
        finally:
            connection.close()
    fail(422,"SOURCE_REDIRECT_INVALID","Public source exceeded the supported redirect count.")


def fetch(url, allowed_hosts, maximum=10485760):
    try:
        return _fetch(url, allowed_hosts, maximum)
    except (OSError, http.client.HTTPException):
        # Public-source GETs have no mutation outcome to duplicate. Preserve a
        # safe retry path for DNS, TLS, connect and bounded read failures.
        fail(503, "PUBLIC_SOURCE_UNAVAILABLE", "The approved public document could not be retrieved.", retryable=True)
    except (ValueError, UnicodeError):
        fail(422, "UNSAFE_SOURCE_URL", "Supply a valid approved public HTTPS document URL.")
