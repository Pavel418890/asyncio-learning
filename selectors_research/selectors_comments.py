"""Selectors module.

This module allows high-level and efficient I/O multiplexing, built upon the
`select` module primitives.

About select/poll/epoll:
https://habr.com/ru/company/infopulse/blog/415259/
"""

from abc import ABCMeta, abstractmethod
from collections import namedtuple
from collections.abc import Mapping
import math
import select
import sys

# generic events, that must be mapped to implementation-specific ones
# (basically used directly only by the select.select call, other calls
# (poll, epoll, etc) use there own bit masks.
# BUT in the end only these bit masks will be exposed to the end user.
EVENT_READ = (1 << 0)  # 1
EVENT_WRITE = (1 << 1)  # 2


def _fileobj_to_fd(fileobj):
    """Return a file descriptor from a file object.

    Parameters:
    fileobj -- file object or file descriptor

    Returns:
    corresponding file descriptor

    Raises:
    ValueError if the object is invalid
    """
    # if already an fd
    if isinstance(fileobj, int):
        fd = fileobj
    # if a file like object, try calling .fileno()
    else:
        try:
            fd = int(fileobj.fileno())
        except (AttributeError, TypeError, ValueError):
            raise ValueError("Invalid file object: "
                             "{!r}".format(fileobj)) from None
    if fd < 0:
        raise ValueError("Invalid file descriptor: {}".format(fd))
    return fd


# 1) fileobj - original file object (could be already an fd as well)
# 2) fd - fd derived from the file object
# 3) events - event mask
#   (EVENT_READ (1); EVENT_WRITE (2); EVENT_READ|EVENT_WRITE (3))
# 4) data - arbitrary user data which will be returned with the key, if the
#    'polling' succeeds.
SelectorKey = namedtuple('SelectorKey', ['fileobj', 'fd', 'events', 'data'])

SelectorKey.__doc__ = """SelectorKey(fileobj, fd, events, data)

    Object used to associate a file object to its backing
    file descriptor, selected event mask, and attached data.
"""


class _SelectorMapping(Mapping):
    """Mapping of file objects to selector keys."""

    def __init__(self, selector: '_BaseSelectorImpl'):
        self._selector = selector

    def __len__(self):
        # returns the length of the fd to SelectorKey dict
        return len(self._selector._fd_to_key)

    def __getitem__(self, fileobj):
        try:
            # This method of the _BaseSelectorImpl class converts a file object
            # to an fd by:
            #   1) first trying to call .fileno() on it
            #   2) if this doesn't succeed, by iterating the SelectorKeys
            #   (values of the ._fd_to_key dict) and comparing every
            #   selector_key.fileobj to the passed in fileobj, if it succeeds
            #   it returns the selector_key's .fd attribute
            fd = self._selector._fileobj_lookup(fileobj)
            # return the SelectorKey which contains the passed in fileobj
            # in its .fileobj attr
            return self._selector._fd_to_key[fd]
        except KeyError:
            raise KeyError("{!r} is not registered".format(fileobj)) from None

    def __iter__(self):
        # returns an iterator over the fds in the map
        return iter(self._selector._fd_to_key)


