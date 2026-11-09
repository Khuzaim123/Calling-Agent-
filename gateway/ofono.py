"""oFono HFP Hands-Free call control over the system D-Bus (dbus-fast).

oFono's hfp_hf plugin exposes a phone connected over Bluetooth HFP as a modem:
  org.ofono.Manager.GetModems()                    -> [(path, props)], path like /hfp/org/bluez/hci0/dev_AA_BB_...
  org.ofono.Modem.SetProperty("Powered", true)
  org.ofono.VoiceCallManager.Dial(number, "default") -> call path   (HFP ATD on the phone)
  org.ofono.VoiceCallManager signals CallAdded(path, props) / CallRemoved(path)
  org.ofono.VoiceCall.Answer() / Hangup()          (HFP ATA / AT+CHUP)
  org.ofono.VoiceCall signal PropertyChanged("State", v)  states: dialing alerting incoming waiting active held disconnected
STATUS: written against the documented oFono API; NOT yet run against the Redmi 9C.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

log = logging.getLogger("gateway.ofono")
SERVICE = "org.ofono"


@dataclass
class CallEvent:
    kind: str                 # added | state | removed | modem_lost | modem_ready
    path: str = ""
    state: str = ""
    number: str = ""


def _v(x):
    return getattr(x, "value", x)


class OfonoModem:
    def __init__(self, bt_address: str = ""):
        self.bt_address = bt_address.upper().replace(":", "_")
        self.events: asyncio.Queue[CallEvent] = asyncio.Queue()
        self.bus = None
        self.modem_path: str | None = None
        self.vcm = None
        self._calls: dict[str, object] = {}

    async def _iface(self, path: str, name: str):
        intro = await self.bus.introspect(SERVICE, path)
        return self.bus.get_proxy_object(SERVICE, path, intro).get_interface(name)

    async def start(self) -> None:
        from dbus_fast import BusType
        from dbus_fast.aio import MessageBus

        self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        manager = await self._iface("/", "org.ofono.Manager")
        manager.on_modem_added(lambda path, props: asyncio.ensure_future(self._maybe_attach(path, props)))
        manager.on_modem_removed(self._on_modem_removed)
        for path, props in await manager.call_get_modems():
            if await self._maybe_attach(path, props):
                break

    def _matches(self, path: str, props: dict) -> bool:
        if _v(props.get("Type")) not in (None, "hfp"):
            return False
        return not self.bt_address or self.bt_address in path.upper()

    async def _maybe_attach(self, path: str, props: dict) -> bool:
        if self.vcm is not None or not self._matches(path, props):
            return False
        from dbus_fast import Variant

        modem = await self._iface(path, "org.ofono.Modem")
        if not _v(props.get("Powered", False)):
            await modem.call_set_property("Powered", Variant("b", True))
        for _ in range(40):                                   # wait for VoiceCallManager to appear
            p = await modem.call_get_properties()
            if "org.ofono.VoiceCallManager" in _v(p.get("Interfaces", [])):
                break
            await asyncio.sleep(0.25)
        else:
            log.error("modem %s has no VoiceCallManager (phone connected but HFP not ready?)", path)
            return False
        self.modem_path = path
        self.vcm = await self._iface(path, "org.ofono.VoiceCallManager")
        self.vcm.on_call_added(lambda cpath, cprops: asyncio.ensure_future(self._watch(cpath, cprops)))
        self.vcm.on_call_removed(lambda cpath: self.events.put_nowait(CallEvent("removed", cpath)))
        for cpath, cprops in await self.vcm.call_get_calls():     # calls that existed before we started
            await self._watch(cpath, cprops)
        log.info("attached to oFono modem %s (%s)", path, _v(props.get("Name")))
        self.events.put_nowait(CallEvent("modem_ready", path))
        return True

    def _on_modem_removed(self, path: str) -> None:
        if path == self.modem_path:
            log.warning("phone disconnected (modem %s removed)", path)
            self.vcm, self.modem_path = None, None
            self._calls.clear()
            self.events.put_nowait(CallEvent("modem_lost", path))

    async def _watch(self, path: str, props: dict) -> None:
        call = await self._iface(path, "org.ofono.VoiceCall")
        self._calls[path] = call

        def changed(name, value):
            if name == "State":
                self.events.put_nowait(CallEvent("state", path, _v(value)))
        call.on_property_changed(changed)
        self.events.put_nowait(CallEvent("added", path, _v(props.get("State", "")),
                                         _v(props.get("LineIdentification", ""))))

    def ready(self) -> bool:
        return self.vcm is not None

    async def dial(self, number: str) -> str:
        if self.vcm is None:
            raise RuntimeError("phone not connected over HFP")
        return await self.vcm.call_dial(number, "default")

    async def answer(self, path: str) -> None:
        await self._calls[path].call_answer()

    async def hangup(self, path: str) -> None:
        call = self._calls.get(path)
        if call is not None:
            await call.call_hangup()

    def info(self) -> dict:
        return {"modem": self.modem_path, "ready": self.ready()}
