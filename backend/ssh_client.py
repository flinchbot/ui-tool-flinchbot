"""
SSH access to the UniFi console (optional, off by default, per connection).

Why: the Network API doesn't expose everything (logs, processes, disk, config
files on the box). With SSH enabled for a connection the AI gets one extra
tool, ssh_command. Read-only commands run straight away; anything that could
change the box goes through the same Approve/Deny card as the API write tools.
The decision is made HERE (is_read_only), not by the model.

Security
  * The host key is pinned the first time you press "Test SSH", like the TLS
    certificate. A different key later stops the connection with a clear error.
  * The password lives in the OS password store (Windows Credential Manager or
    the macOS Keychain; data/secrets.json as the fallback), separate from the
    UniFi login.
  * Command output is sent to the AI as-is: nothing in free text can be
    redacted reliably. The UI says so where you turn SSH on.
  * Shell features that chain or redirect (; && || & > < ` $( ) are never
    treated as read-only. Pipes are fine when every stage is read-only.
"""

import base64
import hashlib
import shlex
import socket
import threading
import time

try:
    import paramiko
except ImportError:          # the app still works without SSH support
    paramiko = None

DEFAULT_PORT = 22
CONNECT_TIMEOUT = 15
COMMAND_TIMEOUT = 60
MAX_OUTPUT_BYTES = 400_000      # stop reading after this much; the model gets a trimmed view
MAX_OUTPUT_CHARS = 20_000       # per stream, in the tool result
MAX_COMMAND_CHARS = 2_000


class SSHError(Exception):
    def __init__(self, message, kind="ssh", hostkey=None):
        super().__init__(message)
        self.kind = kind
        self.hostkey = hostkey


class _HostKeyMismatch(Exception):
    def __init__(self, seen):
        super().__init__("host key mismatch")
        self.seen = seen


def hostkey_fingerprint(key):
    return hashlib.sha256(key.asbytes()).hexdigest()


def format_hostkey(fp_hex):
    """OpenSSH style: SHA256:<base64 without padding>."""
    if not fp_hex:
        return ""
    return "SHA256:" + base64.b64encode(bytes.fromhex(fp_hex)).decode().rstrip("=")


# ── read-only command classifier ─────────────────────────────────────────────
# first word -> None (always read-only) or a function(args) -> True if read-only.
def _no_flags(*bad):
    bad = set(bad)
    return lambda args: not any(a in bad or any(a.startswith(b + "=") for b in bad) for a in args)


def _no_words(*bad):
    bad = set(bad)
    return lambda args: not any(a in bad for a in args)


def _second_in(*ok):
    ok = set(ok)
    return lambda args: bool(args) and args[0] in ok


def _needs(flag):
    return lambda args: flag in args


_READ_ONLY = {
    # files and text
    "cat": None, "head": None, "zcat": None, "wc": None, "sort": None, "uniq": None, "cut": None,
    "tr": None, "column": None, "strings": None, "file": None, "md5sum": None, "sha256sum": None,
    "echo": None, "printf": None, "true": None, "basename": None, "dirname": None, "realpath": None,
    "tail": _no_flags("-f", "-F", "--follow"),
    "grep": None, "egrep": None, "fgrep": None, "zgrep": None,
    "ls": None, "stat": None, "du": None, "df": None, "readlink": None, "tree": None,
    "find": _no_words("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"),
    # system
    "uptime": None, "w": None, "who": None, "id": None, "hostname": None, "uname": None, "free": None,
    "ps": None, "pgrep": None, "lsof": None, "lsblk": None, "lscpu": None, "lsusb": None, "lspci": None,
    "nproc": None, "vmstat": None, "iostat": None, "mpstat": None, "last": None, "lastlog": None,
    "date": _no_flags("-s", "--set"),
    "top": _needs("-b"),
    "dmesg": _no_flags("-c", "-C", "--clear", "-n", "--console-level"),
    "journalctl": _no_flags("--vacuum-size", "--vacuum-time", "--vacuum-files", "--rotate", "--flush", "--sync"),
    "systemctl": _second_in("status", "list-units", "list-unit-files", "list-timers", "list-dependencies",
                            "is-active", "is-enabled", "is-failed", "show", "cat"),
    "service": lambda args: len(args) >= 2 and args[1] == "status",
    "docker": lambda args: bool(args) and (args[0] in ("ps", "logs", "inspect", "images", "version", "info",
                                                       "top", "port", "diff")
                                           or (args[0] == "stats" and "--no-stream" in args)),
    "podman": _second_in("ps", "logs", "inspect", "images", "version", "info", "top"),
    # network
    "ip": _no_words("add", "del", "delete", "set", "flush", "change", "replace", "append", "up", "down", "exec"),
    "ifconfig": lambda args: len([a for a in args if not a.startswith("-")]) <= 1 and
                             not any(a in ("up", "down", "add", "del", "mtu", "netmask", "hw", "promisc") for a in args),
    "ss": None, "netstat": None, "nstat": None,
    "arp": _no_flags("-s", "-d", "-f"),
    "route": _no_words("add", "del", "delete", "flush", "change"),
    "ping": lambda args: "-c" in args or any(a.startswith("-c") for a in args),
    "ping6": lambda args: "-c" in args or any(a.startswith("-c") for a in args),
    "traceroute": None, "tracepath": None, "nslookup": None, "dig": None, "host": None,
    "mtr": lambda args: "-r" in args or "--report" in args,
    "iptables": _no_flags("-A", "-I", "-D", "-R", "-F", "-X", "-N", "-P", "-Z", "-E", "--append", "--insert",
                          "--delete", "--replace", "--flush", "--delete-chain", "--new-chain", "--policy",
                          "--zero", "--rename-chain"),
    "ip6tables": _no_flags("-A", "-I", "-D", "-R", "-F", "-X", "-N", "-P", "-Z", "-E", "--append", "--insert",
                           "--delete", "--replace", "--flush", "--delete-chain", "--new-chain", "--policy",
                           "--zero", "--rename-chain"),
    "nft": _second_in("list"),
    "ethtool": lambda args: not any(a in ("-s", "-K", "-A", "-G", "-C", "-r", "-E", "-f", "-p", "-L", "-X",
                                           "--change", "--set-channels", "--reset", "--flash") or
                                    a.startswith("--set") or a.startswith("--change") for a in args),
    "brctl": _second_in("show", "showmacs", "showstp"),
    "bridge": _no_words("add", "del", "delete", "set", "replace", "change", "flush"),
    "swconfig": lambda args: "show" in args and "set" not in args and "load" not in args,
    "tcpdump": lambda args: "-w" not in args and ("-c" in args or any(a.startswith("-c") for a in args)),
    "curl": _no_flags("-X", "--request", "-d", "--data", "--data-raw", "--data-binary", "--data-urlencode",
                      "-F", "--form", "-T", "--upload-file", "-o", "--output", "-O", "--remote-name",
                      "-K", "--config", "--remote-name-all"),
    # UniFi OS / UniFi device tools (only clearly informational sub-commands)
    "info": None, "ubnt-device-info": None,
    "mca-cli-op": _second_in("info"),
    "ubnt-systool": _second_in("cputemp", "hwaddr", "serialnum", "board", "shortname", "id", "boardname"),
    "ubnt-tools": _second_in("id"),
    "mca-dump": None, "mca-status": None,
}

