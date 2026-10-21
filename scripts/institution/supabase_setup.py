"""Generate reviewed SQL Editor artifacts offline; never opens a connection."""

import argparse
from pathlib import Path

from scripts.institution.migrate import sql_bundle


def storage_sql():
    """Create only the two fixed private buckets; preserve existing buckets."""
    from app.institution_config import InstitutionSettings

    evidence, reports = (
        InstitutionSettings.PRIVATE_STORAGE_BUCKET,
        InstitutionSettings.REPORT_STORAGE_BUCKET,
    )
    return f"""-- Run manually in the SAME Supabase project after the migration bundle.
BEGIN;
INSERT INTO storage.buckets(id,name,public,file_size_limit,allowed_mime_types)
VALUES ('{evidence}','{evidence}',false,20971520,ARRAY['application/pdf','image/jpeg','image/png','text/csv','application/octet-stream']),
       ('{reports}','{reports}',false,20971520,ARRAY['application/pdf','text/csv','application/json','application/geo+json'])
ON CONFLICT (id) DO NOTHING;
DO $private_buckets$
BEGIN
 IF EXISTS(SELECT 1 FROM storage.buckets WHERE id IN ('{evidence}','{reports}') AND (public OR name<>id OR file_size_limit<20971520)) THEN
  RAISE EXCEPTION 'Institution buckets must be private, named by ID, and support 20 MiB. Review existing settings manually; no bucket was overwritten.';
 END IF;
END $private_buckets$;
-- Restrict only institution objects, even if existing farmer policies are broad.
DO $institution_storage_guard$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_policies WHERE schemaname='storage' AND tablename='objects' AND policyname='tt_institution_private_objects') THEN
  CREATE POLICY tt_institution_private_objects ON storage.objects
   AS RESTRICTIVE FOR ALL TO anon,authenticated
   USING (bucket_id NOT IN ('{evidence}','{reports}'))
   WITH CHECK (bucket_id NOT IN ('{evidence}','{reports}'));
 END IF;
END $institution_storage_guard$;
COMMIT;
SELECT id,public,file_size_limit,allowed_mime_types FROM storage.buckets WHERE id IN ('{evidence}','{reports}') ORDER BY id;
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=".institution-private/supabase")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Refresh the local generated bundle; never applies SQL.",
    )
    args = parser.parse_args()
    directory = Path(args.output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    for filename, source in (
        ("institution_migrations.sql", sql_bundle()),
        ("institution_storage.sql", storage_sql()),
        ("institution_demo_cleanup.sql", sql_bundle(min_version=16)),
    ):
        path = directory / filename
        with path.open(
            "w" if args.force else "x", encoding="utf-8", newline="\n"
        ) as output:
            output.write(source)
        print("OFFLINE SQL", path)


if __name__ == "__main__":
    main()
