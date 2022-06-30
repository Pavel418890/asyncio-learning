import socket
import sys
import weakref

from . import coroutines
from . import events
from . import exceptions
from . import format_helpers
from . import protocols
from .log import logger
from .tasks import sleep

# Default line length limit (in StreamReader buffer)
_DEFAULT_LIMIT = 2 ** 16  # 64 KiB


class FlowControlMixin(protocols.Protocol):
    """Reusable flow control logic for StreamWriter.drain().

    This implements the protocol methods pause_writing(),
    resume_writing() and connection_lost().  If the subclass overrides
    these it must call the super methods.

    StreamWriter.drain() must wait for _drain_helper() coroutine.
    """

    def __init__(self, loop=None):
        if loop is None:
            self._loop = events.get_event_loop()
        else:
            self._loop = loop

        # set to True when the underlying transport calls `.pause_writing` method
        # on this protocol (when the transport's buffer goes over
        # the high-water mark), set back to False by `.resume_writing` method
        self._paused = False

        # Future set by the `._drain_helper` method, which immediately after
        # that blocks on awaiting this future (await `._drain_helper`
        # is called by StreamWriter via its `.drain` method usually in this
        # manner:
        #   w.write(data)
        #   await w.drain()
        # )
        self._drain_waiter = None

        # this flag is set to True via `StreamReaderProtocol.connection_lost` =>
        # `<this mixin>.connection_lost` method calls
        self._connection_lost = False

    def pause_writing(self):
        """
        Called by a transport.

        Called when the transport's buffer goes over the high-water mark.

        Sets the self._paused to True, so when a user calls
            StreamWriter.write(data)
            await StreamWriter.drain()
        the `.drain()` method via this mixin's `._drain_helper()` method will
        create a `._drain_waiter` future if self._paused is True.
        It will block on this future until a call to `.resume_writing()` method
        happens, which completes the `._drain_waiter` future unblocking the call
        to await `._drain_waiter`. The `.resume_writing()` method will also
        set the self._paused flag to False.

        Pause and resume calls are paired -- pause_writing() is called
        once when the buffer goes strictly over the high-water mark
        (even if subsequent writes increases the buffer size even
        more), and eventually resume_writing() is called once when the
        buffer size reaches the low-water mark.

        Note that if the buffer size equals the high-water mark,
        pause_writing() is not called -- it must go strictly over.
        Conversely, resume_writing() is called when the buffer size is
        equal or lower than the low-water mark.  These end conditions
        are important to ensure that things go as expected when either
        mark is zero.

        NOTE: This is the only Protocol callback that is not called
        through EventLoop.call_soon() -- if it were, it would have no
        effect when it's most needed (when the app keeps writing
        without yielding until pause_writing() is called).
        """
        assert not self._paused
        self._paused = True
        if self._loop.get_debug():
            logger.debug("%r pauses writing", self)

    def resume_writing(self):
        """
        Called by a transport.

        Called when the transport's buffer drains below the low-water mark.

        See pause_writing() for details.

        If the StreamWriter wasn't previously paused, raises an assertion error.

        Sets the self._paused flag to False and if someone previously called
            StreamWriter.write(data)
            await StreamWriter.drain()
        which created a `._drain_waiter` future and blocked on awaiting it,
        then this call completes the future (if not already completed)
        effectively unblocking the call to await StreamWriter.drain().
        """
        assert self._paused
        self._paused = False
        if self._loop.get_debug():
            logger.debug("%r resumes writing", self)

        waiter = self._drain_waiter
        if waiter is not None:
            self._drain_waiter = None
            if not waiter.done():
                waiter.set_result(None)

    def connection_lost(self, exc):
        """
        Called by StreamReaderProtocol's `.connection_lost` method
        which writes eof of to its underlying StreamReader or sets an exception
        on it. So if someone previously blocked on
            StreamWriter.write(data)
            await StreamWriter.drain()
        this call will unblock the call to `await StreamWriter.drain()`
        by completing the `self._drain_waiter`waiter future (if exception
        was not None, will complete with this exception)
        """
        self._connection_lost = True
        # Wake up the writer if currently paused.
        if not self._paused:
            return
        waiter = self._drain_waiter
        if waiter is None:
            return
        self._drain_waiter = None
        if waiter.done():
            return
        if exc is None:
            waiter.set_result(None)
        else:
            waiter.set_exception(exc)

    async def _drain_helper(self):
        """
        Is called by StreamWriter's `.drain()` method usually in the following
        manner:
            StreamWriter.write(data)
            await StreamWriter.drain()
        With the help of this method `await StreamWriter.drain()` will
        immediately return if the underlying protocol is not paused by a
        transport, or will block on a `self._drain_waiter` future until the
        transport unblocks the `await waiter` call by calling this mixin's
        `.resume_writing()` method.
        If connection was lost await StreamWriter.drain() with the help
        of this method will raise a ConnectionResetError('Connection lost')
        exception.
        """
        if self._connection_lost:
            raise ConnectionResetError('Connection lost')
        if not self._paused:
            return
        waiter = self._drain_waiter
        assert waiter is None or waiter.cancelled()
        waiter = self._loop.create_future()
        self._drain_waiter = waiter
        await waiter

    def _get_close_waiter(self, stream):
        """
        Used by await StreamWriter.wait_closed().
        This method will be overridden in StreamReaderProtocol and return
        a future to block on until connection is lost.
        """
        raise NotImplementedError


