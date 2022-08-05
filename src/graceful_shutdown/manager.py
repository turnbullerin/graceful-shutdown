""" Provides the ShutdownProtection() context manager which allows for processes to better control how they will
    respond to SIGINT and SIGBREAK. In addition, the manager will turn closing the console windows and shutdown/logoff
    events in Windows into proper SystemExit() exceptions. The main use case for this module is to allow a block of code
    to manage its execution and shutdown when the system is going down or a user has requested the program to halt, thus
    enabling clean-up tasks to be performed.

    SIGINT is typically triggered by entering CTRL-C to a console program or by the system when requesting the program
    terminate. It is supported on both Windows and Unix.

    SIGTERM (or the Windows equivalent SIGBREAK) is typically issued by the system when requesting the program
    terminate or when CTRL-BREAK is entered. Note that, on Windows, SIGBREAK from a CLOSE, SHUTDOWN, or LOGOFF event
    is not given a chance to be processed by the interpreter. This package leverages pywin32 to provide an
    appropriate handler that ensures SIGTERM on Linux and SIGBREAK/CTRL_CLOSE_EVENT/etc on Windows are handled in a
    similar fashion. In this documentation, all of these will be referred to as SIGTERM for simplicity.

    An upside of this handler is that it causes SystemExit to be raised on CTRL_CLOSE_EVENT and CTRL_SHUTDOWN_EVENT
    (without such a handler, the process is terminated abruptly). This allows modules that trap SystemExit to work
    properly, as well as the atexit module.

    Of note, issuing SIGKILL on Unix systems (and the Windows equivalent command taskkill) cannot be caught by Python
    code and has a similar effect to a complete power failure of the system: there is no opportunity to handle this
    event. In a number of cases, both Windows and Unix will escalate requesting that a program terminate to issuing
    SIGKILL (after about 90 seconds for Linux, though this is configurable, and 5-20 seconds in Windows). For this
    reason, it is important to keep exit handlers short and limit the amount of time this package will delay before
    raising the appropriate exceptions to end the process.

    A typical use case might be as follows:

    from graceful_shutdown import ShutdownProtection

    with ShutdownProtection(4) as protected_block:
        # Let's loop on something
        while True:
            # Allow system shutdown at the start of the loop and renew our shutdown protection time
            protected_block.allow_break()
            try:
                # do something that shouldn't be interrupted
                pass
            except (SystemExit, KeyboardInterrupt) as ex:
                # our 4-second shutdown protection time has expired and the system is shutting down!
                # rollback our something
                # Make sure you re-raise the exception
                raise ex

    If the loop operation takes less than 4 seconds to complete, this will ensure that the operation completes
    successfully and then the system is shutdown. If it takes longer, then the exception is raised and will have to be
    handled appropriately.

    This project has not been tested in a multithreaded environment, use at your own risk in threads.

"""

import signal
import uuid
import time
import threading
import _thread
import os
import importlib.util
import logging
from autoinject import injector, CacheStrategy


class ShutdownImminentException(Exception):
    """ Raised if shutdown is imminent when a protected process tries to start. """
    pass