class BaseSelector(metaclass=ABCMeta):
    """Selector abstract base class.

    A selector supports registering file objects to be monitored for specific
    I/O events.

    A file object is a file descriptor or any object with a `fileno()` method.
    An arbitrary object can be attached to the file object, which can be used
    for example to store context information, a callback, etc.

    A selector can use various implementations (select(), poll(), epoll()...)
    depending on the platform. The default `Selector` class uses the most
    efficient implementation on the current platform.
    """

    @abstractmethod
    def register(self, fileobj, events, data=None):
        """Register a file object.

        Parameters:
        fileobj -- file object or file descriptor
        events  -- events to monitor (bitwise mask of EVENT_READ|EVENT_WRITE)
        data    -- attached data

        Returns:
        SelectorKey instance

        Raises:
        ValueError if events is invalid
        KeyError if fileobj is already registered
        OSError if fileobj is closed or otherwise is unacceptable to
                the underlying system call (if a system call is made)

        Note:
        OSError may or may not be raised
        """
        raise NotImplementedError

    @abstractmethod
    def unregister(self, fileobj):
        """Unregister a file object.

        Parameters:
        fileobj -- file object or file descriptor

        Returns:
        SelectorKey instance

        Raises:
        KeyError if fileobj is not registered

        Note:
        If fileobj is registered but has since been closed this does
        *not* raise OSError (even if the wrapped syscall does)
        """
        raise NotImplementedError

    def modify(self, fileobj, events, data=None):
        """Change a registered file object monitored events or attached data.

        Parameters:
        fileobj -- file object or file descriptor
        events  -- events to monitor (bitwise mask of EVENT_READ|EVENT_WRITE)
        data    -- attached data

        Returns:
        SelectorKey instance

        Raises:
        Anything that unregister() or register() raises
        """
        self.unregister(fileobj)
        return self.register(fileobj, events, data)

    @abstractmethod
    def select(self, timeout=None):
        """Perform the actual selection, until some monitored file objects are
        ready or a timeout expires.

        Parameters:
        timeout -- if timeout > 0, this specifies the maximum wait time, in
                   seconds
                   if timeout <= 0, the select() call won't block, and will
                   report the currently ready file objects
                   if timeout is None, select() will block until a monitored
                   file object becomes ready

        Returns:
        list of (key, events) for ready file objects
        `events` is a bitwise mask of EVENT_READ|EVENT_WRITE
        Basically tells what event/events occurred on that key.
        """
        raise NotImplementedError

    def close(self):
        """Close the selector.

        This must be called to make sure that any underlying resource is freed.
        """
        pass

    def get_key(self, fileobj):
        """Return the key associated to a registered file object.

        Returns:
        SelectorKey for this file object
        """
        mapping = self.get_map()
        if mapping is None:
            raise RuntimeError('Selector is closed')
        try:
            return mapping[fileobj]
        except KeyError:
            raise KeyError("{!r} is not registered".format(fileobj)) from None

    @abstractmethod
    def get_map(self):
        """
        Return a mapping of file objects to selector keys.
        Usually an instance of the _SelectorMapping class defined above
        """
        raise NotImplementedError

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class _BaseSelectorImpl(BaseSelector):
    """Base selector implementation."""

    def __init__(self):
        # this maps file descriptors to keys
        self._fd_to_key = {}
        # read-only mapping returned by get_map()
        # maps registered file objects to their corresponding keys
        self._map = _SelectorMapping(self)

    def _fileobj_lookup(self, fileobj):
        """Return a file descriptor from a file object.

        This wraps _fileobj_to_fd() to do an exhaustive search in case
        the object is invalid but we still have it in our map.  This
        is used by unregister() so we can unregister an object that
        was previously registered even if it is closed.  It is also
        used by _SelectorMapping.
        """
        try:
            return _fileobj_to_fd(fileobj)
        except ValueError:
            # Do an exhaustive search.
            for key in self._fd_to_key.values():
                if key.fileobj is fileobj:
                    return key.fd
            # Raise ValueError after all.
            raise

    def register(self, fileobj, events, data=None):
        """
        Details:

        1) Validates that the events passed in are not None, and either
           EVENT_READ, EVENT_WRITE or EVENT_READ|EVENT_WRITE

        2) Creates a namedtuple SelectorKey consisting of the original file
           object, its underlying fd, events bitmask, arbitrary user data

        3) Validates that the file object passed in wasn't already registered

        4) Registers the resulting SelectorKey by associating it with the
          file object's underlying fd within the self._fd_to_key dict.
        """

        # 1) ~(EVENT_READ | EVENT_WRITE) is -4
        # 2) -4 & 3/1/2 :only those combinations give a result of 0 (False),
        #    meaning that if an invalid event mask is passed in
        #    it will result in a non-zero value thus True.
        if (not events) or (events & ~(EVENT_READ | EVENT_WRITE)):
            raise ValueError("Invalid events: {!r}".format(events))

        # Named Tuple consisting of:
        # 1) original file object
        # 2) the underlying fd of the file object
        # 3) event mask
        # 4) arbitrary user data (for example callbacks)
        key = SelectorKey(fileobj, self._fileobj_lookup(fileobj), events, data)

        if key.fd in self._fd_to_key:
            raise KeyError("{!r} (FD {}) is already registered"
                           .format(fileobj, key.fd))

        # map the file object's underlying fd to the newly created SelectorKey
        self._fd_to_key[key.fd] = key
        return key

    def unregister(self, fileobj):
        """
        Just pops and returns the SelectorKey from the fd to SelectorKey dict
        and returns the SelectorKey
        """
        try:
            # pop the SelectorKey by converting the passed in fileobj to
            # its underlying fd
            key = self._fd_to_key.pop(self._fileobj_lookup(fileobj))
        except KeyError:
            raise KeyError("{!r} is not registered".format(fileobj)) from None
        return key

    def modify(self, fileobj, events, data=None):
        """
        Details:

        1) If the file object wasn't registered, raises a KeyError
        2) If the events bitmask is different from the old one, deletes the
           old SelectorKey from the dict and just creates and registers
           a new SelectorKey with the passed in fileobj, events and data.
        3) If only the data is different, replaces the data in the
           already registered SelectorKey (this returns a new NamedTuple)
           and replaces the old selector key with the new one that contains
           the new passed in data.
        """
        try:
            # check that the fileobj is registered
            # and also keep a reference to the 'old' SelectorKey
            key = self._fd_to_key[self._fileobj_lookup(fileobj)]
        except KeyError:
            raise KeyError("{!r} is not registered".format(fileobj)) from None

        # if the passed in event mask is not equal to the event mask
        # registered before with the SelectorKey,
        # we need to update the event mask of the already registered
        # SelectorKey
        # Basically if a new event mask is passed in we just register
        # a new SelectorKey and delete the old one
        if events != key.events:
            # first remove the registered SelectorKey from the self._fd_to_key
            # dict
            self.unregister(fileobj)
            # just register a new SelectorKey with the passed in data
            key = self.register(fileobj, events, data)
        # if only the data needs to be modified
        elif data != key.data:
            # Use a shortcut to update the data.
            # this returns a new SelectorKey (namedtuple) with the updated data
            key = key._replace(data=data)
            # swap out the old SelectorKey for the modified new one
            self._fd_to_key[key.fd] = key

        return key

    def close(self):
        """
        Clears the fd to SelectorKey dict together with the
        file object to SelectorKey mapping
        """
        self._fd_to_key.clear()
        self._map = None

    def get_map(self):
        """
        Returns the file object to SelectorKey mapping
        """
        return self._map

    def _key_from_fd(self, fd):
        """Return the key associated to a given file descriptor.

        Parameters:
        fd -- file descriptor

        Returns:
        corresponding key, or None if not found

        Basically just does a dict lookup by fd, if not found
        returns None
        """
        try:
            return self._fd_to_key[fd]
        except KeyError:
            return None


