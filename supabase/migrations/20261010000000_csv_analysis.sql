alter table csv_files add column if not exists incomplete_rows integer not null default 0;

create index if not exists csv_files_status on csv_files (status, created_at desc);
create index if not exists analyses_status on analyses (status);

notify pgrst, 'reload schema';
