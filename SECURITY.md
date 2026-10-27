# Security

## What power-watch holds

- The **Telegram bot token** and, optionally, the **purifier's device token**, in
  `/etc/power-watch.env` (mode 600, read by systemd as root).
- The service runs as a throwaway system user (`DynamicUser=yes`) with no access to your home
  directory, and only talks to your LAN and `api.telegram.org`.
- Neither token is ever logged: requests to Telegram carry the token in the URL, so no URL or
  request is ever written to the journal (a test checks this).

## Reporting a vulnerability

Please don't open a public issue. Use GitHub's
[private vulnerability reporting](https://github.com/levadadenys/power-watch/security/advisories/new)
for this repository, with the steps to reproduce. You'll get an answer within a week.