class StreamReaderProtocol(FlowControlMixin, protocols.Protocol):
    """Helper class to adapt between Protocol and StreamReader.

    (This is a helper class instead of making StreamReader itself a
    Protocol subclass, because the StreamReader has other potential
    uses, and to prevent the user of the StreamReader to accidentally
    call inappropriate methods of the protocol.)
    """

    _source_traceback = None

    def __init__(self, stream_reader, client_connected_cb=None, loop=None):
        # call init of FlowControlMixin
        super().__init__(loop=loop)

        # StreamReader does all the heavy lifting, we keep a weakref to it
        if stream_reader is not None:
            self._stream_reader_wr = weakref.ref(stream_reader)
            self._source_traceback = stream_reader._source_traceback
        else:
            self._stream_reader_wr = None

        # TODO !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!! - add comments and
        #  check out what `create_server()` does
        if client_connected_cb is not None:
            # This is a stream created by the `create_server()` function.
            # Keep a strong reference to the reader until a connection
            # is established.
            self._strong_reader = stream_reader

        self._reject_connection = False
        # StreamWriter instance, is only created if client_connected_cb
        # was supplied, wil be passed to it (created in `self.connection_made`
        # method)
        self._stream_writer = None

        # the underlying transport which will pass read data to this protocol
        # which in turn will pass it to StreamReader
        self._transport = None
        # A client provided callback, which will be called with a StreamReader
        # and StreamWriter instance when connection is successfully established
        self._client_connected_cb = client_connected_cb

        self._over_ssl = False

        # will be used by clients in the following manner:
        #   `await StreamWriter.wait_closed()` =>
        #   `await <this protocol>._get_close_waiter()` =>
        #   which returns this future.
        # The call to `await StreamWriter.wait_closed()` will unblock when
        # `self._closed` future is completed via
        # `<this protocol>.connection_lost()` method which will set a None
        # result on it if no exception occurred, or set an exception on this
        # future if there was one (the exception will be retrieved upon this
        # object's GC via its __del__ method.
        self._closed = self._loop.create_future()

    @property
    def _stream_reader(self):
        if self._stream_reader_wr is None:
            return None
        # called to get strong reference from weakref
        return self._stream_reader_wr()

    def connection_made(self, transport):
        if self._reject_connection:
            context = {
                'message': ('An open stream was garbage collected prior to '
                            'establishing network connection; '
                            'call "stream.close()" explicitly.')
            }
            if self._source_traceback:
                context['source_traceback'] = self._source_traceback
            self._loop.call_exception_handler(context)
            # Close the transport immediately.
            # Buffered data will be lost.  No more data will be received.
            # The protocol's connection_lost() method will (eventually) be
            # called with None as its argument.
            transport.abort()
            return

        self._transport = transport
        reader = self._stream_reader
        # when a successful connection is made by a transport, set the
        # passed in transport on the StreamReader instance (it will
        # be able to communicate with it)
        if reader is not None:
            reader.set_transport(transport)

        self._over_ssl = transport.get_extra_info('sslcontext') is not None

        if self._client_connected_cb is not None:
            # TODO !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
            self._stream_writer = StreamWriter(transport, self,
                                               reader,
                                               self._loop)

            # pass StreamReader and StreamWriter instances to user-provided on
            # client connected callback
            res = self._client_connected_cb(reader,
                                            self._stream_writer)
            # if it's a coroutine (wasn't executed in the previous step), then
            # schedule it to be run by the loop
            if coroutines.iscoroutine(res):
                self._loop.create_task(res)
            self._strong_reader = None

    def connection_lost(self, exc):
        reader = self._stream_reader
        # if a StreamReader is still present on connection lost, if exception
        # is None signal EOF to it, if exc - signal an exception to it
        if reader is not None:
            if exc is None:
                reader.feed_eof()
            else:
                reader.set_exception(exc)
        # if `._closed` future isn't completed yet, complete it with
        # exception or a None result
        if not self._closed.done():
            if exc is None:
                self._closed.set_result(None)
            else:
                self._closed.set_exception(exc)

        # If protocol isn't paused will return immediately
        # If protocol is current;y paused will unblock the call
        # to `await StreamWriter.drain()` by completing the `._drain_waiter`
        # future (if exception  was not None, will complete with the exception
        # passed in)
        super().connection_lost(exc)

        self._stream_reader_wr = None
        self._stream_writer = None
        self._transport = None

    def data_received(self, data):
        """
        Called by a transport when it receives some data
        The argument is a bytes object containing the incoming data.

        The incoming data is 'fed' to the underlying StreamReader instance
        (if any) which in turn unblocks any calls to the StreamReader's
        `.read()`/`.readline()`/`.readuntil()`/`.readexactly()` methods
        via `StreamReader._wakeup_waiter` method.
        After this if conditions are met,
        `.read()`/`.readline()`/`.readuntil()`/`.readexactly()` may return
        the read data to the caller.
        """
        reader = self._stream_reader
        if reader is not None:
            reader.feed_data(data)

    def eof_received(self):
        """
        Called when the other end calls write_eof()
        (another asyncio protocol => transport chain) or equivalent.

        If this returns a false value (including None), the transport
        will close itself.  If it returns a true value, closing the
        transport is up to the protocol.

        The incoming eof is 'fed' to the underlying StreamReader instance
        (if any) which in turn unblocks any calls to the StreamReader's
        `.read()`/`.readline()`/`.readuntil()`/`.readexactly()` methods
        via `StreamReader._wakeup_waiter` method and sets ._eof flag to True,
        so that `.read()`/`.readline()`/`.readuntil()`/`.readexactly()` can
        act accordingly after unblocking.


        """
        reader = self._stream_reader
        if reader is not None:
            reader.feed_eof()
        if self._over_ssl:
            # Prevent a warning in SSLProtocol.eof_received:
            # "returning true from eof_received()
            # has no effect when using ssl"
            # This is because SSL doesn't support 'half-closed' connections,
            # so the transport will close itself anyway even if we return True
            return False
        # True - closing the transport is up to the protocol
        return True

    def _get_close_waiter(self, stream):
        """
        Used by await StreamWriter.wait_closed()
        """
        return self._closed

    def __del__(self):
        # Prevent reports about unhandled exceptions.
        # Better than self._closed._log_traceback = False hack
        closed = self._closed
        if closed.done() and not closed.cancelled():
            # Exception will be set if there was one by `self.connection_lost`
            # method
            closed.exception()


