"""Test-only verified GoReleaser handoff at the role's official HTTPS URLs."""
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
from pathlib import Path
import re
import ssl
import subprocess
import tempfile
import threading
from urllib.parse import quote

API = "/repos/perimeterd/perimeterd/releases"


def asset_name(version, platform):
    if platform.startswith("debian"):
        return f"perimeterd_{version}-1_amd64.deb"
    architecture = "aarch64" if platform.endswith("arm64") else "x86_64"
    return f"perimeterd-{version}-1.{architecture}.rpm"


def verified_package(package, manifest, version, platform):
    records = []
    for line in manifest.decode("ascii").splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64})(?:  | \*)([A-Za-z0-9][A-Za-z0-9._+~-]*)", line)
        if not match:
            raise ValueError("invalid checksum manifest record")
        if match[2] == package.name:
            records.append(match[1].lower())
    if len(records) != 1:
        raise ValueError("manifest must have exactly one selected package record")
    data = package.read_bytes()
    if hashlib.sha256(data).hexdigest() != records[0]:
        raise ValueError("selected package checksum mismatch")
    native_version = re.sub(r"^([0-9]+\.[0-9]+\.[0-9]+)-", r"\1~", version) + "-1"
    if platform.startswith("debian"):
        argv = ["dpkg-deb", "--show", "--showformat=${Package}\t${Version}\t${Architecture}", str(package)]
        expected = f"perimeterd\t{native_version}\tamd64"
    else:
        argv = ["rpm", "-qp", "--qf", "%{NAME}\t%{EPOCHNUM}:%{VERSION}-%{RELEASE}\t%{ARCH}", str(package)]
        expected = f"perimeterd\t0:{native_version}\t" + ("aarch64" if platform.endswith("arm64") else "x86_64")
    actual = subprocess.run(argv, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
    if actual != expected:
        raise ValueError(f"native package identity mismatch: expected {expected!r}, got {actual!r}")
    return data


class Candidate:
    def __init__(self, dist, platform):
        dist = Path(dist).resolve()
        metadata = json.loads((dist / "metadata.json").read_bytes())
        self.version = metadata["version"]
        if metadata.get("project_name") != "perimeterd" or not re.fullmatch(
                r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?(?:\+[A-Za-z0-9.-]+)?", self.version):
            raise ValueError("invalid original GoReleaser project/version metadata")
        self.name = asset_name(self.version, platform)
        self.manifest = (dist / "checksums.txt").read_bytes()
        self.package = verified_package(dist / self.name, self.manifest, self.version, platform)




class RestrictedProxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def do_CONNECT(self):
        if self.path not in ("api.github.com:443", "github.com:443"):
            self.send_error(403, "unexpected proxy destination")
            return
        self.fixture_host = self.path.removesuffix(":443")
        self.send_response(200, "Connection Established")
        self.end_headers()
        self.close_connection = True
        try:
            self.connection = self.server.tls_context.wrap_socket(self.connection, server_side=True)
            self.rfile = self.connection.makefile("rb", self.rbufsize)
            self.wfile = self.connection.makefile("wb", self.wbufsize)
            self.handle_one_request()
        except (ssl.SSLError, ConnectionError):
            pass
        finally:
            self.connection.close()

    def do_GET(self):
        host = getattr(self, "fixture_host", None)
        body = self.server.routes.get((host, self.path))
        with self.server.log_lock:
            with self.server.log.open("a") as output:
                output.write(f"{host} {self.path} {200 if body is not None else 404}\n")
        if self.headers.get("Host") not in (host, f"{host}:443") or body is None:
            self.send_error(404, "fixture host/route not configured")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json" if host == "api.github.com" else "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)


class PackageFixture:
    def __init__(self, candidate, logs):
        self.stack = ExitStack()
        try:
            self.work = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="perimeterd-package-tls-")))
            ca_key, self.cert = self.work / "ca.key", self.work / "ca.crt"
            key, cert, csr = self.work / "server.key", self.work / "server.crt", self.work / "server.csr"
            extensions = self.work / "extensions"
            extensions.write_text("subjectAltName=DNS:api.github.com,DNS:github.com\nbasicConstraints=CA:FALSE\nextendedKeyUsage=serverAuth\n")
            commands = [
                ["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=perimeterd disposable test CA", "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign", "-keyout", str(ca_key), "-out", str(self.cert)],
                ["req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=api.github.com", "-keyout", str(key), "-out", str(csr)],
                ["x509", "-req", "-in", str(csr), "-CA", str(self.cert), "-CAkey", str(ca_key), "-CAcreateserial", "-days", "1", "-extfile", str(extensions), "-out", str(cert)],
            ]
            for command in commands:
                subprocess.run(["openssl", *command], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            server = ThreadingHTTPServer(("127.0.0.1", 0), RestrictedProxy)
            self.server = server
            self.stack.callback(server.server_close)
            server.log, server.log_lock = logs / "fixture-requests.log", threading.Lock()
            server.routes = {}
            tag = candidate.version
            names = ("checksums.txt", candidate.name)
            metadata = {"id": 999999, "tag_name": tag, "draft": False, "prerelease": "-" in tag,
                        "published_at": "2026-10-02T00:00:00Z", "assets": [
                            {"id": 9999990 + i, "name": name, "state": "uploaded", "size": len(data),
                             "url": f"https://api.github.com{API}/assets/{9999990 + i}",
                             "browser_download_url": f"https://github.com/perimeterd/perimeterd/releases/download/{tag}/{name}"}
                            for i, (name, data) in enumerate(zip(names, (candidate.manifest, candidate.package)), 1)]}
            server.routes[("api.github.com", f"{API}/tags/{quote(tag, safe='')}")] = json.dumps(metadata).encode()
            for name, data in zip(names, (candidate.manifest, candidate.package)):
                server.routes[("github.com", f"/perimeterd/perimeterd/releases/download/{tag}/{name}")] = data
            server.tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server.tls_context.load_cert_chain(cert, key)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.stack.callback(thread.join, 10)
            self.stack.callback(server.shutdown)
        except BaseException:
            self.stack.close()
            raise

    @property
    def port(self):
        return self.server.server_port

    def environment(self, cert):
        proxy = f"http://127.0.0.1:{self.port}"
        return {"HTTPS_PROXY": proxy, "https_proxy": proxy, "HTTP_PROXY": "", "http_proxy": "",
                "ALL_PROXY": "", "all_proxy": "", "NO_PROXY": "", "no_proxy": "",
                "SSL_CERT_FILE": str(cert), "REQUESTS_CA_BUNDLE": str(cert)}

    def close(self):
        self.stack.close()
