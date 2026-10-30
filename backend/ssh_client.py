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
  * Shell features that chain, redirect or expand (; && || & > < ` $( ) { } ${ } $'')
    are never treated as read-only. Pipes are fine when every stage is read-only.
  * Each command's options are checked the way the program itself reads them
    (glued short options, abbreviated long options), so "sort -o FILE" and
    "ip a a ADDR" are recognised as writes. curl always asks first.
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
# first word -> None (harmless whatever its arguments) or a function(args) -> True if read-only.
#
# How options are read: GNU tools accept "-o file", "-ofile" and "-xyo file" for the same
# option, and long options may be abbreviated ("--out" means "--output"). So a short option
# letter counts wherever it appears in a "-..." word (a value glued to another option can
# trip this too, which only means the command asks for approval), and a long option counts
# when it is a prefix of a forbidden one. Anything the rules can't vouch for asks first.
def _opts(args):
    """-> (short option letters, long option names, positional words). '--' ends options."""
    short, long_, pos, ended = set(), set(), [], False
    for a in args:
        if ended or a == "-" or not a.startswith("-"):
            pos.append(a)
        elif a == "--":
            ended = True
        elif a.startswith("--"):
            long_.add(a.split("=", 1)[0])
        else:
            short.update(a[1:])
    return short, long_, pos


def _long_hit(given, bad):
    """GNU getopt_long accepts any unambiguous prefix, so '--out' is '--output'."""
    return any(b == g or (len(g) >= 4 and b.startswith(g)) for g in given for b in bad)


def _deny(short="", long=(), max_pos=None):
    bad_short, bad_long = set(short), tuple(long)

    def rule(args):
        s, l, pos = _opts(args)
        if s & bad_short or _long_hit(l, bad_long):
            return False
        return max_pos is None or len(pos) <= max_pos
    return rule


def _no_words(*bad):
    bad = set(bad)
    return lambda args: not any(a in bad for a in args)


def _second_in(*ok):
    ok = set(ok)
    return lambda args: bool(args) and args[0] in ok


def _needs(flag):
    return lambda args: flag in args


def _all(*rules):
    return lambda args: all(r(args) for r in rules)


def _count_given(args):
    return any(a == "-c" or (a.startswith("-c") and a[2:].isdigit()) for a in args)


def _date(args):
    # "date 010100002030" sets the clock; only "+FORMAT" may follow the options.
    s, l, pos = _opts(args)
    return not ("s" in s or _long_hit(l, ("--set",))) and all(p.startswith("+") for p in pos)


_IPROUTE_READ = {"show", "list", "ls", "lst", "get"}
_IPROUTE_VALUE_OPTS = {"-f", "-family", "-n", "-netns", "-l", "-loops", "-rc", "-rcvbuf"}


def _iproute(args):
    """ip / bridge: OBJECT [show|list|get ...]. iproute2 accepts abbreviated commands
    ("ip a a" is "ip addr add") and "-batch FILE", so only these forms are read-only."""
    words, skip = [], False
    for a in args:
        if skip:
            skip = False
            continue
        if a.startswith("-"):
            o = a[1:] if a.startswith("--") else a
            if o.startswith("-b") or o.startswith("-fo"):       # -batch FILE, -force
                return False
            if o in _IPROUTE_VALUE_OPTS:
                skip = True
            continue
        words.append(a)
    return len(words) <= 1 or words[1] in _IPROUTE_READ


_ETHTOOL_READ = {"-i", "-S", "-k", "-a", "-g", "-c", "-l", "-m", "-P", "-T", "-d", "-e", "-x", "-u", "-n",
                 "--driver", "--statistics", "--module-info", "--register-dump", "--eeprom-dump",
                 "--phy-statistics"}


def _ethtool(args):
    opts = [a for a in args if a.startswith("-")]
    pos = [a for a in args if not a.startswith("-")]
    return len(opts) <= 1 and all(o in _ETHTOOL_READ or o.startswith("--show-") for o in opts) and len(pos) <= 1


_IPTABLES_WRITE = _deny("AIDRFXNPZEM", ("--append", "--insert", "--delete", "--replace", "--flush",
                                        "--delete-chain", "--new-chain", "--policy", "--zero", "--rename-chain",
                                        "--modprobe"))

_READ_ONLY = {
    # files and text
    "cat": None, "head": None, "zcat": None, "wc": None, "cut": None,
    "tr": None, "column": None, "strings": None, "md5sum": None, "sha256sum": None,
    "echo": None, "printf": None, "true": None, "basename": None, "dirname": None, "realpath": None,
    "sort": _deny("oT", ("--output", "--temporary-directory", "--compress-program")),
    "uniq": _deny(max_pos=1),                                  # a second file name is where it WRITES
    "file": _deny("C", ("--compile",)),
    "tail": _deny("fF", ("--follow", "--retry")),
    "grep": None, "egrep": None, "fgrep": None, "zgrep": None,
    "ls": None, "stat": None, "du": None, "df": None, "readlink": None,
    "tree": _deny("o"),
    "find": _no_words("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"),
    # system
    "uptime": None, "w": None, "who": None, "id": None, "uname": None, "free": None,
    "hostname": _deny("Fb", ("--file", "--boot"), max_pos=0),  # "hostname NAME" renames the box
    "ps": None, "pgrep": None, "lsof": None, "lsblk": None, "lscpu": None, "lsusb": None, "lspci": None,
    "nproc": None, "vmstat": None, "iostat": None, "mpstat": None, "last": None,
    "lastlog": _deny("CS", ("--clear", "--set")),
    "date": _date,
    "top": _needs("-b"),
    "dmesg": _deny("cCnDE", ("--clear", "--read-clear", "--console-level", "--console-off", "--console-on")),
    "journalctl": _deny("", ("--vacuum-size", "--vacuum-time", "--vacuum-files", "--rotate", "--flush", "--sync",
                             "--relinquish-var", "--smart-relinquish-var", "--setup-keys", "--update-catalog",
                             "--cursor-file")),
    "systemctl": _second_in("status", "list-units", "list-unit-files", "list-timers", "list-dependencies",
                            "is-active", "is-enabled", "is-failed", "show", "cat"),
    "service": lambda args: len(args) == 2 and args[1] == "status",
    "docker": lambda args: bool(args) and (args[0] in ("ps", "logs", "inspect", "images", "version", "info",
                                                       "top", "port", "diff")
                                           or (args[0] == "stats" and "--no-stream" in args)),
    "podman": _second_in("ps", "logs", "inspect", "images", "version", "info", "top"),
    # network
    "ip": _iproute,
    "bridge": _iproute,
    "ifconfig": lambda args: len([a for a in args if not a.startswith("-")]) <= 1 and
                             not any(a in ("up", "down", "add", "del", "mtu", "netmask", "hw", "promisc") for a in args),
    "ss": _deny("KD", ("--kill", "--diag")),
    "netstat": None, "nstat": None,
    "arp": _deny("sdf", ("--set", "--delete", "--file")),
    "route": _no_words("add", "del", "delete", "flush", "change"),
    "ping": _count_given,
    "ping6": _count_given,
    "traceroute": None, "tracepath": None, "nslookup": None, "dig": None, "host": None,
    "mtr": lambda args: "-r" in args or "--report" in args,
    "iptables": _IPTABLES_WRITE,
    "ip6tables": _IPTABLES_WRITE,
    "nft": lambda args: bool(args) and args[0] == "list" and not any(a.startswith("-") for a in args),
    "ethtool": _ethtool,
    "brctl": _second_in("show", "showmacs", "showstp"),
    "swconfig": lambda args: "show" in args and "set" not in args and "load" not in args,
    "tcpdump": _all(_count_given, _deny("wzCG")),            # -w writes a file, -z runs a program
    # curl is not here: too many of its options write files or upload data (-o, -D, -c, -T, -d @file,
    # -H @file, --trace...). A curl command waits for approval like any other change.
    # UniFi OS / UniFi device tools (only clearly informational sub-commands)
    "info": _deny("o", ("--output", "--dribble")),
    "ubnt-device-info": None,
    "mca-cli-op": _second_in("info"),
    "ubnt-systool": _second_in("cputemp", "hwaddr", "serialnum", "board", "shortname", "id", "boardname"),
    "ubnt-tools": _second_in("id"),
    "mca-dump": None, "mca-status": None,
}

_SHELL_PUNCT = {";", "&", "&&", "||", "(", ")", "<", ">", ">>", "<<", ">&", "<&", "|&"}
_WRAPPERS = ("sudo", "nice", "ionice", "time")
def _expands(command):
    """True if the shell would expand part of the command into new words: brace expansion
    ("{-o,/tmp/x}"), ANSI-C or locale quoting ($'-o', $"-o"), or ${...}. Text inside single
    quotes is literal ('end$', '{' in a grep pattern), so it doesn't count."""
    quote, i = None, -1
    while i + 1 < len(command):
        i += 1
        ch = command[i]
        if quote == "'":
            if ch == "'":
                quote = None
        elif ch == "\\":
            i += 1                      # the next character is literal
        elif ch == "'" and quote is None:
            quote = "'"
        elif ch == '"':
            quote = None if quote == '"' else '"'
        elif ch == "$" and command[i + 1:i + 2] in ("'", '"', "{"):
            return True
        elif ch in "{}" and quote is None:
            return True
    return False


def _tokens(command):
    lex = shlex.shlex(command.replace(" 2>&1", " "), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    return list(lex)


def is_read_only(command):
    """True only if every pipeline stage is a known read-only command with no
    state-changing options and no shell chaining/redirection/expansion. Anything we
    can't classify is NOT read-only (it then needs the user's approval)."""
    if not isinstance(command, str) or not command.strip() or len(command) > MAX_COMMAND_CHARS:
        return False
    if "\n" in command or "\r" in command or "`" in command or "$(" in command:
        return False
    if _expands(command):
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
        if "=" in name or "$" in name:      # VAR=value cmd, or a name built from a variable
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


def test_ssh(profile, password, accept_hostkey=None):
    """Connect once, capture the host key, run a harmless command. Never raises for
    expected failures - returns a dict for the UI, like unifi_client.test_connection.

    A saved host key stays pinned: a different key is reported (kind "hostkey_changed")
    during the key exchange, before the password is sent. Called again with the key the
    user accepted (accept_hostkey), it logs in only if the console presents that key."""
    pinned = profile.get("ssh_hostkey")
    c = SSHClient(profile["host"], profile.get("ssh_port") or DEFAULT_PORT,
                  profile.get("ssh_username") or "root", password,
                  expected_hostkey=accept_hostkey or pinned or None)
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
        if e.kind == "hostkey" and e.hostkey:
            return {"ok": False, "kind": "hostkey_changed", "hostkey": e.hostkey,
                    "hostkey_display": format_hostkey(e.hostkey), "pinned": pinned,
                    "error": "The console presented a DIFFERENT SSH host key from the one "
                             + ("you accepted" if accept_hostkey else "saved for this connection")
                             + ". The password was not sent. Trust the new key only if you know the "
                             "console was reset or replaced."}
        return {"ok": False, "kind": e.kind, "error": str(e)}
    finally:
        c.close()
