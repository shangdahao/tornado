#!/usr/bin/env python
#
# Copyright 2009 Facebook
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""An I/O event loop for non-blocking sockets.

Typical applications will use a single `IOLoop` object, in the
`IOLoop.instance` singleton.  The `IOLoop.start` method should usually
be called at the end of the ``main()`` function.  Atypical applications may
use more than one `IOLoop`, such as one `IOLoop` per thread, or per `unittest`
case.

In addition to I/O events, the `IOLoop` can also schedule time-based events.
`IOLoop.add_timeout` is a non-blocking alternative to `time.sleep`.

这个 module 是异步机制的核心，它保存了所有打开的file descriptors 和其 handlers。 它的工作是选择那些
准备读或写的 file descriptor，并调用其 handler。

ioloop 实际上是对 epoll(kqueue) 的封装，并加入了一些对上层事件的处理和 server 相关的底层处理。

tornado 优秀的大并发处理能力得益于它的 web server 从底层开始就自己实现了一整套基于 epoll 的单线程异步架构。

epoll：
ioloop 的实现基于 epoll ，那么什么是 epoll？ epoll 是Linux内核为处理大批量文件描述符而作了改进的 poll 。
那么什么又是 poll ？

socket 通信时的服务端，当它接受（ accept ）一个连接并建立通信后（ connection ）就进行通信，而此时我们并不知道连接的客户端有没有信息发完。
这时候我们有两种选择：
1. 一直在这里等着直到收发数据结束；
2. 每隔一定时间来看看这里有没有数据；

第二种办法要比第一种好一些，多个连接可以统一在一定时间内轮流看一遍里面有没有数据要读写，看上去我们可以处理多个连接了，这个方式就是 poll / select 的解决方案。 
看起来似乎解决了问题，但实际上，随着连接越来越多，轮询所花费的时间将越来越长，而服务器连接的 socket 大多不是活跃的，所以轮询所花费的大部分时间将是无用的。
为了解决这个问题， epoll 被创造出来，它的概念和 poll 类似，不过每次轮询时，他只会把有数据活跃的 socket 挑出来轮询，这样在有大量连接时轮询就节省了大量时间。



