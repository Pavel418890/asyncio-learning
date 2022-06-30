__all__ = ()

import reprlib
from _thread import get_ident

from . import format_helpers

# States for Future.
# Currently are represented as an enum in C code
_PENDING = 'PENDING'
_CANCELLED = 'CANCELLED'
_FINISHED = 'FINISHED'


def isfuture(obj):
    """Check for a Future.

    This returns True when obj is a Future instance or is advertising
    itself as duck-type compatible by setting _asyncio_future_blocking.
    See comment in Future for more details.

    As a getter and setter underlying _asynciomodule C code is used.

    FutureObj_get_blocking -  returns True if the future is alive and
    _asyncio_future_blocking is True, else returns False.

    FutureObj_set_blocking - ensures on C level that the _asyncio_future_blocking
    attribute cannot be deleted, also ensures that the future is initialized
    before returning the value of _asyncio_future_blocking.
    """
    return (hasattr(obj.__class__, '_asyncio_future_blocking') and
            obj._asyncio_future_blocking is not None)


def _format_callbacks(cb):
    """helper function for Future.__repr__"""
    size = len(cb)
    if not size:
        cb = ''

    def format_cb(callback):
        return format_helpers._format_callback_source(callback, ())

    if size == 1:
        cb = format_cb(cb[0][0])
    elif size == 2:
        cb = '{}, {}'.format(format_cb(cb[0][0]), format_cb(cb[1][0]))
    elif size > 2:
        cb = '{}, <{} more>, {}'.format(format_cb(cb[0][0]),
                                        size - 2,
                                        format_cb(cb[-1][0]))
    return f'cb=[{cb}]'


# bpo-42183: _repr_running is needed for repr protection
# when a Future or Task result contains itself directly or indirectly.
# The logic is borrowed from @reprlib.recursive_repr decorator.
# Unfortunately, the direct decorator usage is impossible because of
# AttributeError: '_asyncio.Task' object has no attribute '__module__' error.
#
# After fixing this thing we can return to the decorator based approach.
_repr_running = set()


# is not implemented in C, but also called from C
def _future_repr_info(future):
    # (Future) -> str
    """helper function for Future.__repr__"""
    info = [future._state.lower()]
    if future._state == _FINISHED:
        if future._exception is not None:
            info.append(f'exception={future._exception!r}')
        else:
            key = id(future), get_ident()  # current thread id
            if key in _repr_running:
                result = '...'
            else:
                _repr_running.add(key)
                try:
                    # use reprlib to limit the length of the output, especially
                    # for very long strings
                    result = reprlib.repr(future._result)
                finally:
                    _repr_running.discard(key)
            info.append(f'result={result}')
    if future._callbacks:
        info.append(_format_callbacks(future._callbacks))
    if future._source_traceback:
        frame = future._source_traceback[-1]
        info.append(f'created at {frame[0]}:{frame[1]}')
    return info
