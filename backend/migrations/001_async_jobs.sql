-- Add background-job progress columns to an existing jobs table.
-- IF NOT EXISTS makes this migration safe to execute more than once.
ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS completed_urls INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS error TEXT;

-- Existing synchronous jobs were already complete before this migration.
UPDATE jobs
SET completed_urls = total_urls,
    finished_at = COALESCE(finished_at, created_at)
WHERE status = 'completed'
  AND completed_urls = 0;
