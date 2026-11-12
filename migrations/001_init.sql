-- Initial schema: tenants, tasks, the transactional outbox and the consumer's inbox.

CREATE TABLE tenants (
    id          uuid        PRIMARY KEY,
    slug        text        NOT NULL,
    name        text        NOT NULL,
    status      text        NOT NULL,
    version     integer     NOT NULL,
    created_at  timestamptz NOT NULL,
    updated_at  timestamptz NOT NULL,
    -- Unique forever, including against destroyed tenants. This constraint, not a
    -- pre-check SELECT, decides the race between concurrent creates.
    CONSTRAINT tenants_slug_unique UNIQUE (slug),
    CONSTRAINT tenants_slug_format CHECK (slug ~ '^[a-z][-a-z0-9]{1,26}[a-z0-9]$'),
    CONSTRAINT tenants_name_not_blank CHECK (btrim(name) <> ''),
    CONSTRAINT tenants_status_valid CHECK (status IN
        ('provisioning', 'active', 'updating', 'destroying', 'destroyed', 'failed')),
    CONSTRAINT tenants_version_positive CHECK (version >= 1)
);

CREATE INDEX tenants_created_at_id ON tenants (created_at DESC, id DESC);

CREATE TABLE tasks (
    id          uuid        PRIMARY KEY,  -- doubles as the event id on the broker
    tenant_id   uuid        NOT NULL REFERENCES tenants (id),
    type        text        NOT NULL,
    status      text        NOT NULL,
    error       text,
    created_at  timestamptz NOT NULL,
    updated_at  timestamptz NOT NULL,
    CONSTRAINT tasks_type_valid CHECK (type IN ('deploy', 'update', 'destroy')),
    CONSTRAINT tasks_status_valid CHECK (status IN ('accepted', 'in_progress', 'done', 'failed'))
);

-- Backstop for "at most one open task per tenant". The tenant status guards already
-- guarantee it; this index makes a bug there fail loudly instead of silently.
CREATE UNIQUE INDEX tasks_one_open_per_tenant ON tasks (tenant_id)
    WHERE status IN ('accepted', 'in_progress');

CREATE INDEX tasks_created_at_id ON tasks (created_at DESC, id DESC);
CREATE INDEX tasks_tenant_created_at_id ON tasks (tenant_id, created_at DESC, id DESC);

-- Transactional outbox: written in the same transaction as the task, published by
-- the relay only after commit.
CREATE TABLE outbox (
    id            bigserial   PRIMARY KEY,
    event_id      uuid        NOT NULL UNIQUE,  -- = task id
    routing_key   text        NOT NULL,
    payload       jsonb       NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    published_at  timestamptz,
    attempts      integer     NOT NULL DEFAULT 0
);

CREATE INDEX outbox_unpublished ON outbox (id) WHERE published_at IS NULL;

-- Inbox: idempotency keys of worker updates already applied.
CREATE TABLE processed_messages (
    message_id    uuid        PRIMARY KEY,
    task_id       uuid        NOT NULL,
    processed_at  timestamptz NOT NULL DEFAULT now()
);
