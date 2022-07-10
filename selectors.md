* [select/pselect](#select)
* [poll](#poll)
* [epoll](#epoll)
* [kqueue](#kqueue)
* [devpoll](#devpoll)
* [selectors.py](#selectors)

<a id="select"><h1>select</h1></a>

```shell script
int select(int nfds, fd_set *readfds, fd_set *writefds,
          fd_set *exceptfds, struct timeval *timeout);

void FD_CLR(int fd, fd_set *set);    # remove from a set
int  FD_ISSET(int fd, fd_set *set);  # test to see if a fd is a part of set
void FD_SET(int fd, fd_set *set);    # add to set
void FD_ZERO(fd_set *set);           # clear a set
```

select() allow a program to monitor multiple file descriptors, waiting until one
or more of the file descriptors become "ready" for some class of I/O operation (
e.g., input possible). A file descriptor is considered ready if it is possible
to perform a corresponding I/O operation
(e.g., read, or a sufficiently small write) without blocking. select() can
monitor only file descriptors numbers that are less than FD_SETSIZE

Three independent sets of file descriptors are watched.

1. The file descriptors listed in readfds will be watched to see if characters
   become available for reading (more precisely, to see if a read will not
   block; in particular, a file descriptor is also ready on end-of-file).

2. The file descriptors in writefds will be watched to see if space is available
   for write (though a large write may still block).

3. The file descriptors in exceptfds will be watched for exceptional conditions.

On exit, each of the file descriptor sets is modified in place to indicate which
file descriptors actually changed status.
(Thus, if using select() within a loop, the sets must be reinitialized before
each call.)

Each of the three file descriptor sets may be specified as NULL if no file
descriptors are to be watched for the corresponding class of events.

The timeout argument specifies the interval that select() should block waiting
for a file descriptor to become ready. The call will block until either:

* a file descriptor becomes ready;
* the call is interrupted by a signal handler
* the timeout expires.

Note that the timeout interval will be rounded up to the system clock
granularity, and kernel scheduling delays mean that the blocking interval may
overrun by a small amount. If both fields of the timeval structure are zero,
then select() returns immediately.  (This is useful for polling.)
If timeout is NULL (no timeout), select() can block indefinitely.

On success, select() return the number of file descriptors contained in the
three returned descriptor sets
(that is, the total number of bits that are set in readfds, writefds, exceptfds)
which may be zero if the timeout expires before anything interesting happens. On
error, -1 is returned, and errno is set to indicate the error; the file
descriptor sets are unmodified, and timeout becomes undefined.

Pros:

1. Cross-platform
2. Working with timeout in nanoseconds if platform support; poll and epoll work
   only with milliseconds.
3. Some code calls select() with all three sets empty, nfds zero, and a non-NULL
   timeout as a fairly portable way to sleep with subsecond precision.

Cons:

1. If a file descriptor being monitored by select() is closed in another thread,
   the result is unspecified. On some UNIX systems, select() unblocks and
   returns, with an indication that the file descriptor is ready (
   a subsequent I/O operation will likely fail with an error, unless another
   process reopens file descriptor between the time select()
   returned and the I/O operation is performed
   ). On Linux (and some other systems), closing the file descriptor in another
   thread has no effect on select().

2. The Linux kernel imposes no fixed limit, but the glibc implementation makes
   fd_set a fixed-size type, with FD_SETSIZE defined as 1024, To monitor file
   descriptors greater than 1023, use poll(2) instead.

3. Always need to calculate the highest-numbered file descriptor in any of the
   three sets, minus 1.

4. select()  should check all specified file descriptors in the three file
   descriptor sets, up to the limit - O(n) operation. However, the current
   implementation ignores any file descriptor in these sets that is greater than
   the maximum file descriptor number that the process currently has open.

5. If using select() within a loop, the sets must be reinitialized before each
   call.

<a id="poll"><h1>poll</h1></a>
The set of file descriptors to be monitored is specified in the fds argument,
which is an array of structures of the following form:

```shell script
int poll(struct pollfd *fds, nfds_t nfds, int timeout);

struct pollfd {
   int   fd;         /* file descriptor */
   short events;     /* requested events */
   short revents;    /* returned events */
};
```

The caller should specify the number of items in the fds array in nfds.

The field `fd` contains a file descriptor for an open file.  
If this field is negative, then the corresponding events field is ignored and
the `revents` field returns zero.  (This provides an easy way of ignoring a file
descriptor for a single poll() call: simply negate the fd field.

**Note**, however, that this technique can't be used to ignore file descriptor
0.)

The field `events` is an input parameter, a bit mask specifying the events the
application is interested in for the fd.  
This field may be specified as zero, in which case the only events that can be
returned in `revents` are ``POLLHUP``, ``POLLERR``, and ``POLLNVAL`` (see below)
.

The field `revents` is an output parameter, filled by the kernel with the events
that actually occurred. The bits returned in `revents` can include any of those
specified in events, or one of the values `POLLERR`, `POLLHUP`, or `POLLNVAL`.

If none of the events requested (and no error) has occurred for any of the file
descriptors, then poll() blocks until one of the events occurs.

The `timeout` argument specifies the number of milliseconds that poll()
should block waiting for a fd to become ready. The call will block until either:

* a file descriptor becomes ready;
* the call is interrupted by a signal handler; or
* the timeout expires.

**Note**  that the timeout interval will be rounded up to the system clock
granularity, and kernel scheduling delays mean that the blocking interval may
overrun by a small amount. Specifying a negative value in timeout means an
infinite timeout. Specifying a timeout of zero causes poll()
to return immediately, even if no file descriptors are ready.

`POLLIN` 1 There is data to read.

`POLLOUT` 4 Writing is now possible, though a write larger that the available
space in a socket or pipe will still block (unless O_NONBLOCK is set).

`POLLRDHUP` 8192 Stream socket peer closed connection, or shut down writing half
of connection.

`POLLERR` 8 Error condition (only returned in `revents`; ignored in `events`).
This bit is also set for a fd referring to the write end of a pipe when the read
end has been closed.

`POLLHUP` 16 Hang up (only returned in `revents`; ignored in events). Note that
when reading from a channel such as a pipe or a stream socket, this event merely
indicates that the peer closed its end of the channel. Subsequent reads from the
channel will return 0 (end of file)
only after all outstanding data in the channel has been consumed.

`POLLNVAL` 32 Invalid request: fd not open (only returned in `revents`; ignored
in events).

`POLLRDNORM` 64 Equivalent to `POLLIN`.

`POLLRDBAND` 128 Priority band data can be read (generally unused on Linux).

`POLLWRNORM` 256 Equivalent to `POLLOUT`.

`POLLWRBAND` 512 Priority data may be written.

On success, a positive number is returned; this is the number of structures  
which have nonzero  `revents` fields (in other words, those descriptors with
events or errors reported). A value of 0 indicates that the call timed out and
no file descriptors were ready. On error, -1 is returned, and errno is set
appropriately.

Pros:

1. There is no limit to monitor fds.
2. Within the loop no needed reinitialized `pollfd` structure, just
   reset `revents`
   before call poll again.
3. Better events structure. For example disconnect remote client without reading
   from socket.

Cons:

1. poll() should check `revents` from all specified file descriptors - O(n)
   operation
2. There is no way to dynamically change the observed set of events.

<a id="epoll"><h1>epoll</h1>
The epoll API performs a similar task to poll(2): monitoring multiple file
descriptors to see if I/O is possible on any of them. The epoll API can be used
either as an edge-triggered or a level-triggered interface and scales well to
large numbers of watched file descriptors.

The following system calls are provided to create and manage an epoll instance:

* epoll_create(2) creates a new epoll instance and returns a file descriptor
  referring to that instance.  (The more recent epoll_create1(2)  extends the
  functionality of epoll_create(2).)

* epoll_ctl(2) adds items to the interest list of the epoll instance.


* epoll_wait(2)  waits for I/O events, blocking the calling thread if no events
  are currently available.  (This system call can be thought of as fetching
  items from the ready list of the epoll instance.)

The central concept of the epoll API is the epoll instance, an in-kernel data
structure which, from a user-space perspective, can be considered as a container
for two lists:

* The interest list - the set of file descriptors that the process has
  registered an interest in monitoring.

* The ready list: the set of file descriptors that are "ready" for I/O. The
  ready list is a subset of (or, more precisely, a set of references to) the
  file descriptors in the interest list. The ready list is dynamically populated
  by the kernel as a result of I/O activity on those file descriptors.

Pros:

1. Linear search from all fds is no more needed. epoll() return list of fds by
   the kernel, where events is happen.
2. Can associate some context that will be return with fd(callback or other)
3. Allow from user space add/remove sockets in any time and from another thread
   and even modify tracked events.
4. Allow track events from a particular queue events from another thread

Cons:

1. Used more syscall(epoll_ctl, epoll_wait) then poll/select therefore more
   context switching. In a scenario that 5000 connections required change event
   from read to write 5000 syscall will be called
   (poll/select used simple bitwise operation for that).

2. Provided in Linux OS only

<a id="selectors"><h1>selector.py</h1></a>

Selectors - abstract class supports registering file object to be monitored for
specific I/O events.

Common objects and methods:

1. SelectorKey - named tuple that stored

* fileobj - file object(socket/pipe/fifo) itself,
* fd - file descriptor associated with that file
* events - read/write or both events that must be waited on that file
* data - some payload or callback which will be returned when events happened

2. `_fileobj_to_fd` - function received file object or fd. On file object try
   call .fileno() method if is not ValueError will be raised. As a result fd
   will be returned, fd must be greater then 0 or ValueError will be raised. OS
   not provided negative fd numbers.

3. SelectorMapping - read-only key-value storage class implementing iterable
   protocol.

   3.1. `__init__` - define any BaseSelector object as class attribute.

   3.2. `__iter__` - allow to iterate over all fds stored in that selector
   object.

   3.3. `__len__` - return length of fds dict storage.

   3.4. `__getitem__` - in a simple scenario uses `_file_to_fd` function to
   return fd. Do an exhaustive search in case if object is invalid but still
   in `_fo_to_key`. Iterate over `_fd_to_key` dict values, check that received
   file object is a `Selector.fileobj`, get the `SelectorKey.fd` and store them
   , while dict will be exhaust and ValueError will be raised. In case if fd is
   found get it from `_fd_to_key` dict or KeyError will be raised.


4. BaseSelector - base implementation selector class that managing state of a
   selector, implement context manager protocol.

   4.1. `__init__` - define `_fd_to_key` dict {"fd": SelectorKey} self storage
   `_map` - SelectorMapping described earlier.

   4.2. `_fileobj_lookup` - common method that return a fd from a file object.
   in a simple scenario uses `_file_to_fd` function to return fd. Do an
   exhaustive search in case if object is invalid but still in `_fo_to_key`.
   Iterate over `_fd_to_key` dict values, check that received file object is
   a `Selector.fileobj`, get the `SelectorKey.fd` and return it is, while dict
   will be exhaust and ValueError will be raised. This method used by
   the `SelectorMapping.__getitem__` and `register`/`unregister`/`modify`
   methods decribed bellow.

   4.3. `get_map` - public method returns self.SelectorMapping.

   4.4. `get_key` - public method returns `SelectorKey` associated with
   registered file object by the `SelectorMapping.__getitem__` mehtod described
   earlier.

   4.5. `_key_from_fd` private method returns `SelectorKey` from fds storage,
   without usage `SelectorMapping` for performance reason (exhaustive search
   used).

   4.6. `register` -

   Args:

   `fileobj`: socket stream|pipe|fifo,

   `events`: bitwise mask(1-read,2-write, 3-read|write),

   `data`: user context

   Perform _bitwise operation*_ that exclude all type of events != 1|2|3. Create
   new `SelectorKey` by the file object and data from args, checked events in
   previous step and fd get from file object .fileno() attribute or exhaustive
   search will be performed in case if invalid file object is received, but
   still in fds storage, anyway fd search in fds storage will be performed and
   KeyError raised if it already there, otherwise add it to fds storage and
   return new `SelectorKey`.

   4.7. `unregister` - return poped file object from fds storage first trying
   get file object.fileno() otherwise use exhaustive search. In case if not
   there KeyError will be raised.

   4.8. `modify` - get the `SelectorKey` from fds storage first trying get file
   object.fileno() otherwise use exhaustive search and after that same operation
   as unregister and register will be called, but lazy. Meaning if updates only
   data(user context) then used private named tuple method `_replace` and
   reset `SelectorKey` in fds storage without usage `unregister` and `register`
   methods at all.
   
   4.9. `close` - clear fds storage and reset `SelectorMapping` as None.
   
   4.10. `__enter__` - return selector itself.
   
   4.11. `__exit__` - clear fds storage and reset `SelectorMapping` as None.

The default selector uses the most efficient implementation on the current
platform; kqueue |epoll | devpoll --> poll --> select by the `_can_use`
method that use `select` C implementation actually.

```shell script
*bitwise operation
   
        ~(1 | 2) = -4  
        -4 - 2's complemented is 1100
         -4 & <events>
         1100 1100 1100 1100 1100 1100 1100 1100 1100 1100 1100 1100
         0001 0010 0011 0100 0101 0110 0111 1000 1001 1010 1011 1100
         ------------------------------------------------------------
         0000 0000 0000 0100 0100 0100 0100 1000 1000 1000 1000 1100...and so on

    base10  0   0    0   4    4    4    4   8    8    8    8    12
```