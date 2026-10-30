"""
A tiny fake SSH server for tests (paramiko): password auth, exec requests,
canned output. Records every command it was asked to run.
"""

import socket
import threading

import paramiko


class _Server(paramiko.ServerInterface):
    def __init__(self, mock):
        self.mock = mock
        self.command = None
        self.ready = threading.Event()

    def check_auth_password(self, username, password):
        self.mock.passwords_seen.append(password)
        if (username, password) == (self.mock.username, self.mock.password):
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_exec_request(self, channel, command):
        self.command = command.decode()
        self.ready.set()
        return True


class MockSSH:
    def __init__(self, username="root", password="pw", port=0):
        self.username, self.password = username, password
        self.key = paramiko.ECDSAKey.generate()
        self.commands = []
        self.passwords_seen = []         # every password a client sent
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", port))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.transports = []
        self.running = True
        threading.Thread(target=self._accept, daemon=True).start()

    def hostkey_hex(self):
        import hashlib
        return hashlib.sha256(self.key.asbytes()).hexdigest()

    def respond(self, cmd):
        """-> (stdout, stderr, exit_code)"""
        if cmd == "uname -a":
            return "Linux mock-unifi 5.4.0 #1 SMP aarch64 GNU/Linux\n", "", 0
        if cmd == "cat /proc/loadavg":
            return "0.10 0.20 0.30 1/200 12345\n", "", 0
        if cmd.startswith("ls /nope"):
            return "", "ls: cannot access '/nope': No such file or directory\n", 2
        if cmd == "reboot":
            return "rebooting\n", "", 0
        return f"ran: {cmd}\n", "", 0

    def _accept(self):
        while self.running:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        t = paramiko.Transport(conn)
        t.add_server_key(self.key)
        self.transports.append(t)
        srv = _Server(self)
        try:
            t.start_server(server=srv)
        except Exception:
            return
        while t.is_active():
            chan = t.accept(2)
            if chan is None:
                continue
            if not srv.ready.wait(5):
                chan.close()
                continue
            cmd, srv.command = srv.command, None
            srv.ready.clear()
            self.commands.append(cmd)
            out, err, code = self.respond(cmd)
            try:
                if out:
                    chan.sendall(out.encode())
                if err:
                    chan.sendall_stderr(err.encode())
                chan.send_exit_status(code)
            except Exception:
                pass
            chan.close()

    def stop(self):
        self.running = False
        for t in self.transports:
            try:
                t.close()
            except Exception:
                pass
        try:
            self.sock.close()
        except OSError:
            pass