class SelectSelector(_BaseSelectorImpl):
    """
    Select-based selector.

    Select performs poorly on systems that are multi-threaded and
    must serve thousands of connections.

    select() builds a bitmap, turns on bits for the fds of interest,
    and then afterward the whole bitmap has to be linearly scanned again.

    select() is O(highest file descriptor) - so even if we only pass in fds
    number 1 and number 234, 234 bits must be scanned linearly.

    I) In a nutshell:

    Cons:
       1) modifies the passed in reader/writer sets, must be reinitialized
          every time
       2) have to iterate over the whole fd set to identify which
          fds generated an event
       3) simultaneous supports only 1024 fds at max
       4) user cannot work with fds tracked by `select` from other threads
         (close, write operations and etc.)
       5) supports only a small range of events (read, write, exceptions)

    Pros:
       1) Is supported by nearly every system out there
       2) supports a nanosecond precision timeout (if the machine support it)

    II) Detailed:

    Cons:

        1) select() modifies the passed in reader/writer sets so that after the
           call they cannot be reused, even if you still need to
           (the sets have to be initialized from scratch every time you perform
           the select() call)

        2) to identify the fds that are `ready/generated an event` you have to
           iterate over each set up till the end
           (checking if the fd generated and event by calling
           FD_ISSET on the fd). It could be so that only the last
           fd generated and event - what a waist of resources and time.

        3) select() only supports simultaneous tracking of 1024 fds at max

        4) select() doesn't allow the user to work with the tracked fds from
           other threads. As the documentation says, if an fd tracked by select
           is closed from another thread, this can lead to undefined behaviour.

        5) select() only supports a small range of events for tracking
           (read, write, errors). So if you need to identify that a socket
           was closed, you must first track read events from it and then try
           to read data, if the read operation returns 0,
           this means the socket was closed.

        6) with select() you always have to calculate the largest fd number
           in the tracked sets, you have to pass this number to
           the select() call.

    Pros:

        1) Portable - very old, nearly every system supports it.
           For example Windows XP doesn't support `poll` but supports `select`.

        2) `select` can work with nanosecond precision timeouts
           (if the machine supports it), on the other hand
           `poll` and `epoll` only support millisecond precision timeouts.
           This isn't relevant for desktops and normal servers,
            but some realtime systems may require it.
    """

    def __init__(self):
        super().__init__()
        # contains fds registered for read events
        # select.select will be called with this set
        self._readers = set()
        # contains fds registered for write events
        # select.select will be called with this set
        self._writers = set()

    def register(self, fileobj, events, data=None):
        """
        Details:

        1) Creates a SelectorKey for the fileobj and associates it with it's
           underlying fd within the self._fd_to_key dict

        2) Adds the underlying fd to reader/writer sets depending on the events
           bitmask passed in, these sets will be later used during
           the select.select call
        """
        # 1) creates a SelectorKey namedtuple consisting of:
        #   original file object, its fd, events mask, user data
        # 2) Associates the resulting SelectorKey with the file object's fd
        #    within the self._fd_to_key dict
        key = super().register(fileobj, events, data)

        # if the events bitmask passed in includes a 'read event'
        # add the file object's fd to a set of readers (select.select
        # will be called on this set together with the writers set)
        if events & EVENT_READ:
            self._readers.add(key.fd)

        # if the events bitmask passed in includes a 'write event'
        # add the file object's fd to a set of writers (select.select
        # will be called on this set together with the readers set)
        if events & EVENT_WRITE:
            self._writers.add(key.fd)

    def unregister(self, fileobj):
        """
        Details:

        1) Removes the SelectorKey associated with the fileobj from the
           self._fd_to_key dict

        2) Removes the underlying file object's fd from reader/writer sets
           so that they will no longer be included in select.select calls
        """
        # removes the associated SelectorKey from the self._fd_to_key dict
        key = super().unregister(fileobj)
        # removes the file object's underlying fd from the
        # readers set so it will no longer be included while polling
        # via select.select
        self._readers.discard(key.fd)
        # removes the file object's underlying fd from the
        # writers set so it will no longer be included while polling
        # via select.select
        self._writers.discard(key.fd)
        return key

    # TODO sometime later:
    #  Don't know why the 'xlist' sockets are the same sockets as the 'write'
    #  sockets on Windows - may be empty iterables  are not allowed on Windows
    #  to be passed in as the 'xlist'.
    #  Although the Windows API state differently:
    #     https://docs.microsoft.com/en-us/windows/win32/api/winsock2/nf-winsock2-select
    if sys.platform == 'win32':
        def _select(self, r, w, _, timeout=None):
            r, w, x = select.select(r, w, w, timeout)
            return r, w + x, []
    else:
        _select = select.select

    def select(self, timeout=None):
        """
        Calls select on the set of registered reader/writer fds/sockets, which
        will return list of ready fds/sockets.

        Returns a list of tuples containing the corresponding SelectorKeys
        and event masks.
        The event mask in the tuple will ensure that even if an event occurred
        on the user's file object, but it wasn't requested by the user,
        it will not be included in the event mask.

        Details:

        1) Polls for I/O events of the registered reader/writer fds

        2) Combines the fds ready for reading and writing into a single
           set so that if a fd happened to be simultaneously ready for reading
           and writing an events bitmask EVENT_READ|EVENT_WRITE can be constructed

        3) Returns a list of tuple consisting of SelectorKeys and event masks
          (the part of the event mask that wasn't requested by the user
          for a particular file object will be excluded from the resulting
          event mask via events & key.events)
          For example:
            If the user registered a file object only for read events,
            but some write events occurred on the underlying fd, the event
            mask returned by this method will be 0, because
            EVENT_READ (1) & EVENT_WRITE (2) will be equal to 0.
        """

        # ensure that timeout is 0, a positive value or None
        timeout = None if timeout is None else max(timeout, 0)
        ready = []
        try:
            # the actual polling of fds (only sockets on windows)
            r, w, _ = self._select(self._readers, self._writers, [], timeout)
        except InterruptedError:
            # if a signal interrupt occurred
            # just return and an empty list
            return ready

        # the whole manipulation of combining
        # readers and writers into a single set
        # is done so that if an fd/socket is present in readers
        # and writers we can calculate the appropriate events mask
        # (EVENT_READ|VENT_WRITE)
        r = set(r)
        w = set(w)
        # combined set of fds/sockets ready for reading and writing
        # without duplicates
        for fd in r | w:
            events = 0
            # if the fd is in the readers set
            # add the read mask to the resulting events bitmask
            if fd in r:
                events |= EVENT_READ
            # if the fd is also in the writers set
            # add the write mask to the resulting events bitmask
            if fd in w:
                events |= EVENT_WRITE

            # get the corresponding SelectorKey namedtuple for
            # the fd on which some event occurred
            key = self._key_from_fd(fd)

            # if the fd was registered,
            # add to results list a tuple:
            #  1) SelectorKey corresponding to the fd/socket
            #  2) Mask - excludes the event mask that wasn't requested by
            #     the user and leaves only the mask that the user requested
            #     if such an event occurred during the select call:
            #         events & key.events - this will ensure that for example
            #         even if a write event occurred on the fd, but it
            #         was only registered for read events, only the EVENT_READ
            #         mask will be returned to the user
            if key:
                ready.append((key, events & key.events))

        return ready


