#!/bin/sh
set -e

R2_MOUNT="/mnt/compile-cache"
LOCAL_FALLBACK="/tmp/loh_compile_cache"

install -d -m 0700 "$R2_MOUNT"

if [ -n "$R2_ACCOUNT_ID" ] \
    && [ -n "$AWS_ACCESS_KEY_ID" ] \
    && [ -n "$AWS_SECRET_ACCESS_KEY" ] \
    && [ -n "$R2_BUCKET_NAME" ]; then
    R2_ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
    echo "Mounting R2 bucket ${R2_BUCKET_NAME} at ${R2_MOUNT} ..."
    /usr/local/bin/tigrisfs \
        --endpoint "$R2_ENDPOINT" \
        --log-level warn \
        --stat-cache-ttl 30s \
        --fsync-on-close \
        --no-specials \
        --uid 0 \
        --gid 0 \
        --file-mode 0600 \
        --dir-mode 0700 \
        -f "$R2_BUCKET_NAME" "$R2_MOUNT" &
    TIGRIS_PID=$!

    attempts=0
    while [ $attempts -lt 20 ]; do
        if mountpoint -q "$R2_MOUNT" 2>/dev/null; then
            echo "R2 bucket mounted successfully."
            break
        fi
        sleep 0.5
        attempts=$((attempts + 1))
    done

    if mountpoint -q "$R2_MOUNT" 2>/dev/null; then
        export COMPILE_CACHE_DIRECTORY="$R2_MOUNT"
    else
        echo "ERROR: R2 mount failed — killing tigrisfs, using local cache."
        kill "$TIGRIS_PID" 2>/dev/null || true
        wait "$TIGRIS_PID" 2>/dev/null || true
        install -d -m 0700 "$LOCAL_FALLBACK"
        export COMPILE_CACHE_DIRECTORY="$LOCAL_FALLBACK"
    fi
else
    echo "R2 credentials not set — using local compile cache."
    install -d -m 0700 "$LOCAL_FALLBACK"
    export COMPILE_CACHE_DIRECTORY="$LOCAL_FALLBACK"
fi

# tigrisfs inherited its own copy at launch. Remove all storage credentials
# before Gunicorn starts so neither the API process nor submitted TeX can read
# them from the process environment.
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY R2_ACCOUNT_ID R2_BUCKET_NAME

exec poetry run gunicorn \
    --workers=1 \
    --threads=4 \
    --timeout=60 \
    --bind=0.0.0.0:8080 \
    app:app
