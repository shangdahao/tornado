#!/usr/bin/env python3
# Foundations of Python Network Programming, Third Edition
# https://github.com/brandon-rhodes/fopnp/blob/m/py3/chapter01/search4.py

"""

The particular protocol stack that you have just explored is four protocols high.

On top is the Google Geocoding API, which tells you how to express your geographic queries as URLs that fetch JSON data containing coordinates.
URLs name documents that can be retrieved using HTTP.
HTTP supports document-oriented commands such as GET using raw TCP/IP sockets.
TCP/IP sockets know how only to send and receive byte strings.

"""

import socket
from urllib.parse import quote_plus

# newline in Telnet should always be '\r\n' but most implementations have either not been updated,
# or keeps the old '\n\r' for backwards compatibility.
request_text = """\
GET /maps/api/geocode/json?address={}&sensor=false HTTP/1.1\r\n\
Host: maps.google.com:80\r\n\
User-Agent: search4.py (Foundations of Python Network Programming)\r\n\
Connection: close\r\n\
\r\n\
"""


def geocode(address):
    sock = socket.socket()
    sock.connect(('maps.google.com', 80))
    request = request_text.format(quote_plus(address))
    sock.sendall(request.encode('ascii'))
    raw_reply = b''
    while True:
        more = sock.recv(4096)
        if not more:
            break
        raw_reply += more
    print(raw_reply.decode('utf-8'))

if __name__ == '__main__':
    geocode('207 N. Defiance St, Archbold, OH')