class _PollLikeSelector(_BaseSelectorImpl):
    """Base class shared between poll, epoll and devpoll selectors."""
    _selector_cls = None
    # will be either select.POLLIN or select.EPOLLIN
    # (both are equal to 1)
    _EVENT_READ = None
    # will be either select.POLLOUT or select.EPOLLOUT
    # (both are equal to 4)
    _EVENT_WRITE = None

    # because poll-like sys calls support more than just read/write

    def __init__(self):
        super().__init__()
        # initialize `pollster` (polling object,
        # which supports registering and unregistering file descriptors,
        # and then polling them for I/O events)
        self._selector = self._selector_cls()

    def register(self, fileobj, events, data=None):
        """
        Details:

        1) Creates a SelectorKey for the fileobj and associates it with it's
           underlying fd within the self._fd_to_key dict

        2) Converts the EVENT_READ|EVENT_WRITE event mask module constants
           to poll/epoll/devpoll native event masks.

        3) Calculates the event mask with the native poll/epoll/devpoll native
           bitmasks

        4) Registers the fd and the calculated native event mask
           with the `pollster` object

        """
        # 1) creates a SelectorKey namedtuple consisting of:
        #   original file object, its fd, events mask, user data
        # 2) Associates the resulting SelectorKey with the file object's fd
        #    within the self._fd_to_key dict
        key = super().register(fileobj, events, data)
        poller_events = 0  # 1, 4 or 5
        # calculate the events bitmask,
        # we need to convert the package constants EVENT_READ/EVENT_WRITE
        # to event masks that the polling object will understand
        if events & EVENT_READ:
            poller_events |= self._EVENT_READ
        if events & EVENT_WRITE:
            poller_events |= self._EVENT_WRITE

        try:
            # register a file descriptor with the polling object
            self._selector.register(key.fd, poller_events)
        except:
            # removes the associated SelectorKey from the self._fd_to_key dict
            super().unregister(fileobj)
            raise

        return key

    def unregister(self, fileobj):
        """
        Details:

        1) Removes the SelectorKey associated with the fileobj from the
            self._fd_to_key dict

        2) Calls self._selector.unregister(key.fd) to remove the file descriptor
           from being tracked by the polling object
        """
        # removes the associated SelectorKey from the self._fd_to_key dict
        key = super().unregister(fileobj)
        try:
            # remove a file descriptor being tracked by a polling object
            self._selector.unregister(key.fd)
        except OSError:
            # This can happen if the FD was closed since it
            # was registered.
            pass

        return key

    def modify(self, fileobj, events, data=None):
        """
        Details:

        1) If the file object wasn't registered, raises a KeyError
        2) If the events bitmask is different from the old one, calculates a new
           event mask and call self._selector.modify(key.fd, selector_events)
           which  modifies an already registered fd and has the same effect
           as register(fd, eventmask)
        3) If data or event mask changed, replaces the old selector
           key with a new modified one that contains
           the new passed in event mask and/or data.
        """
        try:
            # check that the fileobj is registered
            # and also keep a reference to the 'old' SelectorKey
            key = self._fd_to_key[self._fileobj_lookup(fileobj)]
        except KeyError:
            raise KeyError(f"{fileobj!r} is not registered") from None

        changed = False

        # if the passed in event mask is not equal to the event mask previously
        # registered for this file object (stored in SelectorKey), then
        # we need to calculate a new events bitmask
        if events != key.events:
            selector_events = 0  # 1, 4 or 5
            # calculate the new events bitmask
            if events & EVENT_READ:
                selector_events |= self._EVENT_READ
            if events & EVENT_WRITE:
                selector_events |= self._EVENT_WRITE

            try:
                # modifies an already registered fd. This has the same effect
                # as register(fd, eventmask).
                # Attempting to modify a file descriptor that
                # was never registered causes an OSError
                self._selector.modify(key.fd, selector_events)
            except:
                # removes the associated SelectorKey from
                # the self._fd_to_key dict
                super().unregister(fileobj)
                raise

            changed = True

        if data != key.data:
            changed = True

        # if events mask, data or both were changed
        # update the selector key
        if changed:
            # this returns a new SelectorKey (namedtuple) with the updated data
            key = key._replace(events=events, data=data)
            # swap out the old SelectorKey for the modified new one
            self._fd_to_key[key.fd] = key

        return key

    def select(self, timeout=None):
        """
        Calls select on the set of registered reader/writer fds/sockets, which
        will return list of ready fds/sockets.

        Returns a list of tuples containing the corresponding SelectorKeys
        and event masks.
        The event mask in the tuple will ensure that even if an event occurred
        on the user's file object, but it wasn't requested by the user,
        it will not be included in the event mask.

        Details:

        1) Polls for I/O events via the polling object

        2) Converts event masks returned with the signaled fds to
           EVENT_READ/EVENT_WRITE masks which the user expects and calculates
           the final event bitmask
           (EVENT_READ, EVENT_WRITE, EVENT_READ|EVENT_WRITE)

        3) If some other events different from EVENT_READ/EVENT_WRITE
           occur, they will be treated as an EVENT_READ|EVENT_WRITE bitmask

        4) Returns a list of tuple consisting of SelectorKeys and event masks
          (the part of the event mask that wasn't requested by the user
          for a particular file object will be excluded from the resulting
          event mask via events & key.events)
          For example:
                If the user registered a file object only for read events,
                but some write events occurred on the underlying fd, the event
                mask returned by this method will be 0, because
                EVENT_READ (1) & EVENT_WRITE (2) will be equal to 0.
        """
        # This is shared between poll(), devpoll() and epoll().
        # epoll() has a different signature and handling of timeout parameter.
        # ensure that timeout is 0, a positive value or None
        if timeout is None:
            timeout = None
        elif timeout <= 0:
            timeout = 0
        else:
            # poll() has a resolution of 1 millisecond, round away from
            # zero to wait *at least* timeout seconds.
            # 1e3 == 1000.0
            timeout = math.ceil(timeout * 1e3)

        ready = []
        try:
            # the actual polling of fds (only sockets on windows)
            # polls the set of registered file descriptors, and returns
            # a possibly-empty list containing tuples of the following
            # format (fd, event mask)
            fd_event_list = self._selector.poll(timeout)
        except InterruptedError:
            # if a signal interrupt occurred
            # just return an empty list
            # BUT in python 3.5 .poll(timeout) is now retried
            # with a recomputed timeout when interrupted by a signal,
            # so it's highly unlikely that we'll hit this except clause
            return ready

        # 1) calculate the event mask for each 'ready' fd/socket
        # 2) we cannot directly use the event mask returned from `.poll` because:
        #   a) we have to convert the POLL specific events mask to
        #      EVENT_WRITE and EVENT_READ constants expected by the user
        #   b) there could be some other events in the mask (POLLHUP and so on),
        #      which we cannot handle directly, because the end user expects
        #      only read/write events - all the other events will be treated
        #      as read/write events
        for fd, event in fd_event_list:
            events = 0
            # two's compliment of self._EVENT_READ is -2,
            # and when 'bitwise anded` with the return event if it results in a
            # non-zero number, it either means that there was a write event
            # and/or some other event like POLLHUP, POLLNVAL and so on
            if event & ~self._EVENT_READ:
                events |= EVENT_WRITE
            # two's compliment of self._EVENT_WRITE is -5,
            # and when 'bitwise anded` with the return event if it results in a
            # non-zero number, it either means that there was a read event
            # and/or some other event like POLLHUP, POLLNVAL and so on
            if event & ~self._EVENT_WRITE:
                events |= EVENT_READ

            # get the corresponding SelectorKey namedtuple for
            # the fd on which some event occurred
            key = self._key_from_fd(fd)

            if key:
                # if the fd was registered,
                # add to results list a tuple:
                #  1) SelectorKey corresponding to the fd/socket
                #  2) Mask - excludes the event mask that wasn't requested by
                #     the user and leaves only the mask that the user requested
                #     if such an event occurred during the poll call:
                #         events & key.events - this will ensure that for example
                #         even if a write event occurred on the fd, but it
                #         was only registered for read events, only
                #         the EVENT_READ mask will be returned to the user
                ready.append((key, events & key.events))

        return ready


