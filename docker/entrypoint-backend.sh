#!/bin/sh
set -e


for dir in /app/data/data_stores /app/data/personalised_pages /app/memory /app/logs /app/ios/Server /app/prompts /app/skills; do
    mkdir -p "$dir"
done


for dir in /app/data /app/memory /app/logs /app/ios/Server /app/prompts /app/skills; do
    if [ "$(stat -c '%u:%g' "$dir")" != "1500:1500" ]; then
        chown -R hime:hime "$dir"
    fi
done

exec gosu hime "$@"
