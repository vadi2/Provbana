"""Deliver FHIR Subscription notifications to the companies that asked for them.

    .venv/bin/python notify.py --loop       # alongside the service
    .venv/bin/python notify.py --once       # deliver what is due, report, exit
    .venv/bin/python notify.py --status     # subscriptions, and what is queued

A company's platform subscribes (POST /fhir/Subscription, in app.py) and is told
whenever a document passes validation, so it can pull each new one in without
anybody polling $exchange. These are topic-based subscriptions as the
Subscriptions R5 Backport IG defines them for R4: the notification is a
`history` Bundle whose first entry is a Parameters resource carrying the
subscription's status and the event, POSTed to the subscriber's rest-hook.

The service only ever *queues*. Accepting a submission inserts a row per
matching subscription into `notifications`, in the same transaction as the
submission itself, and returns; this process drains that table. Two reasons it
is a process of its own rather than a thread in the service:

  * A submission must never wait on, or fail because of, some other company's
    endpoint. A queue in the database also survives a restart of either side,
    which a queue in memory does not.
  * It is the one part of the system that opens connections to addresses a
    participant chose. In docker-compose.prod.yml it sits on a network of its
    own, on which Matchbox and the service do not exist - the only way back to
    them is the public front door, as for anybody - so the address vetting
    below is the first line of defence and not the only one.

On that vetting. An endpoint is a URL a participant typed, and the box that
calls it sits beside an unauthenticated validator and, on a cloud host, a
metadata service on a link-local address. So the hostname is resolved here,
every address it resolves to has to be a public one, and the connection is then
made to the address that was vetted rather than to the name - resolving twice is
how a DNS answer that changes between the check and the connect gets through.
Redirects are not followed, for the same reason: a 302 is a second URL nobody
vetted. SUBSCRIPTION_ALLOW_PRIVATE=1 lifts the public-only rule for a venue LAN,
where the participants' laptops *are* private addresses; link-local, multicast,
unspecified and the metadata addresses in NEVER stay refused either way, and so
does any of them inside one of the IPv6 wrappers _embedded knows.

Delivery is in order per subscription - only a subscription's oldest pending
notification is ever attempted - and retried on BACKOFF. The first failure puts
an active subscription in `error`, which means "having trouble, still trying"
(a handshake that fails leaves it `requested`, with the reason in
Subscription.error); a success puts it back to `active`. A notification that
runs out of attempts turns the subscription `off`, which means "gave up":
nothing more is queued for it until its owner PUTs it again, which starts over
with a fresh handshake. That is for what the *endpoint* gets wrong. What this
server gets wrong is not held against the subscription: if it may pass - a
resolver that is not answering, a volume that will not read - it is tried again
on TRANSIENT_BACKOFF, and when that runs out, or at once for a document that is
simply gone, the one notification is written off and costs the subscriber a gap
in its event numbers. Except a handshake, which has no next notification to
move on to: that subscription goes `off`, with a note to PUT it again.

It is at-least-once. Each result is written down before the next notification
is sent, and a result that cannot be written is remembered rather than sent
again. A duplicate still happens two ways: an endpoint that took the
notification and then answered late, or not with a 2xx, is sent it again on
the retry, and so is one whose result a restart lost between the POST and its
write. event-number is what a subscriber tells one by. The owner may PUT or
DELETE a subscription at any moment, including while something is in flight to
it: a result only counts if the row it was for is still there as it was
(_record). One notifier per data directory (_claim): two would each send what
the other has in flight.

Stdlib only, like backup.py and reset.py. app.py imports this module for the
endpoint check and the shape of a status, so neither can drift from what is
actually sent.
"""

import argparse
import errno
import http.client
import ipaddress
import json
import os
import socket
import sqlite3
import ssl
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
try:
    import fcntl
except ImportError:          # not a Unix: no lock, and nothing else changes
    fcntl = None
from concurrent.futures import ThreadPoolExecutor
from typing import NamedTuple
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA = os.environ.get("DATA_DIR") or os.path.join(HERE, "data")
DB_NAME = "connectathon.db"

BACKPORT = "http://hl7.org/fhir/uv/subscriptions-backport/StructureDefinition/"
SUBSCRIPTION_PROFILE = BACKPORT + "backport-subscription"
NOTIFICATION_PROFILE = BACKPORT + "backport-subscription-notification-r4"
STATUS_PROFILE = BACKPORT + "backport-subscription-status-r4"
FILTER_CRITERIA_EXT = BACKPORT + "backport-filter-criteria"
PAYLOAD_CONTENT_EXT = BACKPORT + "backport-payload-content"

FHIR_JSON = "application/fhir+json"

# How often the queue is looked at. A query against a local file, so this is
# the latency a subscriber sees and not a cost worth economising on. Floored:
# NOTIFY_POLL=0 would be a loop that never sleeps.
POLL = max(0.1, float(os.environ.get("NOTIFY_POLL", "1")))
# Per attempt, on the wall clock: resolving, connecting, sending and the answer
# together. A rest-hook is meant to take the notification and answer; an
# endpoint that does its ingesting before it answers is holding a worker, and
# ten seconds is already generous for saying "got it". A socket timeout alone
# is not this - it restarts with every byte, so an endpoint that drips its
# answer a byte at a time would hold a worker for as long as it cared to. See
# post().
TIMEOUT = max(0.1, float(os.environ.get("NOTIFY_TIMEOUT", "10")))
# Seconds to wait after the first, second, ... failed attempt. One more attempt
# than there are entries, so an event is tried six times over about 43 minutes -
# long enough to ride out a laptop that went to lunch, short enough that a
# tunnel URL nobody will ever bring back stops being called the same afternoon.
BACKOFF = (5, 30, 120, 600, 1800)
# A handshake is somebody at a keyboard waiting to hear whether the URL they
# just typed works. Three tries, 35 seconds of waiting between them, then no.
HANDSHAKE_BACKOFF = (5, 30)
# For what is this server's trouble and may pass - a resolver that is not
# answering, a volume that cannot be read just now. Counted apart from the
# endpoint's failures and not held against the subscription; four tries over
# two and a half minutes, and then that one notification is written off. A
# handshake excepted: with nothing to move on to, that subscription goes `off`
# (see _record).
TRANSIENT_BACKOFF = (5, 30, 120)
# Endpoints called at once. Each subscription is one task, and the loop does
# not wait for a pass to finish before it starts the next: a subscription still
# being worked on is skipped, and everybody else's new events go out on time -
# for as long as there is a worker free, which is what DRAIN_BUDGET is about.
WORKERS = 16
# Of which so many may be holding a whole document. A full-resource
# notification is the document three times over while it is being put together
# - parsed, wrapped, serialised - and at the 5 MB the service accepts that is
# some 40 MB a worker: sixteen at once were measured at 626 MB, in a container
# that is allowed 512. The rest wait their turn, for at most a few TIMEOUTs.
WHOLE_DOCUMENTS = 4
_WHOLE_DOCUMENTS = threading.BoundedSemaphore(WHOLE_DOCUMENTS)
# After how long no further notification of one subscription's queue is
# started, and the rest is left for the next look (so a drain can overrun this
# by one attempt: a TIMEOUT, plus for a full-resource notification the wait
# for a WHOLE_DOCUMENTS slot, plus the database's busy timeout for writing the
# result down). An endpoint that answers, but slowly, with a backlog
# behind it, would otherwise sit on a worker for the whole backlog - and
# sixteen of those are the whole pool.
DRAIN_BUDGET = 2 * TIMEOUT
# Longest the loop backs off to when passes keep failing (a locked database, a
# full disk), so the log says so every half minute and not every second.
MAX_LOOP_BACKOFF = 30
# How often the loop repeats what it is waiting for, while it is waiting.
REMIND = 60
# What is read of a subscriber's answer before it is thrown away. Nothing in it
# is used; reading some of it is only what lets the connection close cleanly.
MAX_RESPONSE_BYTES = 64 * 1024
MAX_ENDPOINT_CHARS = 2000
# What is kept of an error. It ends up in Subscription.error, which the owner
# reads, so it should be a sentence and not a stack of HTML from a proxy.
MAX_ERROR_CHARS = 300


