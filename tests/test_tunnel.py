"""Cloudflare tunnel: http2 protocol and 'only report the URL once connected' (regression 2026-11-08)."""
import ops.tunnel as tunnel

LOG_BEFORE = """2026-11-08T13:00:01Z INF Requesting new quick Tunnel on trycloudflare.com...
2026-11-08T13:00:03Z INF |  https://queries-interfaces-consisting-break.trycloudflare.com  |
2026-11-08T13:00:13Z ERR Failed to dial a quic connection error="failed to dial to edge with quic: timeout"
"""
LOG_CONNECTED = LOG_BEFORE + ("2026-11-08T13:00:20Z INF Registered tunnel connection connIndex=0 "
                              "connection=abc event=0 ip=198.41.200.13 location=sin01 protocol=http2\n")


class FakeSvc:
    def __init__(self, text, alive=True):
        self.text, self.alive = text, alive

    def tail(self, n=40):
        return self.text

    def running(self):
        return self.alive


def test_ignores_previous_runs_in_the_persistent_log():
    """Regression: the cell printed the previous run's URL (notified-lease...) instead of the live one."""
    old = ("===== start 2026-11-08 13:30:00 =====\n"
           "INF |  https://notified-lease-easily-asn.trycloudflare.com  |\n"
           "INF Registered tunnel connection connIndex=0 protocol=http2\n")
    new_starting = "===== start 2026-11-08 13:35:16 =====\nINF Requesting new quick Tunnel...\n"
    new_ready = new_starting + ("INF |  https://athletic-gale-peak-hereby.trycloudflare.com  |\n"
                                "INF Registered tunnel connection connIndex=0 protocol=http2\n")
    assert tunnel.public_url(FakeSvc(old + new_starting, alive=False), timeout_s=1) is None
    assert tunnel.public_url(FakeSvc(old + new_ready), timeout_s=1) == \
        "https://athletic-gale-peak-hereby.trycloudflare.com"


def test_service_uses_http2(monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_TUNNEL_TOKEN", raising=False)
    cmd = tunnel.service().cmd
    assert cmd[cmd.index("--protocol") + 1] == "http2"


def test_url_only_after_registration(capsys):
    assert tunnel.public_url(FakeSvc(LOG_CONNECTED), timeout_s=1) == \
        "https://queries-interfaces-consisting-break.trycloudflare.com"
    assert tunnel.public_url(FakeSvc(LOG_BEFORE, alive=False), timeout_s=1) is None
    assert "failed to dial to edge with quic" in capsys.readouterr().out
