"""Base implementation of event loop.

The event loop can be broken up into a multiplexer (the part
responsible for notifying us of I/O events) and the event loop proper,
which wraps a multiplexer with functionality for scheduling callbacks,
immediately or at a given time in the future.

Whenever a public API takes a callback, subsequent positional
arguments will be passed to the callback if/when it is called.  This
avoids the proliferation of trivial lambdas implementing closures.
Keyword arguments for the callback are not supported; this is a
conscious design decision, leaving the door open for keyword arguments
to modify the meaning of the API call itself.
"""
import collections
import concurrent.futures
import functools
import heapq
import itertools
import os
import selectors
import socket
import stat
import subprocess
import sys
import threading
import time
import traceback
import warnings
import weakref

from . import coroutines
from . import events
from . import exceptions
from . import futures
from . import protocols
from . import staggered
from . import tasks
from . import transports
from . import trsock
from . import constants
from .log import logger

# Minimum number of _scheduled timer handles before cleanup of
# cancelled handles is performed.
_MIN_SCHEDULED_TIMER_HANDLES = 100

# Minimum fraction of _scheduled timer handles that are cancelled
# before cleanup of cancelled handles is performed.
_MIN_CANCELLED_TIMER_HANDLES_FRACTION = 0.5

_HAS_IPv6 = hasattr(socket, 'AF_INET6')

# Maximum timeout passed to select to avoid OS limitations
MAXIMUM_SELECT_TIMEOUT = 24 * 3600

# Used for deprecation and removal of `loop.create_datagram_endpoint()`'s
# *reuse_address* parameter
_unset = object()


def _format_handle(handle):
    cb = handle._callback
    # if the handle's callback was a Task's .__step or .__wakeup method
    if isinstance(getattr(cb, '__self__', None), tasks.Task):
        # format the task
        return repr(cb.__self__)
    else:
        return str(handle)


def _format_pipe(fd):
    if fd == subprocess.PIPE:
        return '<pipe>'
    elif fd == subprocess.STDOUT:
        return '<stdout>'
    else:
        return repr(fd)


# https://stackoverflow.com/questions/14388706/how-do-so-reuseaddr-and-so-reuseport-differ
# https://habr.com/ru/post/259403/
def _set_reuseport(sock):
    if not hasattr(socket, 'SO_REUSEPORT'):
        raise ValueError('reuse_port not supported by socket module')
    else:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            raise ValueError('reuse_port not supported by socket module, '
                             'SO_REUSEPORT defined but not implemented.')


def _ipaddr_info(host, port, family, type, proto, flowinfo=0, scopeid=0):
    # Try to skip getaddrinfo if "host" is already an IP. Users might have
    # handled name resolution in their own code and pass in resolved IPs.

    # 1) Check that this platform supports socket.inet_pton function
    #    for IP address validation

    # socket.inet_pton(address_family, ip_string)
    # converts an IP address from its family-specific string format
    # to a packed, binary format:
    # 1) struct in_addr for IPv4
    # 2) struct in6_addr for IPv6
    # Can be used when a library or network protocol asks for such type of struct

    # In our case it's used for validation of the IP address, if it already is
    # one
    # If not already an IP address or an invalid IP address will raise
    # `OSError: illegal IP address string passed to inet_pton`

    # So if some Unix platform doesn't have access to this function,
    # just return abruptly
    if not hasattr(socket, 'inet_pton'):
        return

    # 2) Check that host was specified and protocol is either 0 or a
    #    valid protocol type

    # if protocol passed in is invalid or host is None, return abruptly
    if proto not in {0, socket.IPPROTO_TCP, socket.IPPROTO_UDP} or \
        host is None:
        return None

    # 3) Derive protocol from socket type

    # derive protocol type from socket type
    if type == socket.SOCK_STREAM:
        proto = socket.IPPROTO_TCP
    elif type == socket.SOCK_DGRAM:
        proto = socket.IPPROTO_UDP
    else:
        return None

    # 4) Set port to 0 if wasn't specified

    # if port is not specified set it to 0
    # if port is a service name like "http" return abruptly
    if port is None:
        port = 0
    elif isinstance(port, bytes) and port == b'':
        port = 0
    elif isinstance(port, str) and port == '':
        port = 0
    else:
        # If port's a service name like "http", don't skip getaddrinfo.
        # (can be http, because http usually means port 80)
        try:
            port = int(port)
        except (TypeError, ValueError):
            return None

    # 5) Get address family/families to use when calling
    #    socket.inet_pton(address_family, ip_string)

    # user didn't specify the address family, could be either IPv4 or IPv6
    # try both while validating with socket.inet_pton(address_family, ip_string)
    if family == socket.AF_UNSPEC:
        # for IPv4
        afs = [socket.AF_INET]
        # if current platform supports IPv6
        if _HAS_IPv6:
            afs.append(socket.AF_INET6)
    # user specified an address family
    else:
        afs = [family]

    # 6) Convert host to string if needed and validate it

    # idna - encoding for Internationalizing Domain Names in Applications
    if isinstance(host, bytes):
        host = host.decode('idna')
    if '%' in host:
        # Linux's inet_pton doesn't accept an IPv6 zone index after host,
        # like '::1%lo0'.
        return None

    # 7) Validate that host is already an IP address,
    #    if so, return all info about it

    # check host with every address family (could be IPv4 and IPv6 if user
    # didn't specify address family)
    for af in afs:
        try:
            # if success, means a valid IP address of family = `af`
            socket.inet_pton(af, host)
            # The host has already been resolved.
            # is an IPv6 IP address, return all info about it
            if _HAS_IPv6 and af == socket.AF_INET6:
                return af, type, proto, '', (host, port, flowinfo, scopeid)
            else:
                # is an IPv4 IP address, return all info about it
                return af, type, proto, '', (host, port)
        except OSError:
            # `OSError: illegal IP address string passed to inet_pton`
            pass

    # "host" is not an IP address.
    return None


def _interleave_addrinfos(addrinfos, first_address_family_count=1):
    """
    Interleave (чередовать) list of addrinfo tuples by family.

    Creates and returns a flat list of address infos, where each group by family
    comes one after another. Example:
    [<addrinfo1 family1>, <addrinfo2 family1>, <addrinfo3 family2>,
    <addrinfo4 family2> and so on]
    """
    # Group addresses by family
    addrinfos_by_family = collections.OrderedDict()
    for addr in addrinfos:
        family = addr[0]
        if family not in addrinfos_by_family:
            addrinfos_by_family[family] = []
        addrinfos_by_family[family].append(addr)

    # list of list of address infos grouped by family
    addrinfos_lists = list(addrinfos_by_family.values())

    reordered = []
    if first_address_family_count > 1:
        reordered.extend(addrinfos_lists[0][:first_address_family_count - 1])
        del addrinfos_lists[0][:first_address_family_count - 1]

    # Flatten out list of list of address infos grouped by family
    # Exclude Nones
    # Address infos will still be grouped by family
    # but there will be all in one list, without any boundaries
    reordered.extend(
        a for a in itertools.chain.from_iterable(
            itertools.zip_longest(*addrinfos_lists)
        ) if a is not None)

    return reordered


def _run_until_complete_cb(fut):
    """
    Will be called as a Task's done callback within .run_until_complete
    method of the Event Loop (Task will be created from a passed in coro
    to the .run_until_complete method)

    Stops the .run_forever() method of the loop tied to this Task
    because the .run_forever() method is used within .run_until_complete
    """
    # if Task/Future wasn't cancelled
    # check that there was no SystemExit, KeyboardInterrupt raised,
    # return abruptly if there was, because we don't need to stop
    # the .run_forever() method
    if not fut.cancelled():
        exc = fut.exception()
        if isinstance(exc, (SystemExit, KeyboardInterrupt)):
            # Issue #22429: run_forever() already finished, no need to
            # stop it.
            return
    # Get the loop tied to this Task and stop it, so the .run_forever
    # method will break cycles and exit
    futures._get_loop(fut).stop()


# Disables Nagle algorithm on the socket passed in (if its a tcp socket)
# Nagle algorithm - is turned on by default, its purpose is to reduce the
# number of small packets in the world wide web. Algorithm:
#   if the current connection has got unacked (outstanding) data in flight, then
#   small packets will not be sent until we receive an ack for the previous
#   data packet.
if hasattr(socket, 'TCP_NODELAY'):
    def _set_nodelay(sock):
        if (
            sock.family in {socket.AF_INET, socket.AF_INET6}
            and sock.type == socket.SOCK_STREAM
            and sock.proto == socket.IPPROTO_TCP
        ):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
else:
    def _set_nodelay(sock):
        pass


class _SendfileFallbackProtocol(protocols.Protocol):
    """
    This protocol is used as a `sendfile` fallback via user-space buffers.
    On start of `sendfile` will replace transport's current protocol.

    On instantiation:
        1) Save transport's current protocol to restore it after `sendfile`
           fallback completes

        2) Pauses transport from reading - will resume after `sendfile` fallback
           completes (if the transport wasn't paused before `sendfile`)

        3) If transport's write buffer is currently above the high watermark,
           will arrange for the first call to `await self.drain()` to block
           until transport's buffer drop below high watermark

    Mainly implements logic for:
        1) pausing when transport's write buffer goes above high watermark -
            await self.drain();
        2) Pausing and resuming writing via `self.pause_writing` and
           `self.resume_writing` methods called by the transport


    On `sendfile` completion via await `self.restore()`:
        1) Restores original protocol on transport
        2) Notifies transport to resume reading if it wasn't paused before
           sendfile
        3) If original protocol was paused writing before sendfile, resumes it,
           because after sendfile completes transport's write buffer
           is guaranteed to be below the high watermark and thus the original
            protocol `self._proto` can resume writing.
    """

    def __init__(self, transp):
        if not isinstance(transp, transports._FlowControlMixin):
            raise TypeError("transport should be _FlowControlMixin instance")

        self._transport = transp
        # if transport is an instance of `_SelectorSocketTransport` usually
        # protocol will be an instance of `StreamReaderProtocol`
        # save transport's original protocol so it can be restored after
        # sendfile operation completes
        self._proto = transp.get_protocol()

        # transport isn't currently paused reading (underlying socket
        # is not being polled for read events by loop's selector) or
        # isn't closing
        self._should_resume_reading = transp.is_reading()

        # `._protocol_paused` is declared by `transports._FlowControlMixin`,
        # will be True if current protocol `self._proto` is currently paused
        # writing. If was paused before sendfile, original protocol will
        # be notified to resume writing after sendfile completes. This is correct
        # because sendfile will not be triggered until transport's write buffer
        # doesn't drop below the high watermark
        # (will wait via `self.drain` method) and thus after sendfile completes
        # transport's write buffer will still be below the high watermark and
        # thus the original protocol `self._proto` can resume writing
        self._should_resume_writing = transp._protocol_paused

        # if transport isn't already paused or closing notify it to pause
        # reading (will unregister the transport's underlying socket fd from
        # being polled for read events by loop's selector)
        transp.pause_reading()

        # replace original transport's protocol with this protocol
        transp.set_protocol(self)

        # If original protocol was paused writing, this means that transport's
        # write buffer is over the high watermark. This future will be used
        # to block on the first call to `await self.drain()` until transport's
        # buffer drops bellow the high watermark.
        if self._should_resume_writing:
            self._write_ready_fut = self._transport._loop.create_future()
        else:
            self._write_ready_fut = None

    async def drain(self):
        """
        Called/awaited before issuing sendfile to block until transport's
        write buffer drops under high watermark , at this point the
        transport will  call `.resume_writing` on this protocol, which
        will complete `self._write_ready_fut` effectively unblocking this call.
        """
        if self._transport.is_closing():
            raise ConnectionError("Connection closed by peer")
        fut = self._write_ready_fut
        if fut is None:
            return
        await fut

    def connection_made(self, transport):
        raise RuntimeError("Invalid state: "
                           "connection should have been established already.")

    def connection_lost(self, exc):
        """
        Called by transport and will set an exception on `self._write_ready_fut`
        thus resulting in calls to `await self.drain()` to raise an exception

        `.connection_lost` will also be called with the exception on the original
        protocol `self._proto`
        """
        if self._write_ready_fut is not None:
            # Never happens if peer disconnects after sending the whole content
            # Thus disconnection is always an exception from user perspective
            if exc is None:
                self._write_ready_fut.set_exception(
                    ConnectionError("Connection is closed by peer"))
            else:
                self._write_ready_fut.set_exception(exc)
        self._proto.connection_lost(exc)

    def pause_writing(self):
        """
        Called by transport if it's write buffer goes above the high watermark,
        so when `.drain` is called on this protocol it will pause/block until the
        buffer goes under the high water mark
        """
        if self._write_ready_fut is not None:
            return
        self._write_ready_fut = self._transport._loop.create_future()

    def resume_writing(self):
        """
        Called by transport on this protocol when its write buffer goes below
        high water mark, this will this protocol's calls to `await self.drain()`
        """
        if self._write_ready_fut is None:
            return
        self._write_ready_fut.set_result(False)
        self._write_ready_fut = None

    def data_received(self, data):
        raise RuntimeError("Invalid state: reading should be paused")

    def eof_received(self):
        raise RuntimeError("Invalid state: reading should be paused")

    async def restore(self):
        """
        Called after sendfile fallback completes.
        1) Restores original protocol on transport
        2) Notifies transport to resume reading if it wasn't paused before
           sendfile
        3) If original protocol was paused writing before sendfile, resumes it,
           because after sendfile completes transport's write buffer
           is guaranteed to be below the high watermark and thus the original
            protocol `self._proto` can resume writing.
        """
        # restore original protocol for transport
        self._transport.set_protocol(self._proto)
        # if transport wasn't paused reading before sendfile, resume it
        if self._should_resume_reading:
            self._transport.resume_reading()
        if self._write_ready_fut is not None:
            # Cancel the future.
            # Basically it has no effect because protocol is switched back,
            # no code should wait for it anymore.
            self._write_ready_fut.cancel()
        # after sendfile completes transport's write buffer will be below
        # the high watermark (guaranteed) and thus the original
        # protocol `self._proto` can resume writing
        if self._should_resume_writing:
            self._proto.resume_writing()