# Note that ShutdownManager must be a global object.
@injector.register("graceful_shutdown.manager.ShutdownManager", caching_strategy=CacheStrategy.GLOBAL_CACHE)
class ShutdownManager:
    """ Manages the registering of processes that might require specific break-points when shutting down.

        Note that this class is injectable as a thread-wide object; do not make instances of it, use the autoinject
        module to get an instance. This way it remains the only signal handler for the whole thread.
    """

    def __init__(self):
        """ Constructor """
        self._block_registry = {}
        self._attempts = 0
        self._kill_raised = False
        self._cow = None
        self._term_requested = False
        self.terminate_on_logoff = True
        self.terminate_on_hup = True
        self.default_max_exec_time = 4.5 if os.name == 'nt' else 89.5
        self.max_termination_attempts = 3
        self.before_termination = None
        self.hup_handler = None
        self.log = logging.getLogger("graceful_shutdown")
        self._register_signals()

    def _register_signals(self):
        """ Registers this instance as a handler for SIGINT, SIGTERM, SIGBREAK, and the Windows events """
        self.log.debug("Registering SIGINT and SIGTERM")
        signal.signal(signal.SIGINT, self._handle_posix_signal)
        signal.signal(signal.SIGTERM, self._handle_posix_signal)
        if os.name == 'posix':
            self.log.debug("Detected POSIX system, registering SIGQUIT and SIGHUP")
            signal.signal(signal.SIGQUIT, self._handle_posix_signal)
            signal.signal(signal.SIGHUP, self._handle_posix_signal)
        if os.name == 'nt':
            self.log.debug("Detected NT system, registering SIGBREAK")
            signal.signal(signal.SIGBREAK, self._handle_posix_signal)
            if importlib.util.find_spec("win32api"):
                self.log.debug("Win32API found, registering ConsoleCtrlHandler")
                import win32api
                win32api.SetConsoleCtrlHandler(self._handle_windows_signal, True)

    def register_block(self, max_exec_time=None, run_at_exit=False):
        """ Registers a protected block that will be allowed to continue to execute for at least `max_exec_time`
        seconds. """
        # Check if we are going to shut down soon and, if so, crash out.
        if self._attempts > 0 and not run_at_exit:
            raise ShutdownImminentException()
        key = str(uuid.uuid4())
        if max_exec_time is None or max_exec_time <= 0:
            max_exec_time = self.default_max_exec_time
        self._block_registry[key] = time.monotonic() + max_exec_time
        self.log.debug("Registered protected block {}".format(key))
        return key

    def renew_block(self, key, max_exec_time=None):
        """ Resets the max_exec_time for a given protected block """
        if max_exec_time is None or max_exec_time <= 0:
            max_exec_time = self.default_max_exec_time
        self._block_registry[key] = time.monotonic() + max_exec_time

    def unregister_block(self, key):
        """ Unregisters a protected block and checks if an exception should be raised. """
        self.log.debug("Unregistering protected block {}".format(key))
        if key in self._block_registry:
            del self._block_registry[key]
        if self._attempts > 0 and (not self._block_registry) and not self._kill_raised:
            self._raise_exception()

    def check_break(self):
        """ Checks if a break attempt has been seen """
        return self._attempts > 0

    def _raise_exception(self):
        """ Actually raises a SystemExit() or KeyboardInterrupt() exception as needed. """
        self._safe_log(logging.INFO, "Triggering system exit")
        self._kill_raised = True
        if self.before_termination:
            self.before_termination()
        if self._term_requested:
            raise SystemExit(1)
        else:
            raise KeyboardInterrupt()

    def _delayed_exit(self, max_exit_time):
        exit_time = time.monotonic() + max_exit_time - 0.5
        delay_time = self._graceful_exit(True)
        while delay_time > 0:
            self.log.info(f"Shutdown delayed for {delay_time}s")
            time.sleep(delay_time)
            delay_time = min(0.0, exit_time - time.monotonic(), self._graceful_exit(True))
        # If we're out of time and haven't raised an exception, then we will raise one
        self._safe_log(logging.INFO, "Maximum overall delay time exceeded, shutdown triggering")
        self._raise_exception()

    def _safe_log(self, level, message):
        """ Logging within the signal or interrupt handlers can sometimes cause errors if the operation that was
            interrupted was a logging call itself. This wraps calls to the logging system with an exception handler
            to ignore any errors while writing.
        """
        try:
            self.log.log(level, message)
        except RuntimeError as ex:
            pass

    def _handle_windows_signal(self, sig_type):
        """ Handles Windows events """
        import win32con
        self._attempts += 1
        if sig_type in (win32con.CTRL_CLOSE_EVENT, win32con.CTRL_SHUTDOWN_EVENT) or \
                (sig_type == win32con.CTRL_LOGOFF_EVENT and self.terminate_on_logoff):
            # How long we have to process is set by Windows itself; subtract 0.5 seconds to allow for SystemExit to
            # propagate and for time before this point
            exit_time = 5 if sig_type == win32con.CTRL_CLOSE_EVENT else 20
            self._safe_log(logging.INFO, f"Windows signal received, shutdown in at most {exit_time}s")
            self._delayed_exit(exit_time)
            return True
        return False

    def _handle_posix_signal(self, sig_num, frame):
        """ Handle typical Unix signals """
        if os.name == 'posix' and sig_num == signal.SIGHUP:
            if self.hup_handler:
                self.hup_handler()
            if not self.terminate_on_hup:
                return
        self._attempts += 1
        self._safe_log(logging.INFO, f"Received signal {sig_num}")
        delay_time = self._graceful_exit(sig_num != signal.SIGINT)
        if delay_time > 0:
            self._safe_log(logging.INFO, f"Shutdown delayed for {delay_time}s")
            self._cow = _InterruptingCow(delay_time)
            self._cow.start()
        else:
            # Won't reach here, but it's here as a fallback just in case something weird happens with the math
            self._raise_exception()

    def _graceful_exit(self, is_term):
        """ Check if it is time to exit, do so if necessary. If not, return the amount of time in seconds to wait
            before trying again. """
        if is_term or self._attempts > 1:
            self._term_requested = True
        kill_time = self._kill_time()
        curr_time = time.monotonic()
        if kill_time < curr_time:
            if kill_time > 0:
                self._safe_log(logging.INFO, "Maximum process delay time exceeded, shutdown initiated")
            self._raise_exception()
        return kill_time - curr_time

    def _kill_time(self):
        """ Check how long until we are allowed to exit. """
        if self._kill_raised:
            return -1
        if self._attempts >= self.max_termination_attempts:
            self._safe_log(logging.INFO, f"Maximum number of attempts ({self.max_termination_attempts}) reached, shutdown initiated")
            return -1
        if not self._block_registry:
            self._safe_log(logging.INFO, "No registered processed, shutdown initiated")
            return -1
        return max(self._block_registry.values())


