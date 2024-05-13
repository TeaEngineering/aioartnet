Install
-----

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


Useage
-----

https://github.com/TeaEngineering/aioartnet/blob/6aa67e1aa9f78924612395306978e31ab032321e/aioartnet/aio_artnet.py#L667-L691

