from dataclasses import dataclass
from typing import Sequence

# Can be subclassed to mock responses to more complex RDM queries. Built in has:
#   RDM uuid
#   DMX base address
#   DMX profile name
#   DMX channel count


@dataclass
class RDMDevice:
    def __init__(self, uuid: bytes):
        self.uuid = uuid
        self.dmx_base_address = 0
        self.dmx_profile_name = ""
        self.dmx_profile_width = 1


# the RDM interrogator is installed into the ArtNetClient, since it might need to be configurable
# by default, we get the device uuids and ignore them. We also have a standard one that will fetch
# the base address, profile name and channel count of any discovered fixtures, and dispatch events
# for them.
class RDMInterrogator:
    def on_uuids(self, uuids: Sequence[bytes]) -> None:
        self.uuids = uuids

    def poll(self) -> None:
        # do nothing
        pass

    def on_rdm_response(self, data: bytes) -> None:
        pass


class RDMResponder:
    def __init__(self) -> None:
        self.devices: list[RDMDevice] = []

    def get_tod_uuids(self) -> Sequence[RDMDevice]:
        return self.devices

    def answer_rdm(self, data: bytes) -> bytes:
        return bytes()
