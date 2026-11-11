#!/bin/zsh
cd -- "${0:A:h}"
if command -v python3 >/dev/null 2>&1; then
  python3 server.py --open
else
  print 'Coursework needs Python 3.10 or later. Install it from python.org, then open this file again.'
  read '?Press Return to close.'
fi
