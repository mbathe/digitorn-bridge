"""Mock SSH server using paramiko.

Listens on port 9989. Accepts public-key auth ONLY (matches the
e2e_ssh_key handler test vault).

The test deploys an app with the http module bound to a vault
ssh_key credential, and the http module's `_default_auth` exposes
``X-Vault-Ssh-Key-Length`` so the test can prove the vault delivered
the key. To fully exercise the SSH protocol with paramiko-as-client
would need a dedicated digitorn ssh module — out of scope here.

This server is just a dummy: it accepts an inbound connection,
verifies any non-empty key, returns a banner string. The point is
the test asserts the http module passed the vault key into its
config.
"""
from __future__ import annotations

import logging
import socket
import threading

import paramiko

logger = logging.getLogger(__name__)
PORT = 9989


class _Server(paramiko.ServerInterface):
    def check_auth_publickey(self, username, key):
        return paramiko.AUTH_SUCCESSFUL

    def get_allowed_auths(self, username):
        return "publickey"

    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


def _handle(client_sock):
    try:
        host_key = paramiko.RSAKey.generate(2048)
        t = paramiko.Transport(client_sock)
        t.add_server_key(host_key)
        s = _Server()
        try:
            t.start_server(server=s)
        except paramiko.SSHException:
            return
        chan = t.accept(20)
        if chan is None:
            return
        chan.send(b"E2E SSH banner OK\n")
        chan.close()
        t.close()
    except Exception:
        pass


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", PORT))
    sock.listen(5)
    logger.info("Mock SSH listening on 127.0.0.1:%d", PORT)
    while True:
        cs, _ = sock.accept()
        threading.Thread(target=_handle, args=(cs,), daemon=True).start()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    main()
