import socket

import pytest

from rdk_maze_tuner.core.tcp_stream import SocketJsonStream, TcpStreamError, parse_tcp_endpoint


def test_parse_tcp_endpoint_supports_hostnames_and_ipv6():
    assert parse_tcp_endpoint("127.0.0.1:8765") == ("127.0.0.1", 8765)
    assert parse_tcp_endpoint("localhost:1234") == ("localhost", 1234)
    assert parse_tcp_endpoint("[::1]:8765") == ("::1", 8765)


@pytest.mark.parametrize(
    "endpoint",
    ["localhost", ":8765", "localhost:not-a-port", "localhost:0", "localhost:70000", "[::1]8765"],
)
def test_parse_tcp_endpoint_rejects_invalid_values(endpoint):
    with pytest.raises(TcpStreamError):
        parse_tcp_endpoint(endpoint)


def test_socket_json_stream_reads_complete_lines_and_writes_bytes():
    client_socket, peer_socket = socket.socketpair()
    client_socket.settimeout(0.01)
    stream = SocketJsonStream(client_socket)
    try:
        peer_socket.sendall(b'{"type":"ready"}\n{"type":"telemetry"}\n')

        assert stream.readline() == b'{"type":"ready"}\n'
        assert stream.readline() == b'{"type":"telemetry"}\n'

        heartbeat = b'{"type":"heartbeat"}\n'
        assert stream.write(heartbeat) == len(heartbeat)
        assert peer_socket.recv(64) == b'{"type":"heartbeat"}\n'
    finally:
        stream.close()
        peer_socket.close()


def test_socket_json_stream_returns_empty_bytes_on_read_timeout():
    client_socket, peer_socket = socket.socketpair()
    client_socket.settimeout(0.001)
    stream = SocketJsonStream(client_socket)
    try:
        assert stream.readline() == b""
    finally:
        stream.close()
        peer_socket.close()
