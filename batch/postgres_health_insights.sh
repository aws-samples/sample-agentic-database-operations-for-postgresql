#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Force UTF-8 encoding for proper emoji/unicode display
export LANG=en_US.UTF-8
export LC_ALL=en_US.UTF-8


# ============================================================================
# DB Ops Agent - PostgreSQL Health Insights
# ============================================================================
#
# DESCRIPTION:
#   Read-only PostgreSQL health analysis tool that generates an HTML report
#   covering 9 sections: connections, configuration, activity, replication,
#   storage, performance, maintenance, optimization, and summary.
#
# DEPLOYMENT SUPPORT:
#   Auto-detects and adapts analysis for:
#   • EC2 / On-Prem / Self-Managed (full parameter control)
#   • Amazon RDS for PostgreSQL (parameter group tunable)
#   • Amazon Aurora PostgreSQL (storage-decoupled, managed)
#   • Aurora Serverless v2 (ACU-scaled, tunable vs managed tagging)
#
# KEY CAPABILITIES:
#   • Deployment-aware parameter matrix with tuning priorities
#   • Query performance analysis with version-specific SQL (PG 11-18)
#   • Security audit: roles, privileges, SSL, password encryption, RLS
#   • Vacuum/bloat health with stats-based and pgstattuple analysis
#   • XID wraparound monitoring and autovacuum effectiveness
#   • Replication lag, WAL analysis, and lock contention detection
#   • Query execution plans with EXPLAIN fallback strategies
#
# REQUIREMENTS:
#   • PostgreSQL 11-18
#   • psql client
#   • bash 4.0+, bc
#   • Recommended extensions: pg_stat_statements, pg_buffercache, pgstattuple
#
# USAGE:
#   ./postgres_health_insights.sh
#
# OUTPUT:
#   HTML report: pg-health_<customer>_<database>_pg<major>_<deploy>_<timestamp>.html
#
# ============================================================================


echo "=============================================================================="
echo "            DB Ops Agent - PostgreSQL Health Insights"
echo "=============================================================================="
echo ""
echo "DISCLAIMER & SAFETY NOTICE:"
echo "- This tool performs READ-ONLY operations using SELECT statements only"
echo "- No data modifications, schema changes, or configuration updates are made"
echo "- Provided AS-IS without warranty - use at your own risk"
echo "- All recommendations must be validated in non-production before implementation"
echo "- The authors assume no responsibility for any consequences of using this tool"
echo ""
echo "SCOPE:"
echo "- Supported: PostgreSQL 11 through 18"
echo "- Deployments: EC2/On-Prem, Amazon RDS, Aurora (Provisioned & Serverless v2)"
echo "- Sections: Connections, Configuration, Activity, Replication, Storage,"
echo "            Performance, Maintenance, Optimization, Summary"
echo ""
echo "PREREQUISITES:"
echo "- EC2/On-Prem: DB user must have pg_monitor role"
echo "    GRANT pg_monitor TO <username>;"
echo "- RDS/Aurora: Use the master user (rds_superuser) - no additional grants needed"
echo "- Without pg_monitor, the report will run but with reduced visibility"
echo ""
echo "=============================================================================="
echo "Version: 3.0 | Last Updated: March 2026"
echo "=============================================================================="
echo ""

# Prompt user to accept disclaimer
# Support both CLI arguments (for batch mode) and interactive prompts
# Usage: ./postgres_health_insights.sh [customer] [host] [port] [database] [user] [password] [bloat_method]
# If no arguments provided, prompts interactively

if [ $# -ge 5 ]; then
    # Batch mode - arguments provided.
    # SECURITY: the password is NOT a positional argument — it is read from the
    # PGPASSWORD environment variable so it never appears in `ps`/process args.
    # The caller (run_scan.py) exports PGPASSWORD before invoking this script.
    CONTINUE="yes"
    CUSTOMER_NAME="$1"
    DB_HOST="$2"
    DB_PORT="${3:-5432}"
    DB_NAME="$4"
    DB_USER="$5"
    BLOAT_OPTION="${6:-1}"
    echo "Running in batch mode..."
else
    # Interactive mode - prompt for inputs
    read -p "Do you accept these terms? (yes/no): " CONTINUE
    if [[ "$CONTINUE" != "yes" ]]; then
        echo "You must accept the terms to proceed. Exiting."
        exit 1
    fi
    echo ""

    echo "PostgreSQL Health Check - Database Connection Setup"
    echo "=============================================="
    echo ""

    read -p "Customer Name: " CUSTOMER_NAME
    echo ""
    read -p "Database Host/IP: " DB_HOST
    read -p "Database Port [5432]: " DB_PORT
    DB_PORT=${DB_PORT:-5432}
    read -p "Database Name: " DB_NAME
    read -p "Username: " DB_USER
    read -s -p "Password: " PGPASSWORD
    echo ""
    echo ""
fi
# TLS for every psql call below. The password travels over these connections, and
# libpq's default (prefer) attempts TLS but silently falls back to plaintext when the
# server offers none and never checks the certificate. run_scan.py sets PGSSLMODE and
# PGSSLROOTCERT for the batch path (verify-full against the bundled RDS CA).
# A standalone run defaults to verify-full with the bundle shipped
# beside this script. No bundle and no PGSSLMODE is an error, not a silent downgrade: an
# operator who really wants encrypt-only sets PGSSLMODE=require explicitly.
if [ -z "${PGSSLMODE:-}" ]; then
    _bundle="${RDS_CA_BUNDLE:-$(dirname "$0")/certs/rds-global-bundle.pem}"
    if [ ! -f "$_bundle" ]; then
        echo "ERROR: RDS CA bundle not found at $_bundle and PGSSLMODE is unset." >&2
        echo "       Set RDS_CA_BUNDLE to the bundle, or PGSSLMODE explicitly." >&2
        exit 1
    fi
    export PGSSLMODE="verify-full" PGSSLROOTCERT="$_bundle"
fi

echo "Using connection parameters:"
echo "  Customer: $CUSTOMER_NAME"
echo "  Host: $DB_HOST"
echo "  Port: $DB_PORT"
echo "  Database: $DB_NAME"
echo "  User: $DB_USER"
echo "  TLS: sslmode=$PGSSLMODE"
echo ""

# Test database connection first
echo "Testing database connection..."
if ! PGPASSWORD="$PGPASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -c "SELECT 1;" >/dev/null 2>&1; then
    echo "❌ ERROR: Unable to connect to PostgreSQL database"
    echo "   Host: $DB_HOST:$DB_PORT"
    echo "   Database: $DB_NAME"
    echo "   User: $DB_USER"
    echo ""
    echo "Please verify:"
    echo "  - Database credentials are correct"
    echo "  - Database server is running and accessible"
    echo "  - Network connectivity to the database host"
    echo "  - Firewall rules allow connection on port $DB_PORT"
    echo ""
    echo "Script execution aborted."
    exit 1
fi
echo "✅ Database connection successful"
echo ""

# Capture start time and XID for report generation tracking
START_TIME=$(date '+%d-%b-%y %H:%M:%S')
START_EPOCH=$(date +%s)
START_XID=$(PGPASSWORD="$PGPASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -t -c "SELECT txid_snapshot_xmax(txid_current_snapshot());" 2>/dev/null | xargs)
echo "Report generation started at: $START_TIME (XID = $START_XID)"

# Check PostgreSQL extensions (optional but recommended)
echo "Checking PostgreSQL extensions..."

# List of recommended extensions for comprehensive health analysis
RECOMMENDED_EXTENSIONS=("pg_stat_statements" "pg_buffercache" "pgstattuple")
MISSING_EXTENSIONS=()
EXTENSION_WARNINGS=false

echo "Recommended extensions for comprehensive health report:"
for ext in "${RECOMMENDED_EXTENSIONS[@]}"; do
    echo "  - $ext"
done
echo ""

# Check if pg_stat_statements is enabled in shared_preload_libraries
echo "Checking shared_preload_libraries configuration..."
SHARED_PRELOAD=$(PGPASSWORD="$PGPASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -t -c "SELECT current_setting('shared_preload_libraries');" 2>/dev/null | xargs)
echo "Current shared_preload_libraries: $SHARED_PRELOAD"

PG_STAT_STATEMENTS_PRELOADED=false
if [[ "$SHARED_PRELOAD" == *"pg_stat_statements"* ]]; then
    echo "✅ pg_stat_statements is properly configured in shared_preload_libraries"
    PG_STAT_STATEMENTS_PRELOADED=true
else
    echo "⚠️  pg_stat_statements is not loaded in shared_preload_libraries"
    EXTENSION_WARNINGS=true
fi

echo ""
echo "Checking extension installation status..."

# Check each recommended extension
for ext in "${RECOMMENDED_EXTENSIONS[@]}"; do
    echo -n "Checking $ext... "
    INSTALLED=$(PGPASSWORD="$PGPASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -t -c "SELECT COUNT(*) FROM pg_extension WHERE extname = '$ext';" 2>/dev/null | xargs)
    
    if [ "$INSTALLED" = "1" ]; then
        echo "✅ INSTALLED"
        # Set extension availability flags
        case $ext in
            "pg_stat_statements") HAS_PG_STAT_STATEMENTS=true ;;
            "pg_buffercache") HAS_PG_BUFFERCACHE=true ;;
            "pgstattuple") HAS_PGSTATTUPLE=true ;;
        esac
    else
        echo "⚠️  MISSING"
        MISSING_EXTENSIONS+=("$ext")
        EXTENSION_WARNINGS=true
        # Set extension availability flags
        case $ext in
            "pg_stat_statements") HAS_PG_STAT_STATEMENTS=false ;;
            "pg_buffercache") HAS_PG_BUFFERCACHE=false ;;
            "pgstattuple") HAS_PGSTATTUPLE=false ;;
        esac
    fi
done

# Display warnings but continue execution
if [ "$EXTENSION_WARNINGS" = true ]; then
    echo ""
    echo "⚠️  EXTENSION COMPATIBILITY NOTICE"
    echo "=========================================="
    echo "Missing extensions (script will continue with limited analysis):"
    
    if [ "$PG_STAT_STATEMENTS_PRELOADED" = false ]; then
        echo "  - pg_stat_statements: Not preloaded - query performance metrics unavailable"
    fi
    
    for ext in "${MISSING_EXTENSIONS[@]}"; do
        case $ext in
            "pg_stat_statements") echo "  - pg_stat_statements: Query performance metrics unavailable" ;;
            "pg_buffercache") echo "  - pg_buffercache: Buffer cache analysis unavailable" ;;
            "pgstattuple") echo "  - pgstattuple: Table bloat analysis unavailable" ;;
        esac
    done
    
    echo ""
    echo "Impact on Report Sections:"
    echo "  ✅ Sections 1-4: Full analysis available"
    echo "  ⚠️  Section 5: Basic size info only"
    echo "  ⚠️  Section 6: Limited query & I/O metrics"
    echo "  ⚠️  Section 7: Basic vacuum & integrity info only"
    echo "  ⚠️  Section 8: Reduced optimization analysis"
    echo ""
    echo "To enable full functionality, install missing extensions:"
    for ext in "${MISSING_EXTENSIONS[@]}"; do
        echo "  CREATE EXTENSION IF NOT EXISTS $ext;"
    done
    if [ "$PG_STAT_STATEMENTS_PRELOADED" = false ]; then
        echo "  Add 'pg_stat_statements' to shared_preload_libraries and restart PostgreSQL"
    fi
    echo ""
    echo "RECOMMENDED: For complete query text capture in Query Execution Plans:"
    echo "  Set track_activity_query_size = 8192 in postgresql.conf (default: 1024)"
    echo "  This prevents query truncation for complex queries with CTEs/subqueries"
    echo "  Requires PostgreSQL restart to take effect"
    echo ""
    echo "Proceeding with available PostgreSQL system catalogs..."
else
    echo ""
    echo "✅ All recommended extensions are properly installed and configured."
    echo ""
    echo "RECOMMENDED: For complete query text capture in Query Execution Plans:"
    echo "  Set track_activity_query_size = 8192 in postgresql.conf (default: 1024)"
    echo "  This prevents query truncation for complex queries with CTEs/subqueries"
    echo "  Requires PostgreSQL restart to take effect"
    echo ""
    echo "Proceeding with comprehensive PostgreSQL health analysis..."
    HAS_PG_STAT_STATEMENTS=true
    HAS_PG_BUFFERCACHE=true
    HAS_PGSTATTUPLE=true
    HAS_PG_STAT_STATEMENTS_DATA=true
fi
echo ""

# Check track_activity_query_size setting
TRACK_ACTIVITY_QUERY_SIZE_RAW=$(PGPASSWORD="$PGPASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -t -c "SELECT current_setting('track_activity_query_size');" 2>/dev/null | xargs)
# Convert value with unit suffix (e.g. "4kB", "8kB") to bytes
TRACK_ACTIVITY_QUERY_SIZE=$(PGPASSWORD="$PGPASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -t -c "SELECT setting::integer * CASE unit WHEN '8kB' THEN 8192 WHEN 'kB' THEN 1024 WHEN 'B' THEN 1 ELSE 1 END FROM pg_settings WHERE name = 'track_activity_query_size';" 2>/dev/null | xargs)
echo "Checking query text capture configuration..."
echo "Current track_activity_query_size: $TRACK_ACTIVITY_QUERY_SIZE_RAW ($TRACK_ACTIVITY_QUERY_SIZE bytes)"

if [ -n "$TRACK_ACTIVITY_QUERY_SIZE" ] && [ "$TRACK_ACTIVITY_QUERY_SIZE" -lt 4096 ]; then
    echo "⚠️  WARNING: track_activity_query_size is set to $TRACK_ACTIVITY_QUERY_SIZE bytes"
    echo "   Complex queries may be truncated in Query Execution Plans section"
    echo "   Recommended: Increase to 8192 bytes for full query text capture"
    echo ""
elif [ -n "$TRACK_ACTIVITY_QUERY_SIZE" ] && [ "$TRACK_ACTIVITY_QUERY_SIZE" -ge 4096 ]; then
    echo "✅ track_activity_query_size is adequate for most queries"
    echo ""
fi

# Verify pg_stat_statements has data (if extension is installed)
# Note: We check data availability regardless of shared_preload_libraries status
# because current_setting('shared_preload_libraries') may return empty for non-superusers
if [ "$HAS_PG_STAT_STATEMENTS" = true ]; then
    STAT_STATEMENTS_COUNT=$(PGPASSWORD="$PGPASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -t -c "SELECT COUNT(*) FROM pg_stat_statements;" 2>/dev/null | xargs)
    if [ -n "$STAT_STATEMENTS_COUNT" ] && [ "$STAT_STATEMENTS_COUNT" -gt 0 ] 2>/dev/null; then
        HAS_PG_STAT_STATEMENTS_DATA=true
        echo "pg_stat_statements has data ($STAT_STATEMENTS_COUNT entries)"
        if [ "$PG_STAT_STATEMENTS_PRELOADED" = false ]; then
            echo "  Note: shared_preload_libraries check returned empty - this may be a permissions issue"
            echo "  pg_stat_statements data is available and will be used for analysis"
        fi
    else
        echo "WARNING: pg_stat_statements extension is installed but has no data"
        echo "This may be because:"
        echo "  1. pg_stat_statements is not in shared_preload_libraries"
        echo "  2. The extension was recently installed and needs time to collect data"
        echo "  3. pg_stat_statements.track is set to 'none'"
        echo "  4. No queries have been executed since installation"
        echo "Some performance metrics may be incomplete."
        HAS_PG_STAT_STATEMENTS_DATA=false
    fi
else
    HAS_PG_STAT_STATEMENTS_DATA=false
fi

# Get PostgreSQL version for compatibility
PG_VERSION=$(PGPASSWORD="$PGPASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -t -c "SELECT current_setting('server_version_num')::integer;" 2>/dev/null | xargs)
PG_MAJOR_VERSION=$((PG_VERSION / 10000))
PG_MINOR_VERSION=$((PG_VERSION / 100 % 100))
PG_VERSION_FULL=$(PGPASSWORD="$PGPASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -t -c "SELECT current_setting('server_version');" 2>/dev/null | xargs)
echo "PostgreSQL version detected: $PG_VERSION (Major: $PG_MAJOR_VERSION, Minor: $PG_MINOR_VERSION, Full: $PG_VERSION_FULL)"

# Initialize error counter and query counter
ERROR_COUNT=0
QUERY_COUNT=0
COMPLETED_QUERY_COUNT=0

# Version compatibility checks
if [ "$PG_MAJOR_VERSION" -lt 11 ]; then
    echo "ERROR: PostgreSQL version $PG_MAJOR_VERSION is not supported by AWS RDS/Aurora"
    echo "Supported versions: 11, 12, 13, 14, 15, 16, 17, 18"
    exit 1
fi

# ============================================================================
# Version Detection and Capability Checking Functions
# ============================================================================

# Function to detect deployment type (Aurora, RDS, or EC2)
detect_deployment_type() {
    echo "Detecting deployment type..."

    # Step 1: Check for Aurora-specific apg_* parameters (reliable - only exist on Aurora)
    local apg_param_count=$(PGPASSWORD="$PGPASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -t -c "SELECT COUNT(*) FROM pg_settings WHERE name LIKE 'apg_%';" 2>/dev/null | xargs)

    if [ "$apg_param_count" -gt 0 ] 2>/dev/null; then
        # This is Aurora - now check if Serverless via hostname (no database-level distinction exists)
        if [[ "$DB_HOST" == *"serverless"* ]]; then
            DEPLOYMENT_TYPE="Aurora Serverless"
            echo "  ✅ Detected: Amazon Aurora Serverless v2 PostgreSQL ($apg_param_count apg_ parameters)"
        else
            DEPLOYMENT_TYPE="Aurora"
            echo "  ✅ Detected: Amazon Aurora PostgreSQL - Provisioned ($apg_param_count apg_ parameters)"
        fi
    else
        # Step 2: Check for RDS via rds.* parameters or hostname
        local rds_param_count=$(PGPASSWORD="$PGPASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -t -c "SELECT COUNT(*) FROM pg_settings WHERE name LIKE 'rds.%';" 2>/dev/null | xargs)

        if [ "$rds_param_count" -gt 0 ] 2>/dev/null || [[ "$DB_HOST" == *"rds.amazonaws.com"* ]]; then
            DEPLOYMENT_TYPE="RDS"
            echo "  ✅ Detected: Amazon RDS PostgreSQL ($rds_param_count rds.* parameters)"
        else
            # Step 3: Self-managed / EC2 / On-premises
            DEPLOYMENT_TYPE="EC2"
            echo "  ✅ Detected: Self-managed PostgreSQL (EC2 or on-premises)"
        fi
    fi
}

# Function to check if a PostgreSQL function is available
check_function_available() {
    local function_name=$1
    
    # Query pg_proc to check if function exists
    local function_exists=$(PGPASSWORD="$PGPASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -t -c "
        SELECT COUNT(*) 
        FROM pg_proc p
        JOIN pg_namespace n ON p.pronamespace = n.oid
        WHERE p.proname = '$function_name'
        AND n.nspname NOT IN ('pg_catalog', 'information_schema')
        UNION ALL
        SELECT COUNT(*)
        FROM pg_proc p
        JOIN pg_namespace n ON p.pronamespace = n.oid
        WHERE p.proname = '$function_name'
        AND n.nspname IN ('pg_catalog')
    " 2>/dev/null | head -n 1 | xargs)
    
    if [ "$function_exists" -gt 0 ]; then
        echo "true"
    else
        echo "false"
    fi
}

# Function to compare versions: check if version >= target
version_gte() {
    local current_version=$1
    local target_version=$2
    
    if [ "$current_version" -ge "$target_version" ]; then
        return 0  # true
    else
        return 1  # false
    fi
}

# Function to compare versions: check if version < target
version_lt() {
    local current_version=$1
    local target_version=$2
    
    if [ "$current_version" -lt "$target_version" ]; then
        return 0  # true
    else
        return 1  # false
    fi
}

# Detect deployment type
detect_deployment_type

# Store capability flags for commonly used functions
echo ""
echo "Checking function availability..."

# Check for version-specific functions
if version_gte "$PG_VERSION" 130000; then
    HAS_PG_CURRENT_XACT_ID=true
    echo "  ✅ pg_current_xact_id() available (PostgreSQL 13+)"
else
    HAS_PG_CURRENT_XACT_ID=false
    echo "  ℹ️  Using txid_current() (PostgreSQL < 13)"
fi

# Check for pg_ls_waldir function
PG_LS_WALDIR_AVAILABLE=$(check_function_available "pg_ls_waldir")
if [ "$PG_LS_WALDIR_AVAILABLE" = "true" ]; then
    echo "  ✅ pg_ls_waldir() available"
else
    echo "  ⚠️  pg_ls_waldir() not available"
fi

# Check for pgstattuple_approx function (depends on extension)
if [ "$HAS_PGSTATTUPLE" = true ]; then
    PGSTATTUPLE_APPROX_AVAILABLE=$(check_function_available "pgstattuple_approx")
    if [ "$PGSTATTUPLE_APPROX_AVAILABLE" = "true" ]; then
        echo "  ✅ pgstattuple_approx() available"
    else
        echo "  ⚠️  pgstattuple_approx() not available (extension installed but function missing)"
        HAS_PGSTATTUPLE=false
    fi
else
    PGSTATTUPLE_APPROX_AVAILABLE="false"
fi

echo ""
echo "Version and capability detection complete."
echo "  Deployment Type: $DEPLOYMENT_TYPE"
echo "  PostgreSQL Version: $PG_VERSION_FULL"
echo "  Version Number: $PG_VERSION"
echo ""

# ============================================================================
# Bloat Analysis Method Selection
# ==============================================================================
echo "=============================================="
echo "Bloat Analysis Method Selection"
echo "=============================================="
echo ""
if [ -z "$BLOAT_OPTION" ]; then
    echo "Choose bloat analysis method:"
    echo "  1) Stats-based (Fast, Recommended) - Uses pg_stat_user_tables"
    echo "  2) pgstattuple (Accurate) - Requires pgstattuple extension"
    echo ""
    read -p "Select option [1]: " BLOAT_OPTION
fi
BLOAT_OPTION=${BLOAT_OPTION:-1}

if [ "$BLOAT_OPTION" = "2" ]; then
    BLOAT_METHOD="pgstattuple"
    echo ""
    echo "✅ Selected: pgstattuple-based bloat analysis"
    echo "   Using pgstattuple_approx() for accurate bloat measurement"
else
    BLOAT_METHOD="stats"
    echo ""
    echo "✅ Selected: Stats-based bloat analysis"
    echo "   Using pg_stat_user_tables for bloat estimation"
fi
echo ""
echo "Bloat analysis method configured: $BLOAT_METHOD"
echo "=============================================="
echo ""


# ============================================================================
# SQL Generation Functions for Version-Specific Queries
# ============================================================================

# Function to generate version-specific lock contention query
generate_lock_query() {
    local pg_version=$1
    
    # PostgreSQL 14+ introduced wait_event_type column changes
    # PostgreSQL 17+ has further refinements to wait events
    # Use simple NOT l.granted check for maximum compatibility
    
    cat << 'EOF'
WITH lock_waits AS (
    SELECT 
        blocked_locks.pid AS blocked_pid,
        blocked_activity.usename AS blocked_user,
        blocking_locks.pid AS blocking_pid,
        blocking_activity.usename AS blocking_user,
        blocked_activity.query AS blocked_query,
        blocking_activity.query AS blocking_query,
        blocked_locks.mode AS blocked_mode,
        blocking_locks.mode AS blocking_mode,
        blocked_locks.locktype AS lock_type,
        blocked_locks.relation::regclass AS locked_relation,
        COALESCE(EXTRACT(EPOCH FROM (now() - blocked_activity.query_start)), 0)::integer AS wait_time_seconds,
        COALESCE(EXTRACT(EPOCH FROM (now() - blocked_activity.state_change)), 0)::integer AS state_change_seconds
    FROM pg_catalog.pg_locks blocked_locks
    JOIN pg_catalog.pg_stat_activity blocked_activity ON blocked_activity.pid = blocked_locks.pid
    JOIN pg_catalog.pg_locks blocking_locks 
        ON blocking_locks.locktype = blocked_locks.locktype
        AND blocking_locks.database IS NOT DISTINCT FROM blocked_locks.database
        AND blocking_locks.relation IS NOT DISTINCT FROM blocked_locks.relation
        AND blocking_locks.page IS NOT DISTINCT FROM blocked_locks.page
        AND blocking_locks.tuple IS NOT DISTINCT FROM blocked_locks.tuple
        AND blocking_locks.virtualxid IS NOT DISTINCT FROM blocked_locks.virtualxid
        AND blocking_locks.transactionid IS NOT DISTINCT FROM blocked_locks.transactionid
        AND blocking_locks.classid IS NOT DISTINCT FROM blocked_locks.classid
        AND blocking_locks.objid IS NOT DISTINCT FROM blocked_locks.objid
        AND blocking_locks.objsubid IS NOT DISTINCT FROM blocked_locks.objsubid
        AND blocking_locks.pid != blocked_locks.pid
    JOIN pg_catalog.pg_stat_activity blocking_activity ON blocking_activity.pid = blocking_locks.pid
    WHERE NOT blocked_locks.granted
)
SELECT 
    blocked_pid,
    blocked_user,
    blocking_pid,
    blocking_user,
    lock_type,
    locked_relation,
    blocked_mode,
    blocking_mode,
    wait_time_seconds,
    state_change_seconds,
    LEFT(blocked_query, 100) AS blocked_query_preview,
    LEFT(blocking_query, 100) AS blocking_query_preview
FROM lock_waits
ORDER BY wait_time_seconds DESC
LIMIT 50;
EOF
}

# Function to generate version-specific transaction ID query
generate_xid_query() {
    local pg_version=$1
    
    # PostgreSQL 13+ replaced txid_current() with pg_current_xact_id()
    if version_gte "$pg_version" 130000; then
        # PostgreSQL 13+
        cat << 'EOF'
SELECT 
    txid_snapshot_xmax(txid_current_snapshot()) AS current_xid,
    age(datfrozenxid) AS xid_age,
    ROUND(100.0 * age(datfrozenxid) / 2147483647, 2) AS xid_age_percent,
    datfrozenxid,
    datname,
    CASE 
        WHEN age(datfrozenxid) >= 1900000000 THEN 'SEVERE EMERGENCY - Wraparound imminent within hours! (95%)'
        WHEN age(datfrozenxid) >= 1800000000 THEN 'EMERGENCY - Immediate action required! (90%)'
        WHEN age(datfrozenxid) >= 1500000000 THEN 'CRITICAL - Urgent attention needed (75%)'
        WHEN age(datfrozenxid) >= 1200000000 THEN 'WARNING - Monitor closely (60%)'
        WHEN age(datfrozenxid) >= 1000000000 THEN 'INFO - Approaching threshold (50%)'
        ELSE 'OK'
    END AS xid_status
FROM pg_database
WHERE datallowconn
ORDER BY age(datfrozenxid) DESC;
EOF
    else
        # PostgreSQL 11-12
        cat << 'EOF'
SELECT 
    txid_snapshot_xmax(txid_current_snapshot()) AS current_xid,
    age(datfrozenxid) AS xid_age,
    ROUND(100.0 * age(datfrozenxid) / 2147483647, 2) AS xid_age_percent,
    datfrozenxid,
    datname,
    CASE 
        WHEN age(datfrozenxid) >= 1900000000 THEN 'SEVERE EMERGENCY - Wraparound imminent within hours! (95%)'
        WHEN age(datfrozenxid) >= 1800000000 THEN 'EMERGENCY - Immediate action required! (90%)'
        WHEN age(datfrozenxid) >= 1500000000 THEN 'CRITICAL - Urgent attention needed (75%)'
        WHEN age(datfrozenxid) >= 1200000000 THEN 'WARNING - Monitor closely (60%)'
        WHEN age(datfrozenxid) >= 1000000000 THEN 'INFO - Approaching threshold (50%)'
        ELSE 'OK'
    END AS xid_status
FROM pg_database
WHERE datallowconn
ORDER BY age(datfrozenxid) DESC;
EOF
    fi
}

# Function to generate version-specific WAL parameter query
generate_wal_query() {
    local pg_version=$1
    
    # PostgreSQL 13+ replaced wal_keep_segments with wal_keep_size
    if version_gte "$pg_version" 130000; then
        # PostgreSQL 13+
        cat << 'EOF'
SELECT 
    name,
    setting,
    unit,
    CASE 
        WHEN name = 'wal_keep_size' THEN pg_size_pretty(setting::bigint * 1024 * 1024)
        ELSE setting || COALESCE(' ' || unit, '')
    END AS formatted_value,
    short_desc
FROM pg_settings
WHERE name IN (
    'wal_level',
    'wal_keep_size',
    'max_wal_size',
    'min_wal_size',
    'wal_compression',
    'archive_mode',
    'archive_command'
)
ORDER BY name;
EOF
    else
        # PostgreSQL 11-12
        cat << 'EOF'
SELECT 
    name,
    setting,
    unit,
    CASE 
        WHEN name = 'wal_keep_segments' THEN setting || ' segments (~' || pg_size_pretty(setting::bigint * 16 * 1024 * 1024) || ')'
        ELSE setting || COALESCE(' ' || unit, '')
    END AS formatted_value,
    short_desc
FROM pg_settings
WHERE name IN (
    'wal_level',
    'wal_keep_segments',
    'max_wal_size',
    'min_wal_size',
    'wal_compression',
    'archive_mode',
    'archive_command'
)
ORDER BY name;
EOF
    fi
}

# Function to generate bloat analysis query based on method
generate_bloat_query() {
    local method=$1
    local pg_version=$2
    
    if [ "$method" = "pgstattuple" ]; then
        # Accurate pgstattuple-based analysis
        cat << 'EOF'
WITH bloat_data AS (
    SELECT
        schemaname,
        relname AS tablename,
        pg_size_pretty(pg_total_relation_size(quote_ident(schemaname) || '.' || quote_ident(relname))) AS total_size,
        pg_total_relation_size(quote_ident(schemaname) || '.' || quote_ident(relname)) AS total_bytes,
        CASE 
            WHEN EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pgstattuple') THEN
                (SELECT dead_tuple_percent FROM pgstattuple_approx(quote_ident(schemaname) || '.' || quote_ident(relname)))
            ELSE
                ROUND(100.0 * n_dead_tup / NULLIF(n_live_tup + n_dead_tup, 0), 2)
        END AS bloat_percent,
        n_dead_tup,
        n_live_tup,
        last_vacuum,
        last_autovacuum,
        CASE 
            WHEN EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pgstattuple') THEN 'pgstattuple (accurate)'
            ELSE 'stats-based (estimated)'
        END AS analysis_method
    FROM pg_stat_user_tables
    WHERE n_live_tup + n_dead_tup > 0
)
SELECT 
    schemaname,
    tablename,
    total_size,
    bloat_percent,
    n_dead_tup,
    n_live_tup,
    last_vacuum,
    last_autovacuum,
    analysis_method,
    CASE 
        WHEN bloat_percent >= 50 THEN 'CRITICAL'
        WHEN bloat_percent >= 30 THEN 'WARNING'
        WHEN bloat_percent >= 10 THEN 'INFO'
        ELSE 'OK'
    END AS bloat_status
FROM bloat_data
WHERE total_bytes > 1048576  -- Only tables > 1MB
ORDER BY bloat_percent DESC NULLS LAST, total_bytes DESC
LIMIT 50;
EOF
    else
        # Fast stats-based analysis
        cat << 'EOF'
WITH bloat_data AS (
    SELECT
        schemaname,
        relname AS tablename,
        pg_size_pretty(pg_total_relation_size(quote_ident(schemaname) || '.' || quote_ident(relname))) AS total_size,
        pg_total_relation_size(quote_ident(schemaname) || '.' || quote_ident(relname)) AS total_bytes,
        ROUND(100.0 * n_dead_tup / NULLIF(n_live_tup + n_dead_tup, 0), 2) AS bloat_percent,
        n_dead_tup,
        n_live_tup,
        last_vacuum,
        last_autovacuum,
        'stats-based (estimated)' AS analysis_method
    FROM pg_stat_user_tables
    WHERE n_live_tup + n_dead_tup > 0
)
SELECT 
    schemaname,
    tablename,
    total_size,
    bloat_percent,
    n_dead_tup,
    n_live_tup,
    last_vacuum,
    last_autovacuum,
    analysis_method,
    CASE 
        WHEN bloat_percent >= 50 THEN 'CRITICAL'
        WHEN bloat_percent >= 30 THEN 'WARNING'
        WHEN bloat_percent >= 10 THEN 'INFO'
        ELSE 'OK'
    END AS bloat_status
FROM bloat_data
WHERE total_bytes > 1048576  -- Only tables > 1MB
ORDER BY bloat_percent DESC NULLS LAST, total_bytes DESC
LIMIT 50;
EOF
    fi
}

# Function to execute version-aware query with automatic SQL generation
execute_version_aware_query() {
    local title="$1"
    local query_type="$2"
    local section_level="${3:-h3}"
    
    local sql=""
    
    case $query_type in
        "lock")
            sql=$(generate_lock_query "$PG_VERSION")
            ;;
        "xid")
            sql=$(generate_xid_query "$PG_VERSION")
            ;;
        "wal")
            sql=$(generate_wal_query "$PG_VERSION")
            ;;
        "bloat")
            sql=$(generate_bloat_query "$BLOAT_METHOD" "$PG_VERSION")
            ;;
        *)
            echo "ERROR: Unknown query type: $query_type" >&2
            return 1
            ;;
    esac
    
    # Execute the generated SQL using the standard execution function
    execute_sql_to_html "$title" "$sql" "$section_level"
}

# Generate timestamp for filename
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")

# Create HTML filename with customer name, database name, and timestamp
# Sanitize deployment type for filename (replace spaces with hyphens)
DEPLOY_TAG=$(echo "$DEPLOYMENT_TYPE" | tr ' ' '-')
HTML_FILENAME="pg-health_${CUSTOMER_NAME}_${DB_NAME}_pg${PG_MAJOR_VERSION}_${DEPLOY_TAG}_${TIMESTAMP}.html"

# Get largest database name for filename
LARGEST_DB=$(PGPASSWORD="$PGPASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -t -c "SELECT datname FROM pg_database WHERE datname NOT IN ('template0', 'template1', 'postgres') ORDER BY pg_database_size(datname) DESC LIMIT 1;" 2>/dev/null | xargs)

# Use the HTML_FILENAME that was already set with customer name format
OUTPUT_FILE="$HTML_FILENAME"

# HTML Header
cat > "$OUTPUT_FILE" << "EOF"
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🐘 PostgreSQL Health Dashboard - Comprehensive Database Analysis</title>
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 0; padding: 0; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; }
        .container { max-width: 100%; margin: 0; background-color: white; padding: 20px; box-sizing: border-box; }
        
        /* Header with Flexbox */
        .header { 
            background: linear-gradient(135deg, #a8e6cf 0%, #7fcdcd 50%, #6bb6ff 100%); 
            color: #2c3e50; 
            padding: 20px; 
            margin: -20px -20px 20px -20px; 
            border-radius: 12px 12px 0 0; 
            text-align: center; 
            line-height: 1.2; 
        }
        .header h1 { margin: 0; font-size: 2.5em; font-weight: 350; text-shadow: 2px 2px 4px rgba(0,0,0,0.3); color: #2c3e50; }
        .header .server-info { background: rgba(255,255,255,0.1); padding: 10px; margin-top: 10px; border-radius: 8px; font-size: 1.254em; }
        
        /* Section Headers with Flexbox */
        .section-header {
            display: flex;
            align-items: center;
            margin: 0;
            padding: 15px;
            background: linear-gradient(90deg, #f8f9fa 0%, #e9ecef 100%);
            border-radius: 8px;
            border-left: 4px solid #3498db;
        }
        
        /* Section-specific header colors matching navigation cards */
        #section1 { border-left: 4px solid #4f46e5; }
        #section1 h2 { color: #4f46e5; }
        #section2 { border-left: 4px solid #ec4899; }
        #section2 h2 { color: #ec4899; }
        #section3 { border-left: 4px solid #06b6d4; }
        #section3 h2 { color: #06b6d4; }
        #section4 { border-left: 4px solid #10b981; }
        #section4 h2 { color: #10b981; }
        #section5 { border-left: 4px solid #f59e0b; }
        #section5 h2 { color: #f59e0b; }
        #section6 { border-left: 4px solid #8b5cf6; }
        #section6 h2 { color: #8b5cf6; }
        #section7 { border-left: 4px solid #ef4444; }
        #section7 h2 { color: #ef4444; }
        #section8 { border-left: 4px solid #6366f1; }
        #section8 h2 { color: #6366f1; }
        #section9 { border-left: 4px solid #14b8a6; }
        #section9 h2 { color: #14b8a6; }
        .section-header h2 {
            margin: 0;
            color: #34495e;
            font-size: 0.98em;
            line-height: 1;
            flex-grow: 1;
        }
        
        /* Card-based Layout with CSS Grid */
        .metrics-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
            margin: 20px 0;
        }
        
        .metric-card {
            background: white;
            border-radius: 12px;
            padding: 20px;
            box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
            border: 1px solid #e1e8ed;
            transition: transform 0.2s ease, box-shadow 0.2s ease;
        }
        
        .metric-card:hover {
            transform: translateY(-2px);
            box-shadow: 0 8px 15px rgba(0, 0, 0, 0.15);
        }
        
        .metric-card h3 {
            margin: 0 0 15px 0;
            color: #2c3e50;
            font-size: 1.1em;
            border-bottom: 2px solid #ecf0f1;
            padding-bottom: 10px;
        }
        
        /* Status indicators */
        .status-indicator {
            display: inline-block;
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 0.85em;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        
        .status-critical { background-color: #e74c3c; color: white; }
        .status-warning { background-color: #f39c12; color: white; }
        .status-ok { background-color: #27ae60; color: white; }
        .status-info { background-color: #3498db; color: white; }
        
        /* Table improvements */
        .table-container {
            background: white;
            border: 1px solid #e1e8ed;
            border-radius: 12px;
            overflow: hidden;
            box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
            margin: 0;
        }
        
        .table-container h3 {
            margin: 0;
            padding: 10px 15px 0 15px;
            font-size: 0.85em;
            font-weight: 700;
        }
        
        /* Sub-section accordion labels */
        .table-container .accordion-label {
            display: block;
            cursor: pointer;
            transition: all 0.3s ease;
        }
        
        .table-container .accordion-label::after {
            content: '+';
            float: right;
            font-size: 14px;
            font-weight: bold;
            transition: all 0.3s ease;
            color: #2c3e50;
        }
        
        .table-container .accordion-checkbox:checked + .accordion-label::after {
            content: '−';
            transform: rotate(180deg);
        }
        
        table { 
            width: 100%; 
            border-collapse: collapse; 
            margin: 0;
        }
        
        th, td { 
            padding: 12px 15px; 
            text-align: left; 
            border-bottom: 1px solid #e1e8ed;
        }
        
        th { 
            background: linear-gradient(135deg, #3498db 0%, #2980b9 100%);
            color: white; 
            font-weight: 600;
            position: sticky;
            top: 0;
            z-index: 10;
        }
        
        tr:nth-child(even) { background-color: #f8f9fa; }
        tr:hover { background-color: #e3f2fd; }
        
        .critical { background-color: #e74c3c !important; color: white; }
        .warning { background-color: #f39c12 !important; color: white; }
        .ok { background-color: #27ae60 !important; color: white; }
        .info { background-color: #3498db !important; color: white; }
        
        /* Pre-formatted text improvements with syntax highlighting */
        pre { 
            background: linear-gradient(135deg, #f8f9fa 0%, #e9ecef 100%);
            padding: 20px; 
            border-radius: 8px; 
            overflow-x: auto; 
            font-size: 12px;
            border: 1px solid #dee2e6;
            box-shadow: inset 0 2px 4px rgba(0, 0, 0, 0.05);
            font-family: 'Courier New', Consolas, monospace;
        }
        
        /* SQL Syntax Highlighting */
        .sql-keyword { color: #0066cc; font-weight: bold; }
        .sql-string { color: #009900; }
        .sql-number { color: #cc6600; }
        .sql-comment { color: #999999; font-style: italic; }
        
        .section { margin-bottom: 15px; }
        .accordion-checkbox { display: none; }
        .accordion-label { display: block; cursor: pointer; transition: all 0.3s ease; }
        .accordion-content { max-height: 0; overflow: hidden; transition: max-height 0.3s ease-out; }
        .accordion-checkbox:checked ~ .accordion-content { max-height: 10000px; }
        
        /* Add spacing only when sections are expanded */
        .accordion-checkbox:checked + .accordion-label.section-header {
            margin: 30px 0 20px 0;
        }
        
        /* Make collapsed section borders touch without overlapping */
        .accordion-checkbox:not(:checked) + .accordion-label.section-header {
            margin-bottom: -14px;
        }
        .accordion-label::after { 
            content: '+'; 
            float: right; 
            font-size: 18px; 
            font-weight: bold; 
            transition: all 0.3s ease; 
            color: #2c3e50;
            width: 24px;
            height: 24px;
            display: flex;
            align-items: center;
            justify-content: center;
            border-radius: 50%;
            background: rgba(52, 152, 219, 0.1);
        }
        .accordion-checkbox:checked + .accordion-label::after { 
            content: '−'; 
            background: rgba(52, 152, 219, 0.2);
            transform: rotate(180deg);
        }
        .master-toggle { display: block; margin: 20px 0; background: linear-gradient(135deg, #ff6b6b 0%, #ee5a24 100%); color: white; border: none; padding: 12px 24px; border-radius: 50px; cursor: pointer; font-weight: bold; box-shadow: 0 8px 25px rgba(255,107,107,0.3); transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1); }
        .master-toggle:hover { transform: translateY(-3px) scale(1.02); box-shadow: 0 12px 35px rgba(255,107,107,0.4); }
        .master-toggle::before { content: '▶ Expand All'; }
        .master-toggle.expanded::before { content: '▼ Collapse All'; }
        
        /* Footer with Flexbox */
        .footer { 
            background: linear-gradient(135deg, #a8e6cf 0%, #7fcdcd 50%, #6bb6ff 100%); 
            color: #2c3e50; 
            padding: 25px; 
            margin: 30px -20px -20px -20px; 
            border-radius: 0 0 12px 12px; 
            display: flex;
            flex-direction: column;
            align-items: center;
            text-align: center;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; 
        }
        .footer .disclaimer { font-size: 0.85em; opacity: 0.8; margin-top: 15px; }
        .navigator { background: linear-gradient(135deg, #f8fafc 0%, #e2e8f0 100%); padding: 20px; margin: 15px 0; border-radius: 20px; border: 1px solid rgba(255,255,255,0.8); box-shadow: 0 8px 32px rgba(0,0,0,0.06); line-height: 1.1; }
        .navigator h2 { margin: 0 0 28px 0; color: #1e293b; text-align: center; font-weight: 600; font-size: 1.5em; }
        .card-grid { display: grid; grid-template-columns: repeat(9, 1fr); gap: 12px; }
        .nav-card { display: flex; flex-direction: column; align-items: center; background: white; padding: 16px 8px; border-radius: 16px; text-decoration: none; color: white; box-shadow: 0 4px 24px rgba(0,0,0,0.08); transition: all 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.275); text-align: center; border: 1px solid rgba(255,255,255,0.3); position: relative; overflow: hidden; }
        .nav-card::before { content: ''; position: absolute; top: 0; left: 0; right: 0; bottom: 0; background: inherit; z-index: -1; }
        .nav-card:hover { transform: translateY(-12px) scale(1.03); box-shadow: 0 24px 48px rgba(0,0,0,0.16); }
        .nav-card:nth-child(1) { background: linear-gradient(135deg, #4f46e5 0%, #7c3aed 100%); }
        .nav-card:nth-child(2) { background: linear-gradient(135deg, #ec4899 0%, #be185d 100%); }
        .nav-card:nth-child(3) { background: linear-gradient(135deg, #06b6d4 0%, #0891b2 100%); }
        .nav-card:nth-child(4) { background: linear-gradient(135deg, #10b981 0%, #059669 100%); }
        .nav-card:nth-child(5) { background: linear-gradient(135deg, #f59e0b 0%, #d97706 100%); }
        .nav-card:nth-child(6) { background: linear-gradient(135deg, #8b5cf6 0%, #7c3aed 100%); }
        .nav-card:nth-child(7) { background: linear-gradient(135deg, #ef4444 0%, #dc2626 100%); }
        .nav-card:nth-child(8) { background: linear-gradient(135deg, #6366f1 0%, #4f46e5 100%); }
        .nav-card:nth-child(9) { background: linear-gradient(135deg, #14b8a6 0%, #0d9488 100%); }
        .card-icon { font-size: 24px; margin-bottom: 8px; filter: drop-shadow(0 2px 4px rgba(0,0,0,0.1)); }
        .card-title { font-weight: 700; font-size: 14px; margin-bottom: 4px; letter-spacing: -0.025em; }
        .card-desc { font-size: 11px; opacity: 0.9; line-height: 1.3; font-weight: 400; }
        
        /* Limitation Badges */
        .limitation-badge {
            display: inline-block;
            padding: 4px 12px;
            border-radius: 12px;
            font-size: 0.75em;
            font-weight: 600;
            margin-left: 10px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            vertical-align: middle;
        }
        
        .badge-limited {
            background: linear-gradient(135deg, #f59e0b 0%, #d97706 100%);
            color: white;
            border: 1px solid rgba(245, 158, 11, 0.3);
        }
        
        .badge-fallback {
            background: linear-gradient(135deg, #3498db 0%, #2980b9 100%);
            color: white;
            border: 1px solid rgba(52, 152, 219, 0.3);
        }
        
        .badge-error {
            background: linear-gradient(135deg, #e74c3c 0%, #c0392b 100%);
            color: white;
            border: 1px solid rgba(231, 76, 60, 0.3);
        }
        
        .badge-stats-based {
            background: linear-gradient(135deg, #10b981 0%, #059669 100%);
            color: white;
            border: 1px solid rgba(16, 185, 129, 0.3);
        }
        
        .badge-pgstattuple {
            background: linear-gradient(135deg, #8b5cf6 0%, #7c3aed 100%);
            color: white;
            border: 1px solid rgba(139, 92, 246, 0.3);
        }
        
        /* Limitation notice boxes */
        .limitation-notice {
            background: linear-gradient(135deg, #fff3cd 0%, #ffeaa7 100%);
            border: 2px solid #f39c12;
            padding: 12px 15px;
            border-radius: 8px;
            margin: 10px 0;
            color: #856404;
            box-shadow: 0 2px 8px rgba(243, 156, 18, 0.15);
        }
        
        .limitation-notice strong {
            color: #d97706;
        }
        
        .fallback-notice {
            background: linear-gradient(135deg, #d1ecf1 0%, #bee5eb 100%);
            border: 2px solid #3498db;
            padding: 12px 15px;
            border-radius: 8px;
            margin: 10px 0;
            color: #0c5460;
            box-shadow: 0 2px 8px rgba(52, 152, 219, 0.15);
        }
        
        .fallback-notice strong {
            color: #2980b9;
        }
        
        .error-notice {
            background: linear-gradient(135deg, #f8d7da 0%, #f5c6cb 100%);
            border: 2px solid #e74c3c;
            padding: 12px 15px;
            border-radius: 8px;
            margin: 10px 0;
            color: #721c24;
            box-shadow: 0 2px 8px rgba(231, 76, 60, 0.15);
        }
        
        .error-notice strong {
            color: #c0392b;
        }
        
        /* Compatibility Matrix Styles */
        .compatibility-matrix {
            margin: 20px 0;
        }
        
        .compatibility-section {
            background: linear-gradient(135deg, #f8f9fa 0%, #ffffff 100%);
            border: 1px solid #e0e0e0;
            border-radius: 8px;
            padding: 20px;
            margin-bottom: 20px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.05);
        }
        
        .compatibility-section h4 {
            margin: 0 0 15px 0;
            color: #2c3e50;
            font-size: 1.1em;
            border-bottom: 2px solid #3498db;
            padding-bottom: 8px;
        }
        
        .compatibility-table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 10px;
        }
        
        .compatibility-table th {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 12px;
            text-align: left;
            font-weight: 600;
            border: 1px solid #5a67d8;
        }
        
        .compatibility-table td {
            padding: 10px 12px;
            border: 1px solid #e0e0e0;
            background: white;
        }
        
        .compatibility-table tr:nth-child(even) td {
            background: #f8f9fa;
        }
        
        .compatibility-table tr:hover td {
            background: #e3f2fd;
        }
        
        .status-ok {
            color: #10b981;
            font-weight: 600;
        }
        
        .status-warning {
            color: #f59e0b;
            font-weight: 600;
        }
        
        .status-critical {
            color: #ef4444;
            font-weight: 600;
        }
        
        .status-info {
            color: #3498db;
            font-weight: 600;
        }
        
        .limitation-list {
            margin-top: 10px;
        }
        
        .limitation-item {
            display: flex;
            align-items: flex-start;
            background: white;
            border: 1px solid #e0e0e0;
            border-left: 4px solid #3498db;
            border-radius: 6px;
            padding: 15px;
            margin-bottom: 15px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.05);
        }
        
        .limitation-icon {
            font-size: 24px;
            margin-right: 15px;
            flex-shrink: 0;
        }
        
        .limitation-content {
            flex: 1;
        }
        
        .limitation-content strong {
            display: block;
            color: #2c3e50;
            margin-bottom: 8px;
            font-size: 1.05em;
        }
        
        .limitation-content p {
            margin: 5px 0;
            color: #555;
            line-height: 1.6;
        }
        
        .limitation-content ol {
            margin: 10px 0 10px 20px;
            color: #555;
        }
        
        .limitation-content li {
            margin: 5px 0;
        }
        
        .limitation-content a {
            color: #3498db;
            text-decoration: none;
            font-weight: 600;
        }
        
        .limitation-content a:hover {
            text-decoration: underline;
        }
        
        .resource-links ul {
            list-style: none;
            padding: 0;
            margin: 10px 0;
        }
        
        .resource-links li {
            padding: 8px 0;
            border-bottom: 1px solid #e0e0e0;
        }
        
        .resource-links li:last-child {
            border-bottom: none;
        }
        
        .resource-links a {
            color: #3498db;
            text-decoration: none;
            font-weight: 500;
            display: flex;
            align-items: center;
        }
        
        .resource-links a:hover {
            color: #2980b9;
            text-decoration: underline;
        }
        
        .resource-links a::before {
            content: "📖";
            margin-right: 8px;
        }

        /* Query Execution Plans Enhanced Styles */
        .query-explain-container {
            margin: 12px 0;
            border: 1px solid #dee2e6;
            border-radius: 6px;
            overflow: hidden;
            background: white;
        }
        
        .query-header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 8px 12px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .query-number {
            font-weight: 600;
            font-size: 13.6px;
        }
        
        .severity-badge {
            padding: 3px 10px;
            border-radius: 3px;
            font-size: 10.2px;
            font-weight: 600;
            text-transform: uppercase;
        }
        
        .severity-badge.status-critical {
            background-color: #dc2626;
            color: white;
        }
        
        .severity-badge.status-warning {
            background-color: #f59e0b;
            color: white;
        }
        
        .severity-badge.status-info {
            background-color: #3b82f6;
            color: white;
        }
        
        .severity-badge.status-ok {
            background-color: #10b981;
            color: white;
        }
        
        .query-stats {
            padding: 10px 12px;
            background: #f8f9fa;
            border-bottom: 1px solid #dee2e6;
        }
        
        .query-stats table {
            width: 100%;
            font-size: 11px;
            margin: 0;
        }
        
        .query-stats th {
            padding: 6px 8px;
            background: linear-gradient(135deg, #3498db 0%, #2980b9 100%);
            color: white;
            font-weight: 600;
        }
        
        .query-stats td {
            text-align: center;
            padding: 6px 8px;
            background: white;
        }
        
        .query-text {
            padding: 10px 12px;
            background: #f8f9fa;
            border-bottom: 1px solid #dee2e6;
        }
        
        .query-text strong {
            color: #495057;
            font-size: 11.9px;
        }
        
        .query-text pre {
            background: white;
            padding: 8px;
            border-radius: 3px;
            border-left: 3px solid #6c757d;
            margin: 5px 0 0 0;
            overflow-x: auto;
            font-size: 11px;
            font-family: 'Courier New', monospace;
            white-space: pre-wrap;
            word-wrap: break-word;
            line-height: 1.4;
        }
        
        .explain-output {
            padding: 10px 12px;
        }
        
        .explain-output strong {
            color: #495057;
            font-size: 11.9px;
        }
        
        .explain-method-badge {
            padding: 5px 10px;
            margin: 6px 0;
            border-radius: 3px;
            font-size: 11px;
            font-weight: 600;
            display: inline-block;
        }
        
        .explain-method-badge.status-ok {
            background-color: #d4edda;
            color: #155724;
        }
        
        .explain-method-badge.status-warning {
            background-color: #fff3cd;
            color: #856404;
        }
        
        .explain-plan {
            background: #f8f9fa;
            padding: 10px;
            border-radius: 3px;
            border-left: 3px solid #17a2b8;
            margin-top: 6px;
            overflow-x: auto;
            font-size: 10.2px;
            font-family: 'Courier New', monospace;
            white-space: pre;
            line-height: 1.4;
        }
        
        .explain-error {
            background: #fff3cd;
            padding: 10px;
            border-radius: 3px;
            border-left: 3px solid #ffc107;
            margin-top: 6px;
            font-size: 10.2px;
            line-height: 1.5;
            color: #856404;
        }
        
        .section-description {
            padding: 10px 15px;
            color: #6c757d;
            font-size: 14px;
            margin: 0;
            background: white;
        }
        
        .empty-state {
            padding: 20px;
            background: #fff3cd;
            border-radius: 8px;
            border-left: 4px solid #ffc107;
            margin: 15px;
        }
        
        .empty-state p {
            margin: 0 0 10px 0;
            color: #856404;
        }
        
        .empty-state strong {
            color: #856404;
        }
        
        .empty-state ul {
            margin: 10px 0 0 20px;
            color: #856404;
        }
        
        .empty-state li {
            margin: 5px 0;
        }

        
        /* Responsive Design */
        @media screen and (max-width: 1024px) {
            .container { padding: 15px; }
            .header { padding: 20px; margin: -15px -15px 20px -15px; }
            .header h1 { font-size: 2em; }
            .header .server-info { font-size: 1.1em; padding: 12px; }
            .metrics-grid { grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 15px; }
            .card-grid { grid-template-columns: repeat(9, 1fr); }
        }
        
        @media screen and (max-width: 900px) {
            .card-grid { grid-template-columns: repeat(3, 1fr); }
        }
        
        @media screen and (max-width: 768px) {
            .container { padding: 10px; }
            .header { padding: 15px; margin: -10px -10px 15px -10px; }
            .header h1 { font-size: 1.8em; }
            .header .server-info { font-size: 1em; padding: 10px; }
            .section-header { padding: 10px; }
            .section-header h2 { font-size: 1.2em; }
            .metrics-grid { grid-template-columns: 1fr; gap: 15px; }
            .metric-card { padding: 15px; }
            table { font-size: 14px; }
            th, td { padding: 8px 10px; }
            pre { font-size: 12px; padding: 15px; }
            .footer { padding: 15px; margin: 20px -10px -10px -10px; }
            .card-grid { grid-template-columns: repeat(2, 1fr); }
        }
        
        @media screen and (max-width: 480px) {
            .container { padding: 8px; }
            .header { padding: 12px; margin: -8px -8px 12px -8px; }
            .header h1 { font-size: 1.5em; }
            .header .server-info { font-size: 0.9em; padding: 8px; }
            .section-header { padding: 8px; }
            .section-header h2 { font-size: 1.1em; }
            .metric-card { padding: 12px; }
            table { font-size: 12px; }
            th, td { padding: 6px 8px; }
            pre { font-size: 11px; padding: 12px; }
            .footer { padding: 12px; margin: 15px -8px -8px -8px; }
            .footer .disclaimer { font-size: 0.8em; }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1><img src="https://wiki.postgresql.org/images/3/30/PostgreSQL_logo.3colors.120x120.png" alt="PostgreSQL" style="height: 60px; vertical-align: middle; margin-right: 15px;"><strong>PostgreSQL Health Report</strong></h1>
            <div class="server-info">
EOF

# Add extension status to HTML with pg_stat_statements detail
# Build pg_stat_statements status line for header
PGSS_HEADER_INFO=""
if [ "$HAS_PG_STAT_STATEMENTS" = true ] && [ "$PG_STAT_STATEMENTS_PRELOADED" = true ]; then
    PGSS_HEADER_INFO="<br><span style='font-size: 0.8em; opacity: 0.85;'>pg_stat_statements: installed | track_activity_query_size: ${TRACK_ACTIVITY_QUERY_SIZE_RAW}</span>"
elif [ "$HAS_PG_STAT_STATEMENTS" = true ]; then
    PGSS_HEADER_INFO="<br><span style='font-size: 0.8em; opacity: 0.85; color: #856404;'>pg_stat_statements: installed but not in shared_preload_libraries</span>"
else
    PGSS_HEADER_INFO="<br><span style='font-size: 0.8em; opacity: 0.85; color: #856404;'>pg_stat_statements: not installed</span>"
fi

if [ "$EXTENSION_WARNINGS" = true ]; then
    EXTENSION_STATUS_HTML="<div style='margin-top: 10px; padding: 8px; border-radius: 6px; background: rgba(255, 193, 7, 0.2); color: #856404; border: 1px solid rgba(255, 193, 7, 0.3);'>⚠️ Limited Analysis Mode - Some extensions missing${PGSS_HEADER_INFO}</div>"
    EXTENSION_WARNINGS_JS="false"
else
    EXTENSION_STATUS_HTML="<div style='margin-top: 10px; padding: 8px; border-radius: 6px; background: rgba(40, 167, 69, 0.2); color: #155724; border: 1px solid rgba(40, 167, 69, 0.3);'>✅ Full Analysis Mode - All extensions available${PGSS_HEADER_INFO}</div>"
    EXTENSION_WARNINGS_JS="true"
fi

echo "                <strong>Customer:</strong> ${CUSTOMER_NAME} | <strong>Database endpoint:</strong> ${DB_HOST}:${DB_PORT} | <strong>Database:</strong> ${DB_NAME}" >> "$OUTPUT_FILE"
echo "                <br><span style='font-size: 0.6em;'><strong>Start time:</strong> $START_TIME (XID = $START_XID) | <strong>End time:</strong> <span id='end-time'>Processing...</span> | <strong>Duration:</strong> <span id='duration'>Calculating...</span></span>" >> "$OUTPUT_FILE"
echo "                $EXTENSION_STATUS_HTML" >> "$OUTPUT_FILE"

cat >> "$OUTPUT_FILE" << "EOF"
            </div>
        </div>
        <div class="navigator">
            <div class="card-grid">
                <a href="#section1" class="nav-card">
                    <div class="card-icon">🔗</div>
                    <div class="card-title">Overview</div>
                    <div class="card-desc">Connection & Environment</div>
                </a>
                <a href="#section2" class="nav-card">
                    <div class="card-icon">⚙️</div>
                    <div class="card-title">Configuration</div>
                    <div class="card-desc">System Setup</div>
                </a>
                <a href="#section3" class="nav-card">
                    <div class="card-icon">⚡</div>
                    <div class="card-title">Activity</div>
                    <div class="card-desc">Real-time Status</div>
                </a>
                <a href="#section4" class="nav-card">
                    <div class="card-icon">🔄</div>
                    <div class="card-title">Replication</div>
                    <div class="card-desc">High Availability</div>
                </a>
                <a href="#section5" class="nav-card">
                    <div class="card-icon">💾</div>
                    <div class="card-title">Data Footprint</div>
                    <div class="card-desc">Size & WAL</div>
                </a>
                <a href="#section6" class="nav-card">
                    <div class="card-icon">📊</div>
                    <div class="card-title">Performance</div>
                    <div class="card-desc">Query & I/O Metrics</div>
                </a>
                <a href="#section7" class="nav-card">
                    <div class="card-icon">🔧</div>
                    <div class="card-title">Maintenance</div>
                    <div class="card-desc">Vacuum & Integrity</div>
                </a>
                <a href="#section8" class="nav-card">
                    <div class="card-icon">🎯</div>
                    <div class="card-title">Optimization</div>
                    <div class="card-desc">Index & Schema</div>
                </a>
                <a href="#section9" class="nav-card">
                    <div class="card-icon">📋</div>
                    <div class="card-title">Summary</div>
                    <div class="card-desc">Findings & Actions</div>
                </a>
            </div>
        </div>
        <button id="masterToggle" class="master-toggle" onclick="toggleAll()"></button>

EOF

# Function to capture and format PostgreSQL errors with context
capture_pg_error() {
    local title="$1"
    local sql="$2"
    local error_output="$3"
    
    # Increment error counter
    ((ERROR_COUNT++))
    
    # Extract first 5 lines of SQL for preview
    local sql_preview=$(echo "$sql" | head -n 5)
    
    # Format error message for HTML display
    cat >> "$OUTPUT_FILE" << EOF
<div style='background: #f8d7da; border: 2px solid #f5c6cb; padding: 15px; border-radius: 8px; margin: 10px 0; color: #721c24;'>
    <div style='font-size: 1.2em; font-weight: bold; margin-bottom: 10px;'>
        ⚠️ Query Execution Error
    </div>
    <div style='margin-bottom: 8px;'>
        <strong>Query Title:</strong> $title
    </div>
    <div style='margin-bottom: 8px;'>
        <strong>PostgreSQL Version:</strong> $PG_VERSION_FULL (Version Number: $PG_VERSION)
    </div>
    <div style='margin-bottom: 8px;'>
        <strong>Error Message:</strong>
        <pre style='background: #fff; padding: 10px; border-radius: 4px; margin: 5px 0; overflow-x: auto; color: #d32f2f;'>$error_output</pre>
    </div>
    <div style='margin-bottom: 8px;'>
        <strong>SQL Preview (first 5 lines):</strong>
        <pre style='background: #fff; padding: 10px; border-radius: 4px; margin: 5px 0; overflow-x: auto; font-family: monospace; font-size: 0.9em;'>$sql_preview</pre>
    </div>
    <div style='font-size: 0.9em; font-style: italic; color: #856404; background: #fff3cd; padding: 8px; border-radius: 4px; margin-top: 10px;'>
        💡 <strong>Tip:</strong> This error has been logged. The script will continue with remaining queries.
    </div>
</div>
EOF
}

# Function to render limitation badges in HTML
render_limitation_badge() {
    local badge_type="$1"
    local message="$2"
    
    case $badge_type in
        "limited")
            echo "<span class='limitation-badge badge-limited' title='$message'>⚠️ Limited Analysis</span>"
            ;;
        "fallback")
            echo "<span class='limitation-badge badge-fallback' title='$message'>ℹ️ Fallback Mode</span>"
            ;;
        "error")
            echo "<span class='limitation-badge badge-error' title='$message'>❌ Error</span>"
            ;;
        "stats-based")
            echo "<span class='limitation-badge badge-stats-based' title='$message'>📊 Stats-Based</span>"
            ;;
        "pgstattuple")
            echo "<span class='limitation-badge badge-pgstattuple' title='$message'>🔍 Accurate Analysis</span>"
            ;;
        *)
            echo "<span class='limitation-badge badge-limited' title='$message'>ℹ️ $badge_type</span>"
            ;;
    esac
}

# Function to execute SQL with extension dependency checking
execute_sql_with_extension_check() {
    local title="$1"
    local sql="$2"
    local required_extension="$3"
    local fallback_sql="$4"
    local section_level="${5:-h3}"
    local subsection_id="subsection_$(date +%s%N | cut -b1-13)_$(shuf -i 1000-9999 -n 1)"
    
    # Increment query counter
    ((QUERY_COUNT++))
    
    # Display progress indicator to console
    echo "  [$(date '+%H:%M:%S')] Processing query $QUERY_COUNT: $title..."
    
    # Check if required extension is available
    local extension_available=false
    local badge_html=""
    case $required_extension in
        "pg_stat_statements") extension_available=$HAS_PG_STAT_STATEMENTS_DATA ;;
        "pg_buffercache") extension_available=$HAS_PG_BUFFERCACHE ;;
        "pgstattuple") extension_available=$HAS_PGSTATTUPLE ;;
        *) extension_available=true ;; # No extension required
    esac
    
    # Generate badge if using fallback or extension is missing
    if [ "$extension_available" = false ] && [ -n "$fallback_sql" ]; then
        badge_html=$(render_limitation_badge "fallback" "Using fallback query - $required_extension extension not available")
    elif [ "$extension_available" = false ] && [ -z "$fallback_sql" ]; then
        badge_html=$(render_limitation_badge "limited" "Extension required: $required_extension")
    fi
    
    echo "<div class='table-container'>" >> "$OUTPUT_FILE"
    echo "<input type='checkbox' id='$subsection_id' class='accordion-checkbox'>" >> "$OUTPUT_FILE"
    echo "<label for='$subsection_id' class='accordion-label'>" >> "$OUTPUT_FILE"
    echo "<$section_level>$title $badge_html</$section_level>" >> "$OUTPUT_FILE"
    echo "</label>" >> "$OUTPUT_FILE"
    echo "<div class='accordion-content'>" >> "$OUTPUT_FILE"
    
    if [ "$extension_available" = true ]; then
        # Create temporary files for stdout and stderr
        local temp_stdout=$(mktemp)
        local temp_stderr=$(mktemp)
        
        # Execute query and capture both stdout and stderr
        PGPASSWORD="$PGPASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -c "$sql" > "$temp_stdout" 2> "$temp_stderr"
        local exit_code=$?
        
        if [ $exit_code -eq 0 ]; then
            # Query succeeded - display results
            echo "<pre>" >> "$OUTPUT_FILE"
            cat "$temp_stdout" >> "$OUTPUT_FILE"
            echo "</pre>" >> "$OUTPUT_FILE"
            
            # Increment completed query counter
            ((COMPLETED_QUERY_COUNT++))
            echo "  [$(date '+%H:%M:%S')] ✅ Completed: $title (Total: $COMPLETED_QUERY_COUNT/$QUERY_COUNT, Errors: $ERROR_COUNT)"
        else
            # Query failed - capture and display error
            local error_msg=$(cat "$temp_stderr")
            capture_pg_error "$title" "$sql" "$error_msg"
            echo "  [$(date '+%H:%M:%S')] ❌ Failed: $title (Total: $COMPLETED_QUERY_COUNT/$QUERY_COUNT, Errors: $ERROR_COUNT)"
        fi
        
        # Clean up temporary files
        rm -f "$temp_stdout" "$temp_stderr"
    elif [ -n "$fallback_sql" ]; then
        echo "<div class='fallback-notice'>" >> "$OUTPUT_FILE"
        echo "<strong>ℹ️ Fallback Mode:</strong> Extension <code>$required_extension</code> not available. Using alternative query with estimated data." >> "$OUTPUT_FILE"
        echo "</div>" >> "$OUTPUT_FILE"
        
        # Create temporary files for stdout and stderr
        local temp_stdout=$(mktemp)
        local temp_stderr=$(mktemp)
        
        # Execute fallback query and capture both stdout and stderr
        PGPASSWORD="$PGPASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -c "$fallback_sql" > "$temp_stdout" 2> "$temp_stderr"
        local exit_code=$?
        
        if [ $exit_code -eq 0 ]; then
            # Fallback query succeeded - display results
            echo "<pre>" >> "$OUTPUT_FILE"
            cat "$temp_stdout" >> "$OUTPUT_FILE"
            echo "</pre>" >> "$OUTPUT_FILE"
            
            # Increment completed query counter
            ((COMPLETED_QUERY_COUNT++))
            echo "  [$(date '+%H:%M:%S')] ✅ Completed (fallback): $title (Total: $COMPLETED_QUERY_COUNT/$QUERY_COUNT, Errors: $ERROR_COUNT)"
        else
            # Fallback query failed - capture and display error
            local error_msg=$(cat "$temp_stderr")
            capture_pg_error "$title (Fallback)" "$fallback_sql" "$error_msg"
            echo "  [$(date '+%H:%M:%S')] ❌ Failed (fallback): $title (Total: $COMPLETED_QUERY_COUNT/$QUERY_COUNT, Errors: $ERROR_COUNT)"
        fi
        
        # Clean up temporary files
        rm -f "$temp_stdout" "$temp_stderr"
    else
        echo "<div class='limitation-notice'>" >> "$OUTPUT_FILE"
        echo "<strong>⚠️ Extension Required:</strong> <code>$required_extension</code><br>" >> "$OUTPUT_FILE"
        echo "This analysis requires the <code>$required_extension</code> extension to be installed.<br>" >> "$OUTPUT_FILE"
        echo "Install with: <code>CREATE EXTENSION IF NOT EXISTS $required_extension;</code>" >> "$OUTPUT_FILE"
        echo "</div>" >> "$OUTPUT_FILE"
        
        echo "  [$(date '+%H:%M:%S')] ⚠️  Skipped (extension required): $title (Total: $COMPLETED_QUERY_COUNT/$QUERY_COUNT, Errors: $ERROR_COUNT)"
    fi
    
    echo "</div>" >> "$OUTPUT_FILE"
    echo "</div>" >> "$OUTPUT_FILE"
}

# Function to execute SQL and format as PostgreSQL table with accordion structure
execute_sql_to_html() {
    local title="$1"
    local sql="$2"
    local section_level="${3:-h3}"
    
    # Call the extension check function with no extension requirement
    execute_sql_with_extension_check "$title" "$sql" "" "" "$section_level"
}

# 1. DATABASE OVERVIEW
echo ""
echo "=============================================================================="
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting Section 1: DATABASE OVERVIEW"
echo "=============================================================================="
echo '<div class="section">' >> "$OUTPUT_FILE"
echo '<input type="checkbox" id="section1-toggle" class="accordion-checkbox">' >> "$OUTPUT_FILE"
echo '<label for="section1-toggle" class="accordion-label section-header" id="section1"><h2>1. DATABASE OVERVIEW (Connection & Environment)</h2></label>' >> "$OUTPUT_FILE"
echo '<div class="accordion-content">' >> "$OUTPUT_FILE"

execute_sql_to_html "Connection Testing Functions" "
WITH RECURSIVE 
database_stats AS (
    SELECT 
        version() as full_version,  -- This gets the complete version string
        current_setting('server_version') as version,
        current_setting('server_version_num')::integer as version_num,
        current_database() as current_db,
        pg_database_size(current_database()) as db_size,
        current_setting('max_connections') as max_connections,
        pg_postmaster_start_time() as start_time,
        pg_is_in_recovery() as is_replica,
        '$DEPLOYMENT_TYPE'::text as deployment_type,
        EXTRACT(EPOCH FROM (now() - pg_postmaster_start_time())) as uptime_seconds
),
activity_stats AS (
    SELECT 
        COUNT(*) as total_connections,
        COUNT(*) FILTER (WHERE state = 'active') as active_connections,
        COUNT(*) FILTER (WHERE state = 'idle') as idle_connections,
        COUNT(*) FILTER (WHERE state = 'idle in transaction') as idle_in_trans,
        COUNT(DISTINCT usename) as unique_users,
        MAX(EXTRACT(EPOCH FROM (now() - backend_start)))::integer as oldest_connection_seconds
    FROM pg_stat_activity 
    WHERE pid != pg_backend_pid()
),
encoding_info AS (
    SELECT 
        current_setting('client_encoding') as client_encoding,
        current_setting('server_encoding') as server_encoding,
        current_setting('timezone') as timezone
),
extension_stats AS (
    SELECT 
        COUNT(*) as total_extensions,
        string_agg(extname, ', ' ORDER BY extname) as installed_extensions,
        string_agg(extname || ' (' || extversion || ')', ', ' ORDER BY extname) as extension_versions,
        COUNT(*) FILTER (WHERE extname = 'pg_stat_statements') > 0 as has_pg_stat_statements
    FROM pg_extension
),
wal_access AS (
    SELECT 
        CASE 
            WHEN EXISTS (
                SELECT 1 
                FROM pg_proc 
                WHERE proname = 'pg_ls_waldir'
                AND has_function_privilege(oid, 'execute')
            ) THEN 'Available'
            ELSE 'Not Available'
        END as wal_dir_access
)
SELECT 
    metric,
    value,
    description,
    recommendation
FROM (
    -- Complete PostgreSQL Version Information
    SELECT 
        'PostgreSQL Version' as metric,
        full_version as value,
        'Complete PostgreSQL version information' as description,
        CASE 
            WHEN version_num < 100000 THEN 'WARNING: Consider upgrading to a newer version'
            WHEN version_num < 130000 THEN 'Consider upgrading to latest stable version'
            ELSE 'OK - Running recent version'
        END as recommendation
    FROM database_stats

    UNION ALL

    -- Client Encoding
    SELECT 
        'Client Encoding',
        client_encoding,
        'Current client connection encoding',
        CASE 
            WHEN client_encoding != server_encoding THEN 'WARNING: Client and server encoding mismatch'
            ELSE 'OK'
        END
    FROM encoding_info

    UNION ALL

    SELECT 
        'Server Encoding',
        server_encoding,
        'Database server character encoding',
        CASE 
            WHEN server_encoding != 'UTF8' THEN 'Consider using UTF8 for better compatibility'
            ELSE 'OK'
        END
    FROM encoding_info

    UNION ALL

    SELECT 
        'pg_stat_statements',
        CASE WHEN has_pg_stat_statements THEN 'Installed' ELSE 'Not Installed' END,
        'Query performance analysis extension',
        CASE 
            WHEN NOT has_pg_stat_statements THEN 'Consider installing for query performance monitoring'
            ELSE 'OK - Extension available'
        END
    FROM extension_stats


    UNION ALL

    SELECT
        'Extension Inventory',
        total_extensions || ' extensions: ' || extension_versions,
        'Installed extensions with versions',
        CASE
            WHEN total_extensions = 0 THEN 'WARNING: No extensions installed'
            ELSE 'OK - ' || total_extensions || ' extension(s) installed'
        END
    FROM extension_stats
    UNION ALL

    SELECT 
        'Server Uptime (seconds)',
        uptime_seconds::text,
        format('Up since: %s', start_time::timestamp(0)),
        CASE 
            WHEN uptime_seconds < 300 THEN 'WARNING: Recent server restart'
            WHEN uptime_seconds < 86400 THEN 'Server restarted within last 24 hours'
            ELSE 'OK - Server stable'
        END
    FROM database_stats

    UNION ALL

    SELECT 
        'Timezone',
        timezone,
        'Current timezone setting',
        CASE 
            WHEN timezone = 'GMT' THEN 'Consider setting to your local timezone'
            ELSE 'OK'
        END
    FROM encoding_info

    UNION ALL

    SELECT 
        'WAL Directory Access',
        wal_dir_access,
        'Access to WAL directory functions',
        CASE 
            WHEN wal_dir_access = 'Not Available' THEN 'Limited WAL monitoring capability'
            ELSE 'OK - WAL directory accessible'
        END
    FROM wal_access

) metrics
ORDER BY 
    CASE 
        WHEN recommendation LIKE 'WARNING%' THEN 1
        WHEN recommendation LIKE 'Consider%' THEN 2
        WHEN metric IN (
            'PostgreSQL Version',
            'Client Encoding',
            'Server Encoding',
            'pg_stat_statements',
            'Server Uptime (seconds)',
            'Timezone',
            'WAL Directory Access'
        ) THEN 3
        ELSE 4
    END,
    metric;"




execute_sql_to_html "Database Workload Profile" "
WITH db_stats AS (
    SELECT
        tup_returned + tup_fetched AS total_reads,
        tup_inserted + tup_updated + tup_deleted AS total_writes,
        tup_returned,
        tup_fetched,
        tup_inserted,
        tup_updated,
        tup_deleted,
        xact_commit,
        xact_rollback,
        temp_files,
        stats_reset,
        EXTRACT(EPOCH FROM (now() - COALESCE(stats_reset, pg_postmaster_start_time()))) AS stats_seconds
    FROM pg_stat_database
    WHERE datname = current_database()
)
SELECT
    metric,
    value,
    description,
    recommendation
FROM (
    SELECT
        'Workload Classification' AS metric,
        CASE
            WHEN total_reads + total_writes = 0 THEN 'NO ACTIVITY'
            WHEN stats_seconds < 3600 THEN 'INSUFFICIENT DATA'
            WHEN total_reads::numeric / NULLIF(total_reads + total_writes, 0) >= 0.80 THEN
                'READ-HEAVY (' || ROUND(100.0 * total_reads / NULLIF(total_reads + total_writes, 0), 1) || '% reads)'
            WHEN total_writes::numeric / NULLIF(total_reads + total_writes, 0) >= 0.60 THEN
                'WRITE-HEAVY (' || ROUND(100.0 * total_writes / NULLIF(total_reads + total_writes, 0), 1) || '% writes)'
            ELSE
                'MIXED (' || ROUND(100.0 * total_reads / NULLIF(total_reads + total_writes, 0), 1) || '% reads)'
        END AS value,
        ROUND(100.0 * total_reads / NULLIF(total_reads + total_writes, 0), 1) || '% reads / ' ||
        ROUND(100.0 * total_writes / NULLIF(total_reads + total_writes, 0), 1) || '% writes since stats reset' AS description,
        CASE
            WHEN stats_seconds < 3600 THEN 'Allow at least 24 hours of activity before classifying'
            WHEN total_reads::numeric / NULLIF(total_reads + total_writes, 0) >= 0.80 THEN
                'Optimize for reads: shared_buffers, effective_cache_size, random_page_cost'
            WHEN total_writes::numeric / NULLIF(total_reads + total_writes, 0) >= 0.60 THEN
                'Optimize for writes: max_wal_size, checkpoint_timeout, wal_compression'
            ELSE 'Balance read/write: review shared_buffers, work_mem, WAL settings'
        END AS recommendation,
        1 AS sort_order
    FROM db_stats

    UNION ALL

    SELECT
        'Total Reads',
        pg_size_pretty(total_reads) || ' rows',
        'tup_fetched: ' || pg_size_pretty(tup_fetched) || ' + tup_returned: ' || pg_size_pretty(tup_returned),
        CASE WHEN total_reads = 0 THEN 'INFO - No read activity' ELSE 'OK' END,
        2
    FROM db_stats

    UNION ALL

    SELECT
        'Total Writes',
        pg_size_pretty(total_writes) || ' rows',
        'Inserts: ' || pg_size_pretty(tup_inserted) || ' | Updates: ' || pg_size_pretty(tup_updated) || ' | Deletes: ' || pg_size_pretty(tup_deleted),
        CASE
            WHEN tup_updated > tup_inserted AND tup_updated > 1000000 THEN 'High update volume - monitor bloat and autovacuum'
            WHEN tup_inserted > 1000000000 THEN 'High insert volume - monitor WAL generation and autovacuum'
            ELSE 'OK'
        END,
        3
    FROM db_stats

    UNION ALL

    SELECT
        'Transactions Committed',
        xact_commit::text,
        'Total committed since stats reset',
        'OK',
        4
    FROM db_stats

    UNION ALL

    SELECT
        'Transactions Rolled Back',
        xact_rollback::text || ' (' || ROUND(100.0 * xact_rollback / NULLIF(xact_commit + xact_rollback, 0), 2) || '%)',
        'Rollback ratio relative to total transactions',
        CASE
            WHEN ROUND(100.0 * xact_rollback / NULLIF(xact_commit + xact_rollback, 0), 2) > 5 THEN
                'CRITICAL - Rollback ratio above 5%'
            WHEN ROUND(100.0 * xact_rollback / NULLIF(xact_commit + xact_rollback, 0), 2) > 2 THEN
                'WARNING - Rollback ratio above 2%, investigate application error handling'
            ELSE 'OK - Below 2%'
        END,
        5
    FROM db_stats

    UNION ALL

    SELECT
        'Temp Files Created',
        temp_files::text,
        'Queries that spilled to disk',
        CASE
            WHEN temp_files > 100000 THEN 'WARNING - High temp file usage, increase work_mem'
            WHEN temp_files > 10000 THEN 'INFO - Review work_mem if count is growing'
            ELSE 'OK'
        END,
        6
    FROM db_stats

    UNION ALL

    SELECT
        'Stats Since',
        COALESCE(to_char(stats_reset, 'YYYY-MM-DD'), 'Never reset (since server start)') ||
        ' (' || ROUND(stats_seconds / 86400) || ' days ago)',
        'Last pg_stat_database reset',
        CASE
            WHEN stats_seconds < 86400 THEN 'WARNING - Stats recently reset, profile may not be representative'
            WHEN stats_seconds > 2592000 THEN 'Good monitoring window (' || ROUND(stats_seconds / 86400) || ' days)'
            ELSE 'OK'
        END,
        7
    FROM db_stats
) workload_metrics
ORDER BY sort_order;"

echo '</div>' >> "$OUTPUT_FILE"
echo '</div>' >> "$OUTPUT_FILE"

# 2. SYSTEM CONFIGURATION
echo ""
echo "=============================================================================="
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting Section 2: SYSTEM CONFIGURATION"
echo "=============================================================================="
echo '<div class="section">' >> "$OUTPUT_FILE"
echo '<input type="checkbox" id="section2-toggle" class="accordion-checkbox">' >> "$OUTPUT_FILE"
echo '<label for="section2-toggle" class="accordion-label section-header" id="section2"><h2>2. SYSTEM CONFIGURATION (Environment Setup)</h2></label>' >> "$OUTPUT_FILE"
echo '<div class="accordion-content">' >> "$OUTPUT_FILE"

execute_sql_to_html "PostgreSQL Parameters" "
WITH deployment_context AS (
    SELECT '$DEPLOYMENT_TYPE'::text as deployment_type
),
recommended_settings AS (
    -- =====================================================
    -- EC2 / On-Prem / Self-Managed Parameters (full control)
    -- =====================================================
    SELECT 'shared_buffers' as name, 'Set to 25% of total RAM' as recommendation,
        'Critical' as priority, 'Memory' as category, 'EC2' as applicable_to,
        'Primary shared memory buffer pool - most impactful memory setting' as description
    UNION ALL SELECT 'effective_cache_size', 'Set to 75% of total RAM',
        'Critical', 'Memory', 'EC2', 'Planner estimate of total cache available (OS + shared buffers)'
    UNION ALL SELECT 'work_mem', 'Start at 4MB, increase for complex sorts/joins',
        'High', 'Memory', 'EC2', 'Memory per sort/hash operation - multiplied by concurrent operations'
    UNION ALL SELECT 'maintenance_work_mem', '256MB-1GB based on table sizes',
        'High', 'Memory', 'EC2', 'Memory for VACUUM, CREATE INDEX, ALTER TABLE operations'
    UNION ALL SELECT 'huge_pages', 'Set to try for large instances (>32GB RAM)',
        'High', 'Memory', 'EC2', 'Use huge pages for shared memory - reduces TLB misses'
    UNION ALL SELECT 'max_connections', 'Based on workload - use connection pooling if >200',
        'Critical', 'Connection', 'EC2', 'Maximum concurrent connections - each uses ~10MB RAM'
    UNION ALL SELECT 'autovacuum', 'Must be on - never disable',
        'Critical', 'Vacuum', 'EC2', 'Automatic dead tuple cleanup - critical for performance'
    UNION ALL SELECT 'autovacuum_max_workers', 'Set to CPU cores / 4 (minimum 3)',
        'High', 'Vacuum', 'EC2', 'Parallel vacuum workers - increase for write-heavy workloads'
    UNION ALL SELECT 'autovacuum_vacuum_scale_factor', '0.1 for large tables (default 0.2)',
        'High', 'Vacuum', 'EC2', 'Fraction of table that triggers vacuum - lower = more frequent'
    UNION ALL SELECT 'max_wal_size', '2-4GB for write-heavy workloads',
        'High', 'WAL', 'EC2', 'Maximum WAL size between checkpoints - controls checkpoint frequency'
    UNION ALL SELECT 'checkpoint_completion_target', 'Set to 0.9 for better I/O spreading',
        'High', 'Checkpoint', 'EC2', 'Spreads checkpoint writes over longer period'
    UNION ALL SELECT 'random_page_cost', '1.1 for SSD storage, 4.0 for HDD',
        'High', 'Query Planning', 'EC2', 'Cost of random page access - critical for index usage decisions'
    UNION ALL SELECT 'effective_io_concurrency', '200 for SSD storage',
        'High', 'Query Planning', 'EC2', 'Concurrent I/O operations - set high for SSD/NVMe'
    UNION ALL SELECT 'max_parallel_workers_per_gather', 'Set to CPU cores / 2',
        'High', 'Parallel Query', 'EC2', 'Parallel workers per query node - speeds up large scans'
    UNION ALL SELECT 'fsync', 'Must be on - never disable in production',
        'Critical', 'Durability', 'EC2', 'Force WAL sync to disk - disabling risks data corruption'
    UNION ALL SELECT 'full_page_writes', 'Must be on for crash recovery',
        'Critical', 'Durability', 'EC2', 'Write full pages after checkpoint - prevents partial writes'
    UNION ALL SELECT 'synchronous_commit', 'on for durability, off only if data loss acceptable',
        'Critical', 'Durability', 'EC2', 'WAL commit behavior - off gives 3x write speed but risks data'
    UNION ALL SELECT 'ssl', 'Enable for encrypted connections',
        'Critical', 'Security', 'EC2', 'SSL/TLS encryption for client connections'
    UNION ALL SELECT 'password_encryption', 'scram-sha-256 (PostgreSQL 10+)',
        'Critical', 'Security', 'EC2', 'Password hashing method - scram-sha-256 is most secure'
    UNION ALL SELECT 'shared_preload_libraries', 'pg_stat_statements,auto_explain recommended',
        'High', 'Extensions', 'EC2', 'Libraries loaded at server start for monitoring'
    UNION ALL SELECT 'log_min_duration_statement', '1000ms for slow query logging',
        'High', 'Logging', 'EC2', 'Log queries slower than threshold - essential for tuning'
    UNION ALL SELECT 'track_io_timing', 'Enable for I/O performance monitoring',
        'High', 'Logging', 'EC2', 'Track I/O timing in pg_stat_statements - small overhead'
    UNION ALL SELECT 'statement_timeout', 'Set based on application (e.g. 300000 = 5min)',
        'High', 'Statement', 'EC2', 'Kill queries exceeding this duration - prevents runaway queries'
    UNION ALL SELECT 'idle_in_transaction_session_timeout', 'Set to 7200000 (2 hours)',
        'High', 'Statement', 'EC2', 'Kill idle-in-transaction sessions that hold locks'

    -- =====================================================
    -- RDS Parameters (parameter group tunable)
    -- =====================================================
    UNION ALL SELECT 'shared_buffers', '{DBInstanceClassMemory/32768} - auto-scaled by instance class',
        'High', 'Memory', 'RDS', 'Managed via parameter formula - verify adequate for workload'
    UNION ALL SELECT 'effective_cache_size', '{DBInstanceClassMemory/16384} - auto-scaled',
        'Medium', 'Memory', 'RDS', 'Planner cache estimate - auto-tuned but verify'
    UNION ALL SELECT 'work_mem', 'Start at 4MB, tune based on EXPLAIN ANALYZE',
        'High', 'Memory', 'RDS', 'Per-operation sort/hash memory - tunable in parameter group'
    UNION ALL SELECT 'maintenance_work_mem', 'Increase from 64MB default for large tables',
        'High', 'Memory', 'RDS', 'Vacuum/index build memory - tunable in parameter group'
    UNION ALL SELECT 'max_connections', 'Instance-class default - use RDS Proxy for pooling',
        'High', 'Connection', 'RDS', 'Tunable but limited by instance memory - prefer RDS Proxy'
    UNION ALL SELECT 'autovacuum', 'Must be on - verify in parameter group',
        'Critical', 'Vacuum', 'RDS', 'Tunable - ensure enabled in custom parameter group'
    UNION ALL SELECT 'autovacuum_max_workers', 'Instance-class based (3-4 typical)',
        'High', 'Vacuum', 'RDS', 'Tunable - increase for write-heavy workloads'
    UNION ALL SELECT 'autovacuum_vacuum_scale_factor', '0.1 for large tables',
        'High', 'Vacuum', 'RDS', 'Tunable - lower for large tables with frequent updates'
    UNION ALL SELECT 'rds.force_ssl', 'Set to 1 to enforce encrypted connections',
        'Critical', 'Security', 'RDS', 'RDS-specific - forces all connections to use SSL'
    UNION ALL SELECT 'rds.logical_replication', 'Set to 1 if using logical replication/DMS',
        'Medium', 'Replication', 'RDS', 'Required for logical replication and AWS DMS'
    UNION ALL SELECT 'random_page_cost', '1.1 (RDS uses SSD/gp3 storage)',
        'High', 'Query Planning', 'RDS', 'Tunable - set low for SSD-backed storage'
    UNION ALL SELECT 'effective_io_concurrency', '200 for gp3/io1 storage',
        'High', 'Query Planning', 'RDS', 'Tunable - set high for provisioned IOPS'
    UNION ALL SELECT 'max_parallel_workers_per_gather', 'Instance vCPU based',
        'High', 'Parallel Query', 'RDS', 'Tunable - scale with instance vCPUs'
    UNION ALL SELECT 'shared_preload_libraries', 'pg_stat_statements,auto_explain',
        'Critical', 'Extensions', 'RDS', 'Tunable via parameter group - requires reboot'
    UNION ALL SELECT 'log_min_duration_statement', '1000ms for slow query capture',
        'High', 'Logging', 'RDS', 'Tunable - feeds into Performance Insights'
    UNION ALL SELECT 'track_io_timing', 'Enable for Performance Insights integration',
        'High', 'Logging', 'RDS', 'Tunable - enhances RDS Performance Insights data'
    UNION ALL SELECT 'statement_timeout', 'Set based on application requirements',
        'High', 'Statement', 'RDS', 'Tunable - prevents long-running query issues'
    UNION ALL SELECT 'idle_in_transaction_session_timeout', '7200000 recommended',
        'High', 'Statement', 'RDS', 'Tunable - critical for connection management'
    UNION ALL SELECT 'password_encryption', 'scram-sha-256 (PostgreSQL 14+ on RDS)',
        'Critical', 'Security', 'RDS', 'Tunable - upgrade from md5 for better security'

    -- =====================================================
    -- Aurora Parameters (storage-decoupled, most managed)
    -- =====================================================
    UNION ALL SELECT 'shared_buffers', 'Aurora-managed via instance class - verify sizing',
        'Medium', 'Memory', 'Aurora', 'Auto-managed but visible - verify adequate for workload'
    UNION ALL SELECT 'work_mem', 'Start at 4MB, tune for complex analytical queries',
        'High', 'Memory', 'Aurora', 'Tunable - same guidance as standard PostgreSQL'
    UNION ALL SELECT 'maintenance_work_mem', 'Increase for large table maintenance',
        'High', 'Memory', 'Aurora', 'Tunable - important for vacuum on large tables'
    UNION ALL SELECT 'max_connections', 'Instance-class scaled - use Aurora connection pooling',
        'High', 'Connection', 'Aurora', 'Tunable but prefer built-in connection management'
    UNION ALL SELECT 'autovacuum', 'Must be on - Aurora storage still needs tuple cleanup',
        'Critical', 'Vacuum', 'Aurora', 'Tunable - dead tuples still impact query performance'
    UNION ALL SELECT 'autovacuum_vacuum_scale_factor', '0.1 for large tables',
        'High', 'Vacuum', 'Aurora', 'Tunable - Aurora storage does not auto-reclaim dead tuples'
    UNION ALL SELECT 'rds.force_ssl', 'Set to 1 to enforce encrypted connections',
        'Critical', 'Security', 'Aurora', 'Enforce SSL on all Aurora connections'
    UNION ALL SELECT 'random_page_cost', '1.1 (Aurora uses distributed SSD storage)',
        'Medium', 'Query Planning', 'Aurora', 'Tunable - Aurora storage is SSD-based'
    UNION ALL SELECT 'effective_io_concurrency', '200 (Aurora has high I/O throughput)',
        'Medium', 'Query Planning', 'Aurora', 'Tunable - Aurora storage handles high concurrency'
    UNION ALL SELECT 'max_parallel_workers_per_gather', 'Instance vCPU based',
        'Medium', 'Parallel Query', 'Aurora', 'Tunable - leverage Aurora read replicas for scaling'
    UNION ALL SELECT 'shared_preload_libraries', 'pg_stat_statements,auto_explain',
        'Critical', 'Extensions', 'Aurora', 'Tunable via cluster parameter group'
    UNION ALL SELECT 'log_min_duration_statement', '1000ms for Performance Insights',
        'High', 'Logging', 'Aurora', 'Tunable - integrates with Aurora Performance Insights'
    UNION ALL SELECT 'track_io_timing', 'Enable for Aurora monitoring integration',
        'High', 'Logging', 'Aurora', 'Tunable - enhances CloudWatch and PI metrics'
    UNION ALL SELECT 'statement_timeout', 'Set based on application SLAs',
        'Critical', 'Statement', 'Aurora', 'Tunable - prevents runaway queries on shared cluster'
    UNION ALL SELECT 'idle_in_transaction_session_timeout', '7200000 recommended',
        'Critical', 'Statement', 'Aurora', 'Tunable - critical for Aurora connection management'
    UNION ALL SELECT 'aurora_lab_mode', 'Enable for experimental Aurora features',
        'Low', 'Aurora Specific', 'Aurora', 'Access preview features - not for production'
    UNION ALL SELECT 'password_encryption', 'scram-sha-256 recommended',
        'Critical', 'Security', 'Aurora', 'Tunable - use strongest available method'

    -- =====================================================
    -- Aurora Serverless v2 Parameters
    -- Tunable: Parameters you can and should configure
    -- ACU-Managed: Dynamically managed by ACU scaling - shown for visibility
    -- =====================================================

    -- [ACU-Managed] Memory parameters - dynamically scaled with ACU allocation
    UNION ALL SELECT 'shared_buffers', 'ACU-Managed - scales dynamically with ACU allocation',
        'Info', 'Memory', 'Aurora Serverless', '[ACU-Managed] Auto-scaled with ACUs. Verify adequate for workload at min ACU setting'
    UNION ALL SELECT 'effective_cache_size', 'ACU-Managed - adjusted by Aurora based on available memory',
        'Info', 'Memory', 'Aurora Serverless', '[ACU-Managed] Planner estimate auto-adjusted. Review if query plans are suboptimal at low ACUs'

    -- [Tunable] Memory parameters - you control these
    UNION ALL SELECT 'work_mem', 'Start at 4MB, tune carefully - memory scales with ACUs',
        'High', 'Memory', 'Aurora Serverless', '[Tunable] Per-operation sort/hash memory. Be conservative - total memory fluctuates with ACU scaling'
    UNION ALL SELECT 'maintenance_work_mem', 'Keep moderate - ACU scaling affects available memory',
        'Medium', 'Memory', 'Aurora Serverless', '[Tunable] Vacuum/index build memory. Set conservatively for min ACU level'

    -- [ACU-Managed] Connection parameters
    UNION ALL SELECT 'max_connections', 'ACU-Managed - scales with ACU allocation',
        'Info', 'Connection', 'Aurora Serverless', '[ACU-Managed] Dynamically set based on ACUs. If hitting limits, increase min ACU or use RDS Proxy'

    -- [Tunable] Vacuum parameters - critical regardless of scaling
    UNION ALL SELECT 'autovacuum', 'Must be on - critical even on Serverless',
        'Critical', 'Vacuum', 'Aurora Serverless', '[Tunable] Dead tuples impact performance regardless of ACU scaling'
    UNION ALL SELECT 'autovacuum_vacuum_scale_factor', '0.1 for large tables',
        'High', 'Vacuum', 'Aurora Serverless', '[Tunable] Lower for large tables - vacuum efficiency matters more with variable compute'

    -- [Tunable] Security parameters
    UNION ALL SELECT 'rds.force_ssl', 'Set to 1 to enforce encrypted connections',
        'Critical', 'Security', 'Aurora Serverless', '[Tunable] Enforce SSL on all Serverless connections'
    UNION ALL SELECT 'password_encryption', 'scram-sha-256 recommended',
        'Critical', 'Security', 'Aurora Serverless', '[Tunable] Use strongest available authentication method'

    -- [ACU-Managed] Query planning parameters
    UNION ALL SELECT 'random_page_cost', 'ACU-Managed - Aurora storage handles I/O optimization',
        'Info', 'Query Planning', 'Aurora Serverless', '[ACU-Managed] Aurora distributed storage manages I/O. Value shown for query plan analysis'
    UNION ALL SELECT 'effective_io_concurrency', 'ACU-Managed - Aurora storage manages I/O concurrency',
        'Info', 'Query Planning', 'Aurora Serverless', '[ACU-Managed] Aurora storage layer handles concurrency. Shown for visibility'
    UNION ALL SELECT 'max_parallel_workers_per_gather', 'ACU-Managed - parallel capacity scales with ACUs',
        'Info', 'Parallel Query', 'Aurora Serverless', '[ACU-Managed] Parallel workers depend on available ACUs. Review if parallel queries underperform'

    -- [Tunable] Extensions and monitoring - critical for serverless observability
    UNION ALL SELECT 'shared_preload_libraries', 'pg_stat_statements recommended',
        'Critical', 'Extensions', 'Aurora Serverless', '[Tunable] Requires reboot. Essential for monitoring query patterns during ACU scaling'
    UNION ALL SELECT 'log_min_duration_statement', '1000ms for Performance Insights',
        'Critical', 'Logging', 'Aurora Serverless', '[Tunable] Critical for identifying queries that trigger ACU scale-up events'
    UNION ALL SELECT 'track_io_timing', 'Enable for monitoring',
        'High', 'Logging', 'Aurora Serverless', '[Tunable] Helps identify I/O patterns that drive ACU scaling decisions'

    -- [Tunable] Statement controls - especially important on shared serverless compute
    UNION ALL SELECT 'statement_timeout', 'Set based on application SLAs',
        'Critical', 'Statement', 'Aurora Serverless', '[Tunable] Prevents runaway queries that waste ACU capacity and budget'
    UNION ALL SELECT 'idle_in_transaction_session_timeout', '3600000 (1 hour) recommended',
        'Critical', 'Statement', 'Aurora Serverless', '[Tunable] Idle transactions hold ACU capacity and prevent scale-down'
)
SELECT
    dc.deployment_type as detected_deployment,
    rs.name as parameter_name,
    COALESCE(ps.setting, 'N/A') as current_value,
    CASE
        WHEN ps.name IN ('shared_buffers', 'work_mem', 'maintenance_work_mem', 'effective_cache_size', 'temp_file_limit', 'max_wal_size', 'min_wal_size')
        THEN COALESCE(pg_size_pretty(
            ps.setting::bigint *
            CASE
                WHEN ps.unit = '8kB' THEN 8192
                WHEN ps.unit = 'kB' THEN 1024
                WHEN ps.unit = 'MB' THEN 1048576
                WHEN ps.unit = 'GB' THEN 1073741824
                ELSE 1
            END
        ), ps.setting || COALESCE(' ' || ps.unit, ''))
        ELSE COALESCE(ps.setting || COALESCE(' ' || NULLIF(ps.unit, ''), ''), 'N/A')
    END as readable_value,
    COALESCE(ps.boot_val, '-') as default_value,
    rs.recommendation,
    rs.priority as tuning_priority,
    rs.category as parameter_type,
    rs.description,
    COALESCE(ps.context, '-') as change_requires,
    CASE
        WHEN ps.setting IS NULL THEN 'N/A - Parameter not found'
        -- Critical security checks
        WHEN ps.name = 'autovacuum' AND ps.setting = 'off'
            THEN 'CRITICAL: Autovacuum disabled - enable immediately'
        WHEN ps.name = 'ssl' AND ps.setting = 'off'
            THEN 'CRITICAL: SSL disabled - security risk'
        WHEN ps.name = 'rds.force_ssl' AND ps.setting = '0'
            THEN 'CRITICAL: SSL not enforced'
        WHEN ps.name = 'fsync' AND ps.setting = 'off'
            THEN 'CRITICAL: fsync disabled - data corruption risk'
        WHEN ps.name = 'full_page_writes' AND ps.setting = 'off'
            THEN 'CRITICAL: Partial write protection disabled'
        -- Memory checks
        WHEN ps.name = 'shared_buffers' AND ps.setting::bigint < 1048576 AND dc.deployment_type = 'EC2'
            THEN 'WARNING: Likely too small for production'
        WHEN ps.name = 'work_mem' AND ps.setting::bigint > 262144
            THEN 'WARNING: Very high - monitor memory pressure'
        -- Performance checks
        WHEN ps.name = 'random_page_cost' AND ps.setting::numeric > 2.0
            THEN 'WARNING: Too high for SSD - set to 1.1'
        WHEN ps.name = 'effective_io_concurrency' AND ps.setting::int < 100
            THEN 'WARNING: Increase for SSD storage'
        WHEN ps.name = 'max_connections' AND ps.setting::int > 1000
            THEN 'WARNING: Very high - use connection pooling'
        WHEN ps.name = 'track_io_timing' AND ps.setting = 'off'
            THEN 'INFO: Enable for I/O monitoring'
        WHEN ps.name = 'log_checkpoints' AND ps.setting = 'off'
            THEN 'INFO: Enable for checkpoint monitoring'
        WHEN ps.name = 'shared_preload_libraries' AND ps.setting NOT LIKE '%pg_stat_statements%'
            THEN 'WARNING: pg_stat_statements not loaded'
        WHEN ps.name = 'password_encryption' AND ps.setting = 'md5'
            THEN 'WARNING: Upgrade to scram-sha-256'
        ELSE 'OK'
    END as health_status
FROM recommended_settings rs
CROSS JOIN deployment_context dc
LEFT JOIN pg_settings ps ON ps.name = rs.name
WHERE rs.applicable_to = dc.deployment_type
    AND (rs.name != 'wal_keep_segments' OR current_setting('server_version_num')::integer < 130000)
    AND (rs.name != 'wal_keep_size' OR current_setting('server_version_num')::integer >= 130000)
    AND (rs.name != 'idle_session_timeout' OR current_setting('server_version_num')::integer >= 140000)
ORDER BY
    CASE rs.priority
        WHEN 'Critical' THEN 1
        WHEN 'High' THEN 2
        WHEN 'Medium' THEN 3
        WHEN 'Low' THEN 4
        WHEN 'Info' THEN 5
    END,
    rs.category,
    rs.name;"

# --- Security & Access Control Sub-sections (REQ-6.x, REQ-11.9) ---

# 2.1 User & Role Analysis (REQ-6.1)
execute_sql_to_html "User & Role Analysis" "
SELECT
    rolname AS role_name,
    CASE WHEN rolsuper THEN 'YES' ELSE 'no' END AS is_superuser,
    CASE WHEN rolcreatedb THEN 'YES' ELSE 'no' END AS can_create_db,
    CASE WHEN rolcreaterole THEN 'YES' ELSE 'no' END AS can_create_role,
    CASE WHEN rolcanlogin THEN 'YES' ELSE 'no' END AS can_login,
    CASE WHEN rolreplication THEN 'YES' ELSE 'no' END AS replication,
    CASE WHEN rolbypassrls THEN 'YES' ELSE 'no' END AS bypass_rls,
    rolconnlimit AS connection_limit,
    CASE
        -- AWS-managed service accounts (RDS/Aurora) - expected, no action required
        WHEN rolname = 'rdsadmin' THEN 'AWS-MANAGED - RDS internal service account (expected, no action required)'
        WHEN rolname = 'rds_superuser' THEN 'AWS-MANAGED - RDS admin role granted to master user (expected)'
        WHEN rolname = 'rds_replication' THEN 'AWS-MANAGED - RDS replication role (expected for HA)'
        WHEN rolname = 'rds_password' THEN 'AWS-MANAGED - RDS password management role (expected)'
        WHEN rolname = 'rdsrepladmin' THEN 'AWS-MANAGED - RDS replication admin role (expected)'
        WHEN rolname LIKE 'rds%' THEN 'AWS-MANAGED - RDS/Aurora system role (expected, no action required)'
        -- Standard PostgreSQL risk assessment
        WHEN rolsuper THEN 'CRITICAL - Superuser has unrestricted access'
        WHEN rolcreaterole AND rolcreatedb THEN 'HIGH - Can create roles and databases'
        WHEN rolcreaterole THEN 'MEDIUM - Can create roles'
        WHEN rolcreatedb THEN 'LOW - Can create databases'
        WHEN rolreplication THEN 'MEDIUM - Replication privilege'
        WHEN rolbypassrls THEN 'HIGH - Can bypass row-level security'
        ELSE 'OK'
    END AS risk_level
FROM pg_roles
WHERE rolname NOT LIKE 'pg_%'
ORDER BY
    CASE
        WHEN rolsuper THEN 1
        WHEN rolcreaterole AND rolcreatedb THEN 2
        WHEN rolcreaterole THEN 3
        WHEN rolbypassrls THEN 4
        WHEN rolreplication THEN 5
        WHEN rolcreatedb THEN 6
        ELSE 7
    END,
    rolname
LIMIT 200;"

# 2.2 Privilege Audit (REQ-6.2)
execute_sql_to_html "Privilege Audit" "
WITH table_privs AS (
    SELECT
        grantee,
        table_schema,
        table_name,
        privilege_type,
        is_grantable
    FROM information_schema.table_privileges
    WHERE grantee NOT IN (SELECT rolname FROM pg_roles WHERE rolname LIKE 'pg_%')
      AND grantee NOT LIKE 'rds%'
),
ranked_tables AS (
    SELECT
        grantee,
        table_schema,
        table_name,
        ROW_NUMBER() OVER (PARTITION BY grantee, table_schema ORDER BY table_name) as rn
    FROM (SELECT DISTINCT grantee, table_schema, table_name FROM table_privs) dt
),
privilege_summary AS (
    SELECT
        tp.grantee,
        tp.table_schema,
        COUNT(DISTINCT tp.table_name) as table_count,
        -- First 200 table names aggregated
        (SELECT string_agg(rt.table_name, ', ' ORDER BY rt.table_name)
         FROM ranked_tables rt
         WHERE rt.grantee = tp.grantee AND rt.table_schema = tp.table_schema AND rt.rn <= 200
        ) as table_list,
        string_agg(DISTINCT tp.privilege_type, ', ' ORDER BY tp.privilege_type) as privileges,
        COUNT(*) as total_grants,
        COUNT(*) FILTER (WHERE tp.is_grantable = 'YES') as grantable_count,
        bool_or(tp.is_grantable = 'YES') as has_grantable
    FROM table_privs tp
    GROUP BY tp.grantee, tp.table_schema
)
SELECT
    grantee,
    table_schema as schema,
    table_count::text || ' tables' as scope,
    CASE
        WHEN table_count <= 200 THEN table_list
        ELSE table_list || ' ... +' || (table_count - 200)::text || ' more'
    END as tables,
    privileges,
    total_grants::text || ' grants' || CASE WHEN grantable_count > 0 THEN ' (' || grantable_count || ' grantable)' ELSE '' END as grant_summary,
    CASE
        WHEN grantee = 'PUBLIC' THEN 'WARNING - PUBLIC role has grants on ' || table_count || ' tables in ' || table_schema
        WHEN has_grantable AND privileges LIKE '%INSERT%' AND privileges LIKE '%DELETE%' THEN 'HIGH - Full DML with grant option on ' || table_count || ' tables'
        WHEN privileges LIKE '%INSERT%' AND privileges LIKE '%UPDATE%' AND privileges LIKE '%DELETE%' THEN 'MEDIUM - Full DML access on ' || table_count || ' tables'
        WHEN has_grantable THEN 'INFO - Has grant option (can delegate privileges)'
        WHEN table_count > 50 THEN 'INFO - Broad access across ' || table_count || ' tables'
        ELSE 'OK'
    END as risk_level
FROM privilege_summary
ORDER BY
    CASE
        WHEN grantee = 'PUBLIC' THEN 0
        WHEN has_grantable AND privileges LIKE '%INSERT%' AND privileges LIKE '%DELETE%' THEN 1
        WHEN privileges LIKE '%INSERT%' AND privileges LIKE '%UPDATE%' AND privileges LIKE '%DELETE%' THEN 2
        WHEN has_grantable THEN 3
        ELSE 4
    END,
    grantee,
    table_schema
LIMIT 50;"

# 2.3 SSL Connection Analysis (REQ-6.3)
execute_sql_to_html "SSL Connection Analysis" "
WITH ssl_summary AS (
    SELECT
        sa.usename,
        sa.datname,
        ss.ssl as is_ssl,
        COALESCE(ss.version, 'N/A') as ssl_version,
        COALESCE(ss.cipher, 'N/A') as ssl_cipher,
        COALESCE(ss.bits, 0) as cipher_bits,
        sa.client_addr IS NULL as is_local,
        COUNT(*) as connection_count
    FROM pg_stat_ssl ss
    JOIN pg_stat_activity sa ON ss.pid = sa.pid
    WHERE sa.pid IS NOT NULL
    GROUP BY sa.usename, sa.datname, ss.ssl, ss.version, ss.cipher, ss.bits, (sa.client_addr IS NULL)
)
SELECT
    usename as username,
    datname as database,
    connection_count as connections,
    CASE WHEN is_ssl THEN 'YES' ELSE 'NO' END as ssl_enabled,
    ssl_version,
    ssl_cipher,
    cipher_bits::text as bits,
    CASE WHEN is_local THEN 'Local' ELSE 'Remote' END as conn_type,
    CASE
        WHEN NOT is_ssl AND NOT is_local THEN 'WARNING - ' || connection_count || ' unencrypted remote connection(s)'
        WHEN NOT is_ssl AND is_local THEN 'OK - Local connection(s)'
        WHEN is_ssl AND cipher_bits < 128 THEN 'WARNING - Weak cipher (' || cipher_bits || ' bits)'
        WHEN is_ssl THEN 'OK - Encrypted (' || ssl_version || ')'
        ELSE 'UNKNOWN'
    END as security_status
FROM ssl_summary
ORDER BY
    CASE
        WHEN NOT is_ssl AND NOT is_local THEN 0
        WHEN is_ssl AND cipher_bits < 128 THEN 1
        WHEN NOT is_ssl AND is_local THEN 2
        ELSE 3
    END,
    connection_count DESC,
    usename
LIMIT 50;"

# 2.4 Password Security (REQ-6.4) - with superuser fallback
if PGPASSWORD="$PGPASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -t -c "SELECT 1 FROM pg_authid LIMIT 1;" 2>/dev/null | grep -q '1'; then
    execute_sql_to_html "Password Security" "
SELECT
    rolname AS role_name,
    CASE WHEN rolcanlogin THEN 'YES' ELSE 'no' END AS can_login,
    CASE
        WHEN rolpassword IS NULL THEN 'NO PASSWORD SET'
        WHEN rolpassword LIKE 'md5%' THEN 'md5 (WEAK - upgrade to SCRAM-SHA-256)'
        WHEN rolpassword LIKE 'SCRAM-SHA-256\$%' THEN 'SCRAM-SHA-256 (GOOD)'
        ELSE 'UNKNOWN'
    END AS password_method,
    CASE
        WHEN rolvaliduntil IS NULL THEN 'Never expires'
        WHEN rolvaliduntil < now() THEN 'EXPIRED on ' || rolvaliduntil::date::text
        WHEN rolvaliduntil < now() + interval '30 days' THEN 'Expires soon: ' || rolvaliduntil::date::text
        ELSE rolvaliduntil::date::text
    END AS password_expiry,
    CASE
        WHEN rolpassword IS NULL AND rolcanlogin THEN 'CRITICAL - Login role with no password'
        WHEN rolpassword LIKE 'md5%' THEN 'WARNING - Using weak md5 hashing'
        WHEN rolvaliduntil IS NOT NULL AND rolvaliduntil < now() THEN 'WARNING - Password expired'
        WHEN rolvaliduntil IS NOT NULL AND rolvaliduntil < now() + interval '30 days' THEN 'INFO - Password expiring soon'
        ELSE 'OK'
    END AS security_status
FROM pg_authid
WHERE rolname NOT LIKE 'pg_%'
ORDER BY
    CASE
        WHEN rolpassword IS NULL AND rolcanlogin THEN 1
        WHEN rolpassword LIKE 'md5%' THEN 2
        WHEN rolvaliduntil IS NOT NULL AND rolvaliduntil < now() THEN 3
        ELSE 4
    END,
    rolname
LIMIT 200;"
else
    execute_sql_to_html "Password Security" "
SELECT
    rolname AS role_name,
    CASE WHEN rolcanlogin THEN 'YES' ELSE 'no' END AS can_login,
    'N/A (requires superuser to inspect pg_authid)' AS password_method,
    CASE
        WHEN rolvaliduntil IS NULL THEN 'Never expires'
        WHEN rolvaliduntil < now() THEN 'EXPIRED on ' || rolvaliduntil::date::text
        WHEN rolvaliduntil < now() + interval '30 days' THEN 'Expires soon: ' || rolvaliduntil::date::text
        ELSE rolvaliduntil::date::text
    END AS password_expiry,
    CASE
        WHEN rolvaliduntil IS NOT NULL AND rolvaliduntil < now() THEN 'WARNING - Password expired'
        WHEN rolvaliduntil IS NOT NULL AND rolvaliduntil < now() + interval '30 days' THEN 'INFO - Password expiring soon'
        ELSE 'Limited check - connect as superuser for full audit'
    END AS security_status
FROM pg_roles
WHERE rolname NOT LIKE 'pg_%'
ORDER BY
    CASE
        WHEN rolvaliduntil IS NOT NULL AND rolvaliduntil < now() THEN 1
        ELSE 2
    END,
    rolname
LIMIT 200;"
fi

# 2.5 Sensitive Data Detection (REQ-6.5)
# NOTE: Heuristic column name matching - may produce false positives.
# Patterns match common PII/sensitive column naming conventions.
execute_sql_to_html "Sensitive Data Detection (Heuristic)" "
SELECT
    table_schema,
    table_name,
    column_name,
    data_type,
    CASE
        WHEN column_name ~* '(password|passwd|pwd|secret)' THEN 'PASSWORD/SECRET'
        WHEN column_name ~* '(ssn|social_security|national_id|sin_number)' THEN 'GOVERNMENT ID'
        WHEN column_name ~* '(credit_card|card_number|ccn|pan_number)' THEN 'CREDIT CARD'
        WHEN column_name ~* '(email|e_mail)' THEN 'EMAIL'
        WHEN column_name ~* '(phone|mobile|cell|telephone)' THEN 'PHONE'
        WHEN column_name ~* '(dob|date_of_birth|birth_date|birthday)' THEN 'DATE OF BIRTH'
        WHEN column_name ~* '(salary|income|wage|compensation)' THEN 'FINANCIAL'
        WHEN column_name ~* '(bank_account|account_number|routing_number|iban)' THEN 'BANK ACCOUNT'
        WHEN column_name ~* '(api_key|apikey|access_token|auth_token|bearer)' THEN 'API KEY/TOKEN'
        WHEN column_name ~* '(private_key|encryption_key|signing_key)' THEN 'ENCRYPTION KEY'
        ELSE 'POTENTIAL PII'
    END AS sensitivity_category,
    'WARNING - Column name suggests sensitive data; verify and ensure proper protection' AS recommendation
FROM information_schema.columns
WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
  AND column_name ~* '(password|passwd|pwd|secret|ssn|social_security|national_id|sin_number|credit_card|card_number|ccn|pan_number|email|e_mail|phone|mobile|cell|telephone|dob|date_of_birth|birth_date|birthday|salary|income|wage|compensation|bank_account|account_number|routing_number|iban|api_key|apikey|access_token|auth_token|bearer|private_key|encryption_key|signing_key)'
ORDER BY
    CASE
        WHEN column_name ~* '(password|passwd|pwd|secret|private_key|encryption_key)' THEN 1
        WHEN column_name ~* '(ssn|social_security|credit_card|card_number|bank_account)' THEN 2
        WHEN column_name ~* '(api_key|apikey|access_token|auth_token)' THEN 3
        ELSE 4
    END,
    table_schema,
    table_name,
    column_name
LIMIT 500;"

# 2.6 Row-Level Security (REQ-6.6)
execute_sql_to_html "Row-Level Security Status" "
SELECT
    n.nspname AS schema_name,
    c.relname AS table_name,
    CASE WHEN c.relrowsecurity THEN 'ENABLED' ELSE 'DISABLED' END AS rls_enabled,
    CASE WHEN c.relforcerowsecurity THEN 'YES' ELSE 'no' END AS force_rls,
    COALESCE(p.polname, 'N/A') AS policy_name,
    COALESCE(
        CASE p.polcmd
            WHEN '*' THEN 'ALL'
            WHEN 'r' THEN 'SELECT'
            WHEN 'a' THEN 'INSERT'
            WHEN 'w' THEN 'UPDATE'
            WHEN 'd' THEN 'DELETE'
            ELSE p.polcmd::text
        END, 'N/A') AS command_type,
    COALESCE(pg_catalog.pg_get_expr(p.polqual, p.polrelid), 'N/A') AS policy_expression,
    CASE
        WHEN c.relrowsecurity AND p.polname IS NOT NULL THEN 'OK - RLS active with policies'
        WHEN c.relrowsecurity AND p.polname IS NULL THEN 'WARNING - RLS enabled but no policies defined'
        ELSE 'INFO - RLS not enabled'
    END AS status
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_policy p ON p.polrelid = c.oid
WHERE c.relkind = 'r'
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND (c.relrowsecurity OR p.polname IS NOT NULL)
ORDER BY
    CASE WHEN c.relrowsecurity AND p.polname IS NULL THEN 0 ELSE 1 END,
    n.nspname,
    c.relname
LIMIT 200;"

# 2.7 Audit Configuration (REQ-6.7)
execute_sql_to_html "Audit Configuration" "
SELECT
    'pgaudit Extension' AS check_item,
    CASE
        WHEN EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pgaudit') THEN 'INSTALLED (v' || (SELECT extversion FROM pg_extension WHERE extname = 'pgaudit') || ')'
        ELSE 'NOT INSTALLED'
    END AS current_value,
    CASE
        WHEN EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pgaudit') THEN 'OK - pgaudit provides detailed audit logging'
        ELSE 'RECOMMENDATION - Consider installing pgaudit for compliance audit trails'
    END AS status
UNION ALL
SELECT
    'log_statement Setting' AS check_item,
    current_setting('log_statement') AS current_value,
    CASE current_setting('log_statement')
        WHEN 'none' THEN 'WARNING - No SQL statements are logged'
        WHEN 'ddl' THEN 'OK - DDL statements logged (schema changes)'
        WHEN 'mod' THEN 'OK - DDL + data modification statements logged'
        WHEN 'all' THEN 'INFO - All statements logged (may impact performance)'
        ELSE 'UNKNOWN'
    END AS status
UNION ALL
SELECT
    'log_connections Setting' AS check_item,
    current_setting('log_connections') AS current_value,
    CASE WHEN current_setting('log_connections') = 'on' THEN 'OK - Connection attempts are logged'
         ELSE 'RECOMMENDATION - Enable to track connection attempts'
    END AS status
UNION ALL
SELECT
    'log_disconnections Setting' AS check_item,
    current_setting('log_disconnections') AS current_value,
    CASE WHEN current_setting('log_disconnections') = 'on' THEN 'OK - Disconnections are logged'
         ELSE 'RECOMMENDATION - Enable to track session durations'
    END AS status
UNION ALL
SELECT
    'log_checkpoints Setting' AS check_item,
    current_setting('log_checkpoints') AS current_value,
    CASE WHEN current_setting('log_checkpoints') = 'on' THEN 'OK - Checkpoint activity is logged'
         ELSE 'RECOMMENDATION - Enable to monitor checkpoint frequency'
    END AS status
LIMIT 10;"

# 2.8 DB/Role Setting Overrides (REQ-11.9)
execute_sql_to_html "Database/Role Setting Overrides" "
SELECT
    COALESCE(d.datname, 'ALL DATABASES') AS database_name,
    COALESCE(r.rolname, 'ALL ROLES') AS role_name,
    unnest(s.setconfig) AS setting_override,
    'WARNING - Hidden ALTER DATABASE/ROLE SET override detected; may cause unexpected behavior' AS severity
FROM pg_db_role_setting s
LEFT JOIN pg_database d ON d.oid = s.setdatabase
LEFT JOIN pg_roles r ON r.oid = s.setrole
ORDER BY
    COALESCE(d.datname, ''),
    COALESCE(r.rolname, '')
LIMIT 200;"

echo '</div>' >> "$OUTPUT_FILE"
echo '</div>' >> "$OUTPUT_FILE"

# 3. CURRENT ACTIVITY
echo ""
echo "=============================================================================="
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting Section 3: CURRENT ACTIVITY"
echo "=============================================================================="
echo '<div class="section">' >> "$OUTPUT_FILE"
echo '<input type="checkbox" id="section3-toggle" class="accordion-checkbox">' >> "$OUTPUT_FILE"
echo '<label for="section3-toggle" class="accordion-label section-header" id="section3"><h2>3. CURRENT ACTIVITY (Real-time Status)</h2></label>' >> "$OUTPUT_FILE"
echo '<div class="accordion-content">' >> "$OUTPUT_FILE"
echo '<p class="section-description">Note: Activity data is based on a point-in-time snapshot from pg_stat_activity. Values reflect the state at query execution time and may differ between sub-sections as each query captures its own snapshot.</p>' >> "$OUTPUT_FILE"

execute_sql_to_html "Current Activity" "
WITH connection_stats AS (
    SELECT 
        state,
        wait_event_type,
        wait_event,
        datname,
        usename,
        application_name,
        client_addr,
        backend_type,
        EXTRACT(EPOCH FROM (now() - state_change))::integer as state_duration_secs,
        EXTRACT(EPOCH FROM (now() - xact_start))::integer as xact_duration_secs,
        EXTRACT(EPOCH FROM (now() - query_start))::integer as query_duration_secs,
        EXTRACT(EPOCH FROM (now() - backend_start))::integer as connection_duration_secs
    FROM pg_stat_activity
    WHERE pid != pg_backend_pid()
),
db_connections AS (
    SELECT 
        datname,
        COUNT(*) as connections
    FROM connection_stats
    WHERE datname IS NOT NULL
    GROUP BY datname
),
app_connections AS (
    SELECT 
        COALESCE(application_name, 'unknown') as application_name,
        COUNT(*) as connections
    FROM connection_stats
    GROUP BY application_name
)
SELECT 
    metric,
    value,
    description,
    details,
    recommendation
FROM (
    -- Total Connections
    SELECT 
        'Total Connections' as metric,
        COUNT(*)::text as value,
        'All database connections' as description,
        format('Max allowed: %s, Current: %s (%s%% utilized)', 
            current_setting('max_connections'),
            COUNT(*),
            ROUND(COUNT(*)::numeric * 100 / current_setting('max_connections')::numeric, 1)
        ) as details,
        CASE 
            WHEN COUNT(*)::float / current_setting('max_connections')::integer > 0.75 
            THEN 'WARNING: Connection usage high, consider connection pooling'
            ELSE 'OK'
        END as recommendation
    FROM connection_stats

    UNION ALL

    -- Active Queries
    SELECT 
        'Active Queries',
        COUNT(*)::text,
        'Currently executing queries',
        format('Long running (>5 min): %s, Normal: %s', 
            COUNT(*) FILTER (WHERE state = 'active' AND query_duration_secs > 300),
            COUNT(*) FILTER (WHERE state = 'active' AND query_duration_secs <= 300)
        ),
        CASE 
            WHEN COUNT(*) FILTER (WHERE state = 'active' AND query_duration_secs > 300) > 0
            THEN 'WARNING: Long-running queries detected'
            ELSE 'OK'
        END
    FROM connection_stats
    WHERE state = 'active'

    UNION ALL

    -- Idle Connections
    SELECT 
        'Idle Connections',
        COUNT(*)::text,
        'Inactive connections',
        format('Idle time > 1 hour: %s, Total idle: %s', 
            COUNT(*) FILTER (WHERE state_duration_secs > 3600),
            COUNT(*)
        ),
        CASE 
            WHEN COUNT(*) FILTER (WHERE state_duration_secs > 3600) > 10
            THEN 'Consider closing idle connections older than 1 hour'
            ELSE 'OK'
        END
    FROM connection_stats
    WHERE state = 'idle'

    UNION ALL

    -- Idle in Transaction
    SELECT 
        'Idle in Transaction',
        COUNT(*)::text,
        'Transactions open but inactive',
        format('Count: %s, Max age: %s sec, Critical (>10 min): %s, Warning (>1 min): %s',
            COUNT(*) FILTER (WHERE state = 'idle in transaction'),
            COALESCE(MAX(xact_duration_secs) FILTER (WHERE state = 'idle in transaction'), 0),
            COUNT(*) FILTER (WHERE state like 'idle in transaction%' AND xact_duration_secs > 600),
            COUNT(*) FILTER (WHERE state like 'idle in transaction%' AND xact_duration_secs > 60)
        ),
        CASE 
            WHEN COUNT(*) FILTER (WHERE state like 'idle in transaction%' AND xact_duration_secs > 600) > 0
            THEN 'CRITICAL: Long-running idle transactions detected'
            WHEN COUNT(*) FILTER (WHERE state like 'idle in transaction%' AND xact_duration_secs > 60) > 0
            THEN 'WARNING: Idle transactions detected'
            ELSE 'OK'
        END
    FROM connection_stats
    WHERE state like 'idle in transaction%'

    UNION ALL

    -- Waiting Queries
    SELECT 
        'Waiting Queries',
        COUNT(*)::text,
        'Queries waiting for resources',
        format('Lock waits: %s, IO waits: %s, Other: %s',
            COUNT(*) FILTER (WHERE wait_event_type = 'Lock'),
            COUNT(*) FILTER (WHERE wait_event_type = 'IO'),
            COUNT(*) FILTER (WHERE wait_event_type NOT IN ('Lock', 'IO') AND wait_event_type IS NOT NULL)
        ),
        CASE 
            WHEN COUNT(*) FILTER (WHERE wait_event_type = 'Lock') > 0
            THEN 'WARNING: Lock contention detected'
            ELSE 'OK'
        END
    FROM connection_stats
    WHERE wait_event_type IS NOT NULL
) stats
ORDER BY 
    CASE metric
        WHEN 'Total Connections' THEN 1
        WHEN 'Active Queries' THEN 2
        WHEN 'Waiting Queries' THEN 3
        WHEN 'Idle in Transaction' THEN 4
        WHEN 'Idle Connections' THEN 5
        ELSE 9
    END;"

# Add general recommendations for Current Activity monitoring
cat >> "$OUTPUT_FILE" << 'EOF'
<div class="table-container">
<h3 style="font-size: 0.7em; margin: 0; padding: 0;">📋 General Recommendations for Activity Monitoring</h3>
<div style="background: linear-gradient(135deg, #e8f4fd 0%, #f0f8ff 100%); padding: 15px; border-radius: 8px; border-left: 4px solid #3498db; margin: 15px 0; font-size: 0.7em; line-height: 1.3;">
    <div style="color: #2c3e50; margin: 0;"><strong>🔧 AWS Recommendations:</strong><br>
    <strong><a href="https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Database-Insights.html" target="_blank" style="color: #3498db; text-decoration: none;">CloudWatch Database Insights</a></strong>: Real-time session analysis + top SQL identification | <strong><a href="https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_Monitoring.OS.html" target="_blank" style="color: #3498db; text-decoration: none;">Enhanced Monitoring</a></strong>: Detailed OS metrics + connection limit alarms | <strong><a href="https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_PerfInsights.html" target="_blank" style="color: #3498db; text-decoration: none;">RDS Performance Insights</a></strong>: Query performance tracking</div>
    
    <div style="color: #2c3e50; margin-top: 10px;"><strong>🔍 PostgreSQL Extensions & Tools:</strong><br>
    <strong><a href="https://www.postgresql.org/docs/current/auto-explain.html" target="_blank" style="color: #3498db; text-decoration: none;">auto_explain</a></strong>: Automatic slow query execution plans | <strong><a href="https://github.com/postgrespro/pg_wait_sampling" target="_blank" style="color: #3498db; text-decoration: none;">pg_wait_sampling</a></strong>: Granular wait event analysis + lock contention | <strong><a href="https://www.postgresql.org/docs/current/pgstatstatements.html" target="_blank" style="color: #3498db; text-decoration: none;">pg_stat_statements</a></strong>: Query performance tracking | <strong><a href="https://www.postgresql.org/docs/current/monitoring-stats.html#MONITORING-PG-STAT-ACTIVITY-VIEW" target="_blank" style="color: #3498db; text-decoration: none;">pg_stat_activity</a></strong>: Real-time session monitoring</div>
</div>
</div>
EOF

# --- Application Insights Sub-sections (REQ-7.x) ---

# 3.1 Connection Pooling Detection (REQ-7.1)
execute_sql_to_html "Connection Pooling Detection" "
WITH session_ages AS (
    SELECT
        application_name,
        client_addr,
        COUNT(*) AS connection_count,
        MIN(EXTRACT(EPOCH FROM (now() - backend_start))) AS min_age_seconds,
        MAX(EXTRACT(EPOCH FROM (now() - backend_start))) AS max_age_seconds,
        AVG(EXTRACT(EPOCH FROM (now() - backend_start))) AS avg_age_seconds,
        STDDEV(EXTRACT(EPOCH FROM (now() - backend_start))) AS stddev_age_seconds
    FROM pg_stat_activity
    WHERE backend_type = 'client backend'
      AND pid <> pg_backend_pid()
    GROUP BY application_name, client_addr
),
pooler_detection AS (
    SELECT
        COALESCE(application_name, '') AS application_name,
        COALESCE(client_addr::text, 'local') AS client_address,
        connection_count,
        ROUND(min_age_seconds::numeric, 1) AS min_age_sec,
        ROUND(max_age_seconds::numeric, 1) AS max_age_sec,
        ROUND(avg_age_seconds::numeric, 1) AS avg_age_sec,
        ROUND(COALESCE(stddev_age_seconds, 0)::numeric, 1) AS stddev_age_sec,
        CASE
            WHEN LOWER(COALESCE(application_name, '')) ~ '(pgbouncer|pgpool|odyssey|pgcat|supavisor)' THEN 'HIGH - Known pooler detected in application_name'
            WHEN connection_count >= 10 AND COALESCE(stddev_age_seconds, 0) < 5 THEN 'MEDIUM - Many connections with similar ages (possible pooler)'
            WHEN connection_count >= 5 AND max_age_seconds - min_age_seconds < 10 THEN 'MEDIUM - Connections started within narrow window'
            WHEN connection_count >= 20 THEN 'LOW - High connection count from single source'
            ELSE 'NONE - No pooling pattern detected'
        END AS pooling_confidence
    FROM session_ages
)
SELECT
    application_name,
    client_address,
    connection_count,
    min_age_sec,
    max_age_sec,
    avg_age_sec,
    stddev_age_sec,
    pooling_confidence
FROM pooler_detection
ORDER BY connection_count DESC
LIMIT 50;"

# 3.2 Application Identification (REQ-7.2)
execute_sql_to_html "Application Identification" "
SELECT
    COALESCE(NULLIF(application_name, ''), '<unnamed>') AS application,
    COUNT(*) AS total_connections,
    COUNT(*) FILTER (WHERE state = 'active') AS active,
    COUNT(*) FILTER (WHERE state = 'idle') AS idle,
    COUNT(*) FILTER (WHERE state = 'idle in transaction') AS idle_in_txn,
    COUNT(*) FILTER (WHERE state IS NULL OR state = 'disabled') AS other,
    COUNT(DISTINCT usename) AS distinct_users,
    COUNT(DISTINCT datname) AS distinct_databases,
    COUNT(DISTINCT client_addr) AS distinct_clients,
    ROUND(AVG(EXTRACT(EPOCH FROM (now() - backend_start)))::numeric, 1) AS avg_session_age_sec,
    MAX(EXTRACT(EPOCH FROM (now() - backend_start)))::int AS max_session_age_sec
FROM pg_stat_activity
WHERE backend_type = 'client backend'
  AND pid <> pg_backend_pid()
GROUP BY application_name
ORDER BY total_connections DESC
LIMIT 50;"

# 3.3 Workload Characterization (REQ-7.3)
execute_sql_to_html "Workload Characterization" "
WITH query_types AS (
    SELECT
        CASE
            WHEN UPPER(LEFT(LTRIM(query), 6)) = 'SELECT' THEN 'READ (SELECT)'
            WHEN UPPER(LEFT(LTRIM(query), 6)) = 'INSERT' THEN 'WRITE (INSERT)'
            WHEN UPPER(LEFT(LTRIM(query), 6)) = 'UPDATE' THEN 'WRITE (UPDATE)'
            WHEN UPPER(LEFT(LTRIM(query), 6)) = 'DELETE' THEN 'WRITE (DELETE)'
            WHEN UPPER(LEFT(LTRIM(query), 4)) = 'COPY' THEN 'WRITE (COPY)'
            WHEN query = '' OR query IS NULL THEN 'NO QUERY'
            ELSE 'OTHER'
        END AS query_type,
        state,
        datname
    FROM pg_stat_activity
    WHERE backend_type = 'client backend'
      AND pid <> pg_backend_pid()
),
type_summary AS (
    SELECT
        query_type,
        COUNT(*) AS connection_count,
        COUNT(*) FILTER (WHERE state = 'active') AS active_count,
        COUNT(DISTINCT datname) AS databases
    FROM query_types
    GROUP BY query_type
),
totals AS (
    SELECT
        SUM(connection_count) AS total,
        SUM(CASE WHEN query_type LIKE 'READ%' THEN connection_count ELSE 0 END) AS reads,
        SUM(CASE WHEN query_type LIKE 'WRITE%' THEN connection_count ELSE 0 END) AS writes
    FROM type_summary
)
SELECT
    ts.query_type,
    ts.connection_count,
    ts.active_count,
    ts.databases,
    CASE WHEN t.total > 0
        THEN ROUND((ts.connection_count * 100.0 / t.total)::numeric, 1)
        ELSE 0
    END AS pct_of_total,
    CASE WHEN t.reads + t.writes > 0
        THEN 'Read/Write Ratio: ' || t.reads || ':' || t.writes
        ELSE 'No read/write activity'
    END AS rw_ratio_summary
FROM type_summary ts
CROSS JOIN totals t
ORDER BY ts.connection_count DESC
LIMIT 20;"

# 3.4 Client Analysis (REQ-7.4)
execute_sql_to_html "Client Analysis" "
SELECT
    COALESCE(client_addr::text, 'local/unix') AS client_address,
    COALESCE(client_hostname, '') AS client_hostname,
    COUNT(*) AS total_connections,
    COUNT(*) FILTER (WHERE state = 'active') AS active,
    COUNT(*) FILTER (WHERE state = 'idle') AS idle,
    COUNT(*) FILTER (WHERE state = 'idle in transaction') AS idle_in_txn,
    COUNT(DISTINCT usename) AS distinct_users,
    COUNT(DISTINCT datname) AS distinct_databases,
    COUNT(DISTINCT application_name) AS distinct_apps,
    STRING_AGG(DISTINCT COALESCE(NULLIF(application_name, ''), '<unnamed>'), ', ' ORDER BY COALESCE(NULLIF(application_name, ''), '<unnamed>')) AS applications,
    ROUND(AVG(EXTRACT(EPOCH FROM (now() - backend_start)))::numeric, 1) AS avg_session_age_sec
FROM pg_stat_activity
WHERE backend_type = 'client backend'
  AND pid <> pg_backend_pid()
GROUP BY client_addr, client_hostname
ORDER BY total_connections DESC
LIMIT 50;"

# 3.5 Connection Churn (REQ-7.5)
execute_sql_to_html "Connection Churn" "
WITH age_buckets AS (
    SELECT
        CASE
            WHEN EXTRACT(EPOCH FROM (now() - backend_start)) < 60 THEN '< 1 min (very new)'
            WHEN EXTRACT(EPOCH FROM (now() - backend_start)) < 300 THEN '1-5 min (new)'
            WHEN EXTRACT(EPOCH FROM (now() - backend_start)) < 3600 THEN '5-60 min (recent)'
            WHEN EXTRACT(EPOCH FROM (now() - backend_start)) < 86400 THEN '1-24 hours (stable)'
            ELSE '> 24 hours (long-lived)'
        END AS connection_age_bucket,
        CASE
            WHEN EXTRACT(EPOCH FROM (now() - backend_start)) < 60 THEN 1
            WHEN EXTRACT(EPOCH FROM (now() - backend_start)) < 300 THEN 2
            WHEN EXTRACT(EPOCH FROM (now() - backend_start)) < 3600 THEN 3
            WHEN EXTRACT(EPOCH FROM (now() - backend_start)) < 86400 THEN 4
            ELSE 5
        END AS bucket_order,
        state
    FROM pg_stat_activity
    WHERE backend_type = 'client backend'
      AND pid <> pg_backend_pid()
),
bucket_summary AS (
    SELECT
        connection_age_bucket,
        bucket_order,
        COUNT(*) AS connection_count,
        COUNT(*) FILTER (WHERE state = 'active') AS active,
        COUNT(*) FILTER (WHERE state = 'idle') AS idle,
        COUNT(*) FILTER (WHERE state = 'idle in transaction') AS idle_in_txn
    FROM age_buckets
    GROUP BY connection_age_bucket, bucket_order
),
totals AS (
    SELECT SUM(connection_count) AS total FROM bucket_summary
)
SELECT
    bs.connection_age_bucket,
    bs.connection_count,
    CASE WHEN t.total > 0
        THEN ROUND((bs.connection_count * 100.0 / t.total)::numeric, 1)
        ELSE 0
    END AS pct_of_total,
    bs.active,
    bs.idle,
    bs.idle_in_txn,
    CASE
        WHEN bs.bucket_order <= 2 AND bs.connection_count > (t.total * 0.5) THEN 'HIGH CHURN - Majority of connections are very new'
        WHEN bs.bucket_order <= 2 AND bs.connection_count > (t.total * 0.3) THEN 'MODERATE CHURN - Significant portion of new connections'
        WHEN bs.bucket_order >= 4 AND bs.connection_count > (t.total * 0.8) THEN 'STABLE - Most connections are long-lived'
        ELSE 'NORMAL'
    END AS churn_indicator
FROM bucket_summary bs
CROSS JOIN totals t
ORDER BY bs.bucket_order
LIMIT 20;"

echo '</div>' >> "$OUTPUT_FILE"
echo '</div>' >> "$OUTPUT_FILE"

# 4. REPLICATION STATUS
echo ""
echo "=============================================================================="
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting Section 4: REPLICATION STATUS"
echo "=============================================================================="
echo '<div class="section">' >> "$OUTPUT_FILE"
echo '<input type="checkbox" id="section4-toggle" class="accordion-checkbox">' >> "$OUTPUT_FILE"
echo '<label for="section4-toggle" class="accordion-label section-header" id="section4"><h2>4. REPLICATION STATUS (High Availability)</h2></label>' >> "$OUTPUT_FILE"
echo '<div class="accordion-content">' >> "$OUTPUT_FILE"

execute_sql_to_html "Replication Information" "
WITH db_role AS (
    SELECT
        CASE WHEN pg_is_in_recovery() THEN 'REPLICA' ELSE 'PRIMARY' END as server_role,
        current_setting('wal_level') as wal_level,
        '$DEPLOYMENT_TYPE' as deployment
)
SELECT
    metric,
    value,
    description,
    details,
    recommendation
FROM (
    SELECT
        'Deployment Type' as metric,
        deployment as value,
        'Detected database deployment environment' as description,
        CASE deployment
            WHEN 'Aurora' THEN 'Aurora uses storage-level replication (6-way, 3 AZs). WAL-based replication not applicable'
            WHEN 'Aurora Serverless' THEN 'Aurora Serverless v2 inherits Aurora storage-level replication. Fully managed HA'
            WHEN 'RDS' THEN 'RDS supports Read Replicas via streaming replication. Multi-AZ uses synchronous standby'
            ELSE 'Self-managed PostgreSQL. Full control over replication configuration'
        END as details,
        CASE deployment
            WHEN 'Aurora' THEN 'INFO: Use Aurora Replicas for read scaling. Cross-region via Aurora Global Database'
            WHEN 'Aurora Serverless' THEN 'INFO: HA is automatic. Consider Aurora Global Database for cross-region DR'
            WHEN 'RDS' THEN 'INFO: Use RDS Read Replicas for read scaling. Enable Multi-AZ for automatic failover'
            ELSE 'Configure streaming replication for HA. Consider pg_basebackup for replica setup'
        END as recommendation,
        0 as sort_order
    FROM db_role

    UNION ALL

    SELECT
        'Database Role' as metric,
        server_role as value,
        'Current database role and WAL configuration' as description,
        'WAL Level: ' || wal_level || ', Server Role: ' || server_role as details,
        CASE
            WHEN deployment IN ('Aurora', 'Aurora Serverless') THEN
                CASE WHEN server_role = 'REPLICA' THEN 'OK: Aurora Reader instance' ELSE 'OK: Aurora Writer instance' END
            WHEN deployment = 'RDS' THEN
                CASE
                    WHEN server_role = 'REPLICA' THEN 'OK: RDS Read Replica'
                    WHEN wal_level = 'minimal' THEN 'INFO: WAL level managed by RDS - set via parameter group if Read Replicas needed'
                    ELSE 'OK'
                END
            ELSE
                CASE
                    WHEN server_role = 'PRIMARY' AND wal_level = 'minimal' THEN 'WARNING: WAL level minimal does not support replication'
                    WHEN server_role = 'PRIMARY' AND wal_level = 'replica' THEN 'OK: WAL level supports streaming replication'
                    WHEN server_role = 'PRIMARY' AND wal_level = 'logical' THEN 'OK: WAL level supports logical replication'
                    ELSE 'OK'
                END
        END as recommendation,
        1 as sort_order
    FROM db_role

    UNION ALL

    SELECT
        'Connected Replicas' as metric,
        COALESCE((SELECT COUNT(*)::text FROM pg_stat_replication), '0') as value,
        CASE deployment
            WHEN 'Aurora' THEN 'Aurora Replicas connected via storage layer (may not appear in pg_stat_replication)'
            WHEN 'Aurora Serverless' THEN 'Serverless replicas managed automatically'
            WHEN 'RDS' THEN 'RDS Read Replicas connected via streaming replication'
            ELSE 'Streaming replication replicas connected'
        END as description,
        CASE
            WHEN (SELECT COUNT(*) FROM pg_stat_replication) > 0 THEN
                (SELECT COUNT(*)::text FROM pg_stat_replication) || ' replica(s) connected'
            WHEN deployment IN ('Aurora', 'Aurora Serverless') THEN
                'Aurora replicas use storage-level replication - check AWS Console for replica status'
            ELSE 'No replicas connected'
        END as details,
        CASE
            WHEN (SELECT COUNT(*) FROM pg_stat_replication) > 0 THEN 'OK: Replication active'
            WHEN deployment IN ('Aurora', 'Aurora Serverless') THEN 'INFO: Check Aurora console for replica count and lag'
            WHEN deployment = 'RDS' THEN 'INFO: No Read Replicas configured. Consider adding for read scaling'
            ELSE 'INFO: No replication configured'
        END as recommendation,
        2 as sort_order
    FROM db_role

    UNION ALL

    SELECT
        'WAL Archiving' as metric,
        CASE
            WHEN deployment IN ('Aurora', 'Aurora Serverless') THEN 'Managed'
            WHEN archived_count > 0 THEN 'Active'
            ELSE 'Inactive'
        END as value,
        CASE deployment
            WHEN 'Aurora' THEN 'Aurora manages WAL archiving internally via continuous backup'
            WHEN 'Aurora Serverless' THEN 'WAL archiving fully managed by Aurora Serverless'
            WHEN 'RDS' THEN 'RDS manages automated backups and WAL archiving'
            ELSE 'WAL archiving status for point-in-time recovery'
        END as description,
        CASE
            WHEN deployment IN ('Aurora', 'Aurora Serverless') THEN 'Continuous backup enabled. Retention configurable via AWS Console'
            WHEN deployment = 'RDS' THEN 'Automated backups: ' || COALESCE(archived_count::text, '0') || ' WALs archived'
            ELSE 'Successfully archived WALs: ' || COALESCE(archived_count, 0)
        END as details,
        CASE
            WHEN deployment IN ('Aurora', 'Aurora Serverless') THEN 'INFO: Managed by AWS. Configure backup retention in AWS Console'
            WHEN deployment = 'RDS' THEN 'INFO: Managed by RDS. Verify backup retention period in AWS Console'
            WHEN archived_count > 0 THEN 'OK: WAL archiving active'
            ELSE 'WARNING: WAL archiving inactive - no point-in-time recovery possible'
        END as recommendation,
        3 as sort_order
    FROM pg_stat_archiver
    CROSS JOIN db_role

    UNION ALL

    SELECT
        'WAL-Based Replication' as metric,
        CASE
            WHEN wal_level IN ('logical', 'replica') AND (SELECT COUNT(*) FROM pg_replication_slots) > 0 THEN
                'Active (' || (SELECT COUNT(*)::text FROM pg_replication_slots) || ' slot(s))'
            WHEN wal_level IN ('logical', 'replica') THEN 'Configured (no slots)'
            ELSE 'Not configured'
        END as value,
        CASE
            WHEN deployment IN ('Aurora', 'Aurora Serverless') AND wal_level IN ('logical', 'replica') THEN
                'User-configured WAL replication detected on ' || deployment || '. WAL accumulation requires monitoring'
            WHEN deployment = 'RDS' AND wal_level IN ('logical', 'replica') THEN
                'WAL-based replication active on RDS. Replication slots accumulate WAL if subscribers disconnect'
            WHEN wal_level IN ('logical', 'replica') THEN
                'WAL-based replication configured. Monitor slot lag to prevent disk exhaustion'
            ELSE 'WAL level does not support replication'
        END as description,
        CASE
            WHEN (SELECT COUNT(*) FROM pg_replication_slots WHERE NOT active) > 0 THEN
                'INACTIVE SLOTS: ' || (SELECT COUNT(*)::text FROM pg_replication_slots WHERE NOT active) ||
                ' inactive slot(s). WAL lag: ' ||
                COALESCE((SELECT pg_size_pretty(MAX(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn))) FROM pg_replication_slots WHERE NOT active), 'N/A')
            WHEN (SELECT COUNT(*) FROM pg_replication_slots) > 0 THEN
                'All ' || (SELECT COUNT(*)::text FROM pg_replication_slots) || ' slot(s) active. Max lag: ' ||
                COALESCE((SELECT pg_size_pretty(MAX(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn))) FROM pg_replication_slots), 'N/A')
            ELSE 'No replication slots'
        END as details,
        CASE
            WHEN (SELECT COUNT(*) FROM pg_replication_slots WHERE NOT active) > 0 THEN
                'CRITICAL: Drop inactive slots to prevent WAL buildup. ' ||
                CASE WHEN deployment IN ('Aurora', 'Aurora Serverless') THEN 'Aurora storage grows with WAL - direct cost impact'
                     WHEN deployment = 'RDS' THEN 'WAL accumulation can fill RDS storage'
                     ELSE 'WAL accumulation can exhaust disk space'
                END
            WHEN (SELECT COUNT(*) FROM pg_replication_slots) > 0 AND
                 (SELECT MAX(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) FROM pg_replication_slots) > 1073741824 THEN
                'WARNING: Slot lag exceeds 1GB. Monitor subscriber health'
            WHEN wal_level IN ('logical', 'replica') AND (SELECT COUNT(*) FROM pg_replication_slots) > 0 THEN
                'OK: WAL replication active. Monitor slot lag regularly'
            WHEN wal_level IN ('logical', 'replica') THEN
                'INFO: WAL level set but no replication slots created'
            ELSE 'INFO: No WAL-based replication configured'
        END as recommendation,
        4 as sort_order
    FROM db_role
) AS replication_metrics
ORDER BY sort_order;"

# --- Replication Sub-sections (REQ-9.x) ---

# 4.1 Detailed Replication Lag (REQ-9.1) - with permission fallback
if PGPASSWORD="$PGPASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -t -c "SELECT 1 FROM pg_stat_replication LIMIT 1;" 2>/dev/null | grep -q '1'; then
    execute_sql_to_html "Detailed Replication Lag" "
SELECT
    client_addr,
    application_name,
    state,
    sync_state,
    sent_lsn,
    write_lsn,
    flush_lsn,
    replay_lsn,
    COALESCE(write_lag::text, 'N/A') AS write_lag,
    COALESCE(flush_lag::text, 'N/A') AS flush_lag,
    COALESCE(replay_lag::text, 'N/A') AS replay_lag,
    CASE
        WHEN replay_lag > interval '5 minutes' THEN 'CRITICAL - Replay lag exceeds 5 minutes'
        WHEN replay_lag > interval '1 minute' THEN 'WARNING - Replay lag exceeds 1 minute'
        WHEN replay_lag > interval '10 seconds' THEN 'INFO - Moderate replay lag'
        WHEN replay_lag IS NULL THEN 'N/A'
        ELSE 'OK'
    END AS lag_status
FROM pg_stat_replication
ORDER BY replay_lag DESC NULLS LAST
LIMIT 50;"
else
    execute_sql_to_html "Detailed Replication Lag" "
SELECT
    CASE '$DEPLOYMENT_TYPE'
        WHEN 'Aurora' THEN 'Aurora: Use AWS Console for replica lag'
        WHEN 'Aurora Serverless' THEN 'Serverless: Replica lag managed automatically'
        WHEN 'RDS' THEN 'RDS: Limited access to pg_stat_replication'
        ELSE 'Requires superuser or pg_read_all_stats role'
    END AS status,
    CASE '$DEPLOYMENT_TYPE'
        WHEN 'Aurora' THEN 'Aurora replicas use storage-level replication. Check CloudWatch AuroraReplicaLag metric'
        WHEN 'Aurora Serverless' THEN 'Replication is fully managed. Monitor via CloudWatch metrics'
        WHEN 'RDS' THEN 'pg_stat_replication requires rds_superuser. Check RDS Console for Read Replica lag'
        ELSE 'pg_stat_replication is not accessible with current permissions'
    END AS details,
    CASE '$DEPLOYMENT_TYPE'
        WHEN 'Aurora' THEN 'Monitor AuroraReplicaLag and AuroraReplicaLagInMilliseconds in CloudWatch'
        WHEN 'Aurora Serverless' THEN 'No action needed - HA is automatic'
        WHEN 'RDS' THEN 'Check ReplicaLag metric in CloudWatch or RDS Console > Monitoring'
        ELSE 'Connect as superuser or grant pg_read_all_stats to view detailed replication lag'
    END AS recommendation
LIMIT 1;"
fi

# 4.2 WAL Sender Status (REQ-9.2) - with permission fallback
if PGPASSWORD="$PGPASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -t -c "SELECT 1 FROM pg_stat_replication LIMIT 1;" 2>/dev/null | grep -q '1'; then
    execute_sql_to_html "WAL Sender Status" "
SELECT
    pid,
    usename,
    application_name,
    client_addr,
    state,
    sent_lsn,
    sync_state,
    sync_priority,
    CASE
        WHEN state = 'streaming' THEN 'OK - Actively streaming'
        WHEN state = 'catchup' THEN 'INFO - Replica catching up'
        WHEN state = 'startup' THEN 'INFO - WAL sender starting'
        WHEN state = 'backup' THEN 'INFO - Sending backup'
        ELSE COALESCE('State: ' || state, 'UNKNOWN')
    END AS sender_status
FROM pg_stat_replication
ORDER BY sync_priority, application_name
LIMIT 50;"
else
    execute_sql_to_html "WAL Sender Status" "
SELECT
    CASE '$DEPLOYMENT_TYPE'
        WHEN 'Aurora' THEN 'Aurora: WAL sender not applicable'
        WHEN 'Aurora Serverless' THEN 'Serverless: WAL sender managed automatically'
        WHEN 'RDS' THEN 'RDS: Limited access to pg_stat_replication'
        ELSE 'Requires superuser or pg_read_all_stats role'
    END AS status,
    CASE '$DEPLOYMENT_TYPE'
        WHEN 'Aurora' THEN 'Aurora uses storage-level replication, not WAL streaming. No WAL senders to monitor'
        WHEN 'Aurora Serverless' THEN 'WAL management is fully automated in Serverless v2'
        WHEN 'RDS' THEN 'pg_stat_replication requires rds_superuser. WAL sender info available in RDS Console'
        ELSE 'pg_stat_replication is not accessible with current permissions'
    END AS details,
    CASE '$DEPLOYMENT_TYPE'
        WHEN 'Aurora' THEN 'INFO: No action needed - Aurora handles replication at storage layer'
        WHEN 'Aurora Serverless' THEN 'INFO: No action needed - fully managed'
        WHEN 'RDS' THEN 'Check RDS Console > Monitoring for replication metrics'
        ELSE 'Connect as superuser or grant pg_read_all_stats to view WAL sender status'
    END AS recommendation
LIMIT 1;"
fi

# 4.3 Synchronous Replication Config (REQ-9.3)
execute_sql_to_html "Synchronous Replication Config" "
SELECT
    current_setting('synchronous_standby_names') AS synchronous_standby_names,
    current_setting('synchronous_commit') AS synchronous_commit,
    CASE
        WHEN current_setting('synchronous_standby_names') = '' THEN 'Disabled'
        ELSE 'Enabled'
    END AS sync_replication_active,
    CASE
        WHEN current_setting('synchronous_standby_names') = '' THEN 'INFO - Asynchronous replication (no synchronous standbys configured)'
        WHEN current_setting('synchronous_commit') = 'off' THEN 'WARNING - synchronous_commit is off despite synchronous standbys configured'
        WHEN current_setting('synchronous_commit') = 'local' THEN 'INFO - synchronous_commit=local, WAL committed locally only'
        WHEN current_setting('synchronous_commit') IN ('on', 'remote_write', 'remote_apply') THEN 'OK - Synchronous replication active with ' || current_setting('synchronous_commit')
        ELSE 'INFO - synchronous_commit=' || current_setting('synchronous_commit')
    END AS config_status
LIMIT 1;"

# 4.4 Logical Replication Monitoring (REQ-10.6)
execute_sql_to_html "Logical Replication Monitoring" "
SELECT 
    slot_name,
    plugin,
    slot_type,
    database,
    active,
    pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) as lag_bytes,
    pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) as lag_size,
    pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn) as unflushed_bytes,
    pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn)) as unflushed_size,
    CASE 
        WHEN NOT active THEN 'CRITICAL: Inactive slot - WAL accumulation risk'
        WHEN pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) > 1073741824 THEN 'CRITICAL: >1GB lag'
        WHEN pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) > 104857600 THEN 'WARNING: >100MB lag'
        ELSE 'OK'
    END as status,
    CASE 
        WHEN NOT active THEN 'Drop unused slot: SELECT pg_drop_replication_slot(''' || slot_name || ''');'
        WHEN pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) > 1073741824 THEN 
            'Check subscriber health and network connectivity'
        ELSE 'Monitor lag trends'
    END as recommendation
FROM pg_replication_slots
WHERE slot_type = 'logical'
ORDER BY lag_bytes DESC NULLS LAST;"

# Add general recommendations for Replication Status monitoring
cat >> "$OUTPUT_FILE" << 'EOF'
<div class="table-container">
<h3 style="font-size: 0.7em; margin: 0; padding: 0;">📋 General Recommendations for Replication Monitoring</h3>
<div style="background: linear-gradient(135deg, #e8f4fd 0%, #f0f8ff 100%); padding: 15px; border-radius: 8px; border-left: 4px solid #3498db; margin: 15px 0; font-size: 0.7em; line-height: 1.3;">
    <div style="color: #2c3e50; margin: 0;"><strong>🔧 AWS Recommendations:</strong><br>
    <strong><a href="https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-global-database.html" target="_blank" style="color: #3498db; text-decoration: none;">Aurora Global Database</a></strong>: Cross-region disaster recovery + read scaling | <strong><a href="https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Concepts.MultiAZ.html" target="_blank" style="color: #3498db; text-decoration: none;">RDS Multi-AZ deployments</a></strong>: Automatic failover + high availability</div>
    
    <div style="color: #2c3e50; margin-top: 10px;"><strong>🔍 PostgreSQL Extensions & Tools:</strong><br>
    <strong><a href="https://www.postgresql.org/docs/current/monitoring-stats.html#MONITORING-PG-STAT-REPLICATION-VIEW" target="_blank" style="color: #3498db; text-decoration: none;">pg_stat_replication</a></strong>: Detailed replication lag monitoring | <strong><a href="https://www.postgresql.org/docs/current/view-pg-replication-slots.html" target="_blank" style="color: #3498db; text-decoration: none;">pg_replication_slots</a></strong>: Logical replication management + WAL buildup prevention</div>
</div>
</div>
EOF

echo '</div>' >> "$OUTPUT_FILE"
echo '</div>' >> "$OUTPUT_FILE"

# 5. DATA FOOTPRINT
echo ""
echo "=============================================================================="
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting Section 5: DATA FOOTPRINT"
echo "=============================================================================="
echo '<div class="section">' >> "$OUTPUT_FILE"
echo '<input type="checkbox" id="section5-toggle" class="accordion-checkbox">' >> "$OUTPUT_FILE"
echo '<label for="section5-toggle" class="accordion-label section-header" id="section5"><h2>5. DATA FOOTPRINT (Size & WAL Analysis)</h2></label>' >> "$OUTPUT_FILE"
echo '<div class="accordion-content">' >> "$OUTPUT_FILE"

execute_sql_to_html "Database Sizes" "
SELECT 
    datname as database_name,
    COALESCE(pg_size_pretty(pg_database_size(datname)), 'N/A (inaccessible)') as formatted_size,
    COALESCE(ROUND(pg_database_size(datname)::numeric / 1073741824, 2), 0) as size_in_gb,
    CASE 
        WHEN pg_database_size(datname) IS NULL THEN 'INACCESSIBLE'
        WHEN pg_database_size(datname) > 53687091200 THEN 'LARGE (>50GB)'
        WHEN pg_database_size(datname) > 10737418240 THEN 'MEDIUM (10-50GB)'
        ELSE 'SMALL (<10GB)'
    END as size_category,
    CASE 
        WHEN datallowconn = false THEN 'No connections allowed'
        WHEN datname IN ('template0', 'template1') THEN 'Template database'
        WHEN pg_database_size(datname) IS NULL THEN 'Access denied or database unavailable'
        ELSE 'Accessible'
    END as access_status
FROM pg_database 
WHERE datname IS NOT NULL
ORDER BY 
    CASE WHEN pg_database_size(datname) IS NULL THEN 1 ELSE 0 END,
    pg_database_size(datname) DESC NULLS LAST;"

# WAL Directory Analysis with version and permission check
if [ "$PG_VERSION" -ge 100000 ] && PGPASSWORD="$PGPASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -t -c "SELECT has_function_privilege('pg_ls_waldir()', 'execute');" 2>/dev/null | grep -q 't'; then
    execute_sql_to_html "WAL File Directory Analysis" "
SELECT 
    COUNT(*) as wal_file_count,
    pg_size_pretty(SUM(size)) as total_wal_size,
    SUM(size) as total_wal_bytes,
    pg_size_pretty(AVG(size)::bigint) as avg_wal_file_size,
    pg_size_pretty(MAX(size)) as max_wal_file_size,
    pg_size_pretty(MIN(size)) as min_wal_file_size
FROM pg_ls_waldir();"
else
    execute_sql_to_html "WAL File Directory Analysis" "
SELECT 
    CASE 
        WHEN $PG_VERSION < 100000 THEN 'Function not available in PostgreSQL < 10'
        ELSE 'Access denied or function unavailable'
    END as wal_file_count,
    'N/A' as total_wal_size,
    0 as total_wal_bytes,
    'N/A' as avg_wal_file_size,
    'N/A' as max_wal_file_size,
    'N/A' as min_wal_file_size;"
fi

# Add general recommendations for Storage & Capacity monitoring
cat >> "$OUTPUT_FILE" << 'EOF'
<div class="table-container">
<h3 style="font-size: 0.7em; margin: 0; padding: 0;">📋 General Recommendations for Storage & Capacity Monitoring</h3>
<div style="background: linear-gradient(135deg, #e8f4fd 0%, #f0f8ff 100%); padding: 15px; border-radius: 8px; border-left: 4px solid #3498db; margin: 15px 0; font-size: 0.7em; line-height: 1.3;">
    <div style="color: #2c3e50; margin: 0;"><strong>🔧 AWS Recommendations:</strong><br>
    <strong><a href="https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_PIOPS.StorageTypes.html#USER_PIOPS.Autoscaling" target="_blank" style="color: #3498db; text-decoration: none;">RDS Storage Autoscaling</a></strong>: Automatic storage increase when thresholds reached | <strong><a href="https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/monitoring-cloudwatch.html" target="_blank" style="color: #3498db; text-decoration: none;">CloudWatch metrics</a></strong>: FreeStorageSpace monitoring + automated capacity alerts</div>
    
    <div style="color: #2c3e50; margin-top: 10px;"><strong>🔍 PostgreSQL Extensions & Tools:</strong><br>
    <strong><a href="https://www.postgresql.org/docs/current/pgstattuple.html" target="_blank" style="color: #3498db; text-decoration: none;">pgstattuple</a></strong>: Detailed table and index bloat analysis | <strong><a href="https://www.postgresql.org/docs/current/monitoring-stats.html#MONITORING-PG-STAT-ALL-TABLES-VIEW" target="_blank" style="color: #3498db; text-decoration: none;">pg_stat_user_tables</a></strong>: Table size growth monitoring + vacuum effectiveness tracking</div>
</div>
</div>
EOF

echo '</div>' >> "$OUTPUT_FILE"
echo '</div>' >> "$OUTPUT_FILE"

# 6. QUERY & I/O PERFORMANCE
echo ""
echo "=============================================================================="
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting Section 6: QUERY & I/O PERFORMANCE"
echo "=============================================================================="
echo '<div class="section">' >> "$OUTPUT_FILE"
echo '<input type="checkbox" id="section6-toggle" class="accordion-checkbox">' >> "$OUTPUT_FILE"
echo '<label for="section6-toggle" class="accordion-label section-header" id="section6"><h2>6. QUERY & I/O PERFORMANCE (Metrics & Analysis)</h2></label>' >> "$OUTPUT_FILE"
echo '<div class="accordion-content">' >> "$OUTPUT_FILE"

if [ "$PG_VERSION" -ge 170000 ]; then
execute_sql_to_html "Checkpoint Statistics" "
WITH checkpoint_settings AS (
    SELECT current_setting('server_version_num')::integer as version_num
),
checkpoint_counts AS (
    SELECT num_timed as checkpoints_timed, num_requested as checkpoints_req FROM pg_stat_checkpointer
),
max_duration AS (
    SELECT max(extract(epoch from now() - stats_reset)::integer) as max_uptime_seconds
    FROM pg_stat_bgwriter
)
SELECT 
    metric,
    value,
    description,
    recommendation
FROM (
    -- Checkpoint Timeout
    SELECT 
        'Checkpoint Timeout' as metric,
        current_setting('checkpoint_timeout') as value,
        'Time between automatic WAL checkpoints' as description,
        CASE 
            WHEN current_setting('checkpoint_timeout') < '5min' THEN 'WARNING: Frequent checkpoints may impact performance. Consider increasing (recommended: 5min to 30min)'
            WHEN current_setting('checkpoint_timeout') > '30min' THEN 'WARNING: Long checkpoint intervals may lead to longer recovery times'
            ELSE 'Normal range (5min to 30min)'
        END as recommendation

    UNION ALL

    -- Checkpoint Completion Target
    SELECT 
        'Checkpoint Completion Target',
        current_setting('checkpoint_completion_target'),
        'Target of checkpoint completion in relation to checkpoint timeout interval',
        CASE 
            WHEN current_setting('checkpoint_completion_target')::float < 0.5 THEN 'WARNING: Low target may cause I/O spikes. Consider increasing (recommended: 0.8-0.9)'
            WHEN current_setting('checkpoint_completion_target')::float > 0.9 THEN 'WARNING: Very high target may cause rushes at checkpoint end'
            ELSE 'Normal range (0.5-0.9)'
        END

    UNION ALL

    -- Max WAL Size
    SELECT 
        'Maximum WAL Size',
        current_setting('max_wal_size'),
        'Maximum WAL size that triggers a checkpoint',
        'Normal (depends on your system capacity)'

    UNION ALL

    -- Min WAL Size
    SELECT 
        'Minimum WAL Size',
        current_setting('min_wal_size'),
        'Minimum WAL size to maintain',
        'Normal'

    UNION ALL

    -- Checkpoint Warning
    SELECT 
        'Checkpoint Warning',
        current_setting('checkpoint_warning'),
        'Warning threshold for checkpoint completion time',
        CASE 
            WHEN current_setting('checkpoint_warning')::interval < '30 seconds' THEN 'Consider increasing for better monitoring'
            ELSE 'Normal'
        END

    UNION ALL

    -- WAL Configuration
    SELECT 
        'WAL Configuration',
        'See description',
        format('Max WAL: %s, Min WAL: %s',
            current_setting('max_wal_size'),
            current_setting('min_wal_size')
        ),
        'Review if experiencing performance issues'

    UNION ALL

    -- Forced Checkpoint Ratio
    SELECT
        'Forced Checkpoint Ratio',
        CASE
            WHEN (SELECT COALESCE(checkpoints_timed, 0) + COALESCE(checkpoints_req, 0) FROM checkpoint_counts) = 0 THEN 'N/A'
            ELSE ROUND(
                (SELECT checkpoints_req * 100.0 / NULLIF(checkpoints_timed + checkpoints_req, 0) FROM checkpoint_counts)::numeric, 1
            )::text || '%'
        END,
        'Percentage of checkpoints forced (requested) vs timed',
        CASE
            WHEN (SELECT COALESCE(checkpoints_timed, 0) + COALESCE(checkpoints_req, 0) FROM checkpoint_counts) = 0 THEN 'No checkpoint data available'
            WHEN (SELECT checkpoints_req * 100.0 / NULLIF(checkpoints_timed + checkpoints_req, 0) FROM checkpoint_counts) > 50 THEN 'WARNING: High forced checkpoint ratio - consider increasing max_wal_size or checkpoint_timeout'
            WHEN (SELECT checkpoints_req * 100.0 / NULLIF(checkpoints_timed + checkpoints_req, 0) FROM checkpoint_counts) > 20 THEN 'INFO: Moderate forced checkpoint ratio - review WAL settings'
            ELSE 'Normal - most checkpoints are timed'
        END

    UNION ALL

    -- Buffer Writer Statistics
    SELECT 
        'Buffer Writer Stats',
        (SELECT buffers_clean::text FROM pg_stat_bgwriter),
        'Number of buffers written by background writer',
        CASE 
            WHEN (SELECT buffers_clean FROM pg_stat_bgwriter) = 0 THEN 'WARNING: Background writer might be inactive'
            ELSE 'Normal'
        END

    UNION ALL

    -- Uptime Information
    SELECT 
        'Database Uptime',
        (SELECT (max_uptime_seconds/3600)::text || ' hours' FROM max_duration),
        'Time since last statistics reset',
        CASE 
            WHEN (SELECT max_uptime_seconds FROM max_duration) < 3600 
            THEN 'Recently reset statistics'
            ELSE 'Normal'
        END

    UNION ALL

    -- Checkpoint-related settings summary
    SELECT 
        'Checkpoint Settings Summary',
        'See description',
        format(
            'Timeout: %s, Completion Target: %s',
            current_setting('checkpoint_timeout'),
            current_setting('checkpoint_completion_target')
        ),
        'Review settings if experiencing performance issues'

) as checkpoint_info
ORDER BY 
    CASE metric
        WHEN 'Checkpoint Settings Summary' THEN 1
        WHEN 'Checkpoint Timeout' THEN 2
        WHEN 'Checkpoint Completion Target' THEN 3
        WHEN 'Forced Checkpoint Ratio' THEN 4
        WHEN 'Maximum WAL Size' THEN 5
        WHEN 'Minimum WAL Size' THEN 6
        WHEN 'WAL Configuration' THEN 7
        WHEN 'Buffer Writer Stats' THEN 8
        WHEN 'Checkpoint Warning' THEN 9
        WHEN 'Database Uptime' THEN 10
        ELSE 11
    END;"


else
execute_sql_to_html "Checkpoint Statistics" "
WITH checkpoint_settings AS (
    SELECT current_setting('server_version_num')::integer as version_num
),
checkpoint_counts AS (
    SELECT checkpoints_timed, checkpoints_req FROM pg_stat_bgwriter
),
max_duration AS (
    SELECT max(extract(epoch from now() - stats_reset)::integer) as max_uptime_seconds
    FROM pg_stat_bgwriter
)
SELECT 
    metric,
    value,
    description,
    recommendation
FROM (
    -- Checkpoint Timeout
    SELECT 
        'Checkpoint Timeout' as metric,
        current_setting('checkpoint_timeout') as value,
        'Time between automatic WAL checkpoints' as description,
        CASE 
            WHEN current_setting('checkpoint_timeout') < '5min' THEN 'WARNING: Frequent checkpoints may impact performance. Consider increasing (recommended: 5min to 30min)'
            WHEN current_setting('checkpoint_timeout') > '30min' THEN 'WARNING: Long checkpoint intervals may lead to longer recovery times'
            ELSE 'Normal range (5min to 30min)'
        END as recommendation

    UNION ALL

    -- Checkpoint Completion Target
    SELECT 
        'Checkpoint Completion Target',
        current_setting('checkpoint_completion_target'),
        'Target of checkpoint completion in relation to checkpoint timeout interval',
        CASE 
            WHEN current_setting('checkpoint_completion_target')::float < 0.5 THEN 'WARNING: Low target may cause I/O spikes. Consider increasing (recommended: 0.8-0.9)'
            WHEN current_setting('checkpoint_completion_target')::float > 0.9 THEN 'WARNING: Very high target may cause rushes at checkpoint end'
            ELSE 'Normal range (0.5-0.9)'
        END

    UNION ALL

    -- Max WAL Size
    SELECT 
        'Maximum WAL Size',
        current_setting('max_wal_size'),
        'Maximum WAL size that triggers a checkpoint',
        'Normal (depends on your system capacity)'

    UNION ALL

    -- Min WAL Size
    SELECT 
        'Minimum WAL Size',
        current_setting('min_wal_size'),
        'Minimum WAL size to maintain',
        'Normal'

    UNION ALL

    -- Checkpoint Warning
    SELECT 
        'Checkpoint Warning',
        current_setting('checkpoint_warning'),
        'Warning threshold for checkpoint completion time',
        CASE 
            WHEN current_setting('checkpoint_warning')::interval < '30 seconds' THEN 'Consider increasing for better monitoring'
            ELSE 'Normal'
        END

    UNION ALL

    -- WAL Configuration
    SELECT 
        'WAL Configuration',
        'See description',
        format('Max WAL: %s, Min WAL: %s',
            current_setting('max_wal_size'),
            current_setting('min_wal_size')
        ),
        'Review if experiencing performance issues'

    UNION ALL

    -- Forced Checkpoint Ratio
    SELECT
        'Forced Checkpoint Ratio',
        CASE
            WHEN (SELECT COALESCE(checkpoints_timed, 0) + COALESCE(checkpoints_req, 0) FROM checkpoint_counts) = 0 THEN 'N/A'
            ELSE ROUND(
                (SELECT checkpoints_req * 100.0 / NULLIF(checkpoints_timed + checkpoints_req, 0) FROM checkpoint_counts)::numeric, 1
            )::text || '%'
        END,
        'Percentage of checkpoints forced (requested) vs timed',
        CASE
            WHEN (SELECT COALESCE(checkpoints_timed, 0) + COALESCE(checkpoints_req, 0) FROM checkpoint_counts) = 0 THEN 'No checkpoint data available'
            WHEN (SELECT checkpoints_req * 100.0 / NULLIF(checkpoints_timed + checkpoints_req, 0) FROM checkpoint_counts) > 50 THEN 'WARNING: High forced checkpoint ratio - consider increasing max_wal_size or checkpoint_timeout'
            WHEN (SELECT checkpoints_req * 100.0 / NULLIF(checkpoints_timed + checkpoints_req, 0) FROM checkpoint_counts) > 20 THEN 'INFO: Moderate forced checkpoint ratio - review WAL settings'
            ELSE 'Normal - most checkpoints are timed'
        END

    UNION ALL

    -- Buffer Writer Statistics
    SELECT 
        'Buffer Writer Stats',
        (SELECT buffers_clean::text FROM pg_stat_bgwriter),
        'Number of buffers written by background writer',
        CASE 
            WHEN (SELECT buffers_clean FROM pg_stat_bgwriter) = 0 THEN 'WARNING: Background writer might be inactive'
            ELSE 'Normal'
        END

    UNION ALL

    -- Uptime Information
    SELECT 
        'Database Uptime',
        (SELECT (max_uptime_seconds/3600)::text || ' hours' FROM max_duration),
        'Time since last statistics reset',
        CASE 
            WHEN (SELECT max_uptime_seconds FROM max_duration) < 3600 
            THEN 'Recently reset statistics'
            ELSE 'Normal'
        END

    UNION ALL

    -- Checkpoint-related settings summary
    SELECT 
        'Checkpoint Settings Summary',
        'See description',
        format(
            'Timeout: %s, Completion Target: %s',
            current_setting('checkpoint_timeout'),
            current_setting('checkpoint_completion_target')
        ),
        'Review settings if experiencing performance issues'

) as checkpoint_info
ORDER BY 
    CASE metric
        WHEN 'Checkpoint Settings Summary' THEN 1
        WHEN 'Checkpoint Timeout' THEN 2
        WHEN 'Checkpoint Completion Target' THEN 3
        WHEN 'Forced Checkpoint Ratio' THEN 4
        WHEN 'Maximum WAL Size' THEN 5
        WHEN 'Minimum WAL Size' THEN 6
        WHEN 'WAL Configuration' THEN 7
        WHEN 'Buffer Writer Stats' THEN 8
        WHEN 'Checkpoint Warning' THEN 9
        WHEN 'Database Uptime' THEN 10
        ELSE 11
    END;"


fi

execute_sql_to_html "Background Writer Statistics" "
WITH version_check AS (
    SELECT current_setting('server_version_num')::integer as version_num
),
bgwriter_stats AS (
    SELECT 
        buffers_clean,
        maxwritten_clean,
        buffers_alloc,
        stats_reset,
        extract(epoch from (now() - stats_reset))::bigint as stats_uptime_secs
    FROM pg_stat_bgwriter
),
computed_stats AS (
    SELECT 
        *,
        ROUND(buffers_clean::numeric / NULLIF(GREATEST(stats_uptime_secs, 1), 0), 2) as clean_rate_per_sec,
        ROUND(100.0 * buffers_clean::numeric / NULLIF(buffers_alloc, 0), 2) as clean_ratio
    FROM bgwriter_stats
)
SELECT 
    metric,
    value,
    description,
    recommendation
FROM (
    -- Version Information
    SELECT 
        'PostgreSQL Version' as metric,
        current_setting('server_version') as value,
        'Current PostgreSQL version' as description,
        'Informational' as recommendation

    UNION ALL

    -- Background Writer Clean Rate
    SELECT 
        'Buffers Clean Rate',
        clean_rate_per_sec::text || ' buffers/sec',
        'Average rate of buffers written by background writer',
        CASE 
            WHEN clean_rate_per_sec = 0 THEN 'WARNING: Background writer inactive'
            WHEN clean_rate_per_sec < 1 THEN 'Low background writer activity'
            WHEN clean_rate_per_sec > 1000 THEN 'High background writer activity'
            ELSE 'Normal activity'
        END
    FROM computed_stats

    UNION ALL

    -- Maxwritten Clean Events
    SELECT 
        'Maxwritten Clean Events',
        maxwritten_clean::text,
        'Number of times background writer stopped due to writing too many buffers',
        CASE 
            WHEN maxwritten_clean = 0 THEN 'No background writer stops'
            WHEN maxwritten_clean > 100 THEN 'Consider increasing bgwriter_lru_maxpages'
            ELSE 'Normal operation'
        END
    FROM computed_stats

    UNION ALL

    -- Buffer Allocation Stats
    SELECT 
        'Buffers Allocated',
        buffers_alloc::text,
        'Total number of buffers allocated',
        CASE 
            WHEN buffers_alloc = 0 THEN 'WARNING: No buffer allocations'
            ELSE 'Normal buffer allocation'
        END
    FROM computed_stats

    UNION ALL

    -- Background Writer Efficiency
    SELECT 
        'Background Writer Efficiency',
        clean_ratio::text || '%',
        'Percentage of total allocations handled by background writer',
        CASE 
            WHEN clean_ratio < 10 THEN 'Low efficiency - Consider tuning bgwriter parameters'
            WHEN clean_ratio > 90 THEN 'Very high background writer activity'
            ELSE 'Normal efficiency range'
        END
    FROM computed_stats

    UNION ALL

    -- Statistics Duration
    SELECT 
        'Statistics Duration',
        CASE 
            WHEN stats_uptime_secs < 60 THEN stats_uptime_secs::text || ' seconds'
            WHEN stats_uptime_secs < 3600 THEN ROUND((stats_uptime_secs/60.0)::numeric, 2)::text || ' minutes'
            ELSE ROUND((stats_uptime_secs/3600.0)::numeric, 2)::text || ' hours'
        END,
        'Time since statistics were last reset',
        CASE 
            WHEN stats_uptime_secs < 300 THEN 'Recently reset - statistics may not be representative'
            ELSE 'Sufficient duration for meaningful statistics'
        END
    FROM computed_stats

    UNION ALL

    -- Background Writer Settings
    SELECT 
        'Background Writer Settings',
        CONCAT(
            'delay: ', current_setting('bgwriter_delay'), ', ',
            'lru_maxpages: ', current_setting('bgwriter_lru_maxpages'), ', ',
            'lru_multiplier: ', current_setting('bgwriter_lru_multiplier')
        ),
        'Current background writer configuration',
        CASE 
            WHEN current_setting('bgwriter_lru_maxpages')::int < 100 THEN 'Consider increasing bgwriter_lru_maxpages'
            ELSE 'Review if experiencing performance issues'
        END

) as bgwriter_metrics
ORDER BY 
    CASE metric
        WHEN 'PostgreSQL Version' THEN 1
        WHEN 'Background Writer Settings' THEN 2
        WHEN 'Buffers Clean Rate' THEN 3
        WHEN 'Background Writer Efficiency' THEN 4
        WHEN 'Maxwritten Clean Events' THEN 5
        WHEN 'Buffers Allocated' THEN 6
        WHEN 'Statistics Duration' THEN 7
        ELSE 8
    END;"

execute_sql_to_html "Autovacuum Worker Utilization" "
WITH version_info AS (
    SELECT current_setting('server_version_num')::integer as version_num
),
autovacuum_activity AS (
    SELECT 
        COUNT(*) FILTER (
            WHERE CASE 
                WHEN EXISTS (SELECT 1 FROM version_info WHERE version_num >= 100000) THEN 
                    backend_type = 'autovacuum worker'
                ELSE 
                    query LIKE '%autovacuum%' OR query LIKE '%vacuum%'
                END
        ) as active_workers,
        COUNT(*) FILTER (
            WHERE query LIKE '%autoanalyze%' OR query LIKE '%analyze%'
        ) as active_analyze,
        current_setting('autovacuum_max_workers')::int as max_workers
    FROM pg_stat_activity
    WHERE state <> 'idle'
),
vacuum_stats AS (
    SELECT 
        COUNT(*) as total_tables,
        COUNT(*) FILTER (WHERE n_dead_tup > 0) as tables_needing_vacuum,
        COUNT(*) FILTER (WHERE n_dead_tup > 10000) as high_dead_tuples,
        ROUND(AVG(n_dead_tup) FILTER (WHERE n_dead_tup > 0))::bigint as avg_dead_tuples,
        MAX(n_dead_tup) as max_dead_tuples,
        ROUND(AVG(
            EXTRACT(EPOCH FROM (now() - COALESCE(last_autovacuum, last_vacuum)))
        ) FILTER (WHERE last_autovacuum IS NOT NULL OR last_vacuum IS NOT NULL))::bigint as avg_vacuum_age
    FROM pg_stat_user_tables
)
SELECT 
    metric,
    value,
    description,
    recommendation
FROM (
    -- Version Check
    SELECT 
        'PostgreSQL Version' as metric,
        current_setting('server_version') as value,
        'Current PostgreSQL version' as description,
        'Informational' as recommendation
    
    UNION ALL

    -- Active Workers
    SELECT 
        'Active Autovacuum Workers',
        active_workers::text || ' of ' || max_workers::text || ' workers',
        'Currently running autovacuum processes',
        CASE 
            WHEN active_workers >= max_workers THEN 
                'CRITICAL: All workers busy - consider increasing autovacuum_max_workers'
            WHEN active_workers >= (max_workers * 0.8) THEN 
                'WARNING: Worker pool nearly full - monitor closely'
            ELSE 'Normal utilization'
        END
    FROM autovacuum_activity

    UNION ALL

    -- Worker Configuration
    SELECT 
        'Autovacuum Configuration',
        format(
            'scale_factor: %s, analyze_scale_factor: %s, cost_delay: %s',
            current_setting('autovacuum_vacuum_scale_factor'),
            current_setting('autovacuum_analyze_scale_factor'),
            current_setting('autovacuum_vacuum_cost_delay')
        ),
        'Current autovacuum configuration parameters',
        CASE 
            WHEN current_setting('autovacuum_vacuum_scale_factor')::float > 0.2 THEN 
                'Consider reducing vacuum_scale_factor for more frequent vacuums'
            ELSE 'Configuration appears reasonable'
        END

    UNION ALL

    -- Tables Needing Vacuum
    SELECT 
        'Tables Needing Vacuum',
        tables_needing_vacuum::text || ' of ' || total_tables::text || ' tables',
        format('Average dead tuples: %s, Maximum: %s', 
               avg_dead_tuples, 
               max_dead_tuples),
        CASE 
            WHEN tables_needing_vacuum::float / NULLIF(total_tables, 0) > 0.3 THEN 
                'WARNING: Large percentage of tables need vacuum'
            WHEN high_dead_tuples > 5 THEN 
                'WARNING: Several tables have high dead tuple counts'
            ELSE 'Normal vacuum need'
        END
    FROM vacuum_stats

    UNION ALL

    -- Vacuum Age
    SELECT 
        'Average Vacuum Age',
        CASE 
            WHEN avg_vacuum_age < 3600 THEN round(avg_vacuum_age/60.0, 1)::text || ' minutes'
            WHEN avg_vacuum_age < 86400 THEN round(avg_vacuum_age/3600.0, 1)::text || ' hours'
            ELSE round(avg_vacuum_age/86400.0, 1)::text || ' days'
        END,
        'Average time since last vacuum across all tables',
        CASE 
            WHEN avg_vacuum_age > 86400 THEN 'WARNING: Long average time since last vacuum'
            WHEN avg_vacuum_age > 43200 THEN 'Consider reviewing vacuum frequency'
            ELSE 'Normal vacuum frequency'
        END
    FROM vacuum_stats

    UNION ALL

    -- Autovacuum Settings Check
    SELECT 
        'Autovacuum Settings',
        CASE WHEN current_setting('autovacuum')::boolean 
             THEN 'Enabled' 
             ELSE 'Disabled' 
        END,
        format('analyze_threshold: %s, vacuum_threshold: %s',
               current_setting('autovacuum_analyze_threshold'),
               current_setting('autovacuum_vacuum_threshold')),
        CASE 
            WHEN NOT current_setting('autovacuum')::boolean THEN 
                'CRITICAL: Autovacuum is disabled'
            ELSE 'Settings appear normal'
        END

) as autovacuum_metrics
ORDER BY 
    CASE metric
        WHEN 'PostgreSQL Version' THEN 1
        WHEN 'Autovacuum Settings' THEN 2
        WHEN 'Active Autovacuum Workers' THEN 3
        WHEN 'Autovacuum Configuration' THEN 4
        WHEN 'Tables Needing Vacuum' THEN 5
        WHEN 'Average Vacuum Age' THEN 6
        ELSE 7
    END;"

execute_sql_to_html "Connection Pooling Efficiency" "
WITH version_info AS (
    SELECT current_setting('server_version_num')::integer as version_num
),
connection_stats AS (
    SELECT 
        COUNT(*) as total_connections,
        COUNT(*) FILTER (WHERE state = 'active') as active_connections,
        COUNT(*) FILTER (WHERE state = 'idle') as idle_connections,
        COUNT(DISTINCT usename) as unique_users,
        COUNT(DISTINCT application_name) FILTER (WHERE application_name IS NOT NULL) as unique_applications,
        COUNT(DISTINCT client_addr) FILTER (WHERE client_addr IS NOT NULL) as unique_clients,
        current_setting('max_connections')::int as max_connections,
        COALESCE(current_setting('idle_in_transaction_session_timeout'), '0')::text as idle_timeout,
        current_setting('statement_timeout') as statement_timeout
    FROM pg_stat_activity
    WHERE pid != pg_backend_pid()
),
connection_age AS (
    SELECT 
        COUNT(*) FILTER (
            WHERE state = 'idle' 
            AND EXTRACT(EPOCH FROM (now() - state_change)) > 300
        ) as long_idle_connections,
        ROUND(AVG(EXTRACT(EPOCH FROM (now() - backend_start)))::numeric, 2) as avg_connection_age
    FROM pg_stat_activity
    WHERE pid != pg_backend_pid()
),
app_connections AS (
    SELECT 
        COALESCE(application_name, 'Unknown') as application_name,
        COUNT(*) as conn_count,
        COUNT(*) FILTER (WHERE state = 'active') as active_count,
        COUNT(*) FILTER (WHERE state = 'idle') as idle_count
    FROM pg_stat_activity
    WHERE pid != pg_backend_pid()
    GROUP BY application_name
    ORDER BY COUNT(*) DESC
    LIMIT 5
)
SELECT 
    metric,
    value,
    description,
    recommendation
FROM (
    -- Version Information
    SELECT 
        'PostgreSQL Version' as metric,
        current_setting('server_version') as value,
        'Current PostgreSQL version' as description,
        'Informational' as recommendation

    UNION ALL

    -- Connection Utilization
    SELECT 
        'Connection Utilization',
        total_connections::text || ' of ' || max_connections::text || 
        ' (' || ROUND(100.0 * total_connections / max_connections, 1)::text || '%)',
        'Current connection usage',
        CASE 
            WHEN total_connections::float / max_connections > 0.85 THEN 
                'CRITICAL: Near connection limit - Implement connection pooling immediately'
            WHEN total_connections::float / max_connections > 0.70 THEN 
                'WARNING: High connection usage - Consider connection pooling'
            ELSE 'Normal utilization'
        END
    FROM connection_stats

    UNION ALL

    -- Active vs Idle Connections
    SELECT 
        'Connection State Distribution',
        'Active: ' || active_connections::text || ', Idle: ' || idle_connections::text,
        'Distribution of connection states',
        CASE 
            WHEN idle_connections::float / NULLIF(total_connections, 0) > 0.75 THEN 
                'WARNING: High proportion of idle connections - Connection pooling recommended'
            WHEN idle_connections::float / NULLIF(total_connections, 0) > 0.50 THEN 
                'Consider implementing connection pooling'
            ELSE 'Normal distribution'
        END
    FROM connection_stats

    UNION ALL

    -- Long-Running Idle Connections
    SELECT 
        'Long Idle Connections',
        long_idle_connections::text || ' connections',
        'Connections idle for more than 5 minutes',
        CASE 
            WHEN long_idle_connections > 20 THEN 'CRITICAL: Many long-idle connections - Review application connection management'
            WHEN long_idle_connections > 10 THEN 'WARNING: Consider implementing connection timeout or pooling'
            ELSE 'Normal'
        END
    FROM connection_age

    UNION ALL

    -- Client Diversity
    SELECT 
        'Client Distribution',
        unique_users::text || ' users, ' || 
        unique_applications::text || ' applications, ' ||
        unique_clients::text || ' client IPs',
        'Distribution of connection sources',
        CASE 
            WHEN unique_applications = 1 AND total_connections > 10 THEN 
                'Single application with many connections - Implement connection pooling'
            WHEN total_connections::float / NULLIF(unique_applications, 0) > 10 THEN 
                'High connections per application - Consider pooling'
            ELSE 'Normal distribution'
        END
    FROM connection_stats

    UNION ALL

    -- Top Applications
    SELECT 
        'Top Applications',
        string_agg(application_name || ' (' || conn_count::text || ' connections)', ', '),
        'Applications with most connections',
        'Monitor high-connection applications'
    FROM app_connections

    UNION ALL

    -- Connection Timeout Settings
    SELECT 
        'Timeout Settings',
        'Idle timeout: ' || CASE 
            WHEN idle_timeout = '0' THEN 'disabled'
            ELSE idle_timeout
        END || ', Statement: ' || statement_timeout,
        'Current timeout configurations',
        CASE 
            WHEN idle_timeout = '0' THEN 
                'Consider setting idle_in_transaction_session_timeout'
            ELSE 'Timeout settings present'
        END
    FROM connection_stats

) metrics
ORDER BY 
    CASE metric
        WHEN 'PostgreSQL Version' THEN 1
        WHEN 'Connection Utilization' THEN 2
        WHEN 'Connection State Distribution' THEN 3
        WHEN 'Long Idle Connections' THEN 4
        WHEN 'Client Distribution' THEN 5
        WHEN 'Top Applications' THEN 6
        WHEN 'Timeout Settings' THEN 7
        ELSE 8
    END;"

execute_sql_to_html "Buffer Pool Hit Ratios by Database" "
WITH db_buffer_stats AS (
    SELECT 
        d.datname,
        COALESCE(s.blks_hit, 0) as total_hits,
        COALESCE(s.blks_read, 0) as total_reads,
        COALESCE(s.blks_hit + s.blks_read, 0) as total_accesses,
        pg_database_size(d.datname) as db_size
    FROM pg_database d
    LEFT JOIN pg_stat_database s ON d.oid = s.datid
    WHERE d.datallowconn AND NOT d.datistemplate
)
SELECT 
    datname as database_name,
    pg_size_pretty(db_size) as database_size,
    total_hits as buffer_hits,
    total_reads as disk_reads,
    total_accesses as total_buffer_accesses,
    COALESCE(
        ROUND(100.0 * total_hits / NULLIF(total_accesses, 0), 2),
        0
    ) as hit_ratio_percent,
    CASE 
        WHEN total_accesses = 0 THEN 'No activity'
        WHEN ROUND(100.0 * total_hits / NULLIF(total_accesses, 0), 2) < 90 THEN 'CRITICAL: Poor cache performance'
        WHEN ROUND(100.0 * total_hits / NULLIF(total_accesses, 0), 2) < 95 THEN 'WARNING: Below optimal cache performance'
        ELSE 'OK: Good cache performance'
    END as performance_status,
    CASE 
        WHEN total_accesses = 0 THEN 'Database has no recorded I/O activity'
        WHEN ROUND(100.0 * total_hits / NULLIF(total_accesses, 0), 2) < 90 THEN 'Consider increasing shared_buffers'
        WHEN ROUND(100.0 * total_hits / NULLIF(total_accesses, 0), 2) < 95 THEN 'Monitor workload and consider memory tuning'
        ELSE 'Cache performance is optimal'
    END as recommendation
FROM db_buffer_stats
ORDER BY hit_ratio_percent ASC, db_size DESC;"

execute_sql_to_html "Lock Tree and Deadlock Analysis" "
WITH lock_waits AS (
    SELECT 
        pid,
        datname,
        usename,
        application_name,
        state,
        wait_event_type,
        wait_event,
        query,
        EXTRACT(EPOCH FROM (now() - state_change))::integer as wait_duration_seconds,
        EXTRACT(EPOCH FROM (now() - query_start))::integer as query_duration_seconds,
        EXTRACT(EPOCH FROM (now() - xact_start))::integer as transaction_duration_seconds,
        backend_type
    FROM pg_stat_activity
    WHERE wait_event_type IS NOT NULL
    AND pid != pg_backend_pid()
),
lock_tree AS (
    SELECT 
        blocked.pid as blocked_pid,
        blocked.usename as blocked_user,
        blocked.query as blocked_query,
        blocking.pid as blocking_pid,
        blocking.usename as blocking_user,
        blocking.query as blocking_query,
        blocking.state as blocking_state,
        l.mode as lock_mode,
        l.locktype,
        l.relation::regclass as relation_name,
        EXTRACT(EPOCH FROM (now() - blocked.query_start))::integer as blocked_duration_seconds,
        EXTRACT(EPOCH FROM (now() - blocking.query_start))::integer as blocking_duration_seconds
    FROM pg_locks l
    JOIN pg_stat_activity blocked ON l.pid = blocked.pid
    JOIN pg_locks bl ON l.relation = bl.relation AND l.database = bl.database
    JOIN pg_stat_activity blocking ON bl.pid = blocking.pid
    WHERE NOT l.granted AND bl.granted
    AND blocked.pid != blocking.pid
),
deadlock_candidates AS (
    SELECT 
        t1.blocked_pid,
        t1.blocking_pid,
        t2.blocked_pid as potential_deadlock_with,
        t1.relation_name,
        'Potential circular dependency' as deadlock_type
    FROM lock_tree t1
    JOIN lock_tree t2 ON t1.blocked_pid = t2.blocking_pid AND t1.blocking_pid = t2.blocked_pid
),
lock_summary AS (
    SELECT 
        wait_event_type,
        wait_event,
        COUNT(*) as current_waiters,
        AVG(wait_duration_seconds)::integer as avg_wait_seconds,
        MAX(wait_duration_seconds) as max_wait_seconds,
        MIN(wait_duration_seconds) as min_wait_seconds,
        COUNT(*) FILTER (WHERE wait_duration_seconds > 60) as long_waiters_1min,
        COUNT(*) FILTER (WHERE wait_duration_seconds > 300) as long_waiters_5min
    FROM lock_waits
    WHERE wait_event_type = 'Lock'
    GROUP BY wait_event_type, wait_event
),
overall_wait_stats AS (
    SELECT 
        COUNT(*) as total_waiting_processes,
        COUNT(*) FILTER (WHERE wait_event_type = 'Lock') as lock_waiters,
        COUNT(*) FILTER (WHERE wait_event_type = 'IO') as io_waiters,
        COUNT(*) FILTER (WHERE wait_event_type NOT IN ('Lock', 'IO')) as other_waiters,
        AVG(wait_duration_seconds) FILTER (WHERE wait_event_type = 'Lock')::integer as avg_lock_wait_seconds,
        MAX(wait_duration_seconds) FILTER (WHERE wait_event_type = 'Lock') as max_lock_wait_seconds
    FROM lock_waits
)
SELECT 
    metric,
    value,
    description,
    details,
    recommendation
FROM (
    -- Overall Wait Statistics
    SELECT 
        'Overall Wait Statistics' as metric,
        total_waiting_processes::text as value,
        'Summary of all waiting processes' as description,
        format('Total waiting: %s, Lock waits: %s, IO waits: %s, Other waits: %s, Avg lock wait: %ss, Max lock wait: %ss',
            total_waiting_processes,
            lock_waiters,
            io_waiters, 
            other_waiters,
            COALESCE(avg_lock_wait_seconds, 0),
            COALESCE(max_lock_wait_seconds, 0)
        ) as details,
        CASE 
            WHEN lock_waiters > 10 THEN 'CRITICAL: High number of lock waiters'
            WHEN max_lock_wait_seconds > 300 THEN 'WARNING: Long lock waits detected'
            WHEN lock_waiters > 5 THEN 'WARNING: Moderate lock contention'
            ELSE 'OK'
        END as recommendation
    FROM overall_wait_stats
    
    UNION ALL
    
    -- Lock Wait Details
    SELECT 
        'Lock Wait Details' as metric,
        current_waiters::text as value,
        format('%s - %s', wait_event_type, wait_event) as description,
        format('Current waiters: %s, Avg wait: %ss, Max wait: %ss, Long waits (>1min): %s, Long waits (>5min): %s',
            current_waiters,
            avg_wait_seconds,
            max_wait_seconds,
            long_waiters_1min,
            long_waiters_5min
        ) as details,
        CASE 
            WHEN max_wait_seconds > 600 THEN 'CRITICAL: Very long lock waits (>10min)'
            WHEN max_wait_seconds > 300 THEN 'WARNING: Long lock waits (>5min)'
            WHEN long_waiters_1min > 5 THEN 'WARNING: Multiple long lock waits'
            ELSE 'Monitor'
        END as recommendation
    FROM lock_summary
    
    UNION ALL
    
    -- Current Lock Waiters Detail
    SELECT 
        'Current Lock Waiters' as metric,
        COUNT(*)::text as value,
        'Detailed view of current lock waiters' as description,
        COALESCE(string_agg(
            format('PID: %s, DB: %s, User: %s, App: %s, Wait: %s (%ss), Query: %s',
                pid,
                datname,
                usename,
                COALESCE(application_name, 'unknown'),
                wait_event,
                wait_duration_seconds,
                substring(query, 1, 50)
            ),
            E'\\n'
        ), 'No current lock waiters') as details,
        CASE 
            WHEN COUNT(*) > 0 THEN 'Review blocking queries and consider query optimization'
            ELSE 'No action needed'
        END as recommendation
    FROM lock_waits
    WHERE wait_event_type = 'Lock'
    
    UNION ALL
    
    -- Lock Tree Analysis
    SELECT 
        'Lock Tree Analysis' as metric,
        COUNT(*)::text as value,
        'Blocking relationships between sessions' as description,
        COALESCE(string_agg(
            format('BLOCKED: PID %s (%s) waiting for PID %s (%s) | Relation: %s | Lock: %s | Blocked: %ss | Blocking: %ss',
                blocked_pid,
                blocked_user,
                blocking_pid,
                blocking_user,
                COALESCE(relation_name::text, 'N/A'),
                lock_mode,
                blocked_duration_seconds,
                blocking_duration_seconds
            ),
            E'\\n'
        ), 'No blocking relationships found') as details,
        CASE 
            WHEN COUNT(*) > 10 THEN 'CRITICAL: High number of blocking relationships'
            WHEN COUNT(*) > 5 THEN 'WARNING: Multiple blocking relationships detected'
            WHEN COUNT(*) > 0 THEN 'INFO: Some blocking detected - monitor closely'
            ELSE 'OK: No blocking relationships'
        END as recommendation
    FROM lock_tree
    
    UNION ALL
    
    -- Deadlock Detection
    SELECT 
        'Deadlock Detection' as metric,
        COUNT(*)::text as value,
        'Potential circular lock dependencies' as description,
        COALESCE(string_agg(
            format('DEADLOCK CANDIDATE: PID %s <-> PID %s <-> PID %s on relation %s',
                blocked_pid,
                blocking_pid,
                potential_deadlock_with,
                COALESCE(relation_name::text, 'N/A')
            ),
            E'\\n'
        ), 'No potential deadlocks detected') as details,
        CASE 
            WHEN COUNT(*) > 0 THEN 'CRITICAL: Potential deadlock situation detected - investigate immediately'
            ELSE 'OK: No deadlock patterns detected'
        END as recommendation
    FROM deadlock_candidates
    
    UNION ALL
    
    -- Lock Chain Analysis
    SELECT 
        'Lock Chain Analysis' as metric,
        'Chain depth analysis' as value,
        'Analysis of lock dependency chains' as description,
        COALESCE((
            WITH RECURSIVE lock_chains AS (
                -- Base case: direct blocking relationships
                SELECT 
                    blocked_pid,
                    blocking_pid,
                    1 as chain_depth,
                    ARRAY[blocked_pid, blocking_pid] as chain_path
                FROM lock_tree
                
                UNION ALL
                
                -- Recursive case: extend chains
                SELECT 
                    lc.blocked_pid,
                    lt.blocking_pid,
                    lc.chain_depth + 1,
                    lc.chain_path || lt.blocking_pid
                FROM lock_chains lc
                JOIN lock_tree lt ON lc.blocking_pid = lt.blocked_pid
                WHERE lc.chain_depth < 10  -- Prevent infinite recursion
                AND NOT (lt.blocking_pid = ANY(lc.chain_path))  -- Prevent cycles
            )
            SELECT string_agg(
                format('Chain depth %s: %s sessions involved',
                    chain_depth,
                    array_length(chain_path, 1)
                ),
                E'\\n'
            )
            FROM (
                SELECT chain_depth, chain_path
                FROM lock_chains
                WHERE chain_depth = (SELECT MAX(chain_depth) FROM lock_chains)
                LIMIT 5
            ) chains
        ), 'No lock chains detected') as details,
        CASE 
            WHEN EXISTS (SELECT 1 FROM lock_tree) THEN 'Monitor lock chains for performance impact'
            ELSE 'No lock chains to analyze'
        END as recommendation
    
    UNION ALL
    
    -- Historical Deadlock Information
    SELECT 
        'Historical Deadlocks' as metric,
        'Database statistics' as value,
        'Deadlock count from pg_stat_database' as description,
        COALESCE((
            SELECT string_agg(
                format('Database: %s, Deadlocks: %s',
                    datname,
                    deadlocks
                ),
                E'\\n'
            )
            FROM pg_stat_database
            WHERE deadlocks > 0
        ), 'No historical deadlocks recorded') as details,
        CASE 
            WHEN EXISTS (SELECT 1 FROM pg_stat_database WHERE deadlocks > 100)
            THEN 'WARNING: High historical deadlock count - review application logic'
            WHEN EXISTS (SELECT 1 FROM pg_stat_database WHERE deadlocks > 10)
            THEN 'INFO: Some historical deadlocks - monitor patterns'
            ELSE 'OK: Low or no historical deadlocks'
        END as recommendation

) wait_metrics
ORDER BY 
    CASE metric
        WHEN 'Overall Wait Statistics' THEN 1
        WHEN 'Lock Tree Analysis' THEN 2
        WHEN 'Deadlock Detection' THEN 3
        WHEN 'Lock Chain Analysis' THEN 4
        WHEN 'Historical Deadlocks' THEN 5
        WHEN 'Lock Wait Details' THEN 6
        WHEN 'Current Lock Waiters' THEN 7
    END;"

execute_sql_to_html "I/O Statistics" "
WITH table_io_stats AS (
    SELECT 
        schemaname,
        relname as tablename,
        relid,
        heap_blks_read,
        heap_blks_hit,
        idx_blks_read,
        idx_blks_hit,
        toast_blks_read,
        toast_blks_hit,
        tidx_blks_read,
        tidx_blks_hit,
        -- Use OID-based functions instead of string concatenation for safety and performance
        -- Add error handling with COALESCE to handle inaccessible tables
        COALESCE(pg_total_relation_size(relid), 0) as total_size_bytes,
        COALESCE(pg_relation_size(relid), 0) as table_size_bytes,
        COALESCE(pg_indexes_size(relid), 0) as index_size_bytes
    FROM pg_statio_user_tables
    WHERE heap_blks_read > 0
),
table_stats AS (
    SELECT 
        schemaname,
        relname as tablename,
        relid,
        n_live_tup,
        n_dead_tup,
        seq_scan,
        idx_scan,
        n_tup_ins,
        n_tup_upd,
        n_tup_del,
        n_tup_hot_upd,
        last_vacuum,
        last_autovacuum,
        last_analyze,
        last_autoanalyze
    FROM pg_stat_user_tables
    WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
)
SELECT 
    tio.schemaname,
    tio.tablename,
    CASE 
        WHEN tio.total_size_bytes = 0 THEN 'N/A (inaccessible)'
        ELSE pg_size_pretty(tio.total_size_bytes)
    END as total_size,
    CASE 
        WHEN tio.table_size_bytes = 0 THEN 'N/A (inaccessible)'
        ELSE pg_size_pretty(tio.table_size_bytes)
    END as table_size,
    CASE 
        WHEN tio.index_size_bytes = 0 THEN 'N/A (inaccessible)'
        ELSE pg_size_pretty(tio.index_size_bytes)
    END as index_size,
    
    -- Heap (Table) Statistics
    tio.heap_blks_read as table_blocks_read,
    tio.heap_blks_hit as table_blocks_hit,
    ROUND(100.0 * tio.heap_blks_hit / NULLIF(tio.heap_blks_hit + tio.heap_blks_read, 0), 2) as table_cache_hit_ratio,
    
    -- Index Statistics
    tio.idx_blks_read as index_blocks_read,
    tio.idx_blks_hit as index_blocks_hit,
    ROUND(100.0 * tio.idx_blks_hit / NULLIF(tio.idx_blks_hit + tio.idx_blks_read, 0), 2) as index_cache_hit_ratio,
    
    -- Overall Statistics
    ROUND(100.0 * (tio.heap_blks_hit + tio.idx_blks_hit + tio.toast_blks_hit + tio.tidx_blks_hit)::numeric /
        NULLIF((tio.heap_blks_hit + tio.heap_blks_read + 
                tio.idx_blks_hit + tio.idx_blks_read + 
                tio.toast_blks_hit + tio.toast_blks_read + 
                tio.tidx_blks_hit + tio.tidx_blks_read)::numeric, 0), 2) as overall_cache_hit_ratio,
    
    -- Health Check
    CASE 
        WHEN tio.heap_blks_read > 1000 AND 
             (tio.heap_blks_hit::float / NULLIF(tio.heap_blks_hit + tio.heap_blks_read, 0)) < 0.95 
        THEN 'CRITICAL: Poor cache hit ratio'
        WHEN ts.n_dead_tup > 10000 AND (ts.n_dead_tup::float / NULLIF(ts.n_live_tup + ts.n_dead_tup, 0)) > 0.1
        THEN 'WARNING: High dead tuple ratio'
        WHEN ts.seq_scan > 1000 AND (ts.idx_scan::float / NULLIF(ts.seq_scan + ts.idx_scan, 0)) < 0.1
        THEN 'WARNING: High sequential scan ratio'
        ELSE 'OK'
    END as health_status,
    
    -- Recommendations
    CASE 
        WHEN tio.heap_blks_read > 1000 AND 
             (tio.heap_blks_hit::float / NULLIF(tio.heap_blks_hit + tio.heap_blks_read, 0)) < 0.95 
        THEN 'Consider increasing shared_buffers or work_mem'
        WHEN ts.n_dead_tup > 10000 AND (ts.n_dead_tup::float / NULLIF(ts.n_live_tup + ts.n_dead_tup, 0)) > 0.1
        THEN 'Consider running VACUUM'
        WHEN ts.seq_scan > 1000 AND (ts.idx_scan::float / NULLIF(ts.seq_scan + ts.idx_scan, 0)) < 0.1
        THEN 'Consider adding indexes'
        ELSE 'No immediate action needed'
    END as recommendation
FROM table_io_stats tio
JOIN table_stats ts ON tio.relid = ts.relid
ORDER BY tio.heap_blks_read DESC
LIMIT 20;"

# Version-compatible Query Performance Analysis with pg_stat_statements availability check
# Generate primary SQL for pg_stat_statements (version-aware)
    QUERY_PERF_PRIMARY_SQL_PG14="
WITH frequent_queries AS (
    SELECT 
        queryid,
        COALESCE(query, '<query text not available>') as query,
        calls,
        total_exec_time::numeric as execution_time,
        mean_exec_time::numeric as mean_time,
        stddev_exec_time::numeric as stddev_time,
        min_exec_time::numeric as min_time,
        max_exec_time::numeric as max_time,
        rows as query_rows,
        shared_blks_hit,
        shared_blks_read,
        shared_blks_written,
        temp_blks_read,
        temp_blks_written,
        -- Use stats reset time from pg_stat_statements_info (PostgreSQL 14+)
        (SELECT stats_reset FROM pg_stat_statements_info) as first_seen,
        CURRENT_TIMESTAMP as last_seen,
        -- Query type classification via regex
        CASE
            WHEN query ~* '^\\\\s*(WITH\\\\s+.+\\\\s+)?INSERT\\\\s' THEN 'INSERT'
            WHEN query ~* '^\\\\s*(WITH\\\\s+.+\\\\s+)?UPDATE\\\\s' THEN 'UPDATE'
            WHEN query ~* '^\\\\s*(WITH\\\\s+.+\\\\s+)?DELETE\\\\s' THEN 'DELETE'
            WHEN query ~* '^\\\\s*WITH\\\\s+.*\\\\b(INSERT|UPDATE|DELETE)\\\\b' THEN 'CTE-WRITE'
            WHEN query ~* '^\\\\s*(WITH\\\\s+.+\\\\s+)?SELECT\\\\s' THEN 'SELECT'
            ELSE 'OTHER'
        END as query_type,
        -- WAL metrics (PG 13+)
        COALESCE(wal_records, 0) as wal_records,
        COALESCE(wal_fpi, 0) as wal_fpi,
        COALESCE(wal_bytes, 0) as wal_bytes,
        -- Planning time (PG 13+)
        COALESCE(plans, 0) as plans,
        COALESCE(total_plan_time, 0)::numeric as total_plan_time,
        COALESCE(mean_plan_time, 0)::numeric as mean_plan_time,
        -- I/O wait time (requires track_io_timing)
        COALESCE(blk_read_time, 0)::numeric as blk_read_time,
        COALESCE(blk_write_time, 0)::numeric as blk_write_time,
        -- Local blocks
        COALESCE(local_blks_hit, 0) as local_blks_hit,
        COALESCE(local_blks_read, 0) as local_blks_read,
        COALESCE(local_blks_written, 0) as local_blks_written,
        -- JIT stats (not available in PG 14, use zero placeholders)
        0 as jit_functions,
        0::numeric as jit_generation_time,
        0::numeric as jit_inlining_time,
        0::numeric as jit_optimization_time,
        0::numeric as jit_emission_time
    FROM pg_stat_statements
    WHERE calls > 10
        AND query IS NOT NULL
        AND query != '<insufficient privilege>'
),
query_metrics AS (
    SELECT
        queryid,
        query,
        calls,
        execution_time as total_time_ms,
        ROUND((execution_time / NULLIF(calls, 0))::numeric, 2) as avg_time_ms,
        ROUND(mean_time::numeric, 2) as mean_time_ms,
        ROUND(COALESCE(stddev_time, 0)::numeric, 2) as stddev_time_ms,
        ROUND(min_time::numeric, 2) as min_time_ms,
        ROUND(max_time::numeric, 2) as max_time_ms,
        ROUND((query_rows::float / NULLIF(calls, 0))::numeric, 2) as avg_rows,
        ROUND((100.0 * shared_blks_hit / NULLIF(shared_blks_hit + shared_blks_read, 0))::numeric, 2) as cache_hit_ratio,
        (temp_blks_read + temp_blks_written) as temp_blocks,
        (shared_blks_read + shared_blks_written) as total_io_blocks,
        shared_blks_hit,
        shared_blks_read,
        shared_blks_written,
        ROUND((NULLIF(stddev_time, 0) / NULLIF(mean_time, 0))::numeric, 2) as time_variability,
        ROUND((query_rows::numeric / NULLIF(execution_time, 0))::numeric, 2) as rows_per_ms,
        pg_size_pretty((COALESCE(shared_blks_read + shared_blks_written, 0)::bigint * 8192)::bigint) as total_io_size,
        pg_size_pretty((COALESCE(temp_blks_read + temp_blks_written, 0)::bigint * 8192)::bigint) as total_temp_size,
        first_seen,
        last_seen,
        EXTRACT(EPOCH FROM (last_seen - first_seen)) as active_seconds,
        ROUND((calls::numeric / NULLIF(EXTRACT(EPOCH FROM (last_seen - first_seen)), 0))::numeric, 4) as queries_per_second,
        (SELECT string_agg(DISTINCT nspname, ', ')
         FROM pg_namespace
         WHERE nspname != 'pg_catalog' AND nspname != 'information_schema'
           AND nspname !~ '^pg_'
           AND query ~* ('\\\\m' || nspname || '\\\\.')) as detected_schemas,
        (SELECT string_agg(DISTINCT CASE WHEN schemaname IS NOT NULL THEN schemaname || '.' || tablename ELSE tablename END, ', ')
         FROM pg_tables
         WHERE (schemaname || '.' || tablename) = ANY(string_to_array(regexp_replace(regexp_replace(query, '\\$[0-9]+', '', 'g'), '[^a-zA-Z0-9_.]', ' ', 'g'), ' '))
            OR tablename = ANY(string_to_array(regexp_replace(regexp_replace(query, '\\$[0-9]+', '', 'g'), '[^a-zA-Z0-9_.]', ' ', 'g'), ' '))
         LIMIT 10) as referenced_tables,
        -- Query type passthrough
        query_type,
        -- WAL metrics
        wal_records,
        wal_fpi,
        wal_bytes,
        pg_size_pretty(wal_bytes::bigint) as wal_size,
        CASE
            WHEN wal_bytes > 0 THEN ROUND((wal_bytes / NULLIF(calls, 0))::numeric, 2)
            ELSE 0
        END as wal_bytes_per_call,
        -- Planning time
        plans,
        ROUND(total_plan_time::numeric, 2) as total_plan_time_ms,
        ROUND(mean_plan_time::numeric, 2) as mean_plan_time_ms,
        CASE
            WHEN (total_plan_time + execution_time) > 0
            THEN ROUND((100.0 * total_plan_time / (total_plan_time + execution_time))::numeric, 2)
            ELSE 0
        END as plan_time_pct,
        -- I/O wait time
        ROUND(blk_read_time::numeric, 2) as blk_read_time_ms,
        ROUND(blk_write_time::numeric, 2) as blk_write_time_ms,
        CASE
            WHEN execution_time > 0
            THEN ROUND((100.0 * (blk_read_time + blk_write_time) / execution_time)::numeric, 2)
            ELSE 0
        END as io_wait_pct,
        CASE
            WHEN (blk_read_time + blk_write_time) = 0 AND (shared_blks_read + shared_blks_written) > 100
            THEN 'track_io_timing may be OFF'
            ELSE 'OK'
        END as io_timing_status,
        -- Local blocks
        local_blks_hit,
        local_blks_read,
        local_blks_written,
        pg_size_pretty(((local_blks_read + local_blks_written)::bigint * 8192)::bigint) as local_io_size,
        -- Write ratio
        CASE
            WHEN (shared_blks_hit + shared_blks_read + shared_blks_written) > 0
            THEN ROUND((100.0 * shared_blks_written / (shared_blks_hit + shared_blks_read + shared_blks_written))::numeric, 2)
            ELSE 0
        END as write_ratio_pct,
        CASE
            WHEN shared_blks_written = 0 THEN 'READ-ONLY'
            WHEN shared_blks_written > (shared_blks_hit + shared_blks_read) THEN 'WRITE-HEAVY'
            WHEN shared_blks_written > 0 THEN 'MIXED'
            ELSE 'READ-ONLY'
        END as io_pattern,
        -- JIT stats
        ROUND((jit_generation_time + jit_inlining_time + jit_optimization_time + jit_emission_time)::numeric, 2) as total_jit_time_ms,
        CASE
            WHEN jit_functions > 0 AND (jit_generation_time + jit_inlining_time + jit_optimization_time + jit_emission_time) > 0
            THEN CASE
                WHEN (jit_generation_time + jit_inlining_time + jit_optimization_time + jit_emission_time) > execution_time * 0.5
                THEN 'POOR: JIT overhead > 50%% of exec time'
                WHEN (jit_generation_time + jit_inlining_time + jit_optimization_time + jit_emission_time) > execution_time * 0.2
                THEN 'FAIR: JIT overhead 20-50%% of exec time'
                ELSE 'GOOD: JIT overhead < 20%% of exec time'
            END
            ELSE 'N/A: No JIT activity'
        END as jit_efficiency
    FROM frequent_queries
),
query_analysis AS (
    SELECT
        queryid,
        CASE 
            WHEN query IS NULL OR query = '' THEN '<no query text>'
            WHEN query = '<insufficient privilege>' THEN '<insufficient privilege>'
            ELSE query
        END as query_excerpt,
        COALESCE(detected_schemas, 'public or unspecified') as schemas,
        COALESCE(referenced_tables, 'N/A') as tables,
        calls as execution_count,
        first_seen,
        last_seen,
        ROUND(active_seconds::numeric / 86400, 1) as days_active,
        queries_per_second,
        total_time_ms,
        avg_time_ms,
        mean_time_ms,
        stddev_time_ms,
        min_time_ms,
        max_time_ms,
        time_variability,
        avg_rows,
        rows_per_ms,
        COALESCE(cache_hit_ratio, 0) as cache_hit_ratio,
        COALESCE(temp_blocks, 0) as total_temp_blocks,
        COALESCE(total_io_blocks, 0) as total_io_blocks,
        total_io_size,
        total_temp_size,
        shared_blks_hit,
        shared_blks_read,
        shared_blks_written,
        -- New analysis columns
        query_type,
        -- WAL impact
        wal_records,
        wal_fpi,
        wal_size,
        wal_bytes_per_call,
        CASE
            WHEN wal_bytes > 1073741824 THEN 'CRITICAL: > 1GB WAL generated'
            WHEN wal_bytes > 104857600 THEN 'HIGH: > 100MB WAL generated'
            WHEN wal_bytes > 10485760 THEN 'MODERATE: > 10MB WAL generated'
            WHEN wal_bytes > 0 THEN 'LOW'
            ELSE 'NONE'
        END as wal_impact,
        -- Planning time
        plans,
        total_plan_time_ms,
        mean_plan_time_ms,
        plan_time_pct,
        -- I/O wait
        blk_read_time_ms,
        blk_write_time_ms,
        io_wait_pct,
        io_timing_status,
        CASE
            WHEN io_wait_pct > 50 THEN 'IO-BOUND'
            WHEN io_wait_pct > 20 THEN 'IO-MIXED'
            WHEN io_wait_pct > 0 THEN 'CPU-BOUND'
            ELSE 'UNKNOWN'
        END as bottleneck_type,
        -- Local blocks
        local_blks_hit,
        local_blks_read,
        local_blks_written,
        local_io_size,
        -- Write ratio
        write_ratio_pct,
        io_pattern,
        -- JIT stats
        total_jit_time_ms,
        jit_efficiency,
        -- Lock risk assessment
        CASE
            WHEN query_type IN ('UPDATE', 'DELETE') AND avg_time_ms > 500 AND calls > 100
            THEN 'HIGH: Long-running writes with high frequency'
            WHEN query_type IN ('UPDATE', 'DELETE') AND avg_time_ms > 1000
            THEN 'HIGH: Very slow write operations'
            WHEN query_type IN ('UPDATE', 'DELETE', 'CTE-WRITE') AND time_variability > 2
            THEN 'MEDIUM: Write ops with high variance (possible lock waits)'
            WHEN query_type IN ('INSERT', 'UPDATE', 'DELETE', 'CTE-WRITE')
            THEN 'LOW: Normal write operations'
            ELSE 'MINIMAL: Read-only query'
        END as lock_risk,
        -- Cost impact score (0-100, higher = more impactful)
        ROUND(LEAST(100, (
            -- Execution time component (0-30)
            CASE
                WHEN avg_time_ms > 5000 THEN 30
                WHEN avg_time_ms > 1000 THEN 25
                WHEN avg_time_ms > 500 THEN 20
                WHEN avg_time_ms > 100 THEN 10
                ELSE 5
            END +
            -- Frequency component (0-25)
            CASE
                WHEN calls >= 100000 THEN 25
                WHEN calls >= 10000 THEN 20
                WHEN calls >= 1000 THEN 10
                ELSE 5
            END +
            -- I/O component (0-20)
            CASE
                WHEN COALESCE(cache_hit_ratio, 0) < 50 THEN 20
                WHEN COALESCE(cache_hit_ratio, 0) < 80 THEN 15
                WHEN COALESCE(cache_hit_ratio, 0) < 95 THEN 10
                ELSE 0
            END +
            -- WAL component (0-15)
            CASE
                WHEN wal_bytes > 1073741824 THEN 15
                WHEN wal_bytes > 104857600 THEN 10
                WHEN wal_bytes > 10485760 THEN 5
                ELSE 0
            END +
            -- Temp usage component (0-10)
            CASE
                WHEN COALESCE(temp_blocks, 0) > 10000 THEN 10
                WHEN COALESCE(temp_blocks, 0) > 1000 THEN 5
                ELSE 0
            END
        ))::numeric, 0) as cost_impact_score,
        CASE
            WHEN avg_time_ms > 1000 THEN 'VERY SLOW'
            WHEN avg_time_ms > 500 THEN 'SLOW'
            WHEN avg_time_ms > 100 THEN 'MEDIUM'
            ELSE 'FAST'
        END as speed_classification,
        CASE
            WHEN calls >= 100000 THEN 'VERY HIGH'
            WHEN calls >= 10000 THEN 'HIGH'
            WHEN calls >= 1000 THEN 'MEDIUM'
            ELSE 'LOW'
        END as frequency_category,
        CASE
            WHEN time_variability > 2 THEN 'INCONSISTENT: High variance'
            WHEN time_variability > 1 THEN 'VARIABLE: Moderate variance'
            ELSE 'CONSISTENT: Stable'
        END as performance_consistency,
        CASE
            WHEN COALESCE(cache_hit_ratio, 0) >= 99 THEN 'EXCELLENT'
            WHEN COALESCE(cache_hit_ratio, 0) >= 95 THEN 'GOOD'
            WHEN COALESCE(cache_hit_ratio, 0) >= 90 THEN 'FAIR'
            WHEN COALESCE(cache_hit_ratio, 0) >= 80 THEN 'POOR'
            ELSE 'CRITICAL'
        END as io_efficiency,
        CASE
            WHEN temp_blocks = 0 THEN 'NONE'
            WHEN temp_blocks < 100 THEN 'LOW'
            WHEN temp_blocks < 1000 THEN 'MODERATE'
            WHEN temp_blocks < 10000 THEN 'HIGH'
            ELSE 'CRITICAL'
        END as temp_usage,
        CASE
            WHEN avg_time_ms > 1000 AND calls > 1000 THEN 'CRITICAL: Frequently used slow query'
            WHEN avg_time_ms > 1000 THEN 'CRITICAL: High average execution time'
            WHEN time_variability > 2 THEN 'WARNING: High execution time variability'
            WHEN COALESCE(cache_hit_ratio, 0) < 80 AND shared_blks_read > 1000 THEN 'WARNING: Poor cache performance'
            WHEN temp_blocks > 10000 THEN 'WARNING: High temporary space usage'
            ELSE 'OK'
        END as health_status,
        CASE
            WHEN avg_time_ms > 1000 AND calls > 1000 THEN
                CASE
                    WHEN COALESCE(cache_hit_ratio, 0) < 80 THEN 'CRITICAL: Add indexes and increase shared_buffers. Cache hit: ' || COALESCE(cache_hit_ratio, 0)::text || '%'
                    WHEN temp_blocks > 10000 THEN 'CRITICAL: Increase work_mem to avoid disk writes. Temp usage: ' || total_temp_size
                    ELSE 'CRITICAL: Review query plan and optimize logic'
                END
            WHEN time_variability > 2 THEN
                'WARNING: High variance (' || time_variability::text || 'x) - check for parameter sniffing or lock contention'
            WHEN COALESCE(cache_hit_ratio, 0) < 80 AND shared_blks_read > 1000 THEN
                'WARNING: Poor cache hit ratio (' || COALESCE(cache_hit_ratio, 0)::text || '%) - review indexing strategy'
            WHEN temp_blocks > 10000 THEN
                'WARNING: High temp usage (' || total_temp_size || ') - review sort/join operations'
            WHEN avg_time_ms > 500 THEN
                'REVIEW: Consider optimization if frequently used'
            ELSE 'OK: No immediate action needed'
        END as recommendation,
        ROUND(((
            CASE
                WHEN avg_time_ms > 1000 THEN 0
                WHEN avg_time_ms > 500 THEN 25
                WHEN avg_time_ms > 100 THEN 50
                ELSE 100
            END +
            COALESCE(CASE
                WHEN cache_hit_ratio < 50 THEN 0
                WHEN cache_hit_ratio < 80 THEN 25
                WHEN cache_hit_ratio < 95 THEN 50
                ELSE 100
            END, 100) +
            CASE
                WHEN time_variability > 2 THEN 0
                WHEN time_variability > 1 THEN 50
                ELSE 100
            END
        ) / 3)::numeric, 2) as performance_score,
        CASE
            WHEN calls >= 100000 AND avg_time_ms < 10 THEN
                'This query runs very frequently and is fast. It''s well-optimized.'
            WHEN calls >= 100000 AND avg_time_ms >= 10 THEN
                'This query runs very frequently. Even small improvements will have big impact.'
            WHEN avg_time_ms >= 1000 THEN
                'This query is slow. Users may experience delays when this runs.'
            WHEN COALESCE(cache_hit_ratio, 0) < 90 THEN
                'This query reads a lot from disk instead of memory. Adding indexes could help.'
            WHEN temp_blocks > 1000 THEN
                'This query uses temporary disk space, which slows it down. It needs more memory or optimization.'
            WHEN time_variability > 1 THEN
                'This query has unpredictable performance. Sometimes fast, sometimes slow.'
            ELSE
                'This query is performing reasonably well.'
        END as performance_insight,
        format('Executed %s times | Avg: %sms (min: %sms, max: %sms) | Returns ~%s rows | Cache: %s%% | Temp: %s | QPS: %s',
               calls,
               ROUND(avg_time_ms::numeric, 1),
               ROUND(min_time_ms::numeric, 1),
               ROUND(max_time_ms::numeric, 1),
               ROUND(avg_rows::numeric, 0),
               ROUND(COALESCE(cache_hit_ratio, 0)::numeric, 1),
               total_temp_size,
               ROUND(queries_per_second::numeric, 2)) as performance_summary
    FROM query_metrics
)
SELECT 
    schemas,
    tables,
    query_excerpt,
    execution_count,
    first_seen,
    last_seen,
    days_active,
    queries_per_second,
    performance_score,
    health_status,
    speed_classification,
    frequency_category,
    performance_summary,
    performance_insight,
    performance_consistency,
    io_efficiency,
    temp_usage,
    recommendation,
    ROUND(total_time_ms::numeric, 2) as total_time_ms,
    ROUND(avg_time_ms::numeric, 2) as avg_time_ms,
    ROUND(min_time_ms::numeric, 2) as min_time_ms,
    ROUND(max_time_ms::numeric, 2) as max_time_ms,
    ROUND(stddev_time_ms::numeric, 2) as stddev_time_ms,
    time_variability,
    ROUND(avg_rows::numeric, 2) as avg_rows,
    rows_per_ms,
    shared_blks_read as disk_reads,
    shared_blks_hit as cache_hits,
    ROUND(cache_hit_ratio::numeric, 2) as cache_hit_ratio,
    total_io_blocks,
    total_io_size,
    total_temp_blocks,
    total_temp_size,
    query_type,
    wal_records,
    wal_fpi,
    wal_size,
    wal_bytes_per_call,
    wal_impact,
    plans,
    total_plan_time_ms,
    mean_plan_time_ms,
    plan_time_pct,
    blk_read_time_ms,
    blk_write_time_ms,
    io_wait_pct,
    io_timing_status,
    bottleneck_type,
    local_blks_hit,
    local_blks_read,
    local_blks_written,
    local_io_size,
    write_ratio_pct,
    io_pattern,
    total_jit_time_ms,
    jit_efficiency,
    lock_risk,
    cost_impact_score
FROM query_analysis
ORDER BY 
    CASE 
        WHEN health_status LIKE 'CRITICAL%' THEN 1
        WHEN health_status LIKE 'WARNING%' THEN 2
        ELSE 3
    END,
    performance_score ASC,
    execution_count DESC
LIMIT 20;"


if version_gte "$PG_VERSION" 170000; then
    # PostgreSQL 17+ renamed blk_read_time -> shared_blk_read_time, blk_write_time -> shared_blk_write_time
    QUERY_PERF_PRIMARY_SQL="
WITH frequent_queries AS (
    SELECT 
        queryid,
        COALESCE(query, '<query text not available>') as query,
        calls,
        total_exec_time::numeric as execution_time,
        mean_exec_time::numeric as mean_time,
        stddev_exec_time::numeric as stddev_time,
        min_exec_time::numeric as min_time,
        max_exec_time::numeric as max_time,
        rows as query_rows,
        shared_blks_hit,
        shared_blks_read,
        shared_blks_written,
        temp_blks_read,
        temp_blks_written,
        -- Use stats reset time from pg_stat_statements_info (PostgreSQL 14+)
        (SELECT stats_reset FROM pg_stat_statements_info) as first_seen,
        CURRENT_TIMESTAMP as last_seen,
        -- Query type classification via regex
        CASE
            WHEN query ~* '^\\\\s*(WITH\\\\s+.+\\\\s+)?INSERT\\\\s' THEN 'INSERT'
            WHEN query ~* '^\\\\s*(WITH\\\\s+.+\\\\s+)?UPDATE\\\\s' THEN 'UPDATE'
            WHEN query ~* '^\\\\s*(WITH\\\\s+.+\\\\s+)?DELETE\\\\s' THEN 'DELETE'
            WHEN query ~* '^\\\\s*WITH\\\\s+.*\\\\b(INSERT|UPDATE|DELETE)\\\\b' THEN 'CTE-WRITE'
            WHEN query ~* '^\\\\s*(WITH\\\\s+.+\\\\s+)?SELECT\\\\s' THEN 'SELECT'
            ELSE 'OTHER'
        END as query_type,
        -- WAL metrics (PG 13+)
        COALESCE(wal_records, 0) as wal_records,
        COALESCE(wal_fpi, 0) as wal_fpi,
        COALESCE(wal_bytes, 0) as wal_bytes,
        -- Planning time (PG 13+)
        COALESCE(plans, 0) as plans,
        COALESCE(total_plan_time, 0)::numeric as total_plan_time,
        COALESCE(mean_plan_time, 0)::numeric as mean_plan_time,
        -- I/O wait time (requires track_io_timing)
        COALESCE(shared_blk_read_time, 0)::numeric as blk_read_time,
        COALESCE(shared_blk_write_time, 0)::numeric as blk_write_time,
        -- Local blocks
        COALESCE(local_blks_hit, 0) as local_blks_hit,
        COALESCE(local_blks_read, 0) as local_blks_read,
        COALESCE(local_blks_written, 0) as local_blks_written,
        -- JIT stats (PG 15+ columns from pg_stat_statements 1.10)
        COALESCE(jit_functions, 0) as jit_functions,
        COALESCE(jit_generation_time, 0)::numeric as jit_generation_time,
        COALESCE(jit_inlining_time, 0)::numeric as jit_inlining_time,
        COALESCE(jit_optimization_time, 0)::numeric as jit_optimization_time,
        COALESCE(jit_emission_time, 0)::numeric as jit_emission_time
    FROM pg_stat_statements
    WHERE calls > 10
        AND query IS NOT NULL
        AND query != '<insufficient privilege>'
),
query_metrics AS (
    SELECT
        queryid,
        query,
        calls,
        execution_time as total_time_ms,
        ROUND((execution_time / NULLIF(calls, 0))::numeric, 2) as avg_time_ms,
        ROUND(mean_time::numeric, 2) as mean_time_ms,
        ROUND(COALESCE(stddev_time, 0)::numeric, 2) as stddev_time_ms,
        ROUND(min_time::numeric, 2) as min_time_ms,
        ROUND(max_time::numeric, 2) as max_time_ms,
        ROUND((query_rows::float / NULLIF(calls, 0))::numeric, 2) as avg_rows,
        ROUND((100.0 * shared_blks_hit / NULLIF(shared_blks_hit + shared_blks_read, 0))::numeric, 2) as cache_hit_ratio,
        (temp_blks_read + temp_blks_written) as temp_blocks,
        (shared_blks_read + shared_blks_written) as total_io_blocks,
        shared_blks_hit,
        shared_blks_read,
        shared_blks_written,
        ROUND((NULLIF(stddev_time, 0) / NULLIF(mean_time, 0))::numeric, 2) as time_variability,
        ROUND((query_rows::numeric / NULLIF(execution_time, 0))::numeric, 2) as rows_per_ms,
        pg_size_pretty((COALESCE(shared_blks_read + shared_blks_written, 0)::bigint * 8192)::bigint) as total_io_size,
        pg_size_pretty((COALESCE(temp_blks_read + temp_blks_written, 0)::bigint * 8192)::bigint) as total_temp_size,
        first_seen,
        last_seen,
        EXTRACT(EPOCH FROM (last_seen - first_seen)) as active_seconds,
        ROUND((calls::numeric / NULLIF(EXTRACT(EPOCH FROM (last_seen - first_seen)), 0))::numeric, 4) as queries_per_second,
        (SELECT string_agg(DISTINCT nspname, ', ')
         FROM pg_namespace
         WHERE nspname != 'pg_catalog' AND nspname != 'information_schema'
           AND nspname !~ '^pg_'
           AND query ~* ('\\\\m' || nspname || '\\\\.')) as detected_schemas,
        (SELECT string_agg(DISTINCT CASE WHEN schemaname IS NOT NULL THEN schemaname || '.' || tablename ELSE tablename END, ', ')
         FROM pg_tables
         WHERE (schemaname || '.' || tablename) = ANY(string_to_array(regexp_replace(regexp_replace(query, '\\$[0-9]+', '', 'g'), '[^a-zA-Z0-9_.]', ' ', 'g'), ' '))
            OR tablename = ANY(string_to_array(regexp_replace(regexp_replace(query, '\\$[0-9]+', '', 'g'), '[^a-zA-Z0-9_.]', ' ', 'g'), ' '))
         LIMIT 10) as referenced_tables,
        -- Query type passthrough
        query_type,
        -- WAL metrics
        wal_records,
        wal_fpi,
        wal_bytes,
        pg_size_pretty(wal_bytes::bigint) as wal_size,
        CASE
            WHEN wal_bytes > 0 THEN ROUND((wal_bytes / NULLIF(calls, 0))::numeric, 2)
            ELSE 0
        END as wal_bytes_per_call,
        -- Planning time
        plans,
        ROUND(total_plan_time::numeric, 2) as total_plan_time_ms,
        ROUND(mean_plan_time::numeric, 2) as mean_plan_time_ms,
        CASE
            WHEN (total_plan_time + execution_time) > 0
            THEN ROUND((100.0 * total_plan_time / (total_plan_time + execution_time))::numeric, 2)
            ELSE 0
        END as plan_time_pct,
        -- I/O wait time
        ROUND(blk_read_time::numeric, 2) as blk_read_time_ms,
        ROUND(blk_write_time::numeric, 2) as blk_write_time_ms,
        CASE
            WHEN execution_time > 0
            THEN ROUND((100.0 * (blk_read_time + blk_write_time) / execution_time)::numeric, 2)
            ELSE 0
        END as io_wait_pct,
        CASE
            WHEN (blk_read_time + blk_write_time) = 0 AND (shared_blks_read + shared_blks_written) > 100
            THEN 'track_io_timing may be OFF'
            ELSE 'OK'
        END as io_timing_status,
        -- Local blocks
        local_blks_hit,
        local_blks_read,
        local_blks_written,
        pg_size_pretty(((local_blks_read + local_blks_written)::bigint * 8192)::bigint) as local_io_size,
        -- Write ratio
        CASE
            WHEN (shared_blks_hit + shared_blks_read + shared_blks_written) > 0
            THEN ROUND((100.0 * shared_blks_written / (shared_blks_hit + shared_blks_read + shared_blks_written))::numeric, 2)
            ELSE 0
        END as write_ratio_pct,
        CASE
            WHEN shared_blks_written = 0 THEN 'READ-ONLY'
            WHEN shared_blks_written > (shared_blks_hit + shared_blks_read) THEN 'WRITE-HEAVY'
            WHEN shared_blks_written > 0 THEN 'MIXED'
            ELSE 'READ-ONLY'
        END as io_pattern,
        -- JIT stats
        ROUND((jit_generation_time + jit_inlining_time + jit_optimization_time + jit_emission_time)::numeric, 2) as total_jit_time_ms,
        CASE
            WHEN jit_functions > 0 AND (jit_generation_time + jit_inlining_time + jit_optimization_time + jit_emission_time) > 0
            THEN CASE
                WHEN (jit_generation_time + jit_inlining_time + jit_optimization_time + jit_emission_time) > execution_time * 0.5
                THEN 'POOR: JIT overhead > 50%% of exec time'
                WHEN (jit_generation_time + jit_inlining_time + jit_optimization_time + jit_emission_time) > execution_time * 0.2
                THEN 'FAIR: JIT overhead 20-50%% of exec time'
                ELSE 'GOOD: JIT overhead < 20%% of exec time'
            END
            ELSE 'N/A: No JIT activity'
        END as jit_efficiency
    FROM frequent_queries
),
query_analysis AS (
    SELECT
        queryid,
        CASE 
            WHEN query IS NULL OR query = '' THEN '<no query text>'
            WHEN query = '<insufficient privilege>' THEN '<insufficient privilege>'
            ELSE query
        END as query_excerpt,
        COALESCE(detected_schemas, 'public or unspecified') as schemas,
        COALESCE(referenced_tables, 'N/A') as tables,
        calls as execution_count,
        first_seen,
        last_seen,
        ROUND(active_seconds::numeric / 86400, 1) as days_active,
        queries_per_second,
        total_time_ms,
        avg_time_ms,
        mean_time_ms,
        stddev_time_ms,
        min_time_ms,
        max_time_ms,
        time_variability,
        avg_rows,
        rows_per_ms,
        COALESCE(cache_hit_ratio, 0) as cache_hit_ratio,
        COALESCE(temp_blocks, 0) as total_temp_blocks,
        COALESCE(total_io_blocks, 0) as total_io_blocks,
        total_io_size,
        total_temp_size,
        shared_blks_hit,
        shared_blks_read,
        shared_blks_written,
        -- New analysis columns
        query_type,
        -- WAL impact
        wal_records,
        wal_fpi,
        wal_size,
        wal_bytes_per_call,
        CASE
            WHEN wal_bytes > 1073741824 THEN 'CRITICAL: > 1GB WAL generated'
            WHEN wal_bytes > 104857600 THEN 'HIGH: > 100MB WAL generated'
            WHEN wal_bytes > 10485760 THEN 'MODERATE: > 10MB WAL generated'
            WHEN wal_bytes > 0 THEN 'LOW'
            ELSE 'NONE'
        END as wal_impact,
        -- Planning time
        plans,
        total_plan_time_ms,
        mean_plan_time_ms,
        plan_time_pct,
        -- I/O wait
        blk_read_time_ms,
        blk_write_time_ms,
        io_wait_pct,
        io_timing_status,
        CASE
            WHEN io_wait_pct > 50 THEN 'IO-BOUND'
            WHEN io_wait_pct > 20 THEN 'IO-MIXED'
            WHEN io_wait_pct > 0 THEN 'CPU-BOUND'
            ELSE 'UNKNOWN'
        END as bottleneck_type,
        -- Local blocks
        local_blks_hit,
        local_blks_read,
        local_blks_written,
        local_io_size,
        -- Write ratio
        write_ratio_pct,
        io_pattern,
        -- JIT stats
        total_jit_time_ms,
        jit_efficiency,
        -- Lock risk assessment
        CASE
            WHEN query_type IN ('UPDATE', 'DELETE') AND avg_time_ms > 500 AND calls > 100
            THEN 'HIGH: Long-running writes with high frequency'
            WHEN query_type IN ('UPDATE', 'DELETE') AND avg_time_ms > 1000
            THEN 'HIGH: Very slow write operations'
            WHEN query_type IN ('UPDATE', 'DELETE', 'CTE-WRITE') AND time_variability > 2
            THEN 'MEDIUM: Write ops with high variance (possible lock waits)'
            WHEN query_type IN ('INSERT', 'UPDATE', 'DELETE', 'CTE-WRITE')
            THEN 'LOW: Normal write operations'
            ELSE 'MINIMAL: Read-only query'
        END as lock_risk,
        -- Cost impact score (0-100, higher = more impactful)
        ROUND(LEAST(100, (
            -- Execution time component (0-30)
            CASE
                WHEN avg_time_ms > 5000 THEN 30
                WHEN avg_time_ms > 1000 THEN 25
                WHEN avg_time_ms > 500 THEN 20
                WHEN avg_time_ms > 100 THEN 10
                ELSE 5
            END +
            -- Frequency component (0-25)
            CASE
                WHEN calls >= 100000 THEN 25
                WHEN calls >= 10000 THEN 20
                WHEN calls >= 1000 THEN 10
                ELSE 5
            END +
            -- I/O component (0-20)
            CASE
                WHEN COALESCE(cache_hit_ratio, 0) < 50 THEN 20
                WHEN COALESCE(cache_hit_ratio, 0) < 80 THEN 15
                WHEN COALESCE(cache_hit_ratio, 0) < 95 THEN 10
                ELSE 0
            END +
            -- WAL component (0-15)
            CASE
                WHEN wal_bytes > 1073741824 THEN 15
                WHEN wal_bytes > 104857600 THEN 10
                WHEN wal_bytes > 10485760 THEN 5
                ELSE 0
            END +
            -- Temp usage component (0-10)
            CASE
                WHEN COALESCE(temp_blocks, 0) > 10000 THEN 10
                WHEN COALESCE(temp_blocks, 0) > 1000 THEN 5
                ELSE 0
            END
        ))::numeric, 0) as cost_impact_score,
        CASE
            WHEN avg_time_ms > 1000 THEN 'VERY SLOW'
            WHEN avg_time_ms > 500 THEN 'SLOW'
            WHEN avg_time_ms > 100 THEN 'MEDIUM'
            ELSE 'FAST'
        END as speed_classification,
        CASE
            WHEN calls >= 100000 THEN 'VERY HIGH'
            WHEN calls >= 10000 THEN 'HIGH'
            WHEN calls >= 1000 THEN 'MEDIUM'
            ELSE 'LOW'
        END as frequency_category,
        CASE
            WHEN time_variability > 2 THEN 'INCONSISTENT: High variance'
            WHEN time_variability > 1 THEN 'VARIABLE: Moderate variance'
            ELSE 'CONSISTENT: Stable'
        END as performance_consistency,
        CASE
            WHEN COALESCE(cache_hit_ratio, 0) >= 99 THEN 'EXCELLENT'
            WHEN COALESCE(cache_hit_ratio, 0) >= 95 THEN 'GOOD'
            WHEN COALESCE(cache_hit_ratio, 0) >= 90 THEN 'FAIR'
            WHEN COALESCE(cache_hit_ratio, 0) >= 80 THEN 'POOR'
            ELSE 'CRITICAL'
        END as io_efficiency,
        CASE
            WHEN temp_blocks = 0 THEN 'NONE'
            WHEN temp_blocks < 100 THEN 'LOW'
            WHEN temp_blocks < 1000 THEN 'MODERATE'
            WHEN temp_blocks < 10000 THEN 'HIGH'
            ELSE 'CRITICAL'
        END as temp_usage,
        CASE
            WHEN avg_time_ms > 1000 AND calls > 1000 THEN 'CRITICAL: Frequently used slow query'
            WHEN avg_time_ms > 1000 THEN 'CRITICAL: High average execution time'
            WHEN time_variability > 2 THEN 'WARNING: High execution time variability'
            WHEN COALESCE(cache_hit_ratio, 0) < 80 AND shared_blks_read > 1000 THEN 'WARNING: Poor cache performance'
            WHEN temp_blocks > 10000 THEN 'WARNING: High temporary space usage'
            ELSE 'OK'
        END as health_status,
        CASE
            WHEN avg_time_ms > 1000 AND calls > 1000 THEN
                CASE
                    WHEN COALESCE(cache_hit_ratio, 0) < 80 THEN 'CRITICAL: Add indexes and increase shared_buffers. Cache hit: ' || COALESCE(cache_hit_ratio, 0)::text || '%'
                    WHEN temp_blocks > 10000 THEN 'CRITICAL: Increase work_mem to avoid disk writes. Temp usage: ' || total_temp_size
                    ELSE 'CRITICAL: Review query plan and optimize logic'
                END
            WHEN time_variability > 2 THEN
                'WARNING: High variance (' || time_variability::text || 'x) - check for parameter sniffing or lock contention'
            WHEN COALESCE(cache_hit_ratio, 0) < 80 AND shared_blks_read > 1000 THEN
                'WARNING: Poor cache hit ratio (' || COALESCE(cache_hit_ratio, 0)::text || '%) - review indexing strategy'
            WHEN temp_blocks > 10000 THEN
                'WARNING: High temp usage (' || total_temp_size || ') - review sort/join operations'
            WHEN avg_time_ms > 500 THEN
                'REVIEW: Consider optimization if frequently used'
            ELSE 'OK: No immediate action needed'
        END as recommendation,
        ROUND(((
            CASE
                WHEN avg_time_ms > 1000 THEN 0
                WHEN avg_time_ms > 500 THEN 25
                WHEN avg_time_ms > 100 THEN 50
                ELSE 100
            END +
            COALESCE(CASE
                WHEN cache_hit_ratio < 50 THEN 0
                WHEN cache_hit_ratio < 80 THEN 25
                WHEN cache_hit_ratio < 95 THEN 50
                ELSE 100
            END, 100) +
            CASE
                WHEN time_variability > 2 THEN 0
                WHEN time_variability > 1 THEN 50
                ELSE 100
            END
        ) / 3)::numeric, 2) as performance_score,
        CASE
            WHEN calls >= 100000 AND avg_time_ms < 10 THEN
                'This query runs very frequently and is fast. It''s well-optimized.'
            WHEN calls >= 100000 AND avg_time_ms >= 10 THEN
                'This query runs very frequently. Even small improvements will have big impact.'
            WHEN avg_time_ms >= 1000 THEN
                'This query is slow. Users may experience delays when this runs.'
            WHEN COALESCE(cache_hit_ratio, 0) < 90 THEN
                'This query reads a lot from disk instead of memory. Adding indexes could help.'
            WHEN temp_blocks > 1000 THEN
                'This query uses temporary disk space, which slows it down. It needs more memory or optimization.'
            WHEN time_variability > 1 THEN
                'This query has unpredictable performance. Sometimes fast, sometimes slow.'
            ELSE
                'This query is performing reasonably well.'
        END as performance_insight,
        format('Executed %s times | Avg: %sms (min: %sms, max: %sms) | Returns ~%s rows | Cache: %s%% | Temp: %s | QPS: %s',
               calls,
               ROUND(avg_time_ms::numeric, 1),
               ROUND(min_time_ms::numeric, 1),
               ROUND(max_time_ms::numeric, 1),
               ROUND(avg_rows::numeric, 0),
               ROUND(COALESCE(cache_hit_ratio, 0)::numeric, 1),
               total_temp_size,
               ROUND(queries_per_second::numeric, 2)) as performance_summary
    FROM query_metrics
)
SELECT 
    schemas,
    tables,
    query_excerpt,
    execution_count,
    first_seen,
    last_seen,
    days_active,
    queries_per_second,
    performance_score,
    health_status,
    speed_classification,
    frequency_category,
    performance_summary,
    performance_insight,
    performance_consistency,
    io_efficiency,
    temp_usage,
    recommendation,
    ROUND(total_time_ms::numeric, 2) as total_time_ms,
    ROUND(avg_time_ms::numeric, 2) as avg_time_ms,
    ROUND(min_time_ms::numeric, 2) as min_time_ms,
    ROUND(max_time_ms::numeric, 2) as max_time_ms,
    ROUND(stddev_time_ms::numeric, 2) as stddev_time_ms,
    time_variability,
    ROUND(avg_rows::numeric, 2) as avg_rows,
    rows_per_ms,
    shared_blks_read as disk_reads,
    shared_blks_hit as cache_hits,
    ROUND(cache_hit_ratio::numeric, 2) as cache_hit_ratio,
    total_io_blocks,
    total_io_size,
    total_temp_blocks,
    total_temp_size,
    query_type,
    wal_records,
    wal_fpi,
    wal_size,
    wal_bytes_per_call,
    wal_impact,
    plans,
    total_plan_time_ms,
    mean_plan_time_ms,
    plan_time_pct,
    blk_read_time_ms,
    blk_write_time_ms,
    io_wait_pct,
    io_timing_status,
    bottleneck_type,
    local_blks_hit,
    local_blks_read,
    local_blks_written,
    local_io_size,
    write_ratio_pct,
    io_pattern,
    total_jit_time_ms,
    jit_efficiency,
    lock_risk,
    cost_impact_score
FROM query_analysis
ORDER BY 
    CASE 
        WHEN health_status LIKE 'CRITICAL%' THEN 1
        WHEN health_status LIKE 'WARNING%' THEN 2
        ELSE 3
    END,
    performance_score ASC,
    execution_count DESC
LIMIT 20;"
elif version_gte "$PG_VERSION" 150000; then
    # PostgreSQL 15-16 uses blk_read_time/blk_write_time (original column names)
    QUERY_PERF_PRIMARY_SQL="
WITH frequent_queries AS (
    SELECT 
        queryid,
        COALESCE(query, '<query text not available>') as query,
        calls,
        total_exec_time::numeric as execution_time,
        mean_exec_time::numeric as mean_time,
        stddev_exec_time::numeric as stddev_time,
        min_exec_time::numeric as min_time,
        max_exec_time::numeric as max_time,
        rows as query_rows,
        shared_blks_hit,
        shared_blks_read,
        shared_blks_written,
        temp_blks_read,
        temp_blks_written,
        -- Use stats reset time from pg_stat_statements_info (PostgreSQL 14+)
        (SELECT stats_reset FROM pg_stat_statements_info) as first_seen,
        CURRENT_TIMESTAMP as last_seen,
        -- Query type classification via regex
        CASE
            WHEN query ~* '^\\\\s*(WITH\\\\s+.+\\\\s+)?INSERT\\\\s' THEN 'INSERT'
            WHEN query ~* '^\\\\s*(WITH\\\\s+.+\\\\s+)?UPDATE\\\\s' THEN 'UPDATE'
            WHEN query ~* '^\\\\s*(WITH\\\\s+.+\\\\s+)?DELETE\\\\s' THEN 'DELETE'
            WHEN query ~* '^\\\\s*WITH\\\\s+.*\\\\b(INSERT|UPDATE|DELETE)\\\\b' THEN 'CTE-WRITE'
            WHEN query ~* '^\\\\s*(WITH\\\\s+.+\\\\s+)?SELECT\\\\s' THEN 'SELECT'
            ELSE 'OTHER'
        END as query_type,
        -- WAL metrics (PG 13+)
        COALESCE(wal_records, 0) as wal_records,
        COALESCE(wal_fpi, 0) as wal_fpi,
        COALESCE(wal_bytes, 0) as wal_bytes,
        -- Planning time (PG 13+)
        COALESCE(plans, 0) as plans,
        COALESCE(total_plan_time, 0)::numeric as total_plan_time,
        COALESCE(mean_plan_time, 0)::numeric as mean_plan_time,
        -- I/O wait time (requires track_io_timing)
        COALESCE(blk_read_time, 0)::numeric as blk_read_time,
        COALESCE(blk_write_time, 0)::numeric as blk_write_time,
        -- Local blocks
        COALESCE(local_blks_hit, 0) as local_blks_hit,
        COALESCE(local_blks_read, 0) as local_blks_read,
        COALESCE(local_blks_written, 0) as local_blks_written,
        -- JIT stats (PG 15+ columns from pg_stat_statements 1.10)
        COALESCE(jit_functions, 0) as jit_functions,
        COALESCE(jit_generation_time, 0)::numeric as jit_generation_time,
        COALESCE(jit_inlining_time, 0)::numeric as jit_inlining_time,
        COALESCE(jit_optimization_time, 0)::numeric as jit_optimization_time,
        COALESCE(jit_emission_time, 0)::numeric as jit_emission_time
    FROM pg_stat_statements
    WHERE calls > 10
        AND query IS NOT NULL
        AND query != '<insufficient privilege>'
),
query_metrics AS (
    SELECT
        queryid,
        query,
        calls,
        execution_time as total_time_ms,
        ROUND((execution_time / NULLIF(calls, 0))::numeric, 2) as avg_time_ms,
        ROUND(mean_time::numeric, 2) as mean_time_ms,
        ROUND(COALESCE(stddev_time, 0)::numeric, 2) as stddev_time_ms,
        ROUND(min_time::numeric, 2) as min_time_ms,
        ROUND(max_time::numeric, 2) as max_time_ms,
        ROUND((query_rows::float / NULLIF(calls, 0))::numeric, 2) as avg_rows,
        ROUND((100.0 * shared_blks_hit / NULLIF(shared_blks_hit + shared_blks_read, 0))::numeric, 2) as cache_hit_ratio,
        (temp_blks_read + temp_blks_written) as temp_blocks,
        (shared_blks_read + shared_blks_written) as total_io_blocks,
        shared_blks_hit,
        shared_blks_read,
        shared_blks_written,
        ROUND((NULLIF(stddev_time, 0) / NULLIF(mean_time, 0))::numeric, 2) as time_variability,
        ROUND((query_rows::numeric / NULLIF(execution_time, 0))::numeric, 2) as rows_per_ms,
        pg_size_pretty((COALESCE(shared_blks_read + shared_blks_written, 0)::bigint * 8192)::bigint) as total_io_size,
        pg_size_pretty((COALESCE(temp_blks_read + temp_blks_written, 0)::bigint * 8192)::bigint) as total_temp_size,
        first_seen,
        last_seen,
        EXTRACT(EPOCH FROM (last_seen - first_seen)) as active_seconds,
        ROUND((calls::numeric / NULLIF(EXTRACT(EPOCH FROM (last_seen - first_seen)), 0))::numeric, 4) as queries_per_second,
        (SELECT string_agg(DISTINCT nspname, ', ')
         FROM pg_namespace
         WHERE nspname != 'pg_catalog' AND nspname != 'information_schema'
           AND nspname !~ '^pg_'
           AND query ~* ('\\\\m' || nspname || '\\\\.')) as detected_schemas,
        (SELECT string_agg(DISTINCT CASE WHEN schemaname IS NOT NULL THEN schemaname || '.' || tablename ELSE tablename END, ', ')
         FROM pg_tables
         WHERE (schemaname || '.' || tablename) = ANY(string_to_array(regexp_replace(regexp_replace(query, '\\$[0-9]+', '', 'g'), '[^a-zA-Z0-9_.]', ' ', 'g'), ' '))
            OR tablename = ANY(string_to_array(regexp_replace(regexp_replace(query, '\\$[0-9]+', '', 'g'), '[^a-zA-Z0-9_.]', ' ', 'g'), ' '))
         LIMIT 10) as referenced_tables,
        -- Query type passthrough
        query_type,
        -- WAL metrics
        wal_records,
        wal_fpi,
        wal_bytes,
        pg_size_pretty(wal_bytes::bigint) as wal_size,
        CASE
            WHEN wal_bytes > 0 THEN ROUND((wal_bytes / NULLIF(calls, 0))::numeric, 2)
            ELSE 0
        END as wal_bytes_per_call,
        -- Planning time
        plans,
        ROUND(total_plan_time::numeric, 2) as total_plan_time_ms,
        ROUND(mean_plan_time::numeric, 2) as mean_plan_time_ms,
        CASE
            WHEN (total_plan_time + execution_time) > 0
            THEN ROUND((100.0 * total_plan_time / (total_plan_time + execution_time))::numeric, 2)
            ELSE 0
        END as plan_time_pct,
        -- I/O wait time
        ROUND(blk_read_time::numeric, 2) as blk_read_time_ms,
        ROUND(blk_write_time::numeric, 2) as blk_write_time_ms,
        CASE
            WHEN execution_time > 0
            THEN ROUND((100.0 * (blk_read_time + blk_write_time) / execution_time)::numeric, 2)
            ELSE 0
        END as io_wait_pct,
        CASE
            WHEN (blk_read_time + blk_write_time) = 0 AND (shared_blks_read + shared_blks_written) > 100
            THEN 'track_io_timing may be OFF'
            ELSE 'OK'
        END as io_timing_status,
        -- Local blocks
        local_blks_hit,
        local_blks_read,
        local_blks_written,
        pg_size_pretty(((local_blks_read + local_blks_written)::bigint * 8192)::bigint) as local_io_size,
        -- Write ratio
        CASE
            WHEN (shared_blks_hit + shared_blks_read + shared_blks_written) > 0
            THEN ROUND((100.0 * shared_blks_written / (shared_blks_hit + shared_blks_read + shared_blks_written))::numeric, 2)
            ELSE 0
        END as write_ratio_pct,
        CASE
            WHEN shared_blks_written = 0 THEN 'READ-ONLY'
            WHEN shared_blks_written > (shared_blks_hit + shared_blks_read) THEN 'WRITE-HEAVY'
            WHEN shared_blks_written > 0 THEN 'MIXED'
            ELSE 'READ-ONLY'
        END as io_pattern,
        -- JIT stats
        ROUND((jit_generation_time + jit_inlining_time + jit_optimization_time + jit_emission_time)::numeric, 2) as total_jit_time_ms,
        CASE
            WHEN jit_functions > 0 AND (jit_generation_time + jit_inlining_time + jit_optimization_time + jit_emission_time) > 0
            THEN CASE
                WHEN (jit_generation_time + jit_inlining_time + jit_optimization_time + jit_emission_time) > execution_time * 0.5
                THEN 'POOR: JIT overhead > 50%% of exec time'
                WHEN (jit_generation_time + jit_inlining_time + jit_optimization_time + jit_emission_time) > execution_time * 0.2
                THEN 'FAIR: JIT overhead 20-50%% of exec time'
                ELSE 'GOOD: JIT overhead < 20%% of exec time'
            END
            ELSE 'N/A: No JIT activity'
        END as jit_efficiency
    FROM frequent_queries
),
query_analysis AS (
    SELECT
        queryid,
        CASE 
            WHEN query IS NULL OR query = '' THEN '<no query text>'
            WHEN query = '<insufficient privilege>' THEN '<insufficient privilege>'
            ELSE query
        END as query_excerpt,
        COALESCE(detected_schemas, 'public or unspecified') as schemas,
        COALESCE(referenced_tables, 'N/A') as tables,
        calls as execution_count,
        first_seen,
        last_seen,
        ROUND(active_seconds::numeric / 86400, 1) as days_active,
        queries_per_second,
        total_time_ms,
        avg_time_ms,
        mean_time_ms,
        stddev_time_ms,
        min_time_ms,
        max_time_ms,
        time_variability,
        avg_rows,
        rows_per_ms,
        COALESCE(cache_hit_ratio, 0) as cache_hit_ratio,
        COALESCE(temp_blocks, 0) as total_temp_blocks,
        COALESCE(total_io_blocks, 0) as total_io_blocks,
        total_io_size,
        total_temp_size,
        shared_blks_hit,
        shared_blks_read,
        shared_blks_written,
        -- New analysis columns
        query_type,
        -- WAL impact
        wal_records,
        wal_fpi,
        wal_size,
        wal_bytes_per_call,
        CASE
            WHEN wal_bytes > 1073741824 THEN 'CRITICAL: > 1GB WAL generated'
            WHEN wal_bytes > 104857600 THEN 'HIGH: > 100MB WAL generated'
            WHEN wal_bytes > 10485760 THEN 'MODERATE: > 10MB WAL generated'
            WHEN wal_bytes > 0 THEN 'LOW'
            ELSE 'NONE'
        END as wal_impact,
        -- Planning time
        plans,
        total_plan_time_ms,
        mean_plan_time_ms,
        plan_time_pct,
        -- I/O wait
        blk_read_time_ms,
        blk_write_time_ms,
        io_wait_pct,
        io_timing_status,
        CASE
            WHEN io_wait_pct > 50 THEN 'IO-BOUND'
            WHEN io_wait_pct > 20 THEN 'IO-MIXED'
            WHEN io_wait_pct > 0 THEN 'CPU-BOUND'
            ELSE 'UNKNOWN'
        END as bottleneck_type,
        -- Local blocks
        local_blks_hit,
        local_blks_read,
        local_blks_written,
        local_io_size,
        -- Write ratio
        write_ratio_pct,
        io_pattern,
        -- JIT stats
        total_jit_time_ms,
        jit_efficiency,
        -- Lock risk assessment
        CASE
            WHEN query_type IN ('UPDATE', 'DELETE') AND avg_time_ms > 500 AND calls > 100
            THEN 'HIGH: Long-running writes with high frequency'
            WHEN query_type IN ('UPDATE', 'DELETE') AND avg_time_ms > 1000
            THEN 'HIGH: Very slow write operations'
            WHEN query_type IN ('UPDATE', 'DELETE', 'CTE-WRITE') AND time_variability > 2
            THEN 'MEDIUM: Write ops with high variance (possible lock waits)'
            WHEN query_type IN ('INSERT', 'UPDATE', 'DELETE', 'CTE-WRITE')
            THEN 'LOW: Normal write operations'
            ELSE 'MINIMAL: Read-only query'
        END as lock_risk,
        -- Cost impact score (0-100, higher = more impactful)
        ROUND(LEAST(100, (
            -- Execution time component (0-30)
            CASE
                WHEN avg_time_ms > 5000 THEN 30
                WHEN avg_time_ms > 1000 THEN 25
                WHEN avg_time_ms > 500 THEN 20
                WHEN avg_time_ms > 100 THEN 10
                ELSE 5
            END +
            -- Frequency component (0-25)
            CASE
                WHEN calls >= 100000 THEN 25
                WHEN calls >= 10000 THEN 20
                WHEN calls >= 1000 THEN 10
                ELSE 5
            END +
            -- I/O component (0-20)
            CASE
                WHEN COALESCE(cache_hit_ratio, 0) < 50 THEN 20
                WHEN COALESCE(cache_hit_ratio, 0) < 80 THEN 15
                WHEN COALESCE(cache_hit_ratio, 0) < 95 THEN 10
                ELSE 0
            END +
            -- WAL component (0-15)
            CASE
                WHEN wal_bytes > 1073741824 THEN 15
                WHEN wal_bytes > 104857600 THEN 10
                WHEN wal_bytes > 10485760 THEN 5
                ELSE 0
            END +
            -- Temp usage component (0-10)
            CASE
                WHEN COALESCE(temp_blocks, 0) > 10000 THEN 10
                WHEN COALESCE(temp_blocks, 0) > 1000 THEN 5
                ELSE 0
            END
        ))::numeric, 0) as cost_impact_score,
        CASE
            WHEN avg_time_ms > 1000 THEN 'VERY SLOW'
            WHEN avg_time_ms > 500 THEN 'SLOW'
            WHEN avg_time_ms > 100 THEN 'MEDIUM'
            ELSE 'FAST'
        END as speed_classification,
        CASE
            WHEN calls >= 100000 THEN 'VERY HIGH'
            WHEN calls >= 10000 THEN 'HIGH'
            WHEN calls >= 1000 THEN 'MEDIUM'
            ELSE 'LOW'
        END as frequency_category,
        CASE
            WHEN time_variability > 2 THEN 'INCONSISTENT: High variance'
            WHEN time_variability > 1 THEN 'VARIABLE: Moderate variance'
            ELSE 'CONSISTENT: Stable'
        END as performance_consistency,
        CASE
            WHEN COALESCE(cache_hit_ratio, 0) >= 99 THEN 'EXCELLENT'
            WHEN COALESCE(cache_hit_ratio, 0) >= 95 THEN 'GOOD'
            WHEN COALESCE(cache_hit_ratio, 0) >= 90 THEN 'FAIR'
            WHEN COALESCE(cache_hit_ratio, 0) >= 80 THEN 'POOR'
            ELSE 'CRITICAL'
        END as io_efficiency,
        CASE
            WHEN temp_blocks = 0 THEN 'NONE'
            WHEN temp_blocks < 100 THEN 'LOW'
            WHEN temp_blocks < 1000 THEN 'MODERATE'
            WHEN temp_blocks < 10000 THEN 'HIGH'
            ELSE 'CRITICAL'
        END as temp_usage,
        CASE
            WHEN avg_time_ms > 1000 AND calls > 1000 THEN 'CRITICAL: Frequently used slow query'
            WHEN avg_time_ms > 1000 THEN 'CRITICAL: High average execution time'
            WHEN time_variability > 2 THEN 'WARNING: High execution time variability'
            WHEN COALESCE(cache_hit_ratio, 0) < 80 AND shared_blks_read > 1000 THEN 'WARNING: Poor cache performance'
            WHEN temp_blocks > 10000 THEN 'WARNING: High temporary space usage'
            ELSE 'OK'
        END as health_status,
        CASE
            WHEN avg_time_ms > 1000 AND calls > 1000 THEN
                CASE
                    WHEN COALESCE(cache_hit_ratio, 0) < 80 THEN 'CRITICAL: Add indexes and increase shared_buffers. Cache hit: ' || COALESCE(cache_hit_ratio, 0)::text || '%'
                    WHEN temp_blocks > 10000 THEN 'CRITICAL: Increase work_mem to avoid disk writes. Temp usage: ' || total_temp_size
                    ELSE 'CRITICAL: Review query plan and optimize logic'
                END
            WHEN time_variability > 2 THEN
                'WARNING: High variance (' || time_variability::text || 'x) - check for parameter sniffing or lock contention'
            WHEN COALESCE(cache_hit_ratio, 0) < 80 AND shared_blks_read > 1000 THEN
                'WARNING: Poor cache hit ratio (' || COALESCE(cache_hit_ratio, 0)::text || '%) - review indexing strategy'
            WHEN temp_blocks > 10000 THEN
                'WARNING: High temp usage (' || total_temp_size || ') - review sort/join operations'
            WHEN avg_time_ms > 500 THEN
                'REVIEW: Consider optimization if frequently used'
            ELSE 'OK: No immediate action needed'
        END as recommendation,
        ROUND(((
            CASE
                WHEN avg_time_ms > 1000 THEN 0
                WHEN avg_time_ms > 500 THEN 25
                WHEN avg_time_ms > 100 THEN 50
                ELSE 100
            END +
            COALESCE(CASE
                WHEN cache_hit_ratio < 50 THEN 0
                WHEN cache_hit_ratio < 80 THEN 25
                WHEN cache_hit_ratio < 95 THEN 50
                ELSE 100
            END, 100) +
            CASE
                WHEN time_variability > 2 THEN 0
                WHEN time_variability > 1 THEN 50
                ELSE 100
            END
        ) / 3)::numeric, 2) as performance_score,
        CASE
            WHEN calls >= 100000 AND avg_time_ms < 10 THEN
                'This query runs very frequently and is fast. It''s well-optimized.'
            WHEN calls >= 100000 AND avg_time_ms >= 10 THEN
                'This query runs very frequently. Even small improvements will have big impact.'
            WHEN avg_time_ms >= 1000 THEN
                'This query is slow. Users may experience delays when this runs.'
            WHEN COALESCE(cache_hit_ratio, 0) < 90 THEN
                'This query reads a lot from disk instead of memory. Adding indexes could help.'
            WHEN temp_blocks > 1000 THEN
                'This query uses temporary disk space, which slows it down. It needs more memory or optimization.'
            WHEN time_variability > 1 THEN
                'This query has unpredictable performance. Sometimes fast, sometimes slow.'
            ELSE
                'This query is performing reasonably well.'
        END as performance_insight,
        format('Executed %s times | Avg: %sms (min: %sms, max: %sms) | Returns ~%s rows | Cache: %s%% | Temp: %s | QPS: %s',
               calls,
               ROUND(avg_time_ms::numeric, 1),
               ROUND(min_time_ms::numeric, 1),
               ROUND(max_time_ms::numeric, 1),
               ROUND(avg_rows::numeric, 0),
               ROUND(COALESCE(cache_hit_ratio, 0)::numeric, 1),
               total_temp_size,
               ROUND(queries_per_second::numeric, 2)) as performance_summary
    FROM query_metrics
)
SELECT 
    schemas,
    tables,
    query_excerpt,
    execution_count,
    first_seen,
    last_seen,
    days_active,
    queries_per_second,
    performance_score,
    health_status,
    speed_classification,
    frequency_category,
    performance_summary,
    performance_insight,
    performance_consistency,
    io_efficiency,
    temp_usage,
    recommendation,
    ROUND(total_time_ms::numeric, 2) as total_time_ms,
    ROUND(avg_time_ms::numeric, 2) as avg_time_ms,
    ROUND(min_time_ms::numeric, 2) as min_time_ms,
    ROUND(max_time_ms::numeric, 2) as max_time_ms,
    ROUND(stddev_time_ms::numeric, 2) as stddev_time_ms,
    time_variability,
    ROUND(avg_rows::numeric, 2) as avg_rows,
    rows_per_ms,
    shared_blks_read as disk_reads,
    shared_blks_hit as cache_hits,
    ROUND(cache_hit_ratio::numeric, 2) as cache_hit_ratio,
    total_io_blocks,
    total_io_size,
    total_temp_blocks,
    total_temp_size,
    query_type,
    wal_records,
    wal_fpi,
    wal_size,
    wal_bytes_per_call,
    wal_impact,
    plans,
    total_plan_time_ms,
    mean_plan_time_ms,
    plan_time_pct,
    blk_read_time_ms,
    blk_write_time_ms,
    io_wait_pct,
    io_timing_status,
    bottleneck_type,
    local_blks_hit,
    local_blks_read,
    local_blks_written,
    local_io_size,
    write_ratio_pct,
    io_pattern,
    total_jit_time_ms,
    jit_efficiency,
    lock_risk,
    cost_impact_score
FROM query_analysis
ORDER BY 
    CASE 
        WHEN health_status LIKE 'CRITICAL%' THEN 1
        WHEN health_status LIKE 'WARNING%' THEN 2
        ELSE 3
    END,
    performance_score ASC,
    execution_count DESC
LIMIT 20;"
elif version_gte "$PG_VERSION" 140000; then
    # PostgreSQL 14 uses total_exec_time and pg_stat_statements_info but NO JIT columns
    # JIT columns (jit_functions, jit_generation_time, etc.) were added in pg_stat_statements 1.10 (PG 15)
    QUERY_PERF_PRIMARY_SQL="${QUERY_PERF_PRIMARY_SQL_PG14}"
elif version_gte "$PG_VERSION" 130000; then
    # PostgreSQL 13 uses total_exec_time but doesn't have pg_stat_statements_info
    QUERY_PERF_PRIMARY_SQL="
WITH frequent_queries AS (
    SELECT 
        queryid,
        COALESCE(query, '<query text not available>') as query,
        calls,
        total_exec_time::numeric as execution_time,
        mean_exec_time::numeric as mean_time,
        stddev_exec_time::numeric as stddev_time,
        min_exec_time::numeric as min_time,
        max_exec_time::numeric as max_time,
        rows as query_rows,
        shared_blks_hit,
        shared_blks_read,
        shared_blks_written,
        temp_blks_read,
        temp_blks_written,
        -- Use current timestamp as first_seen (pg_stat_statements_info not available in PG 13)
        CURRENT_TIMESTAMP - interval '1 day' as first_seen,
        CURRENT_TIMESTAMP as last_seen,
        -- Query type classification via regex
        CASE
            WHEN query ~* '^\\s*(WITH\\s+.+\\s+)?INSERT\\s' THEN 'INSERT'
            WHEN query ~* '^\\s*(WITH\\s+.+\\s+)?UPDATE\\s' THEN 'UPDATE'
            WHEN query ~* '^\\s*(WITH\\s+.+\\s+)?DELETE\\s' THEN 'DELETE'
            WHEN query ~* '^\\s*WITH\\s+.*\\b(INSERT|UPDATE|DELETE)\\b' THEN 'CTE-WRITE'
            WHEN query ~* '^\\s*(WITH\\s+.+\\s+)?SELECT\\s' THEN 'SELECT'
            ELSE 'OTHER'
        END as query_type,
        -- WAL metrics (PG 13+)
        COALESCE(wal_records, 0) as wal_records,
        COALESCE(wal_fpi, 0) as wal_fpi,
        COALESCE(wal_bytes, 0) as wal_bytes,
        -- Planning time (PG 13+)
        COALESCE(plans, 0) as plans,
        COALESCE(total_plan_time, 0)::numeric as total_plan_time,
        COALESCE(mean_plan_time, 0)::numeric as mean_plan_time,
        -- I/O wait time (requires track_io_timing)
        COALESCE(blk_read_time, 0)::numeric as blk_read_time,
        COALESCE(blk_write_time, 0)::numeric as blk_write_time,
        -- Local blocks
        COALESCE(local_blks_hit, 0) as local_blks_hit,
        COALESCE(local_blks_read, 0) as local_blks_read,
        COALESCE(local_blks_written, 0) as local_blks_written,
        -- JIT stats (not available in PG 13, use zero placeholders)
        0 as jit_functions,
        0::numeric as jit_generation_time,
        0::numeric as jit_inlining_time,
        0::numeric as jit_optimization_time,
        0::numeric as jit_emission_time
    FROM pg_stat_statements
    WHERE calls > 10
        AND query IS NOT NULL
        AND query != '<insufficient privilege>'
),
query_metrics AS (
    SELECT
        queryid,
        query,
        calls,
        execution_time as total_time_ms,
        ROUND((execution_time / NULLIF(calls, 0))::numeric, 2) as avg_time_ms,
        ROUND(mean_time::numeric, 2) as mean_time_ms,
        ROUND(COALESCE(stddev_time, 0)::numeric, 2) as stddev_time_ms,
        ROUND(min_time::numeric, 2) as min_time_ms,
        ROUND(max_time::numeric, 2) as max_time_ms,
        ROUND((query_rows::float / NULLIF(calls, 0))::numeric, 2) as avg_rows,
        ROUND((100.0 * shared_blks_hit / NULLIF(shared_blks_hit + shared_blks_read, 0))::numeric, 2) as cache_hit_ratio,
        (temp_blks_read + temp_blks_written) as temp_blocks,
        (shared_blks_read + shared_blks_written) as total_io_blocks,
        shared_blks_hit,
        shared_blks_read,
        shared_blks_written,
        ROUND((NULLIF(stddev_time, 0) / NULLIF(mean_time, 0))::numeric, 2) as time_variability,
        ROUND((query_rows::numeric / NULLIF(execution_time, 0))::numeric, 2) as rows_per_ms,
        pg_size_pretty((COALESCE(shared_blks_read + shared_blks_written, 0)::bigint * 8192)::bigint) as total_io_size,
        pg_size_pretty((COALESCE(temp_blks_read + temp_blks_written, 0)::bigint * 8192)::bigint) as total_temp_size,
        first_seen,
        last_seen,
        EXTRACT(EPOCH FROM (last_seen - first_seen)) as active_seconds,
        ROUND((calls::numeric / NULLIF(EXTRACT(EPOCH FROM (last_seen - first_seen)), 0))::numeric, 4) as queries_per_second,
        (SELECT string_agg(DISTINCT nspname, ', ')
         FROM pg_namespace
         WHERE nspname != 'pg_catalog' AND nspname != 'information_schema'
           AND nspname !~ '^pg_'
           AND query ~* ('\\m' || nspname || '\\.')) as detected_schemas,
        (SELECT string_agg(DISTINCT CASE WHEN schemaname IS NOT NULL THEN schemaname || '.' || tablename ELSE tablename END, ', ')
         FROM pg_tables
         WHERE (schemaname || '.' || tablename) = ANY(string_to_array(regexp_replace(regexp_replace(query, '\$[0-9]+', '', 'g'), '[^a-zA-Z0-9_.]', ' ', 'g'), ' '))
            OR tablename = ANY(string_to_array(regexp_replace(regexp_replace(query, '\$[0-9]+', '', 'g'), '[^a-zA-Z0-9_.]', ' ', 'g'), ' '))
         LIMIT 10) as referenced_tables,
        -- Query type passthrough
        query_type,
        -- WAL metrics
        wal_records,
        wal_fpi,
        wal_bytes,
        pg_size_pretty(wal_bytes::bigint) as wal_size,
        CASE
            WHEN wal_bytes > 0 THEN ROUND((wal_bytes / NULLIF(calls, 0))::numeric, 2)
            ELSE 0
        END as wal_bytes_per_call,
        -- Planning time
        plans,
        ROUND(total_plan_time::numeric, 2) as total_plan_time_ms,
        ROUND(mean_plan_time::numeric, 2) as mean_plan_time_ms,
        CASE
            WHEN (total_plan_time + execution_time) > 0
            THEN ROUND((100.0 * total_plan_time / (total_plan_time + execution_time))::numeric, 2)
            ELSE 0
        END as plan_time_pct,
        -- I/O wait time
        ROUND(blk_read_time::numeric, 2) as blk_read_time_ms,
        ROUND(blk_write_time::numeric, 2) as blk_write_time_ms,
        CASE
            WHEN execution_time > 0
            THEN ROUND((100.0 * (blk_read_time + blk_write_time) / execution_time)::numeric, 2)
            ELSE 0
        END as io_wait_pct,
        CASE
            WHEN (blk_read_time + blk_write_time) = 0 AND (shared_blks_read + shared_blks_written) > 100
            THEN 'track_io_timing may be OFF'
            ELSE 'OK'
        END as io_timing_status,
        -- Local blocks
        local_blks_hit,
        local_blks_read,
        local_blks_written,
        pg_size_pretty(((local_blks_read + local_blks_written)::bigint * 8192)::bigint) as local_io_size,
        -- Write ratio
        CASE
            WHEN (shared_blks_hit + shared_blks_read + shared_blks_written) > 0
            THEN ROUND((100.0 * shared_blks_written / (shared_blks_hit + shared_blks_read + shared_blks_written))::numeric, 2)
            ELSE 0
        END as write_ratio_pct,
        CASE
            WHEN shared_blks_written = 0 THEN 'READ-ONLY'
            WHEN shared_blks_written > (shared_blks_hit + shared_blks_read) THEN 'WRITE-HEAVY'
            WHEN shared_blks_written > 0 THEN 'MIXED'
            ELSE 'READ-ONLY'
        END as io_pattern,
        -- JIT stats
        ROUND((jit_generation_time + jit_inlining_time + jit_optimization_time + jit_emission_time)::numeric, 2) as total_jit_time_ms,
        CASE
            WHEN jit_functions > 0 AND (jit_generation_time + jit_inlining_time + jit_optimization_time + jit_emission_time) > 0
            THEN CASE
                WHEN (jit_generation_time + jit_inlining_time + jit_optimization_time + jit_emission_time) > execution_time * 0.5
                THEN 'POOR: JIT overhead > 50%% of exec time'
                WHEN (jit_generation_time + jit_inlining_time + jit_optimization_time + jit_emission_time) > execution_time * 0.2
                THEN 'FAIR: JIT overhead 20-50%% of exec time'
                ELSE 'GOOD: JIT overhead < 20%% of exec time'
            END
            ELSE 'N/A: No JIT activity'
        END as jit_efficiency
    FROM frequent_queries
),
query_analysis AS (
    SELECT
        queryid,
        CASE 
            WHEN query IS NULL OR query = '' THEN '<no query text>'
            WHEN query = '<insufficient privilege>' THEN '<insufficient privilege>'
            ELSE query
        END as query_excerpt,
        COALESCE(detected_schemas, 'public or unspecified') as schemas,
        COALESCE(referenced_tables, 'N/A') as tables,
        calls as execution_count,
        first_seen,
        last_seen,
        ROUND(active_seconds::numeric / 86400, 1) as days_active,
        queries_per_second,
        total_time_ms,
        avg_time_ms,
        mean_time_ms,
        stddev_time_ms,
        min_time_ms,
        max_time_ms,
        time_variability,
        avg_rows,
        rows_per_ms,
        COALESCE(cache_hit_ratio, 0) as cache_hit_ratio,
        COALESCE(temp_blocks, 0) as total_temp_blocks,
        COALESCE(total_io_blocks, 0) as total_io_blocks,
        total_io_size,
        total_temp_size,
        shared_blks_hit,
        shared_blks_read,
        shared_blks_written,
        -- New analysis columns
        query_type,
        -- WAL impact
        wal_records,
        wal_fpi,
        wal_size,
        wal_bytes_per_call,
        CASE
            WHEN wal_bytes > 1073741824 THEN 'CRITICAL: > 1GB WAL generated'
            WHEN wal_bytes > 104857600 THEN 'HIGH: > 100MB WAL generated'
            WHEN wal_bytes > 10485760 THEN 'MODERATE: > 10MB WAL generated'
            WHEN wal_bytes > 0 THEN 'LOW'
            ELSE 'NONE'
        END as wal_impact,
        -- Planning time
        plans,
        total_plan_time_ms,
        mean_plan_time_ms,
        plan_time_pct,
        -- I/O wait
        blk_read_time_ms,
        blk_write_time_ms,
        io_wait_pct,
        io_timing_status,
        CASE
            WHEN io_wait_pct > 50 THEN 'IO-BOUND'
            WHEN io_wait_pct > 20 THEN 'IO-MIXED'
            WHEN io_wait_pct > 0 THEN 'CPU-BOUND'
            ELSE 'UNKNOWN'
        END as bottleneck_type,
        -- Local blocks
        local_blks_hit,
        local_blks_read,
        local_blks_written,
        local_io_size,
        -- Write ratio
        write_ratio_pct,
        io_pattern,
        -- JIT stats
        total_jit_time_ms,
        jit_efficiency,
        -- Lock risk assessment
        CASE
            WHEN query_type IN ('UPDATE', 'DELETE') AND avg_time_ms > 500 AND calls > 100
            THEN 'HIGH: Long-running writes with high frequency'
            WHEN query_type IN ('UPDATE', 'DELETE') AND avg_time_ms > 1000
            THEN 'HIGH: Very slow write operations'
            WHEN query_type IN ('UPDATE', 'DELETE', 'CTE-WRITE') AND time_variability > 2
            THEN 'MEDIUM: Write ops with high variance (possible lock waits)'
            WHEN query_type IN ('INSERT', 'UPDATE', 'DELETE', 'CTE-WRITE')
            THEN 'LOW: Normal write operations'
            ELSE 'MINIMAL: Read-only query'
        END as lock_risk,
        -- Cost impact score (0-100, higher = more impactful)
        ROUND(LEAST(100, (
            -- Execution time component (0-30)
            CASE
                WHEN avg_time_ms > 5000 THEN 30
                WHEN avg_time_ms > 1000 THEN 25
                WHEN avg_time_ms > 500 THEN 20
                WHEN avg_time_ms > 100 THEN 10
                ELSE 5
            END +
            -- Frequency component (0-25)
            CASE
                WHEN calls >= 100000 THEN 25
                WHEN calls >= 10000 THEN 20
                WHEN calls >= 1000 THEN 10
                ELSE 5
            END +
            -- I/O component (0-20)
            CASE
                WHEN COALESCE(cache_hit_ratio, 0) < 50 THEN 20
                WHEN COALESCE(cache_hit_ratio, 0) < 80 THEN 15
                WHEN COALESCE(cache_hit_ratio, 0) < 95 THEN 10
                ELSE 0
            END +
            -- WAL component (0-15)
            CASE
                WHEN wal_bytes > 1073741824 THEN 15
                WHEN wal_bytes > 104857600 THEN 10
                WHEN wal_bytes > 10485760 THEN 5
                ELSE 0
            END +
            -- Temp usage component (0-10)
            CASE
                WHEN COALESCE(temp_blocks, 0) > 10000 THEN 10
                WHEN COALESCE(temp_blocks, 0) > 1000 THEN 5
                ELSE 0
            END
        ))::numeric, 0) as cost_impact_score,
        CASE
            WHEN avg_time_ms > 1000 THEN 'VERY SLOW'
            WHEN avg_time_ms > 500 THEN 'SLOW'
            WHEN avg_time_ms > 100 THEN 'MEDIUM'
            ELSE 'FAST'
        END as speed_classification,
        CASE
            WHEN calls >= 100000 THEN 'VERY HIGH'
            WHEN calls >= 10000 THEN 'HIGH'
            WHEN calls >= 1000 THEN 'MEDIUM'
            ELSE 'LOW'
        END as frequency_category,
        CASE
            WHEN time_variability > 2 THEN 'INCONSISTENT: High variance'
            WHEN time_variability > 1 THEN 'VARIABLE: Moderate variance'
            ELSE 'CONSISTENT: Stable'
        END as performance_consistency,
        CASE
            WHEN COALESCE(cache_hit_ratio, 0) >= 99 THEN 'EXCELLENT'
            WHEN COALESCE(cache_hit_ratio, 0) >= 95 THEN 'GOOD'
            WHEN COALESCE(cache_hit_ratio, 0) >= 90 THEN 'FAIR'
            WHEN COALESCE(cache_hit_ratio, 0) >= 80 THEN 'POOR'
            ELSE 'CRITICAL'
        END as io_efficiency,
        CASE
            WHEN temp_blocks = 0 THEN 'NONE'
            WHEN temp_blocks < 100 THEN 'LOW'
            WHEN temp_blocks < 1000 THEN 'MODERATE'
            WHEN temp_blocks < 10000 THEN 'HIGH'
            ELSE 'CRITICAL'
        END as temp_usage,
        CASE
            WHEN avg_time_ms > 1000 AND calls > 1000 THEN 'CRITICAL: Frequently used slow query'
            WHEN avg_time_ms > 1000 THEN 'CRITICAL: High average execution time'
            WHEN time_variability > 2 THEN 'WARNING: High execution time variability'
            WHEN COALESCE(cache_hit_ratio, 0) < 80 AND shared_blks_read > 1000 THEN 'WARNING: Poor cache performance'
            WHEN temp_blocks > 10000 THEN 'WARNING: High temporary space usage'
            ELSE 'OK'
        END as health_status,
        CASE
            WHEN avg_time_ms > 1000 AND calls > 1000 THEN
                CASE
                    WHEN COALESCE(cache_hit_ratio, 0) < 80 THEN 'CRITICAL: Add indexes and increase shared_buffers. Cache hit: ' || COALESCE(cache_hit_ratio, 0)::text || '%'
                    WHEN temp_blocks > 10000 THEN 'CRITICAL: Increase work_mem to avoid disk writes. Temp usage: ' || total_temp_size
                    ELSE 'CRITICAL: Review query plan and optimize logic'
                END
            WHEN time_variability > 2 THEN
                'WARNING: High variance (' || time_variability::text || 'x) - check for parameter sniffing or lock contention'
            WHEN COALESCE(cache_hit_ratio, 0) < 80 AND shared_blks_read > 1000 THEN
                'WARNING: Poor cache hit ratio (' || COALESCE(cache_hit_ratio, 0)::text || '%) - review indexing strategy'
            WHEN temp_blocks > 10000 THEN
                'WARNING: High temp usage (' || total_temp_size || ') - review sort/join operations'
            WHEN avg_time_ms > 500 THEN
                'REVIEW: Consider optimization if frequently used'
            ELSE 'OK: No immediate action needed'
        END as recommendation,
        ROUND(((
            CASE
                WHEN avg_time_ms > 1000 THEN 0
                WHEN avg_time_ms > 500 THEN 25
                WHEN avg_time_ms > 100 THEN 50
                ELSE 100
            END +
            COALESCE(CASE
                WHEN cache_hit_ratio < 50 THEN 0
                WHEN cache_hit_ratio < 80 THEN 25
                WHEN cache_hit_ratio < 95 THEN 50
                ELSE 100
            END, 100) +
            CASE
                WHEN time_variability > 2 THEN 0
                WHEN time_variability > 1 THEN 50
                ELSE 100
            END
        ) / 3)::numeric, 2) as performance_score,
        CASE
            WHEN calls >= 100000 AND avg_time_ms < 10 THEN
                'This query runs very frequently and is fast. It''s well-optimized.'
            WHEN calls >= 100000 AND avg_time_ms >= 10 THEN
                'This query runs very frequently. Even small improvements will have big impact.'
            WHEN avg_time_ms >= 1000 THEN
                'This query is slow. Users may experience delays when this runs.'
            WHEN COALESCE(cache_hit_ratio, 0) < 90 THEN
                'This query reads a lot from disk instead of memory. Adding indexes could help.'
            WHEN temp_blocks > 1000 THEN
                'This query uses temporary disk space, which slows it down. It needs more memory or optimization.'
            WHEN time_variability > 1 THEN
                'This query has unpredictable performance. Sometimes fast, sometimes slow.'
            ELSE
                'This query is performing reasonably well.'
        END as performance_insight,
        format('Executed %s times | Avg: %sms (min: %sms, max: %sms) | Returns ~%s rows | Cache: %s%% | Temp: %s | QPS: %s',
               calls,
               ROUND(avg_time_ms::numeric, 1),
               ROUND(min_time_ms::numeric, 1),
               ROUND(max_time_ms::numeric, 1),
               ROUND(avg_rows::numeric, 0),
               ROUND(COALESCE(cache_hit_ratio, 0)::numeric, 1),
               total_temp_size,
               ROUND(queries_per_second::numeric, 2)) as performance_summary
    FROM query_metrics
)
SELECT 
    schemas,
    tables,
    query_excerpt,
    execution_count,
    first_seen,
    last_seen,
    days_active,
    queries_per_second,
    performance_score,
    health_status,
    speed_classification,
    frequency_category,
    performance_summary,
    performance_insight,
    performance_consistency,
    io_efficiency,
    temp_usage,
    recommendation,
    ROUND(total_time_ms::numeric, 2) as total_time_ms,
    ROUND(avg_time_ms::numeric, 2) as avg_time_ms,
    ROUND(min_time_ms::numeric, 2) as min_time_ms,
    ROUND(max_time_ms::numeric, 2) as max_time_ms,
    ROUND(stddev_time_ms::numeric, 2) as stddev_time_ms,
    time_variability,
    ROUND(avg_rows::numeric, 2) as avg_rows,
    rows_per_ms,
    shared_blks_read as disk_reads,
    shared_blks_hit as cache_hits,
    ROUND(cache_hit_ratio::numeric, 2) as cache_hit_ratio,
    total_io_blocks,
    total_io_size,
    total_temp_blocks,
    total_temp_size,
    query_type,
    wal_records,
    wal_fpi,
    wal_size,
    wal_bytes_per_call,
    wal_impact,
    plans,
    total_plan_time_ms,
    mean_plan_time_ms,
    plan_time_pct,
    blk_read_time_ms,
    blk_write_time_ms,
    io_wait_pct,
    io_timing_status,
    bottleneck_type,
    local_blks_hit,
    local_blks_read,
    local_blks_written,
    local_io_size,
    write_ratio_pct,
    io_pattern,
    total_jit_time_ms,
    jit_efficiency,
    lock_risk,
    cost_impact_score
FROM query_analysis
ORDER BY 
    CASE 
        WHEN health_status LIKE 'CRITICAL%' THEN 1
        WHEN health_status LIKE 'WARNING%' THEN 2
        ELSE 3
    END,
    performance_score ASC,
    execution_count DESC
LIMIT 20;"
else
    # PostgreSQL 11-12 uses total_time, min_time, etc.
    QUERY_PERF_PRIMARY_SQL="
WITH frequent_queries AS (
    SELECT 
        queryid,
        COALESCE(query, '<query text not available>') as query,
        calls,
        total_time::numeric as execution_time,
        mean_time::numeric as mean_time,
        stddev_time::numeric as stddev_time,
        min_time::numeric as min_time,
        max_time::numeric as max_time,
        rows as query_rows,
        shared_blks_hit,
        shared_blks_read,
        shared_blks_written,
        temp_blks_read,
        temp_blks_written,
        -- Query type classification via regex
        CASE
            WHEN query ~* '^\\s*(WITH\\s+.+\\s+)?INSERT\\s' THEN 'INSERT'
            WHEN query ~* '^\\s*(WITH\\s+.+\\s+)?UPDATE\\s' THEN 'UPDATE'
            WHEN query ~* '^\\s*(WITH\\s+.+\\s+)?DELETE\\s' THEN 'DELETE'
            WHEN query ~* '^\\s*WITH\\s+.*\\b(INSERT|UPDATE|DELETE)\\b' THEN 'CTE-WRITE'
            WHEN query ~* '^\\s*(WITH\\s+.+\\s+)?SELECT\\s' THEN 'SELECT'
            ELSE 'OTHER'
        END as query_type,
        -- WAL metrics (not available in PG 11-12)
        0 as wal_records,
        0 as wal_fpi,
        0::numeric as wal_bytes,
        -- Planning time (not available in PG 11-12)
        0 as plans,
        0::numeric as total_plan_time,
        0::numeric as mean_plan_time,
        -- I/O wait time (requires track_io_timing)
        COALESCE(blk_read_time, 0)::numeric as blk_read_time,
        COALESCE(blk_write_time, 0)::numeric as blk_write_time,
        -- Local blocks
        COALESCE(local_blks_hit, 0) as local_blks_hit,
        COALESCE(local_blks_read, 0) as local_blks_read,
        COALESCE(local_blks_written, 0) as local_blks_written,
        -- JIT stats (not available in PG 11-12)
        0 as jit_functions,
        0::numeric as jit_generation_time,
        0::numeric as jit_inlining_time,
        0::numeric as jit_optimization_time,
        0::numeric as jit_emission_time
    FROM pg_stat_statements
    WHERE calls > 10
        AND query IS NOT NULL
        AND query != '<insufficient privilege>'
),
query_metrics AS (
    SELECT
        queryid,
        query,
        calls,
        execution_time as total_time_ms,
        ROUND((execution_time / NULLIF(calls, 0))::numeric, 2) as avg_time_ms,
        ROUND(mean_time::numeric, 2) as mean_time_ms,
        ROUND(COALESCE(stddev_time, 0)::numeric, 2) as stddev_time_ms,
        ROUND(min_time::numeric, 2) as min_time_ms,
        ROUND(max_time::numeric, 2) as max_time_ms,
        ROUND((query_rows::float / NULLIF(calls, 0))::numeric, 2) as avg_rows,
        ROUND((100.0 * shared_blks_hit / NULLIF(shared_blks_hit + shared_blks_read, 0))::numeric, 2) as cache_hit_ratio,
        (temp_blks_read + temp_blks_written) as temp_blocks,
        (shared_blks_read + shared_blks_written) as total_io_blocks,
        shared_blks_hit,
        shared_blks_read,
        shared_blks_written,
        ROUND((NULLIF(stddev_time, 0) / NULLIF(mean_time, 0))::numeric, 2) as time_variability,
        ROUND((query_rows::numeric / NULLIF(execution_time, 0))::numeric, 2) as rows_per_ms,
        pg_size_pretty((COALESCE(shared_blks_read + shared_blks_written, 0)::bigint * 8192)::bigint) as total_io_size,
        pg_size_pretty((COALESCE(temp_blks_read + temp_blks_written, 0)::bigint * 8192)::bigint) as total_temp_size,
        (SELECT string_agg(DISTINCT nspname, ', ')
         FROM pg_namespace
         WHERE nspname != 'pg_catalog' AND nspname != 'information_schema'
           AND nspname !~ '^pg_'
           AND query ~* ('\\m' || nspname || '\\.')) as detected_schemas,
        (SELECT string_agg(DISTINCT CASE WHEN schemaname IS NOT NULL THEN schemaname || '.' || tablename ELSE tablename END, ', ')
         FROM pg_tables
         WHERE (schemaname || '.' || tablename) = ANY(string_to_array(regexp_replace(regexp_replace(query, '\$[0-9]+', '', 'g'), '[^a-zA-Z0-9_.]', ' ', 'g'), ' '))
            OR tablename = ANY(string_to_array(regexp_replace(regexp_replace(query, '\$[0-9]+', '', 'g'), '[^a-zA-Z0-9_.]', ' ', 'g'), ' '))
         LIMIT 10) as referenced_tables,
        -- Query type passthrough
        query_type,
        -- WAL metrics
        wal_records,
        wal_fpi,
        wal_bytes,
        pg_size_pretty(wal_bytes::bigint) as wal_size,
        CASE
            WHEN wal_bytes > 0 THEN ROUND((wal_bytes / NULLIF(calls, 0))::numeric, 2)
            ELSE 0
        END as wal_bytes_per_call,
        -- Planning time
        plans,
        ROUND(total_plan_time::numeric, 2) as total_plan_time_ms,
        ROUND(mean_plan_time::numeric, 2) as mean_plan_time_ms,
        CASE
            WHEN (total_plan_time + execution_time) > 0
            THEN ROUND((100.0 * total_plan_time / (total_plan_time + execution_time))::numeric, 2)
            ELSE 0
        END as plan_time_pct,
        -- I/O wait time
        ROUND(blk_read_time::numeric, 2) as blk_read_time_ms,
        ROUND(blk_write_time::numeric, 2) as blk_write_time_ms,
        CASE
            WHEN execution_time > 0
            THEN ROUND((100.0 * (blk_read_time + blk_write_time) / execution_time)::numeric, 2)
            ELSE 0
        END as io_wait_pct,
        CASE
            WHEN (blk_read_time + blk_write_time) = 0 AND (shared_blks_read + shared_blks_written) > 100
            THEN 'track_io_timing may be OFF'
            ELSE 'OK'
        END as io_timing_status,
        -- Local blocks
        local_blks_hit,
        local_blks_read,
        local_blks_written,
        pg_size_pretty(((local_blks_read + local_blks_written)::bigint * 8192)::bigint) as local_io_size,
        -- Write ratio
        CASE
            WHEN (shared_blks_hit + shared_blks_read + shared_blks_written) > 0
            THEN ROUND((100.0 * shared_blks_written / (shared_blks_hit + shared_blks_read + shared_blks_written))::numeric, 2)
            ELSE 0
        END as write_ratio_pct,
        CASE
            WHEN shared_blks_written = 0 THEN 'READ-ONLY'
            WHEN shared_blks_written > (shared_blks_hit + shared_blks_read) THEN 'WRITE-HEAVY'
            WHEN shared_blks_written > 0 THEN 'MIXED'
            ELSE 'READ-ONLY'
        END as io_pattern,
        -- JIT stats
        ROUND((jit_generation_time + jit_inlining_time + jit_optimization_time + jit_emission_time)::numeric, 2) as total_jit_time_ms,
        CASE
            WHEN jit_functions > 0 AND (jit_generation_time + jit_inlining_time + jit_optimization_time + jit_emission_time) > 0
            THEN CASE
                WHEN (jit_generation_time + jit_inlining_time + jit_optimization_time + jit_emission_time) > execution_time * 0.5
                THEN 'POOR: JIT overhead > 50%% of exec time'
                WHEN (jit_generation_time + jit_inlining_time + jit_optimization_time + jit_emission_time) > execution_time * 0.2
                THEN 'FAIR: JIT overhead 20-50%% of exec time'
                ELSE 'GOOD: JIT overhead < 20%% of exec time'
            END
            ELSE 'N/A: No JIT activity'
        END as jit_efficiency
    FROM frequent_queries
),
query_analysis AS (
    SELECT
        queryid,
        CASE 
            WHEN query IS NULL OR query = '' THEN '<no query text>'
            WHEN query = '<insufficient privilege>' THEN '<insufficient privilege>'
            ELSE query
        END as query_excerpt,
        COALESCE(detected_schemas, 'public or unspecified') as schemas,
        COALESCE(referenced_tables, 'N/A') as tables,
        calls as execution_count,
        total_time_ms,
        avg_time_ms,
        mean_time_ms,
        stddev_time_ms,
        min_time_ms,
        max_time_ms,
        time_variability,
        avg_rows,
        rows_per_ms,
        COALESCE(cache_hit_ratio, 0) as cache_hit_ratio,
        COALESCE(temp_blocks, 0) as total_temp_blocks,
        COALESCE(total_io_blocks, 0) as total_io_blocks,
        total_io_size,
        total_temp_size,
        shared_blks_hit,
        shared_blks_read,
        shared_blks_written,
        -- New analysis columns
        query_type,
        -- WAL impact
        wal_records,
        wal_fpi,
        wal_size,
        wal_bytes_per_call,
        CASE
            WHEN wal_bytes > 1073741824 THEN 'CRITICAL: > 1GB WAL generated'
            WHEN wal_bytes > 104857600 THEN 'HIGH: > 100MB WAL generated'
            WHEN wal_bytes > 10485760 THEN 'MODERATE: > 10MB WAL generated'
            WHEN wal_bytes > 0 THEN 'LOW'
            ELSE 'NONE'
        END as wal_impact,
        -- Planning time
        plans,
        total_plan_time_ms,
        mean_plan_time_ms,
        plan_time_pct,
        -- I/O wait
        blk_read_time_ms,
        blk_write_time_ms,
        io_wait_pct,
        io_timing_status,
        CASE
            WHEN io_wait_pct > 50 THEN 'IO-BOUND'
            WHEN io_wait_pct > 20 THEN 'IO-MIXED'
            WHEN io_wait_pct > 0 THEN 'CPU-BOUND'
            ELSE 'UNKNOWN'
        END as bottleneck_type,
        -- Local blocks
        local_blks_hit,
        local_blks_read,
        local_blks_written,
        local_io_size,
        -- Write ratio
        write_ratio_pct,
        io_pattern,
        -- JIT stats
        total_jit_time_ms,
        jit_efficiency,
        -- Lock risk assessment
        CASE
            WHEN query_type IN ('UPDATE', 'DELETE') AND avg_time_ms > 500 AND calls > 100
            THEN 'HIGH: Long-running writes with high frequency'
            WHEN query_type IN ('UPDATE', 'DELETE') AND avg_time_ms > 1000
            THEN 'HIGH: Very slow write operations'
            WHEN query_type IN ('UPDATE', 'DELETE', 'CTE-WRITE') AND time_variability > 2
            THEN 'MEDIUM: Write ops with high variance (possible lock waits)'
            WHEN query_type IN ('INSERT', 'UPDATE', 'DELETE', 'CTE-WRITE')
            THEN 'LOW: Normal write operations'
            ELSE 'MINIMAL: Read-only query'
        END as lock_risk,
        -- Cost impact score (0-100, higher = more impactful)
        ROUND(LEAST(100, (
            -- Execution time component (0-30)
            CASE
                WHEN avg_time_ms > 5000 THEN 30
                WHEN avg_time_ms > 1000 THEN 25
                WHEN avg_time_ms > 500 THEN 20
                WHEN avg_time_ms > 100 THEN 10
                ELSE 5
            END +
            -- Frequency component (0-25)
            CASE
                WHEN calls >= 100000 THEN 25
                WHEN calls >= 10000 THEN 20
                WHEN calls >= 1000 THEN 10
                ELSE 5
            END +
            -- I/O component (0-20)
            CASE
                WHEN COALESCE(cache_hit_ratio, 0) < 50 THEN 20
                WHEN COALESCE(cache_hit_ratio, 0) < 80 THEN 15
                WHEN COALESCE(cache_hit_ratio, 0) < 95 THEN 10
                ELSE 0
            END +
            -- WAL component (0-15)
            CASE
                WHEN wal_bytes > 1073741824 THEN 15
                WHEN wal_bytes > 104857600 THEN 10
                WHEN wal_bytes > 10485760 THEN 5
                ELSE 0
            END +
            -- Temp usage component (0-10)
            CASE
                WHEN COALESCE(temp_blocks, 0) > 10000 THEN 10
                WHEN COALESCE(temp_blocks, 0) > 1000 THEN 5
                ELSE 0
            END
        ))::numeric, 0) as cost_impact_score,
        CASE
            WHEN avg_time_ms > 1000 THEN 'VERY SLOW'
            WHEN avg_time_ms > 500 THEN 'SLOW'
            WHEN avg_time_ms > 100 THEN 'MEDIUM'
            ELSE 'FAST'
        END as speed_classification,
        CASE
            WHEN calls >= 100000 THEN 'VERY HIGH'
            WHEN calls >= 10000 THEN 'HIGH'
            WHEN calls >= 1000 THEN 'MEDIUM'
            ELSE 'LOW'
        END as frequency_category,
        CASE
            WHEN time_variability > 2 THEN 'INCONSISTENT: High variance'
            WHEN time_variability > 1 THEN 'VARIABLE: Moderate variance'
            ELSE 'CONSISTENT: Stable'
        END as performance_consistency,
        CASE
            WHEN COALESCE(cache_hit_ratio, 0) >= 99 THEN 'EXCELLENT'
            WHEN COALESCE(cache_hit_ratio, 0) >= 95 THEN 'GOOD'
            WHEN COALESCE(cache_hit_ratio, 0) >= 90 THEN 'FAIR'
            WHEN COALESCE(cache_hit_ratio, 0) >= 80 THEN 'POOR'
            ELSE 'CRITICAL'
        END as io_efficiency,
        CASE
            WHEN temp_blocks = 0 THEN 'NONE'
            WHEN temp_blocks < 100 THEN 'LOW'
            WHEN temp_blocks < 1000 THEN 'MODERATE'
            WHEN temp_blocks < 10000 THEN 'HIGH'
            ELSE 'CRITICAL'
        END as temp_usage,
        CASE
            WHEN avg_time_ms > 1000 AND calls > 1000 THEN 'CRITICAL: Frequently used slow query'
            WHEN avg_time_ms > 1000 THEN 'CRITICAL: High average execution time'
            WHEN time_variability > 2 THEN 'WARNING: High execution time variability'
            WHEN COALESCE(cache_hit_ratio, 0) < 80 AND shared_blks_read > 1000 THEN 'WARNING: Poor cache performance'
            WHEN temp_blocks > 10000 THEN 'WARNING: High temporary space usage'
            ELSE 'OK'
        END as health_status,
        CASE
            WHEN calls >= 50000 AND avg_time_ms >= 100 THEN 'HIGH IMPACT: Frequent and slow'
            WHEN calls >= 100000 THEN 'HIGH IMPACT: Very frequent'
            WHEN avg_time_ms >= 1000 THEN 'MEDIUM IMPACT: Slow execution'
            WHEN total_time_ms >= 100000 THEN 'MEDIUM IMPACT: High cumulative time'
            ELSE 'LOW IMPACT'
        END as impact_assessment,
        CASE
            WHEN avg_time_ms > 1000 AND calls > 1000 THEN
                CASE
                    WHEN COALESCE(cache_hit_ratio, 0) < 80 THEN 'CRITICAL: Add indexes and increase shared_buffers. Cache hit: ' || COALESCE(cache_hit_ratio, 0)::text || '%'
                    WHEN temp_blocks > 10000 THEN 'CRITICAL: Increase work_mem to avoid disk writes. Temp usage: ' || total_temp_size
                    ELSE 'CRITICAL: Review query plan and optimize logic'
                END
            WHEN time_variability > 2 THEN
                'WARNING: High variance (' || time_variability::text || 'x) - check for parameter sniffing or lock contention'
            WHEN COALESCE(cache_hit_ratio, 0) < 80 AND shared_blks_read > 1000 THEN
                'WARNING: Poor cache hit ratio (' || COALESCE(cache_hit_ratio, 0)::text || '%) - review indexing strategy'
            WHEN temp_blocks > 10000 THEN
                'WARNING: High temp usage (' || total_temp_size || ') - review sort/join operations'
            WHEN avg_time_ms > 500 THEN
                'REVIEW: Consider optimization if frequently used'
            ELSE 'OK: No immediate action needed'
        END as recommendation,
        ROUND(((
            CASE
                WHEN avg_time_ms > 1000 THEN 0
                WHEN avg_time_ms > 500 THEN 25
                WHEN avg_time_ms > 100 THEN 50
                ELSE 100
            END +
            COALESCE(CASE
                WHEN cache_hit_ratio < 50 THEN 0
                WHEN cache_hit_ratio < 80 THEN 25
                WHEN cache_hit_ratio < 95 THEN 50
                ELSE 100
            END, 100) +
            CASE
                WHEN time_variability > 2 THEN 0
                WHEN time_variability > 1 THEN 50
                ELSE 100
            END
        ) / 3)::numeric, 2) as performance_score,
        CASE
            WHEN calls >= 100000 AND avg_time_ms < 10 THEN
                'This query runs very frequently and is fast. It''s well-optimized.'
            WHEN calls >= 100000 AND avg_time_ms >= 10 THEN
                'This query runs very frequently. Even small improvements will have big impact.'
            WHEN avg_time_ms >= 1000 THEN
                'This query is slow. Users may experience delays when this runs.'
            WHEN COALESCE(cache_hit_ratio, 0) < 90 THEN
                'This query reads a lot from disk instead of memory. Adding indexes could help.'
            WHEN temp_blocks > 1000 THEN
                'This query uses temporary disk space, which slows it down. It needs more memory or optimization.'
            WHEN time_variability > 1 THEN
                'This query has unpredictable performance. Sometimes fast, sometimes slow.'
            ELSE
                'This query is performing reasonably well.'
        END as performance_insight,
        format('Executed %s times | Avg: %sms (min: %sms, max: %sms) | Returns ~%s rows | Cache: %s%% | Temp: %s',
               calls,
               ROUND(avg_time_ms::numeric, 1),
               ROUND(min_time_ms::numeric, 1),
               ROUND(max_time_ms::numeric, 1),
               ROUND(avg_rows::numeric, 0),
               ROUND(COALESCE(cache_hit_ratio, 0)::numeric, 1),
               total_temp_size) as performance_summary
    FROM query_metrics
)
SELECT 
    schemas,
    tables,
    query_excerpt,
    execution_count,
    performance_score,
    health_status,
    speed_classification,
    frequency_category,
    performance_summary,
    performance_insight,
    performance_consistency,
    io_efficiency,
    temp_usage,
    impact_assessment,
    recommendation,
    ROUND(total_time_ms::numeric, 2) as total_time_ms,
    ROUND(avg_time_ms::numeric, 2) as avg_time_ms,
    ROUND(min_time_ms::numeric, 2) as min_time_ms,
    ROUND(max_time_ms::numeric, 2) as max_time_ms,
    ROUND(stddev_time_ms::numeric, 2) as stddev_time_ms,
    time_variability,
    ROUND(avg_rows::numeric, 2) as avg_rows,
    rows_per_ms,
    shared_blks_read as disk_reads,
    shared_blks_hit as cache_hits,
    ROUND(cache_hit_ratio::numeric, 2) as cache_hit_ratio,
    total_io_blocks,
    total_io_size,
    total_temp_blocks,
    total_temp_size,
    query_type,
    wal_records,
    wal_fpi,
    wal_size,
    wal_bytes_per_call,
    wal_impact,
    plans,
    total_plan_time_ms,
    mean_plan_time_ms,
    plan_time_pct,
    blk_read_time_ms,
    blk_write_time_ms,
    io_wait_pct,
    io_timing_status,
    bottleneck_type,
    local_blks_hit,
    local_blks_read,
    local_blks_written,
    local_io_size,
    write_ratio_pct,
    io_pattern,
    total_jit_time_ms,
    jit_efficiency,
    lock_risk,
    cost_impact_score
FROM query_analysis
ORDER BY 
    CASE 
        WHEN health_status LIKE 'CRITICAL%' THEN 1
        WHEN health_status LIKE 'WARNING%' THEN 2
        ELSE 3
    END,
    performance_score ASC,
    execution_count DESC
LIMIT 20;"
fi

# Fallback SQL using pg_stat_activity when pg_stat_statements is not available
QUERY_PERF_FALLBACK_SQL="
WITH active_queries AS (
    SELECT
        pid,
        usename,
        datname,
        application_name,
        client_addr,
        backend_start,
        query_start,
        state_change,
        state,
        query,
        COALESCE(EXTRACT(EPOCH FROM (now() - query_start)), 0)::numeric(18,2) as query_duration_seconds,
        COALESCE(EXTRACT(EPOCH FROM (now() - backend_start)), 0)::numeric(18,2) as connection_age_seconds,
        wait_event_type,
        wait_event
    FROM pg_stat_activity
    WHERE pid != pg_backend_pid()
        AND query NOT LIKE '%pg_stat_activity%'
        AND state != 'idle'
        AND query_start IS NOT NULL
)
SELECT
    pid,
    usename as username,
    datname as database,
    application_name,
    client_addr,
    state,
    query_duration_seconds,
    connection_age_seconds,
    wait_event_type,
    wait_event,
    query as query_preview,
    CASE
        WHEN query_duration_seconds > 60 THEN 'CRITICAL: Long-running query (>1 min)'
        WHEN query_duration_seconds > 30 THEN 'WARNING: Slow query (>30s)'
        WHEN query_duration_seconds > 10 THEN 'INFO: Active query (>10s)'
        ELSE 'OK'
    END as status,
    CASE
        WHEN query_duration_seconds > 60 THEN 'Investigate query performance - consider canceling if stuck'
        WHEN query_duration_seconds > 30 THEN 'Monitor query - may need optimization'
        WHEN wait_event_type IS NOT NULL THEN 'Query is waiting on: ' || COALESCE(wait_event_type, 'unknown') || ' - ' || COALESCE(wait_event, 'unknown')
        ELSE 'Query executing normally'
    END as recommendation
FROM active_queries
ORDER BY query_duration_seconds DESC
LIMIT 20;
"

# Execute Query Performance Analysis with extension check
execute_sql_with_extension_check "Query Performance Analysis" "$QUERY_PERF_PRIMARY_SQL" "pg_stat_statements" "$QUERY_PERF_FALLBACK_SQL"

# ============================================================================
# QUERY EXTRACTION AND FILTERING MODULE
# ============================================================================
# This module extracts queries from pg_stat_statements with intelligent filtering
# and version-specific SQL generation for PostgreSQL 11-17 compatibility
# ============================================================================

# Function: extract_top_queries_from_pg_stat_statements
# Purpose: Extract and filter queries from pg_stat_statements with version detection
# Inputs: None (uses global variables: DB_HOST, DB_PORT, DB_USER, PGPASSWORD, DB_NAME, PG_VERSION)
# Outputs: Writes filtered queries to temporary file, returns file path
# Filters Applied:
#   - System schema exclusion (pg_catalog, information_schema)
#   - Trivial query exclusion (SELECT 1, version(), SET, SHOW)
#   - Minimum execution time threshold (> 10ms)
#   - Minimum rows examined threshold (> 10 rows)
#   - Sorted by total execution time DESC
#   - Limited to top 50 queries
extract_top_queries_from_pg_stat_statements() {
    local temp_file=$(mktemp)
    
    # Determine the correct time column based on PostgreSQL version
    # PostgreSQL 11-12 use 'total_time', PostgreSQL 13+ use 'total_exec_time'
    local time_column="total_exec_time"
    local mean_time_column="mean_exec_time"
    local min_time_column="min_exec_time"
    local max_time_column="max_exec_time"
    
    if version_lt "$PG_VERSION" 130000; then
        # PostgreSQL 11-12 compatibility
        time_column="total_time"
        mean_time_column="mean_time"
        min_time_column="min_time"
        max_time_column="max_time"
    fi
    
    # Build version-specific SQL query with all filters applied
    # Replace newlines and tabs in query text to prevent parsing issues
    local extraction_sql="
SELECT 
    queryid,
    REPLACE(REPLACE(query, E'\\n', ' '), E'\\t', ' ') as query,
    calls,
    ${mean_time_column} as mean_exec_time,
    ${min_time_column} as min_exec_time,
    ${max_time_column} as max_exec_time,
    rows,
    shared_blks_hit,
    shared_blks_read,
    temp_blks_read,
    temp_blks_written
FROM pg_stat_statements
WHERE 1=1
    -- Filter: System schema exclusion (Requirement 2.1)
    AND query NOT LIKE '%pg_catalog%'
    AND query NOT LIKE '%information_schema%'
    -- Filter: Trivial query exclusion (Requirement 2.2)
    AND query NOT LIKE 'SELECT 1%'
    AND query NOT LIKE 'SELECT version()%'
    AND query NOT LIKE 'SET %'
    AND query NOT LIKE 'SHOW %'
    -- Filter: Execution time threshold (Requirement 2.3)
    AND ${mean_time_column} > 10
    -- Filter: Row examination threshold (Requirement 2.4)
    AND rows > 10
-- Sort and limit (Requirement 2.5)
ORDER BY ${time_column} DESC
LIMIT 50;
"
    
    # Execute query and save to temporary file with tab delimiter
    PGPASSWORD="$PGPASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" \
        -t -A -F $'\t' \
        -c "$extraction_sql" > "$temp_file" 2>/dev/null
    
    # Return the temporary file path
    echo "$temp_file"
}

# ============================================================================
# SEVERITY CALCULATION MODULE
# ============================================================================
# This module calculates severity levels for queries based on performance metrics
# ============================================================================

# Function: calculate_query_severity
# Purpose: Determine performance severity level for a query based on execution time
# Inputs: 
#   $1 - Average execution time in milliseconds (mean_exec_time)
# Outputs: Pipe-delimited string: "SEVERITY_LEVEL|CSS_CLASS|COLOR_CODE"
# Severity Thresholds (Requirements 4.1-4.5):
#   - CRITICAL: >= 1000ms (red)
#   - WARNING:  >= 500ms and < 1000ms (yellow)
#   - INFO:     >= 100ms and < 500ms (blue)
#   - OK:       < 100ms (green)
calculate_query_severity() {
    local avg_exec_time="$1"
    
    # Validate input - ensure we have a numeric value
    if [ -z "$avg_exec_time" ] || ! [[ "$avg_exec_time" =~ ^[0-9]+\.?[0-9]*$ ]]; then
        # Default to OK if invalid input
        echo "OK|status-ok|#10b981"
        return
    fi
    
    # Use bc for floating point comparison
    # CRITICAL: >= 1000ms (Requirement 4.1)
    if (( $(echo "$avg_exec_time >= 1000" | bc -l) )); then
        echo "CRITICAL|status-critical|#dc2626"
    # WARNING: >= 500ms and < 1000ms (Requirement 4.2)
    elif (( $(echo "$avg_exec_time >= 500" | bc -l) )); then
        echo "WARNING|status-warning|#f59e0b"
    # INFO: >= 100ms and < 500ms (Requirement 4.3)
    elif (( $(echo "$avg_exec_time >= 100" | bc -l) )); then
        echo "INFO|status-info|#3b82f6"
    # OK: < 100ms (Requirement 4.4)
    else
        echo "OK|status-ok|#10b981"
    fi
}

# ============================================================================
# ERROR HANDLING MODULE
# ============================================================================
# This module provides error handling and formatting for EXPLAIN failures
# Requirements: 6.2
# ============================================================================

# Function: format_explain_error
# Purpose: Format PostgreSQL error messages in styled HTML boxes
# Inputs:
#   $1 - Error message from PostgreSQL
#   $2 - Query text that caused the error
# Outputs: HTML-formatted error message
# Requirements: 6.2
format_explain_error() {
    local error_msg="$1"
    local query="$2"
    
    # Escape HTML special characters in error message
    error_msg=$(echo "$error_msg" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g; s/"/\&quot;/g')
    
    # Truncate very long error messages
    if [ ${#error_msg} -gt 500 ]; then
        error_msg="${error_msg:0:500}... [truncated]"
    fi
    
    cat << EOF
<div style="padding: 12px; background: #f8d7da; border-left: 4px solid #dc3545; border-radius: 4px; margin: 10px 0;">
    <p style="margin: 0 0 8px 0; font-weight: bold; color: #721c24;">
        <span style="font-size: 16px;">❌</span> EXPLAIN Generation Failed
    </p>
    <p style="margin: 0 0 8px 0; color: #721c24; font-size: 13px;">
        <strong>PostgreSQL Error:</strong>
    </p>
    <pre style="margin: 0; padding: 8px; background: #ffffff; border: 1px solid #f5c6cb; border-radius: 3px; font-family: 'Courier New', Courier, monospace; font-size: 12px; color: #721c24; overflow-x: auto; white-space: pre-wrap; word-wrap: break-word;">${error_msg}</pre>
    <p style="margin: 8px 0 4px 0; color: #721c24; font-size: 12px;">
        <strong>Common causes and remediation:</strong>
    </p>
    <ul style="margin: 0; padding-left: 20px; color: #721c24; font-size: 11px; line-height: 1.6;">
        <li><strong>Parameterized query (\$1, \$2):</strong> EXPLAIN cannot resolve bind parameters. Tool attempts NULL substitution as fallback</li>
        <li><strong>Temp table reference:</strong> Temp tables from other sessions are not visible. EXPLAIN fails on missing objects</li>
        <li><strong>Permission denied:</strong> Current user lacks SELECT on referenced tables. Grant access or run as table owner</li>
        <li><strong>Schema/object not found:</strong> Object may have been dropped since query was recorded in pg_stat_statements</li>
        <li><strong>CTE with DML:</strong> Writable CTEs (INSERT/UPDATE/DELETE inside WITH) cannot be EXPLAINed without ANALYZE</li>
    </ul>
    <p style="margin: 4px 0 0 0; color: #721c24; font-size: 12px;">
        <em>Attempting fallback strategies (parameter replacement, pattern analysis)...</em>
    </p>
</div>
EOF
}

# ============================================================================
# EXPLAIN GENERATION MODULE WITH FALLBACK STRATEGIES
# ============================================================================
# This module generates EXPLAIN plans using multiple fallback strategies
# Strategy 1: JSON format EXPLAIN (Requirements 3.1, 8.1)
# Strategy 2: TEXT format fallback (Requirement 3.2)
# Strategy 3: Parameter replacement (Requirement 3.3)
# Strategy 4: Pattern analysis (Requirements 3.4, 5.1-5.5)
# ============================================================================

# Function: generate_explain_summary
# Purpose: Generate human-readable summary from EXPLAIN TEXT output
# Inputs:
#   $1 - EXPLAIN TEXT output
# Outputs: Human-readable summary
generate_explain_summary() {
    local explain_text="$1"
    local summary=""
    
    # Extract key information
    local total_cost=$(echo "$explain_text" | grep -oP 'cost=[\d.]+\.\.[\d.]+' | head -1 | grep -oP '\.\.[\d.]+' | tr -d '.')
    local plan_rows=$(echo "$explain_text" | grep -oP 'rows=[\d]+' | head -1 | grep -oP '[\d]+')
    local plan_width=$(echo "$explain_text" | grep -oP 'width=[\d]+' | head -1 | grep -oP '[\d]+')
    
    # Detect operation types
    local has_seq_scan=$(echo "$explain_text" | grep -c "Seq Scan" || echo "0")
    local has_index_scan=$(echo "$explain_text" | grep -c "Index Scan" || echo "0")
    local has_nested_loop=$(echo "$explain_text" | grep -c "Nested Loop" || echo "0")
    local has_hash_join=$(echo "$explain_text" | grep -c "Hash Join" || echo "0")
    local has_merge_join=$(echo "$explain_text" | grep -c "Merge Join" || echo "0")
    local has_sort=$(echo "$explain_text" | grep -c "Sort" || echo "0")
    
    summary="📊 EXPLAIN Plan Summary:\n"
    summary="${summary}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    
    if [ -n "$total_cost" ]; then
        summary="${summary}💰 Estimated Cost: ${total_cost}\n"
    fi
    if [ -n "$plan_rows" ]; then
        summary="${summary}📈 Estimated Rows: ${plan_rows}\n"
    fi
    summary="${summary}\n"
    
    summary="${summary}🔍 Operations Detected:\n"
    if [ "$has_seq_scan" -gt 0 ]; then
        summary="${summary}  ⚠️  Sequential Scans: ${has_seq_scan} (consider adding indexes)\n"
    fi
    if [ "$has_index_scan" -gt 0 ]; then
        summary="${summary}  ✅ Index Scans: ${has_index_scan}\n"
    fi
    if [ "$has_nested_loop" -gt 0 ]; then
        summary="${summary}  🔄 Nested Loops: ${has_nested_loop}\n"
    fi
    if [ "$has_hash_join" -gt 0 ]; then
        summary="${summary}  🔗 Hash Joins: ${has_hash_join}\n"
    fi
    if [ "$has_merge_join" -gt 0 ]; then
        summary="${summary}  🔗 Merge Joins: ${has_merge_join}\n"
    fi
    if [ "$has_sort" -gt 0 ]; then
        summary="${summary}  📊 Sort Operations: ${has_sort}\n"
    fi
    
    summary="${summary}\n💡 Key Insights:\n"
    if [ "$has_seq_scan" -gt 0 ]; then
        summary="${summary}  • Sequential scans detected - consider adding indexes on frequently queried columns\n"
    fi
    if [ "$has_nested_loop" -gt 2 ]; then
        summary="${summary}  • Multiple nested loops detected - may indicate missing join indexes\n"
    fi
    if [ "$has_sort" -gt 0 ]; then
        summary="${summary}  • Sort operations present - consider indexes to avoid sorting\n"
    fi
    if [ "$has_index_scan" -gt 0 ] && [ "$has_seq_scan" -eq 0 ]; then
        summary="${summary}  • Good! Query is using indexes efficiently\n"
    fi
    
    echo -e "$summary"
}

# Function: generate_explain_for_query
# Purpose: Generate EXPLAIN plan for a query using multiple fallback strategies
# Inputs: 
#   $1 - SQL query text
# Outputs: Pipe-delimited string: "STATUS|METHOD|OUTPUT"
#   STATUS: success, warning, failed
#   METHOD: JSON, TEXT, Parameter Replacement, Pattern Analysis
#   OUTPUT: EXPLAIN plan text or analysis recommendations
# Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 5.1, 5.2, 5.3, 5.4, 5.5
generate_explain_for_query() {
    local query="$1"
    local explain_output=""
    local explain_status=""
    local explain_method=""
    local last_error=""
    
    # Check if query is truncated (ends with ... or [truncated])
    if [[ "$query" =~ \.\.\.$|truncated\]$ ]]; then
        explain_status="failed"
        explain_method="Query Truncated"
        printf "%s\t%s\n" "$explain_status" "$explain_method"
        cat << EOF
<div style="padding: 12px; background: #fff3cd; border-left: 4px solid #ffc107; border-radius: 4px; margin: 10px 0;">
    <p style="margin: 0 0 8px 0; font-weight: bold; color: #856404;">
        <span style="font-size: 16px;">⚠️</span> Query Text Truncated
    </p>
    <p style="margin: 0 0 8px 0; color: #856404; font-size: 13px;">
        This query exceeds PostgreSQL's <code>track_activity_query_size</code> limit
        (currently set to <strong>${TRACK_ACTIVITY_QUERY_SIZE_RAW}</strong>) and was truncated by pg_stat_statements.
        EXPLAIN generation is not possible with incomplete query text.
    </p>
    <p style="margin: 0 0 8px 0; color: #856404; font-size: 13px;">
        <strong>To capture full query text:</strong>
    </p>
    <ul style="margin: 0; padding-left: 20px; color: #856404; font-size: 13px;">
        <li>Increase <code>track_activity_query_size</code> to 8192 (current: ${TRACK_ACTIVITY_QUERY_SIZE_RAW})</li>
        <li>On RDS/Aurora: modify in the DB parameter group, then reboot</li>
        <li>On EC2/Self-managed: edit postgresql.conf, then restart PostgreSQL</li>
        <li>After restart, reset stats: <code>SELECT pg_stat_statements_reset();</code></li>
    </ul>
    <p style="margin: 8px 0 0 0; color: #856404; font-size: 12px;">
        <em>Note: Query statistics are still available above. To see the execution plan, capture the full query text first.</em>
    </p>
</div>
EOF
        return 0
    fi
    
    # Strategy 1: Try TEXT format EXPLAIN (human-readable) (Requirements 3.2, 8.2)
    explain_output=$(PGPASSWORD="$PGPASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" \
        -t -c "EXPLAIN (ANALYZE false, VERBOSE true, COSTS true, FORMAT TEXT) $query" 2>&1)
    
    if [ $? -eq 0 ] && [ -n "$explain_output" ] && [[ "$explain_output" != *"ERROR"* ]]; then
        # Success with TEXT format - add human-readable summary
        local summary=""
        # Try to generate summary, but don't fail if it doesn't work
        summary=$(generate_explain_summary "$explain_output" 2>/dev/null) || summary=""
        
        explain_status="success"
        explain_method="TEXT"
        # Output status and method on first line (using tab as delimiter), content on subsequent lines
        printf "%s\t%s\n" "$explain_status" "$explain_method"
        
        # Show summary if generated successfully
        if [ -n "$summary" ]; then
            echo "$summary"
            echo ""
            echo "=== Detailed Execution Plan ==="
        fi
        
        echo "$explain_output"
        return 0
    else
        # Capture error message for potential display (Requirement 6.2)
        last_error="$explain_output"
    fi
    
    # Strategy 2: Try parameter replacement with TEXT format (Requirement 3.3)
    # Detect parameterized queries (containing $1, $2, etc.)
    if [[ "$query" =~ \$[0-9]+ ]]; then
        # Replace all $N with NULL
        local modified_query=$(echo "$query" | sed -E 's/\$[0-9]+/NULL/g')
        
        # Try TEXT format with modified query
        explain_output=$(PGPASSWORD="$PGPASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" \
            -t -c "EXPLAIN (ANALYZE false, VERBOSE true, COSTS true, FORMAT TEXT) $modified_query" 2>&1)
        
        if [ $? -eq 0 ] && [ -n "$explain_output" ] && [[ "$explain_output" != *"ERROR"* ]]; then
            # Success with parameter replacement (TEXT) - add summary
            local summary=""
            summary=$(generate_explain_summary "$explain_output" 2>/dev/null) || summary=""
            
            explain_status="warning"
            explain_method="Parameter Replacement"
            # Output status and method on first line (using tab as delimiter), content on subsequent lines
            printf "%s\t%s\n" "$explain_status" "$explain_method"
            echo "⚠️ Note: Parameters (\$1, \$2, etc.) were replaced with NULL for EXPLAIN generation"
            echo ""
            
            # Show summary if generated successfully
            if [ -n "$summary" ]; then
                echo "$summary"
                echo ""
                echo "=== Detailed Execution Plan ==="
            fi
            
            echo "$explain_output"
            return 0
        else
            last_error="$explain_output"
        fi
    fi
    
    # Strategy 3: Check if error is due to truncated query
    if [[ "$last_error" == *"syntax error at end of input"* ]]; then
        explain_status="failed"
        explain_method="Query Truncated"
        printf "%s\t%s\n" "$explain_status" "$explain_method"
        cat << EOF
<div style="padding: 12px; background: #fff3cd; border-left: 4px solid #ffc107; border-radius: 4px; margin: 10px 0;">
    <p style="margin: 0 0 8px 0; font-weight: bold; color: #856404;">
        <span style="font-size: 16px;">⚠️</span> Query Text Truncated
    </p>
    <p style="margin: 0 0 8px 0; color: #856404; font-size: 13px;">
        PostgreSQL reported "syntax error at end of input", which indicates this query was truncated by pg_stat_statements.
        The query exceeds the <code>track_activity_query_size</code> limit (default: 1024 bytes).
    </p>
    <p style="margin: 0 0 8px 0; color: #856404; font-size: 13px;">
        <strong>To capture full query text:</strong>
    </p>
    <ul style="margin: 0; padding-left: 20px; color: #856404; font-size: 13px;">
        <li>Increase <code>track_activity_query_size</code> in postgresql.conf</li>
        <li>Recommended value: 4096 or 8192 bytes for complex queries</li>
        <li>Requires PostgreSQL restart to take effect</li>
        <li>After restart, reset pg_stat_statements: <code>SELECT pg_stat_statements_reset();</code></li>
    </ul>
    <p style="margin: 8px 0 0 0; color: #856404; font-size: 12px;">
        <em>Note: Query statistics are still available above. To see the execution plan, you'll need to capture the full query text.</em>
    </p>
</div>
EOF
        return 0
    fi
    
    # Strategy 4: Pattern analysis with error message (Requirements 3.4, 5.1-5.5, 6.2)
    # Include formatted error message if we have one
    local error_html=""
    if [ -n "$last_error" ]; then
        error_html=$(format_explain_error "$last_error" "$query")
    fi
    
    local pattern_analysis=$(analyze_query_pattern "$query")
    explain_output="${error_html}${pattern_analysis}"
    explain_status="failed"
    explain_method="Pattern Analysis"
    # Output status and method on first line (using tab as delimiter), content on subsequent lines
    printf "%s\t%s\n" "$explain_status" "$explain_method"
    echo "$explain_output"
    return 0
}

# Function: analyze_query_pattern
# Purpose: Perform heuristic pattern analysis when EXPLAIN generation fails
# Inputs: 
#   $1 - SQL query text
# Outputs: HTML-formatted analysis with recommendations
# Requirements: 3.4, 5.1, 5.2, 5.3, 5.4, 5.5
analyze_query_pattern() {
    local query="$1"
    local analysis=""
    
    analysis="<div style='padding: 10px; background: #fff3cd; border-left: 4px solid #ffc107; margin: 10px 0;'>"
    analysis="${analysis}<p style='margin: 0 0 10px 0; font-weight: bold; color: #856404;'>⚠️ EXPLAIN generation failed - Pattern Analysis:</p>"
    analysis="${analysis}<ul style='margin: 5px 0; padding-left: 20px; color: #856404;'>"
    
    local recommendations_found=false
    
    # Requirement 5.1: Detect WHERE clause and recommend indexes
    if [[ "$query" =~ [Ww][Hh][Ee][Rr][Ee] ]]; then
        analysis="${analysis}<li><strong>WHERE clause detected:</strong> Ensure columns in WHERE conditions are indexed for optimal filtering performance.</li>"
        recommendations_found=true
    fi
    
    # Requirement 5.2: Detect JOIN operations, count joins, recommend join indexes
    local join_count=$(echo "$query" | grep -ioE '\b(JOIN|INNER JOIN|LEFT JOIN|RIGHT JOIN|FULL JOIN|CROSS JOIN)\b' | wc -l)
    if [ "$join_count" -gt 0 ]; then
        analysis="${analysis}<li><strong>JOIN operations detected ($join_count joins):</strong> Verify that join columns are properly indexed on both tables to avoid nested loop scans.</li>"
        recommendations_found=true
    fi
    
    # Requirement 5.3: Detect ORDER BY and recommend composite indexes
    if [[ "$query" =~ [Oo][Rr][Dd][Ee][Rr][[:space:]]+[Bb][Yy] ]]; then
        analysis="${analysis}<li><strong>ORDER BY detected:</strong> Consider composite indexes that include both WHERE and ORDER BY columns to enable index-only scans.</li>"
        recommendations_found=true
    fi
    
    # Requirement 5.4: Detect GROUP BY and recommend covering indexes
    if [[ "$query" =~ [Gg][Rr][Oo][Uu][Pp][[:space:]]+[Bb][Yy] ]]; then
        analysis="${analysis}<li><strong>GROUP BY detected:</strong> Check for covering indexes that include all columns in SELECT, WHERE, and GROUP BY clauses.</li>"
        recommendations_found=true
    fi
    
    # Requirement 5.5: Detect LIMIT and recommend early termination indexes
    if [[ "$query" =~ [Ll][Ii][Mm][Ii][Tt] ]]; then
        analysis="${analysis}<li><strong>LIMIT detected:</strong> Ensure proper indexes exist to enable early termination and avoid full table scans.</li>"
        recommendations_found=true
    fi
    
    if [ "$recommendations_found" = false ]; then
        analysis="${analysis}<li>No specific optimization patterns detected. Review query structure and table statistics.</li>"
    fi
    
    analysis="${analysis}</ul>"
    analysis="${analysis}<p style='margin: 10px 0 0 0; font-size: 12px; color: #856404;'><em>Note: These are heuristic recommendations. Actual EXPLAIN plan would provide more specific guidance.</em></p>"
    analysis="${analysis}</div>"
    
    echo "$analysis"
}

# ============================================================================
# HTML RENDERING MODULE
# ============================================================================
# This module generates rich HTML output for query execution plans
# Requirements: 1.1, 1.2, 1.3, 7.1-7.5, 8.3-8.5
# ============================================================================

# Function: render_query_container_header
# Purpose: Generate HTML for query container header with severity badge and query number
# Inputs:
#   $1 - Query number (1-based index)
#   $2 - Severity level (CRITICAL, WARNING, INFO, OK)
#   $3 - CSS class for severity badge
#   $4 - Color code for severity badge
# Outputs: HTML string for query container header
# Requirements: 1.1, 1.3
render_query_container_header() {
    local query_num="$1"
    local severity_level="$2"
    local css_class="$3"
    local color_code="$4"
    
    cat << EOF
<div class="query-explain-container">
    <div class="query-header" style="display: flex; justify-content: space-between; align-items: center; padding: 10px; background: #f8f9fa; border-bottom: 2px solid #dee2e6;">
        <span class="severity-badge" style="padding: 4px 12px; border-radius: 4px; font-weight: bold; font-size: 12px; background-color: ${color_code}; color: white;">
            ${severity_level}
        </span>
        <span class="query-number" style="font-weight: bold; color: #495057; font-size: 14px;">
            Query #${query_num}
        </span>
    </div>
EOF
}

# Function: render_query_statistics
# Purpose: Display comprehensive query statistics including execution metrics and buffer statistics
# Inputs:
#   $1 - calls (execution count)
#   $2 - mean_exec_time (average execution time in ms)
#   $3 - min_exec_time (minimum execution time in ms)
#   $4 - max_exec_time (maximum execution time in ms)
#   $5 - rows (total rows returned)
#   $6 - shared_blks_hit (buffer cache hits)
#   $7 - shared_blks_read (disk reads)
#   $8 - temp_blks_read (temp file reads)
#   $9 - temp_blks_written (temp file writes)
# Outputs: HTML string for query statistics display
# Requirements: 1.2, 7.1, 7.2, 7.3, 7.4, 7.5
render_query_statistics() {
    local calls="$1"
    local mean_exec_time="$2"
    local min_exec_time="$3"
    local max_exec_time="$4"
    local rows="$5"
    local shared_blks_hit="$6"
    local shared_blks_read="$7"
    local temp_blks_read="$8"
    local temp_blks_written="$9"
    
    # Calculate buffer hit ratio (Requirement 7.3)
    local buffer_hit_ratio="N/A"
    if [ -n "$shared_blks_hit" ] && [ -n "$shared_blks_read" ] && \
       [[ "$shared_blks_hit" =~ ^[0-9]+$ ]] && [[ "$shared_blks_read" =~ ^[0-9]+$ ]]; then
        local total_blks=$((shared_blks_hit + shared_blks_read))
        if [ "$total_blks" -gt 0 ]; then
            buffer_hit_ratio=$(echo "scale=2; ($shared_blks_hit * 100.0) / $total_blks" | bc -l)
            buffer_hit_ratio="${buffer_hit_ratio}%"
        fi
    fi
    
    # Handle missing statistics with "N/A" (Requirement 7.5)
    calls="${calls:-N/A}"
    mean_exec_time="${mean_exec_time:-N/A}"
    min_exec_time="${min_exec_time:-N/A}"
    max_exec_time="${max_exec_time:-N/A}"
    rows="${rows:-N/A}"
    shared_blks_hit="${shared_blks_hit:-N/A}"
    shared_blks_read="${shared_blks_read:-N/A}"
    temp_blks_read="${temp_blks_read:-N/A}"
    temp_blks_written="${temp_blks_written:-N/A}"
    
    cat << EOF
    <div class="query-stats" style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 10px; padding: 15px; background: #ffffff; border-bottom: 1px solid #dee2e6;">
        <div style="padding: 8px; background: #f8f9fa; border-radius: 4px;">
            <div style="font-size: 11px; color: #6c757d; font-weight: 600; margin-bottom: 4px;">EXECUTION COUNT</div>
            <div style="font-size: 16px; font-weight: bold; color: #212529;">${calls}</div>
        </div>
        <div style="padding: 8px; background: #f8f9fa; border-radius: 4px;">
            <div style="font-size: 11px; color: #6c757d; font-weight: 600; margin-bottom: 4px;">AVG TIME (ms)</div>
            <div style="font-size: 16px; font-weight: bold; color: #212529;">${mean_exec_time}</div>
        </div>
        <div style="padding: 8px; background: #f8f9fa; border-radius: 4px;">
            <div style="font-size: 11px; color: #6c757d; font-weight: 600; margin-bottom: 4px;">MIN TIME (ms)</div>
            <div style="font-size: 16px; font-weight: bold; color: #212529;">${min_exec_time}</div>
        </div>
        <div style="padding: 8px; background: #f8f9fa; border-radius: 4px;">
            <div style="font-size: 11px; color: #6c757d; font-weight: 600; margin-bottom: 4px;">MAX TIME (ms)</div>
            <div style="font-size: 16px; font-weight: bold; color: #212529;">${max_exec_time}</div>
        </div>
        <div style="padding: 8px; background: #f8f9fa; border-radius: 4px;">
            <div style="font-size: 11px; color: #6c757d; font-weight: 600; margin-bottom: 4px;">ROWS RETURNED</div>
            <div style="font-size: 16px; font-weight: bold; color: #212529;">${rows}</div>
        </div>
        <div style="padding: 8px; background: #f8f9fa; border-radius: 4px;">
            <div style="font-size: 11px; color: #6c757d; font-weight: 600; margin-bottom: 4px;">BUFFER HIT RATIO</div>
            <div style="font-size: 16px; font-weight: bold; color: #212529;">${buffer_hit_ratio}</div>
        </div>
        <div style="padding: 8px; background: #f8f9fa; border-radius: 4px;">
            <div style="font-size: 11px; color: #6c757d; font-weight: 600; margin-bottom: 4px;">CACHE HITS</div>
            <div style="font-size: 16px; font-weight: bold; color: #212529;">${shared_blks_hit}</div>
        </div>
        <div style="padding: 8px; background: #f8f9fa; border-radius: 4px;">
            <div style="font-size: 11px; color: #6c757d; font-weight: 600; margin-bottom: 4px;">DISK READS</div>
            <div style="font-size: 16px; font-weight: bold; color: #212529;">${shared_blks_read}</div>
        </div>
        <div style="padding: 8px; background: #f8f9fa; border-radius: 4px;">
            <div style="font-size: 11px; color: #6c757d; font-weight: 600; margin-bottom: 4px;">TEMP READS</div>
            <div style="font-size: 16px; font-weight: bold; color: #212529;">${temp_blks_read}</div>
        </div>
        <div style="padding: 8px; background: #f8f9fa; border-radius: 4px;">
            <div style="font-size: 11px; color: #6c757d; font-weight: 600; margin-bottom: 4px;">TEMP WRITES</div>
            <div style="font-size: 16px; font-weight: bold; color: #212529;">${temp_blks_written}</div>
        </div>
    </div>
EOF
}

# Function: render_query_text
# Purpose: Display SQL query text with proper escaping and truncation
# Inputs:
#   $1 - SQL query text
# Outputs: HTML string for query text display
# Requirements: 8.3
render_query_text() {
    local query="$1"
    
    # Escape HTML special characters
    query=$(echo "$query" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g; s/"/\&quot;/g; s/'"'"'/\&#39;/g')
    
    # Full query text displayed - no truncation applied
    # Query length is bounded by PostgreSQL's track_activity_query_size setting
    
    cat << EOF
    <div class="query-text" style="padding: 15px; background: #f8f9fa; border-bottom: 1px solid #dee2e6;">
        <div style="font-size: 11px; color: #6c757d; font-weight: 600; margin-bottom: 8px;">SQL QUERY</div>
        <pre style="margin: 0; padding: 10px; background: #ffffff; border: 1px solid #dee2e6; border-radius: 4px; font-family: 'Courier New', Courier, monospace; font-size: 13px; color: #212529; overflow-x: auto; white-space: pre-wrap; word-wrap: break-word;">${query}</pre>
    </div>
EOF
}

# Function: render_explain_output
# Purpose: Display EXPLAIN plan output with proper formatting and truncation
# Inputs:
#   $1 - EXPLAIN output text
#   $2 - EXPLAIN method (JSON, TEXT, Parameter Replacement, Pattern Analysis)
#   $3 - EXPLAIN status (success, warning, failed)
# Outputs: HTML string for EXPLAIN output display
# Requirements: 8.3, 8.4, 8.5
render_explain_output() {
    local explain_output="$1"
    local explain_method="$2"
    local explain_status="$3"
    
    # Only escape HTML for non-HTML output (Pattern Analysis already contains HTML)
    if [ "$explain_method" != "Pattern Analysis" ]; then
        # Escape HTML special characters for EXPLAIN output
        explain_output=$(echo "$explain_output" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g')
    fi
    
    # Truncate output at 5000 characters with indicator (Requirement 8.4)
    local max_length=5000
    local truncated=false
    if [ ${#explain_output} -gt $max_length ]; then
        explain_output="${explain_output:0:$max_length}"
        truncated=true
    fi
    
    # Determine background color based on status
    local bg_color="#ffffff"
    local border_color="#dee2e6"
    local method_color="#6c757d"
    
    case "$explain_status" in
        "success")
            border_color="#28a745"
            method_color="#28a745"
            ;;
        "warning")
            border_color="#ffc107"
            method_color="#856404"
            ;;
        "failed")
            border_color="#dc3545"
            method_color="#721c24"
            ;;
    esac
    
    # For Pattern Analysis, output is already HTML formatted
    if [ "$explain_method" = "Pattern Analysis" ]; then
        cat << EOF
    <div class="explain-output" style="padding: 15px; background: #f8f9fa;">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
            <div style="font-size: 11px; color: #6c757d; font-weight: 600;">EXECUTION PLAN</div>
            <div style="font-size: 11px; color: ${method_color}; font-weight: 600; padding: 2px 8px; background: ${bg_color}; border: 1px solid ${border_color}; border-radius: 3px;">
                METHOD: ${explain_method}
            </div>
        </div>
        ${explain_output}
EOF
    else
        # For actual EXPLAIN output, wrap in <pre> tag
        cat << EOF
    <div class="explain-output" style="padding: 15px; background: #f8f9fa;">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
            <div style="font-size: 11px; color: #6c757d; font-weight: 600;">EXECUTION PLAN</div>
            <div style="font-size: 11px; color: ${method_color}; font-weight: 600; padding: 2px 8px; background: ${bg_color}; border: 1px solid ${border_color}; border-radius: 3px;">
                METHOD: ${explain_method}
            </div>
        </div>
        <pre style="margin: 0; padding: 12px; background: #ffffff; border: 2px solid ${border_color}; border-radius: 4px; font-family: 'Courier New', Courier, monospace; font-size: 12px; color: #212529; overflow-x: auto; white-space: pre-wrap; word-wrap: break-word; max-height: 600px; overflow-y: auto;">${explain_output}</pre>
EOF
    fi
    
    # Add truncation indicator if needed (Requirement 8.4)
    if [ "$truncated" = true ]; then
        cat << EOF
        <div style="margin-top: 8px; padding: 8px; background: #fff3cd; border-left: 4px solid #ffc107; font-size: 12px; color: #856404;">
            ⚠️ Output truncated at 5000 characters. Full plan may be longer.
        </div>
EOF
    fi
    
    echo "    </div>"
}

# Function: render_query_container_footer
# Purpose: Close the query container div
# Outputs: HTML closing tag
render_query_container_footer() {
    echo "</div>"
    # Add visual separator between plans (Requirement 8.5)
    echo '<hr style="margin: 20px 0; border: none; border-top: 2px solid #dee2e6;">'
}

# ============================================================================
# ENHANCED QUERY EXECUTION PLANS SECTION
# ============================================================================
# This section provides rich HTML formatting for EXPLAIN plans with:
# - Intelligent query filtering (system schemas, trivial queries, thresholds)
# - Severity badges based on performance metrics
# - Comprehensive query statistics display
# - Empty state handling with helpful messages
# ============================================================================

# Start the enhanced section with proper HTML structure
cat >> "$OUTPUT_FILE" << 'EOF'
<div class="table-container">
<h3>Query Execution Plans</h3>
<p class="section-description">
    Execution plans for top queries from pg_stat_statements, showing how PostgreSQL executes each query.
    Plans help identify performance bottlenecks, missing indexes, and optimization opportunities.
</p>
EOF

# Check if pg_stat_statements extension is available and has data
if [ "$HAS_PG_STAT_STATEMENTS" = false ] || [ "$HAS_PG_STAT_STATEMENTS_DATA" = false ]; then
    # Display empty state with helpful message
    cat >> "$OUTPUT_FILE" << 'EOF'
<div class="empty-state">
    <p><strong>⚠️ No query execution plans available</strong></p>
    <p>Possible causes:</p>
    <ul>
        <li>pg_stat_statements extension is not installed or enabled</li>
        <li>Extension was recently installed and needs time to collect data</li>
        <li>No queries have been executed since extension installation</li>
        <li>pg_stat_statements was recently reset</li>
    </ul>
    <p><strong>To enable query analysis:</strong></p>
    <ul>
        <li>Install extension: <code>CREATE EXTENSION IF NOT EXISTS pg_stat_statements;</code></li>
        <li>Add to shared_preload_libraries in postgresql.conf: <code>shared_preload_libraries = 'pg_stat_statements'</code></li>
        <li>Restart PostgreSQL to load the extension</li>
        <li>Execute some queries to populate statistics</li>
    </ul>
</div>
EOF
else
    # Extract queries using the new extraction function
    QUERIES_FILE=$(extract_top_queries_from_pg_stat_statements)
    
    # Count the number of queries extracted
    PLAN_QUERY_COUNT=$(wc -l < "$QUERIES_FILE" 2>/dev/null || echo "0")
    
    if [ -z "$PLAN_QUERY_COUNT" ] || [ "$PLAN_QUERY_COUNT" -eq 0 ]; then
        # No queries found after filtering
        cat >> "$OUTPUT_FILE" << 'EOF'
<div class="empty-state">
    <p><strong>⚠️ No queries found matching analysis criteria</strong></p>
    <p>All queries were filtered out due to:</p>
    <ul>
        <li>System schema queries (pg_catalog, information_schema)</li>
        <li>Trivial queries (SELECT 1, version checks, SET/SHOW commands)</li>
        <li>Queries with average execution time ≤ 10ms</li>
        <li>Queries examining ≤ 10 rows</li>
    </ul>
    <p>This typically indicates a healthy system with efficient queries, or a database with minimal activity.</p>
</div>
EOF
    else
        # Process each query with error resilience (Requirement 6.5)
        query_num=0
        processed_count=0
        error_count=0
        
        # Read queries line by line and process each one (using tab delimiter)
        while IFS=$'\t' read -r queryid query calls mean_exec_time min_exec_time max_exec_time rows shared_blks_hit shared_blks_read temp_blks_read temp_blks_written; do
            ((query_num++))
            
            # Wrap each query processing in error handling (Requirement 6.5)
            {
                # Calculate severity for this query
                severity_info=$(calculate_query_severity "$mean_exec_time")
                IFS='|' read -r severity_level css_class color_code <<< "$severity_info"
                
                # Render query container header
                render_query_container_header "$query_num" "$severity_level" "$css_class" "$color_code" >> "$OUTPUT_FILE"
                
                # Render query statistics
                render_query_statistics "$calls" "$mean_exec_time" "$min_exec_time" "$max_exec_time" \
                                       "$rows" "$shared_blks_hit" "$shared_blks_read" \
                                       "$temp_blks_read" "$temp_blks_written" >> "$OUTPUT_FILE"
                
                # Render query text
                render_query_text "$query" >> "$OUTPUT_FILE"
                
                # Generate and render EXPLAIN output
                # Use a temporary file to handle multi-line output properly
                explain_temp_file=$(mktemp)
                generate_explain_for_query "$query" > "$explain_temp_file"
                
                # Read the first line to get status and method (tab-delimited)
                first_line=$(head -n 1 "$explain_temp_file")
                IFS=$'\t' read -r explain_status explain_method <<< "$first_line"
                
                # Read the rest as the explain output (skip first line)
                explain_output=$(tail -n +2 "$explain_temp_file")
                
                # Clean up temp file
                rm -f "$explain_temp_file"
                
                render_explain_output "$explain_output" "$explain_method" "$explain_status" >> "$OUTPUT_FILE"
                
                # Render query container footer
                render_query_container_footer >> "$OUTPUT_FILE"
                
                ((processed_count++))
                
                # Explicitly return success
                true
            } || {
                # Error occurred during processing - log it and continue (Requirement 6.5)
                ((error_count++))
                cat >> "$OUTPUT_FILE" << EOF
<div style="padding: 12px; background: #fff3cd; border-left: 4px solid #ffc107; border-radius: 4px; margin: 10px 0;">
    <p style="margin: 0; color: #856404;">
        <strong>⚠️ Error processing query #${query_num}</strong>
    </p>
    <p style="margin: 5px 0 0 0; color: #856404; font-size: 12px;">
        An error occurred while processing this query. Continuing with remaining queries...
    </p>
</div>
<hr style="margin: 20px 0; border: none; border-top: 2px solid #dee2e6;">
EOF
            }
        done < "$QUERIES_FILE"
        
        # Add processing summary
        cat >> "$OUTPUT_FILE" << EOF
<div style="padding: 15px; background: #e7f3ff; border-radius: 8px; border-left: 4px solid #3498db; margin: 15px 0;">
    <p style="margin: 0; color: #2c3e50;"><strong>📊 Processing Summary</strong></p>
    <p style="margin: 5px 0 0 0; color: #2c3e50; font-size: 13px;">
        Successfully processed <strong>${processed_count}</strong> of <strong>${PLAN_QUERY_COUNT}</strong> queries.
EOF
        
        if [ "$error_count" -gt 0 ]; then
            cat >> "$OUTPUT_FILE" << EOF
        <br><span style="color: #856404;">⚠️ ${error_count} queries encountered errors during processing.</span>
EOF
        fi
        
        cat >> "$OUTPUT_FILE" << EOF
    </p>
</div>
EOF
    fi
    
    # Clean up temporary file
    rm -f "$QUERIES_FILE"
fi

# Close the table container
echo '</div>' >> "$OUTPUT_FILE"


execute_sql_to_html "Transaction Statistics" "
WITH database_xid_info AS (
    SELECT
        d.datname,
        d.datallowconn,
        -- XID function (bash-expanded for version compatibility)
        txid_snapshot_xmax(txid_current_snapshot()) as current_xid,
        age(d.datfrozenxid) as xid_age,
        -- Calculate percentage of 2 billion maximum
        ROUND(100.0 * age(d.datfrozenxid) / 2147483647, 2) as xid_age_percent,
        pg_database_size(d.datname) as db_size,
        current_setting('autovacuum_freeze_max_age')::bigint as max_age,
        ROUND((age(d.datfrozenxid)::numeric / current_setting('autovacuum_freeze_max_age')::numeric * 100), 2) as xid_usage_pct
    FROM pg_database d
    WHERE d.datallowconn
),
table_vacuum_info AS (
    SELECT
        schemaname,
        relname,
        -- Use BLOAT_METHOD to determine analysis approach
        -- Inline extension checking for pgstattuple_approx
        CASE
            WHEN n_live_tup + n_dead_tup > 0
            THEN ROUND(100.0 * n_dead_tup / (n_live_tup + n_dead_tup), 2)
            ELSE 0
        END as dead_tuple_pct,
        n_dead_tup as dead_tuples,
        last_vacuum,
        last_autovacuum,
        CASE
            WHEN last_vacuum IS NOT NULL AND last_autovacuum IS NOT NULL THEN
                EXTRACT(EPOCH FROM (now() - GREATEST(last_vacuum, last_autovacuum)))
            WHEN last_vacuum IS NOT NULL THEN
                EXTRACT(EPOCH FROM (now() - last_vacuum))
            WHEN last_autovacuum IS NOT NULL THEN
                EXTRACT(EPOCH FROM (now() - last_autovacuum))
            ELSE NULL
        END as seconds_since_vacuum
    FROM pg_stat_user_tables
    WHERE n_dead_tup > 0 OR (n_live_tup + n_dead_tup) > 1000
),
vacuum_settings AS (
    SELECT
        name,
        setting,
        unit,
        CASE
            WHEN name = 'autovacuum' AND setting = 'off' THEN 'CRITICAL'
            WHEN name = 'autovacuum_freeze_max_age' AND setting::bigint < 1000000000 THEN 'WARNING'
            ELSE 'OK'
        END as config_status
    FROM pg_settings
    WHERE name IN (
        'autovacuum',
        'autovacuum_freeze_max_age',
        'vacuum_freeze_min_age',
        'vacuum_freeze_table_age',
        'autovacuum_naptime',
        'autovacuum_max_workers'
    )
),
transaction_analysis AS (
    -- Database XID Health with aggressive monitoring thresholds
    SELECT
        1 as sort_order,
        'Database XID Health' as category,
        datname as name,
        pg_size_pretty(db_size) as size,
        -- Display XID age as both absolute count and percentage of 2 billion maximum
        xid_age::text || ' (' || xid_age_percent || '% of 2B max)' as value,
        -- Color-coded severity indicators for each threshold level
        CASE
            WHEN xid_age >= 1900000000 THEN 'SEVERE EMERGENCY'
            WHEN xid_age >= 1800000000 THEN 'EMERGENCY'
            WHEN xid_age >= 1500000000 THEN 'CRITICAL'
            WHEN xid_age >= 1200000000 THEN 'WARNING'
            WHEN xid_age >= 1000000000 THEN 'INFO'
            ELSE 'OK'
        END as status,
        CASE
            WHEN xid_age >= 1900000000 THEN 'SEVERE EMERGENCY (95%): Wraparound imminent within hours! Execute VACUUM FREEZE NOW!'
            WHEN xid_age >= 1800000000 THEN 'EMERGENCY (90%): Immediate action required! Schedule VACUUM FREEZE immediately!'
            WHEN xid_age >= 1500000000 THEN 'CRITICAL (75%): Urgent attention needed - plan VACUUM FREEZE maintenance window'
            WHEN xid_age >= 1200000000 THEN 'WARNING (60%): Monitor closely - XID age increasing, plan preventive maintenance'
            WHEN xid_age >= 1000000000 THEN 'INFO (50%): Approaching threshold - monitor XID age trends'
            ELSE 'XID age is healthy'
        END as recommendation
    FROM database_xid_info

    UNION ALL

    -- Critical Tables Needing Vacuum
    SELECT
        2 as sort_order,
        'Tables Needing Vacuum' as category,
        quote_ident(schemaname) || '.' || quote_ident(relname) as name,
        ROUND(dead_tuple_pct::numeric, 1)::text || '% dead' as size,
        CASE
            WHEN seconds_since_vacuum IS NULL THEN 'Never vacuumed'
            ELSE ROUND((seconds_since_vacuum / 86400)::numeric, 1)::text || ' days ago'
        END as value,
        CASE
            WHEN dead_tuple_pct >= 50 THEN 'CRITICAL'
            WHEN dead_tuple_pct >= 30 THEN 'WARNING'
            WHEN dead_tuple_pct >= 20 THEN 'INFO'
            ELSE 'OK'
        END as status,
        CASE
            WHEN dead_tuple_pct >= 50 THEN 'CRITICAL: Very high bloat - VACUUM FULL or pg_repack recommended'
            WHEN dead_tuple_pct >= 30 THEN 'WARNING: High dead tuple ratio - schedule VACUUM'
            WHEN dead_tuple_pct >= 20 THEN 'INFO: Moderate bloat - monitor and consider VACUUM'
            ELSE 'Dead tuple ratio is acceptable'
        END as recommendation
    FROM table_vacuum_info
    WHERE dead_tuple_pct >= 20

    UNION ALL

    -- Autovacuum Configuration
    SELECT
        3 as sort_order,
        'Autovacuum Configuration' as category,
        name as name,
        setting || COALESCE(' ' || unit, '') as size,
        CASE
            WHEN name = 'autovacuum' THEN
                CASE WHEN setting = 'on' THEN 'Enabled' ELSE 'Disabled' END
            WHEN name = 'autovacuum_max_workers' THEN setting || ' workers'
            ELSE setting
        END as value,
        config_status as status,
        CASE
            WHEN name = 'autovacuum' AND setting = 'off'
                THEN 'CRITICAL: Enable autovacuum to prevent XID wraparound'
            WHEN name = 'autovacuum_freeze_max_age' AND setting::bigint < 1000000000
                THEN 'WARNING: Consider increasing freeze max age for better performance'
            WHEN name = 'autovacuum_max_workers' AND setting::int < 3
                THEN 'INFO: Consider increasing autovacuum workers for busy systems'
            ELSE 'Configuration appears reasonable'
        END as recommendation
    FROM vacuum_settings

    UNION ALL

    -- Vacuum Summary
    SELECT
        4 as sort_order,
        'Vacuum Summary' as category,
        'System Overview' as name,
        COUNT(*)::text || ' tables analyzed' as size,
        COUNT(*) FILTER (WHERE dead_tuple_pct > 20)::text || ' need attention' as value,
        CASE
            WHEN COUNT(*) FILTER (WHERE dead_tuple_pct > 50) > 0 THEN 'CRITICAL'
            WHEN COUNT(*) FILTER (WHERE dead_tuple_pct > 30) > 0 THEN 'WARNING'
            ELSE 'OK'
        END as status,
        CASE
            WHEN COUNT(*) FILTER (WHERE dead_tuple_pct > 50) > 0
                THEN 'CRITICAL: ' || COUNT(*) FILTER (WHERE dead_tuple_pct > 50)::text || ' tables have severe bloat'
            WHEN COUNT(*) FILTER (WHERE dead_tuple_pct > 30) > 0
                THEN 'WARNING: ' || COUNT(*) FILTER (WHERE dead_tuple_pct > 30)::text || ' tables need vacuum'
            ELSE 'All tables have reasonable bloat levels'
        END as recommendation
    FROM table_vacuum_info
)
SELECT
    category,
    name,
    size,
    value,
    status,
    recommendation
FROM transaction_analysis
ORDER BY
    sort_order,
    CASE status
        WHEN 'SEVERE EMERGENCY' THEN 0
        WHEN 'EMERGENCY' THEN 1
        WHEN 'CRITICAL' THEN 2
        WHEN 'WARNING' THEN 3
        WHEN 'INFO' THEN 4
        ELSE 5
    END,
    name;"


# 6.x Function Performance (requires track_functions = 'all' or 'pl')
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Checking track_functions setting for Function Performance..."
TRACK_FUNCTIONS_SETTING=$(PGPASSWORD="$PGPASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -t -c "SELECT current_setting('track_functions');" 2>/dev/null | xargs)

if [ "$TRACK_FUNCTIONS_SETTING" = "all" ] || [ "$TRACK_FUNCTIONS_SETTING" = "pl" ]; then
    execute_sql_to_html "Function Performance" "
WITH function_stats AS (
    SELECT
        n.nspname AS schema_name,
        p.proname AS function_name,
        l.lanname AS language,
        CASE p.prokind
            WHEN 'f' THEN 'function'
            WHEN 'p' THEN 'procedure'
            WHEN 'a' THEN 'aggregate'
            WHEN 'w' THEN 'window'
            ELSE p.prokind::text
        END AS kind,
        s.calls,
        ROUND(s.total_time::numeric, 2) AS total_time_ms,
        ROUND(s.self_time::numeric, 2) AS self_time_ms,
        CASE WHEN s.calls > 0
            THEN ROUND((s.total_time / s.calls)::numeric, 4)
            ELSE 0
        END AS avg_time_ms,
        CASE WHEN s.calls > 0
            THEN ROUND((s.self_time / s.calls)::numeric, 4)
            ELSE 0
        END AS avg_self_time_ms,
        CASE
            WHEN s.calls > 10000 AND (s.total_time / GREATEST(s.calls, 1)) > 100 THEN 'CRITICAL'
            WHEN s.calls > 1000 AND (s.total_time / GREATEST(s.calls, 1)) > 50 THEN 'WARNING'
            WHEN s.calls > 100 AND (s.total_time / GREATEST(s.calls, 1)) > 10 THEN 'INFO'
            ELSE 'OK'
        END AS severity,
        CASE
            WHEN s.calls > 10000 AND (s.total_time / GREATEST(s.calls, 1)) > 100
                THEN 'High-frequency slow function - review logic and consider caching'
            WHEN s.calls > 1000 AND (s.total_time / GREATEST(s.calls, 1)) > 50
                THEN 'Moderate concern - check for unnecessary complexity'
            WHEN s.total_time > 60000
                THEN 'High cumulative time - consider optimization'
            ELSE 'Performance acceptable'
        END AS recommendation
    FROM pg_stat_user_functions s
    JOIN pg_proc p ON p.oid = s.funcid
    JOIN pg_namespace n ON n.oid = p.pronamespace
    JOIN pg_language l ON l.oid = p.prolang
    WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
)
SELECT
    schema_name,
    function_name,
    language,
    kind,
    calls,
    total_time_ms,
    self_time_ms,
    avg_time_ms,
    avg_self_time_ms,
    severity,
    recommendation
FROM function_stats
ORDER BY total_time_ms DESC
LIMIT 50;"
else
    execute_sql_to_html "Function Performance" "
SELECT
    'track_functions is set to: ' || current_setting('track_functions') AS status,
    'Set track_functions = ''all'' (or ''pl'' for PL functions only) to enable function performance tracking' AS recommendation,
    'Requires PostgreSQL restart after parameter change' AS note;"
fi

# 6.x Wait Event Analysis
execute_sql_to_html "Wait Event Analysis" "
WITH wait_events AS (
    SELECT
        COALESCE(wait_event_type, 'CPU/Running') AS wait_event_type,
        COALESCE(wait_event, 'CPU/Running') AS wait_event,
        state,
        COUNT(*) AS session_count,
        COUNT(*) FILTER (WHERE state = 'active') AS active_sessions,
        COUNT(*) FILTER (WHERE state = 'idle') AS idle_sessions,
        COUNT(*) FILTER (WHERE state = 'idle in transaction') AS idle_in_txn
    FROM pg_stat_activity
    WHERE pid != pg_backend_pid()
      AND backend_type = 'client backend'
    GROUP BY wait_event_type, wait_event, state
),
wait_summary AS (
    SELECT
        wait_event_type,
        wait_event,
        SUM(session_count) AS total_sessions,
        SUM(active_sessions) AS active_sessions,
        SUM(idle_sessions) AS idle_sessions,
        SUM(idle_in_txn) AS idle_in_txn_sessions,
        CASE
            WHEN wait_event_type = 'Lock' AND SUM(session_count) > 5 THEN 'CRITICAL'
            WHEN wait_event_type = 'Lock' THEN 'WARNING'
            WHEN wait_event_type = 'IO' AND SUM(active_sessions) > 10 THEN 'WARNING'
            WHEN wait_event_type = 'LWLock' AND SUM(active_sessions) > 5 THEN 'WARNING'
            WHEN wait_event_type IN ('BufferPin', 'Extension') THEN 'INFO'
            ELSE 'OK'
        END AS severity,
        CASE
            WHEN wait_event_type = 'Lock' AND SUM(session_count) > 5
                THEN 'Multiple sessions blocked on locks - investigate blocking queries'
            WHEN wait_event_type = 'Lock'
                THEN 'Lock contention detected - monitor for escalation'
            WHEN wait_event_type = 'IO' AND SUM(active_sessions) > 10
                THEN 'High I/O wait - check storage performance and shared_buffers'
            WHEN wait_event_type = 'LWLock' AND wait_event = 'WALWriteLock'
                THEN 'WAL write contention - consider wal_buffers tuning'
            WHEN wait_event_type = 'LWLock' AND wait_event LIKE '%Buffer%'
                THEN 'Buffer contention - review shared_buffers sizing'
            WHEN wait_event_type = 'LWLock'
                THEN 'Lightweight lock contention - may indicate resource pressure'
            ELSE 'Normal operation'
        END AS recommendation
    FROM wait_events
    GROUP BY wait_event_type, wait_event
)
SELECT
    wait_event_type,
    wait_event,
    total_sessions,
    active_sessions,
    idle_sessions,
    idle_in_txn_sessions,
    severity,
    recommendation
FROM wait_summary
ORDER BY
    CASE severity
        WHEN 'CRITICAL' THEN 0
        WHEN 'WARNING' THEN 1
        WHEN 'INFO' THEN 2
        ELSE 3
    END,
    total_sessions DESC
LIMIT 50;"

# 6.x Session Duration Analysis
execute_sql_to_html "Session Duration Analysis" "
WITH session_info AS (
    SELECT
        pid,
        COALESCE(NULLIF(application_name, ''), '<unnamed>') AS application_name,
        COALESCE(client_addr::text, 'local') AS client_address,
        state,
        COALESCE(wait_event_type, 'None') AS wait_event_type,
        backend_start,
        xact_start,
        query_start,
        state_change,
        EXTRACT(EPOCH FROM (now() - backend_start))::integer AS session_age_seconds,
        EXTRACT(EPOCH FROM (now() - COALESCE(xact_start, now())))::integer AS txn_age_seconds,
        EXTRACT(EPOCH FROM (now() - COALESCE(query_start, now())))::integer AS query_age_seconds
    FROM pg_stat_activity
    WHERE pid != pg_backend_pid()
      AND backend_type = 'client backend'
),
session_analysis AS (
    SELECT
        pid,
        application_name,
        client_address,
        state,
        wait_event_type,
        CASE
            WHEN session_age_seconds > 86400 THEN (session_age_seconds / 86400) || 'd ' || ((session_age_seconds % 86400) / 3600) || 'h'
            WHEN session_age_seconds > 3600 THEN (session_age_seconds / 3600) || 'h ' || ((session_age_seconds % 3600) / 60) || 'm'
            ELSE (session_age_seconds / 60) || 'm ' || (session_age_seconds % 60) || 's'
        END AS session_duration,
        CASE
            WHEN state = 'idle in transaction' AND txn_age_seconds > 300 THEN 'CRITICAL'
            WHEN state = 'idle in transaction' AND txn_age_seconds > 60 THEN 'WARNING'
            WHEN state = 'active' AND query_age_seconds > 3600 THEN 'CRITICAL'
            WHEN state = 'active' AND query_age_seconds > 300 THEN 'WARNING'
            WHEN session_age_seconds > 86400 AND state = 'idle' THEN 'INFO'
            ELSE 'OK'
        END AS severity,
        CASE
            WHEN state = 'idle in transaction' AND txn_age_seconds > 300
                THEN 'Long idle-in-transaction (' || (txn_age_seconds / 60) || 'min) - holding locks, blocking vacuum'
            WHEN state = 'idle in transaction' AND txn_age_seconds > 60
                THEN 'Idle-in-transaction for ' || (txn_age_seconds / 60) || 'min - review application commit behavior'
            WHEN state = 'active' AND query_age_seconds > 3600
                THEN 'Query running > 1hr - check for missing indexes or inefficient plans'
            WHEN state = 'active' AND query_age_seconds > 300
                THEN 'Long-running query (' || (query_age_seconds / 60) || 'min) - monitor for lock impact'
            WHEN session_age_seconds > 86400 AND state = 'idle'
                THEN 'Idle session > 24h - consider connection pooling or idle_session_timeout'
            ELSE 'Normal'
        END AS recommendation,
        session_age_seconds
    FROM session_info
)
SELECT
    pid,
    application_name,
    client_address,
    state,
    wait_event_type,
    session_duration,
    severity,
    recommendation
FROM session_analysis
WHERE severity != 'OK'
ORDER BY
    CASE severity
        WHEN 'CRITICAL' THEN 0
        WHEN 'WARNING' THEN 1
        WHEN 'INFO' THEN 2
        ELSE 3
    END,
    session_age_seconds DESC
LIMIT 50;"

# 6.x Prepared Statement Analysis
execute_sql_to_html "Prepared Statement Analysis" "
WITH prepared_stats AS (
    SELECT
        name AS statement_name,
        statement,
        prepare_time,
        EXTRACT(EPOCH FROM (now() - prepare_time))::integer AS age_seconds,
        parameter_types::text AS parameter_types,
        from_sql,
        CASE
            WHEN EXTRACT(EPOCH FROM (now() - prepare_time)) > 86400 THEN 'WARNING'
            WHEN EXTRACT(EPOCH FROM (now() - prepare_time)) > 3600 THEN 'INFO'
            ELSE 'OK'
        END AS severity,
        CASE
            WHEN EXTRACT(EPOCH FROM (now() - prepare_time)) > 86400
                THEN 'Prepared statement older than 24h - may indicate connection leak or missing DEALLOCATE'
            WHEN EXTRACT(EPOCH FROM (now() - prepare_time)) > 3600
                THEN 'Prepared statement older than 1h - verify this is expected for long-lived connections'
            ELSE 'Normal lifecycle'
        END AS recommendation
    FROM pg_prepared_statements
),
summary AS (
    SELECT
        COUNT(*) AS total_prepared,
        COUNT(*) FILTER (WHERE age_seconds > 86400) AS older_than_24h,
        COUNT(*) FILTER (WHERE age_seconds > 3600) AS older_than_1h,
        COUNT(*) FILTER (WHERE from_sql) AS from_sql_count,
        COUNT(*) FILTER (WHERE NOT from_sql) AS from_protocol_count
    FROM prepared_stats
)
SELECT
    COALESCE(ps.statement_name, '(summary)') AS statement_name,
    CASE
        WHEN ps.statement_name IS NULL THEN 'Total: ' || s.total_prepared || ' prepared statements (' || s.from_sql_count || ' SQL, ' || s.from_protocol_count || ' protocol)'
        ELSE LEFT(ps.statement, 100)
    END AS statement_preview,
    CASE
        WHEN ps.statement_name IS NULL THEN NULL
        ELSE ps.prepare_time::text
    END AS prepare_time,
    CASE
        WHEN ps.statement_name IS NULL THEN
            CASE
                WHEN s.older_than_24h > 0 THEN 'WARNING'
                WHEN s.older_than_1h > 5 THEN 'INFO'
                ELSE 'OK'
            END
        ELSE ps.severity
    END AS severity,
    CASE
        WHEN ps.statement_name IS NULL THEN
            CASE
                WHEN s.total_prepared = 0 THEN 'No prepared statements found in current session'
                WHEN s.older_than_24h > 0 THEN s.older_than_24h || ' statements older than 24h - check for connection leaks'
                ELSE 'Prepared statement usage looks healthy'
            END
        ELSE ps.recommendation
    END AS recommendation
FROM summary s
LEFT JOIN prepared_stats ps ON true
ORDER BY
    CASE WHEN ps.statement_name IS NULL THEN 0 ELSE 1 END,
    CASE ps.severity
        WHEN 'WARNING' THEN 0
        WHEN 'INFO' THEN 1
        ELSE 2
    END,
    ps.age_seconds DESC NULLS LAST
LIMIT 50;"

# 6.x Temp File Heavy Queries (REQ-11.7)
execute_sql_with_extension_check "Temp File Heavy Queries" "
SELECT
    query AS query_preview,
    calls,
    temp_blks_written,
    temp_blks_read,
    pg_size_pretty(temp_blks_written * 8192) AS temp_written_size,
    pg_size_pretty(temp_blks_read * 8192) AS temp_read_size,
    ROUND(CASE WHEN calls > 0 THEN temp_blks_written::numeric / calls ELSE 0 END, 2) AS avg_temp_blks_per_call,
    CASE
        WHEN temp_blks_written > 100000 THEN 'CRITICAL'
        WHEN temp_blks_written > 10000 THEN 'WARNING'
        WHEN temp_blks_written > 1000 THEN 'INFO'
        ELSE 'OK'
    END AS severity,
    CASE
        WHEN temp_blks_written > 100000 THEN 'Excessive temp file usage - increase work_mem (current: ' || current_setting('work_mem') || ') or optimize query to reduce sorts/hashes'
        WHEN temp_blks_written > 10000 THEN 'Significant temp file usage - consider increasing work_mem or adding indexes to avoid large sorts'
        WHEN temp_blks_written > 1000 THEN 'Moderate temp file usage - monitor and consider work_mem tuning'
        ELSE 'Temp file usage within acceptable range'
    END AS recommendation
FROM pg_stat_statements
WHERE temp_blks_written > 0
ORDER BY temp_blks_written DESC
LIMIT 10;" "pg_stat_statements" "
SELECT
    'pg_stat_statements extension is not available' AS note,
    current_setting('work_mem') AS current_work_mem,
    'Install pg_stat_statements to identify queries generating excessive temp files' AS recommendation;"

# Add general recommendations for Performance Metrics monitoring
cat >> "$OUTPUT_FILE" << 'EOF'
<div class="table-container">
<h3 style="font-size: 0.7em; margin: 0; padding: 0;">📋 General Recommendations for Performance Metrics Monitoring</h3>
<div style="background: linear-gradient(135deg, #e8f4fd 0%, #f0f8ff 100%); padding: 15px; border-radius: 8px; border-left: 4px solid #3498db; margin: 15px 0; font-size: 0.7em; line-height: 1.3;">
    <div style="color: #2c3e50; margin: 0;"><strong>🔧 AWS Recommendations:</strong><br>
    <strong><a href="https://aws.amazon.com/premiumsupport/technology/trusted-advisor/" target="_blank" style="color: #3498db; text-decoration: none;">AWS Trusted Advisor</a></strong>: Database performance optimization recommendations and cost analysis | <strong><a href="https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_WorkingWithDBInstanceParamGroups.html" target="_blank" style="color: #3498db; text-decoration: none;">RDS Parameter Groups</a></strong>: Optimize checkpoint, memory, and query planner settings for your workload</div>
    
    <div style="color: #2c3e50; margin-top: 10px;"><strong>🔍 PostgreSQL Extensions & Tools:</strong><br>
    <strong><a href="https://www.postgresql.org/docs/current/pgstatstatements.html" target="_blank" style="color: #3498db; text-decoration: none;">pg_stat_statements</a></strong>: Comprehensive query performance tracking and optimization | <strong><a href="https://www.postgresql.org/docs/current/pgbuffercache.html" target="_blank" style="color: #3498db; text-decoration: none;">pg_buffercache</a></strong>: Buffer pool analysis and memory optimization insights</div>
</div>
</div>
EOF

echo '</div>' >> "$OUTPUT_FILE"
echo '</div>' >> "$OUTPUT_FILE"

# 7. MAINTENANCE & HEALTH
echo ""
echo "=============================================================================="
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting Section 7: MAINTENANCE & HEALTH"
echo "=============================================================================="
echo '<div class="section">' >> "$OUTPUT_FILE"
echo '<input type="checkbox" id="section7-toggle" class="accordion-checkbox">' >> "$OUTPUT_FILE"
echo '<label for="section7-toggle" class="accordion-label section-header" id="section7"><h2>7. MAINTENANCE & HEALTH (Vacuum, XID & Integrity)</h2></label>' >> "$OUTPUT_FILE"
echo '<div class="accordion-content">' >> "$OUTPUT_FILE"

execute_sql_to_html "Database Integrity Check" "
WITH version_info AS (
    SELECT current_setting('server_version_num')::integer as version_num
),
database_stats AS (
    SELECT
        d.datname,
        d.datallowconn,
        d.datconnlimit,
        age(d.datfrozenxid) as xid_age,
        pg_database_size(d.datname) as db_size,
        s.xact_commit,
        s.xact_rollback,
        s.deadlocks,
        s.blks_read,
        s.blks_hit
    FROM pg_database d
    LEFT JOIN pg_stat_database s ON d.oid = s.datid
    WHERE d.datname = current_database()
),
index_stats AS (
    SELECT
        n.nspname as schemaname,
        c.relname as tablename,
        ic.relname as indexname,
        i.indisvalid,
        i.indisready,
        i.indislive
    FROM pg_index i
    JOIN pg_class c ON i.indrelid = c.oid
    JOIN pg_class ic ON i.indexrelid = ic.oid
    JOIN pg_namespace n ON c.relnamespace = n.oid
    WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
),
table_stats AS (
    SELECT
        n.nspname as schemaname,
        c.relname as tablename,
        c.oid as relid,
        c.relpages,
        c.reltuples,
        -- Use pg_stat_user_tables for bloat estimation (works without pgstattuple)
        CASE
            WHEN COALESCE(s.n_live_tup, 0) + COALESCE(s.n_dead_tup, 0) > 0
            THEN ROUND(100.0 * COALESCE(s.n_dead_tup, 0) / (COALESCE(s.n_live_tup, 0) + COALESCE(s.n_dead_tup, 0)), 2)
            ELSE 0
        END as dead_tuple_pct,
        COALESCE(s.n_dead_tup, 0) as n_dead_tup,
        COALESCE(s.n_live_tup, 0) as n_live_tup,
        s.last_analyze,
        s.last_autoanalyze,
        s.n_mod_since_analyze,
        COALESCE(c.reloptions::text LIKE '%autovacuum_enabled=false%', false) as autovacuum_disabled
    FROM pg_class c
    JOIN pg_namespace n ON c.relnamespace = n.oid
    LEFT JOIN pg_stat_user_tables s ON c.relname = s.relname AND n.nspname = s.schemaname
    WHERE c.relkind = 'r'
    AND n.nspname NOT IN ('pg_catalog', 'information_schema')
    AND pg_total_relation_size(c.oid) > 1048576
),
integrity_checks AS (
    -- Database Level Checks
    SELECT
        1 as check_order,
        'Database Status' as check_type,
        datname as object_name,
        CASE
            WHEN NOT datallowconn THEN 'CRITICAL'
            WHEN xid_age > 1800000000 THEN 'EMERGENCY'
            WHEN xid_age > 1500000000 THEN 'CRITICAL'
            WHEN datconnlimit > 0 AND datconnlimit < 10 THEN 'WARNING'
            ELSE 'OK'
        END as status,
        'Database connectivity and XID age check' as description,
        CASE
            WHEN NOT datallowconn THEN 'Database not accepting connections'
            WHEN xid_age > 1800000000 THEN 'XID wraparound imminent (' || xid_age || ' transactions)'
            WHEN xid_age > 1500000000 THEN 'High XID age (' || xid_age || ' transactions)'
            WHEN datconnlimit > 0 AND datconnlimit < 10 THEN 'Low connection limit (' || datconnlimit || ')'
            ELSE 'Database operational'
        END as details,
        CASE
            WHEN NOT datallowconn THEN 'CRITICAL: Check database status and restore connectivity'
            WHEN xid_age > 1800000000 THEN 'EMERGENCY: Immediate VACUUM FREEZE required'
            WHEN xid_age > 1500000000 THEN 'CRITICAL: Schedule VACUUM FREEZE immediately'
            WHEN datconnlimit > 0 AND datconnlimit < 10 THEN 'WARNING: Consider increasing connection limit'
            ELSE 'OK'
        END as recommendation
    FROM database_stats

    UNION ALL

    -- Invalid Indexes
    SELECT
        2,
        'Invalid Indexes',
        schemaname || '.' || tablename || '.' || indexname,
        'WARNING',
        'Broken or incomplete index detected',
        CASE
            WHEN NOT indisvalid THEN 'Index is invalid'
            WHEN NOT indisready THEN 'Index is not ready'
            WHEN NOT indislive THEN 'Index is not live'
        END,
        'WARNING: REINDEX ' || schemaname || '.' || indexname || ' recommended'
    FROM index_stats
    WHERE NOT indisvalid OR NOT indisready OR NOT indislive

    UNION ALL

    -- Table Integrity (Now with accurate bloat detection)
    SELECT
        3,
        'Table Bloat',
        ts.schemaname || '.' || ts.tablename,
        CASE
            WHEN ts.dead_tuple_pct > 30 THEN 'WARNING'
            WHEN ts.dead_tuple_pct > 20 THEN 'INFO'
            ELSE 'OK'
        END,
        'Real-time table bloat analysis using pgstattuple_approx',
        'Dead tuple percentage: ' || ROUND(ts.dead_tuple_pct::numeric, 1) || '% (' ||
        COALESCE(ts.n_dead_tup::text, '0') || ' dead, ' ||
        COALESCE(ts.n_live_tup::text, '0') || ' live)',
        CASE
            WHEN ts.dead_tuple_pct > 30 THEN 'WARNING: High bloat - VACUUM FULL or pg_repack recommended'
            WHEN ts.dead_tuple_pct > 20 THEN 'INFO: Moderate bloat - schedule VACUUM'
            ELSE 'OK'
        END
    FROM table_stats ts
    WHERE ts.dead_tuple_pct > 10

    UNION ALL

    -- Statistics Status (with stale stats and autovacuum disabled detection)
    SELECT
        4,
        'Statistics Status',
        ts.schemaname || '.' || ts.tablename,
        CASE
            WHEN ts.autovacuum_disabled THEN 'WARNING'
            WHEN ts.last_analyze IS NULL AND ts.last_autoanalyze IS NULL THEN 'WARNING'
            WHEN COALESCE(ts.last_analyze, ts.last_autoanalyze) < (now() - interval '7 days') THEN 'INFO'
            WHEN ts.n_mod_since_analyze > 10000 THEN 'INFO'
            ELSE 'OK'
        END,
        'Table statistics freshness',
        'Last analyzed: ' || COALESCE(
            GREATEST(ts.last_analyze, ts.last_autoanalyze)::text,
            'never'
        ) || ', Mods since analyze: ' || COALESCE(ts.n_mod_since_analyze::text, 'N/A') ||
        CASE WHEN ts.autovacuum_disabled THEN ', Autovacuum: DISABLED' ELSE '' END,
        CASE
            WHEN ts.autovacuum_disabled
            THEN 'WARNING: Autovacuum disabled on this table - manual maintenance required'
            WHEN ts.last_analyze IS NULL AND ts.last_autoanalyze IS NULL
            THEN 'WARNING: Run ANALYZE on this table'
            WHEN ts.n_mod_since_analyze > 10000 AND COALESCE(ts.last_analyze, ts.last_autoanalyze) < (now() - interval '7 days')
            THEN 'WARNING: Stale statistics with ' || ts.n_mod_since_analyze || ' modifications since last analyze'
            WHEN COALESCE(ts.last_analyze, ts.last_autoanalyze) < (now() - interval '7 days')
            THEN 'INFO: Statistics are stale - consider ANALYZE'
            ELSE 'OK'
        END
    FROM table_stats ts
    WHERE ts.reltuples > 1000

    UNION ALL

    -- Cache Hit Ratio
    SELECT
        5,
        'Cache Efficiency',
        datname,
        CASE
            WHEN blks_hit + blks_read = 0 THEN 'INFO'
            WHEN (blks_hit::numeric / NULLIF(blks_hit + blks_read, 0)) < 0.85 THEN 'WARNING'
            WHEN (blks_hit::numeric / NULLIF(blks_hit + blks_read, 0)) < 0.95 THEN 'INFO'
            ELSE 'OK'
        END,
        'Buffer cache performance analysis',
        CASE
            WHEN blks_hit + blks_read = 0 THEN 'No I/O activity recorded'
            ELSE ROUND((blks_hit::numeric / (blks_hit + blks_read) * 100), 2)::text || '% cache hit ratio'
        END,
        CASE
            WHEN blks_hit + blks_read = 0 THEN 'INFO: Monitor after database activity'
            WHEN (blks_hit::numeric / NULLIF(blks_hit + blks_read, 0)) < 0.85
            THEN 'WARNING: Poor cache performance - increase shared_buffers'
            WHEN (blks_hit::numeric / NULLIF(blks_hit + blks_read, 0)) < 0.95
            THEN 'INFO: Consider memory optimization'
            ELSE 'OK'
        END
    FROM database_stats

    UNION ALL

    -- Transaction Health
    SELECT
        6,
        'Transaction Health',
        datname,
        CASE
            WHEN xact_commit + xact_rollback = 0 THEN 'INFO'
            WHEN (xact_rollback::numeric / NULLIF(xact_commit + xact_rollback, 0)) > 0.10 THEN 'WARNING'
            WHEN (xact_rollback::numeric / NULLIF(xact_commit + xact_rollback, 0)) > 0.05 THEN 'INFO'
            ELSE 'OK'
        END,
        'Application transaction patterns',
        CASE
            WHEN xact_commit + xact_rollback = 0 THEN 'No transactions recorded'
            ELSE ROUND((xact_rollback::numeric / (xact_commit + xact_rollback) * 100), 2)::text ||
                 '% rollback ratio (' || xact_rollback || ' rollbacks, ' || xact_commit || ' commits)'
        END,
        CASE
            WHEN xact_commit + xact_rollback = 0 THEN 'INFO: Monitor after application activity'
            WHEN (xact_rollback::numeric / NULLIF(xact_commit + xact_rollback, 0)) > 0.10
            THEN 'WARNING: High rollback ratio - investigate application errors'
            WHEN (xact_rollback::numeric / NULLIF(xact_commit + xact_rollback, 0)) > 0.05
            THEN 'INFO: Elevated rollback ratio - monitor application health'
            ELSE 'OK'
        END
    FROM database_stats
)
SELECT
    check_type as \"Check Type\",
    object_name as \"Object\",
    status as \"Status\",
    description as \"Description\",
    details as \"Details\",
    recommendation as \"Recommendation\"
FROM integrity_checks
ORDER BY
    check_order,
    CASE status
        WHEN 'EMERGENCY' THEN 0
        WHEN 'CRITICAL' THEN 1
        WHEN 'WARNING' THEN 2
        WHEN 'INFO' THEN 3
        ELSE 4
    END,
    object_name;"

# Add general recommendations for Maintenance Health monitoring
cat >> "$OUTPUT_FILE" << 'EOF'
<div class="table-container">
<h3 style="font-size: 0.7em; margin: 0; padding: 0;">📋 General Recommendations for Maintenance Health Monitoring</h3>
<div style="background: linear-gradient(135deg, #e8f4fd 0%, #f0f8ff 100%); padding: 15px; border-radius: 8px; border-left: 4px solid #3498db; margin: 15px 0; font-size: 0.7em; line-height: 1.3;">
    <div style="color: #2c3e50; margin: 0;"><strong>🔧 AWS Recommendations:</strong><br>
    <strong><a href="https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_UpgradeDBInstance.Maintenance.html" target="_blank" style="color: #3498db; text-decoration: none;">RDS Maintenance Windows</a></strong>: Schedule automated patching and minor version upgrades during low-traffic periods | <strong><a href="https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_WorkingWithAutomatedBackups.html" target="_blank" style="color: #3498db; text-decoration: none;">RDS Automated Backups</a></strong>: Point-in-time recovery for data protection and integrity verification</div>
    
    <div style="color: #2c3e50; margin-top: 10px;"><strong>🔍 PostgreSQL Extensions & Tools:</strong><br>
    <strong><a href="https://www.postgresql.org/docs/current/app-pgchecksums.html" target="_blank" style="color: #3498db; text-decoration: none;">pg_checksums</a></strong>: Data corruption detection and file-level integrity verification | <strong><a href="https://www.postgresql.org/docs/current/monitoring-stats.html#MONITORING-PG-STAT-DATABASE-VIEW" target="_blank" style="color: #3498db; text-decoration: none;">pg_stat_database</a></strong>: Database-level statistics and transaction health metrics monitoring</div>
</div>
</div>
EOF

# 7.x Sequence Exhaustion Analysis (PG 10+ only - pg_sequences view)
if [ "$PG_VERSION" -ge 100000 ] 2>/dev/null; then
    execute_sql_to_html "Sequence Exhaustion Analysis" "
SELECT
    schemaname,
    sequencename,
    last_value,
    max_value,
    ROUND(100.0 * last_value / max_value, 2) as percent_used,
    CASE
        WHEN last_value::numeric / max_value::numeric > 0.9 THEN 'CRITICAL: >90% used'
        WHEN last_value::numeric / max_value::numeric > 0.75 THEN 'WARNING: >75% used'
        WHEN last_value::numeric / max_value::numeric > 0.5 THEN 'INFO: >50% used'
        ELSE 'OK'
    END as status,
    CASE
        WHEN last_value::numeric / max_value::numeric > 0.9 THEN
            'URGENT: Sequence nearly exhausted. Consider: ALTER SEQUENCE ' || schemaname || '.' || sequencename || ' RESTART WITH 1; or use BIGINT'
        WHEN last_value::numeric / max_value::numeric > 0.75 THEN
            'Plan migration to BIGINT or implement sequence cycling'
        ELSE 'Monitor sequence usage'
    END as recommendation
FROM pg_sequences
WHERE last_value IS NOT NULL
  AND max_value > 0
  AND last_value::numeric / max_value::numeric > 0.5
ORDER BY percent_used DESC;"
fi

# 7.x Constraint Validation Status
execute_sql_to_html "Constraint Validation Status" "
SELECT
    n.nspname as schema_name,
    c.conrelid::regclass as table_name,
    c.conname as constraint_name,
    CASE c.contype
        WHEN 'c' THEN 'CHECK'
        WHEN 'f' THEN 'FOREIGN KEY'
        WHEN 'u' THEN 'UNIQUE'
        WHEN 'x' THEN 'EXCLUSION'
        ELSE c.contype::text
    END as constraint_type,
    pg_get_constraintdef(c.oid) as constraint_definition,
    CASE
        WHEN c.contype = 'f' THEN 'WARNING: Unvalidated FK - data integrity not guaranteed'
        WHEN c.contype = 'c' THEN 'WARNING: Unvalidated CHECK - constraint may be violated'
        ELSE 'INFO: Unvalidated constraint'
    END as status,
    'Run: ALTER TABLE ' || c.conrelid::regclass || ' VALIDATE CONSTRAINT ' || c.conname || ';' as recommendation
FROM pg_constraint c
JOIN pg_namespace n ON n.oid = c.connamespace
WHERE NOT c.convalidated
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
ORDER BY
    CASE c.contype WHEN 'f' THEN 0 WHEN 'c' THEN 1 ELSE 2 END,
    c.conrelid::regclass::text;"

# 7.x Materialized View Freshness
execute_sql_to_html "Materialized View Freshness" "
SELECT
    m.schemaname,
    m.matviewname as view_name,
    m.ispopulated,
    pg_size_pretty(pg_total_relation_size(quote_ident(m.schemaname) || '.' || quote_ident(m.matviewname))) as view_size,
    s.n_live_tup as row_count,
    s.last_analyze,
    s.last_autoanalyze,
    CASE
        WHEN NOT m.ispopulated THEN 'CRITICAL: View never populated'
        WHEN s.last_analyze IS NULL AND s.last_autoanalyze IS NULL THEN 'WARNING: Never analyzed - freshness unknown'
        WHEN GREATEST(COALESCE(s.last_analyze, '1970-01-01'), COALESCE(s.last_autoanalyze, '1970-01-01')) < NOW() - INTERVAL '7 days' THEN 'WARNING: Possibly stale (>7 days since last analyze)'
        ELSE 'OK'
    END as status,
    CASE
        WHEN NOT m.ispopulated THEN 'Run: REFRESH MATERIALIZED VIEW ' || quote_ident(m.schemaname) || '.' || quote_ident(m.matviewname) || ';'
        WHEN s.last_analyze IS NULL AND s.last_autoanalyze IS NULL THEN 'Consider scheduling regular REFRESH MATERIALIZED VIEW'
        WHEN GREATEST(COALESCE(s.last_analyze, '1970-01-01'), COALESCE(s.last_autoanalyze, '1970-01-01')) < NOW() - INTERVAL '7 days' THEN 'Review refresh schedule - data may be outdated'
        ELSE 'Refresh schedule appears adequate'
    END as recommendation
FROM pg_matviews m
LEFT JOIN pg_stat_user_tables s ON s.relname = m.matviewname AND s.schemaname = m.schemaname
WHERE m.schemaname NOT IN ('pg_catalog', 'information_schema')
ORDER BY
    CASE
        WHEN NOT m.ispopulated THEN 0
        WHEN s.last_analyze IS NULL AND s.last_autoanalyze IS NULL THEN 1
        ELSE 2
    END,
    m.matviewname;"

echo '</div>' >> "$OUTPUT_FILE"
echo '</div>' >> "$OUTPUT_FILE"

# 8. OPTIMIZATION
echo ""
echo "=============================================================================="
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting Section 8: OPTIMIZATION"
echo "=============================================================================="
echo '<div class="section">' >> "$OUTPUT_FILE"
echo '<input type="checkbox" id="section8-toggle" class="accordion-checkbox">' >> "$OUTPUT_FILE"
echo '<label for="section8-toggle" class="accordion-label section-header" id="section8"><h2>8. OPTIMIZATION (Index, Table & Schema)</h2></label>' >> "$OUTPUT_FILE"
echo '<div class="accordion-content">' >> "$OUTPUT_FILE"

execute_sql_to_html "Index Statistics" "
WITH RECURSIVE 
version_info AS (
    SELECT current_setting('server_version_num')::integer as version_num
),
index_stats AS (
    SELECT
        ns.nspname as schemaname,
        ci.relname as indexname,
        ct.relname as tablename,
        i.indrelid::regclass as table_relation,
        i.indexrelid::regclass as index_relation,
        i.indisunique,
        i.indisprimary,
        i.indisvalid,
        i.indisready,
        i.indislive,
        am.amname as index_type,
        pg_get_indexdef(i.indexrelid) as index_definition,
        pg_relation_size(i.indexrelid) as index_size_bytes,
        coalesce(s.idx_scan, 0) as idx_scan,
        coalesce(s.idx_tup_read, 0) as idx_tup_read,
        coalesce(s.idx_tup_fetch, 0) as idx_tup_fetch,
        age(c.relfrozenxid) as xid_age
    FROM pg_index i
    JOIN pg_class ci ON ci.oid = i.indexrelid
    JOIN pg_class ct ON ct.oid = i.indrelid
    JOIN pg_namespace ns ON ns.oid = ct.relnamespace
    JOIN pg_am am ON ci.relam = am.oid
    LEFT JOIN pg_stat_user_indexes s ON s.indexrelid = i.indexrelid
    JOIN pg_class c ON i.indexrelid = c.oid
    WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
),
table_stats AS (
    SELECT 
        schemaname,
        relname,
        n_live_tup,
        n_dead_tup,
        seq_scan,
        idx_scan
    FROM pg_stat_user_tables
),
index_analysis AS (
    SELECT 
        i.schemaname,
        i.tablename,
        i.indexname,
        i.index_type,
        i.idx_scan,
        i.idx_tup_read,
        i.idx_tup_fetch,
        i.index_size_bytes,
        i.indisunique,
        i.indisprimary,
        i.indisvalid,
        i.indisready,
        i.indislive,
        t.n_live_tup,
        t.n_dead_tup,
        t.seq_scan,
        t.idx_scan as table_idx_scan,
        CASE 
            WHEN i.idx_scan > 0 THEN 
                (i.idx_tup_read::numeric / i.idx_scan)::numeric(10,2)
            ELSE 0 
        END as avg_tuples_per_scan,
        CASE
            WHEN NOT i.indisvalid THEN 'INVALID'
            WHEN NOT i.indisready THEN 'NOT READY'
            WHEN NOT i.indislive THEN 'NOT LIVE'
            WHEN i.idx_scan = 0 THEN 'UNUSED'
            WHEN i.idx_scan < 50 THEN 'RARELY USED'
            ELSE 'HEALTHY'
        END as index_health
    FROM index_stats i
    LEFT JOIN table_stats t ON 
        i.schemaname = t.schemaname AND 
        i.tablename = t.relname
)
SELECT 
    schemaname as \"Schema\",
    tablename as \"Table\",
    indexname as \"Index\",
    pg_size_pretty(index_size_bytes) as \"Size\",
    CAST(ROUND((index_size_bytes::numeric / (1024*1024)), 2) as text) || ' MB' as \"Size MB\",
    idx_scan as \"Scans\",
    idx_tup_read as \"Rows Read\",
    idx_tup_fetch as \"Rows Fetched\",
    index_type as \"Type\",
    CASE 
        WHEN indisprimary THEN 'PRIMARY KEY'
        WHEN indisunique THEN 'UNIQUE'
        ELSE 'NORMAL'
    END as \"Category\",
    index_health as \"Health\",
    CASE
        WHEN NOT indisvalid THEN 
            'CRITICAL: Invalid index - REINDEX required'
        WHEN NOT indisready OR NOT indislive THEN
            'CRITICAL: Index not ready or not live - check system health'
        WHEN idx_scan = 0 AND NOT indisprimary AND NOT indisunique THEN 
            CASE 
                WHEN index_size_bytes > 100*1024*1024 THEN 
                    'CRITICAL: Large unused index (' || pg_size_pretty(index_size_bytes) || ') - consider dropping'
                WHEN index_size_bytes > 10*1024*1024 THEN 
                    'WARNING: Medium unused index (' || pg_size_pretty(index_size_bytes) || ') - review usage'
                ELSE 
                    'INFO: Unused index - monitor usage'
            END
        WHEN idx_scan < 50 AND index_size_bytes > 10*1024*1024 THEN 
            'WARNING: Large rarely-used index (' || pg_size_pretty(index_size_bytes) || ')'
        ELSE 'OK'
    END as \"Recommendation\",
    avg_tuples_per_scan as \"Avg Tuples/Scan\"
FROM index_analysis
WHERE (
    -- Show problematic indexes
    NOT indisvalid OR 
    NOT indisready OR 
    NOT indislive OR
    -- Show unused large indexes
    (idx_scan = 0 AND index_size_bytes > 1024*1024 AND NOT indisprimary) OR
    -- Show rarely used large indexes
    (idx_scan < 50 AND index_size_bytes > 10*1024*1024)
)
ORDER BY 
    CASE 
        WHEN NOT indisvalid THEN 1  -- Invalid indexes first
        WHEN NOT indisready OR NOT indislive THEN 2  -- Problem indexes next
        WHEN idx_scan = 0 THEN 3  -- Unused indexes
        ELSE 4  -- Rarely used indexes
    END,
    index_size_bytes DESC,
    schemaname,
    tablename,
    indexname
LIMIT 50;"

# Conditional TOAST query based on pgstattuple availability
if [ "$HAS_PGSTATTUPLE" = true ]; then
    # Version A: WITH pgstattuple_approx() for accurate dead tuple counts
    TOAST_QUERY="
WITH toast_info AS (
    SELECT
        n.nspname as schema_name,
        t.relname AS table_name,
        tt.relname AS toast_table_name,
        pg_total_relation_size(t.oid) AS main_table_size,
        pg_total_relation_size(tt.oid) AS toast_table_size,
        t.reltoastrelid,
        t.reltuples::bigint AS estimated_rows,
        t.relpages as main_table_pages,
        tt.relpages as toast_pages,
        -- Use pgstattuple_approx for accurate dead tuple detection
        COALESCE((pgstattuple_approx(quote_ident(n.nspname)||'.'||quote_ident(t.relname))).dead_tuple_count, s.n_dead_tup, 0) as n_dead_tup,
        COALESCE((pgstattuple_approx(quote_ident(n.nspname)||'.'||quote_ident(t.relname))).approx_tuple_count, s.n_live_tup, 0) as n_live_tup,
        s.last_vacuum,
        s.last_autovacuum,
        COALESCE(s.vacuum_count, 0) as vacuum_count,
        COALESCE(s.autovacuum_count, 0) as autovacuum_count,
        age(t.relfrozenxid) as main_table_age,
        age(tt.relfrozenxid) as toast_table_age,
        CASE
            WHEN s.last_vacuum IS NOT NULL AND s.last_autovacuum IS NOT NULL THEN
                EXTRACT(EPOCH FROM (now() - GREATEST(s.last_vacuum, s.last_autovacuum)))::bigint
            WHEN s.last_vacuum IS NOT NULL THEN
                EXTRACT(EPOCH FROM (now() - s.last_vacuum))::bigint
            WHEN s.last_autovacuum IS NOT NULL THEN
                EXTRACT(EPOCH FROM (now() - s.last_autovacuum))::bigint
            ELSE NULL
        END as seconds_since_last_vacuum
    FROM pg_class t
    JOIN pg_namespace n ON n.oid = t.relnamespace
    JOIN pg_class tt ON tt.oid = t.reltoastrelid
    LEFT JOIN pg_stat_user_tables s ON s.relid = t.oid
    WHERE t.relkind = 'r'
    AND t.reltoastrelid != 0
    AND n.nspname NOT IN ('pg_catalog', 'information_schema')
    AND pg_total_relation_size(t.oid) > 1048576  -- Only tables > 1MB
),
critical_age_info AS (
    SELECT
        current_setting('autovacuum_freeze_max_age')::bigint as max_age,
        current_setting('autovacuum_multixact_freeze_max_age')::bigint as multi_max_age
),
toast_summary AS (
    SELECT
        COUNT(*) as total_tables,
        pg_size_pretty(SUM(toast_table_size)) as total_toast_size,
        COUNT(*) FILTER (WHERE n_dead_tup > 10000) as tables_needing_vacuum,
        COUNT(*) FILTER (WHERE main_table_age > (SELECT max_age * 0.5 FROM critical_age_info)) as tables_high_xid,
        COUNT(*) FILTER (WHERE main_table_age > (SELECT max_age * 0.8 FROM critical_age_info)) as tables_critical_xid
    FROM toast_info
)
SELECT
    metric,
    value,
    status,
    description,
    details,
    recommendation
FROM (
    -- TOAST Health Overview
    SELECT
        'TOAST Health Overview' as metric,
        total_tables::text || ' tables' as value,
        CASE
            WHEN tables_critical_xid > 0 THEN 'CRITICAL'
            WHEN tables_needing_vacuum > total_tables * 0.3 THEN 'WARNING'
            WHEN tables_high_xid > 0 THEN 'WARNING'
            ELSE 'OK'
        END as status,
        'Overall TOAST table health assessment' as description,
        format('Total TOAST tables: %s\nTotal TOAST size: %s\nTables needing vacuum: %s\nTables with high XID age: %s\nTables at critical XID age: %s',
            total_tables,
            total_toast_size,
            tables_needing_vacuum,
            tables_high_xid,
            tables_critical_xid) as details,
        CASE
            WHEN tables_critical_xid > 0 THEN 'CRITICAL: Immediate VACUUM FREEZE required for XID wraparound prevention'
            WHEN tables_needing_vacuum > total_tables * 0.3 THEN 'WARNING: Over 30% of TOAST tables need vacuum maintenance'
            WHEN tables_high_xid > 0 THEN 'WARNING: Monitor XID age and plan vacuum maintenance'
            ELSE 'TOAST tables are healthy'
        END as recommendation
    FROM toast_summary

    UNION ALL

    -- Critical TOAST Tables
    SELECT
        'Critical TOAST Tables' as metric,
        COUNT(*)::text as value,
        CASE
            WHEN COUNT(*) FILTER (WHERE main_table_age > (SELECT max_age * 0.8 FROM critical_age_info)) > 0 THEN 'CRITICAL'
            WHEN COUNT(*) FILTER (WHERE n_dead_tup > 50000) > 0 THEN 'WARNING'
            ELSE 'OK'
        END as status,
        'Tables requiring immediate attention' as description,
        COALESCE((
            SELECT string_agg(
                format('%s.%s - Age: %s, Dead: %s, TOAST: %s',
                    schema_name,
                    table_name,
                    main_table_age,
                    n_dead_tup,
                    pg_size_pretty(toast_table_size)),
                E'\n')
            FROM (
                SELECT schema_name, table_name, main_table_age, n_dead_tup, toast_table_size
                FROM toast_info
                WHERE main_table_age > (SELECT max_age * 0.5 FROM critical_age_info)
                   OR n_dead_tup > 10000
                ORDER BY main_table_age DESC, n_dead_tup DESC
                LIMIT 10
            ) t
        ), 'No critical tables found') as details,
        CASE
            WHEN COUNT(*) FILTER (WHERE main_table_age > (SELECT max_age * 0.8 FROM critical_age_info)) > 0
                THEN 'CRITICAL: Run VACUUM FREEZE immediately on high-age tables'
            WHEN COUNT(*) FILTER (WHERE n_dead_tup > 50000) > 0
                THEN 'WARNING: Schedule VACUUM for tables with high dead tuple count'
            ELSE 'No immediate action required'
        END as recommendation
    FROM toast_info

    UNION ALL

    -- TOAST Size Analysis
    SELECT
        'TOAST Size Analysis' as metric,
        COUNT(*)::text || ' tables analyzed' as value,
        CASE
            WHEN COUNT(*) FILTER (WHERE toast_table_size > main_table_size) > 0 THEN 'INFO'
            ELSE 'OK'
        END as status,
        'TOAST storage efficiency analysis' as description,
        COALESCE((
            SELECT string_agg(
                format('%s.%s - TOAST: %s (%s%% of main table)',
                    schema_name,
                    table_name,
                    pg_size_pretty(toast_table_size),
                    ROUND((toast_table_size::numeric * 100 / NULLIF(main_table_size, 0)), 1)),
                E'\n')
            FROM (
                SELECT schema_name, table_name, toast_table_size, main_table_size
                FROM toast_info
                ORDER BY toast_table_size DESC
                LIMIT 10
            ) t
        ), 'No TOAST tables found') as details,
        CASE
            WHEN COUNT(*) FILTER (WHERE toast_table_size > main_table_size * 2) > 0
                THEN 'INFO: Some tables have very large TOAST storage - consider schema optimization'
            ELSE 'TOAST storage ratios are reasonable'
        END as recommendation
    FROM toast_info

    UNION ALL

    -- Vacuum Efficiency
    SELECT
        'Vacuum Efficiency' as metric,
        COUNT(*)::text || ' tables monitored' as value,
        CASE
            WHEN COUNT(*) FILTER (WHERE seconds_since_last_vacuum > 604800 AND n_dead_tup > 1000) > 0 THEN 'WARNING'
            WHEN COUNT(*) FILTER (WHERE seconds_since_last_vacuum IS NULL) > 0 THEN 'INFO'
            ELSE 'OK'
        END as status,
        'Vacuum frequency and effectiveness for TOAST tables' as description,
        COALESCE((
            SELECT string_agg(
                format('%s.%s - Dead: %s, Last vacuum: %s, Manual: %s, Auto: %s',
                    schema_name,
                    table_name,
                    n_dead_tup,
                    CASE
                        WHEN seconds_since_last_vacuum IS NULL THEN 'never'
                        ELSE format('%s days ago', (seconds_since_last_vacuum/86400)::int)
                    END,
                    vacuum_count,
                    autovacuum_count),
                E'\n')
            FROM (
                SELECT schema_name, table_name, n_dead_tup, seconds_since_last_vacuum,
                       vacuum_count, autovacuum_count
                FROM toast_info
                ORDER BY n_dead_tup DESC, seconds_since_last_vacuum DESC NULLS FIRST
                LIMIT 10
            ) t
        ), 'No vacuum information available') as details,
        CASE
            WHEN COUNT(*) FILTER (WHERE seconds_since_last_vacuum > 604800 AND n_dead_tup > 1000) > 0
                THEN 'WARNING: Some tables have not been vacuumed recently despite dead tuples'
            WHEN COUNT(*) FILTER (WHERE seconds_since_last_vacuum IS NULL) > 0
                THEN 'INFO: Some tables have never been vacuumed - monitor for growth'
            ELSE 'Vacuum frequency is appropriate'
        END as recommendation
    FROM toast_info
) metrics
ORDER BY
    CASE status
        WHEN 'CRITICAL' THEN 1
        WHEN 'WARNING' THEN 2
        WHEN 'INFO' THEN 3
        ELSE 4
    END,
    CASE metric
        WHEN 'TOAST Health Overview' THEN 1
        WHEN 'Critical TOAST Tables' THEN 2
        WHEN 'TOAST Size Analysis' THEN 3
        WHEN 'Vacuum Efficiency' THEN 4
        ELSE 5
    END;
"
else
    # Version B: WITHOUT pgstattuple_approx() - uses only pg_stat_user_tables
    TOAST_QUERY="
WITH toast_info AS (
    SELECT
        n.nspname as schema_name,
        t.relname AS table_name,
        tt.relname AS toast_table_name,
        pg_total_relation_size(t.oid) AS main_table_size,
        pg_total_relation_size(tt.oid) AS toast_table_size,
        t.reltoastrelid,
        t.reltuples::bigint AS estimated_rows,
        t.relpages as main_table_pages,
        tt.relpages as toast_pages,
        -- Use pg_stat_user_tables only (no pgstattuple_approx)
        COALESCE(s.n_dead_tup, 0) as n_dead_tup,
        COALESCE(s.n_live_tup, 0) as n_live_tup,
        s.last_vacuum,
        s.last_autovacuum,
        COALESCE(s.vacuum_count, 0) as vacuum_count,
        COALESCE(s.autovacuum_count, 0) as autovacuum_count,
        age(t.relfrozenxid) as main_table_age,
        age(tt.relfrozenxid) as toast_table_age,
        CASE
            WHEN s.last_vacuum IS NOT NULL AND s.last_autovacuum IS NOT NULL THEN
                EXTRACT(EPOCH FROM (now() - GREATEST(s.last_vacuum, s.last_autovacuum)))::bigint
            WHEN s.last_vacuum IS NOT NULL THEN
                EXTRACT(EPOCH FROM (now() - s.last_vacuum))::bigint
            WHEN s.last_autovacuum IS NOT NULL THEN
                EXTRACT(EPOCH FROM (now() - s.last_autovacuum))::bigint
            ELSE NULL
        END as seconds_since_last_vacuum
    FROM pg_class t
    JOIN pg_namespace n ON n.oid = t.relnamespace
    JOIN pg_class tt ON tt.oid = t.reltoastrelid
    LEFT JOIN pg_stat_user_tables s ON s.relid = t.oid
    WHERE t.relkind = 'r'
    AND t.reltoastrelid != 0
    AND n.nspname NOT IN ('pg_catalog', 'information_schema')
    AND pg_total_relation_size(t.oid) > 1048576  -- Only tables > 1MB
),
critical_age_info AS (
    SELECT
        current_setting('autovacuum_freeze_max_age')::bigint as max_age,
        current_setting('autovacuum_multixact_freeze_max_age')::bigint as multi_max_age
),
toast_summary AS (
    SELECT
        COUNT(*) as total_tables,
        pg_size_pretty(SUM(toast_table_size)) as total_toast_size,
        COUNT(*) FILTER (WHERE n_dead_tup > 10000) as tables_needing_vacuum,
        COUNT(*) FILTER (WHERE main_table_age > (SELECT max_age * 0.5 FROM critical_age_info)) as tables_high_xid,
        COUNT(*) FILTER (WHERE main_table_age > (SELECT max_age * 0.8 FROM critical_age_info)) as tables_critical_xid
    FROM toast_info
)
SELECT
    metric,
    value,
    status,
    description,
    details,
    recommendation
FROM (
    -- Extension Availability Notice
    SELECT
        'Extension Notice' as metric,
        'pgstattuple not available' as value,
        'INFO' as status,
        'Dead tuple counts are estimated from pg_stat_user_tables' as description,
        'The pgstattuple extension is not installed. Dead tuple counts shown are estimates (est) from pg_stat_user_tables statistics, which may not be as accurate as pgstattuple_approx() results. Consider installing pgstattuple extension for more accurate analysis.' as details,
        'To enable accurate dead tuple detection, run: CREATE EXTENSION IF NOT EXISTS pgstattuple;' as recommendation

    UNION ALL

    -- TOAST Health Overview
    SELECT
        'TOAST Health Overview' as metric,
        total_tables::text || ' tables' as value,
        CASE
            WHEN tables_critical_xid > 0 THEN 'CRITICAL'
            WHEN tables_needing_vacuum > total_tables * 0.3 THEN 'WARNING'
            WHEN tables_high_xid > 0 THEN 'WARNING'
            ELSE 'OK'
        END as status,
        'Overall TOAST table health assessment' as description,
        format('Total TOAST tables: %s\nTotal TOAST size: %s\nTables needing vacuum (est): %s\nTables with high XID age: %s\nTables at critical XID age: %s',
            total_tables,
            total_toast_size,
            tables_needing_vacuum,
            tables_high_xid,
            tables_critical_xid) as details,
        CASE
            WHEN tables_critical_xid > 0 THEN 'CRITICAL: Immediate VACUUM FREEZE required for XID wraparound prevention'
            WHEN tables_needing_vacuum > total_tables * 0.3 THEN 'WARNING: Over 30% of TOAST tables need vacuum maintenance'
            WHEN tables_high_xid > 0 THEN 'WARNING: Monitor XID age and plan vacuum maintenance'
            ELSE 'TOAST tables are healthy'
        END as recommendation
    FROM toast_summary

    UNION ALL

    -- Critical TOAST Tables
    SELECT
        'Critical TOAST Tables' as metric,
        COUNT(*)::text as value,
        CASE
            WHEN COUNT(*) FILTER (WHERE main_table_age > (SELECT max_age * 0.8 FROM critical_age_info)) > 0 THEN 'CRITICAL'
            WHEN COUNT(*) FILTER (WHERE n_dead_tup > 50000) > 0 THEN 'WARNING'
            ELSE 'OK'
        END as status,
        'Tables requiring immediate attention' as description,
        COALESCE((
            SELECT string_agg(
                format('%s.%s - Age: %s, Dead (est): %s, TOAST: %s',
                    schema_name,
                    table_name,
                    main_table_age,
                    n_dead_tup,
                    pg_size_pretty(toast_table_size)),
                E'\n')
            FROM (
                SELECT schema_name, table_name, main_table_age, n_dead_tup, toast_table_size
                FROM toast_info
                WHERE main_table_age > (SELECT max_age * 0.5 FROM critical_age_info)
                   OR n_dead_tup > 10000
                ORDER BY main_table_age DESC, n_dead_tup DESC
                LIMIT 10
            ) t
        ), 'No critical tables found') as details,
        CASE
            WHEN COUNT(*) FILTER (WHERE main_table_age > (SELECT max_age * 0.8 FROM critical_age_info)) > 0
                THEN 'CRITICAL: Run VACUUM FREEZE immediately on high-age tables'
            WHEN COUNT(*) FILTER (WHERE n_dead_tup > 50000) > 0
                THEN 'WARNING: Schedule VACUUM for tables with high dead tuple count'
            ELSE 'No immediate action required'
        END as recommendation
    FROM toast_info

    UNION ALL

    -- TOAST Size Analysis
    SELECT
        'TOAST Size Analysis' as metric,
        COUNT(*)::text || ' tables analyzed' as value,
        CASE
            WHEN COUNT(*) FILTER (WHERE toast_table_size > main_table_size) > 0 THEN 'INFO'
            ELSE 'OK'
        END as status,
        'TOAST storage efficiency analysis' as description,
        COALESCE((
            SELECT string_agg(
                format('%s.%s - TOAST: %s (%s%% of main table)',
                    schema_name,
                    table_name,
                    pg_size_pretty(toast_table_size),
                    ROUND((toast_table_size::numeric * 100 / NULLIF(main_table_size, 0)), 1)),
                E'\n')
            FROM (
                SELECT schema_name, table_name, toast_table_size, main_table_size
                FROM toast_info
                ORDER BY toast_table_size DESC
                LIMIT 10
            ) t
        ), 'No TOAST tables found') as details,
        CASE
            WHEN COUNT(*) FILTER (WHERE toast_table_size > main_table_size * 2) > 0
                THEN 'INFO: Some tables have very large TOAST storage - consider schema optimization'
            ELSE 'TOAST storage ratios are reasonable'
        END as recommendation
    FROM toast_info

    UNION ALL

    -- Vacuum Efficiency
    SELECT
        'Vacuum Efficiency' as metric,
        COUNT(*)::text || ' tables monitored' as value,
        CASE
            WHEN COUNT(*) FILTER (WHERE seconds_since_last_vacuum > 604800 AND n_dead_tup > 1000) > 0 THEN 'WARNING'
            WHEN COUNT(*) FILTER (WHERE seconds_since_last_vacuum IS NULL) > 0 THEN 'INFO'
            ELSE 'OK'
        END as status,
        'Vacuum frequency and effectiveness for TOAST tables' as description,
        COALESCE((
            SELECT string_agg(
                format('%s.%s - Dead (est): %s, Last vacuum: %s, Manual: %s, Auto: %s',
                    schema_name,
                    table_name,
                    n_dead_tup,
                    CASE
                        WHEN seconds_since_last_vacuum IS NULL THEN 'never'
                        ELSE format('%s days ago', (seconds_since_last_vacuum/86400)::int)
                    END,
                    vacuum_count,
                    autovacuum_count),
                E'\n')
            FROM (
                SELECT schema_name, table_name, n_dead_tup, seconds_since_last_vacuum,
                       vacuum_count, autovacuum_count
                FROM toast_info
                ORDER BY n_dead_tup DESC, seconds_since_last_vacuum DESC NULLS FIRST
                LIMIT 10
            ) t
        ), 'No vacuum information available') as details,
        CASE
            WHEN COUNT(*) FILTER (WHERE seconds_since_last_vacuum > 604800 AND n_dead_tup > 1000) > 0
                THEN 'WARNING: Some tables have not been vacuumed recently despite dead tuples'
            WHEN COUNT(*) FILTER (WHERE seconds_since_last_vacuum IS NULL) > 0
                THEN 'INFO: Some tables have never been vacuumed - monitor for growth'
            ELSE 'Vacuum frequency is appropriate'
        END as recommendation
    FROM toast_info
) metrics
ORDER BY
    CASE status
        WHEN 'CRITICAL' THEN 1
        WHEN 'WARNING' THEN 2
        WHEN 'INFO' THEN 3
        ELSE 4
    END,
    CASE metric
        WHEN 'Extension Notice' THEN 0
        WHEN 'TOAST Health Overview' THEN 1
        WHEN 'Critical TOAST Tables' THEN 2
        WHEN 'TOAST Size Analysis' THEN 3
        WHEN 'Vacuum Efficiency' THEN 4
        ELSE 5
    END;
"
fi

# Execute the appropriate TOAST query
execute_sql_to_html "TOAST Tables Status" "$TOAST_QUERY"

execute_sql_to_html "Large Table Partitioning Analysis" "
WITH RECURSIVE 
version_check AS (
    SELECT current_setting('server_version_num')::integer as version_num
),
table_sizes AS (
    SELECT 
        n.nspname as schema_name,
        c.relname as table_name,
        c.relkind,
        pg_total_relation_size(c.oid) as total_size,
        pg_size_pretty(pg_total_relation_size(c.oid)) as formatted_size,
        (SELECT version_num FROM version_check) as pg_version
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
    AND c.relkind IN ('r', 'p')
    AND pg_total_relation_size(c.oid) > 1073741824  -- Tables larger than 1GB
),
partition_details AS (
    SELECT 
        n.nspname as schema_name,
        c.relname as table_name,
        CASE 
            WHEN EXISTS (
                SELECT 1 FROM version_check WHERE version_num >= 100000
            ) THEN 
                CASE 
                    WHEN c.relkind = 'p' THEN 'PARTITIONED'
                    WHEN c.relispartition THEN 'PARTITION'
                    ELSE 'REGULAR'
                END
            ELSE 'REGULAR'
        END as table_type,
        count(i.inhrelid) as partition_count
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    LEFT JOIN pg_inherits i ON c.oid = i.inhparent
    WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
    GROUP BY n.nspname, c.relname, c.relkind, c.relispartition
),
table_analysis AS (
    SELECT 
        ts.schema_name,
        ts.table_name,
        ts.formatted_size,
        ROUND(ts.total_size::numeric / 1073741824.0, 2) as size_gb,
        COALESCE(pd.table_type, 'REGULAR') as table_type,
        COALESCE(pd.partition_count, 0) as partition_count,
        ts.total_size,
        ts.pg_version
    FROM table_sizes ts
    LEFT JOIN partition_details pd 
        ON ts.schema_name = pd.schema_name 
        AND ts.table_name = pd.table_name
)
SELECT 
    schema_name,
    table_name,
    formatted_size as \"Total Size\",
    size_gb as \"Size in GB\",
    table_type as \"Table Type\",
    partition_count as \"Partition Count\",
    CASE 
        WHEN table_type = 'REGULAR' AND total_size > 53687091200 THEN 
            'HIGH PRIORITY: Consider partitioning (table > 50GB)'
        WHEN table_type = 'REGULAR' AND total_size > 21474836480 THEN 
            'MEDIUM PRIORITY: Consider partitioning (table > 20GB)'
        WHEN table_type = 'REGULAR' AND total_size > 10737418240 THEN 
            'LOW PRIORITY: Evaluate partitioning needs (table > 10GB)'
        WHEN table_type = 'PARTITIONED' AND partition_count < 2 THEN
            'WARNING: Partitioned table with insufficient partitions'
        WHEN table_type = 'PARTITIONED' THEN
            'OK: Table is properly partitioned'
        ELSE 'INFO: Monitor growth patterns'
    END as \"Recommendation\",
    CASE 
        WHEN pg_version >= 100000 THEN
            CASE table_type
                WHEN 'REGULAR' THEN
                    'Possible partition keys:'
                    || E'\\n- Date/Timestamp columns (for time-based partitioning)'
                    || E'\\n- ID or Serial columns (for range partitioning)'
                    || E'\\n- Category or Status columns (for list partitioning)'
                WHEN 'PARTITIONED' THEN
                    'Maintenance tasks:'
                    || E'\\n- Monitor partition count and size'
                    || E'\\n- Plan for partition rotation'
                    || E'\\n- Check partition pruning efficiency'
                ELSE ''
            END
        ELSE 'Partitioning requires PostgreSQL 10 or later'
    END as \"Additional Info\",
    CASE 
        WHEN table_type = 'REGULAR' AND total_size > 53687091200 THEN 'CRITICAL'
        WHEN table_type = 'REGULAR' AND total_size > 21474836480 THEN 'WARNING'
        WHEN table_type = 'PARTITIONED' AND partition_count < 2 THEN 'WARNING'
        WHEN table_type = 'PARTITIONED' THEN 'OK'
        ELSE 'INFO'
    END as \"Status\"
FROM table_analysis
ORDER BY 
    CASE 
        WHEN table_type = 'REGULAR' AND total_size > 53687091200 THEN 1
        WHEN table_type = 'REGULAR' AND total_size > 21474836480 THEN 2
        WHEN table_type = 'REGULAR' AND total_size > 10737418240 THEN 3
        WHEN table_type = 'PARTITIONED' AND partition_count < 2 THEN 4
        ELSE 5
    END,
    total_size DESC;"

# Generate and execute Table Bloat Analysis query using BLOAT_METHOD variable
BLOAT_SQL=$(generate_bloat_query "$BLOAT_METHOD" "$PG_VERSION")

# Create section with appropriate badge based on analysis method
SUBSECTION_ID="subsection_$(date +%s%N | cut -b1-13)_$(shuf -i 1000-9999 -n 1)"

# Generate badge HTML based on analysis method
if [ "$BLOAT_METHOD" = "pgstattuple" ]; then
    BLOAT_BADGE=$(render_limitation_badge "pgstattuple" "Using pgstattuple extension for accurate bloat measurements")
    BLOAT_NOTICE_CLASS="fallback-notice"
    BLOAT_NOTICE_ICON="🔍"
    BLOAT_NOTICE_TITLE="Accurate Analysis"
    BLOAT_NOTICE_TEXT="Using pgstattuple extension for precise bloat measurements"
else
    BLOAT_BADGE=$(render_limitation_badge "stats-based" "Using pg_stat_user_tables statistics for fast bloat estimation")
    BLOAT_NOTICE_CLASS="limitation-notice"
    BLOAT_NOTICE_ICON="📊"
    BLOAT_NOTICE_TITLE="Stats-Based Analysis"
    BLOAT_NOTICE_TEXT="Using pg_stat_user_tables statistics for bloat estimation (fast method)"
fi

cat >> "$OUTPUT_FILE" << EOF
<div class='table-container'>
<input type='checkbox' id='$SUBSECTION_ID' class='accordion-checkbox'>
<label for='$SUBSECTION_ID' class='accordion-label'>
<h3>Table Bloat Analysis $BLOAT_BADGE</h3>
</label>
<div class='accordion-content'>
<div class='$BLOAT_NOTICE_CLASS'>
<strong>$BLOAT_NOTICE_ICON $BLOAT_NOTICE_TITLE:</strong> $BLOAT_NOTICE_TEXT
</div>
EOF

# Execute the generated query
TEMP_STDOUT=$(mktemp)
TEMP_STDERR=$(mktemp)

PGPASSWORD="$PGPASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -c "$BLOAT_SQL" > "$TEMP_STDOUT" 2> "$TEMP_STDERR"
EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    # Query succeeded - display results
    echo "<pre>" >> "$OUTPUT_FILE"
    cat "$TEMP_STDOUT" >> "$OUTPUT_FILE"
    echo "</pre>" >> "$OUTPUT_FILE"
else
    # Query failed - capture and display error
    ERROR_MSG=$(cat "$TEMP_STDERR")
    capture_pg_error "Table Bloat Analysis" "$BLOAT_SQL" "$ERROR_MSG"
fi

# Clean up temporary files
rm -f "$TEMP_STDOUT" "$TEMP_STDERR"

cat >> "$OUTPUT_FILE" << 'EOF'
</div>
</div>
EOF


# 8.1 Unused Indexes (REQ-8.1)
execute_sql_to_html "Unused Indexes" "
SELECT
    s.schemaname AS schema_name,
    s.relname AS table_name,
    s.indexrelname AS index_name,
    pg_size_pretty(pg_relation_size(s.indexrelid)) AS index_size,
    s.idx_scan AS index_scans,
    s.idx_tup_read AS tuples_read,
    s.idx_tup_fetch AS tuples_fetched,
    CASE
        WHEN pg_relation_size(s.indexrelid) > 104857600 THEN 'HIGH PRIORITY - Unused index > 100MB, consider dropping'
        WHEN pg_relation_size(s.indexrelid) > 10485760 THEN 'MEDIUM PRIORITY - Unused index > 10MB, consider dropping'
        ELSE 'LOW PRIORITY - Small unused index, consider dropping'
    END AS recommendation
FROM pg_stat_user_indexes s
JOIN pg_indexes i ON s.schemaname = i.schemaname
    AND s.relname = i.tablename
    AND s.indexrelname = i.indexname
WHERE s.idx_scan = 0
    AND s.indexrelname NOT LIKE '%_pkey'
    AND s.schemaname NOT IN ('pg_catalog', 'information_schema')
ORDER BY pg_relation_size(s.indexrelid) DESC
LIMIT 200;"

# 8.2 Duplicate Indexes (REQ-8.2)
execute_sql_to_html "Duplicate Indexes" "
WITH index_info AS (
    SELECT
        n.nspname AS schema_name,
        ct.relname AS table_name,
        ci.relname AS index_name,
        i.indkey::text AS index_columns,
        pg_get_indexdef(i.indexrelid) AS index_definition,
        pg_size_pretty(pg_relation_size(i.indexrelid)) AS index_size,
        pg_relation_size(i.indexrelid) AS index_size_bytes,
        i.indisunique AS is_unique,
        i.indisprimary AS is_primary
    FROM pg_index i
    JOIN pg_class ci ON ci.oid = i.indexrelid
    JOIN pg_class ct ON ct.oid = i.indrelid
    JOIN pg_namespace n ON n.oid = ct.relnamespace
    WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
)
SELECT
    a.schema_name,
    a.table_name,
    a.index_name AS index_1,
    b.index_name AS index_2,
    a.index_columns AS shared_columns,
    a.index_size AS index_1_size,
    b.index_size AS index_2_size,
    CASE
        WHEN a.is_primary THEN 'Consider dropping ' || b.index_name || ' (duplicate of primary key)'
        WHEN b.is_primary THEN 'Consider dropping ' || a.index_name || ' (duplicate of primary key)'
        WHEN a.is_unique AND NOT b.is_unique THEN 'Consider dropping ' || b.index_name || ' (covered by unique index)'
        WHEN b.is_unique AND NOT a.is_unique THEN 'Consider dropping ' || a.index_name || ' (covered by unique index)'
        WHEN a.index_size_bytes >= b.index_size_bytes THEN 'Consider dropping ' || b.index_name || ' (smaller duplicate)'
        ELSE 'Consider dropping ' || a.index_name || ' (smaller duplicate)'
    END AS recommendation
FROM index_info a
JOIN index_info b ON a.schema_name = b.schema_name
    AND a.table_name = b.table_name
    AND a.index_columns = b.index_columns
    AND a.index_name < b.index_name
ORDER BY a.schema_name, a.table_name, a.index_name
LIMIT 200;"

# 8.3 Idle Connections (REQ-8.3)
execute_sql_to_html "Idle Connections" "
SELECT
    pid,
    usename AS username,
    datname AS database,
    COALESCE(NULLIF(application_name, ''), '<unnamed>') AS application_name,
    state,
    CASE
        WHEN state_change IS NOT NULL THEN
            EXTRACT(EPOCH FROM (now() - state_change))::integer || ' seconds'
        ELSE 'unknown'
    END AS idle_duration,
    COALESCE(client_addr::text, 'local') AS client_address,
    CASE
        WHEN state = 'idle' AND state_change < now() - interval '1 hour' THEN 'CRITICAL - Idle > 1 hour, strongly consider terminating'
        WHEN state = 'idle' AND state_change < now() - interval '15 minutes' THEN 'WARNING - Idle > 15 minutes, consider terminating'
        WHEN state = 'idle' AND state_change < now() - interval '5 minutes' THEN 'INFO - Idle > 5 minutes, monitor'
        ELSE 'OK - Recently active'
    END AS recommendation
FROM pg_stat_activity
WHERE state = 'idle'
    AND pid != pg_backend_pid()
ORDER BY state_change ASC NULLS FIRST
LIMIT 200;"

# 8.4 Oversized Data Types (REQ-8.4)
execute_sql_to_html "Oversized Data Types" "
SELECT
    table_schema AS schema_name,
    table_name,
    column_name,
    data_type ||
        CASE
            WHEN character_maximum_length IS NOT NULL THEN '(' || character_maximum_length || ')'
            WHEN numeric_precision IS NOT NULL AND data_type IN ('numeric', 'decimal') THEN '(' || numeric_precision || ',' || COALESCE(numeric_scale, 0) || ')'
            ELSE ''
        END AS current_type,
    CASE
        WHEN data_type = 'text' THEN 'Consider varchar(n) with appropriate length limit if max length is known'
        WHEN data_type = 'bigint' THEN 'Consider integer if values fit within -2147483648 to 2147483647 range'
        WHEN data_type IN ('numeric', 'decimal') AND numeric_precision IS NULL THEN 'Consider adding precision and scale to avoid arbitrary precision overhead'
        WHEN data_type = 'character varying' AND character_maximum_length IS NULL THEN 'Consider adding a length limit to varchar'
        WHEN data_type = 'double precision' THEN 'Consider real (4 bytes) if double precision (8 bytes) is not needed'
        WHEN data_type = 'timestamp with time zone' AND column_name LIKE '%date%' THEN 'Consider date type if time component is not needed'
        ELSE 'Review data type sizing'
    END AS recommendation
FROM information_schema.columns
WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
    AND (
        data_type = 'text'
        OR data_type = 'bigint'
        OR (data_type IN ('numeric', 'decimal') AND numeric_precision IS NULL)
        OR (data_type = 'character varying' AND character_maximum_length IS NULL)
        OR data_type = 'double precision'
        OR (data_type = 'timestamp with time zone' AND column_name LIKE '%date%')
    )
ORDER BY table_schema, table_name, ordinal_position
LIMIT 200;"

# 8.5 Unused Tables (REQ-8.5)
execute_sql_to_html "Unused Tables" "
SELECT
    schemaname AS schema_name,
    relname AS table_name,
    pg_size_pretty(pg_total_relation_size(relid)) AS total_size,
    n_live_tup AS estimated_row_count,
    COALESCE(last_vacuum::text, 'never') AS last_vacuum,
    COALESCE(last_autovacuum::text, 'never') AS last_autovacuum,
    COALESCE(last_analyze::text, 'never') AS last_analyze,
    COALESCE(last_autoanalyze::text, 'never') AS last_autoanalyze,
    seq_scan + idx_scan AS total_scans,
    CASE
        WHEN pg_total_relation_size(relid) > 104857600 THEN 'HIGH PRIORITY - Unused table > 100MB, consider archiving or dropping'
        WHEN pg_total_relation_size(relid) > 10485760 THEN 'MEDIUM PRIORITY - Unused table > 10MB, consider archiving or dropping'
        ELSE 'LOW PRIORITY - Small unused table, verify if still needed'
    END AS recommendation
FROM pg_stat_user_tables
WHERE seq_scan + idx_scan = 0
    AND schemaname NOT IN ('pg_catalog', 'information_schema')
ORDER BY pg_total_relation_size(relid) DESC
LIMIT 200;"


# 8.6 Foreign Keys Without Indexes (REQ-10.2)
execute_sql_to_html "Foreign Keys Without Indexes" "
SELECT
    c.conrelid::regclass AS table_name,
    c.conname AS constraint_name,
    pg_get_constraintdef(c.oid) AS constraint_definition,
    'CREATE INDEX ON ' || c.conrelid::regclass || ' (' ||
    string_agg(a.attname, ', ' ORDER BY x.n) || ');' AS recommended_index,
    CASE
        WHEN pg_total_relation_size(c.conrelid) > 1073741824 THEN 'CRITICAL: Large table without FK index'
        WHEN pg_total_relation_size(c.conrelid) > 104857600 THEN 'WARNING: Medium table without FK index'
        ELSE 'INFO: Small table without FK index'
    END as severity
FROM pg_constraint c
CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS x(attnum, n)
JOIN pg_attribute a ON a.attnum = x.attnum AND a.attrelid = c.conrelid
WHERE c.contype = 'f'
  AND NOT EXISTS (
      SELECT 1 FROM pg_index i
      WHERE i.indrelid = c.conrelid
        AND c.conkey::int[] <@ i.indkey::int[]
  )
GROUP BY c.conrelid, c.conname, c.oid
ORDER BY pg_total_relation_size(c.conrelid) DESC
LIMIT 200;"

# 8.7 Trigger Analysis (REQ-10.10)
execute_sql_to_html "Trigger Analysis" "
SELECT
    tgrelid::regclass AS table_name,
    tgname AS trigger_name,
    CASE tgtype & 66
        WHEN 2 THEN 'BEFORE'
        WHEN 64 THEN 'INSTEAD OF'
        ELSE 'AFTER'
    END AS timing,
    CASE
        WHEN tgtype & 4 > 0 AND tgtype & 8 > 0 AND tgtype & 16 > 0 THEN 'INSERT/UPDATE/DELETE'
        WHEN tgtype & 4 > 0 AND tgtype & 8 > 0 THEN 'INSERT/UPDATE'
        WHEN tgtype & 4 > 0 AND tgtype & 16 > 0 THEN 'INSERT/DELETE'
        WHEN tgtype & 8 > 0 AND tgtype & 16 > 0 THEN 'UPDATE/DELETE'
        WHEN tgtype & 4 > 0 THEN 'INSERT'
        WHEN tgtype & 8 > 0 THEN 'UPDATE'
        WHEN tgtype & 16 > 0 THEN 'DELETE'
        WHEN tgtype & 32 > 0 THEN 'TRUNCATE'
        ELSE 'UNKNOWN'
    END AS event,
    CASE
        WHEN tgtype & 1 > 0 THEN 'FOR EACH ROW'
        ELSE 'FOR EACH STATEMENT'
    END AS granularity,
    p.proname AS function_name,
    CASE tgenabled
        WHEN 'O' THEN 'ENABLED (origin)'
        WHEN 'D' THEN 'DISABLED'
        WHEN 'R' THEN 'ENABLED (replica)'
        WHEN 'A' THEN 'ENABLED (always)'
        ELSE 'ENABLED'
    END AS status,
    CASE
        WHEN tgenabled = 'D' THEN 'INFO: Trigger is disabled - verify if intentional'
        WHEN tgtype & 1 = 0 THEN 'INFO: Statement-level trigger'
        WHEN (tgtype & 4 > 0 AND tgtype & 8 > 0 AND tgtype & 16 > 0) THEN 'WARNING: Trigger fires on all DML events - may impact performance'
        WHEN (tgtype & 66) = 2 AND tgtype & 1 > 0 THEN 'WARNING: BEFORE ROW trigger - runs for every row, monitor performance'
        ELSE 'OK'
    END AS recommendation
FROM pg_trigger t
JOIN pg_proc p ON p.oid = t.tgfoid
WHERE NOT t.tgisinternal
ORDER BY tgrelid::regclass::text, tgname
LIMIT 200;"

# 8.x Sequential Scan Heavy Tables (REQ-11.8)
execute_sql_to_html "Sequential Scan Heavy Tables" "
SELECT
    schemaname || '.' || relname AS table_name,
    pg_size_pretty(pg_total_relation_size(relid)) AS table_size,
    seq_scan,
    idx_scan,
    seq_tup_read,
    CASE WHEN seq_scan > 0
        THEN ROUND(seq_tup_read::numeric / seq_scan, 0)
        ELSE 0
    END AS avg_rows_per_seq_scan,
    CASE
        WHEN pg_total_relation_size(relid) > 1073741824 AND COALESCE(idx_scan, 0) = 0 THEN 'CRITICAL'
        WHEN seq_scan > 10000 AND COALESCE(idx_scan, 0) < seq_scan * 0.01 THEN 'CRITICAL'
        WHEN seq_scan > 1000 AND COALESCE(idx_scan, 0) < seq_scan * 0.1 THEN 'WARNING'
        ELSE 'INFO'
    END AS severity,
    CASE
        WHEN pg_total_relation_size(relid) > 1073741824 AND COALESCE(idx_scan, 0) = 0
            THEN 'CRITICAL: Large table (' || pg_size_pretty(pg_total_relation_size(relid)) || ') with zero index scans - add appropriate indexes immediately'
        WHEN seq_scan > 10000 AND COALESCE(idx_scan, 0) < seq_scan * 0.01
            THEN 'High sequential scan ratio - analyze query patterns and add covering indexes'
        WHEN seq_scan > 1000 AND COALESCE(idx_scan, 0) < seq_scan * 0.1
            THEN 'Sequential scans dominate - review WHERE clauses and consider adding indexes'
        ELSE 'Monitor sequential scan growth'
    END AS recommendation
FROM pg_stat_user_tables
WHERE pg_total_relation_size(relid) > 10485760
    AND seq_scan > 1000
    AND COALESCE(idx_scan, 0) < seq_scan * 0.1
ORDER BY seq_tup_read DESC
LIMIT 50;"

# Add general recommendations for Optimization Opportunities monitoring
cat >> "$OUTPUT_FILE" << 'EOF'
<div class="table-container">
<h3 style="font-size: 0.7em; margin: 0; padding: 0;">📋 General Recommendations for Optimization Opportunities</h3>
<div style="background: linear-gradient(135deg, #e8f4fd 0%, #f0f8ff 100%); padding: 15px; border-radius: 8px; border-left: 4px solid #3498db; margin: 15px 0; font-size: 0.7em; line-height: 1.3;">
    <div style="color: #2c3e50; margin: 0;"><strong>🔍 PostgreSQL Extensions & Tools:</strong><br>
    <strong><a href="https://www.postgresql.org/docs/current/monitoring-stats.html#MONITORING-PG-STAT-ALL-INDEXES-VIEW" target="_blank" style="color: #3498db; text-decoration: none;">pg_stat_user_indexes</a></strong> and <strong><a href="https://github.com/pgexperts/pgx_scripts" target="_blank" style="color: #3498db; text-decoration: none;">pg_unused_indexes</a></strong>: Identifying unused and inefficient indexes to drop or optimize | <strong><a href="https://github.com/reorg/pg_repack" target="_blank" style="color: #3498db; text-decoration: none;">pg_repack</a></strong>: Online table reorganization to eliminate bloat without downtime | <strong><a href="https://www.postgresql.org/docs/current/pgstattuple.html" target="_blank" style="color: #3498db; text-decoration: none;">pgstattuple</a></strong>: Detailed bloat analysis and <strong><a href="https://github.com/pgpartman/pg_partman" target="_blank" style="color: #3498db; text-decoration: none;">pg_partman</a></strong>: Automated partition management | <strong><a href="https://www.postgresql.org/docs/current/storage-toast.html" target="_blank" style="color: #3498db; text-decoration: none;">pg_toast</a></strong>: Large object storage optimization and <strong><a href="https://github.com/cybertec-postgresql/pg_squeeze" target="_blank" style="color: #3498db; text-decoration: none;">pg_squeeze</a></strong>: Online table defragmentation</div>
</div>
</div>
EOF

echo '</div>' >> "$OUTPUT_FILE"
echo '</div>' >> "$OUTPUT_FILE"

# 9. HEALTH SUMMARY
echo ""
echo "=============================================================================="
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting Section 9: HEALTH SUMMARY"
echo "=============================================================================="
echo '<div class="section">' >> "$OUTPUT_FILE"
echo '<input type="checkbox" id="section9-toggle" class="accordion-checkbox">' >> "$OUTPUT_FILE"
echo '<label for="section9-toggle" class="accordion-label section-header" id="section9"><h2>9. HEALTH SUMMARY (Findings & Actions)</h2></label>' >> "$OUTPUT_FILE"
echo '<div class="accordion-content">' >> "$OUTPUT_FILE"

execute_sql_to_html "Core Health Metrics" "
WITH health_metrics AS (
    SELECT
        ROUND((COUNT(*)::numeric / GREATEST(current_setting('max_connections')::integer, 1) * 100), 1) as conn_usage_pct,
        current_setting('max_connections')::integer as max_connections,
        COUNT(*) as current_connections
    FROM pg_stat_activity
    WHERE pid != pg_backend_pid()
),
xid_metrics AS (
    SELECT
        ROUND((age(datfrozenxid)::numeric / GREATEST(current_setting('autovacuum_freeze_max_age')::numeric, 1) * 100), 1) as xid_age_pct,
        age(datfrozenxid) as xid_age
    FROM pg_database
    WHERE datname = current_database()
),
cache_metrics AS (
    SELECT
        CASE
            WHEN blks_hit + blks_read = 0 THEN 100.0
            ELSE ROUND((blks_hit::numeric / GREATEST(blks_hit + blks_read, 1) * 100), 1)
        END as cache_hit_ratio,
        blks_hit as total_hits,
        blks_read as total_reads
    FROM pg_stat_database
    WHERE datname = current_database()
),
query_metrics AS (
    SELECT
        COUNT(*) FILTER (WHERE state = 'active' AND query_start IS NOT NULL AND EXTRACT(EPOCH FROM (now() - query_start)) > 300) as long_queries,
        COUNT(*) FILTER (WHERE state = 'active') as active_queries,
        COUNT(*) FILTER (WHERE state = 'idle in transaction' AND state_change IS NOT NULL AND EXTRACT(EPOCH FROM (now() - state_change)) > 300) as long_idle_in_transaction,
        COUNT(*) FILTER (WHERE wait_event_type IS NOT NULL AND wait_event_type != 'Activity') as waiting_queries
    FROM pg_stat_activity
    WHERE pid != pg_backend_pid()
),
vacuum_metrics AS (
    SELECT
        COUNT(*) FILTER (WHERE
            pg_total_relation_size(relid) > 1048576 AND
            n_live_tup + n_dead_tup > 0 AND 
            n_dead_tup::numeric / (n_live_tup + n_dead_tup) > 0.2
        ) as tables_needing_vacuum,
        COUNT(*) as total_tables,
        COUNT(*) FILTER (WHERE last_vacuum IS NULL AND last_autovacuum IS NULL) as never_vacuumed_tables
    FROM pg_stat_user_tables
),
lock_metrics AS (
    SELECT
        COUNT(*) FILTER (WHERE NOT granted) as blocked_queries,
        COUNT(DISTINCT relation) FILTER (WHERE mode LIKE '%ExclusiveLock' AND NOT granted) as locked_tables
    FROM pg_locks l
    LEFT JOIN pg_stat_activity a ON l.pid = a.pid
    WHERE a.pid != pg_backend_pid()
)
SELECT
    check_category,
    current_value,
    status,
    recommendation
FROM (
    SELECT
        'Connection Usage' as check_category,
        conn_usage_pct::text || '% (' || current_connections || '/' || max_connections || ')' as current_value,
        CASE
            WHEN conn_usage_pct > 85 THEN 'CRITICAL'
            WHEN conn_usage_pct > 70 THEN 'WARNING'
            WHEN conn_usage_pct > 50 THEN 'INFO'
            ELSE 'OK'
        END as status,
        CASE
            WHEN conn_usage_pct > 85 THEN 'URGENT: Implement connection pooling immediately - risk of connection exhaustion'
            WHEN conn_usage_pct > 70 THEN 'Consider connection pooling to prevent issues'
            WHEN conn_usage_pct > 50 THEN 'Monitor connection usage trends'
            ELSE 'Connection usage is healthy'
        END as recommendation,
        CASE
            WHEN conn_usage_pct > 85 THEN 1
            WHEN conn_usage_pct > 70 THEN 2
            WHEN conn_usage_pct > 50 THEN 3
            ELSE 4
        END as priority
    FROM health_metrics

    UNION ALL

    SELECT
        'Transaction ID Age',
        COALESCE(xid_age_pct::text || '% (' || xid_age::text || ' transactions)', 'Unable to calculate'),
        CASE
            WHEN xid_age_pct IS NULL THEN 'UNKNOWN'
            WHEN xid_age_pct > 80 THEN 'EMERGENCY'
            WHEN xid_age_pct > 50 THEN 'CRITICAL'
            WHEN xid_age_pct > 25 THEN 'WARNING'
            WHEN xid_age_pct > 15 THEN 'INFO'
            ELSE 'OK'
        END,
        CASE
            WHEN xid_age_pct IS NULL THEN 'Unable to determine transaction ID age'
            WHEN xid_age_pct > 80 THEN 'EMERGENCY: XID wraparound imminent - immediate VACUUM FREEZE required'
            WHEN xid_age_pct > 50 THEN 'CRITICAL: Schedule immediate VACUUM FREEZE to prevent wraparound'
            WHEN xid_age_pct > 25 THEN 'WARNING: Schedule VACUUM FREEZE maintenance window'
            WHEN xid_age_pct > 15 THEN 'INFO: Plan maintenance window for VACUUM'
            ELSE 'Transaction ID age is healthy'
        END,
        CASE
            WHEN xid_age_pct IS NULL THEN 3
            WHEN xid_age_pct > 80 THEN 0
            WHEN xid_age_pct > 50 THEN 1
            WHEN xid_age_pct > 25 THEN 1
            WHEN xid_age_pct > 15 THEN 2
            ELSE 4
        END
    FROM xid_metrics

    UNION ALL

    SELECT
        'Cache Hit Ratio',
        cache_hit_ratio::text || '% (hits: ' || COALESCE(total_hits::text, '0') || ', reads: ' || COALESCE(total_reads::text, '0') || ')',
        CASE
            WHEN total_hits + total_reads = 0 THEN 'INFO'
            WHEN cache_hit_ratio < 85 THEN 'WARNING'
            WHEN cache_hit_ratio < 95 THEN 'INFO'
            ELSE 'OK'
        END,
        CASE
            WHEN total_hits + total_reads = 0 THEN 'No database activity detected yet'
            WHEN cache_hit_ratio < 85 THEN 'Poor cache performance - increase shared_buffers or investigate I/O bottlenecks'
            WHEN cache_hit_ratio < 95 THEN 'Consider memory optimization or workload analysis'
            ELSE 'Cache performance is optimal'
        END,
        CASE
            WHEN total_hits + total_reads = 0 THEN 4
            WHEN cache_hit_ratio < 85 THEN 2
            WHEN cache_hit_ratio < 95 THEN 3
            ELSE 4
        END
    FROM cache_metrics

    UNION ALL

    SELECT
        'Query Performance',
        long_queries::text || ' long queries (>5min), ' || active_queries::text || ' active, ' ||
        waiting_queries::text || ' waiting',
        CASE
            WHEN long_queries > 5 THEN 'CRITICAL'
            WHEN long_queries > 0 OR waiting_queries > 3 THEN 'WARNING'
            WHEN waiting_queries > 0 THEN 'INFO'
            ELSE 'OK'
        END,
        CASE
            WHEN long_queries > 5 THEN 'Multiple long-running queries detected - investigate and optimize immediately'
            WHEN long_queries > 0 THEN 'Long-running queries detected - review and optimize'
            WHEN waiting_queries > 3 THEN 'Multiple queries waiting - check for lock contention'
            WHEN waiting_queries > 0 THEN 'Some queries waiting - monitor for patterns'
            ELSE 'Query performance is healthy'
        END,
        CASE
            WHEN long_queries > 5 THEN 1
            WHEN long_queries > 0 OR waiting_queries > 3 THEN 2
            WHEN waiting_queries > 0 THEN 3
            ELSE 4
        END
    FROM query_metrics

    UNION ALL

    SELECT
        'Vacuum Status',
        tables_needing_vacuum::text || ' of ' || total_tables::text || ' tables need vacuum (>20% dead tuples)',
        CASE
            WHEN total_tables = 0 THEN 'INFO'
            WHEN tables_needing_vacuum::numeric / GREATEST(total_tables, 1) > 0.5 THEN 'CRITICAL'
            WHEN tables_needing_vacuum > 0 THEN 'WARNING'
            ELSE 'OK'
        END,
        CASE
            WHEN total_tables = 0 THEN 'No user tables found'
            WHEN tables_needing_vacuum::numeric / GREATEST(total_tables, 1) > 0.5 THEN 'Over half of tables need vacuum - review autovacuum settings'
            WHEN tables_needing_vacuum > 0 THEN 'Schedule VACUUM for tables with high dead tuple ratio'
            ELSE 'All tables have healthy tuple ratios'
        END,
        CASE
            WHEN total_tables = 0 THEN 4
            WHEN tables_needing_vacuum::numeric / GREATEST(total_tables, 1) > 0.5 THEN 1
            WHEN tables_needing_vacuum > 0 THEN 2
            ELSE 4
        END
    FROM vacuum_metrics

    UNION ALL

    SELECT
        'Lock Contention',
        blocked_queries::text || ' blocked queries, ' || locked_tables::text || ' locked tables',
        CASE
            WHEN blocked_queries > 5 THEN 'CRITICAL'
            WHEN blocked_queries > 0 OR locked_tables > 0 THEN 'WARNING'
            ELSE 'OK'
        END,
        CASE
            WHEN blocked_queries > 5 THEN 'Severe lock contention - investigate blocking queries immediately'
            WHEN blocked_queries > 0 THEN 'Lock contention detected - review concurrent operations'
            WHEN locked_tables > 0 THEN 'Tables locked - monitor for long-running maintenance'
            ELSE 'No significant lock contention'
        END,
        CASE
            WHEN blocked_queries > 5 THEN 1
            WHEN blocked_queries > 0 OR locked_tables > 0 THEN 2
            ELSE 4
        END
    FROM lock_metrics
) health_summary
ORDER BY priority, check_category;"

execute_sql_to_html "Critical System Metrics" "
WITH disk_space_metrics AS (
    SELECT
        pg_database_size(current_database()) as current_db_size,
        (SELECT sum(pg_tablespace_size(spcname)) FROM pg_tablespace) as total_tablespace_size
),
tablespace_metrics AS (
    SELECT 
        spcname,
        pg_tablespace_size(spcname) as size,
        CASE 
            WHEN spcname = 'pg_default' THEN 
                (SELECT CAST(SUBSTRING(setting FROM '[0-9]+') AS numeric) * 1024.0 * 8192.0
                 FROM pg_settings WHERE name = 'max_files_per_process')
            ELSE NULL
        END as estimated_max_size
    FROM pg_tablespace
),
lock_contention AS (
    SELECT 
        count(*) as waiting_session_count,
        max(COALESCE(EXTRACT(EPOCH FROM (now() - a.query_start)), 0)) as max_wait_time_seconds,
        COALESCE(l.relation::regclass::text, 'transaction') as locked_table
    FROM pg_locks l
    JOIN pg_stat_activity a ON l.pid = a.pid
    WHERE NOT l.granted
    GROUP BY l.relation
    ORDER BY count(*) DESC
    LIMIT 5
),
lock_summary AS (
    SELECT 
        COALESCE(SUM(waiting_session_count), 0) as total_waiting,
        COALESCE(MAX(max_wait_time_seconds), 0) as max_wait,
        COALESCE(
            (SELECT locked_table FROM lock_contention ORDER BY waiting_session_count DESC LIMIT 1),
            'N/A'
        ) as top_locked_table
    FROM lock_contention
)
SELECT 
    check_category,
    current_value,
    status,
    recommendation
FROM (
    -- Disk Space Metrics
    SELECT
        'Database Size' as check_category,
        pg_size_pretty(current_db_size) || ' (Current DB)' as current_value,
        CASE
            WHEN current_db_size::numeric > (100 * power(1024, 3))::numeric THEN 'WARNING'
            ELSE 'INFO'
        END as status,
        CASE
            WHEN current_db_size::numeric > (100 * power(1024, 3))::numeric THEN 'Large database - ensure proper backup strategy and monitoring'
            ELSE 'Database size is within normal range'
        END as recommendation
    FROM disk_space_metrics

    UNION ALL

    SELECT
        'Tablespace Usage',
        spcname || ': ' || pg_size_pretty(size) || 
        CASE 
            WHEN estimated_max_size IS NOT NULL THEN ' of ' || pg_size_pretty(estimated_max_size) || 
                 ' (' || ROUND(100.0 * size::numeric / NULLIF(estimated_max_size::numeric, 0), 1) || '%)'
            ELSE ''
        END,
        CASE
            WHEN estimated_max_size IS NOT NULL AND size::numeric > estimated_max_size::numeric * 0.85 THEN 'CRITICAL'
            WHEN estimated_max_size IS NOT NULL AND size::numeric > estimated_max_size::numeric * 0.7 THEN 'WARNING'
            ELSE 'INFO'
        END,
        CASE
            WHEN estimated_max_size IS NOT NULL AND size::numeric > estimated_max_size::numeric * 0.85 THEN 'Tablespace nearly full - urgent action required'
            WHEN estimated_max_size IS NOT NULL AND size::numeric > estimated_max_size::numeric * 0.7 THEN 'Tablespace filling up - plan for expansion'
            ELSE 'Tablespace has adequate free space'
        END
    FROM tablespace_metrics

    UNION ALL

    SELECT
        'Lock Contention',
        CASE 
            WHEN total_waiting > 0 THEN 
                total_waiting::text || ' sessions waiting for ' || top_locked_table || 
                ' (max wait: ' || ROUND((max_wait/60)::numeric, 1) || ' min)'
            ELSE 'No lock contention detected'
        END,
        CASE
            WHEN total_waiting > 3 THEN 'CRITICAL'
            WHEN total_waiting > 0 THEN 'WARNING'
            ELSE 'OK'
        END,
        CASE
            WHEN total_waiting > 3 THEN 'Severe lock contention on ' || top_locked_table || ' - investigate immediately'
            WHEN total_waiting > 0 THEN 'Lock contention on ' || top_locked_table || ' - review concurrent transactions'
            ELSE 'No lock contention issues'
        END
    FROM lock_summary
) critical_metrics
ORDER BY 
    CASE status 
        WHEN 'CRITICAL' THEN 1 
        WHEN 'WARNING' THEN 2 
        WHEN 'INFO' THEN 3 
        ELSE 4 
    END,
    check_category;"

execute_sql_with_extension_check "Maintenance & Optimization Status" "
WITH largest_objects AS (
    (SELECT
        'table' as object_type,
        quote_ident(schemaname) || '.' || quote_ident(relname) as object_name,
        pg_total_relation_size(relid) as size,
        n_live_tup as live_tuples,
        n_dead_tup as dead_tuples
     FROM pg_stat_user_tables
     ORDER BY pg_total_relation_size(relid) DESC
     LIMIT 5)
    UNION ALL
    (SELECT
        'index' as object_type,
        quote_ident(schemaname) || '.' || quote_ident(relname) as object_name,
        pg_relation_size(relid) as size,
        NULL as live_tuples,
        NULL as dead_tuples
     FROM pg_stat_user_indexes
     ORDER BY pg_relation_size(relid) DESC
     LIMIT 5)
),
unused_indexes AS (
    SELECT
        quote_ident(i.schemaname) || '.' || quote_ident(i.relname) as index_name,
        quote_ident(i.schemaname) || '.' || quote_ident(t.relname) as table_name,
        pg_relation_size(i.indexrelid) as index_size,
        i.idx_scan as scans,
        EXTRACT(EPOCH FROM (now() - COALESCE(pg_stat_get_last_vacuum_time(t.relid), pg_stat_get_last_autovacuum_time(t.relid), now() - interval '365 days')))::bigint / 86400 as days_monitored
    FROM pg_stat_user_indexes i
    JOIN pg_stat_user_tables t ON i.relid = t.relid
    WHERE i.idx_scan = 0
    AND NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        WHERE c.conindid = i.indexrelid AND c.contype IN ('p', 'u')
    )
    ORDER BY pg_relation_size(i.indexrelid) DESC
    LIMIT 5
),
table_bloat AS (
    SELECT
        sut.schemaname || '.' || sut.relname as table_name,
        pg_total_relation_size(sut.relid) as total_size,
        -- Stats-based bloat estimation using pg_stat_user_tables
        CASE
            WHEN sut.n_live_tup + sut.n_dead_tup > 0 THEN
                (pg_total_relation_size(sut.relid)::numeric * sut.n_dead_tup / (sut.n_live_tup + sut.n_dead_tup))::bigint
            ELSE 0
        END as bloat_size,
        CASE
            WHEN sut.n_live_tup + sut.n_dead_tup > 0
            THEN ROUND(100.0 * sut.n_dead_tup / (sut.n_live_tup + sut.n_dead_tup), 2)
            ELSE 0
        END as bloat_percentage,
        CASE
            WHEN sut.last_vacuum IS NOT NULL OR sut.last_autovacuum IS NOT NULL THEN
                ROUND(sut.n_dead_tup::numeric / NULLIF(EXTRACT(EPOCH FROM (now() - COALESCE(sut.last_vacuum, sut.last_autovacuum)))::numeric / 86400, 0), 0)
            ELSE 0
        END as dead_tuples_per_day,
        EXTRACT(EPOCH FROM (now() - COALESCE(sut.last_vacuum, sut.last_autovacuum, now())))::bigint / 86400 as days_since_vacuum
    FROM pg_stat_user_tables sut
    WHERE pg_total_relation_size(sut.relid) > 1048576 -- Only tables > 1MB
    ORDER BY
        CASE
            WHEN sut.n_live_tup + sut.n_dead_tup > 0 THEN
                (pg_total_relation_size(sut.relid)::numeric * sut.n_dead_tup / (sut.n_live_tup + sut.n_dead_tup))::bigint
            ELSE 0
        END DESC
    LIMIT 20
),
autovacuum_status AS (
    SELECT
        sut.schemaname || '.' || sut.relname as table_name,
        sut.n_dead_tup as dead_tuples,
        sut.n_live_tup as live_tuples,
        CASE
            WHEN sut.n_live_tup + sut.n_dead_tup > 0
            THEN ROUND(100.0 * sut.n_dead_tup / (sut.n_live_tup + sut.n_dead_tup), 2)
            ELSE 0
        END as dead_tuple_pct,
        EXTRACT(EPOCH FROM (now() - last_autovacuum))/3600 as hours_since_autovacuum,
        CASE
            WHEN sut.autovacuum_count > 0 THEN
                ROUND(EXTRACT(EPOCH FROM (now() - pg_postmaster_start_time()))::numeric / (sut.autovacuum_count * 86400), 1)
            ELSE NULL
        END as avg_days_between_vacuums
    FROM pg_stat_user_tables sut
    WHERE sut.n_dead_tup > 10000
    AND last_autovacuum IS NOT NULL
    ORDER BY sut.n_dead_tup DESC
    LIMIT 10
),
toast_oid_usage AS (
    SELECT
        n.nspname || '.' || c.relname as table_name,
        toast_n.nspname || '.' || toast_c.relname as toast_table_name,
        -- Convert OID to bigint first, then to numeric for calculation
        toast_c.oid::bigint as current_oid,
        4294967295::bigint as max_oid_limit,
        -- Calculate percentage used based on toast table's current OID
        ROUND((toast_c.oid::bigint)::numeric * 100.0 / 4294967295::numeric, 2) as oid_usage_pct,
        CASE
            WHEN t.n_tup_ins > 0 AND EXTRACT(EPOCH FROM (now() - pg_postmaster_start_time())) > 0 THEN
                ROUND(t.n_tup_ins::numeric / (EXTRACT(EPOCH FROM (now() - pg_postmaster_start_time()))::numeric / 86400), 0)
            ELSE 0
        END as estimated_daily_inserts
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_class toast_c ON toast_c.oid = c.reltoastrelid
    JOIN pg_namespace toast_n ON toast_n.oid = toast_c.relnamespace
    LEFT JOIN pg_stat_user_tables t ON t.relid = c.oid
    WHERE c.relkind = 'r'
    AND c.reltoastrelid != 0
    AND n.nspname NOT IN ('pg_catalog', 'information_schema')
    AND toast_c.oid::bigint > 100000  -- Only show tables with significant OID usage
    ORDER BY toast_c.oid::bigint DESC
    LIMIT 10
)
SELECT
    check_category,
    current_value,
    status,
    recommendation,
    confidence_level
FROM (
    -- LARGE OBJECTS
    SELECT
        'Large Objects' as check_category,
        (object_type || ': ' || object_name || ' (' || pg_size_pretty(size) || ')') as current_value,
        CASE
            WHEN size::numeric > (10 * 1024^3)::numeric THEN 'WARNING'
            ELSE 'INFO'
        END as status,
        CASE
            WHEN object_type = 'table' AND size::numeric > (50 * 1024^3)::numeric THEN 90
            WHEN object_type = 'table' AND size::numeric > (20 * 1024^3)::numeric THEN 70
            WHEN object_type = 'table' AND size::numeric > (10 * 1024^3)::numeric THEN 50
            WHEN object_type = 'index' AND size::numeric > (10 * 1024^3)::numeric THEN 60
            WHEN object_type = 'index' AND size::numeric > (5 * 1024^3)::numeric THEN 40
            ELSE 20
        END as risk_score,
        CASE
            WHEN object_type = 'table' AND size::numeric > (10 * 1024^3)::numeric THEN
                'This table (' || object_name || ') is very large (' || pg_size_pretty(size) || '). Like a massive filing cabinet, it takes longer to search through and uses significant storage space. Large tables can slow down queries and backups.'
            WHEN object_type = 'index' AND size::numeric > (5 * 1024^3)::numeric THEN
                'This index (' || object_name || ') is unusually large (' || pg_size_pretty(size) || '). Think of it as a very thick phone book - it helps find things but takes up a lot of space. Large indexes can slow down data modifications.'
            ELSE
                'This ' || object_type || ' (' || object_name || ') is large but within normal range for active databases.'
        END as plain_english_explanation,
        CASE
            WHEN size::numeric > (50 * 1024^3)::numeric THEN 'IMMEDIATE - Already impacting performance'
            WHEN size::numeric > (20 * 1024^3)::numeric THEN 'SHORT TERM - Will need attention within 1-3 months'
            WHEN size::numeric > (10 * 1024^3)::numeric THEN 'MEDIUM TERM - Plan for optimization within 6 months'
            ELSE 'LONG TERM - Monitor growth trends'
        END as time_to_impact,
        CASE
            WHEN object_type = 'table' AND size::numeric > (10 * 1024^3)::numeric THEN
                '1. Analyze table growth rate over last 30 days' || E'\\n' ||
                '2. Review if old data can be archived' || E'\\n' ||
                '3. Consider table partitioning by date/range' || E'\\n' ||
                '4. Evaluate if columns can be normalized' || E'\\n' ||
                '5. Schedule maintenance window for partitioning'
            WHEN object_type = 'index' AND size::numeric > (5 * 1024^3)::numeric THEN
                '1. Check if index is actually being used (pg_stat_user_indexes)' || E'\\n' ||
                '2. Review index columns - can it be optimized?' || E'\\n' ||
                '3. Consider partial indexes if only subset of data is queried' || E'\\n' ||
                '4. Evaluate if BRIN index could replace B-tree for large sequential data'
            ELSE
                '1. Monitor size growth monthly' || E'\\n' ||
                '2. Set up alerts for 20GB threshold' || E'\\n' ||
                '3. No immediate action required'
        END as next_steps,
        CASE
            WHEN object_type = 'table' AND size::numeric > (50 * 1024^3)::numeric THEN
                'URGENT: Implement table partitioning immediately. Current size (' || pg_size_pretty(size) || ') is causing performance issues.'
            WHEN object_type = 'table' AND size::numeric > (10 * 1024^3)::numeric THEN
                'Consider partitioning this large table to improve query performance and maintenance operations.'
            WHEN object_type = 'index' AND size::numeric > (5 * 1024^3)::numeric THEN
                'Review index strategy - consider partial indexes or alternative index types (BRIN for sequential data).'
            ELSE
                'Size is within normal range - continue monitoring.'
        END as recommendation,
        CASE
            WHEN size::numeric > (50 * 1024^3)::numeric THEN 'HIGH (95%) - Size clearly indicates action needed'
            WHEN size::numeric > (10 * 1024^3)::numeric THEN 'MEDIUM (75%) - Depends on growth rate and query patterns'
            ELSE 'LOW (50%) - Informational only'
        END as confidence_level
    FROM largest_objects

    UNION ALL

    -- UNUSED INDEXES
    SELECT
        'Unused Indexes' as check_category,
        (index_name || ' on ' || table_name || ' (' || pg_size_pretty(index_size) || ')') as current_value,
        CASE
            WHEN index_size::numeric > (100 * 1024^2)::numeric AND days_monitored > 30 THEN 'WARNING'
            WHEN index_size::numeric > (10 * 1024^2)::numeric AND days_monitored > 7 THEN 'INFO'
            ELSE 'INFO'
        END as status,
        CASE
            WHEN index_size::numeric > (1 * 1024^3)::numeric AND days_monitored > 90 THEN 85
            WHEN index_size::numeric > (500 * 1024^2)::numeric AND days_monitored > 60 THEN 70
            WHEN index_size::numeric > (100 * 1024^2)::numeric AND days_monitored > 30 THEN 55
            ELSE 30
        END as risk_score,
        'This index (' || index_name || ') has never been used in the last ' || days_monitored || ' days. ' ||
        'It''s like keeping an unused phone book on your desk - it takes up space (' || pg_size_pretty(index_size) || ') ' ||
        'and slows down every time you add, update, or delete data in the table. Removing it could speed up data modifications by 5-15%.' as plain_english_explanation,
        CASE
            WHEN days_monitored > 90 THEN 'NO IMPACT - Safe to remove after ' || days_monitored || ' days of monitoring'
            WHEN days_monitored > 30 THEN 'LOW RISK - Monitor for ' || (90 - days_monitored) || ' more days before removing'
            ELSE 'WAIT - Need ' || (30 - days_monitored) || ' more days of monitoring data'
        END as time_to_impact,
        CASE
            WHEN days_monitored > 90 THEN
                '1. Verify no application code references this index' || E'\\n' ||
                '2. Check if any scheduled jobs might use it' || E'\\n' ||
                '3. Create backup: pg_dump or document CREATE INDEX statement' || E'\\n' ||
                '4. Drop index: DROP INDEX CONCURRENTLY ' || index_name || ';' || E'\\n' ||
                '5. Monitor application for 48 hours' || E'\\n' ||
                '6. Measure write performance improvement'
            WHEN days_monitored > 30 THEN
                '1. Continue monitoring for ' || (90 - days_monitored) || ' more days' || E'\\n' ||
                '2. Review application code for index usage' || E'\\n' ||
                '3. Check query logs for index scans' || E'\\n' ||
                '4. Document findings before taking action'
            ELSE
                '1. Wait ' || (30 - days_monitored) || ' more days for sufficient monitoring data' || E'\\n' ||
                '2. Verify pg_stat_statements is enabled' || E'\\n' ||
                '3. Review if index was recently created'
        END as next_steps,
        CASE
            WHEN days_monitored > 90 AND index_size::numeric > (100 * 1024^2)::numeric THEN
                'RECOMMENDED: Drop this unused index. Confidence HIGH - ' || days_monitored || ' days of zero usage. Expected benefit: ~' || pg_size_pretty(index_size) || ' storage savings + faster writes.'
            WHEN days_monitored > 30 THEN
                'MONITOR: Continue tracking for ' || (90 - days_monitored) || ' more days before deciding.'
            ELSE
                'WAIT: Need more monitoring data (' || (30 - days_monitored) || ' days) before making decision.'
        END as recommendation,
        CASE
            WHEN days_monitored > 90 THEN 'HIGH (90%) - Sufficient monitoring period with zero usage'
            WHEN days_monitored > 60 THEN 'MEDIUM (70%) - Good monitoring data but recommend 90 days'
            WHEN days_monitored > 30 THEN 'MEDIUM (60%) - Minimum monitoring period met'
            ELSE 'LOW (30%) - Insufficient monitoring data'
        END as confidence_level
    FROM unused_indexes

    UNION ALL

    -- TABLE BLOAT
    SELECT
        'Table Bloat' as check_category,
        (table_name || ' (' || ROUND(bloat_percentage::numeric, 1) || '% bloat, ' || pg_size_pretty(bloat_size) || ' wasted)') as current_value,
        CASE
            WHEN bloat_percentage > 50 AND bloat_size > (500 * 1024^2) THEN 'CRITICAL'
            WHEN bloat_percentage > 30 AND bloat_size > (100 * 1024^2) THEN 'WARNING'
            WHEN bloat_percentage > 20 THEN 'INFO'
            ELSE 'OK'
        END as status,
        CASE
            WHEN bloat_percentage > 50 AND bloat_size > (1 * 1024^3) THEN 95
            WHEN bloat_percentage > 40 AND bloat_size > (500 * 1024^2) THEN 80
            WHEN bloat_percentage > 30 AND bloat_size > (100 * 1024^2) THEN 65
            WHEN bloat_percentage > 20 THEN 40
            ELSE 15
        END as risk_score,
        'Your table (' || table_name || ') has ' || ROUND(bloat_percentage::numeric, 1) || '% wasted space (' || pg_size_pretty(bloat_size) || '). ' ||
        'Imagine a filing cabinet where ' || ROUND(bloat_percentage::numeric, 1) || ' out of every 100 folders are empty but still taking up space. ' ||
        'This makes finding data slower because the database has to scan through all those empty spaces. ' ||
        CASE
            WHEN bloat_percentage > 30 THEN 'Queries on this table are likely 30-50% slower than they should be.'
            WHEN bloat_percentage > 20 THEN 'Queries are slightly slower (10-20%) due to bloat.'
            ELSE 'Minimal performance impact currently.'
        END as plain_english_explanation,
        CASE
            WHEN bloat_percentage > 50 THEN 'IMMEDIATE - Already causing significant performance degradation'
            WHEN bloat_percentage > 30 AND dead_tuples_per_day > 0 THEN
                'SHORT TERM - Will reach critical (50%) in ~' || ROUND((50 - bloat_percentage) / (dead_tuples_per_day::numeric * 100 / NULLIF(total_size / 8192, 0))) || ' days'
            WHEN bloat_percentage > 20 THEN 'MEDIUM TERM - Monitor closely, plan maintenance within 30-60 days'
            ELSE 'LONG TERM - Normal bloat levels'
        END as time_to_impact,
        CASE
            WHEN bloat_percentage > 50 AND bloat_size > (1 * 1024^3) THEN
                '1. URGENT: Schedule maintenance window (requires ' || ROUND((total_size::numeric / (1024^3))::numeric * 2) || '-' ||
                ROUND((total_size::numeric / (1024^3))::numeric * 3) || ' hours)' || E'\\n' ||
                '2. Choose method:' || E'\\n' ||
                '   a) VACUUM FULL (requires downtime, locks table)' || E'\\n' ||
                '   b) pg_repack (no downtime, needs 2x table space)' || E'\\n' ||
                '3. Ensure ' || pg_size_pretty(total_size) || ' free space available' || E'\\n' ||
                '4. Create backup before maintenance' || E'\\n' ||
                '5. Notify users of maintenance window' || E'\\n' ||
                '6. Monitor query performance after completion'
            WHEN bloat_percentage > 30 THEN
                '1. Schedule regular VACUUM during off-peak hours' || E'\\n' ||
                '2. Review autovacuum settings (may need tuning)' || E'\\n' ||
                '3. Plan for VACUUM FULL in next maintenance window' || E'\\n' ||
                '4. Monitor bloat growth weekly' || E'\\n' ||
                '5. Estimated time needed: ' || ROUND((total_size::numeric / (1024^3))::numeric * 1.5) || ' hours'
            ELSE
                '1. Continue regular autovacuum schedule' || E'\\n' ||
                '2. Monitor bloat percentage monthly' || E'\\n' ||
                '3. No immediate action required'
        END as next_steps,
        CASE
            WHEN bloat_percentage > 50 AND bloat_size > (500 * 1024^2) THEN
                'CRITICAL: Immediate VACUUM FULL or pg_repack required. Performance is significantly degraded. Expected improvement: ' || ROUND(bloat_percentage::numeric) || '% faster queries, ' || pg_size_pretty(bloat_size) || ' storage reclaimed.'
            WHEN bloat_percentage > 30 AND bloat_size > (100 * 1024^2) THEN
                'WARNING: Schedule VACUUM FULL during next maintenance window. Current bloat (' || ROUND(bloat_percentage::numeric, 1) || '%) is impacting query performance.'
            WHEN bloat_percentage > 20 THEN
                'INFO: Moderate bloat detected. Schedule regular VACUUM and monitor growth.'
            ELSE
                'OK: Bloat is within acceptable range (<20%).'
        END as recommendation,
        CASE
            WHEN '\$BLOAT_METHOD' = 'pgstattuple' THEN 'HIGH (95%) - Using pgstattuple for accurate measurement'
            WHEN bloat_percentage > 30 THEN 'MEDIUM (75%) - Stats-based estimation, actual bloat may vary ±10%'
            ELSE 'MEDIUM (70%) - Stats-based estimation'
        END as confidence_level
    FROM table_bloat

    UNION ALL

    -- AUTOVACUUM STATUS
    SELECT
        'Autovacuum Status' as check_category,
        (table_name || ' (' || ROUND(dead_tuple_pct::numeric, 1) || '% dead tuples, ' || ROUND(hours_since_autovacuum::numeric, 1) || ' hours since last vacuum)') as current_value,
        CASE
            WHEN dead_tuple_pct > 30 THEN 'CRITICAL'
            WHEN dead_tuple_pct > 20 THEN 'WARNING'
            WHEN dead_tuple_pct > 10 THEN 'INFO'
            ELSE 'OK'
        END as status,
        CASE
            WHEN dead_tuple_pct > 40 THEN 90
            WHEN dead_tuple_pct > 30 THEN 75
            WHEN dead_tuple_pct > 20 THEN 55
            WHEN dead_tuple_pct > 10 THEN 35
            ELSE 15
        END as risk_score,
        'Table ' || table_name || ' has ' || ROUND(dead_tuple_pct::numeric, 1) || '% dead tuples (outdated data). ' ||
        'Think of it like a document with lots of crossed-out text - the database has to read through all the crossed-out parts to find current data. ' ||
        'Last cleanup (vacuum) was ' || ROUND(hours_since_autovacuum::numeric, 1) || ' hours ago. ' ||
        CASE
            WHEN dead_tuple_pct > 30 THEN 'This is significantly slowing down queries and wasting storage.'
            WHEN dead_tuple_pct > 20 THEN 'This is causing noticeable performance degradation.'
            WHEN dead_tuple_pct > 10 THEN 'Minor performance impact, but should be addressed.'
            ELSE 'Normal levels - autovacuum is working effectively.'
        END as plain_english_explanation,
        CASE
            WHEN dead_tuple_pct > 30 THEN 'IMMEDIATE - Already causing performance issues'
            WHEN dead_tuple_pct > 20 AND avg_days_between_vacuums IS NOT NULL THEN
                'SHORT TERM - Will reach critical (30%) in ~' || ROUND((30 - dead_tuple_pct)::numeric / (dead_tuple_pct::numeric / NULLIF(avg_days_between_vacuums::numeric, 0))) || ' days'
            WHEN dead_tuple_pct > 10 THEN 'MEDIUM TERM - Monitor and tune autovacuum within 2-4 weeks'
            ELSE 'LONG TERM - Autovacuum working effectively'
        END as time_to_impact,
        CASE
            WHEN dead_tuple_pct > 30 THEN
                '1. IMMEDIATE: Run manual VACUUM on this table' || E'\\n' ||
                '2. Check autovacuum settings:' || E'\\n' ||
                '   - autovacuum_vacuum_scale_factor (default: 0.2)' || E'\\n' ||
                '   - autovacuum_vacuum_threshold (default: 50)' || E'\\n' ||
                '3. Consider table-specific autovacuum tuning:' || E'\\n' ||
                '   ALTER TABLE ' || table_name || ' SET (autovacuum_vacuum_scale_factor = 0.05);' || E'\\n' ||
                '4. Monitor dead tuple percentage daily' || E'\\n' ||
                '5. Review application for excessive UPDATE/DELETE operations'
            WHEN dead_tuple_pct > 20 THEN
                '1. Schedule manual VACUUM during off-peak hours' || E'\\n' ||
                '2. Review autovacuum_naptime (default: 1min)' || E'\\n' ||
                '3. Check if autovacuum workers are sufficient' || E'\\n' ||
                '4. Monitor vacuum frequency over next week' || E'\\n' ||
                '5. Consider lowering vacuum thresholds for this table'
            ELSE
                '1. Continue monitoring weekly' || E'\\n' ||
                '2. Current autovacuum settings are adequate' || E'\\n' ||
                '3. No action required'
        END as next_steps,
        CASE
            WHEN dead_tuple_pct > 30 THEN
                'CRITICAL: Autovacuum is not keeping up. Run manual VACUUM immediately and tune autovacuum settings. Dead tuple percentage (' || ROUND(dead_tuple_pct::numeric, 1) || '%) indicates vacuum frequency is too low.'
            WHEN dead_tuple_pct > 20 THEN
                'WARNING: High dead tuple percentage. Review and adjust autovacuum settings. Consider more aggressive vacuum schedule for this table.'
            WHEN dead_tuple_pct > 10 THEN
                'INFO: Moderate dead tuple percentage. Monitor vacuum effectiveness and consider minor tuning.'
            ELSE
                'OK: Autovacuum is working effectively. Dead tuple levels are normal.'
        END as recommendation,
        CASE
            WHEN '\$BLOAT_METHOD' = 'pgstattuple' AND dead_tuple_pct > 20 THEN 'HIGH (95%) - Accurate measurement via pgstattuple'
            WHEN dead_tuple_pct > 20 THEN 'MEDIUM (75%) - Stats-based estimation'
            ELSE 'MEDIUM (70%) - Stats-based estimation'
        END as confidence_level
    FROM autovacuum_status

    UNION ALL

    -- TOAST OID USAGE
    SELECT
        'TOAST OID Usage' as check_category,
        (table_name || ' TOAST: ' || toast_table_name || ' (OID: ' || current_oid || ', ' || oid_usage_pct || '% of OID space used)') as current_value,
        CASE
            WHEN oid_usage_pct >= 90 THEN 'EMERGENCY'
            WHEN oid_usage_pct >= 50 THEN 'CRITICAL'
            WHEN oid_usage_pct >= 25 THEN 'WARNING'
            ELSE 'INFO'
        END as status,
        CASE
            WHEN oid_usage_pct >= 90 THEN 100
            WHEN oid_usage_pct >= 75 THEN 95
            WHEN oid_usage_pct >= 50 THEN 85
            WHEN oid_usage_pct >= 25 THEN 60
            ELSE 20
        END as risk_score,
        'Table ' || table_name || ' is using ' || oid_usage_pct || '% of available TOAST OID space. ' ||
        'TOAST tables store large column values (like long text or binary data). Each row gets a unique ID number (OID), and there are only 4.3 billion available. ' ||
        CASE
            WHEN oid_usage_pct >= 90 THEN 'You''re almost out of IDs! This is like a parking garage that''s 90% full - new cars (data) will struggle to find a spot, causing severe slowdowns or failures.'
            WHEN oid_usage_pct >= 50 THEN 'You''re over halfway through available IDs. Like a phone number system running out of numbers, this will cause collisions and performance issues.'
            WHEN oid_usage_pct >= 25 THEN 'You''re using a quarter of available IDs. Not critical yet, but worth monitoring if this table grows quickly.'
            ELSE 'OID usage is normal. No concerns at this level.'
        END as plain_english_explanation,
        CASE
            WHEN oid_usage_pct >= 90 AND estimated_daily_inserts > 0 THEN
                'EMERGENCY - Estimated ' || ROUND((4294967295 - current_oid)::numeric / NULLIF(estimated_daily_inserts, 0)) || ' days until OID exhaustion'
            WHEN oid_usage_pct >= 50 AND estimated_daily_inserts > 0 THEN
                'CRITICAL - Estimated ' || ROUND((4294967295 - current_oid)::numeric / NULLIF(estimated_daily_inserts, 0)) || ' days until OID exhaustion'
            WHEN oid_usage_pct >= 25 AND estimated_daily_inserts > 0 THEN
                'WARNING - Estimated ' || ROUND((4294967295 - current_oid)::numeric / NULLIF(estimated_daily_inserts, 0)) || ' days until reaching 50% threshold'
            ELSE 'LONG TERM - Monitor quarterly'
        END as time_to_impact,
        CASE
            WHEN oid_usage_pct >= 90 THEN
                '1. EMERGENCY ACTION REQUIRED WITHIN 24-48 HOURS' || E'\\n' ||
                '2. Immediate options:' || E'\\n' ||
                '   a) Partition table immediately (recommended)' || E'\\n' ||
                '   b) Archive old data to separate table' || E'\\n' ||
                '   c) VACUUM FULL to reclaim OIDs (requires downtime)' || E'\\n' ||
                '3. Stop non-critical inserts if possible' || E'\\n' ||
                '4. Contact database administrator immediately' || E'\\n' ||
                '5. Plan for schema redesign to prevent recurrence'
            WHEN oid_usage_pct >= 50 THEN
                '1. URGENT: Schedule maintenance within 1-2 weeks' || E'\\n' ||
                '2. Implement table partitioning strategy' || E'\\n' ||
                '3. Archive historical data (older than 6-12 months)' || E'\\n' ||
                '4. Consider VACUUM FULL during maintenance window' || E'\\n' ||
                '5. Review data retention policies' || E'\\n' ||
                '6. Monitor OID growth daily'
            WHEN oid_usage_pct >= 25 THEN
                '1. Plan partitioning strategy within 1-3 months' || E'\\n' ||
                '2. Analyze table growth rate' || E'\\n' ||
                '3. Review data archival options' || E'\\n' ||
                '4. Set up monitoring alerts for 50% threshold' || E'\\n' ||
                '5. Document current growth patterns'
            ELSE
                '1. Monitor OID usage quarterly' || E'\\n' ||
                '2. No immediate action required' || E'\\n' ||
                '3. Set alert for 25% threshold'
        END as next_steps,
        CASE
            WHEN oid_usage_pct >= 90 THEN
                'EMERGENCY: Imminent OID exhaustion. Severe write performance degradation likely. Risk of application outage. Immediate partitioning/archiving required.'
            WHEN oid_usage_pct >= 50 THEN
                'CRITICAL: High OID collision risk causing degraded write performance. Immediate action required: partition table, VACUUM FULL, or schema redesign.'
            WHEN oid_usage_pct >= 25 THEN
                'WARNING: High-growth table detected - Review schema and estimate time to next threshold. Early insert contention may occur.'
            ELSE
                'TOAST OID usage is within normal range'
        END as recommendation,
        CASE
            WHEN oid_usage_pct >= 50 THEN 'HIGH (95%) - OID exhaustion is a known critical issue'
            WHEN oid_usage_pct >= 25 THEN 'HIGH (90%) - Growth pattern indicates future risk'
            ELSE 'MEDIUM (70%) - Informational monitoring'
        END as confidence_level
    FROM toast_oid_usage
) maintenance_metrics
ORDER BY
    CASE status
        WHEN 'EMERGENCY' THEN 0
        WHEN 'CRITICAL' THEN 1
        WHEN 'WARNING' THEN 2
        WHEN 'INFO' THEN 3
        ELSE 4
    END,
    check_category;"


# Add Critical Implementation Guidelines subsection
cat >> "$OUTPUT_FILE" << 'EOF'
<div class="table-container">
<h3 style="font-size: 0.85em; margin: 0; padding: 10px 15px 0 15px; color: #dc2626; font-weight: bold;">⚠️ CRITICAL IMPLEMENTATION GUIDELINES</h3>
<div style="background: linear-gradient(135deg, #fef2f2 0%, #fee2e2 100%); padding: 20px; border-radius: 8px; border-left: 4px solid #dc2626; margin: 15px 0; font-size: 0.8em; line-height: 1.5;">
    <div style="color: #7f1d1d; margin: 0; font-weight: 600;">All recommendations and suggestions presented in this health report must be thoroughly reviewed, analyzed for technical and business impact, and validated in non-production environments before implementation in production systems.</div>
    
    <div style="color: #7f1d1d; margin-top: 12px; font-weight: 600;">Critical and warning indicators require comprehensive understanding of root causes, potential remediation impacts, and proper change management procedures before executing any corrective actions on production databases.</div>
</div>
</div>
EOF

echo '</div>' >> "$OUTPUT_FILE"
echo '</div>' >> "$OUTPUT_FILE"

# Capture end time for report completion tracking
END_TIME=$(date '+%d-%b-%y %H:%M:%S')
END_XID=$(PGPASSWORD="$PGPASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -t -c "SELECT txid_snapshot_xmax(txid_current_snapshot());" 2>/dev/null | xargs)
END_EPOCH=$(date +%s)
DURATION_SECONDS=$((END_EPOCH - START_EPOCH))

# Calculate duration in human-readable format
DURATION_MINUTES=$((DURATION_SECONDS / 60))
DURATION_REMAINING_SECONDS=$((DURATION_SECONDS % 60))

if [ $DURATION_MINUTES -gt 0 ]; then
    DURATION_DISPLAY="${DURATION_MINUTES}m ${DURATION_REMAINING_SECONDS}s"
else
    DURATION_DISPLAY="${DURATION_SECONDS}s"
fi

# HTML Footer with error count summary
cat >> "$OUTPUT_FILE" << EOF
        <div class="footer">
            <div style="margin-bottom: 15px; padding: 15px; background: rgba(255,255,255,0.2); border-radius: 8px;">
                <strong style="font-size: 1.1em;">Report Generation Summary</strong><br>
                <div style="margin-top: 10px; font-size: 0.95em;">
                    <strong>Completed:</strong> $END_TIME (XID = $END_XID)<br>
                    <strong>Duration:</strong> $DURATION_DISPLAY<br>
                    <strong>Queries Failed:</strong> <span style="color: $([ $ERROR_COUNT -eq 0 ] && echo '#27ae60' || echo '#e74c3c'); font-weight: bold;">$ERROR_COUNT</span>
                    $([ $ERROR_COUNT -gt 0 ] && echo "<br><em style='color: #856404;'>⚠️ Some queries encountered errors. Review error messages above for details.</em>" || echo "<br><em style='color: #155724;'>✅ All queries executed successfully.</em>")
                </div>
            </div>
            <div class="disclaimer">
                <strong style="color: navy; font-weight: bold; font-size: 1.05em;">NOTICE:</strong> <span style="color: #2c3e50; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;">All recommendations should be validated in a non-production environment prior to production deployment.</span>
            </div>
        </div>
    </div>
    <script>
        function toggleAll() {
            const btn = document.getElementById('masterToggle');
            const checkboxes = document.querySelectorAll('.accordion-checkbox');
            const isExpanded = btn.classList.contains('expanded');
            
            checkboxes.forEach(cb => cb.checked = !isExpanded);
            btn.classList.toggle('expanded');
        }
        
        // Navigation card click-to-toggle functionality
        document.addEventListener('DOMContentLoaded', function() {
            // Update end time and duration in header
            const endTimeElement = document.getElementById('end-time');
            const durationElement = document.getElementById('duration');
            if (endTimeElement) {
                endTimeElement.textContent = '$END_TIME (XID = $END_XID)';
            }
            if (durationElement) {
                durationElement.textContent = '$DURATION_DISPLAY';
            }
            
            document.querySelectorAll('.nav-card').forEach(card => {
                card.addEventListener('click', function(e) {
                    e.preventDefault();
                    
                    // Get target section ID
                    const sectionId = this.getAttribute('href').substring(1);
                    const checkbox = document.getElementById(sectionId + '-toggle');
                    
                    // Toggle main section
                    checkbox.checked = !checkbox.checked;
                    
                    // Toggle all sub-sections within this section
                    const sectionContent = document.querySelector(\`#\${sectionId} + .accordion-content\`);
                    if (sectionContent) {
                        const subCheckboxes = sectionContent.querySelectorAll('.accordion-checkbox');
                        subCheckboxes.forEach(subCheckbox => {
                            subCheckbox.checked = checkbox.checked;
                        });
                    }
                    
                    // Scroll to section
                    document.getElementById(sectionId).scrollIntoView({ behavior: 'smooth' });
                });
            });
        });
    </script>
</body>
</html>
EOF

echo ""
echo "=============================================================================="
echo "                    Report Generation Complete"
echo "=============================================================================="
echo ""
echo "📊 Execution Summary:"
echo "  • Report File: $OUTPUT_FILE"
echo "  • Start Time: $START_TIME"
echo "  • End Time: $END_TIME (XID = $END_XID)"
echo "  • Total Duration: $DURATION_DISPLAY"
echo ""
echo "📈 Query Statistics:"
echo "  • Total Queries Executed: $QUERY_COUNT"
echo "  • Successfully Completed: $COMPLETED_QUERY_COUNT"
echo "  • Failed Queries: $ERROR_COUNT"
if [ $QUERY_COUNT -gt 0 ]; then
    SUCCESS_RATE=$(awk "BEGIN {printf \"%.1f\", ($COMPLETED_QUERY_COUNT / $QUERY_COUNT) * 100}")
    echo "  • Success Rate: ${SUCCESS_RATE}%"
fi
echo ""
if [ $ERROR_COUNT -gt 0 ]; then
    echo "⚠️  WARNING: $ERROR_COUNT queries encountered errors during execution."
    echo "   Review the HTML report for detailed error messages and context."
    echo "   Look for red error boxes in the report sections."
else
    echo "✅ All queries executed successfully!"
    echo "   The database health report is complete and ready for review."
fi
echo ""
echo "📄 Next Steps:"
echo "  1. Open the HTML report in your web browser"
echo "  2. Review each section for health metrics and recommendations"
echo "  3. Pay special attention to any CRITICAL or WARNING indicators"
echo "  4. Implement recommended optimizations in a test environment first"
echo ""
echo "=============================================================================="
