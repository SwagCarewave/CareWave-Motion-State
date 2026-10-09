create extension if not exists pgcrypto;

create type session_status as enum ('running', 'stopped');
create type rx_status as enum ('ok', 'weak', 'lost', 'none');
create type guardian_result as enum ('정상 활동', '도움 필요', '잘못된 감지');
create type file_status as enum ('uploading', 'uploaded', 'upload_failed');
create type analysis_status as enum ('queued', 'running', 'succeeded', 'failed');

create or replace function set_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

create table sessions (
    id uuid primary key default gen_random_uuid(),
    status session_status not null default 'running',
    started_at timestamptz not null default now(),
    stopped_at timestamptz,
    last_packet_at timestamptz,
    model_sha256 text not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table device_status (
    id uuid primary key default gen_random_uuid(),
    session_id uuid not null references sessions (id) on delete cascade,
    rx text not null check (rx in ('RX1', 'RX2', 'RX3')),
    status rx_status not null default 'none',
    packet_rate real not null default 0,
    last_packet_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (session_id, rx)
);

create table signal_frames (
    id uuid primary key default gen_random_uuid(),
    session_id uuid not null references sessions (id) on delete cascade,
    ts timestamptz not null,
    state text not null,
    motion_index real,
    activity_score real,
    candidate_sec real not null default 0,
    event_no integer,
    signal_ok boolean not null default true,
    packet_rate jsonb not null default '{}',
    heatmap real[],
    created_at timestamptz not null default now()
);

create index signal_frames_session_ts on signal_frames (session_id, ts);
create index signal_frames_ts on signal_frames (ts);

create table csv_files (
    id uuid primary key default gen_random_uuid(),
    filename text not null,
    storage_path text not null unique,
    size_bytes bigint not null check (size_bytes > 0 and size_bytes <= 52428800),
    sha256 text not null,
    status file_status not null default 'uploading',
    rows integer not null,
    packets integer not null,
    receivers text[] not null default '{}',
    packets_by_rx jsonb not null default '{}',
    record_start timestamptz not null,
    record_end timestamptz not null,
    error_code text,
    error_message text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index csv_files_created on csv_files (created_at desc);

create table analyses (
    id uuid primary key default gen_random_uuid(),
    file_id uuid not null references csv_files (id) on delete cascade,
    status analysis_status not null default 'queued',
    progress real not null default 0 check (progress between 0 and 1),
    result_path text,
    frame_count integer,
    event_count integer,
    summary jsonb,
    error_code text,
    error_message text,
    attempt integer not null default 1,
    started_at timestamptz,
    finished_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index analyses_file on analyses (file_id, created_at desc);

create table activity_events (
    id uuid primary key default gen_random_uuid(),
    session_id uuid references sessions (id) on delete cascade,
    analysis_id uuid references analyses (id) on delete cascade,
    event_no integer not null,
    started_at timestamptz not null,
    alerted_at timestamptz not null,
    ended_at timestamptz,
    duration_sec real not null default 0,
    alert_message text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    check ((session_id is null) <> (analysis_id is null)),
    unique nulls not distinct (session_id, analysis_id, event_no)
);

create index activity_events_started on activity_events (started_at desc);
create index activity_events_session on activity_events (session_id) where session_id is not null;
create index activity_events_analysis on activity_events (analysis_id) where analysis_id is not null;

create table guardian_confirmations (
    id uuid primary key default gen_random_uuid(),
    event_id uuid not null unique references activity_events (id) on delete cascade,
    result guardian_result not null,
    confirmed_at timestamptz not null default now(),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create trigger sessions_updated before update on sessions for each row execute function set_updated_at();
create trigger device_status_updated before update on device_status for each row execute function set_updated_at();
create trigger csv_files_updated before update on csv_files for each row execute function set_updated_at();
create trigger analyses_updated before update on analyses for each row execute function set_updated_at();
create trigger activity_events_updated before update on activity_events for each row execute function set_updated_at();
create trigger guardian_confirmations_updated before update on guardian_confirmations for each row execute function set_updated_at();

alter table sessions enable row level security;
alter table device_status enable row level security;
alter table signal_frames enable row level security;
alter table csv_files enable row level security;
alter table analyses enable row level security;
alter table activity_events enable row level security;
alter table guardian_confirmations enable row level security;

grant usage on schema public to service_role;
grant select, insert, update, delete on
    sessions, device_status, signal_frames, csv_files, analyses, activity_events, guardian_confirmations
    to service_role;

create or replace function purge_live_signal_frames(keep_seconds integer default 3600)
returns integer
language sql
as $$
    with gone as (
        delete from signal_frames
        where ts < now() - make_interval(secs => keep_seconds)
        returning 1
    )
    select count(*)::integer from gone;
$$;

grant execute on function purge_live_signal_frames(integer) to service_role;

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values
    ('csv-uploads', 'csv-uploads', false, 52428800, array['text/csv']),
    ('analysis-results', 'analysis-results', false, 52428800, array['application/gzip'])
on conflict (id) do nothing;

notify pgrst, 'reload schema';
