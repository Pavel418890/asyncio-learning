import concurrent.futures
import contextvars
import logging
import sys


from . import format_helpers
from . import events
from . import base_futures
from . import exceptions

# check comments, uses C code under the hood
isfuture = base_futures.isfuture

# check comments, use C code under the hood
_PENDING = base_futures._PENDING
_CANCELLED = base_futures._CANCELLED
_FINISHED = base_futures._FINISHED

STACK_DEBUG = logging.DEBUG - 1  # heavy-duty debugging


class Future:
    """
    Now is implemented almost exclusively in C (built-in/extension type).

    This class is *almost* compatible with concurrent.futures.Future.

    Differences:

    - This class is not thread-safe.

    - result() and exception() do not take a timeout argument and
      raise an exception when the future isn't done yet.

    - Callbacks registered with add_done_callback() are always called
      via the event loop's call_soon().

    - This class is not compatible with the wait() and as_completed()
      methods in the concurrent.futures package.

    (In Python 3.4 or later we may be able to unify the implementations.)
    """

    # Class variables serving as defaults for instance variables.

    # getter implemented in C - FutureObj_get_state (no special setter):
    # when retrieved ensures that the Future was initialized/alive
    # (loop is not None)
    # returns unicode repr from int
    _state = _PENDING

    # getter implemented in C - FutureObj_get_result (no special setter):
    # when retrieved ensures that the Future was initialized/alive
    # (loop is not None),
    # increments ref to _result and returns the result object,
    # if _result is NULL, returns None
    _result = None

    # getter implemented in C - FutureObj_get_exception (no special setter):
    # when retrieved ensures that the Future was initialized/alive
    # (loop is not None),
    # increments ref to _exception and returns the Exception object,
    # if _exception is NULL, returns None
    _exception = None

    # getter implemented in C - FutureObj_get_loop (no special setter):
    # when retrieved ensures that the Future was initialized/alive
    # (loop is not None), else
    # returns None. If alive increments ref to _loop and returns it.
    _loop = None

    # getter implemented in C - FutureObj_get_source_traceback
    # (no special setter):
    # if Future is not alive (loop is not None)
    # or _source_traceback is NULL returns None,
    # else increments ref to _source_traceback and returns it.
    _source_traceback = None

    # getter and setter implemented in C:
    # 1) getter FutureObj_get_cancel_message:
    #    if _cancel_message is NULL, returns None, else increments ref
    #    to _cancel_message and returns it
    # 2) setter FutureObj_set_cancel_message:
    #    ensures that this attribute cannot be deleted, else sets the message
    _cancel_message = None

    # A saved CancelledError for later chaining as an exception context.
    _cancelled_exc = None

    # This field is used for a dual purpose:
    # - Its presence is a marker to declare that a class implements
    #   the Future protocol (i.e. is intended to be duck-type compatible)
    #   + this attr cannot be deleted from Future.
    #   The value must also be not-None, to enable a subclass to declare
    #   that it is not compatible by setting this to None.
    # - It is set by __iter__() below so that Task._step() can tell
    #   the difference between
    #   `await Future()` or`yield from Future()` (correct) vs.
    #   `yield Future()` (incorrect).
    # 1) getter FutureObj_get_cancel_message:
    #    returns True if the future is alive (loop is not None) and
    #    _asyncio_future_blocking is True, else returns False.
    # 2) setter FutureObj_set_cancel_message:
    #    ensures on C level that the _asyncio_future_blocking
    #    attribute cannot be deleted, also ensures that the future is initialized
    #    before returning the value of _asyncio_future_blocking.
    _asyncio_future_blocking = False  # getter and setter implemented in C

    # getter and setter implemented in C
    # 1) getter FutureObj_get_log_traceback -
    #    ensures future is alive (loop is not None) and returns value
    # 2) setter FutureObj_set_log_traceback -
    #    ensures that this attribute cannot be deleted, also ensure that this
    #    __log_traceback can only be set to False in python code

    # is set to True when self.set_exception method is called
    __log_traceback = False

    def __init__(self, *, loop=None):
        """
        Implemented in C - _asyncio_Future___init___impl which calls
        future_init (also C code).

        Initialize the future.

        The optional event_loop argument allows explicitly setting the event
        loop object used by the future. If it's not provided, the future uses
        the default event loop.
        """
        #  this is done in C (future_init)
        if loop is None:
            # gets the loop from thread local storage (sets it if not set),
            # or gets it from cached loop static variable
            self._loop = events.get_event_loop()
        else:
            self._loop = loop

        # in C
        self._callbacks = []

        # now all implemented in C
        if self._loop.get_debug():
            self._source_traceback = format_helpers.extract_stack(
                sys._getframe(1))

    # is implemented in C (_asyncio_Future__repr_info_impl) but calls the same
    # Python implementation of base_futures._future_repr_info under the hood.
    _repr_info = base_futures._future_repr_info

    def __repr__(self):
        # base_futures._future_repr_info is called only with self as the future
        # arg
        return '<{} {}>'.format(self.__class__.__name__,
                                ' '.join(self._repr_info()))

    def __del__(self):
        """
        Is implemented in C: FutureObj_finalize.
        The C code does basically the same as this pure python implementation.

        Exception traceback will be logged upon GC if .set_exception()
        was called but the result of the future or its exception were not
        retrieved before GC happened.
        """
        if not self.__log_traceback:
            # set_exception() was not called, or result() or exception()
            # has consumed the exception
            # self.__log_traceback can only be set to True when calling
            # .set_exception(), in all other cases it gets set to False.
            # It cannot be set to True from the outside either.
            return
        exc = self._exception
        context = {
            'message':
                f'{self.__class__.__name__} exception was never retrieved',
            'exception': exc,
            'future': self,
        }
        if self._source_traceback:
            context['source_traceback'] = self._source_traceback
        # TODO !!!!!!!!!!!!!!!!!!!!!!!!!!!
        self._loop.call_exception_handler(context)

    # exactly as __getitem__, but is called on the class itself,
    # not the instance
    # implemented in C, does the same as this python code
    def __class_getitem__(cls, type):
        return cls

    @property
    def _log_traceback(self):
        return self.__log_traceback

    @_log_traceback.setter
    def _log_traceback(self, val):
        if val:
            raise ValueError('_log_traceback can only be set to False')
        self.__log_traceback = False

    def get_loop(self):
        """
        Return the event loop the Future is bound to.

        Is implemented in C, exactly the same but also checks if Future is alive
        (loop is not None), if not alive, returns None.
        """
        loop = self._loop
        if loop is None:
            raise RuntimeError("Future object is not initialized.")
        return loop

    def _make_cancelled_error(self):
        """
        This method is implemented in C (does the same thing as this python code)
        _asyncio_Future__make_cancelled_error_impl calls create_cancelled_error
        which uses the pure python implementation of CancelledError.

        Create the CancelledError to raise if the Future is cancelled.

        This should only be called once when handling a cancellation since
        it erases the saved context exception value.
        """
        if self._cancel_message is None:
            exc = exceptions.CancelledError()
        else:
            exc = exceptions.CancelledError(self._cancel_message)
        # __cause__ is the cause of the exception - due to the given exception,
        # the current exception was raised.
        # This is a direct link - X threw this exception,
        # therefore Y has to throw this exception.

        # __context__ on the other hand means that the current exception was
        # raised while trying to handle another exception, and defines
        # the exception that was being handled at the time this one was raised.
        # This is so that you don't lose the fact that the other exceptions
        # happened (and hence were at this code to throw the exception) -
        # the context. X threw this exception, while handling it,
        # Y was also thrown.
        exc.__context__ = self._cancelled_exc
        # Remove the reference since we don't need this anymore.
        self._cancelled_exc = None
        return exc

    def cancel(self, msg=None):
        """Cancel the future and schedule callbacks.

        If the future is already done or cancelled, return False.  Otherwise,
        change the future's state to cancelled, schedule the callbacks and
        return True.

        Implemented in C:
        _asyncio_Future_cancel_impl ensures that the Future is alive
        (self._loop is non None) and the calls future_cancel
        which does practically the same stuff as this pure python
        implementation.
        """
        self.__log_traceback = False
        if self._state != _PENDING:
            return False
        self._state = _CANCELLED
        self._cancel_message = msg
        self.__schedule_callbacks()
        return True

    def __schedule_callbacks(self):
        """Internal: Ask the event loop to call all callbacks.

        The callbacks are scheduled to be called as soon as possible. Also
        clears the callback list.

        Is implemented in C 'future_schedule_callbacks'.
        Differs a little from this pure python implementation:

        Callbacks in the future object are stored as follows:

        callback0 -- a pointer to the first callback (for optimization)
        callbacks -- a list of 2nd, 3rd, ... callbacks

        1) If there is a first callback self._loop.call_soon is called on it,
          if an error occurs, all the remaining callbacks from the callback
          list are cleared and not called.
        2) The remaining 2nd, 3rd, ... callbacks are called if any via
           self._loop.call_soon. If an error occurs during calling one of the
           callbacks the rest is cleared and never called.

        All other implementation details remain practically the same as in
        pure python.
        """
        callbacks = self._callbacks[:]
        if not callbacks:
            return

        self._callbacks[:] = []

        # TODO !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
        for callback, ctx in callbacks:
            self._loop.call_soon(callback, self, context=ctx)

    def cancelled(self):
        """
        Return True if the future was cancelled.

        Implemented in C (does the same stuff as this python code,
        but also checks if the Future is alive (loop is not None))
        _asyncio_Future_cancelled_impl
        """
        return self._state == _CANCELLED

    # Don't implement running(); see http://bugs.python.org/issue18699

    def done(self):
        """Return True if the future is done.

        Done means either that a result / exception are available, or that the
        future was cancelled.

        Implemented in C (does the same stuff as this python code,
        but also checks if the Future is alive (loop is not None))
        _asyncio_Future_done_impl
        """
        return self._state != _PENDING

    def result(self):
        """
        Return the result this future represents.

        If the future has been cancelled, raises CancelledError.  If the
        future's result isn't yet available, raises InvalidStateError.  If
        the future is done and has an exception set, this exception is raised.

        Implemented in C:
        does the same stuff as this pure python implementation
        (adds a future alive check at the start of the method call
        (self._loop is not None).
        _asyncio_Future_result_impl calls future_get_result
        """
        if self._state == _CANCELLED:
            exc = self._make_cancelled_error()
            raise exc
        if self._state != _FINISHED:
            raise exceptions.InvalidStateError('Result is not ready.')
        self.__log_traceback = False
        if self._exception is not None:
            raise self._exception
        return self._result

    def exception(self):
        """Return the exception that was set on this future.

        The exception (or None if no exception was set) is returned only if
        the future is done.  If the future has been cancelled, raises
        CancelledError.  If the future isn't done yet, raises
        InvalidStateError.

        Implemented in C:
        does the same stuff as this pure python implementation
        (adds a future alive check at the start of the method call
        (self._loop is not None).
        asyncio_Future_exception_impl
        """
        if self._state == _CANCELLED:
            exc = self._make_cancelled_error()
            raise exc
        if self._state != _FINISHED:
            raise exceptions.InvalidStateError('Exception is not set.')
        self.__log_traceback = False
        return self._exception

    def add_done_callback(self, fn, *, context=None):
        """Add a callback to be run when the future becomes done.

        The callback is called with a single argument - the future object. If
        the future is already done when this is called, the callback is
        scheduled with call_soon.

        Implemented in pure C:
        _asyncio_Future_add_done_callback_impl -> future_add_done_callback
        Does pretty much the same as this pure python implementation.
        """
        if self._state != _PENDING:
            self._loop.call_soon(fn, self, context=context)
        else:
            if context is None:
                # we get the current active context and pass it as the argument
                context = contextvars.copy_context()
            self._callbacks.append((fn, context))

    def remove_done_callback(self, fn):
        """Remove all instances of a callback from the "call when done" list.

        Returns the number of callbacks removed.

        Implemented in C:
        Same implementation but also checks for future liveliness
        (loop is not None) and checks fut_callback0 and fut_context0
        (first in the list)
        in addition to the list (2nd, 3rd, etc) of callbacks.
        _asyncio_Future_remove_done_callback
        """
        filtered_callbacks = [(f, ctx)
                              for (f, ctx) in self._callbacks
                              if f != fn]
        removed_count = len(self._callbacks) - len(filtered_callbacks)
        if removed_count:
            self._callbacks[:] = filtered_callbacks
        return removed_count

    # So-called internal methods (note: no set_running_or_notify_cancel()).

    def set_result(self, result):
        """Mark the future done and set its result.

        If the future is already done when this method is called, raises
        InvalidStateError.

        Implemented in C:
        same stuff as this pure python implementation, but checks if loop
        is alive before doing anything else (self._loop is not None).
        _asyncio_Future_set_result -> future_set_result
        """
        if self._state != _PENDING:
            raise exceptions.InvalidStateError(f'{self._state}: {self!r}')
        self._result = result
        self._state = _FINISHED
        self.__schedule_callbacks()

    def set_exception(self, exception):
        """Mark the future done and set an exception.

        If the future is already done when this method is called, raises
        InvalidStateError.

        Implemented in C:
        does pretty much the same stuff as this pure python implementation
        but check if the future is alive before doing anything else
        (self._loop is not None).
        _asyncio_Future_set_exception => future_set_exception
        """
        if self._state != _PENDING:
            raise exceptions.InvalidStateError(f'{self._state}: {self!r}')
        if isinstance(exception, type):
            exception = exception()
        if type(exception) is StopIteration:
            raise TypeError("StopIteration interacts badly with generators "
                            "and cannot be raised into a Future")
        self._exception = exception
        self._state = _FINISHED
        self.__schedule_callbacks()
        self.__log_traceback = True

    def __await__(self):
        if not self.done():
            # It is set by __iter__() below so that Task._step() can tell
            # the difference between
            # `await Future()` or`yield from Future()` (correct) vs.
            # `yield Future()` (incorrect).
            self._asyncio_future_blocking = True
            yield self  # This tells Task to wait for completion.
        if not self.done():
            raise RuntimeError("await wasn't used with future")
        return self.result()  # May raise too.

    __iter__ = __await__  # make compatible with 'yield from'.


