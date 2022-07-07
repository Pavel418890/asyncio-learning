Selectors - abstract class
supports registering file object to be monitored for specific I/O events.
The default selector uses the most efficient implementation on the current
platform; kqueue |epoll | devpoll --> poll --> select.
 
* [select/pselect](#select)
* [poll](#poll)
* [epoll](#epoll)
* [kqueue](#kqueue)
* [devpoll](#devpoll)

<a id="select"><h1>select</h1></a>
```shell script
int select(int nfds, fd_set *readfds, fd_set *writefds,
          fd_set *exceptfds, struct timeval *timeout);

void FD_CLR(int fd, fd_set *set);    # remove from a set
int  FD_ISSET(int fd, fd_set *set);  # test to see if a fd is a part of set
void FD_SET(int fd, fd_set *set);    # add to set
void FD_ZERO(fd_set *set);           # clear a set
```
select() allow a program to monitor multiple file descriptors,
waiting until one or more of the file descriptors become "ready" for some class
of I/O operation (e.g., input possible).  A file descriptor is considered ready 
if it is possible to perform a corresponding I/O operation 
(e.g., read, or a sufficiently small write) without blocking.
select() can monitor only file descriptors numbers that are less than FD_SETSIZE

Three independent sets of file descriptors are watched.  

1. The file descriptors listed in readfds will be watched to see if characters
become available for reading (more precisely, to see if a read will not block;
in  particular, a file descriptor is also ready on end-of-file).

2. The file descriptors in writefds will be watched to see if space is available
for write (though a large write may still block).

3. The file descriptors in exceptfds will be watched for exceptional conditions.

On exit, each of the file descriptor sets is modified in place to indicate which
file descriptors actually changed status.
(Thus, if using select() within a loop, the sets must be reinitialized before each call.)

Each of the three file descriptor sets may be specified as NULL 
if no file descriptors are to be watched for the corresponding class of events.

The timeout argument specifies the interval that select() should block waiting
for a file descriptor to become ready. The call will block until either:
* a file descriptor becomes ready;
* the call is interrupted by a signal handler
* the timeout expires.

Note  that  the  timeout  interval  will be rounded up to the system clock
granularity, and kernel scheduling delays mean that the blocking interval 
may overrun by a small amount.  If both fields of the timeval structure are zero,
then select() returns immediately.  (This is useful for polling.)
If timeout is NULL (no timeout), select() can block indefinitely.


On success, select() return the number of file descriptors
contained in the three returned descriptor sets 
(that is, the total number of bits that are set in readfds, writefds, exceptfds)
which may be  zero  if the timeout expires before anything interesting happens.
On error, -1 is returned, and errno is set to indicate the error;
the file descriptor sets are unmodified, and timeout becomes undefined.


Pros:
1. Cross-platform 
2. Working with timeout in nanoseconds if platform support; poll and epoll 
work only with milliseconds.
3. Some code calls select() with all three sets empty, nfds zero,
and a non-NULL timeout as a fairly portable way to sleep with subsecond precision.

Cons:
1. If a file descriptor being monitored by select() is closed in another thread,
the result is unspecified.  On some UNIX systems, select() unblocks and returns,
with an indication that the file descriptor is ready (
a subsequent  I/O operation will likely fail with an error,
unless another process reopens file descriptor between the time select()
returned and the I/O operation is performed
).
On Linux (and some other systems), closing the file descriptor in another thread
has no effect on select().
 
2. The Linux kernel imposes no fixed limit, but the glibc implementation makes
fd_set a fixed-size type, with FD_SETSIZE defined as 1024, 
To monitor file descriptors greater than 1023, use poll(2) instead.

3. Always need to calculate the highest-numbered file descriptor in any
of the three sets, minus 1.

4. select()  should  check  all specified file descriptors in the three file
descriptor sets, up to the limit - O(n) operation.
However, the current implementation ignores any file
descriptor in these sets that is greater than the maximum file descriptor number
that the process currently has open.

5. If using select() within a loop, the sets must be reinitialized before each call.

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

The  field `fd` contains a file descriptor for an open file.  
If this field is negative, then the corresponding events field is ignored
and the `revents` field returns zero.  (This provides an easy way of  ignoring  a
file  descriptor  for a single poll() call: simply negate the fd field.

**Note**, however, that this technique can't be used to ignore file descriptor 0.)

The field `events` is an input parameter, a bit mask specifying the events
the application is  interested  in for the fd.  
This field may be specified as zero, in which case the only events that can be
returned in `revents` are ``POLLHUP``, ``POLLERR``, and ``POLLNVAL`` (see below).

The field `revents` is an output parameter, filled by the kernel with the events
that actually occurred. The bits  returned in `revents` can include any
of those specified in events, or one of the values `POLLERR`, `POLLHUP`, or `POLLNVAL`.

If  none  of  the events requested (and no error) has occurred for 
any of the file descriptors, then poll() blocks until one of the events occurs.

The `timeout` argument specifies the number of milliseconds that poll()
should block waiting for a fd to become ready.
The call will block until either:
*  a file descriptor becomes ready;
*  the call is interrupted by a signal handler; or
*  the timeout expires.

**Note**  that  the  timeout interval will be rounded up to the system 
clock granularity, and kernel scheduling delays mean that the blocking 
interval may overrun by a small amount.  Specifying a negative value in timeout
means  an infinite timeout. Specifying a timeout of zero causes poll()
to return immediately, even if no file descriptors are ready.

`POLLIN` 1 There is data to read.

`POLLOUT` 4 Writing is now possible, though a write larger that the available
space in a  socket  or  pipe  will still block (unless O_NONBLOCK is set).

`POLLRDHUP` 8192 Stream socket peer closed connection, or shut down writing half
of connection.

`POLLERR` 8 Error  condition (only returned in `revents`; ignored in `events`).
This bit is also set for a fd referring to the write end of a pipe when the read end has been closed.

`POLLHUP` 16 Hang up (only returned in `revents`; ignored in events). 
Note that when reading from a  channel  such as  a pipe or a stream socket,
this event merely indicates that the peer closed its end of the channel.
Subsequent reads from the channel will return 0 (end of file)
only after all outstanding  data in the channel has been consumed.

`POLLNVAL` 32 Invalid request: fd not open (only returned in `revents`; ignored in events).

`POLLRDNORM` 64 Equivalent to `POLLIN`.

`POLLRDBAND` 128 Priority band data can be read (generally unused on Linux).

`POLLWRNORM` 256 Equivalent to `POLLOUT`.

`POLLWRBAND` 512 Priority data may be written.

On success, a positive number is returned; this is the number of  structures  
which  have  nonzero  `revents` fields (in other words, those descriptors 
with events or errors reported).  A value of 0 indicates that the
call timed out and no file descriptors were ready.
On error, -1 is returned, and errno is set appropriately.

Pros:
1. There is no limit to monitor fds. 
2. Within the loop no needed reinitialized `pollfd` structure, just reset `revents` 
before call poll again. 
3. Better events structure. For example disconnect remote client without reading from socket.

Cons:
1. poll() should  check `revents` from all specified file descriptors - O(n) operation    
2. There is no way to dynamically change the observed set of events. 