def allow_private() -> bool:
    return os.environ.get("SUBSCRIPTION_ALLOW_PRIVATE", "").strip().lower() in (
        "1", "true", "yes", "on")


def log(msg: str) -> None:
    print("%s notify: %s" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg),
          flush=True)


# ---------------------------------------------------------------- endpoints


class EndpointError(ValueError):
    """The endpoint is not one this server will call. The message is written
    for the participant who typed the URL."""


class Unresolved(EndpointError):
    """The name could not be looked up just now. Not "no such name" - the
    resolver did not give an answer at all, and the likeliest resolver to be
    broken is the one this box was given."""


NOT_THE_NAMES_FAULT = (socket.EAI_AGAIN, socket.EAI_FAIL, socket.EAI_SYSTEM)


def parse_endpoint(url) -> urllib.parse.SplitResult:
    if not isinstance(url, str) or not url.strip():
        raise EndpointError("channel.endpoint must be the URL to notify")
    # Printable ASCII and nothing else. http.client refuses a space or a
    # control character anyway, but it refuses at delivery, with a message
    # that quotes the path back - and the path is where people put the secret
    # when they cannot set a header - and a path past ASCII with an error that
    # reads as a bug of ours. A name or a path outside ASCII has a spelling
    # inside it (punycode, percent-encoding), which is the one that goes on
    # the wire.
    if len(url) > MAX_ENDPOINT_CHARS or not all("!" <= c <= "~" for c in url):
        raise EndpointError(
            "channel.endpoint is not a usable URL - printable ASCII only, so "
            "percent-encode the path and write an international host name in "
            "its xn-- form")
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        raise EndpointError("channel.endpoint is not a usable URL")
    if parts.scheme not in ("http", "https"):
        raise EndpointError("channel.endpoint must be an http or https URL")
    if not parts.hostname or not _host(parts):
        raise EndpointError("channel.endpoint has no host")
    # Credentials belong in channel.header, which is not echoed into logs the
    # way a URL is - and user@host is the classic way to make a URL look like
    # it points somewhere it does not.
    if parts.username is not None or parts.password is not None:
        raise EndpointError(
            "channel.endpoint must not carry credentials - use channel.header")
    if port == 0:
        raise EndpointError("channel.endpoint has an unusable port")
    return parts


# Metadata and host-agent services that are *not* on a link-local address, so
# that neither rule below would catch them: Azure's is a public address, and
# the others are non-public ones (private or CGNAT, to ipaddress) that
# SUBSCRIPTION_ALLOW_PRIVATE would let through.
NEVER = tuple(ipaddress.ip_network(net) for net in (
    "168.63.129.16/32",     # Azure wire server
    "100.100.100.200/32",   # Alibaba Cloud metadata
    "192.0.0.192/32",       # Oracle Cloud metadata
    "fd00:ec2::/32",        # AWS metadata and time sync, over IPv6
))
NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))
V4_COMPATIBLE = ipaddress.ip_network("::/96")
V4_TRANSLATED = ipaddress.ip_network("::ffff:0:0:0/96")     # RFC 2765 SIIT


def _embedded(ip) -> list:
    """The IPv4 addresses an IPv6 one is a wrapper around. 64:ff9b::7f00:1 is a
    global address as far as its own bits go, and is 127.0.0.1 to the NAT64
    gateway that routes it. The wrappers with a fixed shape, that is: a NAT64
    gateway on a prefix of its operator's choosing cannot be told from any
    other address, which is one more reason the notifier's network has nothing
    on it worth reaching."""
    if ip.version != 6:
        return []
    inner = [ip.ipv4_mapped, ip.sixtofour] + list(ip.teredo or ())
    low = ipaddress.ip_address(int(ip) & 0xFFFFFFFF)
    if (int(ip) >> 32) & 0xFFFFFFFF in (0x00005EFE, 0x02005EFE):    # ISATAP
        inner.append(low)
    if any(ip in net for net in NAT64) or ip in V4_TRANSLATED:
        inner.append(low)
    if ip in V4_COMPATIBLE and int(ip) > 1:
        inner.append(low)
    return [v4 for v4 in inner if v4 is not None]


def vet_address(text: str, private_ok: bool) -> None:
    """Refuse an address this server has no business calling."""
    outer = ipaddress.ip_address(text)
    for ip in [outer] + _embedded(outer):
        if (ip.is_link_local or ip.is_multicast or ip.is_unspecified
                or any(ip in net for net in NEVER)):
            # Link-local is where most cloud metadata services live, and NEVER
            # is the rest of them. Whatever the venue network looks like.
            raise EndpointError("channel.endpoint resolves to %s, which is not an "
                                "address this server will call" % outer)
        if not private_ok and not ip.is_global:
            raise EndpointError(
                "channel.endpoint resolves to %s, which is not a public address - "
                "this server only calls endpoints on the public internet" % outer)


def _ip_literal(host: str):
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    # 2130706433, 0x7f.1 and 127.1 are all 127.0.0.1 to the resolver. They are
    # vetted at delivery whatever happens here; saying so at the door is only
    # the kinder place to say it.
    try:
        return ipaddress.ip_address(socket.inet_aton(host))
    except (OSError, ValueError, UnicodeError):
        return None


def _host(parts: urllib.parse.SplitResult) -> str:
    """The host as it is vetted, resolved and connected to - one spelling for
    all three. `localhost.` is localhost, and an endpoint the door lets in as
    127.0.0.1 must not be one delivery then fails to resolve as "127.0.0.1."."""
    return parts.hostname.rstrip(".")


def check_endpoint(url, private_ok: bool | None = None) -> None:
    """Everything that can be said about an endpoint without touching the
    network, which is all the service itself is allowed to do with one."""
    private_ok = allow_private() if private_ok is None else private_ok
    parts = parse_endpoint(url)
    host = _host(parts)
    literal = _ip_literal(host)
    if literal is not None:
        vet_address(str(literal), private_ok)
    elif not private_ok and (host == "localhost" or host.endswith(".localhost")):
        raise EndpointError(
            "channel.endpoint is this server's own localhost, not yours - give "
            "an address this server can reach")


# getaddrinfo has no timeout of its own and cannot be cancelled, and a name
# server that never answers is as good a way of holding a worker as an endpoint
# that never does. So each lookup gets a thread of its own to be abandoned in:
# a fixed pool of them would be used up by as many names that hang, and every
# other subscriber's lookups would queue behind those for good. One per name at
# a time - whoever else asks meanwhile waits on the same answer - so a name
# that hangs costs one thread however often it is retried. Daemon threads, so
# that one still hanging does not keep --once from exiting.
class _Lookup:
    def __init__(self):
        self.done = threading.Event()
        self.infos = None
        self.error = None


_LOOKUPS: dict[tuple, _Lookup] = {}
_LOOKUPS_LOCK = threading.Lock()


def _look_up(key: tuple, lookup: _Lookup) -> None:
    try:
        lookup.infos = socket.getaddrinfo(*key, type=socket.SOCK_STREAM)
    except BaseException as exc:
        lookup.error = exc
    finally:
        with _LOOKUPS_LOCK:
            if _LOOKUPS.get(key) is lookup:
                del _LOOKUPS[key]
        lookup.done.set()


