
![GitHub Actions Workflow Status](https://img.shields.io/github/actions/workflow/status/TeaEngineering/aioartnet/check.yml) ![PyPI version](https://badge.fury.io/py/aioartnet.svg)


**aioartnet** is a pure python asyncio connector for the [royalty-free Art-Net protocol](https://art-net.org.uk/background/), which is a transport to transmit and recieve the [DMX-512 lighting control protocol](https://en.wikipedia.org/wiki/DMX512) over Ethernet (UDP). The protocol is the modern standard for interconnecting smart lighting fixtures to both open and propriety lighting control systems.

This library aims to be simple and robust, and can both input data into Art-Net, as well as output it from "artnet" to your user code. It builds a dynamic model of the network's Art-Net nodes, their ports and the universe(s) of DMX-512 that are being controlled.

It can also be used __passively__ to build the network model without joining as an Art-Net Node.

Install
-----

Use `pip` to install [the package from pypi](https://pypi.org/project/aioartnet/):

    $ pip install aioartnet
    $ python -m aioartnet.main
    INFO:aioartnet:preferred interfaces: [(1, 'wlp4s0'), (10, 'br-ee82b9af434e'), (10, 'docker0'), (10, 'lo')]
    INFO:aioartnet:using interface wlp4s0 with ip 192.168.1.205 broadcast ip 192.168.1.255
    INFO:aioartnet:configured own port Port<Input,DMX,0:0:1>
    INFO:aioartnet:configured own port Port<Output,DMX,0:0:5>
    status:
      ArtNetNode<aioartnet,192.168.1.205:6454>                     [Port<Input,DMX,0:0:1>, Port<Output,DMX,0:0:5>]
      ArtNetNode<ODE Mk3,192.168.1.238:6454>                       [Port<Output,DMX,0:0:0>, Port<Output,DMX,0:0:1>]
      ArtNetNode<DMX Monitor,192.168.1.222:6454>                   []
     0:0:1 pubs:[ArtNetNode<aioartnet,192.168.1.205:6454>] subs:[ArtNetNode<ODE Mk3,192.168.1.238:6454>]
     0:0:5 pubs:[] subs:[ArtNetNode<aioartnet,192.168.1.205:6454>]
     0:0:0 pubs:[] subs:[ArtNetNode<ODE Mk3,192.168.1.238:6454>]


Getting Started
-----

https://github.com/TeaEngineering/aioartnet/blob/6aa67e1aa9f78924612395306978e31ab032321e/aioartnet/aio_artnet.py#L667-L691


Features
----

| Message                            | Recieve             | Transmit           |
|------------------------------------|---------------------|--------------------|
| 15-bit port addresses              | :heavy_check_mark:  | :heavy_check_mark: |
| >4 ports (bindIndex)               | :heavy_check_mark:  | :heavy_check_mark: |
| dynamic reconfigure from software  | :heavy_check_mark:  | :heavy_check_mark: |
| merge-mode (LTP/HTP) in reciever   | -                   | -                  |
| RDM commands                       | -                   | -                  |


Implemented Messages
-----

| Message                            | Recieve             | Transmit           |
|------------------------------------|---------------------|--------------------|
| ArtPoll                            | :heavy_check_mark:  | :heavy_check_mark: |
| ArtPollReply                       | :heavy_check_mark:  | :heavy_check_mark: |
| ArtDMX                             | :heavy_check_mark:  | :heavy_check_mark: |
| ArtIpProg / ArtIpProgReply         | -                   | -                  |
| ArtAddress                         | -                   | -                  |
| ArtDataRequest / ArtDataReply      | -                   | -                  |
| ArtDiagData                        | -                   | -                  |
| ArtTimeCode                        | -                   | -                  |
| ArtCommand                         | -                   | -                  |
| ArtTrigger                         | -                   | -                  |
| ArtSync                            | -                   | -                  |
| RDM ArtTodRequest / ArtTodData / ArtTodControl / ArtRdm / ArtRdmSub /



