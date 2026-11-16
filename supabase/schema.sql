-- AgentLens mock loan pipeline: Supabase schema (free tier is fine).
-- Paste this whole file into Supabase -> SQL Editor -> New query -> Run.
-- Safe to run again: it only creates what's missing.
--
-- Privacy design:
--   * applications      : loan details only. No name, DOB, address or account number.
--   * customer_identity : identity + the customer's free-text message, ENCRYPTED in
--                         Python (Fernet) before they get here. The key lives in your
--                         .env, never in Supabase, so the database only ever holds ciphertext.
--   * documents         : payslip / statement text, encrypted the same way.
--   * mock_external_data: stand-ins for outside services (credit bureau, the customer's
--                         chat replies). Not personal data.
--   * handoffs          : what each agent passes to the next (token + facts only).
--   * event_log         : what AgentLens reads. One row per agent step, incl. every
--                         ticket issued and every refused access (rule DP4).
--   * customer_tracker  : customer-facing status.
-- Row Level Security is ON with no policies, so the public (anon) key can read or
-- write nothing. Only the server, using the secret / service_role key, can.

create table if not exists public.applications (
    case_id               text primary key,
    customer_token        text not null unique,
    -- submitted -> verifying -> deciding -> approved / declined / referred. 'held' = only a human can change it.
    status                text not null default 'submitted'
                          check (status in ('submitted', 'verifying', 'deciding', 'approved', 'declined',
                                            'referred', 'held', 'error')),
    submitted_at          timestamptz not null default now(),
    -- application form (not personal)
    stated_annual_income  numeric not null,
    requested_amount      numeric not null,
    term_years            integer,          -- optional on the form; the intake AI asks if it's missing
    purpose               text not null,
    employment            text,
    monthly_expenses      numeric,
    credit_card_limit     numeric,
    has_hecs              boolean not null default false,
    existing_customer     boolean not null default true,
    residency             text not null default 'AU',
    household             text not null default 'single',
    dependants            integer not null default 0,
    -- outcome (written by the pipeline)
    decision              text,
    decision_confidence   integer,
    decided_at            timestamptz
);

create table if not exists public.customer_identity (
    customer_token              text primary key,
    case_id                     text not null references public.applications (case_id) on delete cascade,
    identity_encrypted          text not null,   -- Fernet: {"name", "date_of_birth", "address", "account_number", "employer"}
    customer_message_encrypted  text not null,   -- Fernet: the free-text message from the form
    created_at                  timestamptz not null default now()
);

create table if not exists public.documents (
    case_id            text not null references public.applications (case_id) on delete cascade,
    doc_type           text not null check (doc_type in ('payslip', 'bank_statement')),
    content_encrypted  text not null,            -- Fernet: the document text
    created_at         timestamptz not null default now(),
    primary key (case_id, doc_type)
);

create table if not exists public.mock_external_data (
    case_id           text primary key references public.applications (case_id) on delete cascade,
    credit_report     jsonb not null,             -- what the mock credit bureau returns
    customer_replies  jsonb not null default '[]' -- scripted answers if the intake AI asks a question
);

-- What one agent passes to the next: token + facts only, never identity.
create table if not exists public.handoffs (
    case_id     text not null references public.applications (case_id) on delete cascade,
    stage       text not null check (stage in ('intake', 'verification', 'decision')),
    data        jsonb not null,
    created_at  timestamptz not null default now(),
    primary key (case_id, stage)
);

create table if not exists public.event_log (
    event_id     bigint generated always as identity primary key,
    case_id      text not null,
    agent_name   text not null,
    step_name    text not null,
    "timestamp"  timestamptz not null,
    input_data   jsonb not null,
    output_data  jsonb not null,
    result       text not null check (result in ('pass', 'fail', 'flag')),
    notes        text
);
create index if not exists idx_event_log_case on public.event_log (case_id, event_id);

create table if not exists public.customer_tracker (
    case_id       text primary key,
    current_step  text not null,
    status        text not null,
    last_updated  timestamptz not null,
    message       text
);

-- Speed: the server reserves event IDs from this sequence in blocks and writes
-- events in batches, so event_id must accept the values it supplies.
alter table public.event_log alter column event_id set generated by default;

create or replace function public.reserve_event_ids(n integer)
returns setof bigint
language sql
as $$
    select nextval(pg_get_serial_sequence('public.event_log', 'event_id'))
    from generate_series(1, greatest(n, 1));
$$;
revoke execute on function public.reserve_event_ids(integer) from public, anon, authenticated;

-- ===========================================================================
-- AgentLens: its own tables. It READS event_log and WRITES only these.
-- ===========================================================================

create table if not exists public.agent_licence (
    agent_name         text primary key,
    trust_score        integer not null default 100,
    level              text not null default 'FULL'
                       check (level in ('SUSPENDED', 'L', 'P1', 'P2', 'FULL')),
    cases_at_level     integer not null default 0,
    last_level_change  timestamptz,
    risk_owner         text not null default 'unassigned',
    fast_track         boolean not null default false,   -- after a verified fix: 10 cases to promote
    recent_cases       jsonb not null default '[]'        -- last 50 case_ids handled (mistake_rate window)
);

create table if not exists public.mistake_log (
    id                         bigint generated always as identity primary key,
    agent_name                 text,
    case_id                    text,
    event_id                   bigint,
    mistake_type               text check (mistake_type in ('MINOR', 'MAJOR', 'CRITICAL')),
    mistake_reason             text,     -- plain-English why this is wrong
    agent_stated_reasoning     text,     -- from that event's notes / output_data
    what_should_have_happened  text,
    "timestamp"                timestamptz default now(),
    rule                       text,     -- which detection rule fired
    pattern_key                text,     -- groups the same cause, e.g. misreads_field:pay_frequency
    details                    jsonb not null default '{}',
    resolved                   boolean not null default false
);
create index if not exists idx_mistake_agent on public.mistake_log (agent_name, pattern_key);

create table if not exists public.licence_history (
    id            bigint generated always as identity primary key,
    agent_name    text,
    case_id       text,
    event_id      bigint,
    score_change  integer,
    reason        text,
    new_score     integer,
    new_level     text,
    "timestamp"   timestamptz default now()
);

create table if not exists public.improvement_suggestion (
    id                   bigint generated always as identity primary key,
    agent_name           text,
    pattern_description  text,
    root_cause           text,
    suggested_fix        text,
    status               text not null default 'proposed'
                         check (status in ('proposed', 'applied', 'testing', 'resolved', 'rejected')),
    iteration            integer not null default 1,
    created_at           timestamptz default now(),
    pattern_key          text,
    case_ids             jsonb not null default '[]',
    parent_id            bigint,
    applied_by           text,
    fix_description      text,
    verify_result        jsonb
);

create table if not exists public.agentlens_state (
    key    text primary key,
    value  jsonb
);

alter table public.applications           enable row level security;
alter table public.customer_identity      enable row level security;
alter table public.documents              enable row level security;
alter table public.mock_external_data     enable row level security;
alter table public.handoffs               enable row level security;
alter table public.event_log              enable row level security;
alter table public.customer_tracker       enable row level security;
alter table public.agent_licence          enable row level security;
alter table public.mistake_log            enable row level security;
alter table public.licence_history        enable row level security;
alter table public.improvement_suggestion enable row level security;
alter table public.agentlens_state        enable row level security;