def _getaddrinfo(host: str, port: int, timeout: float) -> list:
    """socket.getaddrinfo, given up on after `timeout`: TimeoutError."""
    key = (host, port)
    with _LOOKUPS_LOCK:
        lookup = _LOOKUPS.get(key)
        if lookup is None:
            lookup = _Lookup()
            thread = threading.Thread(target=_look_up, args=(key, lookup),
                                      name="resolve %s" % host, daemon=True)
            thread.start()      # RuntimeError if it cannot: nothing to clean up
            _LOOKUPS[key] = lookup
    if not lookup.done.wait(timeout):
        raise TimeoutError
    if lookup.error is not None:
        raise lookup.error
    return lookup.infos


def resolve_endpoint(url, private_ok: bool, timeout: float = TIMEOUT
                     ) -> tuple[urllib.parse.SplitResult, str]:
    """(the parsed URL, the first vetted address to connect to)."""
    parts, addresses = resolve_addresses(url, private_ok, timeout)
    return parts, addresses[0]


def resolve_addresses(url, private_ok: bool, timeout: float = TIMEOUT
                      ) -> tuple[urllib.parse.SplitResult, list[str]]:
    """(the parsed URL, every address of the host, each vetted, in the order
    the resolver gave them - which is the order to try connecting in)."""
    parts = parse_endpoint(url)
    host = _host(parts)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    literal = _ip_literal(host)
    if literal is not None:
        vet_address(str(literal), private_ok)
        return parts, [str(literal)]
    try:
        infos = _getaddrinfo(host, port, timeout)
    except TimeoutError:
        raise Unresolved("channel.endpoint host %s could not be looked up within "
                         "%gs" % (host, timeout))
    except (socket.gaierror, UnicodeError) as exc:
        if isinstance(exc, socket.gaierror) and exc.errno in NOT_THE_NAMES_FAULT:
            raise Unresolved(
                "channel.endpoint host %s could not be looked up just now (%s) - "
                "a resolver's failure rather than a wrong name" % (host, exc))
        raise EndpointError("channel.endpoint host %s does not resolve (%s)"
                            % (host, exc))
    addresses = list(dict.fromkeys(info[4][0] for info in infos))
    if not addresses:
        raise EndpointError("channel.endpoint host %s does not resolve" % host)
    # Every one of them, not just the one about to be used: a name with a
    # public and a private record is not an accident.
    for address in addresses:
        vet_address(address.split("%", 1)[0], private_ok)
    return parts, addresses


# ---------------------------------------------------------------- delivery


class DeliveryError(Exception):
    """One attempt failed. The message goes into Subscription.error."""


class ServerTrouble(Exception):
    """One attempt could not be made, and the endpoint had no part in that.
    Retried on TRANSIENT_BACKOFF, and not held against the subscription - a
    handshake excepted, which turns it `off` when the tries run out."""


_TLS = ssl.create_default_context()


class _Pinned:
    """Connects to the addresses that were vetted, whatever the name says now.
    Host header and everything else still come from the URL.

    And can be cut: the socket shut down under whatever is blocked on it, from
    another thread. http.client cannot be asked to do that, because it lets go
    of its own reference (conn.sock = None) as soon as it has read the headers
    of an answer that closes the connection - HTTP/1.0, or Connection: close -
    while the body is still to be read. So the socket is kept here as well."""

    def __init__(self, addresses):
        self._addresses = list(addresses)
        self._held = None
        self._cut = False
        self.connected = False
        self._cutting = threading.Lock()

    def _connect_plain(self):
        """A socket to the first vetted address that takes the connection -
        what create_connection() does with a name, and what pinning would
        otherwise lose: a host with an IPv6 record this box cannot route to
        and an IPv4 one it can is reachable, not "Network is unreachable"."""
        errors = []
        for i, address in enumerate(self._addresses):
            if i and self._cut:
                # The first address took the whole deadline. A second connect
                # would get a socket timeout of its own on top of it.
                raise TimeoutError("the deadline passed before a connection was made")
            try:
                return socket.create_connection((address, self.port), self.timeout)
            except OSError as exc:
                errors.append(exc)
        # What the endpoint did, over what this box could not do. A dual-stack
        # name whose port is closed, called from a box with no IPv6 route, is
        # refused on the A record and unreachable on the AAAA - and the
        # resolver sorts the address that cannot be routed last, so the last
        # error alone would say "our routing" of every dead endpoint there is.
        theirs = [exc for exc in errors if exc.errno not in OUR_ERRNOS]
        raise (theirs or errors)[-1]

    def _hold(self, sock):
        with self._cutting:
            self._held = sock
            if self._cut:       # the deadline passed while it was being made
                _shut(sock)
        return sock

    def cut(self):
        with self._cutting:
            self._cut = True
            if self._held is not None:
                _shut(self._held)


def _shut(sock) -> None:
    try:
        # socket.socket's, also for an SSLSocket: its own shutdown() takes the
        # TLS state from under the thread that is using it first.
        socket.socket.shutdown(sock, socket.SHUT_RDWR)
    except OSError:
        pass


class _PinnedHTTPConnection(_Pinned, http.client.HTTPConnection):
    def __init__(self, host, port, addresses, timeout):
        http.client.HTTPConnection.__init__(self, host, port, timeout=timeout)
        _Pinned.__init__(self, addresses)

    def connect(self):
        self.sock = self._hold(self._connect_plain())
        self.connected = True


class _PinnedHTTPSConnection(_Pinned, http.client.HTTPSConnection):
    def __init__(self, host, port, addresses, timeout):
        http.client.HTTPSConnection.__init__(self, host, port, timeout=timeout,
                                             context=_TLS)
        _Pinned.__init__(self, addresses)

    def connect(self):
        plain = self._connect_plain()
        # Wrapped without the handshake and held before it: wrapping takes the
        # descriptor away from `plain`, so a cut aimed at that one during the
        # handshake would hit nothing, and the deadline would have been spent.
        # The certificate is checked against the name in the URL, so pinning
        # the address costs nothing in what TLS proves.
        try:
            self.sock = self._hold(_TLS.wrap_socket(
                plain, server_hostname=self.host, do_handshake_on_connect=False))
        except BaseException:
            plain.close()
            raise
        self.sock.do_handshake()
        self.connected = True


def _describe(exc: BaseException, parts: urllib.parse.SplitResult) -> str:
    """An exception as a sentence fit for Subscription.error and the log. The
    text is the standard library's, and nothing promises it will never quote
    the request back - so the path and query are taken out of it if it does."""
    text = str(exc) or type(exc).__name__
    for secret in (parts.query, parts.path):
        if len(secret) > 1:
            text = text.replace(secret, "[...]")
    return text


# Of the attempt's timeout, the share name resolution may use. The rest is the
# endpoint's whatever the resolver did: a lookup that comes back at the last
# moment would otherwise leave the endpoint milliseconds, and the failure that
# followed be charged to it. A lookup slower than this is Unresolved - the
# resolver's trouble, and ours.
RESOLVE_SHARE = 0.5
# Connection failures that are this box's and not the endpoint's: out of file
# descriptors, ports or buffers, or no route out at all. The endpoint was never
# reached, so it is not charged - ServerTrouble, like a resolver that does not
# answer. ENETUNREACH is also what a router answers for a network that is
# really gone, which is then retried for a few minutes and written off as one
# event: the subscription is not turned off over what may be our routing.
OUR_ERRNOS = frozenset((errno.EMFILE, errno.ENFILE, errno.ENOBUFS, errno.ENOMEM,
                        errno.ENETDOWN, errno.ENETUNREACH, errno.EADDRNOTAVAIL))


