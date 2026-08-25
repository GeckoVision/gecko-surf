"""`--port` moves the RPC port and NOTHING ELSE — which made two forks impossible.

surfpool's WebSocket port is separate from its RPC port and defaults to 8900. So two
forks on different `port` values still collided on it, and the second died with
"WebSocket port 8900 is already in use" — surfaced by SurfpoolFork as the far less
obvious "surfpool exited before becoming ready".

That is the same shape as the `--store` half-select: a knob that moves one half of a pair
and silently leaves the other pinned. It is also why an orphaned fork from a previous run
can block every subsequent one on the machine, which is exactly what it did — an
11-hour-old surfpool on `--port 8937` held 8900 and no new fork could start.

These assert on the ARGV, so they need no surfpool binary and no network.
"""

from gecko.pda_testkit import SurfpoolFork


def test_the_ws_port_defaults_to_rpc_port_plus_one() -> None:
    """surfpool's own pairing is 8899/8900, so port+1 keeps existing callers byte-identical
    while making the second half movable at all."""
    assert SurfpoolFork("https://rpc.example", port=8899).ws_port == 8900
    assert SurfpoolFork("https://rpc.example", port=8937).ws_port == 8938


def test_an_explicit_ws_port_wins() -> None:
    fork = SurfpoolFork("https://rpc.example", port=8941, ws_port=9999)
    assert fork.port == 8941
    assert fork.ws_port == 9999


def test_two_forks_on_different_ports_do_not_share_a_ws_port() -> None:
    """The regression itself: before this, both of these answered 8900."""
    first = SurfpoolFork("https://rpc.example", port=8899)
    second = SurfpoolFork("https://rpc.example", port=8941)
    assert first.ws_port != second.ws_port
