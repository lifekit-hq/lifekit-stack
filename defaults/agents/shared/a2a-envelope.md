<!-- lifekit:a2a-envelope:begin -->
## Agent-to-agent messages: envelope and receipt

Every `sessions_send` message to another agent is one JSON envelope, and every request or question is answered with a receipt before any work. The rule is the vault build standard "Envelope and receipt at every door" (2026-11-16); this block only carries it into your turns.

1. Send `{"a2a":1,"thread","from","to","type","turn","reply_expected","deadline","origin","owner","class","fingerprint","acceptance","body"}`. `class` is dev, ops, finance, life or unclear; `acceptance` is the done-when; `fingerprint` is the `thread` id unless the item says otherwise. A `type` that carries a status uses the A2A state names (submitted, working, input-required, auth-required, completed, failed, canceled, rejected) and no others.
2. When you receive a `request` or `question`, send one `type: ack` first, before any work or reply, carrying `thread`, `state` (`submitted` or `rejected`) and where the item now lives. Reject, naming the missing field, an envelope that lacks one.
3. A `thread` that is already open gets no second item: append to it and ack with `state: working`.
4. The ack is the receipt. Store it and never re-send the message. An ack that does not arrive is a failure to file under "Development-class problems", not a reason to ask again.
5. This holds on every turn, including scheduled ones.
<!-- lifekit:a2a-envelope:end -->
