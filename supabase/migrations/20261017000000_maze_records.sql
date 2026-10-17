create table if not exists maze_records (
    id uuid primary key default gen_random_uuid(),
    name text not null check (char_length(name) between 1 and 20),
    duration_sec integer not null check (duration_sec >= 0),
    created_at timestamptz not null default now()
);

create index if not exists maze_records_ranking on maze_records (duration_sec, created_at);

alter table maze_records enable row level security;

grant select, insert, update, delete on maze_records to service_role;

notify pgrst, 'reload schema';