_SHELL_PUNCT = {";", "&", "&&", "||", "(", ")", "<", ">", ">>", "<<", ">&", "<&", "|&"}
_WRAPPERS = ("sudo", "nice", "ionice", "time")


def _tokens(command):
    lex = shlex.shlex(command.replace(" 2>&1", " "), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    return list(lex)


def is_read_only(command):
    """True only if every pipeline stage is a known read-only command with no
    state-changing options and no shell chaining/redirection. Anything we can't
    classify is NOT read-only (it then needs the user's approval)."""
    if not isinstance(command, str) or not command.strip() or len(command) > MAX_COMMAND_CHARS:
        return False
    if "\n" in command or "\r" in command or "`" in command or "$(" in command:
        return False
    try:
        toks = _tokens(command)
    except ValueError:              # unbalanced quotes etc.
        return False
    stages, cur = [], []
    for t in toks:
        if t == "|":
            stages.append(cur)
            cur = []
        elif t in _SHELL_PUNCT or any(c in t for c in "();<>&"):
            return False
        else:
            cur.append(t)
    stages.append(cur)
    for stage in stages:
        while stage and stage[0] in _WRAPPERS:
            stage = stage[1:]
        if stage and stage[0] == "timeout" and len(stage) > 2:
            stage = stage[2:]
        if not stage:
            return False
        name = stage[0].rsplit("/", 1)[-1]
        if "=" in name:                 # VAR=value cmd: don't try to be clever
            return False
        if name not in _READ_ONLY:
            return False
        rule = _READ_ONLY[name]
        if rule is not None and not rule(stage[1:]):
            return False
    return True


# ── client ────────────────────────────────────────────────────────────────────
class SSHClient:
    """One SSH connection to a console, reconnected on demand. Thread-safe (one
    command at a time)."""

    def __init__(self, host, port, username, password, expected_hostkey=None, timeout=CONNECT_TIMEOUT):
        if paramiko is None:
            raise SSHError("The 'paramiko' package isn't installed. Close the app and start it again "
                           "(start.bat on Windows, start.command on a Mac) to install it.", "setup")
        self.host, self.port = host, int(port or DEFAULT_PORT)
        self.username, self.password = username, password or ""
        self.expected_hostkey = expected_hostkey or None
        self.timeout = timeout
        self.seen_hostkeys = []
        self._client = None
        self._lock = threading.RLock()

    @property
    def last_hostkey(self):
        return self.seen_hostkeys[-1] if self.seen_hostkeys else None

    def _policy(self):
        seen, expected = self.seen_hostkeys, self.expected_hostkey

        class Pin(paramiko.MissingHostKeyPolicy):
            def missing_host_key(self, client, hostname, key):
                fp = hostkey_fingerprint(key)
                seen.append(fp)
                if expected and fp != expected:
                    raise _HostKeyMismatch(fp)
        return Pin()

    def connect(self):
        with self._lock:
            self.close()
            c = paramiko.SSHClient()
            c.set_missing_host_key_policy(self._policy())      # nothing pre-trusted: the pin decides
            try:
                c.connect(self.host, port=self.port, username=self.username, password=self.password,
                          timeout=self.timeout, banner_timeout=self.timeout, auth_timeout=self.timeout,
                          allow_agent=False, look_for_keys=False)
            except _HostKeyMismatch as e:
                raise SSHError(
                    "The console at this address presented a DIFFERENT SSH host key than the one "
                    "saved for this connection. Either the console was reset/replaced, or this "
                    "address now belongs to a different device. Open the connection, press "
                    "'Test SSH' and save only if you're sure.", "hostkey", hostkey=e.seen)
            except paramiko.AuthenticationException:
                raise SSHError(
                    f"SSH login as '{self.username}' was rejected. On a UniFi OS console the user "
                    f"is 'root' and the password is set under Console Settings → SSH; on a "
                    f"self-hosted controller use the server's own login.", "auth")
            except paramiko.SSHException as e:
                raise SSHError(f"SSH to {self.host}:{self.port} failed: {e}", "unreachable")
            except (socket.timeout, TimeoutError):
                raise SSHError(f"Timed out reaching {self.host}:{self.port} over SSH. Is SSH enabled "
                               f"on the console and is this PC on that network?", "unreachable")
            except (ConnectionRefusedError, OSError) as e:
                raise SSHError(f"Can't reach {self.host}:{self.port} over SSH: "
                               f"{getattr(e, 'strerror', None) or e}", "unreachable")
            self._client = c
            return c

    def close(self):
        with self._lock:
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:
                    pass
                self._client = None

    def _alive(self):
        t = self._client.get_transport() if self._client else None
        return bool(t and t.is_active())

    def run(self, command, timeout=COMMAND_TIMEOUT):
        """Run one command; returns exit_code / stdout / stderr / truncated.
        Does NOT decide whether the command is safe - callers use is_read_only()."""
        if not isinstance(command, str) or not command.strip():
            raise SSHError("Empty command", "input")
        if len(command) > MAX_COMMAND_CHARS:
            raise SSHError(f"Command too long ({len(command)} characters, max {MAX_COMMAND_CHARS})", "input")
        with self._lock:
            if not self._alive():
                self.connect()
            try:
                chan = self._client.get_transport().open_session(timeout=self.timeout)
            except Exception:
                self.connect()       # stale connection: one reconnect
                chan = self._client.get_transport().open_session(timeout=self.timeout)
            out, err, truncated, timed_out = bytearray(), bytearray(), False, False
            try:
                chan.settimeout(timeout)
                chan.exec_command(command)
                chan.shutdown_write()                   # no stdin: 'cat' with no file ends at once
                deadline = time.time() + timeout
                while True:
                    got = False
                    while chan.recv_ready():
                        out += chan.recv(65536); got = True
                    while chan.recv_stderr_ready():
                        err += chan.recv_stderr(65536); got = True
                    if len(out) + len(err) > MAX_OUTPUT_BYTES:
                        truncated = True
                        break
                    if chan.exit_status_ready() and not got:
                        break
                    if time.time() > deadline:
                        timed_out = True
                        break
                    if not got:
                        time.sleep(0.05)
                exit_code = None if (truncated or timed_out) else chan.recv_exit_status()
            finally:
                chan.close()
        if timed_out:
            raise SSHError(f"The command did not finish within {timeout} seconds and was stopped. "
                           f"Avoid commands that wait for input or run forever (use -c with ping, "
                           f"-b with top, no -f with tail).", "timeout")
        return {"exit_code": exit_code, "stdout": _clip(out), "stderr": _clip(err), "truncated": truncated}


def _clip(buf):
    text = bytes(buf).decode("utf-8", errors="replace")
    if len(text) > MAX_OUTPUT_CHARS:
        omitted = len(text) - MAX_OUTPUT_CHARS
        return text[:MAX_OUTPUT_CHARS] + f"\n...[{omitted:,} more characters not shown: use grep/head/tail]"
    return text


def test_ssh(profile, password):
    """Connect once, capture the host key, run a harmless command. Never raises for
    expected failures - returns a dict for the UI, like unifi_client.test_connection."""
    pinned = profile.get("ssh_hostkey")
    c = SSHClient(profile["host"], profile.get("ssh_port") or DEFAULT_PORT,
                  profile.get("ssh_username") or "root", password, expected_hostkey=None)
    try:
        c.connect()
        try:
            r = c.run("uname -a", timeout=15)
            banner = (r["stdout"] or r["stderr"]).strip().splitlines()[:1]
        except SSHError:
            banner = []
        fp = c.last_hostkey
        return {"ok": True, "hostkey": fp, "hostkey_display": format_hostkey(fp),
                "hostkey_changed": bool(pinned and fp and fp != pinned),
                "uname": banner[0] if banner else ""}
    except SSHError as e:
        return {"ok": False, "kind": e.kind, "error": str(e)}
    finally:
        c.close()