def post(url: str, body: bytes, headers: list[str], private_ok: bool,
         timeout: float | None = None) -> int:
    """POST one notification. Returns the 2xx status, or raises DeliveryError
    for what the endpoint got wrong and ServerTrouble for what this server
    could not do just now - a name that could not be looked up, a connection
    that could not be opened."""
    timeout = TIMEOUT if timeout is None else timeout
    started = time.monotonic()
    try:
        parts, addresses = resolve_addresses(url, private_ok, timeout * RESOLVE_SHARE)
    except Unresolved as exc:
        raise ServerTrouble(str(exc))
    except EndpointError as exc:
        raise DeliveryError(str(exc))
    port = parts.port or (443 if parts.scheme == "https" else 80)
    cls = _PinnedHTTPSConnection if parts.scheme == "https" else _PinnedHTTPConnection
    left = max(0.05, timeout - (time.monotonic() - started))
    # What the endpoint was actually given, for the messages below: a lookup
    # that took its whole share leaves half, and "within 10s" would be untrue.
    given = round(left, 1)
    conn = cls(_host(parts), port, addresses, left)
    target = (parts.path or "/") + ("?" + parts.query if parts.query else "")
    send = {"Content-Type": FHIR_JSON, "User-Agent": "nordic-hackathon-2026-notify"}
    for line in headers:
        name, _, value = line.partition(":")
        send[name.strip()] = value.strip()

    # The socket timeout bounds each connect, send and recv, and an endpoint
    # that keeps every one of them short can still take as long as it likes
    # over all of them. So the whole attempt gets a deadline, and at it the
    # socket is shut down under whatever is blocked on it.
    expired = threading.Event()

    def cut_off():
        expired.set()
        conn.cut()

    deadline = threading.Timer(left, cut_off)
    deadline.daemon = True
    deadline.start()
    sent = False
    try:
        interrupted = None
        try:
            conn.request("POST", target, body=body, headers=send)
            sent = True
        except (BrokenPipeError, ConnectionResetError,
                ssl.SSLEOFError, ssl.SSLZeroReturnError) as exc:
            # An endpoint that has seen enough - a body over its limit, a
            # missing header - may answer and hang up while the rest is still
            # being sent. Its answer is the useful half of that; look for it.
            # Over TLS the same hang-up is an EOF "in violation of protocol",
            # which read as a certificate problem would send the subscriber
            # looking in the wrong place. Not when the hang-up came before
            # there was a connection to send on, though: request() connects
            # too, and a TLS handshake that is reset - https on a plain port -
            # has no answer behind it.
            if not conn.connected:
                raise
            interrupted = exc
        try:
            resp = conn.getresponse()
            status = resp.status
            try:
                resp.read(MAX_RESPONSE_BYTES)
            except (OSError, http.client.HTTPException, ValueError):
                # The status line was the answer. A body that then breaks
                # off, or runs into the deadline, is one nobody was going to
                # use, and sending the notification again for it is a
                # duplicate the endpoint has already said it has.
                pass
        except (OSError, http.client.HTTPException):
            # The deadline looks exactly like a hang-up from in here - it *is*
            # one, made by cut_off() - and must not be reported as theirs.
            if interrupted is None or expired.is_set():
                raise
            raise DeliveryError(
                "the endpoint closed the connection while the notification was "
                "still being sent (%d bytes) - a body size limit in front of it?"
                % len(body))
    except DeliveryError:
        raise
    except (OSError, http.client.HTTPException, ValueError) as exc:
        # A cut shows up as an OSError or an HTTPException in every phase it
        # has been tried in. A bare ValueError is a guard for the one it has
        # not - the standard library raises it for I/O on what has been closed
        # under it: the deadline's doing, or else a bug. (Bare, because a
        # certificate that fails verification is a ValueError *and* an OSError.)
        if not isinstance(exc, (OSError, http.client.HTTPException)) \
                and not expired.is_set():
            raise
        if expired.is_set() or isinstance(exc, TimeoutError):
            if not sent:
                raise DeliveryError(
                    "the endpoint did not take the notification (%d bytes) "
                    "within %gs" % (len(body), given))
            raise DeliveryError("the endpoint did not answer within %gs" % given)
        if isinstance(exc, OSError) and exc.errno in OUR_ERRNOS and not conn.connected:
            raise ServerTrouble(
                "this server could not open a connection to the endpoint just "
                "now (%s) - not your endpoint's doing"
                % (errno.errorcode.get(exc.errno) or exc.errno))
        if isinstance(exc, ssl.SSLError):
            raise DeliveryError("TLS failure talking to the endpoint: %s"
                                % _describe(exc, parts))
        if isinstance(exc, http.client.BadStatusLine) \
                and not isinstance(exc, http.client.RemoteDisconnected):
            # Its text is the peer's first line, whatever the peer is - and
            # on a venue LAN that is a way of reading the banner of any port
            # in the room out of Subscription.error.
            raise DeliveryError("the endpoint did not answer with HTTP")
        raise DeliveryError("could not reach the endpoint: %s" % _describe(exc, parts))
    finally:
        deadline.cancel()
        conn.close()
    if interrupted is not None and 200 <= status < 300:
        # Its word stands - a 2xx is the endpoint saying it has it - but
        # somebody asked why that platform never ingested an event this log
        # calls delivered needs to be able to find that it hung up first.
        log("%s answered %d before it had taken the whole notification (%d "
            "bytes) - counted as delivered" % (_host(parts), status, len(body)))
    if 300 <= status < 400:
        raise DeliveryError(
            "the endpoint answered %d - redirects are not followed, subscribe "
            "with the URL it redirects to" % status)
    if not 200 <= status < 300:
        raise DeliveryError("the endpoint answered %d" % status)
    return status


# ---------------------------------------------------------------- resources


def subscription_url(sub) -> str:
    return "%s/Subscription/%d" % (sub["base_url"], sub["id"])


def status_parameters(sub, kind: str, status: str | None = None,
                      event: dict | None = None) -> dict:
    """The backport's R4 stand-in for SubscriptionStatus: a Parameters resource.

    Used for the first entry of every notification and for $status, which the
    IG defines as the same thing asked for rather than sent. `event` is
    {"number", "ts", "submission_id", "slug", "name"}.
    """
    parameter = [
        {"name": "subscription",
         "valueReference": {"reference": subscription_url(sub)}},
        {"name": "topic", "valueCanonical": sub["topic"]},
        {"name": "status", "valueCode": status or sub["status"]},
        {"name": "type", "valueCode": kind},
        # A string, not an integer: the IG carries a 64-bit count in a version
        # of FHIR that has no integer64.
        # Inclusive of this notification, and no more than that: an event
        # delivered late, with newer ones queued behind it, says its own
        # number and not where the counter has got to since - or every
        # subscriber working through a backlog would conclude it had missed
        # the events it is about to be sent.
        {"name": "events-since-subscription-start",
         "valueString": str(event["number"] if event else sub["events"])},
    ]
    if event is not None:
        part = [
            {"name": "event-number", "valueString": str(event["number"])},
            {"name": "timestamp", "valueInstant": event["ts"]},
        ]
        # `empty` says only that something happened. What, and whose, is for
        # the subscriber to come and ask - that is the point of choosing it.
        if sub["content"] != "empty":
            part.append({"name": "focus", "valueReference": {
                "reference": "%s/Bundle/%d" % (sub["base_url"], event["submission_id"]),
                "type": "Bundle"}})
            # Whose document it is. The Bundle does not say - a document names
            # its custodian however its author saw fit - and without this a
            # subscriber has to go through $submissions to find out.
            part.append({"name": "additional-context", "valueReference": {
                "reference": "%s/Organization/%s" % (sub["base_url"], event["slug"]),
                "type": "Organization", "display": event["name"]}})
        parameter.append({"name": "notification-event", "part": part})
    if kind == "query-status" and sub["error"]:
        parameter.append({"name": "error",
                          "valueCodeableConcept": {"text": sub["error"]}})
    return {
        "resourceType": "Parameters",
        "id": str(uuid.uuid4()),
        "meta": {"profile": [STATUS_PROFILE]},
        "parameter": parameter,
    }


