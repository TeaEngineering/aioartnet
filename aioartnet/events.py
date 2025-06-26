from dataclasses import dataclass, field
from typing import Protocol

from .models import ArtNetNode, ArtNetPort, ArtNetUniverse


@dataclass
class ArtNetEvent(Protocol):
    text: str = field(init=False, repr=False)


@dataclass
class NodeDiscovered(ArtNetEvent):
    node: ArtNetNode
    text: str = field(init=False, repr=False, default="node-added")


@dataclass
class NodeLost(ArtNetEvent):
    node: ArtNetNode
    text: str = field(init=False, repr=False, default="node-removed")


@dataclass
class NodeChanged(ArtNetEvent):
    node: ArtNetNode
    text: str = field(init=False, repr=False, default="node-changed")


@dataclass
class NodePortAdded(ArtNetEvent):
    node: ArtNetNode
    port: ArtNetPort
    text: str = field(init=False, repr=False, default="node-port-changed")


@dataclass
class NodePortRemoved(ArtNetEvent):
    node: ArtNetNode
    port: ArtNetPort
    text: str = field(init=False, repr=False, default="node-port-changed")


@dataclass
class UniverseDiscovered(ArtNetEvent):
    universe: ArtNetUniverse
    text: str = field(init=False, repr=False, default="universe-added")


@dataclass
class UniverseDMX(ArtNetEvent):
    universe: ArtNetUniverse
    data: bytes
    text: str = field(init=False, repr=False, default="universe-dmx")
