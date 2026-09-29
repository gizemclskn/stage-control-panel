\# Stage Control Panel



Android app that controls a simulated stage device (8 light channels, 8 audio channels) over a TCP socket.

The device is simulated by a Python server running on your computer.



Full documentation (architecture, protocol, setup) will be added in the final release.



\## Quick start (server)



&#x20;   python server/stage\_server.py



\## Run server tests



&#x20;   python -m unittest discover -s server -v