def notification_bundle(sub, kind: str, status: str, event: dict | None = None,
                        focus: dict | None = None) -> dict:
    """What is POSTed to the endpoint. `focus` is the submitted Bundle itself,
    and only a full-resource subscription has one."""
    params = status_parameters(sub, kind, status, event)
    entries = [{
        "fullUrl": "urn:uuid:" + params["id"],
        "resource": params,
        "request": {"method": "GET", "url": subscription_url(sub) + "/$status"},
        "response": {"status": "200"},
    }]
    if event is not None and sub["content"] != "empty":
        # id-only has the entry too, without the resource: the IG leaves that
        # open, its own example does it, and a client that walks Bundle.entry
        # rather than the Parameters finds the document either way.
        entry = {
            "fullUrl": "%s/Bundle/%d" % (sub["base_url"], event["submission_id"]),
            "request": {"method": "POST", "url": "Bundle"},
            "response": {"status": "201"},
        }
        if focus is not None:
            entry["resource"] = focus
        entries.append(entry)
    return {
        "resourceType": "Bundle",
        "id": str(uuid.uuid4()),
        "meta": {"profile": [NOTIFICATION_PROFILE]},
        "type": "history",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "entry": entries,
    }


def _not_json(constant: str):
    raise ValueError("%s is not JSON" % constant)


def _focus_bundle(data_dir: str, row) -> dict | None:
    """The submitted document, under the id this server knows it by.

    The file is the submitter's exact bytes and its Bundle.id is whatever they
    wrote, if anything. Inside a notification it sits under a fullUrl ending in
    our id, and an entry whose id contradicts its fullUrl is one a receiving
    server is entitled to refuse - so the copy that travels is given ours. The
    untouched bytes are what that fullUrl serves.
    """
    path = os.path.join(data_dir, "submissions", row["team_slug"], row["filename"])
    try:
        with open(path, "rb") as fh:
            # NaN and Infinity are Python's JSON and nobody else's: read and
            # written back, they are a body the endpoint cannot parse and a
            # failure it would be charged for.
            bundle = json.load(fh, parse_constant=_not_json)
    except (OSError, ValueError, RecursionError) as exc:
        # Said here, with the path, because nowhere else knows it: "missing"
        # and "permission denied on the volume" are different afternoons.
        log("CANNOT READ %s: %s: %s" % (path, type(exc).__name__, exc))
        if isinstance(exc, OSError) and not isinstance(exc, FileNotFoundError):
            # Out of file handles, an I/O error: it may well read in a minute.
            raise ServerTrouble("this server could not read the document this "
                                "event was about just now (%s)"
                                % (errno.errorcode.get(exc.errno) or type(exc).__name__))
        return None
    if not isinstance(bundle, dict):
        log("CANNOT READ %s: not a JSON object" % path)
        return None
    bundle["id"] = str(row["submission_id"])
    return bundle


# ---------------------------------------------------------------- the queue


