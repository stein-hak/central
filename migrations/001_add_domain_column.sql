-- Migration: Add domain column to nodes table
-- This handles existing installations

-- Step 1: Add domain column as nullable first
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS domain VARCHAR(255);

-- Step 2: For existing nodes, extract domain from URL
-- This attempts to parse the hostname from the URL
UPDATE nodes
SET domain = CASE
    WHEN url ~ '^https?://([^:/]+)' THEN
        substring(url from '^https?://([^:/]+)')
    ELSE
        name || '.example.com'
END
WHERE domain IS NULL;

-- Step 3: Now make it NOT NULL
ALTER TABLE nodes ALTER COLUMN domain SET NOT NULL;

-- Verify
SELECT id, name, url, domain FROM nodes;
