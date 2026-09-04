create table public.profiles (
  id uuid primary key,
  display_name text not null
);

alter table public.profiles enable row level security;