class Server(events.AbstractServer):

    def __init__(self, loop, sockets, protocol_factory, ssl_context, backlog,
                 ssl_handshake_timeout):
        self._loop = loop
        self._sockets = sockets
        self._active_count = 0
        self._waiters = []
        self._protocol_factory = protocol_factory
        self._backlog = backlog
        self._ssl_context = ssl_context
        self._ssl_handshake_timeout = ssl_handshake_timeout
        self._serving = False
        self._serving_forever_fut = None

    def __repr__(self):
        return f'<{self.__class__.__name__} sockets={self.sockets!r}>'

    def _attach(self):
        """
        Keep track of the number of currently active client/transport 
        connections. Is used for waiting until there are no more active
        client connections on server close.
        """
        # ensure server is still serving
        assert self._sockets is not None
        self._active_count += 1

    def _detach(self):
        """
        Called on each client/transport disconnect, if a server close was
        requested and there are no more active client connections - unblocks
        all calls to `await .wait_closed()`.
        """
        # ensure that there still exists at least one client connection,
        # the one that is currently being closed
        assert self._active_count > 0
        self._active_count -= 1
        if self._active_count == 0 and self._sockets is None:
            self._wakeup()

    def _wakeup(self):
        """
        Is called either by `self.close()` or by `self._detach()`, only called
        when a server close was requested and there are no more active client
        connections. Unblocks all calls to `await .wait_closed()`. 
        """
        waiters = self._waiters
        self._waiters = None
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(waiter)

    def _start_serving(self):
        """
        If this server is already serving - return immediately

        Else set `self._serving` to True

        For every socket on which the user wants to listen on:
            1) Start listening on socket with backlog provided by user

            2) `self._loop._start_serving` arranges for socket to be polled
                for read events by loop's selector (`self._add_reader` method)

            3) Every loop iteration if a read event occurs on the listening
               socket a callback wil be called - loop's `._accept_connection()`
               method

            4) loop's `._accept_connection()` method will continuously call
               `socket.accept()` for as many times as the size of the socket's
                backlog, when there are no more connections waiting for
                an .accept(), will break from the loop and return

            5) For every successful `.accept()` a client connection/socket will
               be returned

            6) `._accept_connection()` will set the client's socket to
               non-blocking mode, add client's address to `extra` data which
               will be later passed on to a socket transport, will create a task
               from the loop's `._accept_connection2()` method which will
               be called during loop's next iteration

            7) loop's `._accept_connection2()` method will instantiate a
               protocol using the protocol factory provided by the user
               (for example will instantiate a StreamReaderProtocol)

            8) after that will create a waiter future and instantiate a
               `_SelectorSocketTransport` passing it the waiter future

            9) `_SelectorSocketTransport` upon instantiation among other things
               will store a reference to this server in a instance attribute,
               will call `._attach()` on this server which will increment
               the number of active transports associated with this server
               by 1 (self._active_count += 1)

            10) Will disable the Nagle algorithm on the client's socket -
                small writes will be sent without waiting for the TCP ACK.
                This generally decreases the latency

            11) Will arrange for protocol's `.connection_made` method to be
                called during loop's next iteration

            12) protocol's `.connection_made` method if a client connected
                callback was provided by the protocol factory will create
                a task from it passing it a StreamReader and StreamWriter
                instance associated with the protocol. So the client connected
                callback provided by the user can read and write to client's
                socket using the provided StreamReader and StreamWriter.

            13) `_SelectorSocketTransport` will also arrange after
                protocol's `.connection_made` is called for the client's socket
                to be polled for read events by the loop's selector

            14) For every read event on the client's socket, transport's
                `._read_ready__data_received()` callback method will be called

            15) transport's `._read_ready__data_received()` method will try
                to receive data from the socket and then call protocol's
                `.data_received` method with the received data, which in turn
                will feed the data to a StreamReader instance
                (the one that was provided to the user's client connected
                callback)

            16) When received data is fed to the StreamReader instance, user
                calls to
                await `.read()`/`.readline()`/`.readuntil()`/`.readexactly()`
                on the StreamReader in the client connected callback will
                unblock and return the received data to the user

            17) After protocol's `.connection_made` was called and client's
                socket was registered to be polled for read events transport
                will unblock any calls to `await waiter_future` by completing
                it

            18) So loop's `._accept_connection2()` task will complete when
                protocol's `.connection_made` was called and client's
                socket was registered to be polled for read events, because
                `._accept_connection2()` blocked on awaiting the waiter
                future after it instantiated the socket transport
        """
        if self._serving:
            return

        self._serving = True

        for sock in self._sockets:
            sock.listen(self._backlog)

            self._loop._start_serving(
                self._protocol_factory, sock, self._ssl_context,
                self, self._backlog, self._ssl_handshake_timeout
            )

    def get_loop(self):
        return self._loop

    def is_serving(self):
        return self._serving

    @property
    def sockets(self):
        """
        Used for providing socket repr via TransportSocket wrapper which bans
        all potentially disruptive operations (like "socket.close()") .
        """
        if self._sockets is None:
            return ()
        return tuple(trsock.TransportSocket(s) for s in self._sockets)

    def close(self):
        """
        If server isn't already closed:

        1) Unregisters all listening sockets from being polled for
          read events/new incoming client connections and closes these sockets.

        2) Will arrange for user calls to `await .server_forever()` to unblock
           by cancelling `self._serving_forever_fut`

        3) If at this point there are no more active client connections, unblocks
           all user calls to `await .wait_closed()`

        4) If there are still active client connections, calls to
           `await .wait_closed()` will be unblocked only when the number of
           active client connections drops to 0. This is done via
           `self._detach()` which is called by the transport on each client
           connection loss and which will complete all waiter futures
           when the number of active client connections drops to 0, effectively
           unblocking calls to `await .wait_closed()`.
        """
        sockets = self._sockets
        # no listening sockets, server already closed
        if sockets is None:
            return
        self._sockets = None

        # unregister every listening socket from being polled for read events
        # by loop's selector, close socket afterwards
        for sock in sockets:
            self._loop._stop_serving(sock)

        self._serving = False

        # will be true if serving is cancelled via this method,
        # we cancel it to unblock call to `await .server_forever()`
        if (self._serving_forever_fut is not None and
            not self._serving_forever_fut.done()):
            self._serving_forever_fut.cancel()
            self._serving_forever_fut = None

        # if number of currently still active client connections represented by
        # socket transports is 0,
        # complete all waiter futures which will unblock all calls to
        # `await self.wait_closed()`
        # if there are some clients still connected and communicating, calls
        # to `await self.wait_closed()` will be unblocked later when all
        # the clients disconnect
        # (this server isn't considered closed until there are no more active
        # client connections),
        # (calls to `await self.wait_closed()` will be unblocked via call to
        # `self._detach()`, which will complete all
        # waiter futures when active client connection count drops to 0,
        # `self._detach()` is called by the transport when client connection
        # is lost)

        # This is why the api is:
        #   `.close()` => `await .wait_closed()` and NOT `await .close()`,
        # because there may be still active client connections after `.close()`
        # completes
        if self._active_count == 0:
            self._wakeup()

    async def start_serving(self):
        """
        For detailed docstring check out `self._start_serving()` method
        """
        self._start_serving()
        # Skip one loop iteration so that all 'loop.add_reader'
        # go through.
        await tasks.sleep(0)

    async def serve_forever(self):
        """
        1) Starts serving - for detailed docstring check out 
          `self._start_serving()` method
          
        2) Blocks on awaiting `self._serving_forever_fut` which will be cancelled
           by a call to `self.close()` or some other means
           
        3) Blocks until there are no more active client connections
        """
        if self._serving_forever_fut is not None:
            raise RuntimeError(
                f'server {self!r} is already being awaited on serve_forever()'
            )

        # there are no listening sockets
        if self._sockets is None:
            raise RuntimeError(f'server {self!r} is closed')

        # for detailed docstring check out `self._start_serving()` method
        self._start_serving()
        # will be cancelled either by a call to `self.close()` or something
        # completely different, this will unblock call to
        # `await .serve_forever()`
        self._serving_forever_fut = self._loop.create_future()

        # block until `self.close()` is called or the future is cancelled by
        # some other means
        try:
            await self._serving_forever_fut
        except exceptions.CancelledError:
            try:
                self.close()
                # block until there are no more active client connections
                await self.wait_closed()
            finally:
                raise
        finally:
            self._serving_forever_fut = None

    async def wait_closed(self):
        """
        Wait until all listening sockets are no longer polled for read events
        and closed, also wait until all active client connections have finished
        their communication and are closed.

        This call will be unblocked either by a call to `self.close()` if at
        that time there are no more active client connections, or by
        call to `self._detach()` when number of active client connections
        drops to 0 (`self._detach()` is called for every disconnect of a
        transport, so the last transport to disconnect will effectively unblock
        all calls to `await self.wait_closed()`)
        """
        # if server is already closed return immediately
        if self._sockets is None or self._waiters is None:
            return

        waiter = self._loop.create_future()
        self._waiters.append(waiter)
        await waiter