# Needed for testing purposes.
_PyFuture = Future


def _get_loop(fut):
    """
    Implemented in C:
    Does pretty much the same as this pure python implementation,
    but first tries a fast path of execution:
    Check if the fut passed in is an instance of Future or Task, and if it is
    returns its C attribute ((FutureObj *)fut)->fut_loop straight away.
    If this doesn't happen then it falls back to almost the same logic as in this
    pure python implementation.
    get_future_loop
    """
    # Tries to call Future.get_loop() if it's available.
    # Otherwise fallbacks to using the old '_loop' property.
    try:
        get_loop = fut.get_loop
    except AttributeError:
        pass
    else:
        return get_loop()
    return fut._loop


def _set_result_unless_cancelled(fut, result):
    """Helper setting the result only if the future was not cancelled."""
    if fut.cancelled():
        return
    fut.set_result(result)


def _convert_future_exc(exc):
    """
    Converts exceptions from concurrent.futures exception to their equivalent
    asyncio exceptions or returns the exception unmodified.
    """
    exc_class = type(exc)
    if exc_class is concurrent.futures.CancelledError:
        return exceptions.CancelledError(*exc.args)
    elif exc_class is concurrent.futures.TimeoutError:
        return exceptions.TimeoutError(*exc.args)
    elif exc_class is concurrent.futures.InvalidStateError:
        return exceptions.InvalidStateError(*exc.args)
    else:
        return exc


