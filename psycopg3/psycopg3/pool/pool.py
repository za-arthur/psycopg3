"""
psycopg3 synchronous connection pool
"""

# Copyright (C) 2021 The Psycopg Team

import time
import random
import logging
import threading
from queue import Queue, Empty
from typing import Any, Callable, Deque, Dict, Iterator, List, Optional
from contextlib import contextmanager
from collections import deque

from ..pq import TransactionStatus
from ..connection import Connection

from . import tasks
from .base import ConnectionAttempt
from .sched import Scheduler
from .errors import PoolClosed, PoolTimeout

logger = logging.getLogger(__name__)

WORKER_TIMEOUT = 60.0


class ConnectionPool:

    _num_pool = 0

    def __init__(
        self,
        conninfo: str = "",
        kwargs: Optional[Dict[str, Any]] = None,
        configure: Optional[Callable[[Connection], None]] = None,
        minconn: int = 4,
        maxconn: Optional[int] = None,
        name: Optional[str] = None,
        timeout: float = 30.0,
        max_idle: float = 10 * 60.0,
        reconnect_timeout: float = 5 * 60.0,
        reconnect_failed: Optional[Callable[["ConnectionPool"], None]] = None,
        num_workers: int = 3,
    ):
        if maxconn is None:
            maxconn = minconn
        if maxconn < minconn:
            raise ValueError(
                f"can't create {self.__class__.__name__}"
                f" with maxconn={maxconn} < minconn={minconn}"
            )
        if not name:
            self.__class__._num_pool += 1
            name = f"pool-{self._num_pool}"

        if num_workers < 1:
            # TODO: allow num_workers to be 0 - sync pool?
            raise ValueError("num_workers must be at least 1")

        self.conninfo = conninfo
        self.kwargs: Dict[str, Any] = kwargs or {}
        self._configure: Callable[[Connection], None]
        self._configure = configure or (lambda conn: None)
        self._reconnect_failed: Callable[["ConnectionPool"], None]
        self._reconnect_failed = reconnect_failed or (lambda pool: None)
        self.name = name
        self.minconn = minconn
        self.maxconn = maxconn
        self.timeout = timeout
        self.reconnect_timeout = reconnect_timeout
        self.max_idle = max_idle
        self.num_workers = num_workers

        self._nconns = minconn  # currently in the pool, out, being prepared
        self._pool: Deque[Connection] = deque()
        self._waiting: Deque["WaitingClient"] = deque()
        self._lock = threading.RLock()
        self._sched = Scheduler()

        # Min number of connections in the pool in a max_idle unit of time.
        # It is reset periodically by the ShrinkPool scheduled task.
        # It is used to shrink back the pool if maxcon > minconn and extra
        # connections have been acquired, if we notice that in the last
        # max_idle interval they weren't all used.
        self._nconns_min = minconn

        # to notify that the pool is full
        self._pool_full_event: Optional[threading.Event] = None

        self._tasks: "Queue[tasks.MaintenanceTask]" = Queue()
        self._workers: List[threading.Thread] = []
        for i in range(num_workers):
            t = threading.Thread(
                target=self.worker, args=(self._tasks,), daemon=True
            )
            self._workers.append(t)

        self._sched_runner = threading.Thread(
            target=self._sched.run, daemon=True
        )

        # _close should be the last property to be set in the state
        # to avoid warning on __del__ in case __init__ fails.
        self._closed = False

        # The object state is complete. Start the worker threads
        self._sched_runner.start()
        for t in self._workers:
            t.start()

        # Populate the pool with initial minconn connections in background
        for i in range(self._nconns):
            self.run_task(tasks.AddConnection(self))

        # Schedule a task to shrink the pool if connections over minconn have
        # remained unused. However if the pool cannot't grow don't bother.
        if maxconn > minconn:
            self.schedule_task(tasks.ShrinkPool(self), self.max_idle)

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__module__}.{self.__class__.__name__}"
            f" {self.name!r} at 0x{id(self):x}>"
        )

    def __del__(self) -> None:
        # If the '_closed' property is not set we probably failed in __init__.
        # Don't try anything complicated as probably it won't work.
        if hasattr(self, "_closed"):
            self.close(timeout=0)

    def wait_ready(self, timeout: float = 30.0) -> None:
        """
        Wait for the pool to be full after init.

        Raise `PoolTimeout` if not ready within *timeout* sec.
        """
        with self._lock:
            assert not self._pool_full_event
            if len(self._pool) >= self._nconns:
                return
            self._pool_full_event = threading.Event()

        if not self._pool_full_event.wait(timeout):
            self.close()  # stop all the threads
            raise PoolTimeout(
                f"pool initialization incomplete after {timeout} sec"
            )

        with self._lock:
            self._pool_full_event = None

    @contextmanager
    def connection(
        self, timeout: Optional[float] = None
    ) -> Iterator[Connection]:
        """Context manager to obtain a connection from the pool.

        Returned the connection immediately if available, otherwise wait up to
        *timeout* or `self.timeout` and throw `PoolTimeout` if a connection is
        not available in time.

        Upon context exit, return the connection to the pool. Apply the normal
        connection context behaviour (commit/rollback the transaction in case
        of success/error). If the connection is no more in working state
        replace it with a new one.
        """
        conn = self.getconn(timeout=timeout)
        try:
            with conn:
                yield conn
        finally:
            self.putconn(conn)

    def getconn(self, timeout: Optional[float] = None) -> Connection:
        """Obtain a contection from the pool.

        You should preferrably use `connection()`. Use this function only if
        it is not possible to use the connection as context manager.

        After using this function you *must* call a corresponding `putconn()`:
        failing to do so will deplete the pool. A depleted pool is a sad pool:
        you don't want a depleted pool.
        """
        logger.info("connection requested to %r", self.name)
        # Critical section: decide here if there's a connection ready
        # or if the client needs to wait.
        with self._lock:
            if self._closed:
                raise PoolClosed(f"the pool {self.name!r} is closed")

            pos: Optional[WaitingClient] = None
            if self._pool:
                # Take a connection ready out of the pool
                conn = self._pool.popleft()
                if len(self._pool) < self._nconns_min:
                    self._nconns_min = len(self._pool)
            else:
                # No connection available: put the client in the waiting queue
                pos = WaitingClient()
                self._waiting.append(pos)

                # If there is space for the pool to grow, let's do it
                if self._nconns < self.maxconn:
                    self._nconns += 1
                    logger.info(
                        "growing pool %r to %s", self.name, self._nconns
                    )
                    self.run_task(tasks.AddConnection(self))

        # If we are in the waiting queue, wait to be assigned a connection
        # (outside the critical section, so only the waiting client is locked)
        if pos:
            if timeout is None:
                timeout = self.timeout
            conn = pos.wait(timeout=timeout)

        # Tell the connection it belongs to a pool to avoid closing on __exit__
        # Note that this property shouldn't be set while the connection is in
        # the pool, to avoid to create a reference loop.
        conn._pool = self
        logger.info("connection given by %r", self.name)
        return conn

    def putconn(self, conn: Connection) -> None:
        """Return a connection to the loving hands of its pool.

        Use this function only paired with a `getconn()`. You don't need to use
        it if you use the much more comfortable `connection()` context manager.
        """
        # Quick check to discard the wrong connection
        pool = getattr(conn, "_pool", None)
        if pool is not self:
            if pool:
                msg = f"it comes from pool {pool.name!r}"
            else:
                msg = "it doesn't come from any pool"
            raise ValueError(
                f"can't return connection to pool {self.name!r}, {msg}: {conn}"
            )

        logger.info("returning connection to %r", self.name)

        # If the pool is closed just close the connection instead of returning
        # it to the pool. For extra refcare remove the pool reference from it.
        if self._closed:
            conn._pool = None
            conn.close()
            return

        # Use a worker to perform eventual maintenance work in a separate thread
        self.run_task(tasks.ReturnConnection(self, conn))

    @property
    def closed(self) -> bool:
        """`!True` if the pool is closed."""
        return self._closed

    def close(self, timeout: float = 1.0) -> None:
        """Close the pool and make it unavailable to new clients.

        All the waiting and future client will fail to acquire a connection
        with a `PoolClosed` exception. Currently used connections will not be
        closed until returned to the pool.

        Wait *timeout* for threads to terminate their job, if positive.
        """
        if self._closed:
            return

        with self._lock:
            self._closed = True

            # Take waiting client and pool connections out of the state
            waiting = list(self._waiting)
            self._waiting.clear()
            pool = list(self._pool)
            self._pool.clear()

        # Now that the flag _closed is set, getconn will fail immediately,
        # putconn will just close the returned connection.

        # Stop the scheduler
        self._sched.enter(0, None)

        # Stop the worker threads
        for i in range(len(self._workers)):
            self.run_task(tasks.StopWorker(self))

        # Signal to eventual clients in the queue that business is closed.
        for pos in waiting:
            pos.fail(PoolClosed(f"the pool {self.name!r} is closed"))

        # Close the connections still in the pool
        for conn in pool:
            conn.close()

        # Wait for the worker threads to terminate
        if timeout > 0:
            for t in [self._sched_runner] + self._workers:
                if not t.is_alive():
                    continue
                t.join(timeout)
                if t.is_alive():
                    logger.warning(
                        "couldn't stop thread %s in pool %r within %s seconds",
                        t,
                        self.name,
                        timeout,
                    )

    def run_task(self, task: tasks.MaintenanceTask) -> None:
        """Run a maintenance task in a worker thread."""
        self._tasks.put(task)

    def schedule_task(self, task: tasks.MaintenanceTask, delay: float) -> None:
        """Run a maintenance task in a worker thread in the future."""
        self._sched.enter(delay, task.tick)

    @classmethod
    def worker(cls, q: "Queue[tasks.MaintenanceTask]") -> None:
        """Runner to execute pending maintenance task.

        The function is designed to run as a separate thread.

        Block on the queue *q*, run a task received. Finish running if a
        StopWorker is received.
        """
        # Don't make all the workers time out at the same moment
        timeout = WORKER_TIMEOUT * (0.9 + 0.1 * random.random())
        while True:
            # Use a timeout to make the wait unterruptable
            try:
                task = q.get(timeout=timeout)
            except Empty:
                continue

            # Run the task. Make sure don't die in the attempt.
            try:
                task.run()
            except Exception as e:
                logger.warning(
                    "task run %s failed: %s: %s", task, e.__class__.__name__, e
                )

            if isinstance(task, tasks.StopWorker):
                return

    def configure(self, conn: Connection) -> None:
        """Configure a connection after creation."""
        self._configure(conn)

    def reconnect_failed(self) -> None:
        """
        Called when reconnection failed for longer than `reconnect_timeout`.
        """
        self._reconnect_failed(self)

    def _connect(self) -> Connection:
        """Return a new connection configured for the pool."""
        conn = Connection.connect(self.conninfo, **self.kwargs)
        self.configure(conn)
        conn._pool = self
        return conn

    def _add_connection(self, attempt: Optional[ConnectionAttempt]) -> None:
        """Try to connect and add the connection to the pool.

        If failed, reschedule a new attempt in the future for a few times, then
        give up, decrease the pool connections number and call
        `self.reconnect_failed()`.

        """
        now = time.monotonic()
        if not attempt:
            attempt = ConnectionAttempt(
                reconnect_timeout=self.reconnect_timeout
            )

        try:
            conn = self._connect()
        except Exception as e:
            logger.warning(f"error connecting in {self.name!r}: {e}")
            if attempt.time_to_give_up(now):
                logger.warning(
                    "reconnection attempt in pool %r failed after %s sec",
                    self.name,
                    self.reconnect_timeout,
                )
                with self._lock:
                    self._nconns -= 1
                self.reconnect_failed()
            else:
                attempt.update_delay(now)
                self.schedule_task(
                    tasks.AddConnection(self, attempt), attempt.delay
                )
        else:
            self._add_to_pool(conn)

    def _return_connection(self, conn: Connection) -> None:
        """
        Return a connection to the pool after usage.
        """
        self._reset_connection(conn)
        if conn.pgconn.transaction_status == TransactionStatus.UNKNOWN:
            # Connection no more in working state: create a new one.
            logger.warning("discarding closed connection: %s", conn)
            self.run_task(tasks.AddConnection(self))
        else:
            self._add_to_pool(conn)

    def _add_to_pool(self, conn: Connection) -> None:
        """
        Add a connection to the pool.

        The connection can be a fresh one or one already used in the pool.

        If a client is already waiting for a connection pass it on, otherwise
        put it back into the pool
        """
        # Remove the pool reference from the connection before returning it
        # to the state, to avoid to create a reference loop.
        # Also disable the warning for open connection in conn.__del__
        conn._pool = None

        pos: Optional[WaitingClient] = None

        # Critical section: if there is a client waiting give it the connection
        # otherwise put it back into the pool.
        with self._lock:
            while self._waiting:
                # If there is a client waiting (which is still waiting and
                # hasn't timed out), give it the connection and notify it.
                pos = self._waiting.popleft()
                if pos.set(conn):
                    break

            else:
                # No client waiting for a connection: put it back into the pool
                self._pool.append(conn)

                # If we have been asked to wait for pool init, notify the
                # waiter if the pool is full.
                if self._pool_full_event and len(self._pool) >= self._nconns:
                    self._pool_full_event.set()

    def _reset_connection(self, conn: Connection) -> None:
        """
        Bring a connection to IDLE state or close it.
        """
        status = conn.pgconn.transaction_status
        if status == TransactionStatus.IDLE:
            return

        if status in (TransactionStatus.INTRANS, TransactionStatus.INERROR):
            # Connection returned with an active transaction
            logger.warning("rolling back returned connection: %s", conn)
            try:
                conn.rollback()
            except Exception as e:
                logger.warning(
                    "rollback failed: %s: %s. Discarding connection %s",
                    e.__class__.__name__,
                    e,
                    conn,
                )
                conn.close()

        elif status == TransactionStatus.ACTIVE:
            # Connection returned during an operation. Bad... just close it.
            logger.warning("closing returned connection: %s", conn)
            conn.close()

    def _shrink_if_possible(self) -> None:
        to_close: Optional[Connection] = None

        with self._lock:
            # Reset the min number of connections used
            nconns_min = self._nconns_min
            self._nconns_min = len(self._pool)

            # If the pool can shrink and connections were unused, drop one
            if self._nconns > self.minconn and nconns_min > 0:
                to_close = self._pool.popleft()
                self._nconns -= 1

        if to_close:
            logger.info(
                "shrinking pool %r to %s because %s unused connections"
                " in the last %s sec",
                self.name,
                self._nconns,
                nconns_min,
                self.max_idle,
            )
            to_close.close()