def connect(data_dir: str) -> sqlite3.Connection:
    # timeout=30 to match app.py: a delivery result landing while the service
    # commits a submission should wait its turn, not be lost to "locked".
    conn = sqlite3.connect(os.path.join(data_dir, DB_NAME), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


DUE = """
    SELECT n.id AS notification_id, n.kind, n.event_number, n.submission_id,
           n.ts AS event_ts, n.attempts, n.faults, n.next_attempt,
           s.id, s.status, s.topic, s.endpoint, s.headers, s.content,
           s.base_url, s.events, s.error,
           t.slug AS team_slug, t.name AS team_name, sub.filename
    FROM notifications n
    JOIN subscriptions s ON s.id = n.subscription_id
    LEFT JOIN submissions sub ON sub.id = n.submission_id
    LEFT JOIN teams t ON t.id = sub.team_id
    WHERE n.state = 'pending'
"""


def stamp(when: datetime) -> str:
    """A UTC instant as the queue stores it. Always with microseconds, so that
    next_attempt can be compared as text: isoformat() drops them when they are
    zero, and "…:00+00:00" against "…:00.5+00:00" only sorts right by luck."""
    return when.astimezone(timezone.utc).isoformat(timespec="microseconds")


def due_queues(conn: sqlite3.Connection, now: datetime) -> list[list[sqlite3.Row]]:
    """One list per subscription with something to send now, oldest first.

    A subscription whose oldest pending notification is still backing off is
    left out entirely, newer notifications and all: delivering event 5 while
    event 4 waits for its retry is how a subscriber ends up ingesting a
    company's older document over its newer one.
    """
    queues: dict[int, list[sqlite3.Row]] = {}
    for row in conn.execute(DUE + " ORDER BY n.id"):
        queues.setdefault(row["id"], []).append(row)
    cutoff = stamp(now)
    # What has never been tried is due whatever its stamp says. The stamp is
    # the service's clock at the time; if the clock has since been stepped
    # back, "not before a minute from now" is not what anybody meant by it.
    return [q for q in queues.values()
            if q[0]["next_attempt"] <= cutoff
            or not (q[0]["attempts"] or q[0]["faults"])]


class Outcome(NamedTuple):
    """How one attempt went, and whose doing that was.

    `endpoint` is the subscriber's side: it did not answer, or not with a 2xx.
    Those are retried, put the subscription in `error` and in the end turn it
    `off`. `server` is ours, and will not pass - the document is gone, or is
    not one that can be read back - and the subscriber is not made to pay for
    it: the one notification is written off, the log says why at length, and
    the queue moves on. `transient` is ours too, of the kind that may pass -
    the resolver is not answering, the volume cannot be read, a thread would
    not start - and a bug in this code, which cannot be told from those. It is
    tried again on TRANSIENT_BACKOFF, counted in `faults` and not `attempts`,
    and written off like `server` when that runs out. For a handshake, either
    of ours turns the subscription `off` instead: there is no next
    notification to move on to, and its owner is told to PUT it again.
    """
    kind: str            # one of KINDS
    error: str = ""


KINDS = ("delivered", "endpoint", "server", "transient")
DELIVERED = Outcome("delivered")
DROPPED = "result dropped, the subscription changed meanwhile"


def _attempt(data_dir: str, row, private_ok: bool) -> Outcome:
    """Send one notification."""
    if row["kind"] == "event-notification" and row["content"] == "full-resource":
        with _WHOLE_DOCUMENTS:
            return _attempt_held(data_dir, row, private_ok)
    return _attempt_held(data_dir, row, private_ok)


def _attempt_held(data_dir: str, row, private_ok: bool) -> Outcome:
    try:
        event = focus = None
        if row["kind"] == "event-notification":
            event = {"number": row["event_number"], "ts": row["event_ts"],
                     "submission_id": row["submission_id"],
                     "slug": row["team_slug"], "name": row["team_name"]}
            if row["team_slug"] is None:
                # Nothing in the service deletes one submission, so this is a
                # database somebody has been into by hand. There is nothing
                # left to point at.
                return Outcome("server", "the document this event was about no "
                                         "longer exists on this server")
            if row["content"] == "full-resource":
                focus = _focus_bundle(data_dir, row)
                if focus is None:
                    return Outcome("server", "this server could not read the "
                                             "document this event was about")
        # What the subscriber is told the status is, is what it will be once
        # this has been accepted: a handshake is still `requested`, and an
        # event that gets through is by definition on an `active` subscription.
        status = "requested" if row["kind"] == "handshake" else "active"
        body = json.dumps(notification_bundle(row, row["kind"], status, event, focus))
        headers = json.loads(row["headers"])
        try:
            post(row["endpoint"], body.encode("utf-8"), headers, private_ok)
        except DeliveryError as exc:
            return Outcome("endpoint", str(exc)[:MAX_ERROR_CHARS])
        return DELIVERED
    except ServerTrouble as exc:
        return Outcome("transient", str(exc)[:MAX_ERROR_CHARS])
    except Exception as exc:
        # A bug, in here or in post() - or the box out of threads or memory,
        # which looks the same from here and passes, so it is tried again. The
        # traceback goes to the log, where somebody can do something about it;
        # the subscriber gets a sentence that says it was not them.
        log("BUG delivering Subscription/%d #%d:\n%s"
            % (row["id"], row["event_number"], traceback.format_exc()))
        return Outcome("transient", "this server failed to send the notification "
                                    "(%s) - not your endpoint's doing"
                                    % type(exc).__name__)


def _record(conn: sqlite3.Connection, row, outcome: Outcome, now: datetime) -> str:
    """Write one attempt's result down. Returns a word for the log."""
    if outcome.kind not in KINDS:
        # Before anything is written. A kind nobody handles would otherwise
        # fall into a retry branch below and be retried for ever.
        raise ValueError("unknown outcome %r" % (outcome.kind,))
    handshake = row["kind"] == "handshake"
    ours = outcome.kind in ("server", "transient")
    # The endpoint's failures and ours are counted apart, so that a resolver
    # that hiccups on the fifth retry does not use up the endpoint's last try,
    # nor the other way round.
    attempts = row["attempts"] + (not ours)
    faults = row["faults"] + ours
    if outcome.kind == "transient":
        backoff, tries = TRANSIENT_BACKOFF, faults
    else:
        backoff, tries = (HANDSHAKE_BACKOFF if handshake else BACKOFF), attempts
    out_of_tries = tries > len(backoff)
    gave_up = outcome.kind == "endpoint" and out_of_tries
    written_off = outcome.kind == "server" or (outcome.kind == "transient"
                                               and out_of_tries)
    error = outcome.error
    conn.execute("BEGIN IMMEDIATE")
    try:
        if outcome.kind == "delivered":
            state, retry = "delivered", None
        elif gave_up or written_off:
            state, retry = "failed", None
        else:
            state, retry = "pending", now + timedelta(seconds=backoff[tries - 1])
        # Everything below hangs on this one row still being there, pending.
        # The owner may have PUT or deleted the subscription while the attempt
        # was in flight - which removes what was queued for it, and ids are
        # never reused - and a late result for the old endpoint then says
        # nothing about the new one: it must not activate it unshaken, fail
        # its handshake or turn it off.
        changed = conn.execute(
            "UPDATE notifications SET state = ?, attempts = ?, faults = ?, "
            "error = ?, next_attempt = coalesce(?, next_attempt) "
            "WHERE id = ? AND subscription_id = ? AND state = 'pending' "
            "AND attempts = ? AND faults = ?",
            (state, attempts, faults, error, retry and stamp(retry),
             row["notification_id"], row["id"], row["attempts"],
             row["faults"])).rowcount
        if not changed:
            conn.rollback()
            return DROPPED

        if outcome.kind == "delivered":
            # `active` as well as `error`, for an event: an active
            # subscription may be carrying the note a write-off left below.
            conn.execute(
                "UPDATE subscriptions SET status = 'active', error = '' "
                "WHERE id = ? AND status IN (%s)"
                % ("'requested'" if handshake else "'active', 'error'"),
                (row["id"],))
            verdict = "delivered"
        elif written_off:
            if handshake:
                # No handshake, no subscription - and nothing will retry it.
                conn.execute(
                    "UPDATE subscriptions SET status = 'off', error = ? "
                    "WHERE id = ? AND status != 'off'",
                    (error + ". PUT the subscription to try again", row["id"]))
            else:
                # `error` means something is being retried. If this was the
                # retry, and nothing is queued behind it, nothing is: the next
                # event is what will say how the endpoint is doing. The status
                # is not made any worse - this was not the endpoint's doing -
                # but the owner is told, until the next delivery: "ours" covers
                # a name whose own servers are what is not answering, and a
                # subscription that loses every event to that while reading
                # `active` and nothing else is one nobody knows to look at.
                conn.execute(
                    "UPDATE subscriptions SET error = ?, status = CASE "
                    "WHEN status = 'error' AND NOT EXISTS ("
                    "SELECT 1 FROM notifications WHERE subscription_id = ? "
                    "AND state = 'pending') THEN 'active' ELSE status END "
                    "WHERE id = ? AND status IN ('active', 'error')",
                    ("Event %d was not sent, and will not be: %s"
                     % (row["event_number"], error), row["id"], row["id"]))
            verdict = "WRITTEN OFF, our fault"
        elif gave_up:
            # Everything still queued behind it goes too: `off` means nothing
            # more is sent, and a PUT that revives the subscription should not
            # be followed by an hour-old backlog.
            conn.execute(
                "UPDATE notifications SET state = 'failed', error = ? "
                "WHERE subscription_id = ? AND state = 'pending'",
                ("subscription turned off", row["id"]))
            # Not over an `off` that is already there: one that reached its
            # `end` meanwhile is off for that reason, and should go on saying so.
            conn.execute(
                "UPDATE subscriptions SET status = 'off', error = ? "
                "WHERE id = ? AND status != 'off'",
                ("Gave up after %d attempts: %s. PUT the subscription to try "
                 "again" % (attempts, error), row["id"]))
            verdict = "gave up, subscription off"
        elif ours:
            # The subscription is left as it is - this is not its endpoint's
            # doing. Except that somebody waiting on a handshake is looking at
            # `requested` and wondering, so they are told why.
            if handshake:
                conn.execute(
                    "UPDATE subscriptions SET error = ? "
                    "WHERE id = ? AND status = 'requested'", (error, row["id"]))
            verdict = "NOT SENT, our trouble, retry in %ds" % backoff[tries - 1]
        else:
            # A handshake that has not got through leaves the subscription
            # `requested` - `error` is for one that worked once.
            conn.execute(
                "UPDATE subscriptions SET error = ?, status = CASE status "
                "WHEN 'active' THEN 'error' ELSE status END "
                "WHERE id = ? AND status != 'off'", (error, row["id"]))
            verdict = "failed, retry in %ds" % backoff[tries - 1]
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return verdict


# Attempts that were made and whose result could not be written down - a
# database locked for longer than its timeout, a full disk. Without this the
# row is still pending and untouched a second later, and the subscriber is sent
# the same event every second for as long as the trouble lasts. Remembered
# here, the write is retried first and nothing is re-sent. In memory only: a
# restart in that window re-sends, which is what at-least-once means. Keyed by
# database as well as by id: notification 1 of one data directory is not
# notification 1 of another.
_UNRECORDED: dict[tuple[str, int], Outcome] = {}
_UNRECORDED_LOCK = threading.Lock()


def _database(conn: sqlite3.Connection) -> str:
    return conn.execute("PRAGMA database_list").fetchone()[2]


class _Unwritten(Exception):
    """_drain stopped because a result could not be written down. Carries what
    it had counted, and tells the Dispatcher to give this subscription a rest
    rather than find the same trouble again a second later."""

    def __init__(self, delivered: int, failed: int):
        super().__init__(delivered, failed)
        self.counts = (delivered, failed)


def _drain(data_dir: str, queue: list, private_ok: bool,
           now: datetime | None = None) -> tuple[int, int]:
    """Work through one subscription's queue, in order, writing each result
    down before the next is sent, until an attempt fails or none is started
    after DRAIN_BUDGET. Returns (delivered, failed attempts); raises _Unwritten
    if a result could not be recorded."""
    delivered = failed = 0
    started = time.monotonic()
    conn = connect(data_dir)
    try:
        database = _database(conn)
        for listed in queue:
            key = (database, listed["notification_id"])
            # Read again, now: the list was made when the pass began, and the
            # owner may since have PUT a new endpoint or deleted the lot. What
            # is gone is not sent, and what is sent goes where it now points.
            row = conn.execute(DUE + " AND n.id = ? AND s.status != 'off'",
                               (key[1],)).fetchone()
            if row is None:
                break
            with _UNRECORDED_LOCK:
                outcome = _UNRECORDED.get(key)
            if outcome is None:
                outcome = _attempt(data_dir, row, private_ok)
            # The host and nothing after it: a path is where people put the
            # secret when they cannot set a header.
            what = "Subscription/%d %s #%d -> %s" % (
                row["id"], row["kind"], row["event_number"],
                urllib.parse.urlsplit(row["endpoint"]).hostname)
            try:
                verdict = _record(conn, row, outcome, now or datetime.now(timezone.utc))
            except Exception as exc:
                # Whatever it was - a locked database, or a bug in _record -
                # the POST has been made, and not knowing how to write that
                # down is no reason to make it again.
                with _UNRECORDED_LOCK:
                    _UNRECORDED[key] = outcome
                log("%s: %s, but that COULD NOT BE WRITTEN DOWN (%s) - nothing "
                    "more is sent to this subscription until it is%s"
                    % (what, outcome.kind, exc,
                       "" if isinstance(exc, sqlite3.Error)
                       else "\n" + traceback.format_exc()))
                raise _Unwritten(delivered, failed + 1)
            with _UNRECORDED_LOCK:
                _UNRECORDED.pop(key, None)
            # A dropped result still says how the attempt went: whether the
            # old endpoint was handed the notification before its owner
            # re-pointed the subscription is what an operator asked about a
            # duplicate needs to know, and nothing else records it.
            log("%s: %s%s%s" % (
                what, verdict,
                " (was %s)" % outcome.kind if verdict == DROPPED else "",
                " (%s)" % outcome.error if outcome.error else ""))
            if verdict == DROPPED:
                # Whatever is queued now is a different subscription's worth,
                # and the next look reads it fresh.
                break
            delivered += outcome.kind == "delivered"
            failed += outcome.kind != "delivered"
            if outcome.kind in ("endpoint", "transient"):
                # Still pending, most likely, and the next one may not
                # overtake it. If it was given up on instead, the rest went
                # with it.
                break
            if time.monotonic() - started > DRAIN_BUDGET:
                break
    finally:
        conn.close()
    return delivered, failed


def expire(conn: sqlite3.Connection, now: datetime) -> None:
    """Turn `off` what has reached its `end`, and write off whatever is still
    queued for a subscription that is off - however it came to be."""
    cutoff = stamp(now)
    ended = conn.execute(
        "SELECT 1 FROM subscriptions WHERE status != 'off' "
        "AND end_ts IS NOT NULL AND end_ts != '' AND end_ts <= ? LIMIT 1",
        (cutoff,)).fetchone()
    # Queued for a subscription that is off, or that is not there at all -
    # which takes a DELETE made by hand, without foreign keys on. No query
    # that joins the two tables would ever show that row again.
    stranded = conn.execute(
        "SELECT 1 FROM notifications WHERE state = 'pending' "
        "AND subscription_id NOT IN (SELECT id FROM subscriptions "
        "                            WHERE status != 'off') LIMIT 1").fetchone()
    if ended or stranded:    # rarely - and no write lock taken to find that out
        _expire(conn, cutoff)
    # The lock is not held across the query - a worker with a result to
    # remember must not wait on the database for it - and only what was
    # remembered before the query is judged by it.
    with _UNRECORDED_LOCK:
        remembered = list(_UNRECORDED)
    if remembered:
        database = _database(conn)
        pending = {r[0] for r in conn.execute(
            "SELECT id FROM notifications WHERE state = 'pending'")}
        with _UNRECORDED_LOCK:
            for key in remembered:
                if key[0] == database and key[1] not in pending:
                    _UNRECORDED.pop(key, None)


def _expire(conn: sqlite3.Connection, cutoff: str) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE subscriptions SET status = 'off', "
            "error = 'Ended at ' || end_ts || ', as the subscription asked' "
            "WHERE status != 'off' AND end_ts IS NOT NULL AND end_ts != '' "
            "AND end_ts <= ?", (cutoff,))
        conn.execute(
            "UPDATE notifications SET state = 'failed', "
            "error = 'subscription turned off' WHERE state = 'pending' "
            "AND subscription_id NOT IN (SELECT id FROM subscriptions "
            "                            WHERE status != 'off')")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


