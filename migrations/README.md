# Database Migrations

## For Existing Installations

If you already have a running installation and want to upgrade to the version with separate domain field:

### Apply Migration

```bash
# Connect to your running database
docker compose exec postgres psql -U postgres -d xui_central -f /migrations/001_add_domain_column.sql
```

Or from host:

```bash
psql -U postgres -h localhost -p 5432 -d xui_central -f migrations/001_add_domain_column.sql
```

### What This Does

1. Adds `domain` column to `nodes` table (nullable first)
2. Populates it by extracting hostname from existing `url` values
3. Makes the column NOT NULL after populating

### After Migration

You need to **manually verify and fix** the domain values:

```bash
docker compose exec postgres psql -U postgres -d xui_central
```

```sql
-- Check extracted domains
SELECT id, name, url, domain FROM nodes;

-- Fix any incorrect domains
UPDATE nodes SET domain = 'vienna.example.com' WHERE id = 1;
UPDATE nodes SET domain = 'london.example.com' WHERE id = 2;
```

## For Fresh Installations

Just run `docker compose up -d` - the schema is automatically created.