class WaitingClient:
    """A position in a queue for a client waiting for a connection."""

    __slots__ = ("conn", "error", "_cond")

    def __init__(self) -> None:
        self.conn: Optional[Connection] = None
        self.error: Optional[Exception] = None

        # The WaitingClient behaves in a way similar to an Event, but we need
        # to notify reliably the flagger that the waiter has "accepted" the
        # message and it hasn't timed out yet, otherwise the pool may give a
        # connection to a client that has already timed out getconn(), which
        # will be lost.
        self._cond = threading.Condition(threading.Lock())

    def wait(self, timeout: float) -> Connection:
        """Wait for a connection to be set and return it.

        Raise an exception if the wait times out or if fail() is called.
        """
        with self._cond:
            if not (self.conn or self.error):
                if not self._cond.wait(timeout):
                    self.error = PoolTimeout(
                        f"couldn't get a connection after {timeout} sec"
                    )

        if self.conn:
            return self.conn
        else:
            assert self.error
            raise self.error

    def set(self, conn: Connection) -> bool:
        """Signal the client waiting that a connection is ready.

        Return True if the client has "accepted" the connection, False
        otherwise (typically because wait() has timed out).
        """
        with self._cond:
            if self.conn or self.error:
                return False

            self.conn = conn
            self._cond.notify_all()
            return True

    def fail(self, error: Exception) -> bool:
        """Signal the client that, alas, they won't have a connection today.

        Return True if the client has "accepted" the error, False otherwise
        (typically because wait() has timed out).
        """
        with self._cond:
            if self.conn or self.error:
                return False

            self.error = error
            self._cond.notify_all()
            return True
