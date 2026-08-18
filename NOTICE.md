# Upstream Notice

This project is a companion notifier and routing integration for
[cc-connect](https://github.com/chenhg5/cc-connect). The custom Weixin routing
changes are maintained in the `quote-router` branch of the author's cc-connect
fork and are based on upstream `v1.4.1`.

The notifier itself is maintained separately so upstream cc-connect can be
updated without copying its entire source tree into this repository. Combined
GitHub Releases build the pinned fork commit in CI and include its provenance,
checksum, and third-party notice without copying the upstream source tree into
this repository.