if hasattr(select, 'poll'):
    class PollSelector(_PollLikeSelector):
        """
        Poll-based selector.

        More performant than select.

        When we need to track only a 1000 fds or less, isn't less performant
        than epoll.

        I) In a nutshell:

        Pros:
            1) No limit of tracked fds like with select
            2) The structures used to register fd for polling must
               not be reinitialized every time, only its `.revents` field
               needs to be reset
            3) A bigger variety of events, for example we can register that
               a socket hang up without reading from the fd, but just
               checking for a specialized event.

        Cons:
            1) Isn't available on all platforms
            2) Timeout precision is 1 millisecond
            3) To identify the fds that are `ready/generated an event` you have
              to iterate over each structure in the set up till the end, checking
              the `.revents` field
            4) Fds/sockets cannot be modified from a different thread when
              polling is in progress (and you cannot add or delete as well)
            5) `.poll` cannot be called from different threads simultaneously
        """
        _selector_cls = select.poll
        _EVENT_READ = select.POLLIN  # 1
        _EVENT_WRITE = select.POLLOUT  # 4

if hasattr(select, 'epoll'):
    class EpollSelector(_PollLikeSelector):
        """
        Epoll-based selector.

        Is available only on Linux (in 2002 was added to the kernel)

        More performant than poll if we need to monitor more than 1000
        sockets/fds.

        Details:

        Pros:

            1) Returns only those fds that generated an event (we do not have to
              iterate over the whole set of fds)
            2) Some context/user data can be associated with a tracked fd/socket
            3) You can add/delete fds from another thread when polling is
               in progress, and even modify the tracked events
            4) epoll_wait() cann be called from different threads at the same
               time

        Cons:

            1) Requires more sys calls than poll
            2) Switch the tracked events for a socket will require a sys call
               for each socket, while with poll this will only require bitwise
               operations via iteration.
        """
        _selector_cls = select.epoll
        _EVENT_READ = select.EPOLLIN  # 1
        _EVENT_WRITE = select.EPOLLOUT  # 4

        def fileno(self):
            # return the file descriptor number of the control fd
            # (the epoll object)
            return self._selector.fileno()

        def select(self, timeout=None):
            """
            Calls select on the set of registered reader/writer fds/sockets,
            which
            will return list of ready fds/sockets.

            Returns a list of tuples containing the corresponding SelectorKeys
            and event masks.
            The event mask in the tuple will ensure that even if an event
            occurred
            on the user's file object, but it wasn't requested by the user,
            it will not be included in the event mask.

            Details:

            1) Polls for I/O events via the polling object

            2) Converts event masks returned with the signaled fds to
              EVENT_READ/EVENT_WRITE masks which the user expects and calculates
              the final event bitmask
              (EVENT_READ, EVENT_WRITE, EVENT_READ|EVENT_WRITE)

            3) If some other events different from EVENT_READ/EVENT_WRITE
               occur, they will be treated as an EVENT_READ|EVENT_WRITE bitmask

            4) Returns a list of tuple consisting of SelectorKeys and event masks
               (the part of the event mask that wasn't requested by the user
                for a particular file object will be excluded from the resulting
                event mask via events & key.events)
                For example:
                    If the user registered a file object only for read events,
                    but some write events occurred on the underlying fd,
                    the event mask returned by this method will be 0, because
                    EVENT_READ (1) & EVENT_WRITE (2) will be equal to 0.
            """
            if timeout is None:
                timeout = -1
            elif timeout <= 0:
                timeout = 0
            else:
                # epoll_wait() has a resolution of 1 millisecond, round away
                # from zero to wait *at least* timeout seconds.
                timeout = math.ceil(timeout * 1e3) * 1e-3

            # epoll_wait() expects `maxevents` to be greater than zero;
            # we want to make sure that `select()` can be called when no
            # FD is registered.
            max_ev = max(len(self._fd_to_key), 1)

            ready = []
            try:
                # the actual polling of fds
                # polls the set of registered file descriptors, and returns
                # a possibly-empty list containing tuples of the following
                # format (fd, event mask)
                fd_event_list = self._selector.poll(timeout, max_ev)
            except InterruptedError:
                # if a signal interrupt occurred
                # just return an empty list
                # BUT in python 3.5 .poll(timeout) is now retried
                # with a recomputed timeout when interrupted by a signal,
                # so it's highly unlikely that we'll hit this except clause
                return ready

            # 1) calculate the event mask for each 'ready' fd
            # 2) we cannot directly use the event mask returned from
            #   `.poll` because:
            #        a) we have to convert the POLL specific events mask to
            #           EVENT_WRITE and EVENT_READ constants expected by the user
            #        b) there could be some other events in the mask
            #           (EPOLLHUP and so on), which we cannot handle directly,
            #           because the end user expects only read/write events -
            #           all the other events will be treated as read/write events
            for fd, event in fd_event_list:
                events = 0
                # two's compliment of select.EPOLLIN is -2,
                # and when 'bitwise anded` with the return event if it results
                # in a non-zero number, it either means that there was
                # a write event and/or some other event like EPOLLHUP,
                # EPOLLERR and so on
                if event & ~select.EPOLLIN:
                    events |= EVENT_WRITE
                # two's compliment of self._EVENT_WRITE is -5,
                # and when 'bitwise anded` with the return event if it
                # results in a non-zero number, it either means that there
                # was a read event and/or some other event like EPOLLHUP,
                # EPOLLERR and so on
                if event & ~select.EPOLLOUT:
                    events |= EVENT_READ

                # get the corresponding SelectorKey namedtuple for
                # the fd on which some event occurred
                key = self._key_from_fd(fd)

                if key:
                    # if the fd was registered,
                    # add to results list a tuple:
                    #  1) SelectorKey corresponding to the fd/socket
                    #  2) Mask - excludes the event mask that wasn't requested by
                    #     the user and leaves only the mask that the user
                    #     requested if such an event occurred during the
                    #     poll call:
                    #         events & key.events - this will ensure that
                    #         for example even if a write event occurred on the
                    #         fd, but it was only registered for read events,
                    #         only the EVENT_READ mask will be returned
                    #         to the user
                    ready.append((key, events & key.events))
            return ready

        def close(self):
            """
            Details:

            1) Closes the control file descriptor of the epoll object

            2) Clears the fd to SelectorKey dict together with the
               file object to SelectorKey mapping
            """
            # close the control file descriptor of the epoll object
            self._selector.close()
            # clears the fd to SelectorKey dict together with the
            # file object to SelectorKey mapping
            super().close()

