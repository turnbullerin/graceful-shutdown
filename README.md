# Graceful Shutdown

This package provides a context manager which traps signals that would normally cause the program to exit and delay
exiting until (a) a suitable breakpoint is reached, (b) the context manager exits, or (c) the maximum execution time is
reached. In addition, it causes programs run on Windows using python.exe (but not pythonw.exe) to raise `SystemExit`
exceptions when the window is closed or the system is shutdown.

## Usage

A typical use case is as follows:

```python
from graceful_shutdown import ShutdownProtection

with ShutdownProtection(4) as protected_block:
  
  # BLOCK 1
  try:
      # do some stuff you don't want interrupted
  except (SystemExit, KeyboardInterrupt) as ex:
      # rollback as needed
      # only called if there is a timeout in the block

  protected_block.allow_break()

  # BLOCK 2
  try:
      # do some other stuff
  except (SystemExit, KeyboardInterrupt) as ex:
      # rollback as needed
```

In this example, if the Python process is requested to exit via `SIGINT`, `SIGTERM`, `SIGQUIT`, `SIGHUP`, `SIGBREAK`, 
`CTRL_C_EVENT`, `CTRL_BREAK_EVENT`, `CTRL_CLOSE_EVENT`, `CTRL_LOGOFF_EVENT`, or `CTRL_SHUTDOWN_EVENT`, the blocks of 
code before and after `allow_break()` are guaranteed to have at least 4 seconds of execution time before an exception is 
raised. This time is tracked from when the context manager is initialized or from the most recent call to 
`allow_break()` or `renew()` on the context object.

It is recommended to set the execution time to no more than 4.5 seconds as Windows typically allows only 5 seconds for
shutdown routines to execute after `CTRL_CLOSE_EVENT` is fired (when the console window is closed). If you know that you
will be in a Unix environment, you can extend this up to your Unix system's time between issuing `SIGTERM` and `SIGKILL`
when a shutdown is issued (typically 90 seconds). If you are using a tool such as NSSM on Windows, the interval between
`CTRL-C` and taskkill being called will vary, so consider your configuration carefully. The default execution time is 
4.5 seconds on Windows and 89.5 seconds on other systems which should be suitable for most use cases.

If you do not want to use `ShutdownProtection()` but you do want `CTRL_CLOSE_EVENT`, `CTRL_SHUTDOWN_EVENT`, and 
`CTRL_LOGOFF_EVENT` to raise `SystemExit` instead of abruptly ending the process, call `configure_shutdown_manager()`
before starting your program:

```python
from graceful_shutdown import configure_shutdown_manager

configure_shutdown_manager()
# do your work here
```

In addition, if you want to use `ShutdownProtection()` inside a thread, it is important to configure the shutdown 
manager in the main thread so that it can register the signal handlers properly:

```python
from graceful_shutdown import configure_shutdown_manager, ShutdownProtection
import threading
import time


class ProtectedThread(threading.Thread):

    def run(self):
        with ShutdownProtection() as protected_block:
            while True:
                protected_block.allow_break()
                # do something protected here
                time.sleep(0.1)
                
# Call this first, otherwise signal registration won't happen properly
configure_shutdown_manager()

# Create, start, and then join our thread
t = ProtectedThread()
t.daemon = True  # Ensures the thread exits when the main thread exits
t.start()
t.join()
# Now if you SIGINT the thread, the main program will wait for the current loop of the thread to finish before exiting.

```

## Configuration
`configure_shutdown_manager()` takes several arguments that can be used to configure the behaviour. The default value of
None for each will be ignored (the previously set value will be kept).

* `terminate_on_logoff`: If set to `False`, `CTRL_LOGOFF_EVENT` will be ignored (defaults to `True`)
* `terminate_on_hup`: If set to `False`, `SIGHUP` will be ignored (defaults to `True`)
* `max_exec_time`: Overrides the default value for `max_exec_time` (4.5 seconds on Windows, 89.5 on Unix)
* `max_attempts`: Sets the maximum number of signals or events to handle before interrupting the running process 
  (defaults to 3). With the default of 3, entering CTRL-C three times will immediately halt the program with a 
  SystemExit exception; fewer attempts will allow the program to attempt to complete.
* `before_termination`: If specified, this function will be called immediately prior to raising `SystemExit` or 
  `KeyboardInterrupt`. This function should be quick to run as any significant delays may lead to `SIGKILL`. Set this to
  `False` or another non-truthy value other than `None` to remove the previously set callback.
* `before_hup`: If specified, this function will be called immediately after receiving SIGHUP. It ignores whether
  `terminate_on_hup` is set; if it is True, then the system will proceed to exit after this function is called.

## Cautionary Note

While this package can help you handle shutdown events gracefully, there are two caveats:

1. Your shutdown code must complete within the execution window, otherwise many systems progress to `SIGKILL`. You can
   avoid this by limiting actions to the minimal set of actions necessary for stability.
2. This does not (and can not) protect against `SIGKILL` (i.e. kill -9 or taskkill), or abrupt power failure. This is 
   not a substitute for a UPS. You should design your code and your hardware setup with this in mind, and only `SIGKILL`
   when truly necessary and if data loss is not a problem.

While I'm actively using this project, there are limited tests for it at the moment. Eventually I will work on the 
proper test cases.

## Events Handled

This package handles the typical POSIX signals for requesting a program end: `SIGINT` (`CTRL-C`), `SIGTERM`, `SIGQUIT`, 
and `SIGHUP`. Note that `SIGHUP` is optional as it is sometimes used to reload configuration instead; you can override 
the termination behaviour by setting `terminate_on_hup = False` and an appropriate handler for `before_hup`. If these 
signals are sent on Windows by using something like NSSM, `SIGINT` and `SIGTERM` will be handled (the others are not
supported on Windows). 

On Windows, `SIGBREAK` is also handled to respond to `CTRL-BREAK`. In addition, the Windows console events of 
`CTRL_CLOSE_EVENT`, `CTRL_SHUTDOWN_EVENT`, and `CTRL_LOGOFF_EVENT` are handled. These only work if you have the pywin32
package installed, and you are running Python using `python.exe`; the console-less `pythonw.exe` cannot receive these
signals. You can override how `CTRL_LOGOFF_EVENT` is handled by setting `terminate_on_logoff = False` to keep the 
process running at logoff (useful for services running in NSSM).