def _set_concurrent_future_state(concurrent, source):
    """ Copy state from a future to a concurrent.futures.Future. """
    assert source.done()
    if source.cancelled():
        concurrent.cancel()
    #  If the concurrent.future has been cancelled (cancel() was called
    #  and returned True) then any threads waiting on the future completing
    #  (though calls to as_completed() or wait()) are notified and False
    #  is returned.
    if not concurrent.set_running_or_notify_cancel():
        return
    # if not cancelled we either set the exception on the concurrent.future
    # or result
    exception = source.exception()
    if exception is not None:
        concurrent.set_exception(_convert_future_exc(exception))
    else:
        result = source.result()
        concurrent.set_result(result)


def _copy_future_state(source, dest):
    """Internal helper to copy state from another Future.

    The other Future may be a concurrent.futures.Future.
    """
    assert source.done()
    if dest.cancelled():
        return
    assert not dest.done()
    if source.cancelled():
        dest.cancel()
    else:
        exception = source.exception()
        if exception is not None:
            dest.set_exception(_convert_future_exc(exception))
        else:
            result = source.result()
            dest.set_result(result)


def _chain_future(source, destination):
    """Chain two futures so that when one completes, so does the other.

    The result (or exception) of source will be copied to destination.
    If destination is cancelled, source gets cancelled too.
    Compatible with both asyncio.Future and concurrent.futures.Future.
    """
    if not isfuture(source) and not isinstance(source,
                                               concurrent.futures.Future):
        raise TypeError('A future is required for source argument')
    if not isfuture(destination) and not isinstance(destination,
                                                    concurrent.futures.Future):
        raise TypeError('A future is required for destination argument')

    # get the event loops for the futures if they are asyncio.Future instances
    source_loop = _get_loop(source) if isfuture(source) else None
    dest_loop = _get_loop(destination) if isfuture(destination) else None

    def _set_state(future, other):
        if isfuture(future):
            _copy_future_state(other, future)
        else:
            _set_concurrent_future_state(future, other)

    def _call_check_cancel(destination):
        if destination.cancelled():
            # if source is from the same loop as destination or its a
            # concurrent.Future then just cancel it directly
            if source_loop is None or source_loop is dest_loop:
                source.cancel()
            # allows to submit work to the event loop from a different thread
            else:
                source_loop.call_soon_threadsafe(source.cancel)

    def _call_set_state(source):
        if (destination.cancelled() and
                dest_loop is not None and dest_loop.is_closed()):
            return
        if dest_loop is None or dest_loop is source_loop:
            _set_state(destination, source)
        else:
            dest_loop.call_soon_threadsafe(_set_state, destination, source)

    # if destination was cancelled then the source will be cancelled when
    # done callbacks are called
    destination.add_done_callback(_call_check_cancel)
    # The result (or exception) of source will be copied to destination.
    source.add_done_callback(_call_set_state)


def wrap_future(future, *, loop=None):
    """Wrap concurrent.futures.Future object."""
    if isfuture(future):
        return future
    assert isinstance(future, concurrent.futures.Future), \
        f'concurrent.futures.Future is expected, got {future!r}'
    if loop is None:
        loop = events.get_event_loop()
    # creates a future bound to 'loop' (passes it in to init)
    new_future = loop.create_future()
    # new_future (asyncio.Future) will have the result or exception
    # of concurrent.Future set on it, when the concurrent.Future will be done.
    _chain_future(future, new_future)
    return new_future


# Replace the pure python future implementation and also some helper functions
# with their C implementation (_asynciomodule.c)
try:
    import _asyncio
except ImportError:
    pass
else:
    # _CFuture is needed for tests.
    Future = _CFuture = _asyncio.Future