if hasattr(select, 'devpoll'):
    class DevpollSelector(_PollLikeSelector):
        """
        Solaris /dev/poll selector.

        Is more performant than the standard poll, but available only on Solaris.
        """
        _selector_cls = select.devpoll
        _EVENT_READ = select.POLLIN
        _EVENT_WRITE = select.POLLOUT

        def fileno(self):
            # return the file descriptor number of the polling object
            return self._selector.fileno()

        def close(self):
            """
            Details:

            1) Closes the file descriptor of the devpoll object

            2) Clears the fd to SelectorKey dict together with the
               file object to SelectorKey mapping
            """
            # close the file descriptor of the polling object
            self._selector.close()
            # clears the fd to SelectorKey dict together with the
            # file object to SelectorKey mapping
            super().close()

if hasattr(select, 'kqueue'):

    class KqueueSelector(_BaseSelectorImpl):
        """Kqueue-based selector."""

        def __init__(self):
            super().__init__()
            # returns a kernel event queue object,
            # the new file descriptor is non-inheritable.
            self._selector = select.kqueue()

        def fileno(self):
            # return the fd number of the control kernel queue object
            return self._selector.fileno()

        def register(self, fileobj, events, data=None):
            # 1) creates a SelectorKey namedtuple consisting of:
            #   original file object, its fd, events mask, user data
            # 2) Associates the resulting SelectorKey with the file object's fd
            #    within the self._fd_to_key dict
            key = super().register(fileobj, events, data)

            # creates event objects (for reading and/or writing) and
            # registers these event object with the kernel event queue
            try:
                if events & EVENT_READ:
                    # returns a kernel event object:
                    # 1) KQ_FILTER_READ - watch for read events on the passed
                    #    in fd
                    # 2) KQ_EV_ADD - add or modify event
                    kev = select.kevent(key.fd, select.KQ_FILTER_READ,
                                        select.KQ_EV_ADD)

                    # first param must be an iterable of kevent objects:
                    #   register events with the kernel event queue
                    self._selector.control([kev], 0, 0)

                if events & EVENT_WRITE:
                    # returns a kernel event object:
                    # 1) KQ_FILTER_WRITE - watch for write events on the passed
                    #   in fd
                    # 2) KQ_EV_ADD - add or modify event
                    kev = select.kevent(key.fd, select.KQ_FILTER_WRITE,
                                        select.KQ_EV_ADD)
                    self._selector.control([kev], 0, 0)

                    # first param must be an iterable of kevent objects:
                    #   register events with the kernel event queue
                    self._selector.control([kev], 0, 0)
            except:
                # removes the associated SelectorKey from the self._fd_to_key
                # dict
                super().unregister(fileobj)
                raise

            return key

        def unregister(self, fileobj):
            # removes the associated SelectorKey from the self._fd_to_key dict
            key = super().unregister(fileobj)

            if key.events & EVENT_READ:
                # prepare the kernel event object responsible for tracking
                # this file object's read events to be removed
                # from the kernel event queue
                kev = select.kevent(key.fd, select.KQ_FILTER_READ,
                                    select.KQ_EV_DELETE)

                try:
                    # remove the kernel event object from the kernel event queue
                    self._selector.control([kev], 0, 0)
                except OSError:
                    # This can happen if the FD was closed since it
                    # was registered.
                    pass

            if key.events & EVENT_WRITE:
                # prepare the kernel event object responsible for tracking
                # this file object's write events to be removed
                # from the kernel event queue
                kev = select.kevent(key.fd, select.KQ_FILTER_WRITE,
                                    select.KQ_EV_DELETE)
                try:
                    # remove the kernel event object from the kernel event queue
                    self._selector.control([kev], 0, 0)
                except OSError:
                    # See comment above.
                    pass

            return key

        def select(self, timeout=None):
            timeout = None if timeout is None else max(timeout, 0)
            # If max_ev is 0, kqueue will ignore the timeout. For consistent
            # behavior with the other selector classes, we prevent that here
            # (using max). See https://bugs.python.org/issue29255
            max_ev = max(len(self._fd_to_key), 1)

            ready = []
            try:
                # Poll for I/O, this will return a list of kevent objects
                kev_list = self._selector.control(None, max_ev, timeout)
            except InterruptedError:
                # if a signal interrupt occurred
                # just return an empty list
                # BUT from python 3.5 .control(timeout) is now retried
                # with a recomputed timeout when interrupted by a signal,
                return ready

            for kev in kev_list:
                # usually a file descriptor on which the event occurred
                fd = kev.ident
                # name of the kernel filter, identifies the events that occurred
                flag = kev.filter

                # convert kqueue events to module level EVENT_READ/EVENT_WRITE
                # events bitmask
                events = 0
                if flag == select.KQ_FILTER_READ:
                    events |= EVENT_READ
                if flag == select.KQ_FILTER_WRITE:
                    events |= EVENT_WRITE

                # get the corresponding SelectorKey namedtuple for
                # the fd on which some event occurred
                key = self._key_from_fd(fd)

                if key:
                    # if the fd was registered,
                    # add to results list a tuple:
                    #  1) SelectorKey corresponding to the fd/socket
                    #  2) Mask - excludes the event mask that wasn't requested by
                    #     the user and leaves only the mask that the user
                    #     requested if such an event occurred during the
                    #     .control call:
                    #         events & key.events - this will ensure that
                    #         for example even if a write event occurred on the
                    #         fd, but it was only registered for read events,
                    #         only the EVENT_READ mask will be returned
                    #         to the user
                    ready.append((key, events & key.events))

        def close(self):
            # close the control file descriptor of the kqueue object
            self._selector.close()
            # clears the fd to SelectorKey dict together with the
            # file object to SelectorKey mapping
            super().close()


