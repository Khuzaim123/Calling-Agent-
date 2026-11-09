"""Redmi 9C GSM gateway over Bluetooth Hands-Free (HFP).

Runs on a small Linux box (e.g. Raspberry Pi) paired with the phone. The phone
keeps doing what it does natively (SIM, GSM, dialing, ringing, caller ID, call
audio over HFP); this package turns HFP call control (oFono, D-Bus) and HFP
call audio (PipeWire) into the voice engine's existing WebSocket protocol.
No app on the phone, no root.
"""

__version__ = "0.1.0"