class StreamWriter:
    """Wraps a Transport.

    This exposes write(), writelines(), [can_]write_eof(),
    get_extra_info() and close().  It adds drain() which returns an
    optional Future on which you can wait for flow control.  It also
    adds a transport property which references the Transport
    directly.
    """

    def __init__(self, transport, protocol, reader, loop):
        # is a transport
        self._transport = transport
        # is a StreamReaderProtocol
        self._protocol = protocol
        # drain() expects that the reader has an exception() method
        assert reader is None or isinstance(reader, StreamReader)
        # is None or a StreamReader
        self._reader = reader
        self._loop = loop
        # TODO !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
        #  add comments to attrs
        self._complete_fut = self._loop.create_future()
        self._complete_fut.set_result(None)

    # TODO !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
    #   add comments when you examine at least one transport
    @property
    def transport(self):
        return self._transport

    def write(self, data):
        self._transport.write(data)

    def writelines(self, data):
        self._transport.writelines(data)

    def write_eof(self):
        return self._transport.write_eof()

    def can_write_eof(self):
        return self._transport.can_write_eof()

    def close(self):
        return self._transport.close()

    def is_closing(self):
        return self._transport.is_closing()

    async def wait_closed(self):
        # TODO !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
        await self._protocol._get_close_waiter(self)

    def get_extra_info(self, name, default=None):
        return self._transport.get_extra_info(name, default)

    # TODO !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
    #   add more detailed doc string
    async def drain(self):
        """Flush the write buffer.

        The intended use is to write

          w.write(data)
          await w.drain()
        """
        # first check if StreamReader produced an exception
        if self._reader is not None:
            exc = self._reader.exception()
            if exc is not None:
                raise exc

        if self._transport.is_closing():
            # Wait for protocol.connection_lost() call
            # Raise connection closing error if any,
            # ConnectionResetError otherwise
            # Yield to the event loop so connection_lost() may be
            # called.  Without this, _drain_helper() would return
            # immediately, and code that calls
            #     write(...); await drain()
            # in a loop would never call connection_lost(), so it
            # would not see an error when the socket is closed.
            await sleep(0)

        # 1) If `._connection_lost` flag was set to True on the protocol -
        #    raises ConnectionResetError('Connection lost')
        # 2) If protocol wasn't paused by transport via `protocol.pause_writing`
        #    method - returns immediately
        # 3) If protocol is paused by transport via it's `.pause_writing` method
        #    because the transport's buffer went over the high-water mark -
        #    creates a waiter future and blocks on awaiting this future. The
        #    call will unblock when the transport calls protocol's
        #    `.resume_writing` method setting an exception or result on
        #    the waiter future (transport will do this when its buffer drains
        #    below the low-water mark)
        await self._protocol._drain_helper()