class _InterruptingCow(threading.Thread):
    """ This thread simply interrupts the parent thread when the max_exec_time is exceeded """

    def __init__(self, max_exec_time=None):
        super().__init__()
        self.max_exec_time = max_exec_time
        self.daemon = True

    def run(self):
        """ Implements run() """
        time.sleep(self.max_exec_time)
        try:
            _thread.interrupt_main()
        except KeyboardInterrupt:
            pass


class HaltProtectedBlockException(Exception):
    """ Raised when shutdown is imminent but there are still protected blocks remaining """
    pass


class ShutdownProtection:
    """ Context manager for managing shutdown protection. Returns an instance of ProtectedBlock. """

    def __init__(self, max_exec_time=None, run_at_exit=False):
        """ Constructor """
        self.max_exec_time = max_exec_time
        self.run_at_exit = run_at_exit
        self._prot_block = None

    def __enter__(self):
        """ Implementation of __enter__() """
        self._prot_block = ProtectedBlock(self.max_exec_time, self.run_at_exit)
        self._prot_block.protect()
        return self._prot_block

    def __exit__(self, exc_type, exc_val, exc_tb):
        """ Implementation of __exit__()"""
        self._prot_block.unprotect()
        return exc_type == HaltProtectedBlockException


class ProtectedBlock:
    """ Provides controls for a protected block """

    manager: ShutdownManager = None

    @injector.construct
    def __init__(self, max_exec_time, run_at_exit):
        self.max_exec_time = max_exec_time
        self.run_at_exit = run_at_exit
        self._prot_key = None

    def protect(self):
        """ Protects the block from being interrupted. """
        self._prot_key = self.manager.register_block(self.max_exec_time, self.run_at_exit)

    def unprotect(self):
        """ Removes the block from protection"""
        if self._prot_key:
            self.manager.unregister_block(self._prot_key)
            self._prot_key = None

    def allow_break(self, renew=True):
        """ Allows the ShutdownManager to break the current block here, if necessary. This typically raises one of
            KeyboardInterrupt, SystemExit, or HaltProtectedBlockException. If execution can continue, renew() is called
            automatically.
        """
        if self.manager.check_break():
            # Doing this here will raise SystemExit or KeyboardInterrupt instead if necessary.
            self.unprotect()
            # Note that it will not raise it if there are 2+ processes blocking shutdown (e.g. asyncio is being used
            # and two coroutines have started a block with protection). In this case, HaltProtectedBlockException() is
            # raised instead.
            raise HaltProtectedBlockException()
        elif renew:
            self.renew()

    def renew(self, max_exec_time=None):
        """ Renews the amount of time this block is protected for """
        if not self._prot_key:
            raise ValueError("Block must be protected first")
        self.manager.renew_block(self._prot_key, max_exec_time if max_exec_time else self.max_exec_time)


@injector.inject
def configure_shutdown_manager(
        terminate_on_logoff=None,
        terminate_on_hup=None,
        max_exec_time=None,
        max_attempts=None,
        before_termination=None,
        before_hup=None,
        sm: ShutdownManager = None
):
    if terminate_on_logoff is not None:
        sm.terminate_on_logoff = terminate_on_logoff
    if terminate_on_hup is not None:
        sm.terminate_on_hup = terminate_on_hup
    if max_exec_time is not None:
        sm.default_max_exec_time = max_exec_time
    if max_attempts is not None:
        sm.max_termination_attempts = max_attempts
    if before_termination is not None:
        sm.before_termination = before_termination
    if before_hup is not None:
        sm.hup_handler = before_hup