class BaseEventLoop(events.AbstractEventLoop):

    def __init__(self):
        self._timer_cancelled_count = 0
        self._closed = False
        self._stopping = False
        self._ready = collections.deque()
        self._scheduled = []
        self._default_executor = None
        self._internal_fds = 0
        # Identifier of the thread running the event loop, or None if the
        # event loop is not running
        self._thread_id = None
        self._clock_resolution = time.get_clock_info('monotonic').resolution
        self._exception_handler = None
        self.set_debug(coroutines._is_debug_mode())
        # In debug mode, if the execution of a callback or a step of a task
        # exceed this duration in seconds, the slow callback/task is logged.
        self.slow_callback_duration = 0.1
        self._current_handle = None
        self._task_factory = None
        self._coroutine_origin_tracking_enabled = False
        self._coroutine_origin_tracking_saved_depth = None

        # A weak set of all asynchronous generators that are
        # being iterated by the loop.
        self._asyncgens = weakref.WeakSet()
        # Set to True when `loop.shutdown_asyncgens` is called.
        self._asyncgens_shutdown_called = False
        # Set to True when `loop.shutdown_default_executor` is called.
        self._executor_shutdown_called = False

    def __repr__(self):
        return (
            f'<{self.__class__.__name__} running={self.is_running()} '
            f'closed={self.is_closed()} debug={self.get_debug()}>'
        )

    def create_future(self):
        """Create a Future object attached to the loop."""
        return futures.Future(loop=self)

    def create_task(self, coro, *, name=None):
        """Schedule a coroutine object.

        Return a task object.
        """
        self._check_closed()
        if self._task_factory is None:
            # this will call the loop's .call_soon() on the task's
            # .__step method, so the task's .__step method will be called
            # as soon as possible after it was created
            # (during next loop iteration or currently running iteration if
            # self._ready deque is still not processed
            # during this iteration of the loop)
            # If a future will be yielded from the task's coro when calling
            # .__step (coro.send(None))
            # then task's .__wakeup method will be added to the future's
            # done callbacks and will be called by the loop when the future
            # will be done. .__wakeup will call .__step under the hood
            # and if a result was returned via StopIteration it will
            # be set on the task
            task = tasks.Task(coro, loop=self, name=name)
            # only in _DEBUG mode
            if task._source_traceback:
                del task._source_traceback[-1]
        else:
            # the call of task's .__step depends on the _task_factory
            # implementation, but most likely it will also be scheduled
            # with loop's .call_soon() and thus called as soon as possible
            task = self._task_factory(self, coro)
            tasks._set_task_name(task, name)

        return task

    def set_task_factory(self, factory):
        """Set a task factory that will be used by loop.create_task().

        If factory is None the default task factory will be set.

        If factory is a callable, it should have a signature matching
        '(loop, coro)', where 'loop' will be a reference to the active
        event loop, 'coro' will be a coroutine object.  The callable
        must return a Future.
        """
        if factory is not None and not callable(factory):
            raise TypeError('task factory must be a callable or None')
        self._task_factory = factory

    def get_task_factory(self):
        """Return a task factory, or None if the default one is in use."""
        return self._task_factory

    def _make_socket_transport(self, sock, protocol, waiter=None, *,
                               extra=None, server=None):
        """Create socket transport."""
        raise NotImplementedError

    def _make_ssl_transport(
        self, rawsock, protocol, sslcontext, waiter=None,
        *, server_side=False, server_hostname=None,
        extra=None, server=None,
        ssl_handshake_timeout=None,
        call_connection_made=True):
        """Create SSL transport."""
        raise NotImplementedError

    def _make_datagram_transport(self, sock, protocol,
                                 address=None, waiter=None, extra=None):
        """Create datagram transport."""
        raise NotImplementedError

    def _make_read_pipe_transport(self, pipe, protocol, waiter=None,
                                  extra=None):
        """Create read pipe transport."""
        raise NotImplementedError

    def _make_write_pipe_transport(self, pipe, protocol, waiter=None,
                                   extra=None):
        """Create write pipe transport."""
        raise NotImplementedError

    async def _make_subprocess_transport(self, protocol, args, shell,
                                         stdin, stdout, stderr, bufsize,
                                         extra=None, **kwargs):
        """Create subprocess transport."""
        raise NotImplementedError

    def _write_to_self(self):
        """Write a byte to self-pipe, to wake up the event loop.

        This may be called from a different thread.

        The subclass is responsible for implementing the self-pipe.
        """
        raise NotImplementedError

    def _process_events(self, event_list):
        """Process selector events."""
        raise NotImplementedError

    def _check_closed(self):
        if self._closed:
            raise RuntimeError('Event loop is closed')

    def _check_default_executor(self):
        if self._executor_shutdown_called:
            raise RuntimeError('Executor shutdown has been called')

    def _asyncgen_finalizer_hook(self, agen):
        self._asyncgens.discard(agen)
        if not self.is_closed():
            # can be called from a different thread and the Handle
            # will be run during next iteration of the event loop
            # creating a Task and scheduling it to run (also in a Handle)
            self.call_soon_threadsafe(self.create_task, agen.aclose())

    def _asyncgen_firstiter_hook(self, agen):
        if self._asyncgens_shutdown_called:
            warnings.warn(
                f"asynchronous generator {agen!r} was scheduled after "
                f"loop.shutdown_asyncgens() call",
                ResourceWarning, source=self)

        self._asyncgens.add(agen)

    async def shutdown_asyncgens(self):
        """Shutdown all active asynchronous generators."""
        self._asyncgens_shutdown_called = True

        if not len(self._asyncgens):
            # If Python version is <3.6 or we don't have any asynchronous
            # generators alive.
            return

        closing_agens = list(self._asyncgens)
        self._asyncgens.clear()

        # await the _GatheringFuture which will complete when all the
        # Tasks created from ag.aclose() coros will be done
        results = await tasks.gather(
            *[ag.aclose() for ag in closing_agens],
            return_exceptions=True)

        # is some of the attempts to close async gens caused exceptions
        # signal them (usually just logs the fact with some useful info)
        for result, agen in zip(results, closing_agens):
            if isinstance(result, Exception):
                self.call_exception_handler({
                    'message': f'an error occurred during closing of '
                               f'asynchronous generator {agen!r}',
                    'exception': result,
                    'asyncgen': agen
                })

    # TODO !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
    #  revise doc string
    async def shutdown_default_executor(self):
        """
        Schedule the shutdown of the default executor.

        Creates a future, executes self._do_shutdown in a separate thread
        which waits for the executor to shutdown and after that schedules
        result/exception to be set on the future.

        Wait fot the future to be done.

        By default is called by asyncio.run on cleanup
        (happy path without exception handling details):
        1) It wraps this coroutine in a Task and schedules it
           so when the task gets run for
            the first time the future will be propagated to
            Task's .__step ( method on the 'await future' statement
            (via coro.send(None))).

        2) Task's .__wakeup method will be added to this future as
           a done callback.

        3) when the thread schedules result/exception to be set
           on this future and after it is set the .__wakeup
           method will be executed as a callback and it in turn will call
           .__step again.

        4) .__step will resume (via coro.send(None)) the 'awaiting future'
            and finally will join on the thread.

        5) .__step will catch the StopIteration exception and set None
           as the result of the task
        """
        self._executor_shutdown_called = True
        if self._default_executor is None:
            return
        future = self.create_future()
        thread = threading.Thread(target=self._do_shutdown, args=(future,))
        thread.start()
        try:
            await future
        finally:
            thread.join()

    def _do_shutdown(self, future):
        """
        Wait for the executor to shut down and set the result or exception
        on the future passed in (via the event loop of course)
        """
        try:
            self._default_executor.shutdown(wait=True)
            self.call_soon_threadsafe(future.set_result, None)
        except Exception as ex:
            self.call_soon_threadsafe(future.set_exception, ex)

    def _check_running(self):
        if self.is_running():
            raise RuntimeError('This event loop is already running')
        if events._get_running_loop() is not None:
            raise RuntimeError(
                'Cannot run the event loop while another loop is running')

    def run_forever(self):
        """
        Run until stop() is called.

        Continuously calls self._run_once() which represents a single
        cycle of the Event Loop.
        Does this until the loop is marked as stopping

        In the end sets the event loop within the C level event loop holder
        to None (and in C cache variables also)
        """
        self._check_closed()
        self._check_running()
        # so origin tracking will be enables only in _DEBUG mode
        self._set_coroutine_origin_tracking(self._debug)
        self._thread_id = threading.get_ident()

        old_agen_hooks = sys.get_asyncgen_hooks()

        # sys.set_asyncgen_hooks(...)
        # Accepts two optional keyword arguments which are callables that accept
        # an asynchronous generator iterator as an argument.

        # 1)
        # The firstiter callable will be called when an asynchronous
        # generator is iterated for the first time. (in our case it will be added
        # to this loop's self._asyncgens = weakref.WeakSet())

        # 2)
        # The finalizer will be called when an asynchronous generator
        # is about to be garbage collected.
        # (in our case will remove it from the weakref set and schedule it to be
        # closed during this loop's next iteration)
        sys.set_asyncgen_hooks(firstiter=self._asyncgen_firstiter_hook,
                               finalizer=self._asyncgen_finalizer_hook)

        try:
            events._set_running_loop(self)
            while True:
                self._run_once()
                if self._stopping:
                    break
        finally:
            self._stopping = False
            self._thread_id = None
            events._set_running_loop(None)
            self._set_coroutine_origin_tracking(False)
            sys.set_asyncgen_hooks(*old_agen_hooks)

    def run_until_complete(self, future):
        """Run until the Future is done.

        If the argument is a coroutine, it is wrapped in a Task.

        WARNING: It would be disastrous to call run_until_complete()
        with the same coroutine twice -- it would wrap it in two
        different Tasks and that can't be good (because 2 different Tasks
        will call coro.send(None)/coro.throw(exc) on it. So the coroutine
        could raise a StopIteration before the operation represented
        by it has completed or has been cancelled)

        Return the Future's result, or raise its exception.

        Details (not taking exceptions and cancellations in to account):

        1) If a coroutine is passed in then creates a Task from it
           which will schedule it's .__step method to be run by this loop
           during it's next iteration (the .__step method will be wrapped
           in an events.Handle which has a ._run method to run the callable)

        2) Add a done callback to the Task which will mark this loop as stopping
           which in turn will make the self.run_forever() loop break and exit

        3) Call the .run_forever() method of this loop which will continuously
           call this loop's ._run_once() method which represents a single
           full iteration of this event loop.

        4) During the first iteration of this loop the Task's .__step method
           will be called.

        5) The .__step method will call coro.send(None)

        6) As a result of the previous step the inner most future will be yielded
           to the .__step method through the 'await/yield from' conduit

        7) The.__step method will store the yield Future in self._fut_waiter

        8) The Task's .__wakeup method will be attached to the Future
           as a done callback, so when the Future will be marked completed
           the .__wakeup method will be called

        9) While the Future is not marked as completed, this loop will just waist
           it's cycles doing nothing if nothing else is scheduled as a result
           of the previous coro.send(None).

        10) When the future is done (someone called .set_result or .set_exception
           on it) Task's .__wakeup method will be executed as a done callback

        11) Task's .__wakeup method will call the Task's .__step method

        12) The Task's .__step method  will again call coro.send(None)
            and the Future's .result() will bubble up the yield from/await
            conduit possibly modified or swapped out on the way up to .__step.
            (if there were no more 'awaits/yield froms' along the way.
            If there were then the result will be just returned to the awaiting
            coroutine and it's next await will repeat step 6 - 11)

        13) .__step will get the possibly modified or swapped out result
           of the future by inspecting the  .value attribute of the
           StopIteration exception.

        14) After step gets the result from StopIteration it will call
            .set_result on itself which will set the result and call all
            registered done callbacks

        15) The only (one of) done callback registered was the one that
            marks this loop as stopping which in turn will stop the
            .run_forever() method by breaking from its infinite while loop
            and return.

        16) After that the Task's .result() will be returned to the caller.
        """
        self._check_closed()
        self._check_running()

        # if the passed in 'future' was not a Future already
        # then this flag will be set to True,

        # if the passed in 'future' was already a Future,
        # this flag will be set to False
        new_task = not futures.isfuture(future)

        # if a coroutine was passed in, then ensure future
        # will create a task from it attached to this loop,
        # when the task will be created it will also schedule it's .__step
        # method to be run during the next or current iteration of this loop.
        # So when a couple of lines below when self.run_forever() is called
        # the task's .__step method will be called during the first iteration.

        # This callback will mark this loop as stopping therefore forcing
        # self.run_forever() to break and exit
        future = tasks.ensure_future(future, loop=self)

        if new_task:
            # An exception is raised if the future didn't complete, so there
            # is no need to log the "destroy pending task" message
            future._log_destroy_pending = False

        # add a callback to the Task which will run when the task completes.
        # Task will complete when the future yielded to it via coro.send(None)
        # completes and the Task's .__wakeup method will be called which
        # will call .__step again and set the result received from
        # StopIteration on itself during another coro.send(None) call.
        future.add_done_callback(_run_until_complete_cb)

        # run iteration of the loop the number of times required to
        # complete the task (until the loop is stopped by the above callback)
        try:
            self.run_forever()
        except:
            if new_task and future.done() and not future.cancelled():
                # The coroutine raised a BaseException. Consume the exception
                # to not log a warning, the caller doesn't have access to the
                # local task.
                # The set future's .__log_traceback to False, so the traceback
                # will not be logged at the time this Task/Future
                # is garbage collected
                future.exception()
            raise
        finally:
            future.remove_done_callback(_run_until_complete_cb)

        if not future.done():
            raise RuntimeError('Event loop stopped before Future completed.')

        return future.result()

    def stop(self):
        """Stop running the event loop.

        Every callback already scheduled will still run.  This simply informs
        run_forever to stop looping after a complete iteration.
        """
        self._stopping = True

    def close(self):
        """Close the event loop.

        This clears the queues (ready and scheduled (for delayed TimerHandles)
        and shuts down the executor,
        but does not wait for the executor to finish.

        The event loop must not be running.
        """
        if self.is_running():
            raise RuntimeError("Cannot close a running event loop")
        if self._closed:
            return
        if self._debug:
            logger.debug("Close %r", self)
        self._closed = True
        self._ready.clear()
        self._scheduled.clear()
        self._executor_shutdown_called = True
        executor = self._default_executor
        if executor is not None:
            self._default_executor = None
            executor.shutdown(wait=False)

    def is_closed(self):
        """Returns True if the event loop was closed."""
        return self._closed

    def __del__(self, _warn=warnings.warn):
        """
        If event loop was not closed before GC, log a warning,
        if it is not running close it.
        """
        if not self.is_closed():
            _warn(f"unclosed event loop {self!r}", ResourceWarning, source=self)
            if not self.is_running():
                self.close()

    def is_running(self):
        """Returns True if the event loop is running."""
        return self._thread_id is not None

    def time(self):
        """Return the time according to the event loop's clock.

        This is a float expressed in seconds since an epoch, but the
        epoch, precision, accuracy and drift are unspecified and may
        differ per event loop.
        """
        return time.monotonic()

    def call_later(self, delay, callback, *args, context=None):
        """Arrange for a callback to be called at a given time.

        Return a Handle: an opaque object with a cancel() method that
        can be used to cancel the call.

        The delay can be an int or float, expressed in seconds.  It is
        always relative to the current time.

        Each callback will be called exactly once.  If two callbacks
        are scheduled for exactly the same time, it undefined which
        will be called first.

        Any positional arguments after the callback will be passed to
        the callback when it is called.
        """
        # timer inherits from events.Handler which has a ._run method
        # which will call the callback within
        # the passed in context and with provided args
        # if an exception occurs (other than SystemExit, KeyboardInterrupt)
        # it will call loop's .call_exception_handler method with some context
        # about the occurred exception.
        # Additionally timer has a .when method which returns the 'when'
        # parameter passed in on init (usually some time.monotonic timestamp).
        # Also adds functionality to its .cancel method which indirectly
        # increments loop's ._timer_cancelled_count by 1 if the timer handle
        # is marked as scheduled (via loop's ._timer_handle_cancelled)
        # Also implements rich comparison methods.

        # Also self.call_at will add timer to self._scheduled list and heapify
        # it so that
        # the timer handle to be called soonest will be retrieved first
        # by heappop (timer handler's rich comparison operators compare it's
        #  ._when attr)
        # Basically a priority queue

        # Also will mark the timer handle as scheduled
        timer = self.call_at(self.time() + delay, callback, *args,
                             context=context)
        # only in _DEBUG mode
        if timer._source_traceback:
            del timer._source_traceback[-1]
        return timer

    def call_at(self, when, callback, *args, context=None):
        """Like call_later(), but uses an absolute time.

        Absolute time corresponds to the event loop's time() method.
        """
        self._check_closed()
        if self._debug:
            # check that .call_at is called by the thread running the loop
            self._check_thread()
            # check that the callback provided is a callable and not a coroutine
            # or coroutine function
            self._check_callback(callback, 'call_at')
        # timer inherits from events.Handler which has a ._run method
        # which will call the callback within
        # the passed in context and with provided args
        # if an exception occurs (other than SystemExit, KeyboardInterrupt)
        # it will call loop's .call_exception_handler method with some context
        # about the occurred exception.
        # Additionally timer has a .when method which returns the 'when'
        # parameter passed in on init (usually some time.monotonic timestamp).
        # Also adds functionality to its .cancel method which indirectly
        # increments loop's ._timer_cancelled_count by 1 if the timer handle
        # is marked as scheduled (via loop's ._timer_handle_cancelled)
        # Also implements rich comparison methods.
        timer = events.TimerHandle(when, callback, args, self, context)
        # only in _DEBUG mode
        if timer._source_traceback:
            del timer._source_traceback[-1]
        # add timer to self._scheduled list and heapify it so that
        # the timer handle to be called soonest will be retrieved first
        # by heappop (timer handler's rich comparison operators compare it's
        # ._when attr)
        # Basically a priority queue
        heapq.heappush(self._scheduled, timer)
        # mark the timer handle as scheduled
        timer._scheduled = True
        return timer

    def call_soon(self, callback, *args, context=None):
        """Arrange for a callback to be called as soon as possible.

        This operates as a FIFO queue: callbacks are called in the
        order in which they are registered.  Each callback will be
        called exactly once.

        Any positional arguments after the callback will be passed to
        the callback when it is called.
        """
        self._check_closed()
        if self._debug:
            # check that .call_soon is called by the thread running the loop
            self._check_thread()
            # check that the callback provided is a callable and not a coroutine
            # or coroutine function
            self._check_callback(callback, 'call_soon')
        # handle has a ._run method which will call the callback within
        # the passed in context and with provided args
        # if an exception occurs (other than SystemExit, KeyboardInterrupt)
        # it will call loop's .call_exception_handler method with some context
        # about the occurred exception.
        # The handle was also added to self._ready by ._call_soon
        handle = self._call_soon(callback, args, context)
        # only in _DEBUG mode
        if handle._source_traceback:
            del handle._source_traceback[-1]
        return handle

    def _check_callback(self, callback, method):
        if (coroutines.iscoroutine(callback) or
            coroutines.iscoroutinefunction(callback)):
            raise TypeError(
                f"coroutines cannot be used with {method}()")
        if not callable(callback):
            raise TypeError(
                f'a callable object was expected by {method}(), '
                f'got {callback!r}')

    def _call_soon(self, callback, args, context):
        # handle has a ._run method which will call the callback within
        # the passed in context and with provided args
        # if an exception occurs (other than SystemExit, KeyboardInterrupt)
        # it will call loop's .call_exception_handler method with some context
        # about the occurred exception
        handle = events.Handle(callback, args, self, context)
        # only in _DEBUG mode
        if handle._source_traceback:
            del handle._source_traceback[-1]
        self._ready.append(handle)
        return handle

    def _check_thread(self):
        """Check that the current thread is the thread running the event loop.

        Non-thread-safe methods of this class make this assumption and will
        likely behave incorrectly when the assumption is violated.

        Should only be called when (self._debug == True).  The caller is
        responsible for checking this condition for performance reasons.
        """
        if self._thread_id is None:
            return
        thread_id = threading.get_ident()
        if thread_id != self._thread_id:
            raise RuntimeError(
                "Non-thread-safe operation invoked on an event loop other "
                "than the current one")

    def call_soon_threadsafe(self, callback, *args, context=None):
        """
        Like call_soon(), but thread-safe.

        Will not check that it's called from the same thread that is running
        the loop

        So basically we can add handle to loop's ._ready deque from a different
        thread (not where the loop is running) and wake up the loop
        bu calling self._write_to_self().
        """
        self._check_closed()
        if self._debug:
            # check that the callback provided is a callable and not a coroutine
            # or coroutine function
            self._check_callback(callback, 'call_soon_threadsafe')
        # handle has a ._run method which will call the callback within
        # the passed in context and with provided args
        # if an exception occurs (other than SystemExit, KeyboardInterrupt)
        # it will call loop's .call_exception_handler method with some context
        # about the occurred exception.
        # The handle was also added to self._ready by ._call_soon
        handle = self._call_soon(callback, args, context)
        # only in _DEBUG mode
        if handle._source_traceback:
            del handle._source_traceback[-1]
        # Write a byte to self-pipe, to wake up the event loop.
        # This may be called from a different thread.
        self._write_to_self()
        return handle

    def run_in_executor(self, executor, func, *args):
        self._check_closed()
        if self._debug:
            # check that the callback provided is a callable and not a coroutine
            # or coroutine function
            self._check_callback(func, 'run_in_executor')
        if executor is None:
            executor = self._default_executor
            # Only check when the default executor is being used
            self._check_default_executor()
            if executor is None:
                executor = concurrent.futures.ThreadPoolExecutor(
                    thread_name_prefix='asyncio'
                )
                self._default_executor = executor
        # 1) executor.submit will return a concurrent.futures.Future

        # 2) futures.wrap_future will take the returned concurrent.futures.Future

        # 3) will create a new future bound to this loop by calling this loop's
        #    .create_future() method

        # 4) will add a done callback to the newly created asyncio.Future
        #     instance (via its own .add_done_callback method).
        #     When the returned (from futures.wrap_future) newly created
        #     asyncio.Future is done the callback will be called:
        #     - if the asyncio.Future was cancelled the concurrent.futures.Future
        #       will also be cancelled by calling .cancel() on it directly
        #       (can call directly because concurrent.futures.Future doesn't
        #       have a loop attached to it)

        # 5) will add a done callback to the concurrent.futures.Future
        #  instance (via .add_done_callback method, concurrent.futures.Future
        #  also has this method defined).
        #  When the concurrent.futures.Future is done the callback will
        #  be called:
        #    - if the newly created asyncio.Future was cancelled and there is
        #      a loop attached to the asyncio.Future and the loop is closed
        #      then just return from this callback
        #    - concurrent.futures.Future state will be arranged to get copied to
        #      the newly created asyncio.Future during next loop iteration
        #      by calling this loop's
        #      .call_soon_threadsafe(_set_state, destination, source) method
        #    - how does the concurrent.futures.Future state gets copied to
        #      the asyncio.Future state:
        #         a) if the asyncio.Future was cancelled in between (
        #            .call_soon_threadsafe(func) is obviously not executed
        #             immediately), we just return
        #             (because the concurrent.futures.Future will get cancelled
        #              in asyncio.Future's done callback)
        #         b) we assert that the asyncio.Future is not already done
        #         c) if the source concurrent.futures.Future was cancelled
        #            we cancel the asyncio.Future
        #         d) if the source concurrent.futures.Future resulted in an
        #            exception we set this exception on the asyncio.Future
        #            (before that we convert the concurrent.futures exception
        #             type to the corresponding asyncio exception type)
        #               (after the exception will be set,
        #               all asyncio.Future callbacks will be scheduled to run
        #                  by the loop and it's state set to _FINISHED)
        #         e) if the source concurrent.futures.Future finished normally
        #            we get the result from it and set it on
        #            to the asyncio.Future (after the result will be set,
        #            all asyncio.Future callbacks will be scheduled to run
        #            by the loop and it's state set to _FINISHED)

        # IN A NUTSHELL:
        #  1) After the executor.submit's returned concurrent.futures.Future
        #     completes (with result, exception or cancelled) it's state
        #     will be copied to the returned asyncio.Future (returned by
        #     futures.wrap_future)
        #  2) If the returned asyncio.Future (returned by futures.wrap_future)
        #     gets cancelled, the executor.submit's
        #     returned concurrent.futures.Future will also get cancelled
        return futures.wrap_future(
            executor.submit(func, *args), loop=self)

    def set_default_executor(self, executor):
        if not isinstance(executor, concurrent.futures.ThreadPoolExecutor):
            warnings.warn(
                'Using the default executor that is not an instance of '
                'ThreadPoolExecutor is deprecated and will be prohibited '
                'in Python 3.9',
                DeprecationWarning, 2)
        self._default_executor = executor

    def _getaddrinfo_debug(self, host, port, family, type, proto, flags):
        msg = [f"{host}:{port!r}"]
        if family:
            msg.append(f'family={family!r}')
        if type:
            msg.append(f'type={type!r}')
        if proto:
            msg.append(f'proto={proto!r}')
        if flags:
            msg.append(f'flags={flags!r}')
        msg = ', '.join(msg)
        logger.debug('Get address info %s', msg)

        t0 = self.time()
        addrinfo = socket.getaddrinfo(host, port, family, type, proto, flags)
        dt = self.time() - t0

        msg = f'Getting address info {msg} took {dt * 1e3:.3f}ms: {addrinfo!r}'
        if dt >= self.slow_callback_duration:
            logger.info(msg)
        else:
            logger.debug(msg)
        return addrinfo

    async def getaddrinfo(
        self, host, port, *,
        family=0, type=0, proto=0, flags=0
    ):
        """
        Does a DNS request within a threadpool executor.
        Returns a list of 4 tuples (or 2 if IPv6 is not supported):
        2 tuples - TCP IPv4 and IPv6
        2 tuples - UDP IPv4 and IPv6

        Tuple example:
        (<AddressFamily.AF_INET: 2>, <SocketKind.SOCK_STREAM: 1>, 6,
        '', ('93.184.216.34', 80))
        """
        # getaddrinfo resolves host names via DNS
        # If we know a network service by host name like example.org or the IP
        # address of the network service either in form of IPv4 or IPv6 along
        # with the port number of the network service, getaddrinfo() will
        # return a list of tuples containing information about socket(s)
        # that can be created with the service.
        if self._debug:
            # just times the DNS request
            # and if it's slower than 1 millisecond,
            # logs the fact that a slow DNS resolution occurred
            getaddr_func = self._getaddrinfo_debug
        else:
            getaddr_func = socket.getaddrinfo

        # run in executor because the DNS request cannot be done in
        # non-blocking socket mode
        return await self.run_in_executor(
            None, getaddr_func, host, port, family, type, proto, flags)

    async def getnameinfo(self, sockaddr, flags=0):
        """
        Executes in threadpool `socket.getnameinfo` which translates a socket
        address sockaddr into a 2-tuple (host, port).
        Depending on the settings of flags, the result can contain a
        fully-qualified domain name or numeric address representation in host.
        Similarly, port can contain a string port name or a numeric port number.
        """
        return await self.run_in_executor(
            None, socket.getnameinfo, sockaddr, flags)

    # TODO !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
    #  EXAMINE THIS AGAIN AFTER RESEARCHING _UnixSelectorEventLoop -
    #  `_sock_sendfile_native` is implemented by it
    #  Add docstring about 'sendfile' in _UnixSelectorEventLoop  and 'sendfile'
    #  fallback
    async def sock_sendfile(self, sock, file, offset=0, count=None,
                            *, fallback=True):
        """
        Sendfile advantages:
        https://medium.com/swlh/linux-zero-copy-using-sendfile-75d2eb56b39b


        Using read/write without sendfile:
            File read from disk must go through kernel system cache —
            which resides in the kernel space, then the data is copied
            to userspace’s memory area before being written back to a
            new file/socket — which then in turn goes to kernel memory buffer
            before really flushed out to disk.
            The procedure takes quite many unnecessary operations of
            copying back and forth between kernel and userspace
            without actually doing anything, and the operations consume system
            resources and context switches as well.

        Using sendfile:
            sendfile() claims to make data transfer happening under kernel
            space only so there's no need for user space buffers and unnecessary
            context switches.

        Python 'socket' module:
            https://docs.python.org/3/library/socket.html#socket.socket.sendfile
            https://docs.python.org/3/library/os.html#os.sendfile
            socket.sendfile(file, offset=0, count=None) - Send a file until EOF
            is reached by using high-performance os.sendfile and return
            the total number of bytes which were sent. file must be a
            regular file object opened in binary mode.
        """
        if self._debug and sock.gettimeout() != 0:
            raise ValueError("the socket must be non-blocking")

        # validate params
        self._check_sendfile_params(sock, file, offset, count)

        try:
            # TODO - check out _UnixSelectorEventLoop implementation
            return await self._sock_sendfile_native(sock, file,
                                                    offset, count)
        except exceptions.SendfileNotAvailableError as exc:
            if not fallback:
                raise
        # use fallback if sendfile is not available
        # Isn't optimized like 'sendfile' which uses only kernel space for
        # copying.
        # Fallback uses kernel + user space for copying data from src file
        # to dst socket with the help of a memory view buffer
        return await self._sock_sendfile_fallback(sock, file,
                                                  offset, count)

    async def _sock_sendfile_fallback(self, sock, file, offset, count):
        """
        Fallback if OS doesn't support 'sendfile' or the provided types
        cannot be used with 'sendfile'. When using this we loose the 'sendfile'
        optimization of copying data from src to dst within kernel space.
        Here user space buffers are used.

        Overview:
            Copies data from file via a user-space buffer to socket

        1) If offset was provided seek in file to offset
        2) Block size to be sent in one go will be either the amount of data
           left to send or 256Kb (smallest of the 2)
        3) In a loop:
           - read data from file into memoryview buffer
           - send data over network from buffer
           - stop when all requested data is sent or EOF
        4) Seek in file to offset + total_sent
        """

        # go to offset in file provided by the user
        if offset:
            file.seek(offset)

        # Initial block size to transmit is either `count` bytes to send,
        # provided by our caller or 256Kb - we take the smallest value
        blocksize = (
            min(count, constants.SENDFILE_FALLBACK_READBUFFER_SIZE)
            if count else constants.SENDFILE_FALLBACK_READBUFFER_SIZE
        )

        # allocate buffer of 256Kb or `count` size
        buf = bytearray(blocksize)
        total_sent = 0

        try:
            while True:
                # if our caller doesn't want to sendfile until EOF,
                # update block size every iteration -  will be either
                # the number of bytes left to send or 256Kb (smallest of the 2)
                if count:
                    blocksize = min(count - total_sent, blocksize)
                    # If no more data needs to be sent - stop
                    if blocksize <= 0:
                        break

                # file data will be written into this view
                view = memoryview(buf)[:blocksize]
                # read data from file into buffer
                read = await self.run_in_executor(None, file.readinto, view)
                if not read:
                    break  # EOF
                # send data from buffer over network
                await self.sock_sendall(sock, view[:read])
                # update total amount sent
                total_sent += read
            return total_sent
        finally:
            # update position in file to be after all sent data
            if total_sent > 0 and hasattr(file, 'seek'):
                file.seek(offset + total_sent)

    # TODO - check out _UnixSelectorEventLoop implementation
    async def _sock_sendfile_native(self, sock, file, offset, count):
        # NB: sendfile syscall is not supported for SSL sockets and
        # non-mmap files even if sendfile is supported by OS
        raise exceptions.SendfileNotAvailableError(
            f"syscall sendfile is not available for socket {sock!r} "
            "and file {file!r} combination")

    def _check_sendfile_params(self, sock, file, offset, count):
        if 'b' not in getattr(file, 'mode', 'b'):
            raise ValueError("file should be opened in binary mode")

        if not sock.type == socket.SOCK_STREAM:
            raise ValueError("only SOCK_STREAM type sockets are supported")

        if count is not None:
            if not isinstance(count, int):
                raise TypeError(
                    "count must be a positive integer (got {!r})".format(count))
            if count <= 0:
                raise ValueError(
                    "count must be a positive integer (got {!r})".format(count))

        if not isinstance(offset, int):
            raise TypeError(
                "offset must be a non-negative integer (got {!r})".format(
                    offset))
        if offset < 0:
            raise ValueError(
                "offset must be a non-negative integer (got {!r})".format(
                    offset))

    async def _connect_sock(self, exceptions, addr_info, local_addr_infos=None):
        """
        Create, bind and connect one socket.

        Tries to async connect to a remote socket at address
        """
        my_exceptions = []
        exceptions.append(my_exceptions)
        family, type_, proto, _, address = addr_info
        sock = None

        try:
            sock = socket.socket(family=family, type=type_, proto=proto)
            sock.setblocking(False)

            # try to bind socket to every local address provided address
            if local_addr_infos is not None:
                for _, _, _, _, laddr in local_addr_infos:
                    try:
                        sock.bind(laddr)
                        break
                    except OSError as exc:
                        msg = (
                            f'error while attempting to bind on '
                            f'address {laddr!r}: '
                            f'{exc.strerror.lower()}'
                        )
                        exc = OSError(exc.errno, msg)
                        my_exceptions.append(exc)
                else:  # all bind attempts failed
                    raise my_exceptions.pop()
            # wait till socket is connected to a remote socket at address
            await self.sock_connect(sock, address)
            return sock
        except OSError as exc:
            my_exceptions.append(exc)
            if sock is not None:
                sock.close()
            raise
        except:
            if sock is not None:
                sock.close()
            raise

    async def create_connection(
        self, protocol_factory, host=None, port=None,
        *, ssl=None, family=0,
        proto=0, flags=0, sock=None,
        local_addr=None, server_hostname=None,
        ssl_handshake_timeout=None,
        happy_eyeballs_delay=None, interleave=None):
        """
        Connect to a TCP server.

        Create a streaming transport connection to a given Internet host and
        port: socket family AF_INET or socket.AF_INET6 depending on host (or
        family if specified), socket type SOCK_STREAM. protocol_factory must be
        a callable returning a protocol instance.

        This method is a coroutine which will try to establish the connection
        in the background.  When successful, the coroutine returns a
        (transport, protocol) pair.

        Happy Eyeballs is an algorithm that can make dual-stack applications
        more responsive to users by determining which transport
        would be better used for a particular connection by trying
        them both in parallel.

        The proposed approach is simple – if the client system is dual stack
        capable, then fire off connection attempts in both IPv4 and IPv6
        in parallel, and use (and remember) whichever protocol completes
        the connection sequence first.

        The user benefits because there is no wait time and the decision favours
        speed – whichever protocol performs the connection fastest for
        that particular end site is the protocol that is used
        to carry the payload.

        1) Tries to resolve IP address of host in thread pool. This will
           return a tuple of tuples consisting of information for IP/IPv6
           family socket creation and connection.

        2) Will create a flat list of connection infos grouped by family IP/IPv6
           from the above result

        3) Will try connecting to remote server with each connection info from
           the previous flat list. If `happy_eyeballs_delay` was provided
           by the user will do this concurrently with a delay of
           `happy_eyeballs_delay` between the connecting coros. The first
           connection attempt to successfully complete will be used as the socket

        4) Will create a transport and protocol with the socket using
           user-provided protocol factory. This will:
                - Set socket to non-blocking mode,
                - In case of Unix creates a `_SelectorSocketTransport` which
                  will call a user-provided client connected callback passing it
                  protocol's StreamReader and StreamWriter
                - Will register socket's fd to be polled for read events and
                  when they occur pass read data to the protocol which will
                  pass it on to StreamReader
                - Will block until the above operations complete, after that
                  will return the transport and protocol instances

        5) Returns transport and protocol instances
        """
        if server_hostname is not None and not ssl:
            raise ValueError('server_hostname is only meaningful with ssl')

        if server_hostname is None and ssl:
            # Use host as default for server_hostname.  It is an error
            # if host is empty or not set, e.g. when an
            # already-connected socket was passed or when only a port
            # is given.  To avoid this error, you can pass
            # server_hostname='' -- this will bypass the hostname
            # check.  (This also means that if host is a numeric
            # IP/IPv6 address, we will attempt to verify that exact
            # address; this will probably fail, but it is possible to
            # create a certificate for a specific IP address, so we
            # don't judge it here.)
            if not host:
                raise ValueError('You must set server_hostname '
                                 'when using ssl without a host')
            server_hostname = host

        if ssl_handshake_timeout is not None and not ssl:
            raise ValueError(
                'ssl_handshake_timeout is only meaningful with ssl')

        if happy_eyeballs_delay is not None and interleave is None:
            # If using happy eyeballs, default to interleave addresses by family
            # interleave - чередовать
            interleave = 1

        if host is not None or port is not None:
            if sock is not None:
                raise ValueError(
                    'host/port and sock can not be specified at the same time')

            # If IP is already resolved just returns info about it for socket
            # creation
            # If not yet resolved, does a DNS request in loop's threadpool
            # and returns info for socket creation (a tuple of tuples with info)
            # Example return value:
            #   (<AddressFamily.AF_INET: 2>, <SocketKind.SOCK_STREAM: 1>, 6,
            #     '', ('93.184.216.34', 80))
            infos = await self._ensure_resolved(
                (host, port), family=family,
                type=socket.SOCK_STREAM, proto=proto, flags=flags, loop=self)
            if not infos:
                raise OSError('getaddrinfo() returned empty list')

            # Try to do DNS resolving on local address
            if local_addr is not None:
                laddr_infos = await self._ensure_resolved(
                    local_addr, family=family,
                    type=socket.SOCK_STREAM, proto=proto,
                    flags=flags, loop=self)
                if not laddr_infos:
                    raise OSError('getaddrinfo() returned empty list')
            else:
                laddr_infos = None

            if interleave:
                # Creates and returns a flat list of address infos
                # (usually grouped by IP and IPv6), where each group by family
                # comes one after another. Example:
                #  [<addrinfo1 family1>, <addrinfo2 family1>,
                #   <addrinfo3 family2>, <addrinfo4 family2> and so on]
                infos = _interleave_addrinfos(infos, interleave)

            exceptions = []
            # not using happy eyeballs,
            # so try connecting to each address in address group (IP and IPv6)
            # one after another (in a loop), the last successful connect will
            # be the socket that's going to be used
            if happy_eyeballs_delay is None:
                for addrinfo in infos:
                    try:
                        # Tries to async connect to a remote socket at address
                        sock = await self._connect_sock(
                            exceptions, addrinfo, laddr_infos)
                        break
                    except OSError:
                        continue
            else:  # using happy eyeballs
                # This will try connecting to each remote socket at address
                # concurrently, every next connection attempt will be launched
                # with a delay of `happy_eyeballs_delay`.
                # The first connection attempt to succeed will cancel all
                # other attempts and the socket which this attempt created
                # is going to be used
                sock, _, _ = await staggered.staggered_race(
                    (functools.partial(self._connect_sock,
                                       exceptions, addrinfo, laddr_infos)
                     for addrinfo in infos),
                    happy_eyeballs_delay, loop=self)

            # if all connects were unsuccessful, raise either the first exception
            # if all exception are same, or a combined exception
            if sock is None:
                exceptions = [exc for sub in exceptions for exc in sub]
                if len(exceptions) == 1:
                    raise exceptions[0]
                else:
                    # If they all have the same str(), raise one.
                    model = str(exceptions[0])
                    if all(str(exc) == model for exc in exceptions):
                        raise exceptions[0]
                    # Raise a combined exception so the user can see all
                    # the various error messages.
                    raise OSError('Multiple exceptions: {}'.format(
                        ', '.join(str(exc) for exc in exceptions)))
        else:
            if sock is None:
                raise ValueError(
                    'host and port was not specified and no sock specified')
            if sock.type != socket.SOCK_STREAM:
                # We allow AF_INET, AF_INET6, AF_UNIX as long as they
                # are SOCK_STREAM.
                # We support passing AF_UNIX sockets even though we have
                # a dedicated API for that: create_unix_connection.
                # Disallowing AF_UNIX in this method, breaks backwards
                # compatibility.
                raise ValueError(
                    f'A Stream Socket was expected, got {sock!r}')

        # 1) Sets socket to non-blocking mode,
        # 2) In case of Unix creates a `_SelectorSocketTransport` which
        #    will call a user-provided client connected callback passing it
        #    protocol's StreamReader and StreamWriter
        # 3) Will register socket's fd to be polled for read events and
        #    when they occur pass read data to the protocol which will
        #    pass it on to StreamReader
        # 4) Will block until the above operations complete, after that
        #    will return the transport and protocol instances
        transport, protocol = await self._create_connection_transport(
            sock, protocol_factory, ssl, server_hostname,
            ssl_handshake_timeout=ssl_handshake_timeout)

        if self._debug:
            # Get the socket from the transport because SSL transport closes
            # the old socket and creates a new SSL socket
            sock = transport.get_extra_info('socket')
            logger.debug("%r connected to %s:%r: (%r, %r)",
                         sock, host, port, transport, protocol)

        return transport, protocol

    async def _create_connection_transport(
        self, sock, protocol_factory, ssl,
        server_hostname, server_side=False,
        ssl_handshake_timeout=None
    ):
        """
        1) Sets socket to non-blocking mode, creates protocol via protocol
           factory and waiter future to wait till connecting operation completes.

        2) In case of Unix creates a `_SelectorSocketTransport` passing it
           the socket, protocol and waiter future. The transport will:
                1. Disable the Nagle algorithm on the socket -
                   small writes will be sent without waiting for the TCP ACK.
                   This generally decreases the latency
                   (in some cases significantly.)
                2. Schedule protocol's `.connection_made` to be called during
                   this loop's next iteration:
                       - Will store a reference to the transport in the
                         protocol
                       - Will store a reference to the transport in
                         protocol's StreamReader
                       - If a client connected callback was
                         provided to the protocol creates a StreamWriter,
                         schedules a task with the client connected
                         callback passing it StreamReader and StreamWriter
                3. Schedule the socket's fd to be registered with
                   the event loop's selector which will poll it for read events
                   every loop iteration. Registration will be done after
                   protocol's `.connection_made`. After that on read events
                   a callback will be called which will read from the socket
                   and pass read data on to the protocol instance.
                4. Will schedule the `waiter` future to be completed after
                   protocol's `.connection_made`and socket's fd registration with
                   selector operations complete. This will unblock the upcoming
                   call to `await waiter`.

        3) Wait/block until protocol's `.connection_made` completes and socket's
           fd is registered for read events with event loop's selector

        4) Returns transport and protocol
        """
        sock.setblocking(False)

        protocol = protocol_factory()
        waiter = self.create_future()
        # TODO - after you encounter ssl transports
        if ssl:
            sslcontext = None if isinstance(ssl, bool) else ssl
            transport = self._make_ssl_transport(
                sock, protocol, sslcontext, waiter,
                server_side=server_side, server_hostname=server_hostname,
                ssl_handshake_timeout=ssl_handshake_timeout)
        else:
            # In case of Unix will create a `_SelectorSocketTransport` which
            # will:
            #   1) Disable the Nagle algorithm on the socket -
            #      small writes will be sent without waiting for the TCP ACK.
            #      This generally decreases the latency
            #      (in some cases significantly.)
            #   2) Schedule protocol's `.connection_made` to be called during
            #      this loop's next iteration:
            #          - Will store a reference to the transport in the
            #            protocol
            #          - Will store a reference to the transport in
            #            protocol's StreamReader
            #          - If a client connected callback was
            #            provided to the protocol creates a StreamWriter,
            #            schedules a task with the client connected
            #            callback passing it StreamReader and StreamWriter
            #   3) Schedule the socket's fd to be registered with
            #      the event loop's selector which will poll it for read events
            #      every loop iteration. Registration will be done after
            #      protocol's `.connection_made`. After that on read events
            #      a callback will be called which will read from the socket
            #      and pass read data on to the protocol instance.
            #   4) Will schedule the `waiter` future to be completed after
            #      protocol's `.connection_made`and socket's fd registration with
            #      selector operations complete. This will unblock the upcoming
            #      call to `await waiter`.
            transport = self._make_socket_transport(sock, protocol, waiter)

        try:
            # Wait until protocol's `.connection_made` completes and socket's
            # fd is registered for read events with event loop's selector.
            await waiter
        except:
            transport.close()
            raise

        return transport, protocol

    # TODO - add a more detailed docstring
    async def sendfile(self, transport, file, offset=0, count=None,
                       *, fallback=True):
        """Send a file to transport.

        Return the total number of bytes which were sent.

        The method uses high-performance os.sendfile if available.

        file must be a regular file object opened in binary mode.

        offset tells from where to start reading the file. If specified,
        count is the total number of bytes to transmit as opposed to
        sending the file until EOF is reached. File position is updated on
        return or also in case of error in which case file.tell()
        can be used to figure out the number of bytes
        which were sent.

        fallback set to True makes asyncio to manually read and send
        the file when the platform does not support the sendfile syscall
        (e.g. Windows or SSL socket on Unix).

        Raise SendfileNotAvailableError if the system does not support
        sendfile syscall and fallback is False.
        """
        if transport.is_closing():
            raise RuntimeError("Transport is closing")

        mode = getattr(transport, '_sendfile_compatible',
                       constants._SendfileMode.UNSUPPORTED)

        if mode is constants._SendfileMode.UNSUPPORTED:
            raise RuntimeError(
                f"sendfile is not supported for transport {transport!r}")

        if mode is constants._SendfileMode.TRY_NATIVE:
            try:
                # TODO - add a more detailed docstring after you encounter
                #  _UnixSelectorEventLoop's implementation
                #  of `._sock_sendfile_native`
                return await self._sendfile_native(transport, file,
                                                   offset, count)
            except exceptions.SendfileNotAvailableError as exc:
                if not fallback:
                    raise

        if not fallback:
            raise RuntimeError(
                f"fallback is disabled and native sendfile is not "
                f"supported for transport {transport!r}")

        # Checkout docstring for `._sendfile_fallback`
        return await self._sendfile_fallback(transport, file,
                                             offset, count)

    # TODO - add a more detailed docstring after you encounter
    #  _UnixSelectorEventLoop's implementation of `._sock_sendfile_native`
    async def _sendfile_native(self, transp, file, offset, count):
        """
        BaseSelectorEventLoop implements this method
        """
        raise exceptions.SendfileNotAvailableError(
            "sendfile syscall is not supported")

    async def _sendfile_fallback(self, transp, file, offset, count):
        """
        Fallback using transport's `.write` method if OS doesn't support
        'sendfile' or the provided types cannot be used with 'sendfile'.
        When using this we loose the 'sendfile' optimization of copying data
        from src to dst within kernel space. Here user space buffers are used.

        1) If offset was provided we seek in file to offset

        2) Allocate a buffer of size `count` or 16,384 KB
           if count was not provided

        3) Create a `_SendfileFallbackProtocol` instance using
           passed in `transp`. When the protocol is instantiated it will:
              - Save the transport's current protocol instance (to restore it
                later after `sendfile` fallback completes)
              - Save info about whether the transport is currently paused reading
                or not. Will pause the transport. After `sendfile`
                fallback operation completes if the transport was previously
                reading (not paused) - will resume it.
              - Checks if the original protocol is currently paused writing
                meaning transport's buffer is currently above
                the high watermark - if it is, creates `self._write_ready_fut`
                so when `await self.drain()` is called during this `sendfile`
                fallback we will block on first sending loop iteration
                until transport's write buffer drops  below the high watermark.

        4) We enter a loop in which we will send the file in chunks via
           transport's `.write` method until all the requested data is sent

        5) Every loop iteration:
            - If `count` was provided by the caller we update the chunk size
              to be sent, which will either be the amount of data left
              to be sent or 16,384 KB whichever is smaller. If amount of data
              left to be sent is 0 or less - break from sending loop.

            - Create a memoryview from the buffer previously allocated and
              take a slice from it up to current chunk size

            - In a threadpool read into the buffer memoryview from file
              up to current chunk size

            - If EOF - break from loop

            - Maybe block if transport's write buffer is currently above the
              high watermark until it drops bellow it

            - Write to transport, if transport's write buffer is currently empty,
              tries to send the data to the network connection immediately.
              If not all was sent or transport's write buffer wasn't empty,
              will send the data asynchronously via write events polling
              callback.

            - Update running total sent with amount of data written to transport.

        6) After all data was sent seek in file to position up to amount of data
           sent.

        7) Restore original protocol:
            - Restores original protocol on transport
            - Notifies transport to resume reading if it wasn't paused before
              sendfile
            - If original protocol was paused writing before sendfile, resumes
              it, because after sendfile completes transport's write buffer
              is guaranteed to be below the high watermark and thus the
              original protocol `self._proto` can resume writing.
        """
        # if user provided offset in file from which to start sending data,
        # seek to that offset
        if offset:
            file.seek(offset)

        blocksize = min(count, 16384) if count else 16384
        buf = bytearray(blocksize)
        total_sent = 0

        proto = _SendfileFallbackProtocol(transp)

        try:
            # loop until all data is read from file and written to transport's
            # write buffer and eventually the underlying network connection
            while True:
                # if our caller wants to send only a limited amount of data
                # from the file, every loop iteration we update the blocksize
                # to be sent to correspond to amount left to be sent or if
                # it's still greater than 16,384 KB, then blocksize will remain
                # equal to 16,384 KB
                if count:
                    blocksize = min(count - total_sent, blocksize)
                    # if all requested data was sent, break from the loop
                    if blocksize <= 0:
                        return total_sent

                # create a memoryview slice from the original buffer to
                # correspond to the amount of data to be sent during this loop
                # iteration
                view = memoryview(buf)[:blocksize]

                # read amount of data equal to `blocksize` from file into buffer
                # in a separate thread
                read = await self.run_in_executor(None, file.readinto, view)
                # if no data is left to be read and we got EOF, just break from
                # the loop
                if not read:
                    return total_sent  # EOF

                # if transport's write buffer can still accomodate data, will
                # return immediately
                # if transport's buffer went above its high water mark, will
                # block until transport signals that writes can be resumed
                # because buffer dropped below high watermark
                await proto.drain()

                # If the transport's write buffer is currently empty:
                #   1) Will try to send the data to the network connection
                #      immediately
                #   2) If no or only some data is sent immediately, registers
                #      the socket fd to be tracked for write events
                # If the write buffer is not empty there's no need to register
                # the socket for write events, it's already registered
                # Extends the transport's write buffer with the data passed in
                # and pauses the protocol fro writing if the buffer went above
                # the high watermark
                # Data will be sent asynchronously when polling the socket
                # signals that writes will not block
                transp.write(view[:read])

                # increase total_sent by amount read from file and written
                # to transport's write buffer or the network connection
                total_sent += read
        finally:
            # move position in file up to data sent
            if total_sent > 0 and hasattr(file, 'seek'):
                file.seek(offset + total_sent)

            # 1) Restores original protocol on transport
            # 2) Notifies transport to resume reading if it wasn't paused before
            #    sendfile
            # 3) If original protocol was paused writing before sendfile, resumes
            #    it, because after sendfile completes transport's write buffer
            #    is guaranteed to be below the high watermark and thus the
            #    original protocol `self._proto` can resume writing.
            await proto.restore()

    # TODO !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
    #  !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
    #  EXAMINE THIS AGAIN AFTER SSL PROTOCOL RESEARCH
    async def start_tls(self, transport, protocol, sslcontext, *,
                        server_side=False,
                        server_hostname=None,
                        ssl_handshake_timeout=None):
        """Upgrade transport to TLS.

        Return a new transport that *protocol* should start using
        immediately.
        """
        if ssl is None:
            raise RuntimeError('Python ssl module is not available')

        if not isinstance(sslcontext, ssl.SSLContext):
            raise TypeError(
                f'sslcontext is expected to be an instance of ssl.SSLContext, '
                f'got {sslcontext!r}')

        if not getattr(transport, '_start_tls_compatible', False):
            raise TypeError(
                f'transport {transport!r} is not supported by start_tls()')

        waiter = self.create_future()
        ssl_protocol = sslproto.SSLProtocol(
            self, protocol, sslcontext, waiter,
            server_side, server_hostname,
            ssl_handshake_timeout=ssl_handshake_timeout,
            call_connection_made=False)

        # Pause early so that "ssl_protocol.data_received()" doesn't
        # have a chance to get called before "ssl_protocol.connection_made()".
        transport.pause_reading()

        transport.set_protocol(ssl_protocol)
        conmade_cb = self.call_soon(ssl_protocol.connection_made, transport)
        resume_cb = self.call_soon(transport.resume_reading)

        try:
            await waiter
        except BaseException:
            transport.close()
            conmade_cb.cancel()
            resume_cb.cancel()
            raise

        return ssl_protocol._app_transport

    # TODO !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
    #    ADD DOCSTRING
    async def create_datagram_endpoint(self, protocol_factory,
                                       local_addr=None, remote_addr=None, *,
                                       family=0, proto=0, flags=0,
                                       reuse_address=_unset, reuse_port=None,
                                       allow_broadcast=None, sock=None):
        """Create datagram connection."""
        # if a pre-created socket is passed in
        if sock is not None:
            # if pre-created socket is not a UDP socket - raise error
            if sock.type != socket.SOCK_DGRAM:
                raise ValueError(
                    f'A UDP Socket was expected, got {sock!r}')

            # if params to create a new socket were passed in with a pre-created
            # socket, raise error with the params
            if (local_addr or remote_addr or
                family or proto or flags or
                reuse_port or allow_broadcast):
                # show the problematic kwargs in exception msg
                opts = dict(local_addr=local_addr, remote_addr=remote_addr,
                            family=family, proto=proto, flags=flags,
                            reuse_address=reuse_address, reuse_port=reuse_port,
                            allow_broadcast=allow_broadcast)
                problems = ', '.join(f'{k}={v}' for k, v in opts.items() if v)
                raise ValueError(
                    f'socket modifier keyword arguments can not be used '
                    f'when sock is specified. ({problems})')

            # set the pre-created socket to non-blocking mode
            sock.setblocking(False)
            # because socket was already pre-created `r_addr` remote address
            # will not be used
            r_addr = None
        # if socket wasn't pre-created, and we need to create and connect
        # a new one using the parameters passed in
        else:
            # if local_addr or remote_addr were not specified,
            # we need at least the `family`
            # we create `addr_pairs_info` using the `family` and `proto`
            # passed in, leaving both addresses in `addr_pairs_info` as None
            if not (local_addr or remote_addr):
                if family == 0:
                    raise ValueError('unexpected address family')
                addr_pairs_info = (((family, proto), (None, None)),)
            # if current system supports 'AF_UNIX' sockets and the family
            # specified was 'AF_UNIX'
            elif hasattr(socket, 'AF_UNIX') and family == socket.AF_UNIX:
                # in case of 'AF_UNIX' addresses must be strings
                for addr in (local_addr, remote_addr):
                    if addr is not None and not isinstance(addr, str):
                        raise TypeError('string is expected')

                # if local address doesn't start with 0 or '\x00', remove
                # socket if it exists
                if local_addr and local_addr[0] not in (0, '\x00'):
                    try:
                        if stat.S_ISSOCK(os.stat(local_addr).st_mode):
                            os.remove(local_addr)
                    except FileNotFoundError:
                        pass
                    except OSError as err:
                        # Directory may have permissions only to create socket.
                        logger.error('Unable to check or remove stale UNIX '
                                     'socket %r: %r',
                                     local_addr, err)

                # `addr_pairs_info` will consist of `family` and `proto` passed
                # in and 2 unix domain socket addresses
                addr_pairs_info = (((family, proto),
                                    (local_addr, remote_addr)),)
            # network stack sockets were requested by our caller and he specified
            # at least one address
            else:
                # join address by (family, protocol)
                # after loop will be of the following format
                # {(`family`, `proto`): [local_address, remote_address]}
                addr_infos = {}  # Using order preserving dict
                for idx, addr in ((0, local_addr), (1, remote_addr)):
                    if addr is not None:
                        # network stack addresses should be 2-tuples of
                        # (ip, port)
                        assert isinstance(addr, tuple) and len(addr) == 2, (
                            '2-tuple is expected')

                        # if not an AF_UNIX socket, needs DNS resolving
                        # 1) If address is already a resolved IP, then just
                        #    extracts info from it for socket creation
                        # 2) If not already an IP does a DNS request in loop's
                        #    threadpool and returns info for socket creation
                        #    and the underlying IP address and port.
                        #    Return example:
                        #       (<AddressFamily.AF_INET: 2>,
                        #         <SocketKind.SOCK_STREAM: 1>, 6,
                        #         '', ('93.184.216.34', 80))
                        infos = await self._ensure_resolved(
                            addr, family=family, type=socket.SOCK_DGRAM,
                            proto=proto, flags=flags, loop=self)
                        if not infos:
                            raise OSError('getaddrinfo() returned empty list')

                        for fam, _, pro, _, address in infos:
                            key = (fam, pro)
                            if key not in addr_infos:
                                addr_infos[key] = [None, None]
                            addr_infos[key][idx] = address

                # each addr has to have info for each (family, proto) pair
                # will be of the following format:
                # (
                #    ((`family1`, `proto1`), [local_addr1, remote_addr1]),
                #    ((`family2`, `proto2`), [local_addr2, remote_addr2]),
                #    etc
                # ) where both local_addr and remote_addr are not None
                addr_pairs_info = [
                    (key, addr_pair) for key, addr_pair in addr_infos.items()
                    if not ((local_addr and addr_pair[0] is None) or
                            (remote_addr and addr_pair[1] is None))]

                if not addr_pairs_info:
                    raise ValueError('can not get address information')

            exceptions = []

            # bpo-37228
            if reuse_address is not _unset:
                if reuse_address:
                    raise ValueError("Passing `reuse_address=True` is no "
                                     "longer supported, as the usage of "
                                     "SO_REUSEPORT in UDP poses a significant "
                                     "security concern.")
                else:
                    warnings.warn("The *reuse_address* parameter has been "
                                  "deprecated as of 3.5.10 and is scheduled "
                                  "for removal in 3.11.", DeprecationWarning,
                                  stacklevel=2)

            # for each resolved address info, try to create a UDP socket,
            # set it to be non-blocking, if local address was provided bind
            # it to that address, connect the socket to remote address.
            # After first successful attempt occurs we break the loop, and
            # the resulting connected socket is the one to be used
            for ((family, proto),
                 (local_address, remote_address)) in addr_pairs_info:
                sock = None
                r_addr = None
                try:
                    sock = socket.socket(
                        family=family, type=socket.SOCK_DGRAM, proto=proto)
                    if reuse_port:
                        _set_reuseport(sock)
                    if allow_broadcast:
                        sock.setsockopt(
                            socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                    sock.setblocking(False)

                    if local_addr:
                        sock.bind(local_address)
                    if remote_addr:
                        if not allow_broadcast:
                            await self.sock_connect(sock, remote_address)
                        r_addr = remote_address
                except OSError as exc:
                    if sock is not None:
                        sock.close()
                    exceptions.append(exc)
                except:
                    if sock is not None:
                        sock.close()
                    raise
                else:
                    break
            else:
                raise exceptions[0]

        # create protocol, asyncio doesn't provide UDP protocol implementations
        # out of the box, so this will be a custom UDP protocol implementation
        protocol = protocol_factory()
        waiter = self.create_future()
        transport = self._make_datagram_transport(
            sock, protocol, r_addr, waiter)

        if self._debug:
            if local_addr:
                logger.info("Datagram endpoint local_addr=%r remote_addr=%r "
                            "created: (%r, %r)",
                            local_addr, remote_addr, transport, protocol)
            else:
                logger.debug("Datagram endpoint remote_addr=%r created: "
                             "(%r, %r)",
                             remote_addr, transport, protocol)

        try:
            # Wait until protocol's `.connection_made` completes and socket's
            # fd is registered for read events with event loop's selector.
            await waiter
        except:
            transport.close()
            raise

        return transport, protocol

    async def _ensure_resolved(
        self, address, *,
        family=0, type=socket.SOCK_STREAM,
        proto=0, flags=0, loop
    ):
        """
        If IP is already resolved just returns info about it for socket creation
        If not yet resolved, does a DNS request in loop's threadpool
        and returns info for socket creation
        Example return value:
        (<AddressFamily.AF_INET: 2>, <SocketKind.SOCK_STREAM: 1>, 6,
        '', ('93.184.216.34', 80))
        """
        # could be ('my-domain.com', 80) if not already an IP address
        host, port = address[:2]

        # _ipaddr_info checks if host is already an IP address or not via
        # socket.inet_pton(address_family, ip_string), and if it is,
        # returns info about it, else None
        info = _ipaddr_info(host, port, family, type, proto, *address[2:])
        if info is not None:
            # "host" is already a resolved IP.
            return [info]
        #  getaddrinfo resolves host names via DNS. Info about it:
        #  https://pythontic.com/modules/socket/getaddrinfo
        #  https://masandilov.ru/network/guide_to_network_programming5
        #  https://stackoverflow.com/questions/2157592/how-does-getaddrinfo-do-dns-lookup
        #  https://docs.python.org/3/library/socket.html#socket.getaddrinfo

        # If we know a network service by host name like example.org or the IP
        # address of the network service either in form of IPv4 or IPv6 along
        # with the port number of the network service, getaddrinfo() will
        # return a list of tuples containing information about socket(s)
        # that can be created with the service.
        return await loop.getaddrinfo(host, port, family=family, type=type,
                                      proto=proto, flags=flags)

    async def _create_server_getaddrinfo(self, host, port, family, flags):
        """
        Do DNS resolving of server address (if necessary) in a separate thread.
        Return addr infos. Example:
            ((<AddressFamily.AF_INET: 2>, <SocketKind.SOCK_STREAM: 1>, 6,
             '', ('93.184.216.34', 80), (<other addr tuple>))
        """
        infos = await self._ensure_resolved((host, port), family=family,
                                            type=socket.SOCK_STREAM,
                                            flags=flags, loop=self)
        if not infos:
            raise OSError(f'getaddrinfo({host!r}) returned empty list')
        return infos

    async def create_server(
        self,
        protocol_factory,
        host=None,
        port=None,
        *,
        family=socket.AF_UNSPEC,
        flags=socket.AI_PASSIVE,
        sock=None,
        backlog=100,
        ssl=None,
        reuse_address=None,
        reuse_port=None,
        ssl_handshake_timeout=None,
        start_serving=True
    ):
        """Create a TCP server.

        The host parameter can be a string, in that case the TCP server is
        bound to host and port.

        The host parameter can also be a sequence of strings and in that case
        the TCP server is bound to all hosts of the sequence. If a host
        appears multiple times (possibly indirectly e.g. when hostnames
        resolve to the same IP address), the server is only bound once to that
        host.

        Return a Server object which can be used to stop the service.

        This method is a coroutine.

        If only host/hosts and port were specified:
            1) Do DNS resolving (if necessary) for all hosts
               (each resolving is done in a separate thread)

            2) Create a server socket for each resulting address info

            3) By default on posix platforms except cygwin all sockets will have
               the SO_REUSEADDR option set, which does the following:
                    Changes the way how wildcard addresses ("any IP address")
                    are treated when searching for conflicts.
                    For example, binding socketA to 0.0.0.0:21 and then binding
                    socketB to 192.168.0.1:21 will fail without SO_REUSEADDR set,
                    but will succeed with SO_REUSEADDR, since 0.0.0.0
                    and 192.168.0.1 are not exactly the same address,
                    one is a wildcard for all local addresses and the other one
                    is a very specific local address.

            4) If `reuse_port` param is True (False by default) SO_REUSEPORT
               option will be set on each socket, which does the following:
                    Allows you to bind an arbitrary number of sockets to exactly
                    the same source address and port as long as all prior bound
                    sockets also had SO_REUSEPORT set before they were bound.

            5) Binds every socket to address

        If pre-created and bound socket was provided by caller:
            1 - 5) Do nothing

        6) Create `Server` instance

        7) If `start_serving` param is True (by default is True) call
           `._start_serving()` on the server instance. For detailed docstring
           check out `._start_serving()` method in `Server` class.

        8) Skip one loop iteration so that all 'loop.add_reader' go through

        9) Return `Server` instance
        """
        if isinstance(ssl, bool):
            raise TypeError('ssl argument must be an SSLContext or None')

        if ssl_handshake_timeout is not None and ssl is None:
            raise ValueError(
                'ssl_handshake_timeout is only meaningful with ssl')

        # no pre-created server socket from caller
        if host is not None or port is not None:
            if sock is not None:
                raise ValueError(
                    'host/port and sock can not be specified at the same time')

            # on all posix systems by default listening sockets will have
            # the SO_REUSEADDR option set. Whats does this options do:
            #   Mainly changes the way how wildcard addresses ("any IP address")
            #   are treated when searching for conflicts.
            #   For example, binding socketA to 0.0.0.0:21 and then binding
            #   socketB to 192.168.0.1:21 will fail without SO_REUSEADDR set,
            #   but will succeed with SO_REUSEADDR, since 0.0.0.0 and 192.168.0.1
            #   are not exactly the same address, one is a wildcard for
            #   all local addresses and the other one is a very specific
            #   local address.
            if reuse_address is None:
                # Cygwin — UNIX-подобная среда и интерфейс командной
                # строки для Microsoft Windows
                reuse_address = os.name == 'posix' and sys.platform != 'cygwin'

            sockets = []

            # construct `hosts` list
            if host == '':
                hosts = [None]
            elif (isinstance(host, str) or
                  not isinstance(host, collections.abc.Iterable)):
                hosts = [host]
            else:
                hosts = host

            # create list of coros which will do DNS resolving for every host
            # if necessary (every DNS request is done in a separate thread)
            fs = [self._create_server_getaddrinfo(host, port, family=family,
                                                  flags=flags)
                  for host in hosts]

            # do actual DNS resolving for every host
            infos = await tasks.gather(*fs)
            # flatten all addr infos, will be a set of:
            #  {<addr info tuple>, <addr info tuple>, etc}
            #  sockets will be bound to all addr infos, so there may be more
            #  bound and listening sockets than hosts provided
            infos = set(itertools.chain.from_iterable(infos))

            completed = False

            try:
                # try creating socket for every addr info
                for res in infos:
                    # af - address family, for example, AF_INET
                    # socktype - for example, SOCK_STREAM
                    # proto - for example, PF_INET (TCP/IP)
                    # canonname - record in the DNS database that indicates
                    #             the true host name of a computer associated
                    #             with its aliases
                    # sa - server address, for example, ('93.184.216.34', 80)
                    af, socktype, proto, canonname, sa = res

                    try:
                        # create server socket
                        sock = socket.socket(af, socktype, proto)
                    except socket.error:
                        # Assume it's a bad family/type/protocol combination.
                        if self._debug:
                            logger.warning('create_server() failed to create '
                                           'socket.socket(%r, %r, %r)',
                                           af, socktype, proto, exc_info=True)
                        continue

                    sockets.append(sock)

                    if reuse_address:
                        # be default set on posix systems except cygwin
                        # changes the way how wildcard addresses
                        # ("any IP address")are treated when searching
                        # for conflicts
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR,
                                        True)
                    # None bye default (False)
                    if reuse_port:
                        # SO_REUSEPORT allows you to bind an arbitrary number
                        # of sockets to exactly the same source address and
                        # port as long as all prior bound sockets also had
                        # SO_REUSEPORT set before they were bound
                        _set_reuseport(sock)

                    # Disable IPv4/IPv6 dual stack support (enabled by
                    # default on Linux) which makes a single socket
                    # listen on both address families.
                    # IPv6 takes priority.
                    if (_HAS_IPv6 and
                        af == socket.AF_INET6 and
                        hasattr(socket, 'IPPROTO_IPV6')):
                        sock.setsockopt(socket.IPPROTO_IPV6,
                                        socket.IPV6_V6ONLY,
                                        True)

                    try:
                        # bind socket to resolved addr
                        sock.bind(sa)
                    except OSError as err:
                        raise OSError(err.errno, 'error while attempting '
                                                 'to bind on address %r: %s'
                                      % (sa, err.strerror.lower())) from None

                    completed = True
            finally:
                if not completed:
                    for sock in sockets:
                        sock.close()
        # pre-created and bound to addr socket was provided by caller
        else:
            if sock is None:
                raise ValueError('Neither host/port nor sock were specified')
            if sock.type != socket.SOCK_STREAM:
                raise ValueError(f'A Stream Socket was expected, got {sock!r}')
            sockets = [sock]

        # set all bound socket to non-blocking mode
        for sock in sockets:
            sock.setblocking(False)

        server = Server(self, sockets, protocol_factory,
                        ssl, backlog, ssl_handshake_timeout)

        # True by default
        if start_serving:
            # for details check out `._start_serving()` docstring
            server._start_serving()
            # Skip one loop iteration so that all 'loop.add_reader'
            # go through.
            await tasks.sleep(0)

        if self._debug:
            logger.info("%r is serving", server)

        return server

    async def connect_accepted_socket(
        self,
        protocol_factory,
        sock,
        *,
        ssl=None,
        ssl_handshake_timeout=None
    ):
        """
        1) Receives an already accepted client socket
        2) Sets socket to non-blocking mode, creates protocol via protocol
           factory and waiter future to wait till connecting operation completes.

        3) In case of Unix creates a `_SelectorSocketTransport` passing it
           the socket, protocol and waiter future. The transport will:
                1. Disable the Nagle algorithm on the socket -
                   small writes will be sent without waiting for the TCP ACK.
                   This generally decreases the latency
                   (in some cases significantly.)
                2. Schedule protocol's `.connection_made` to be called during
                   this loop's next iteration:
                       - Will store a reference to the transport in the
                         protocol
                       - Will store a reference to the transport in
                         protocol's StreamReader
                       - If a client connected callback was
                         provided to the protocol creates a StreamWriter,
                         schedules a task with the client connected
                         callback passing it StreamReader and StreamWriter
                3. Schedule the socket's fd to be registered with
                   the event loop's selector which will poll it for read events
                   every loop iteration. Registration will be done after
                   protocol's `.connection_made`. After that on read events
                   a callback will be called which will read from the socket
                   and pass read data on to the protocol instance.
                4. Will schedule the `waiter` future to be completed after
                   protocol's `.connection_made`and socket's fd registration with
                   selector operations complete. This will unblock the upcoming
                   call to `await waiter`.

        3) Wait/block until protocol's `.connection_made` completes and socket's
           fd is registered for read events with event loop's selector

        4) Returns transport and protocol
        """
        if sock.type != socket.SOCK_STREAM:
            raise ValueError(f'A Stream Socket was expected, got {sock!r}')

        if ssl_handshake_timeout is not None and not ssl:
            raise ValueError(
                'ssl_handshake_timeout is only meaningful with ssl')

        transport, protocol = await self._create_connection_transport(
            sock, protocol_factory, ssl, '', server_side=True,
            ssl_handshake_timeout=ssl_handshake_timeout)

        if self._debug:
            # Get the socket from the transport because SSL transport closes
            # the old socket and creates a new SSL socket
            sock = transport.get_extra_info('socket')
            logger.debug("%r handled: (%r, %r)", sock, transport, protocol)
        return transport, protocol

    def get_exception_handler(self):
        """Return an exception handler, or None if the default one is in use.
        """
        return self._exception_handler

    def set_exception_handler(self, handler):
        """Set handler as the new event loop exception handler.

        If handler is None, the default exception handler will
        be set.

        If handler is a callable object, it should have a
        signature matching '(loop, context)', where 'loop'
        will be a reference to the active event loop, 'context'
        will be a dict object (see `call_exception_handler()`
        documentation for details about context).
        """
        if handler is not None and not callable(handler):
            raise TypeError(f'A callable object or None is expected, '
                            f'got {handler!r}')
        self._exception_handler = handler

    def default_exception_handler(self, context):
        """Default exception handler.

        This is called when an exception occurs and no exception
        handler is set, and can be called by a custom exception
        handler that wants to defer to the default behavior.

        This default handler logs the error message and other
        context-dependent information.  In debug mode, a truncated
        stack trace is also appended showing where the given object
        (e.g. a handle or future or task) was created, if any.

        The context parameter has the same meaning as in
        `call_exception_handler()`.
        """
        message = context.get('message')
        if not message:
            message = 'Unhandled exception in event loop'

        exception = context.get('exception')
        if exception is not None:
            exc_info = (type(exception), exception, exception.__traceback__)
        else:
            exc_info = False

        # self._current_handle in debug mode
        # is set to the handle that is currently executed by the loop
        if ('source_traceback' not in context and
            self._current_handle is not None and
            self._current_handle._source_traceback):
            context['handle_traceback'] = \
                self._current_handle._source_traceback

        log_lines = [message]
        for key in sorted(context):
            if key in {'message', 'exception'}:
                continue
            value = context[key]
            if key == 'source_traceback':
                tb = ''.join(traceback.format_list(value))
                value = 'Object created at (most recent call last):\n'
                value += tb.rstrip()
            elif key == 'handle_traceback':
                tb = ''.join(traceback.format_list(value))
                value = 'Handle created at (most recent call last):\n'
                value += tb.rstrip()
            else:
                value = repr(value)
            log_lines.append(f'{key}: {value}')

        logger.error('\n'.join(log_lines), exc_info=exc_info)

    def call_exception_handler(self, context):
        """Call the current event loop's exception handler.

        The context argument is a dict containing the following keys:

        - 'message': Error message;
        - 'exception' (optional): Exception object;
        - 'future' (optional): Future instance;
        - 'task' (optional): Task instance;
        - 'handle' (optional): Handle instance;
        - 'protocol' (optional): Protocol instance;
        - 'transport' (optional): Transport instance;
        - 'socket' (optional): Socket instance;
        - 'asyncgen' (optional): Asynchronous generator that caused
                                 the exception.

        New keys maybe introduced in the future.

        Note: do not overload this method in an event loop subclass.
        For custom exception handling, use the
        `set_exception_handler()` method.
        """
        if self._exception_handler is None:
            try:
                self.default_exception_handler(context)
            except (SystemExit, KeyboardInterrupt):
                raise
            except BaseException:
                # Second protection layer for unexpected errors
                # in the default implementation, as well as for subclassed
                # event loops with overloaded "default_exception_handler".
                logger.error('Exception in default exception handler',
                             exc_info=True)
        else:
            try:
                self._exception_handler(self, context)
            except (SystemExit, KeyboardInterrupt):
                raise
            except BaseException as exc:
                # Exception in the user set custom exception handler.
                try:
                    # Let's try default handler.
                    self.default_exception_handler({
                        'message': 'Unhandled error in exception handler',
                        'exception': exc,
                        'context': context,
                    })
                except (SystemExit, KeyboardInterrupt):
                    raise
                except BaseException:
                    # Guard 'default_exception_handler' in case it is
                    # overloaded.
                    logger.error('Exception in default exception handler '
                                 'while handling an unexpected error '
                                 'in custom exception handler',
                                 exc_info=True)

    def _add_callback(self, handle):
        """Add a Handle to _scheduled (TimerHandle) or _ready."""
        assert isinstance(handle, events.Handle), 'A Handle is required here'
        if handle._cancelled:
            return
        assert not isinstance(handle, events.TimerHandle)
        self._ready.append(handle)

    def _add_callback_signalsafe(self, handle):
        """Like _add_callback() but called from a signal handler."""
        self._add_callback(handle)
        # if the loop is asleep, will wake it up by writing a byte to self-pipe
        # (can be called from a different thread)
        self._write_to_self()

    def _timer_handle_cancelled(self, handle):
        """Notification that a TimerHandle has been cancelled."""
        if handle._scheduled:
            self._timer_cancelled_count += 1

    def _run_once(self):
        """Run one full iteration of the event loop.
        This calls all currently ready callbacks, polls for I/O,
        schedules the resulting callbacks, and finally schedules
        'call_later' callbacks.

        SUPER SHORT DETAILS:
        1) Poll for I/O
        2) Execute all immediate, I/O ready and 'ready' delayed callbacks.

        IN A NUTSHELL:
            All callbacks are wrapped either in an events.Handle
            or events.TimerHandle (for delayed callbacks).
            To call the callback the loop will call the ._run()
            method on the handle which in turn will call the callback
            inside a contextvars.Context with the provided arguments.

            1) Cleans up all cancelled delayed callbacks (self._scheduled)
            (registered with .call_later and .call_at)
            (check out the condition for this below);

            2) If there are callbacks that need to be called immediately
                (self._ready deque is not empty, registered via .call_soon
                and .call_soon_threadsafe)
                or the loop was requested to stop,
                then the I/O polling timeout will be set to 0
                (only instantly ready file objects will be processed during
                the current loop iteration)

            3) If there are no callbacks needed to be called immediately
              (self._ready deque is emtpy) and the loop is not stopping,
              then the I/O polling timeout will be equal to the number of seconds
              left until the soonest delayed callback needs to be called
              (self._scheduled list - priority queue by timestamp)
              (could be 0 if the monotonic clock already reached the point
              in time when the soonest delayed callback needs to be called)

            4) Poll for I/O via select/poll/epoll/kqueue via the selectors module
              (depends on OS and what is available) until the timeout set in
              step 2 or 3 was reached.

            5) Add callbacks associated with ready file objects
              (read or write) returned from current iteration's I/O polling
              (previous step) to self._ready deque, meaning those callbacks will
              be executed during the current loop iteration

            6) Add all delayed/scheduled callbacks for which "the time has come"
              to self._ready deque, meaning that they will be also executed
              during the current loop iteration

            7) Execute all callbacks present in the self._ready deque.
               Will be executed with arguments with which they were registered
               and within the current contextvars.Context or the one that was
               provided during the callback registration.
               Due to the implementation details of this method the callbacks
               will be executed in this order:
                - first all the immediate callbacks
                - all I/O ready callbacks
                - all 'ready' delayed/scheduled callbacks
        """

        # get the count of all scheduled TimerHandles
        # (contain callables within them) added to self._scheduled
        # via self.call_at or self.call_later methods
        sched_count = len(self._scheduled)

        # _MIN_SCHEDULED_TIMER_HANDLES - minimum number of _scheduled
        # timer handles before cleanup of cancelled handles is performed
        # (100 by default)

        # _MIN_CANCELLED_TIMER_HANDLES_FRACTION - minimum fraction of
        # _scheduled timer handles that are cancelled before cleanup of
        # cancelled handles is performed (by default 0.5 - a half of
        # all of the _scheduled timer handles must be cancelled)

        # if the number of _scheduled timer handles is greater than 100
        #  and a half of those _scheduled timer handlers were cancelled
        # (timer handle's .cancel method will increment this loop's
        # ._timer_cancelled_count attr)
        if (sched_count > _MIN_SCHEDULED_TIMER_HANDLES and
            self._timer_cancelled_count / sched_count >
            _MIN_CANCELLED_TIMER_HANDLES_FRACTION):
            # Remove delayed calls that were cancelled if their number
            # is too high

            # new_scheduled will only contain active timer handles
            new_scheduled = []
            for handle in self._scheduled:
                if handle._cancelled:
                    handle._scheduled = False
                else:
                    new_scheduled.append(handle)

            # sort the remaining timer handles so that the ones to be called
            # soonest will be retrieved first by heappop
            # Basically a priority queue by timestamp (._when attr of the handle)
            heapq.heapify(new_scheduled)

            # cleaned up scheduled timer handles
            self._scheduled = new_scheduled

            # reset the timer handles cancelled count
            self._timer_cancelled_count = 0
        else:
            # Remove delayed calls that were cancelled from head of queue.

            # we pop the handles from the head of the queue until there are
            # handles and the popped handle is not cancelled.
            while self._scheduled and self._scheduled[0]._cancelled:
                self._timer_cancelled_count -= 1
                handle = heapq.heappop(self._scheduled)
                handle._scheduled = False

        timeout = None
        # if there are Handles ready to be run
        # (registered via .call_soon or .call_soon_threadsafe methods)
        # or the loop was requested to stop (is stopping)
        # Then the the I/O polling will timeout in 0 secs.
        if self._ready or self._stopping:
            timeout = 0
        # if there are TimerHandles scheduled to be run
        elif self._scheduled:
            # Compute the desired timeout.
            # timestamp (at which to run) of the Timer Handle to be called
            # soonest
            when = self._scheduled[0]._when

            # MAXIMUM_SELECT_TIMEOUT - maximum timeout passed to select
            # to avoid OS limitations (24 * 3600 seconds by default)

            # timeout will be equal to the number of seconds left till
            # the soonest TimerHandle needs to be run
            # if this number of seconds is less than MAXIMUM_SELECT_TIMEOUT
            timeout = min(
                # time left for the the Timer Handle to be called soonest
                # to run (if the time has already passed then 0)
                max(0, when - self.time()),
                MAXIMUM_SELECT_TIMEOUT
            )

        # Poll for I/O events until the soonest TimerHandle needs to be run
        # or if there are callbacks in self._ready just poll for 0 seconds
        # (meaning only those fds that are already ready right this
        # second will be returned)
        # For example if the soonest TimerHandle needs to be run in 5 seconds
        # from now, then poll for 5 seconds.
        # If the soonest TimerHandle needs to be run immediately
        # then poll for 0 seconds.

        # self._selector not set in BaseEventLoop (this loop implementation)
        # self._selector will be set in selector_events.BaseSelectorEventLoop.
        # will just basically return a list of events indicating the
        # file objects ready to be read from or written to.

        # Will block indefinitely (if there are no self._read or self._scheduled
        # handles present) until at least one I/O event occurs
        event_list = self._selector.select(timeout)

        # not implemented by BaseEventLoop (this loop)
        # implemented by selector_events.BaseSelectorEventLoop
        # Will schedule Handles/callbacks for FD selector events
        # to be called as soon as possible
        # (add them to self._ready deque, will be called during
        # this iteration of the loop)
        self._process_events(event_list)

        #  ------------------------------------------------------------------
        #  ------------------------------------------------------------------
        #  ------------------------------------------------------------------
        #  ------------------------------------------------------------------

        # a peek into the implementation of self._process_events(event_list):
        def _process_events(self, event_list):
            # reader and writer events.Handle(
            #   callback, args, loop=self, context=None
            # )
            # will be registered by the _selector together with the fd and the
            # selectors.EVENT_READ or selectors.EVENT_WRITE
            for key, mask in event_list:
                # fileobj - the fd which is ready
                # reader or writer is the events.Handle that contains the
                # callback to be called when the fileobj is readable or writable
                fileobj, (reader, writer) = key.fileobj, key.data

                # if READ event and there is a reader Handle
                if mask & selectors.EVENT_READ and reader is not None:
                    # if the reader handle was cancelled in between
                    if reader._cancelled:
                        self._remove_reader(fileobj)
                    else:
                        # will be added to the self._ready deque to be called
                        # as soon as possible (during current iteration)
                        self._add_callback(reader)
                if mask & selectors.EVENT_WRITE and writer is not None:
                    # if the writer handle was cancelled in between
                    if writer._cancelled:
                        self._remove_writer(fileobj)
                    else:
                        # will be added to the self._ready deque to be called
                        # as soon as possible (during current iteration)
                        self._add_callback(writer)

        #  ------------------------------------------------------------------
        #  ------------------------------------------------------------------
        #  ------------------------------------------------------------------
        #  ------------------------------------------------------------------

        # Handle 'later' callbacks that are ready.

        # add some milliseconds to current time.monotonic
        end_time = self.time() + self._clock_resolution

        # while the list o scheduled TimerHandles is full
        while self._scheduled:
            # get the soonest handle
            handle = self._scheduled[0]
            # if the handle is not yet ready (required time has not passed yet)
            # the break the loop, because the other handles are even
            # later in time
            if handle._when >= end_time:
                break
            # pop handle and heapify the list again
            # (priority by soonest timestamp)
            handle = heapq.heappop(self._scheduled)

            # add the TimerHandle to self._ready deque so it will be called
            # during this iteration of the loop
            handle._scheduled = False
            self._ready.append(handle)

            # This is the only place where callbacks are actually *called*.
            # All other places just add them to ready.
            # Note: We run all currently scheduled callbacks, but not any
            # callbacks scheduled by callbacks run this time around --
            # they will be run the next time (after another I/O poll).
            # Use an idiom that is thread-safe without using locks.

            # So here we run
            # (the order is in which they were added to the deque):
            # 1) All .call_soon and .call_soon_threadsafe callbacks

            # 2) All ready I/O callbacks (file objects associated with those
            #    callbacks are ready to be read from or written to)

            # 3) And lastly all .call_later and .call_at callbacks that are ready
            #   (time has come);

            ntodo = len(self._ready)

            # implemented as a for range because the self._ready deque
            # could be modified during iteration
            for i in range(ntodo):
                # get the first Handle wrapper (FIFO) from the queue
                handle = self._ready.popleft()

                # if cancelled in between, skip it
                if handle._cancelled:
                    continue

                # in debug mode we time the callback execution
                # and if it took longer than 0.1 second we log this fact
                if self._debug:
                    try:
                        self._current_handle = handle
                        t0 = self.time()
                        handle._run()
                        dt = self.time() - t0
                        if dt >= self.slow_callback_duration:
                            logger.warning('Executing %s took %.3f seconds',
                                           _format_handle(handle), dt)
                    finally:
                        self._current_handle = None
                else:
                    # this executes the callback attached to the Handle
                    # with the provided arguments and within contextvars.Context.
                    # If an exception occurred during the call
                    # it will invoke this loop's .call_exception_handler method
                    # with some context about the exception.
                    handle._run()
            handle = None  # Needed to break cycles when an exception occurs.

    def _set_coroutine_origin_tracking(self, enabled):
        if bool(enabled) == bool(self._coroutine_origin_tracking_enabled):
            return

        if enabled:
            self._coroutine_origin_tracking_saved_depth = \
                sys.get_coroutine_origin_tracking_depth()
            sys.set_coroutine_origin_tracking_depth(constants.DEBUG_STACK_DEPTH)
        else:
            sys.set_coroutine_origin_tracking_depth(
                self._coroutine_origin_tracking_saved_depth
            )

        self._coroutine_origin_tracking_enabled = enabled

    def get_debug(self):
        return self._debug

    def set_debug(self, enabled):
        self._debug = enabled

        # will set this flag to True during the next loop iteration,
        # can be called from another thread
        if self.is_running():
            self.call_soon_threadsafe(self._set_coroutine_origin_tracking,
                                      enabled)
