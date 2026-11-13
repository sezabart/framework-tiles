"""Runs before code.py: expose the console (REPL) and a binary data channel.

The tile uses the data channel so pixel bytes can never be mistaken for REPL
input (e.g. a 0x03 byte triggering Ctrl-C).
"""

import usb_cdc

usb_cdc.enable(console=True, data=True)
