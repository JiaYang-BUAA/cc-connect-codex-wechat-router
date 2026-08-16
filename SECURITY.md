# Security

The router is intended for one Windows user on one machine.

- Keep `router_host` on loopback (`127.0.0.1`, `localhost`, or `::1`).
- Use a random token of at least 32 bytes and configure the same token in
  `cc-connect`'s Weixin platform options.
- Never commit `config.json`, `data/`, logs, database paths, or tokens.
- Do not expose the router port through a firewall, reverse proxy, tunnel, or
  LAN binding.

To report a security issue, do not include credentials or private Codex
transcripts. Contact the repository owner privately through GitHub.
