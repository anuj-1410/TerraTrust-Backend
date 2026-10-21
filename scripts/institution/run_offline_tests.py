"""Run pytest without live sockets, with a Windows sandbox temp-dir workaround."""
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main():
    if len(sys.argv) == 1:
        import subprocess
        files = sorted((ROOT / "tests").glob("test_*.py"))
        groups = [[str(p.relative_to(ROOT)) for p in files if not p.name.startswith("test_institution_")], [str(p.relative_to(ROOT)) for p in files if p.name.startswith("test_institution_")]]
        codes = []
        for group in groups:
            if group:
                codes.append(subprocess.run([sys.executable, str(Path(__file__).resolve()), *group], cwd=ROOT).returncode)
        return max(codes, default=0)
    import pytest

    class Offline:
        @pytest.fixture(autouse=True)
        def prohibit_network(self, monkeypatch):
            import socket
            original_connect = socket.socket.connect
            socketpair_code = getattr(socket.socketpair, "__code__", None)
            def denied(connection, address):
                # Windows asyncio implements its internal wakeup socketpair over
                # loopback TCP. Require the actual stdlib builder as caller so
                # a localhost database/proxy is blocked like every other service.
                if socketpair_code is not None and sys._getframe(1).f_code is socketpair_code and isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
                    return original_connect(connection, address)
                raise AssertionError("Offline tests cannot open network sockets.")
            monkeypatch.setattr(socket.socket, "connect", denied)
            monkeypatch.setattr(socket.socket, "connect_ex", denied)

        @pytest.fixture
        def tmp_path(self, request):
            # pytest's mode=0700 directories are inaccessible with this managed
            # Windows sandbox's creator ACL. Ordinary workspace dirs are usable.
            path = ROOT / ".test-artifacts" / (request.node.name[:60] + "-" + uuid4().hex)
            path.mkdir(mode=0o777, parents=True)
            return path

    return pytest.main(["-q", "-p", "no:cacheprovider", *sys.argv[1:]], plugins=[Offline()])


if __name__ == "__main__":
    raise SystemExit(main())
