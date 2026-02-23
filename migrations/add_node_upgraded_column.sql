-- Add 'upgraded' column to nodes table
-- This indicates if a node has synced clients across inbounds (uses HA ports)

-- Check if column already exists and add it if not
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_name='nodes' AND column_name='upgraded'
    ) THEN
        ALTER TABLE nodes ADD COLUMN upgraded BOOLEAN DEFAULT FALSE;
        RAISE NOTICE '✓ Column "upgraded" added successfully';
    ELSE
        RAISE NOTICE '✓ Column "upgraded" already exists';
    END IF;
END $$;

-- Show current nodes
SELECT id, name, enabled, upgraded FROM nodes ORDER BY id;
