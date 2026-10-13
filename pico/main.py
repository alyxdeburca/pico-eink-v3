"""Boot entry point.

Renders whatever was last persisted (display.txt or frame.raw), then
serves display commands from the PC over USB serial. Ctrl+C or the
EXIT command drops back to the REPL.
"""

import server

server.serve()