def _can_use(method):
    """Check if we can use the selector depending upon the
    operating system. """
    # Implementation based upon
    # https://github.com/sethmlarson/selectors2/blob/master/selectors2.py
    selector = getattr(select, method, None)
    if selector is None:
        # select module does not implement method
        return False
    # check if the OS and Kernel actually support the method. Call may fail with
    # OSError: [Errno 38] Function not implemented
    try:
        selector_obj = selector()
        if method == 'poll':
            # check that poll actually works
            selector_obj.poll(0)
        else:
            # close epoll, kqueue, and devpoll fd
            selector_obj.close()
        return True
    except OSError:
        return False


# Linux - use epoll if possible else poll and then select

# MacOS - use kqueue if possible else poll and then select

# Windows - use select (poll is supported on windows higher than XP
# but its WSAPoll, which has the same api as poll, but python's select
# module doesn't use WSAPoll for windows)

# Solaris - uses devpoll if possible else poll and then select


# Choose the best implementation, roughly:
#    epoll|kqueue|devpoll > poll > select.
# select() also can't accept a FD > FD_SETSIZE (usually around 1024)
if _can_use('kqueue'):
    DefaultSelector = KqueueSelector
elif _can_use('epoll'):
    DefaultSelector = EpollSelector
elif _can_use('devpoll'):
    DefaultSelector = DevpollSelector
elif _can_use('poll'):
    DefaultSelector = PollSelector
else:
    DefaultSelector = SelectSelector
