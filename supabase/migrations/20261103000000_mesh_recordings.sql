alter table csv_files add column if not exists kind text not null default 'csi4';

alter table csv_files drop constraint if exists csv_files_kind_check;
alter table csv_files add constraint csv_files_kind_check check (kind in ('csi4', 'mesh6'));

alter table csv_files drop constraint if exists csv_files_size_bytes_check;
alter table csv_files add constraint csv_files_size_bytes_check
    check (size_bytes > 0 and size_bytes <= case when kind = 'mesh6' then 314572800 else 52428800 end);

notify pgrst, 'reload schema';
