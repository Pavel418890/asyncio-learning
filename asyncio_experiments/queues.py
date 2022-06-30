import collections
import heapq

from . import locks
from . import mixins


class QueueEmpty(Exception):
    """Raised when Queue.get_nowait() is called on an empty Queue."""
    pass


class QueueFull(Exception):
    """Raised when the Queue.put_nowait() method is called on a full Queue."""
    pass


class Queue(mixins._LoopBoundMixin):
    """A queue, useful for coordinating producer and consumer coroutines.

    If maxsize is less than or equal to zero, the queue size is infinite. If it
    is an integer greater than 0, then "await put()" will block when the
    queue reaches maxsize, until an item is removed by get().

    Unlike the standard library Queue, you can reliably know this Queue's size
    with qsize(), since your single-threaded asyncio application won't be
    interrupted between calling qsize() and doing an operation on the Queue.

    Details:

    1) There are 2 deques - for getter and putter futures

    2) To block calls to queue.join() a locks.Event() is used
       which is initially set, so that calls to queue.join() will not block
       before there are any items in the queue
       (before any put operations have been performed)

       Every put operation clears the locks.Event()
       (this is an idempotent operation -
       just sets the internal ._value flag to False)
       and increments the unfinished_tasks counter.
       After that calls to queue.join() will block on await event.wait().

       .task_done call decrements the unfinished_tasks counter and if it reaches
       0 sets the locks.Event() effectively unblocking the call to queue.join()

    3) put and get methods block if the the queue is full and empty respectively.
       This is done via creating a putter/getter future and adding it to a
       deque of waiter futures.

       Put and get methods block on 'await getter/putter' and will unblock
       when the opposite party calls get/put which under the covers will
       take the first waiter future in line and set it's result, so that
       during loop's next iteration coro.send(True) will unblock the call
       to 'await getter/putter'. After that the fullness/emptiness of the loop
       is checked once more, if not full/empty the item is put/taken and the
       call to put/get exits. Else the process is repeated all over again.
    """

    def __init__(self, maxsize=0, *, loop=mixins._marker):
        super().__init__(loop=loop)
        self._maxsize = maxsize

        # Futures.
        self._getters = collections.deque()
        # Futures.
        self._putters = collections.deque()

        self._unfinished_tasks = 0
        self._finished = locks.Event()
        # no one waiting on Event at this point
        # will be initially signalled
        # so if no 'puts' were done before calling queue.join()
        # queue.join() will not block
        self._finished.set()

        self._init(maxsize)

    # These three are overridable in subclasses.

    def _init(self, maxsize):
        self._queue = collections.deque()

    def _get(self):
        return self._queue.popleft()

    def _put(self, item):
        self._queue.append(item)

    # End of the overridable methods.

    def _wakeup_next(self, waiters):
        # Wake up the next waiter (if any) that isn't cancelled.
        while waiters:
            waiter = waiters.popleft()
            if not waiter.done():
                waiter.set_result(None)
                break

    def __repr__(self):
        return f'<{type(self).__name__} at {id(self):#x} {self._format()}>'

    def __str__(self):
        return f'<{type(self).__name__} {self._format()}>'

    def __class_getitem__(cls, type):
        return cls

    def _format(self):
        result = f'maxsize={self._maxsize!r}'
        if getattr(self, '_queue', None):
            result += f' _queue={list(self._queue)!r}'
        if self._getters:
            result += f' _getters[{len(self._getters)}]'
        if self._putters:
            result += f' _putters[{len(self._putters)}]'
        if self._unfinished_tasks:
            result += f' tasks={self._unfinished_tasks}'
        return result

    def qsize(self):
        """Number of items in the queue."""
        return len(self._queue)

    @property
    def maxsize(self):
        """Number of items allowed in the queue."""
        return self._maxsize

    def empty(self):
        """Return True if the queue is empty, False otherwise."""
        return not self._queue

    def full(self):
        """Return True if there are maxsize items in the queue.

        Note: if the Queue was initialized with maxsize=0 (the default),
        then full() is never True.
        """
        if self._maxsize <= 0:
            return False
        else:
            return self.qsize() >= self._maxsize

    async def put(self, item):
        """Put an item into the queue.

        Put an item into the queue. If the queue is full, wait until a free
        slot is available before adding item.
        """
        # in a while loop, because even when coro.send is scheduled
        # to awake 'await putter', there might have been some subsequent
        # calls to put/put_nowait in other coroutines before coro.send
        # is actually called (will be called with a delay, during loop's
        # next iteration after self._wakeup_next(self._putters) was called)
        while self.full():
            putter = self._get_loop().create_future()
            self._putters.append(putter)
            try:
                # self.get_nowait will unblock this via calling
                # self._wakeup_next(self._putters) which will set result
                # on the first waiter future in self._putters
                await putter
            except:
                putter.cancel()  # Just in case putter is not done yet.
                try:
                    # Clean self._putters from canceled putters.
                    self._putters.remove(putter)
                except ValueError:
                    # The putter could be removed from self._putters by a
                    # previous get_nowait call.
                    # (via self._wakeup_next(self._putters))
                    pass
                if not self.full() and not putter.cancelled():
                    # We were woken up by get_nowait(), but can't take
                    # the call.  Wake up the next in line.
                    self._wakeup_next(self._putters)
                raise

        return self.put_nowait(item)

    def put_nowait(self, item):
        """Put an item into the queue without blocking.

        If no free slot is immediately available, raise QueueFull.
        """
        if self.full():
            raise QueueFull
        self._put(item)
        # because another task/item was added,
        # this means there's one more unfinished task to be done
        # call .task_done() to decrement the counter
        self._unfinished_tasks += 1
        # this will make calls to queue.join() block until all tasks are done
        # must do clear() because the event was initially set
        # (clear is idempotent - just sets the internal ._value flag to False)
        self._finished.clear()
        self._wakeup_next(self._getters)

    async def get(self):
        """Remove and return an item from the queue.

        If queue is empty, wait until an item is available.
        """
        while self.empty():
            getter = self._get_loop().create_future()
            self._getters.append(getter)
            try:
                # self.put_nowait will unblock this via calling
                # self._wakeup_next(self._getters) which will set result
                # on the first waiter future in self._getters
                await getter
            except:
                getter.cancel()  # Just in case getter is not done yet.
                try:
                    # Clean self._getters from canceled getters.
                    self._getters.remove(getter)
                except ValueError:
                    # The getter could be removed from self._getters by a
                    # previous put_nowait call.
                    pass
                if not self.empty() and not getter.cancelled():
                    # We were woken up by put_nowait(), but can't take
                    # the call.  Wake up the next in line.
                    self._wakeup_next(self._getters)
                raise
        return self.get_nowait()

    def get_nowait(self):
        """Remove and return an item from the queue.

        Return an item if one is immediately available, else raise QueueEmpty.
        """
        if self.empty():
            raise QueueEmpty
        item = self._get()
        # if some coro is blocked inside a .put call on 'await putter'
        # take the first putter from putters and schedule it's Task's
        # .__wakeup method => .__step to do coro.send(None) unblocking
        # the 'await putter' statement
        self._wakeup_next(self._putters)
        return item

    def task_done(self):
        """Indicate that a formerly enqueued task is complete.

        Used by queue consumers. For each get() used to fetch a task,
        a subsequent call to task_done() tells the queue that the processing
        on the task is complete.

        If a join() is currently blocking, it will resume when all items have
        been processed (meaning that a task_done() call was received for every
        item that had been put() into the queue).

        Raises ValueError if called more times than there were items placed in
        the queue.
        """
        if self._unfinished_tasks <= 0:
            raise ValueError('task_done() called too many times')
        self._unfinished_tasks -= 1
        if self._unfinished_tasks == 0:
            # when counter goes to zero set() unblocks call to
            # await self._finished.wait() in .join()
            self._finished.set()

    async def join(self):
        """Block until all items in the queue have been gotten and processed.

        The count of unfinished tasks goes up whenever an item is added to the
        queue. The count goes down whenever a consumer calls task_done() to
        indicate that the item was retrieved and all work on it is complete.
        When the count of unfinished tasks drops to zero, join() unblocks.
        """
        if self._unfinished_tasks > 0:
            await self._finished.wait()


class PriorityQueue(Queue):
    """A subclass of Queue; retrieves entries in priority order (lowest first).

    Entries are typically tuples of the form: (priority number, data).
    """

    def _init(self, maxsize):
        self._queue = []

    def _put(self, item, heappush=heapq.heappush):
        heappush(self._queue, item)

    def _get(self, heappop=heapq.heappop):
        return heappop(self._queue)


class LifoQueue(Queue):
    """A subclass of Queue that retrieves most recently added entries first."""

    def _init(self, maxsize):
        self._queue = []

    def _put(self, item):
        self._queue.append(item)

    def _get(self):
        return self._queue.pop()