class StreamReader:
    _source_traceback = None

    def __init__(self, limit=_DEFAULT_LIMIT, loop=None):
        # The line length limit is a security feature;
        # it also doubles as half the buffer limit.

        if limit <= 0:
            raise ValueError('Limit cannot be <= 0')

        # this represents the maximum number of bytes in a line
        self._limit = limit

        if loop is None:
            self._loop = events.get_event_loop()
        else:
            self._loop = loop

        # this is the buffer into which transports pass read data
        # via `self.feed_data` method, so it can later be retrieved and
        # eventually returned to the caller via
        # `.read()`/`.readline()`/`.readuntil()`/`.readexactly()` methods
        self._buffer = bytearray()
        self._eof = False  # Whether we're done.
        self._waiter = None  # A future used by _wait_for_data()
        self._exception = None
        self._transport = None
        self._paused = False

        if self._loop.get_debug():
            self._source_traceback = format_helpers.extract_stack(
                sys._getframe(1))

    def __repr__(self):
        info = ['StreamReader']
        if self._buffer:
            info.append(f'{len(self._buffer)} bytes')
        if self._eof:
            info.append('eof')
        if self._limit != _DEFAULT_LIMIT:
            info.append(f'limit={self._limit}')
        if self._waiter:
            info.append(f'waiter={self._waiter!r}')
        if self._exception:
            info.append(f'exception={self._exception!r}')
        if self._transport:
            info.append(f'transport={self._transport!r}')
        if self._paused:
            info.append('paused')
        return '<{}>'.format(' '.join(info))

    def exception(self):
        return self._exception

    def set_exception(self, exc):
        """
        If currently `.read()`/`.readline()`/`.readuntil()`/`.readexactly()`
        are blocked on `await waiter` waiting for data, the exception
        will be propagated to these method calls and eventually to their
        caller.
        """
        self._exception = exc

        waiter = self._waiter
        if waiter is not None:
            self._waiter = None
            if not waiter.cancelled():
                waiter.set_exception(exc)

    def _wakeup_waiter(self):
        """
        Wakeup read*() functions waiting for data or EOF.

        Called by `.feed_eof` and `.feed_data`.

        This method sets result on the `self._waiter` future unblocking
        calls to read*() functions which have blocked awaiting
        the `self._waiter` future via
        `self._wait_for_data()` => `await self._waiter`.
        """
        waiter = self._waiter
        if waiter is not None:
            self._waiter = None
            if not waiter.cancelled():
                waiter.set_result(None)

    def set_transport(self, transport):
        assert self._transport is None, 'Transport already set'
        self._transport = transport

    def _maybe_resume_transport(self):
        """
        This is usually called by `.read*() methods, after they have
        retrieved some data from self._buffer
        """
        if self._paused and len(self._buffer) <= self._limit:
            self._paused = False
            self._transport.resume_reading()

    def feed_eof(self):
        # mark that we've reached eof
        self._eof = True
        # unblock calls to `.read`/`.readline`/`.readuntil` methods so they
        # can read data from `self._buffer`
        self._wakeup_waiter()

    def at_eof(self):
        """Return True if the buffer is empty and 'feed_eof' was called."""
        return self._eof and not self._buffer

    def feed_data(self, data):
        assert not self._eof, 'feed_data after feed_eof'

        if not data:
            return

        # extend bytearray buffer with data, so after
        # self._wakeup_waiter() will unblock call
        # to `.read`/`.readline`/`.readuntil`, one of these method calls
        # will get the data from the bytearray buffer
        self._buffer.extend(data)
        # unblock calls to `.read`/`.readline`/`.readuntil` methods so they
        # can read data from `self._buffer`
        self._wakeup_waiter()

        # if not already paused and size of data in buffer is two times
        # greater than self._limit (64 KiB) try to pause transport
        if (self._transport is not None and
                not self._paused and
                len(self._buffer) > 2 * self._limit):
            try:
                self._transport.pause_reading()
            except NotImplementedError:
                # The transport can't be paused.
                # We'll just have to buffer all data.
                # Forget the transport so we don't keep trying.
                self._transport = None
            else:
                self._paused = True

    async def _wait_for_data(self, func_name):
        """Wait until feed_data() or feed_eof() is called.

        If stream was paused, automatically resume it (usually a stream
        is paused by calling `.pause_reading()` on this protocol's transport
        instance, and resumed by calling `.resume_reading()` on this protocol's
        transport instance.

        This is used by `.read()`/`.readline()`/`.readuntil()` methods
        to block until data is ready to be read from bytearray `self._buffer`.
        This call will unblock when `.feed_eof()`/`.feed_data()` methods
        are called filling the buffer with data and completing the waiter
        future via `self._wakeup_waiter()` => `waiter.set_result(None)`.
        """
        # StreamReader uses a future to link the protocol feed_data() method
        # to a read coroutine. Running two read coroutines at the same time
        # would have an unexpected behaviour. It would not be possible to know
        # which coroutine would get the next data.
        if self._waiter is not None:
            raise RuntimeError(
                f'{func_name}() called while another coroutine is '
                f'already waiting for incoming data')

        assert not self._eof, '_wait_for_data after EOF'

        # Waiting for data while paused will deadlock, so prevent it.
        # This is essential for readexactly(n) for case when n > self._limit.
        if self._paused:
            self._paused = False
            self._transport.resume_reading()

        # When data will be available for reading, `.feed_data()`/`.feed_eof()`
        # will set result on this future unblocking call to `await self._waiter`.
        self._waiter = self._loop.create_future()
        try:
            await self._waiter
        finally:
            self._waiter = None

    async def readline(self):
        """Read chunk of data from the stream until newline (b'\n') is found.

        On success, return chunk that ends with newline. If only partial
        line can be read due to EOF, return incomplete line without
        terminating newline. When EOF was reached while no bytes read, empty
        bytes object is returned.

        If limit is reached, ValueError will be raised. In that case, if
        newline was found, complete line including newline will be removed
        from internal buffer. Else, internal buffer will be cleared. Limit is
        compared against part of the line without newline.

        If stream was paused, this function will automatically resume it if
        needed.
        """
        sep = b'\n'
        seplen = len(sep)

        try:
            line = await self.readuntil(sep)
        # if eof occurred before a newline ('\n') was reached the incomplete
        # line will be present in IncompleteReadError's .partial attr
        except exceptions.IncompleteReadError as e:
            return e.partial
        except exceptions.LimitOverrunError as e:
            # B.startswith(prefix[, start[, end]]) -> bool
            # Return True if B starts with the specified prefix, False otherwise.
            # With optional start, test B beginning at that position.
            # With optional end, stop comparing B at that position.
            # prefix can also be a tuple of bytes to try.
            if self._buffer.startswith(sep, e.consumed):
                del self._buffer[:e.consumed + seplen]
            else:
                self._buffer.clear()

            self._maybe_resume_transport()
            # args[0] is the error message -
            # 'Separator is found, but chunk is longer than limit'
            raise ValueError(e.args[0])

        return line

    async def readuntil(self, separator=b'\n'):
        """Read data from the stream until ``separator`` is found.

        On success, the data and separator will be removed from the
        internal buffer (consumed). Returned data will include the
        separator at the end.

        Configured stream limit is used to check result. Limit sets the
        maximal length of data that can be returned, not counting the
        separator.

        If an EOF occurs and the complete separator is still not found,
        an IncompleteReadError exception will be raised, and the internal
        buffer will be reset.  The IncompleteReadError.partial attribute
        may contain the separator partially.

        If the data cannot be read because of over limit, a
        LimitOverrunError exception  will be raised, and the data
        will be left in the internal buffer, so it can be read again.
        """
        seplen = len(separator)
        if seplen == 0:
            raise ValueError('Separator should be at least one-byte string')

        # If exception was previously set by a call to `.set_exception()`
        # then we just raise it without trying to read
        if self._exception is not None:
            raise self._exception

        # Consume whole buffer except last bytes, which length is
        # one less than seplen. Let's check corner cases with
        # separator='SEPARATOR':
        # * we have received almost complete separator (without last
        #   byte). i.e buffer='some textSEPARATO'. In this case we
        #   can safely consume len(separator) - 1 bytes.
        # * last byte of buffer is first byte of separator, i.e.
        #   buffer='abcdefghijklmnopqrS'. We may safely consume
        #   everything except that last byte, but this require to
        #   analyze bytes of buffer that match partial separator.
        #   This is slow and/or require FSM. For this case our
        #   implementation is not optimal, since require rescanning
        #   of data that is known to not belong to separator. In
        #   real world, separator will not be so long to notice
        #   performance problems. Even when reading MIME-encoded
        #   messages :)

        # `offset` is the number of bytes from the beginning of the buffer
        # where there is no occurrence of `separator`.
        offset = 0

        # Loop until we find `separator` in the buffer, exceed the buffer size,
        # or an EOF has happened.
        while True:
            buflen = len(self._buffer)

            # Check if we now have enough data in the buffer for `separator` to
            # fit.
            if buflen - offset >= seplen:
                # B.find(sub[, start[, end]]) -> int
                # Return the lowest index in B where subsection sub is found,
                # such that sub is contained within B[start,end].  Optional
                # arguments start and end are interpreted as in slice notation.
                # Return -1 on failure.
                isep = self._buffer.find(separator, offset)

                if isep != -1:
                    # `separator` is in the buffer. `isep` will be used later
                    # to retrieve the data.
                    break

                # see upper comment for explanation.
                offset = buflen + 1 - seplen
                if offset > self._limit:
                    raise exceptions.LimitOverrunError(
                        'Separator is not found, and chunk exceed the limit',
                        offset)

            # Complete message (with full separator) may be present in buffer
            # even when EOF flag is set. This may happen when the last chunk
            # adds data which makes separator be found. That's why we check for
            # EOF *ater* inspecting the buffer.
            if self._eof:
                chunk = bytes(self._buffer)
                self._buffer.clear()
                raise exceptions.IncompleteReadError(chunk, None)

            # _wait_for_data() will resume reading if stream was paused.
            # will block until `.feed_data()`/`.feed_eof()` is called
            await self._wait_for_data('readuntil')

        if isep > self._limit:
            raise exceptions.LimitOverrunError(
                'Separator is found, but chunk is longer than limit', isep)

        # isep - now represents the starting index of the separator
        # so chunk is now the complete data including the separator
        chunk = self._buffer[:isep + seplen]
        # remove all data including the separator from buffer
        del self._buffer[:isep + seplen]
        # if size of data in buffer after removing the data from it
        # is less than or equal to self._limit, resume transport
        # (if it was previously paused), so it can further feed data to us
        self._maybe_resume_transport()
        # return complete data up to and including the separator
        return bytes(chunk)

    async def read(self, n=-1):
        """Read up to `n` bytes from the stream.

        If n is not provided, or set to -1, read until EOF and return all read
        bytes. If the EOF was received and the internal buffer is empty, return
        an empty bytes object.

        If n is zero, return empty bytes object immediately.

        If n is positive, this function try to read `n` bytes, and may return
        less or equal bytes than requested, but at least one byte. If EOF was
        received before any byte is read, this function returns empty byte
        object.

        Returned value is not limited with limit, configured at stream
        creation.

        If stream was paused, this function will automatically resume it if
        needed.
        """
        # If exception was previously set by a call to `.set_exception()`
        # then we just raise it without trying to read
        if self._exception is not None:
            raise self._exception

        # return empty bytes object if 0 bytes were requested
        # to be read
        if n == 0:
            return b''

        # Try to read until EOF
        if n < 0:
            # This used to just loop creating a new waiter hoping to
            # collect everything in self._buffer, but that would
            # deadlock if the subprocess sends more than self.limit
            # bytes. So just call self.read(self._limit) until EOF.
            blocks = []
            while True:
                block = await self.read(self._limit)
                if not block:
                    break
                blocks.append(block)
            return b''.join(blocks)

        # if buffer is empty and we're not at eof - block on waiter future
        # will be unblocked by calls to `.feed_data()`/`.feed_eof()` which
        # will put data into self._buffer
        if not self._buffer and not self._eof:
            await self._wait_for_data('read')

        # This will work right even if buffer is less than n bytes
        data = bytes(self._buffer[:n])
        del self._buffer[:n]

        # if size of data in buffer after reading is less than or equal
        # to self._limit, resume transport (if it was previously paused),
        # so it can further feed data to us
        self._maybe_resume_transport()

        # `data` will be returned to recursive call
        # `block = await self.read(self._limit)`
        return data

    async def readexactly(self, n):
        """Read exactly `n` bytes.

        Raise an IncompleteReadError if EOF is reached before `n` bytes can be
        read. The IncompleteReadError.partial attribute of the exception will
        contain the partial read bytes.

        if n is zero, return empty bytes object.

        Returned value is not limited with limit, configured at stream
        creation.

        If stream was paused, this function will automatically resume it if
        needed.
        """
        if n < 0:
            raise ValueError('readexactly size can not be less than zero')

        # If exception was previously set by a call to `.set_exception()`
        # then we just raise it without trying to read
        if self._exception is not None:
            raise self._exception

        # return empty bytes object if 0 bytes were requested
        # to be read
        if n == 0:
            return b''

        # while buffer is not filled up with the requested number of bytes
        while len(self._buffer) < n:
            # if eof was reached and the buffer is still not filled up
            # with the requested number of bytes, consumer all read data from
            # buffer and clear it, raise IncompleteReadError with all
            # data read in .partial attr
            if self._eof:
                incomplete = bytes(self._buffer)
                self._buffer.clear()
                raise exceptions.IncompleteReadError(incomplete, n)

            # block on waiter future
            # will be unblocked by calls to `.feed_data()`/`.feed_eof()` which
            # will put data into self._buffer
            await self._wait_for_data('readexactly')

        # if exactly `n` bytes were read into buffer, then consume all bytes
        # from buffer and clear it
        if len(self._buffer) == n:
            data = bytes(self._buffer)
            self._buffer.clear()
        # if more than requested (`n`) number of bytes were read into buffer,
        # consume from buffer up to the requested size and delete data from
        # buffer up to requested size
        else:
            data = bytes(self._buffer[:n])
            del self._buffer[:n]

        # if size of data in buffer after reading is less than or equal
        # to self._limit, resume transport (if it was previously paused),
        # so it can further feed data to us
        self._maybe_resume_transport()

        # return read data of requested size
        return data

    def __aiter__(self):
        return self

    async def __anext__(self):
        # async iteration will read line by line
        # until there's no data to be read
        val = await self.readline()
        if val == b'':
            raise StopAsyncIteration
        return val
