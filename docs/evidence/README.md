# Evidence

Manual runs against the real stack (`docker compose up --build -d --wait`), with the
exact commands and their output, so each behaviour can be reproduced by copy-paste.
They complement the automated test suite, which remains the primary proof.

| Page | Shows |
| --- | --- |
| [outbox-broker-outage.md](outbox-broker-outage.md) | Writes succeed while RabbitMQ is down; the event is published once it returns; nothing is lost |
| [rejected-writes.md](rejected-writes.md) | Duplicate slugs, stale versions and invalid transitions are refused with structured errors and publish nothing |
