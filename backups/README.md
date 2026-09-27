Where to keep snapshots of `~/.config/zero2w-console/flows.json`, taken before
anything that writes flows. `*.json` here is ignored by git: they are your
flows, so they stay on this machine and out of anything you share.

The document carries a `rev` and the server refuses a stale save with 409, so
a stale browser tab cannot overwrite newer work. A restore deliberately posts
**no** `rev`, which the server trusts — that is what lets the command below
overwrite whatever is there.

Restore one with:

    curl -H "X-Console-Token: $(cat ~/.config/zero2w-console/token)" \
         -H 'Content-Type: application/json' -X POST \
         http://localhost:8787/api/flows --data-binary @flows-XXXX.json

An archive of retired flows (a JSON list, each marked with the snapshot it
came from) is not a document to POST whole: copy the flow you want into the
studio.

A snapshot of the device store (`iot-*.json`) must have its tokens removed: a
backup is not a place to keep a credential.

Keep one snapshot per change worth going back to, not one per write.