class AlreadyDelivering(Exception):
    """Another notifier has this data directory."""


def _claim(data_dir: str):
    """One notifier per data directory. "One worker per subscription" is a set
    in one process's memory, and a second process - a --once run beside the
    loop, the old container still stopping while the new one starts - knows
    nothing of it and POSTs whatever the first has in flight a second time.
    The lock goes when the process does, however it goes."""
    if fcntl is None:
        return None
    handle = open(os.path.join(data_dir, "notify.lock"), "a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        # Anything else - a volume that has no locks to give - is not a second
        # notifier, and saying so sends somebody looking for one.
        if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            raise
        raise AlreadyDelivering(
            "another notifier is already delivering from %s" % data_dir)
    return handle


class Dispatcher:
    """Hands due queues to a pool of workers, one subscription per task.

    look() does not wait for what it starts. A subscription a worker is still
    busy with is left alone until that worker is done - which is what keeps
    its notifications in order - and every other subscription is looked at
    again at the next look, whatever the slow one is doing.
    """

    def __init__(self, data_dir: str, private_ok: bool | None = None):
        self.data_dir = data_dir
        self.private_ok = allow_private() if private_ok is None else private_ok
        self.claim = _claim(data_dir)
        self.pool = ThreadPoolExecutor(max_workers=WORKERS,
                                       thread_name_prefix="deliver")
        # Every worker thread is started here, or none is. submit() puts the
        # work item on its queue *before* it starts a thread for it, so a
        # thread that cannot be started (a container out of pids) would leave
        # the item to whichever worker is next free - after look() had given
        # the subscription up as never started and let another look submit it
        # again: two workers on one subscription, and every notification in
        # flight sent twice. With the pool full from the start, submit() never
        # starts a thread again, and a box that cannot start sixteen fails
        # here, where run_loop tries again in a moment.
        ready = threading.Barrier(WORKERS)
        try:
            for task in [self.pool.submit(ready.wait, 30) for _ in range(WORKERS)]:
                task.result()
        except BaseException:
            ready.abort()
            self.pool.shutdown(wait=False, cancel_futures=True)
            if self.claim is not None:
                self.claim.close()
            raise
        self.busy: set[int] = set()
        # subscription -> (troubles in a row, not before, on the monotonic
        # clock). Trouble here is ours - a result that would not write, a
        # locked database - and without a rest it is found again, and logged
        # again, at every look for as long as it lasts.
        self.resting: dict[int, tuple[int, float]] = {}
        self.lock = threading.Lock()

    def look(self, now: datetime | None = None) -> list:
        conn = connect(self.data_dir)
        try:
            expire(conn, now or datetime.now(timezone.utc))
            queues = due_queues(conn, now or datetime.now(timezone.utc))
        finally:
            conn.close()
        started = []
        for queue in queues:
            subscription_id = queue[0]["id"]
            with self.lock:
                if subscription_id in self.busy:
                    continue
                if time.monotonic() < self.resting.get(subscription_id, (0, 0))[1]:
                    continue
                self.busy.add(subscription_id)
            try:
                started.append(
                    self.pool.submit(self._work, subscription_id, queue, now))
            except BaseException:
                # Never started, so _work's finally never runs: with the pool's
                # threads all started in __init__, submit() can only refuse
                # before it has queued anything.
                with self.lock:
                    self.busy.discard(subscription_id)
                raise
        return started

    def _work(self, subscription_id: int, queue: list, now) -> tuple[int, int]:
        troubled = True
        try:
            result = _drain(self.data_dir, queue, self.private_ok, now)
            troubled = False
            return result
        except _Unwritten as exc:
            return exc.counts            # _drain has said what and why
        except sqlite3.Error as exc:
            log("DATABASE TROUBLE working on Subscription/%d: %s"
                % (subscription_id, exc))
            return 0, 1
        except Exception:
            # One subscription's trouble. The others' workers never hear of it.
            log("BUG working on Subscription/%d:\n%s"
                % (subscription_id, traceback.format_exc()))
            return 0, 1
        finally:
            with self.lock:
                self.busy.discard(subscription_id)
                if troubled:
                    count = self.resting.get(subscription_id, (0, 0))[0] + 1
                    self.resting[subscription_id] = (count, time.monotonic() + min(
                        POLL * 2 ** min(count, 10), MAX_LOOP_BACKOFF))
                else:
                    self.resting.pop(subscription_id, None)

    def close(self) -> None:
        self.pool.shutdown(wait=True)
        if self.claim is not None:
            self.claim.close()


def deliver_due(data_dir: str, private_ok: bool | None = None,
                now: datetime | None = None) -> tuple[int, int]:
    """One pass over the queue, waited for. Returns (delivered, failed
    attempts). What --once and the tests use; the loop does not wait."""
    dispatcher = Dispatcher(data_dir, private_ok)
    try:
        results = [task.result() for task in dispatcher.look(now)]
    finally:
        dispatcher.close()
    return sum(r[0] for r in results), sum(r[1] for r in results)


def _has_tables(data_dir: str) -> bool:
    if not os.path.exists(os.path.join(data_dir, DB_NAME)):
        return False
    conn = connect(data_dir)
    try:
        return conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('subscriptions', 'notifications')").fetchone()[0] == 2
    finally:
        conn.close()


def run_loop(data_dir: str) -> int:
    log("delivering from %s every %gs, %s" % (
        data_dir, POLL,
        "private addresses allowed" if allow_private() else "public addresses only"))
    dispatcher = None
    said = None             # what it is waiting for, and when it last said so
    failures = 0
    while True:
        try:
            waiting_for = None
            if not _has_tables(data_dir):
                # The service creates the schema at boot, and this container
                # may well be up first. A directory that is not there at all
                # is more likely a typo or a volume that did not mount.
                waiting_for = (
                    "no subscription tables in %s yet - waiting for the service"
                    if os.path.isdir(data_dir) else
                    "%s DOES NOT EXIST - check DATA_DIR and the volume; waiting")
            elif dispatcher is None:
                try:
                    dispatcher = Dispatcher(data_dir)
                except AlreadyDelivering:
                    # The container this one replaces, still finishing.
                    waiting_for = "another notifier still has %s - waiting for it"
            if dispatcher is not None:
                said = None
                dispatcher.look()
            elif said is None or said[0] != waiting_for \
                    or time.monotonic() - said[1] >= REMIND:
                # Again every minute: said once, a loop that will never do
                # anything looks exactly like one with nothing to do.
                said = (waiting_for, time.monotonic())
                log(waiting_for % data_dir)
            failures = 0
        except Exception:
            # A locked database or a full disk must not end the loop: a
            # delivery daemon that dies on one error stops silently, and the
            # first anyone hears of it is a team asking where its events went.
            # It does slow the loop down, so that the log is a line every half
            # minute to read and not one a second to scroll past.
            failures += 1
            log("LOOK FAILED (%d in a row):\n%s" % (failures, traceback.format_exc()))
        time.sleep(min(POLL * 2 ** min(failures, 10), MAX_LOOP_BACKOFF)
                   if failures else POLL)


def print_status(data_dir: str) -> int:
    if not _has_tables(data_dir):
        print("no subscription tables in %s - check --data-dir, or start the "
              "service once" % data_dir)
        return 1
    conn = connect(data_dir)
    try:
        subs = conn.execute(
            "SELECT s.id, s.status, s.endpoint, s.events, s.error, t.slug, "
            "(SELECT count(*) FROM notifications n WHERE n.subscription_id = s.id "
            " AND n.state = 'pending') AS pending "
            "FROM subscriptions s JOIN teams t ON t.id = s.team_id ORDER BY s.id"
        ).fetchall()
    finally:
        conn.close()
    print("private addresses: %s" % ("allowed" if allow_private() else "refused"))
    for s in subs:
        print("Subscription/%-4d %-9s %-20s events=%-4d pending=%-3d %s%s" % (
            s["id"], s["status"], s["slug"], s["events"], s["pending"],
            urllib.parse.urlsplit(s["endpoint"]).hostname,
            "  [%s]" % s["error"] if s["error"] else ""))
    print("%d subscription(s) in %s" % (len(subs), data_dir))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Deliver FHIR Subscription notifications.",
        epilog="SUBSCRIPTION_ALLOW_PRIVATE=1 lets endpoints be private "
               "addresses, for a venue LAN. Leave it unset on a public box.",
    )
    ap.add_argument("--loop", action="store_true",
                    help="keep delivering, looking at the queue every NOTIFY_POLL seconds")
    ap.add_argument("--once", action="store_true",
                    help="deliver what is due now, then exit - non-zero if "
                         "anything failed or is still queued. Not beside "
                         "--loop, which has the data directory to itself")
    ap.add_argument("--status", action="store_true",
                    help="list the subscriptions and what is queued for each")
    ap.add_argument("--data-dir", default=DEFAULT_DATA)
    args = ap.parse_args()

    if args.status:
        return print_status(args.data_dir)
    if args.loop:
        return run_loop(args.data_dir)
    if args.once:
        if not _has_tables(args.data_dir):
            print("no subscription tables in %s - check --data-dir" % args.data_dir,
                  file=sys.stderr)
            return 1
        try:
            delivered, failed = deliver_due(args.data_dir)
        except AlreadyDelivering as exc:
            print("%s - the loop, most likely, and it needs no help" % exc,
                  file=sys.stderr)
            return 1
        conn = connect(args.data_dir)
        try:
            # Waiting on a retry, or behind a slow endpoint when the pass ran
            # out of time: not delivered either, whichever it was.
            queued = conn.execute(
                "SELECT count(*) FROM notifications WHERE state = 'pending'"
            ).fetchone()[0]
        finally:
            conn.close()
        log("%d delivered, %d failed, %d still queued" % (delivered, failed, queued))
        return 0 if not (failed or queued) else 1
    ap.print_usage(sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
