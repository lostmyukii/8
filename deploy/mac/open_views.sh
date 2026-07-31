#!/usr/bin/env bash
set -Eeuo pipefail

open "http://127.0.0.1:8000/"
open "http://127.0.0.1:1234/index.html"
open "http://127.0.0.1:6080/vnc.html?autoconnect=1&resize=remote"