"""

from __future__ import absolute_import, division, print_function

import collections
import datetime
import errno
import functools
import heapq
import itertools
import logging
import numbers
import os
import select
import sys
import threading
import time
import traceback
import math

from tornado.concurrent import Future, is_future, chain_future, future_set_exc_info, future_add_done_callback
from tornado.log import app_log, gen_log
from tornado.platform.auto import set_close_exec, Waker
from tornado import stack_context
from tornado.util import PY3, Configurable, errno_from_exception, timedelta_to_seconds, TimeoutError, unicode_type, import_object

try:
    import signal
except ImportError:
    signal = None

try:
    from concurrent.futures import ThreadPoolExecutor
except ImportError:
    ThreadPoolExecutor = None

if PY3:
    import _thread as thread
else:
    import thread

try:
    import asyncio
except ImportError:
    asyncio = None


_POLL_TIMEOUT = 3600.0


class IOLoop(Configurable):
    """A level-triggered I/O loop.

    We use ``epoll`` (Linux) or ``kqueue`` (BSD and Mac OS X) if they
    are available, or else we fall back on select(). If you are
    implementing a system that needs to handle thousands of
    simultaneous connections, you should use a system that supports
    either ``epoll`` or ``kqueue``.

    Example usage for a simple TCP server:

    .. testcode::

        import errno
        import functools
        import tornado.ioloop
        import socket

        def connection_ready(sock, fd, events):
            while True:
                try:
                    connection, address = sock.accept()
                except socket.error as e:
                    if e.args[0] not in (errno.EWOULDBLOCK, errno.EAGAIN):
                        raise
                    return
                connection.setblocking(0)
                handle_connection(connection, address)

        if __name__ == '__main__':
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setblocking(0)
            sock.bind(("", port))
            sock.listen(128)

            io_loop = tornado.ioloop.IOLoop.current()
            callback = functools.partial(connection_ready, sock)
            io_loop.add_handler(sock.fileno(), callback, io_loop.READ)
            io_loop.start()

    .. testoutput::
       :hide:

    By default, a newly-constructed `IOLoop` becomes the thread's current
    `IOLoop`, unless there already is a current `IOLoop`. This behavior
    can be controlled with the ``make_current`` argument to the `IOLoop`
    constructor: if ``make_current=True``, the new `IOLoop` will always
    try to become current and it raises an error if there is already a
    current instance. If ``make_current=False``, the new `IOLoop` will
    not try to become current.

    In general, an `IOLoop` cannot survive a fork or be shared across
    processes in any way. When multiple processes are being used, each
    process should create its own `IOLoop`, which also implies that
    any objects which depend on the `IOLoop` (such as
    `.AsyncHTTPClient`) must also be created in the child processes.
    As a guideline, anything that starts processes (including the
    `tornado.process` and `multiprocessing` modules) should do so as
    early as possible, ideally the first thing the application does
    after loading its configuration in ``main()``.

    .. versionchanged:: 4.2
       Added the ``make_current`` keyword argument to the `IOLoop`
       constructor.
    """
    # Constants from the epoll module
    # 声明了 epoll 监听事件的宏定义
    _EPOLLIN = 0x001  # 缓冲区满，有数据可读
    _EPOLLPRI = 0x002
    _EPOLLOUT = 0x004  # 缓冲区空，可写数据
    _EPOLLERR = 0x008  # 发生错误
    _EPOLLHUP = 0x010
    _EPOLLRDHUP = 0x2000
    _EPOLLONESHOT = (1 << 30)
    _EPOLLET = (1 << 31)

    # Our events map exactly to the epoll events
    NONE = 0
    READ = _EPOLLIN
    WRITE = _EPOLLOUT
    ERROR = _EPOLLERR | _EPOLLHUP

    # Global lock for creating global IOLoop instance
    _instance_lock = threading.Lock()

    _current = threading.local()

    @classmethod
    def configure(cls, impl, **kwargs):
        if asyncio is not None:
            from tornado.platform.asyncio import BaseAsyncIOLoop

            if isinstance(impl, (str, unicode_type)):
                impl = import_object(impl)
            if not issubclass(impl, BaseAsyncIOLoop):
                raise RuntimeError(
                    "only AsyncIOLoop is allowed when asyncio is available")
        super(IOLoop, cls).configure(impl, **kwargs)

    @staticmethod
    def instance():
        """Deprecated alias for `IOLoop.current()`.

        .. versionchanged:: 5.0

           Previously, this method returned a global singleton
           `IOLoop`, in contrast with the per-thread `IOLoop` returned
           by `current()`. In nearly all cases the two were the same
           (when they differed, it was generally used from non-Tornado
           threads to communicate back to the main thread's `IOLoop`).
           This distinction is not present in `asyncio`, so in order
           to facilitate integration with that package `instance()`
           was changed to be an alias to `current()`. Applications
           using the cross-thread communications aspect of
           `instance()` should instead set their own global variable
           to point to the `IOLoop` they want to use.

        .. deprecated:: 5.0
        """
        return IOLoop.current()

    @staticmethod
    def initialized():
        """Returns true if there is a current IOLoop.

        .. versionchanged:: 5.0

           Redefined in terms of `current()` instead of `instance()`.

        .. deprecated:: 5.0

           This method only knows about `IOLoop` objects (and not, for
           example, `asyncio` event loops), so it is of limited use.
        """
        return IOLoop.current(instance=False) is not None

    def install(self):
        """Deprecated alias for `make_current()`.

        .. versionchanged:: 5.0

           Previously, this method would set this `IOLoop` as the
           global singleton used by `IOLoop.instance()`. Now that
           `instance()` is an alias for `current()`, `install()`
           is an alias for `make_current()`.

        .. deprecated:: 5.0
        """
        self.make_current()

    @staticmethod
    def clear_instance():
        """Deprecated alias for `clear_current()`.

        .. versionchanged:: 5.0

           Previously, this method would clear the `IOLoop` used as
           the global singleton by `IOLoop.instance()`. Now that
           `instance()` is an alias for `current()`,
           `clear_instance()` is an alias for `clear_instance()`.

        .. deprecated:: 5.0

        """
        IOLoop.clear_current()

    @staticmethod
    def current(instance=True):
        """Returns the current thread's `IOLoop`.

        If an `IOLoop` is currently running or has been marked as
        current by `make_current`, returns that instance.  If there is
        no current `IOLoop` and ``instance`` is true, creates one.

        .. versionchanged:: 4.1
           Added ``instance`` argument to control the fallback to
           `IOLoop.instance()`.
        .. versionchanged:: 5.0
           The ``instance`` argument now controls whether an `IOLoop`
           is created automatically when there is none, instead of
           whether we fall back to `IOLoop.instance()` (which is now
           an alias for this method)
        """
        current = getattr(IOLoop._current, "instance", None)
        if current is None and instance:
            current = None
            if asyncio is not None:
                from tornado.platform.asyncio import AsyncIOLoop, AsyncIOMainLoop
                if IOLoop.configured_class() is AsyncIOLoop:
                    current = AsyncIOMainLoop()
            if current is None:
                current = IOLoop()
            if IOLoop._current.instance is not current:
                raise RuntimeError("new IOLoop did not become current")
        return current

    def make_current(self):
        """Makes this the `IOLoop` for the current thread.

        An `IOLoop` automatically becomes current for its thread
        when it is started, but it is sometimes useful to call
        `make_current` explicitly before starting the `IOLoop`,
        so that code run at startup time can find the right
        instance.

        .. versionchanged:: 4.1
           An `IOLoop` created while there is no current `IOLoop`
           will automatically become current.
        """
        IOLoop._current.instance = self

    @staticmethod
    def clear_current():
        """Clears the `IOLoop` for the current thread.

        Intended primarily for use by test frameworks in between tests.
        """
        IOLoop._current.instance = None

    @classmethod
    def configurable_base(cls):
        return IOLoop

    @classmethod
    def configurable_default(cls):
        if asyncio is not None:
            from tornado.platform.asyncio import AsyncIOLoop
            return AsyncIOLoop
        return PollIOLoop

    def initialize(self, make_current=None):
        if make_current is None:
            if IOLoop.current(instance=False) is None:
                self.make_current()
        elif make_current:
            if IOLoop.current(instance=False) is not None:
                raise RuntimeError("current IOLoop already exists")
            self.make_current()

    def close(self, all_fds=False):
        """Closes the `IOLoop`, freeing any resources used.

        If ``all_fds`` is true, all file descriptors registered on the
        IOLoop will be closed (not just the ones created by the
        `IOLoop` itself).

        Many applications will only use a single `IOLoop` that runs for the
        entire lifetime of the process.  In that case closing the `IOLoop`
        is not necessary since everything will be cleaned up when the
        process exits.  `IOLoop.close` is provided mainly for scenarios
        such as unit tests, which create and destroy a large number of
        ``IOLoops``.

        An `IOLoop` must be completely stopped before it can be closed.  This
        means that `IOLoop.stop()` must be called *and* `IOLoop.start()` must
        be allowed to return before attempting to call `IOLoop.close()`.
        Therefore the call to `close` will usually appear just after
        the call to `start` rather than near the call to `stop`.

        .. versionchanged:: 3.1
           If the `IOLoop` implementation supports non-integer objects
           for "file descriptors", those objects will have their
           ``close`` method when ``all_fds`` is true.
        """
        raise NotImplementedError()

    def add_handler(self, fd, handler, events):
        """Registers the given handler to receive the given events for ``fd``.

        The ``fd`` argument may either be an integer file descriptor or
        a file-like object with a ``fileno()`` method (and optionally a
        ``close()`` method, which may be called when the `IOLoop` is shut
        down).

        The ``events`` argument is a bitwise or of the constants
        ``IOLoop.READ``, ``IOLoop.WRITE``, and ``IOLoop.ERROR``.

        When an event occurs, ``handler(fd, events)`` will be run.

        .. versionchanged:: 4.0
           Added the ability to pass file-like objects in addition to
           raw file descriptors.
        """
        raise NotImplementedError()

    def update_handler(self, fd, events):
        """Changes the events we listen for ``fd``.

        .. versionchanged:: 4.0
           Added the ability to pass file-like objects in addition to
           raw file descriptors.
        """
        raise NotImplementedError()

    def remove_handler(self, fd):
        """Stop listening for events on ``fd``.

        .. versionchanged:: 4.0
           Added the ability to pass file-like objects in addition to
           raw file descriptors.
        """
        raise NotImplementedError()

    def set_blocking_signal_threshold(self, seconds, action):
        """Sends a signal if the `IOLoop` is blocked for more than
        ``s`` seconds.

        Pass ``seconds=None`` to disable.  Requires Python 2.6 on a unixy
        platform.

        The action parameter is a Python signal handler.  Read the
        documentation for the `signal` module for more information.
        If ``action`` is None, the process will be killed if it is
        blocked for too long.
        """
        raise NotImplementedError()

    def set_blocking_log_threshold(self, seconds):
        """Logs a stack trace if the `IOLoop` is blocked for more than
        ``s`` seconds.

        Equivalent to ``set_blocking_signal_threshold(seconds,
        self.log_stack)``
        """
        self.set_blocking_signal_threshold(seconds, self.log_stack)

    def log_stack(self, signal, frame):
        """Signal handler to log the stack trace of the current thread.

        For use with `set_blocking_signal_threshold`.
        """
        gen_log.warning('IOLoop blocked for %f seconds in\n%s',
                        self._blocking_signal_threshold,
                        ''.join(traceback.format_stack(frame)))

    def start(self):
        """Starts the I/O loop.

        The loop will run until one of the callbacks calls `stop()`, which
        will make the loop stop after the current event iteration completes.
        """
        raise NotImplementedError()

    def _setup_logging(self):
        """The IOLoop catches and logs exceptions, so it's
        important that log output be visible.  However, python's
        default behavior for non-root loggers (prior to python
        3.2) is to print an unhelpful "no handlers could be
        found" message rather than the actual log entry, so we
        must explicitly configure logging if we've made it this
        far without anything.

        This method should be called from start() in subclasses.
        """
        if not any([logging.getLogger().handlers,
                    logging.getLogger('tornado').handlers,
                    logging.getLogger('tornado.application').handlers]):
            logging.basicConfig()

    def stop(self):
        """Stop the I/O loop.

        If the event loop is not currently running, the next call to `start()`
        will return immediately.

        To use asynchronous methods from otherwise-synchronous code (such as
        unit tests), you can start and stop the event loop like this::

          ioloop = IOLoop()
          async_method(ioloop=ioloop, callback=ioloop.stop)
          ioloop.start()

        ``ioloop.start()`` will return after ``async_method`` has run
        its callback, whether that callback was invoked before or
        after ``ioloop.start``.

        Note that even after `stop` has been called, the `IOLoop` is not
        completely stopped until `IOLoop.start` has also returned.
        Some work that was scheduled before the call to `stop` may still
        be run before the `IOLoop` shuts down.
        """
        raise NotImplementedError()

    def run_sync(self, func, timeout=None):
        """Starts the `IOLoop`, runs the given function, and stops the loop.

        The function must return either a yieldable object or
        ``None``. If the function returns a yieldable object, the
        `IOLoop` will run until the yieldable is resolved (and
        `run_sync()` will return the yieldable's result). If it raises
        an exception, the `IOLoop` will stop and the exception will be
        re-raised to the caller.

        The keyword-only argument ``timeout`` may be used to set
        a maximum duration for the function.  If the timeout expires,
        a `tornado.util.TimeoutError` is raised.

        This method is useful in conjunction with `tornado.gen.coroutine`
        to allow asynchronous calls in a ``main()`` function::

            @gen.coroutine
            def main():
                # do stuff...

            if __name__ == '__main__':
                IOLoop.current().run_sync(main)

        .. versionchanged:: 4.3
           Returning a non-``None``, non-yieldable value is now an error.
        """
        future_cell = [None]

        def run():
            try:
                result = func()
                if result is not None:
                    from tornado.gen import convert_yielded
                    result = convert_yielded(result)
            except Exception:
                future_cell[0] = Future()
                future_set_exc_info(future_cell[0], sys.exc_info())
            else:
                if is_future(result):
                    future_cell[0] = result
                else:
                    future_cell[0] = Future()
                    future_cell[0].set_result(result)
            self.add_future(future_cell[0], lambda future: self.stop())
        self.add_callback(run)
        if timeout is not None:
            timeout_handle = self.add_timeout(self.time() + timeout, self.stop)
        self.start()
        if timeout is not None:
            self.remove_timeout(timeout_handle)
        if not future_cell[0].done():
            raise TimeoutError('Operation timed out after %s seconds' % timeout)
        return future_cell[0].result()

    def time(self):
        """Returns the current time according to the `IOLoop`'s clock.

        The return value is a floating-point number relative to an
        unspecified time in the past.

        By default, the `IOLoop`'s time function is `time.time`.  However,
        it may be configured to use e.g. `time.monotonic` instead.
        Calls to `add_timeout` that pass a number instead of a
        `datetime.timedelta` should use this function to compute the
        appropriate time, so they can work no matter what time function
        is chosen.
        """
        return time.time()

    def add_timeout(self, deadline, callback, *args, **kwargs):
        """Runs the ``callback`` at the time ``deadline`` from the I/O loop.

        Returns an opaque handle that may be passed to
        `remove_timeout` to cancel.

        ``deadline`` may be a number denoting a time (on the same
        scale as `IOLoop.time`, normally `time.time`), or a
        `datetime.timedelta` object for a deadline relative to the
        current time.  Since Tornado 4.0, `call_later` is a more
        convenient alternative for the relative case since it does not
        require a timedelta object.

        Note that it is not safe to call `add_timeout` from other threads.
        Instead, you must use `add_callback` to transfer control to the
        `IOLoop`'s thread, and then call `add_timeout` from there.

        Subclasses of IOLoop must implement either `add_timeout` or
        `call_at`; the default implementations of each will call
        the other.  `call_at` is usually easier to implement, but
        subclasses that wish to maintain compatibility with Tornado
        versions prior to 4.0 must use `add_timeout` instead.

        .. versionchanged:: 4.0
           Now passes through ``*args`` and ``**kwargs`` to the callback.
        """
        if isinstance(deadline, numbers.Real):
            return self.call_at(deadline, callback, *args, **kwargs)
        elif isinstance(deadline, datetime.timedelta):
            return self.call_at(self.time() + timedelta_to_seconds(deadline),
                                callback, *args, **kwargs)
        else:
            raise TypeError("Unsupported deadline %r" % deadline)

    def call_later(self, delay, callback, *args, **kwargs):
        """Runs the ``callback`` after ``delay`` seconds have passed.

        Returns an opaque handle that may be passed to `remove_timeout`
        to cancel.  Note that unlike the `asyncio` method of the same
        name, the returned object does not have a ``cancel()`` method.

        See `add_timeout` for comments on thread-safety and subclassing.

        .. versionadded:: 4.0
        """
        return self.call_at(self.time() + delay, callback, *args, **kwargs)

    def call_at(self, when, callback, *args, **kwargs):
        """Runs the ``callback`` at the absolute time designated by ``when``.

        ``when`` must be a number using the same reference point as
        `IOLoop.time`.

        Returns an opaque handle that may be passed to `remove_timeout`
        to cancel.  Note that unlike the `asyncio` method of the same
        name, the returned object does not have a ``cancel()`` method.

        See `add_timeout` for comments on thread-safety and subclassing.

        .. versionadded:: 4.0
        """
        return self.add_timeout(when, callback, *args, **kwargs)

    def remove_timeout(self, timeout):
        """Cancels a pending timeout.

        The argument is a handle as returned by `add_timeout`.  It is
        safe to call `remove_timeout` even if the callback has already
        been run.
        """
        raise NotImplementedError()

    def add_callback(self, callback, *args, **kwargs):
        """Calls the given callback on the next I/O loop iteration.

        It is safe to call this method from any thread at any time,
        except from a signal handler.  Note that this is the **only**
        method in `IOLoop` that makes this thread-safety guarantee; all
        other interaction with the `IOLoop` must be done from that
        `IOLoop`'s thread.  `add_callback()` may be used to transfer
        control from other threads to the `IOLoop`'s thread.

        To add a callback from a signal handler, see
        `add_callback_from_signal`.
        
        """
        raise NotImplementedError()

    def add_callback_from_signal(self, callback, *args, **kwargs):
        """Calls the given callback on the next I/O loop iteration.

        Safe for use from a Python signal handler; should not be used
        otherwise.

        Callbacks added with this method will be run without any
        `.stack_context`, to avoid picking up the context of the function
        that was interrupted by the signal.
        
        这个函数和上面的很类似，只不过他是在without any stack_context的时候去执
        关于stack_context，查阅http://www.tornadoweb.org/en/stable/stack_context.html#module-tornado.stack_context。
        """
        raise NotImplementedError()

    def spawn_callback(self, callback, *args, **kwargs):
        """Calls the given callback on the next IOLoop iteration.

        Unlike all other callback-related methods on IOLoop,
        ``spawn_callback`` does not associate the callback with its caller's
        ``stack_context``, so it is suitable for fire-and-forget callbacks
        that should not interfere with the caller.

        .. versionadded:: 4.0
        """
        with stack_context.NullContext():
            self.add_callback(callback, *args, **kwargs)

    def add_future(self, future, callback):
        """Schedules a callback on the ``IOLoop`` when the given
        `.Future` is finished.

        The callback is invoked with one argument, the
        `.Future`.
        
        这个函数呢也是添加一个callback函数，当给定的这个future执行完的时候，callback会去执行，这个函数有唯一的一个参数就是这个future对象。
        关于future呢???
        """
        assert is_future(future)
        callback = stack_context.wrap(callback)
        future_add_done_callback(future,
                           lambda future: self.add_callback(callback, future))

    def run_in_executor(self, executor, func, *args):
        """Runs a function in a ``concurrent.futures.Executor``. If
        ``executor`` is ``None``, the IO loop's default executor will be used.

        Use `functools.partial` to pass keyword arguments to `func`.

        """
        if ThreadPoolExecutor is None:
            raise RuntimeError(
                "concurrent.futures is required to use IOLoop.run_in_executor")

        if executor is None:
            if not hasattr(self, '_executor'):
                from tornado.process import cpu_count
                self._executor = ThreadPoolExecutor(max_workers=(cpu_count() * 5))
            executor = self._executor
        c_future = executor.submit(func, *args)
        # Concurrent Futures are not usable with await. Wrap this in a
        # Tornado Future instead, using self.add_future for thread-safety.
        t_future = Future()
        self.add_future(c_future, lambda f: chain_future(f, t_future))
        return t_future

    def set_default_executor(self, executor):
        """Sets the default executor to use with :meth:`run_in_executor`."""
        self._executor = executor

    def _run_callback(self, callback):
        """Runs a callback with error handling.

        For use in subclasses.
        """
        try:
            ret = callback()
            if ret is not None:
                from tornado import gen
                # Functions that return Futures typically swallow all
                # exceptions and store them in the Future.  If a Future
                # makes it out to the IOLoop, ensure its exception (if any)
                # gets logged too.
                try:
                    ret = gen.convert_yielded(ret)
                except gen.BadYieldError:
                    # It's not unusual for add_callback to be used with
                    # methods returning a non-None and non-yieldable
                    # result, which should just be ignored.
                    pass
                else:
                    self.add_future(ret, self._discard_future_result)
        except Exception:
            self.handle_callback_exception(callback)

    def _discard_future_result(self, future):
        """Avoid unhandled-exception warnings from spawned coroutines."""
        future.result()

    def handle_callback_exception(self, callback):
        """This method is called whenever a callback run by the `IOLoop`
        throws an exception.

        By default simply logs the exception as an error.  Subclasses
        may override this method to customize reporting of exceptions.

        The exception itself is not passed explicitly, but is available
        in `sys.exc_info`.
        """
        app_log.error("Exception in callback %r", callback, exc_info=True)

    def split_fd(self, fd):
        """Returns an (fd, obj) pair from an ``fd`` parameter.

        We accept both raw file descriptors and file-like objects as
        input to `add_handler` and related methods.  When a file-like
        object is passed, we must retain the object itself so we can
        close it correctly when the `IOLoop` shuts down, but the
        poller interfaces favor file descriptors (they will accept
        file-like objects and call ``fileno()`` for you, but they
        always return the descriptor itself).

        This method is provided for use by `IOLoop` subclasses and should
        not generally be used by application code.

        .. versionadded:: 4.0
        """
        try:
            return fd.fileno(), fd
        except AttributeError:
            return fd, fd

    def close_fd(self, fd):
        """Utility method to close an ``fd``.

        If ``fd`` is a file-like object, we close it directly; otherwise
        we use `os.close`.

        This method is provided for use by `IOLoop` subclasses (in
        implementations of ``IOLoop.close(all_fds=True)`` and should
        not generally be used by application code.

        .. versionadded:: 4.0
        """
        try:
            try:
                fd.close()
            except AttributeError:
                os.close(fd)
        except OSError:
            pass


class PollIOLoop(IOLoop):
    """Base class for IOLoops built around a select-like function.

    For concrete implementations, see `tornado.platform.epoll.EPollIOLoop`
    (Linux), `tornado.platform.kqueue.KQueueIOLoop` (BSD and Mac), or
    `tornado.platform.select.SelectIOLoop` (all platforms).
    """
    def initialize(self, impl, time_func=None, **kwargs):
        super(PollIOLoop, self).initialize(**kwargs)
        self._impl = impl

        # 子进程在fork出来的时候，使用了写时复制（COW，Copy-On-Write）方式获得父进程的数据空间、 堆和栈副本，
        # 这其中也包括文件描述符。刚刚fork成功时，父子进程中相同的文件描述符指向系统文件表中的同一项，
        # 接着，一般我们会调用exec执行另一个程序，此时会用全新的程序替换子进程的正文，数据，堆和栈等。
        # 此时保存文件描述符的变量当然也不存在了，我们就无法关闭无用的文件描述符了。
        # 所以通常我们会fork子进程后在子进程中直接执行close关掉无用的文件描述符，然后再执行exec。
        # 所以 close_exec 执行的其实就是 关闭 ＋ 执行的作用。
        # 详情可以查看： http://blog.csdn.net/ljxfblog/article/details/41680115
        if hasattr(self._impl, 'fileno'):
            set_close_exec(self._impl.fileno())
        self.time_func = time_func or time.time

        # 储存被 epoll 监听的 handler，与打开它的文件描述符 ( file descriptor 简称 fd ) 一一对应
        self._handlers = {}

        # 储存 epoll 返回的活跃的 fd event pairs
        self._events = {}

        # 储存各个 fd 回调函数的列表
        self._callbacks = collections.deque()

        # 一个最小堆结构，按照超时时间从小到大排列的 fd 的任务堆（ 通常这个任务都会包含一个 callback ）
        self._timeouts = []

        # 关于 timeout 的计数器
        self._cancellations = 0
        self._running = False
        self._stopped = False
        self._closing = False

        # 当前线程堆标识符 （ thread identify ）
        self._thread_ident = None
        self._pid = os.getpid()

        # 系统信号， 主要用来在 epoll_wait 时判断是否会有 signal alarm 打断 epoll
        self._blocking_signal_threshold = None
        self._timeout_counter = itertools.count()

        # Create a pipe that we send bogus data to when we want to wake
        # the I/O loop when it is idle
        # 一个 waker 类，主要是对于管道 pipe 的操作，因为 ioloop 属于底层的数据操作，这里 epoll 监听的是 pipe
        self._waker = Waker()

        # 将管道加入 epoll 监听，对于 web server 初始化时只需要关心 READ 事件
        self.add_handler(self._waker.fileno(),
                         lambda fd, events: self._waker.consume(),
                         self.READ)

    @classmethod
    def configurable_base(cls):
        return PollIOLoop

    @classmethod
    def configurable_default(cls):
        # Python select doc https://docs.python.org/3/library/select.html
        if hasattr(select, "epoll"):
            from tornado.platform.epoll import EPollIOLoop
            return EPollIOLoop
        if hasattr(select, "kqueue"):
            # Python 2.6+ on BSD or Mac
            from tornado.platform.kqueue import KQueueIOLoop
            return KQueueIOLoop
        from tornado.platform.select import SelectIOLoop
        return SelectIOLoop

    def close(self, all_fds=False):
        self._closing = True
        self.remove_handler(self._waker.fileno())
        if all_fds:
            for fd, handler in list(self._handlers.values()):
                self.close_fd(fd)
        self._waker.close()
        self._impl.close()
        self._callbacks = None
        self._timeouts = None
        if hasattr(self, '_executor'):
            self._executor.shutdown()

    def add_handler(self, fd, handler, events):
        """
        注册一个handler，从fd那里接受事件。 
        fd呢就是一个描述符，events就是要监听的事件。 
        events有这样几种类型，IOLoop.READ, IOLoop.WRITE, 还有IOLoop.ERROR. 很好理解，读写事件，还有错误异常。
        当我们选定的类型事件发生的时候，那么就会执行handler(fd, events)。
        """
        fd, obj = self.split_fd(fd)
        self._handlers[fd] = (obj, stack_context.wrap(handler))
        self._impl.register(fd, events | self.ERROR)

    def update_handler(self, fd, events):
        fd, obj = self.split_fd(fd)
        self._impl.modify(fd, events | self.ERROR)

    def remove_handler(self, fd):
        fd, obj = self.split_fd(fd)
        self._handlers.pop(fd, None)
        self._events.pop(fd, None)
        try:
            self._impl.unregister(fd)
        except Exception:
            gen_log.debug("Error deleting fd from IOLoop", exc_info=True)

    def set_blocking_signal_threshold(self, seconds, action):
        if not hasattr(signal, "setitimer"):
            gen_log.error("set_blocking_signal_threshold requires a signal module "
                          "with the setitimer method")
            return
        self._blocking_signal_threshold = seconds
        if seconds is not None:
            signal.signal(signal.SIGALRM,
                          action if action is not None else signal.SIG_DFL)

    # ioloop 最核心的部分
    def start(self):
        if self._running:
            raise RuntimeError("IOLoop is already running")
        if os.getpid() != self._pid:
            raise RuntimeError("Cannot share PollIOLoops across processes")
        self._setup_logging()
        if self._stopped:
            self._stopped = False
            return
        old_current = getattr(IOLoop._current, "instance", None)
        IOLoop._current.instance = self
        self._thread_ident = thread.get_ident()
        self._running = True

        # signal.set_wakeup_fd closes a race condition in event loops:
        # a signal may arrive at the beginning of select/poll/etc
        # before it goes into its interruptible sleep, so the signal
        # will be consumed without waking the select.  The solution is
        # for the (C, synchronous) signal handler to write to a pipe,
        # which will then be seen by select.
        #
        # In python's signal handling semantics, this only matters on the
        # main thread (fortunately, set_wakeup_fd only works on the main
        # thread and will raise a ValueError otherwise).
        #
        # If someone has already set a wakeup fd, we don't want to
        # disturb it.  This is an issue for twisted, which does its
        # SIGCHLD processing in response to its own wakeup fd being
        # written to.  As long as the wakeup fd is registered on the IOLoop,
        # the loop will still wake up and everything should work.
        old_wakeup_fd = None
        if hasattr(signal, 'set_wakeup_fd') and os.name == 'posix':
            # requires python 2.6+, unix.  set_wakeup_fd exists but crashes
            # the python process on windows.
            try:
                old_wakeup_fd = signal.set_wakeup_fd(self._waker.write_fileno())
                if old_wakeup_fd != -1:
                    # Already set, restore previous value.  This is a little racy,
                    # but there's no clean get_wakeup_fd and in real use the
                    # IOLoop is just started once at the beginning.
                    signal.set_wakeup_fd(old_wakeup_fd)
                    old_wakeup_fd = None
            except ValueError:
                # Non-main thread, or the previous value of wakeup_fd
                # is no longer valid.
                old_wakeup_fd = None

        try:
            # 服务器进程正式开始，类似于其他服务器的 serve_forever
            while True:
                # Prevent IO event starvation by delaying new callbacks
                # to the next iteration of the event loop.
                ncallbacks = len(self._callbacks)

                # Add any timeouts that have come due to the callback list.
                # Do not run anything until we have determined which ones
                # are ready, so timeouts that call add_timeout cannot
                # schedule anything in this iteration.
                # 用于存放这个周期内已过期（ 已超时 ）的任务
                due_timeouts = []
                if self._timeouts:
                    now = self.time()
                    # _timeouts 有数据时一直循环, _timeouts 是个最小堆，第一个数据永远是最小的， 这里第一个数据永远是最接近超时或已超时的
                    while self._timeouts:
                        if self._timeouts[0].callback is None:
                            # The timeout was cancelled.  Note that the
                            # cancellation check is repeated below for timeouts
                            # that are cancelled by another timeout or callback.
                            # 超时任务无回调, 直接弹出, 超时计数器 －1
                            heapq.heappop(self._timeouts)
                            self._cancellations -= 1
                        elif self._timeouts[0].deadline <= now:
                            # 判断最小的数据是否超时, 超时就加到已超时列表里
                            due_timeouts.append(heapq.heappop(self._timeouts))
                        else:
                            # 因为最小堆，如果没超时就直接退出循环（ 后面的数据必定未超时 ）
                            break
                    if (self._cancellations > 512 and
                            self._cancellations > (len(self._timeouts) >> 1)):
                        # Clean up the timeout queue when it gets large and it's
                        # more than half cancellations.
                        # 当超时计数器大于 512 并且 大于 _timeouts 长度一半（ >> 为右移运算， 相当于十进制数据被除 2 ）时，
                        # 清零计数器，并剔除 _timeouts 中无 callbacks 的任务
                        self._cancellations = 0
                        self._timeouts = [x for x in self._timeouts
                                          if x.callback is not None]
                        # 进行 _timeouts 最小堆化
                        heapq.heapify(self._timeouts)

                # 运行 callbacks 里所有的 calllback
                for i in range(ncallbacks):
                    self._run_callback(self._callbacks.popleft())

                # 运行所有已过期任务的 callback
                for timeout in due_timeouts:
                    if timeout.callback is not None:
                        self._run_callback(timeout.callback)

                # Closures may be holding on to a lot of memory, so allow
                # them to be freed before we go into our poll wait.
                # 释放内存
                due_timeouts = timeout = None

                if self._callbacks:
                    # If any callbacks or timeouts called add_callback,
                    # we don't want to wait in poll() before we run them.
                    # _callbacks 里有数据时, 设置 epoll_wait 时间为0（ 立即返回 ）
                    poll_timeout = 0.0
                elif self._timeouts:
                    # If there are any timeouts, schedule the first one.
                    # Use self.time() instead of 'now' to account for time
                    # spent running callbacks.
                    # _timeouts 里有数据时,# 取最小过期时间当 epoll_wait 等待时间，这样当第一个任务过期时立即返回,
                    # 果最小过期时间大于默认等待时间 _POLL_TIMEOUT ＝ 3600，则用 3600，如果最小过期时间小于0 就设置为0 立即返回。
                    poll_timeout = self._timeouts[0].deadline - self.time()
                    poll_timeout = max(0, min(poll_timeout, _POLL_TIMEOUT))
                else:
                    # No timeouts and no callbacks, so use the default.
                    # # 默认 3600 s 等待时间
                    poll_timeout = _POLL_TIMEOUT

                # 检查是否有系统信号中断运行，有则中断，无则继续
                if not self._running:
                    break

                if self._blocking_signal_threshold is not None:
                    # clear alarm so it doesn't fire while poll is waiting for
                    # events.
                    # 开始 epoll_wait 之前确保 signal alarm 都被清空（ 这样在 epoll_wait 过程中不会被 signal alarm 打断 ）
                    signal.setitimer(signal.ITIMER_REAL, 0, 0)

                try:
                    event_pairs = self._impl.poll(poll_timeout) # 获取返回的活跃事件
                except Exception as e:
                    # Depending on python version and IOLoop implementation,
                    # different exception types may be thrown and there are
                    # two ways EINTR might be signaled:
                    # * e.errno == errno.EINTR
                    # * e.args is like (errno.EINTR, 'Interrupted system call')
                    if errno_from_exception(e) == errno.EINTR:
                        continue
                    else:
                        raise

                if self._blocking_signal_threshold is not None:
                    signal.setitimer(signal.ITIMER_REAL,
                                     self._blocking_signal_threshold, 0)

                # Pop one fd at a time from the set of pending fds and run
                # its handler. Since that handler may perform actions on
                # other file descriptors, there may be reentrant calls to
                # this IOLoop that modify self._events
                self._events.update(event_pairs) # 将活跃事件加入 _events
                while self._events:
                    fd, events = self._events.popitem() # 循环弹出事件
                    try:
                        fd_obj, handler_func = self._handlers[fd] # 处理事件
                        handler_func(fd_obj, events)
                    except (OSError, IOError) as e:
                        if errno_from_exception(e) == errno.EPIPE:
                            # Happens when the client closes the connection
                            pass
                        else:
                            self.handle_callback_exception(self._handlers.get(fd))
                    except Exception:
                        self.handle_callback_exception(self._handlers.get(fd))
                fd_obj = handler_func = None

        finally:
            # reset the stopped flag so another start/stop pair can be issued
            # 确保发生异常也继续运行
            self._stopped = False
            if self._blocking_signal_threshold is not None:
                signal.setitimer(signal.ITIMER_REAL, 0, 0)
            IOLoop._current.instance = old_current
            if old_wakeup_fd is not None:
                signal.set_wakeup_fd(old_wakeup_fd)

    def stop(self):
        self._running = False
        self._stopped = True
        self._waker.wake() # 向 pipe 写入随意字符唤醒 ioloop 事件循环

    def time(self):
        return self.time_func()

    def call_at(self, deadline, callback, *args, **kwargs):
        timeout = _Timeout(
            deadline,
            functools.partial(stack_context.wrap(callback), *args, **kwargs),
            self)
        heapq.heappush(self._timeouts, timeout)
        return timeout

    def remove_timeout(self, timeout):
        # Removing from a heap is complicated, so just leave the defunct
        # timeout object in the queue (see discussion in
        # http://docs.python.org/library/heapq.html).
        # If this turns out to be a problem, we could add a garbage
        # collection pass whenever there are too many dead timeouts.
        timeout.callback = None
        self._cancellations += 1

    def add_callback(self, callback, *args, **kwargs):
        if self._closing:
            return
        # Blindly insert into self._callbacks. This is safe even
        # from signal handlers because deque.append is atomic.
        self._callbacks.append(functools.partial(
            stack_context.wrap(callback), *args, **kwargs))
        if thread.get_ident() != self._thread_ident:
            # This will write one byte but Waker.consume() reads many
            # at once, so it's ok to write even when not strictly
            # necessary.
            self._waker.wake()
        else:
            # If we're on the IOLoop's thread, we don't need to wake anyone.
            pass

    def add_callback_from_signal(self, callback, *args, **kwargs):
        with stack_context.NullContext():
            self.add_callback(callback, *args, **kwargs)


class _Timeout(object):
    """An IOLoop timeout, a UNIX timestamp and a callback"""

    # Reduce memory overhead when there are lots of pending callbacks
    __slots__ = ['deadline', 'callback', 'tdeadline']

    def __init__(self, deadline, callback, io_loop):
        if not isinstance(deadline, numbers.Real):
            raise TypeError("Unsupported deadline %r" % deadline)
        self.deadline = deadline
        self.callback = callback
        self.tdeadline = (deadline, next(io_loop._timeout_counter))

    # Comparison methods to sort by deadline, with object id as a tiebreaker
    # to guarantee a consistent ordering.  The heapq module uses __le__
    # in python2.5, and __lt__ in 2.6+ (sort() and most other comparisons
    # use __lt__).
    def __lt__(self, other):
        return self.tdeadline < other.tdeadline

    def __le__(self, other):
        return self.tdeadline <= other.tdeadline


class PeriodicCallback(object):
    """Schedules the given callback to be called periodically.

    The callback is called every ``callback_time`` milliseconds.
    Note that the timeout is given in milliseconds, while most other
    time-related functions in Tornado use seconds.

    If the callback runs for longer than ``callback_time`` milliseconds,
    subsequent invocations will be skipped to get back on schedule.

    `start` must be called after the `PeriodicCallback` is created.

    .. versionchanged:: 5.0
       The ``io_loop`` argument (deprecated since version 4.1) has been removed.
    """
    def __init__(self, callback, callback_time):
        self.callback = callback
        if callback_time <= 0:
            raise ValueError("Periodic callback must have a positive callback_time")
        self.callback_time = callback_time
        self._running = False
        self._timeout = None

    def start(self):
        """Starts the timer."""
        # Looking up the IOLoop here allows to first instantiate the
        # PeriodicCallback in another thread, then start it using
        # IOLoop.add_callback().
        self.io_loop = IOLoop.current()
        self._running = True
        self._next_timeout = self.io_loop.time()
        self._schedule_next()

    def stop(self):
        """Stops the timer."""
        self._running = False
        if self._timeout is not None:
            self.io_loop.remove_timeout(self._timeout)
            self._timeout = None

    def is_running(self):
        """Return True if this `.PeriodicCallback` has been started.

        .. versionadded:: 4.1
        """
        return self._running

    def _run(self):
        if not self._running:
            return
        try:
            return self.callback()
        except Exception:
            self.io_loop.handle_callback_exception(self.callback)
        finally:
            self._schedule_next()

    def _schedule_next(self):
        if self._running:
            current_time = self.io_loop.time()

            if self._next_timeout <= current_time:
                callback_time_sec = self.callback_time / 1000.0
                self._next_timeout += (math.floor((current_time - self._next_timeout) /
                                                  callback_time_sec) + 1) * callback_time_sec

            self._timeout = self.io_loop.add_timeout(self._next_timeout, self._run)
